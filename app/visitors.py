"""Visitor passes: a PIN and/or QR code in UniFi Access for chosen doors and times, emailed to the
visitor, with a list of today's visitors (expected / here / gone)."""
import logging
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import Response
from sqlmodel import Session, select

from . import audit, engine, mailer, unifi
from .auth import allowed_door_ids, managed_site_ids
from .db import get_session
from .models import Door, DoorEvent, Site, User, Visitor, utcnow
from .schedules import to_local, to_utc
from .web import current_site_id, flash, redirect, render, require_scheduler

log = logging.getLogger(__name__)
router = APIRouter()

STATES = {
    "expected": ("Expected", "def"),
    "here": ("Here", "locked"),
    "gone": ("Gone", "closed"),
    "cancelled": ("Cancelled", "closed"),
    "problem": ("Problem", "bad"),
}


# ---------- helpers ----------

def _doors_for(session: Session, user: User) -> list[Door]:
    allowed = allowed_door_ids(session, user)
    return [d for d in session.exec(select(Door).where(Door.present == True, Door.hidden == False)  # noqa: E712
                                    .order_by(Door.name)).all() if d.id in allowed]


def _site_ids(session: Session, user: User) -> set[int]:
    """Sites whose visitors this person can see: any site they can open a door at, or manage."""
    return {d.site_id for d in _doors_for(session, user)} | managed_site_ids(session, user)


def _get(session: Session, user: User, vid: int) -> Optional[Visitor]:
    v = session.get(Visitor, vid)
    return v if v and v.site_id in _site_ids(session, user) else None


def arrivals(session: Session, visitors: list[Visitor]) -> dict[int, datetime]:
    """When each visitor first got in, from the door log (their name, during their pass)."""
    if not visitors:
        return {}
    lo = min(v.start for v in visitors) - timedelta(hours=1)
    hi = max(v.end for v in visitors)
    rows = session.exec(select(DoorEvent).where(DoorEvent.at >= lo, DoorEvent.at <= hi,
                                                DoorEvent.granted == True)  # noqa: E712
                        .order_by(DoorEvent.at)).all()
    out = {}
    for v in visitors:
        name = v.name.lower()
        for e in rows:
            if e.site_id == v.site_id and (e.who or "").lower() == name and v.start - timedelta(hours=1) <= e.at <= v.end:
                out[v.id] = e.at
                break
    return out


def state(v: Visitor, arrived: Optional[datetime], now: Optional[datetime] = None) -> str:
    now = now or utcnow()
    if v.status == "cancelled":
        return "cancelled"
    if v.error and not v.unifi_id:
        return "problem"
    if v.checked_out_at or now > v.end:
        return "gone"
    if v.checked_in_at or arrived:
        return "here"
    return "expected"


def _door_names(session: Session, v: Visitor) -> list[str]:
    return [d.name for d in (session.get(Door, i) for i in v.doors) if d]


def _when(session: Session, v: Visitor) -> tuple[datetime, datetime, object]:
    tz = engine.site_tz(session.get(Site, v.site_id))
    return to_local(v.start, tz), to_local(v.end, tz), tz


def _t(d: datetime) -> str:
    return d.strftime("%-I:%M %p").replace(":00 ", " ")


def pass_email(session: Session, v: Visitor) -> tuple[str, str, str, dict]:
    """Subject, plain text, html and inline images for a visitor's pass."""
    site = session.get(Site, v.site_id)
    s, e, _ = _when(session, v)
    when = f"{s.strftime('%A, %B')} {s.day}, {_t(s)} to {_t(e)}" if s.date() == e.date() else \
        f"{s.strftime('%b')} {s.day} {_t(s)} to {e.strftime('%b')} {e.day} {_t(e)}"
    doors = ", ".join(_door_names(session, v))
    images = {}
    if v.use_qr and v.unifi_id:
        try:
            images["pass"] = unifi.get_client(site).visitor_qr_png(v.unifi_id)
        except unifi.UnifiError as err:
            log.warning("QR code for visitor %s: %s", v.id, err)
    lines = [f"Hi {v.first_name},", "", f"You're expected at {site.name} on {when}."]
    if v.host_name:
        lines.append(f"You're here to see {v.host_name}.")
    lines.append("")
    if v.pin:
        lines.append(f"Your door PIN: {v.pin}  (type it on the reader's keypad)")
    if images:
        lines.append("Your QR code is attached. Hold it up to the reader's camera.")
    lines += [f"It opens: {doors}", "It only works during your visit.", "", "See you soon."]
    text = "\n".join(lines)
    html = f"""<div style="font-family:Arial,sans-serif;font-size:15px;color:#16191f;max-width:520px">
<p>Hi {escape(v.first_name)},</p>
<p>You're expected at <strong>{escape(site.name)}</strong> on <strong>{escape(when)}</strong>.
{f'You are here to see {escape(v.host_name)}.' if v.host_name else ''}</p>
{f'<p style="font-size:13px;color:#646b78;margin:20px 0 4px">YOUR DOOR PIN</p><p style="font-size:32px;font-weight:bold;letter-spacing:6px;margin:0">{escape(v.pin)}</p><p style="color:#646b78">Type it on the keypad at the door.</p>' if v.pin else ''}
{'<p style="font-size:13px;color:#646b78;margin:20px 0 4px">YOUR QR CODE</p><img src="cid:pass" width="220" height="220" alt="QR code"><p style="color:#646b78">Hold it up to the camera on the door reader.</p>' if images else ''}
<p>It opens: {escape(doors)}<br>It only works during your visit.</p>
</div>"""
    return f"Your visit to {site.name}: {s.strftime('%b')} {s.day}", text, html, images


def email_pass(session: Session, v: Visitor) -> None:
    subject, text, html, images = pass_email(session, v)
    mailer.send(mailer.settings(session), v.email, subject, text, html, images)
    v.emailed_at = utcnow()
    session.add(v)


def tell_host(session: Session, v: Visitor) -> None:
    """Email the host that their visitor checked in (if email is set up and the host has an address)."""
    host = session.get(User, v.host_id) if v.host_id else None
    if not host or not host.email or not mailer.configured(session):
        return
    try:
        mailer.send(mailer.settings(session), host.email, f"{v.name} has arrived",
                    f"{v.name}{' from ' + v.company if v.company else ''} just checked in at the front.")
    except mailer.MailError as e:
        log.warning("check-in email: %s", e)


# ---------- pages ----------

@router.get("/visitors")
def visitors_page(request: Request, user: User = Depends(require_scheduler), session: Session = Depends(get_session)):
    sites = _site_ids(session, user)
    pick = current_site_id(request, session, user)
    if pick:
        sites &= {pick}
    now = utcnow()
    rows = session.exec(select(Visitor).where(Visitor.end >= now - timedelta(days=8),
                                              Visitor.start <= now + timedelta(days=60))
                        .order_by(Visitor.start)).all()
    rows = [v for v in rows if v.site_id in sites]
    got = arrivals(session, rows)
    tzs = {x.id: engine.site_tz(x) for x in session.exec(select(Site)).all()}
    today, upcoming, past = [], [], []
    for v in rows:
        tz = tzs.get(v.site_id)
        local_today = to_local(now, tz).date()
        s, e = to_local(v.start, tz), to_local(v.end, tz)
        item = {"v": v, "s": s, "e": e, "state": state(v, got.get(v.id), now),
                "arrived": to_local(got[v.id], tz) if v.id in got else None}
        if s.date() <= local_today <= e.date():
            today.append(item)
        elif s.date() > local_today:
            upcoming.append(item)
        else:
            past.append(item)
    past.reverse()
    counts = {k: sum(1 for i in today if i["state"] == k) for k in STATES}
    return render(request, "visitors.html", user, today=today, upcoming=upcoming, past=past, counts=counts,
                  states=STATES, mail_ready=mailer.configured(session))


def _form(request: Request, user: User, session: Session, values: dict, status: int = 200):
    doors = _doors_for(session, user)
    pick = current_site_id(request, session, user)
    if pick and any(d.site_id == pick for d in doors):  # the site picked in the sidebar
        doors = [d for d in doors if d.site_id == pick]
    site_ids = {d.site_id for d in doors}
    sites = {x.id: x for x in session.exec(select(Site)).all() if x.id in site_ids}
    resp = render(request, "visitor_form.html", user, doors=doors, sites=sites, f=values,
                  mail_ready=mailer.configured(session))
    resp.status_code = status
    return resp


@router.get("/visitors/new")
def visitor_new(request: Request, user: User = Depends(require_scheduler), session: Session = Depends(get_session)):
    now = to_local(utcnow(), None)
    start = (now + timedelta(minutes=60 - now.minute)).replace(second=0, microsecond=0)
    if start.hour >= 17 or start.hour < 7:
        start = datetime.combine(now.date() + timedelta(days=start.hour >= 17), time(9, 0))
    return _form(request, user, session, {
        "date": start.date().isoformat(), "start": start.strftime("%H:%M"),
        "end": (start + timedelta(hours=2)).strftime("%H:%M"), "host": user.display, "creds": "both",
        "send": True, "doors": [], "tell_me": True})


@router.post("/visitors/new")
async def visitor_create(request: Request, bg: BackgroundTasks, user: User = Depends(require_scheduler),
                         session: Session = Depends(get_session)):
    form = await request.form()
    f = {k: str(form.get(k, "")).strip() for k in ("first_name", "last_name", "email", "phone", "company",
                                                    "reason", "host", "date", "start", "end", "creds")}
    f["doors"] = [str(x) for x in form.getlist("doors")]
    f["send"] = bool(form.get("send"))
    f["tell_me"] = bool(form.get("tell_me"))
    allowed = {d.id: d for d in _doors_for(session, user)}
    picked = [allowed[d] for d in f["doors"] if d in allowed]

    def again(msg: str):
        flash(request, msg, "error")
        return _form(request, user, session, f, 400)

    if not f["first_name"]:
        return again("Add the visitor's name.")
    if not picked:
        return again("Pick at least one door they can open.")
    if len({d.site_id for d in picked}) > 1:
        return again("Pick doors at one site. A pass belongs to one UniFi Access console.")
    if f["send"] and not f["email"]:
        return again("Add their email address, or untick “Email the pass”.")
    site = session.get(Site, picked[0].site_id)
    tz = engine.site_tz(site)
    try:
        day = date.fromisoformat(f["date"])
        s = datetime.combine(day, time.fromisoformat(f["start"]))
        e = datetime.combine(day, time.fromisoformat(f["end"]))
    except ValueError:
        return again("Check the date and times.")
    if e <= s:
        e += timedelta(days=1)  # an evening visit that ends after midnight
    start_utc, end_utc = to_utc(s, tz), to_utc(e, tz)
    if end_utc <= utcnow():
        return again("That visit is already over. Pick a time in the future.")
    use_pin, use_qr = f["creds"] in ("pin", "both"), f["creds"] in ("qr", "both")
    v = Visitor(site_id=site.id, first_name=f["first_name"], last_name=f["last_name"], email=f["email"],
                phone=f["phone"], company=f["company"], reason=f["reason"], host_name=f["host"],
                host_id=user.id if f["tell_me"] else None, start=start_utc, end=end_utc,
                door_ids=",".join(d.id for d in picked), use_pin=use_pin, use_qr=use_qr, created_by=user.id)
    client = unifi.get_client(site)
    remote = [d.remote_id for d in picked]
    try:
        v.unifi_id = client.create_visitor(v.first_name, v.last_name, v.email, v.phone, v.company,
                                           v.reason or f"Pass from OpnDoor ({user.display})",
                                           _epoch(start_utc), _epoch(end_utc),
                                           remote)
        if use_pin:
            v.pin = client.new_pin()
            client.set_visitor_pin(v.unifi_id, v.pin)
        if use_qr:
            client.set_visitor_qr(v.unifi_id)
    except unifi.UnifiError as err:
        if v.unifi_id:  # don't leave a half-made visitor on the console
            try:
                client.delete_visitor(v.unifi_id)
            except unifi.UnifiError:
                pass
        return again(f"UniFi Access didn't take the pass: {err}")
    session.add(v)
    session.flush()
    audit.record(session, "visitor.create", f"Visitor pass for {v.name} ({s.strftime('%b')} {s.day}, {_t(s)}–{_t(e)})",
                 user=user, door_ids=[d.id for d in picked])
    session.commit()
    msg = f"Pass made for {v.name}."
    if f["send"]:
        try:
            email_pass(session, v)
            session.commit()
            msg += f" Emailed to {v.email}."
        except mailer.MailError as err:
            flash(request, f"The pass is ready, but it wasn't emailed. {err}", "error")
    flash(request, msg)
    return redirect(f"/visitors/{v.id}")


def _epoch(utc_naive: datetime) -> int:
    return int(utc_naive.replace(tzinfo=timezone.utc).timestamp())


@router.get("/visitors/{vid}")
def visitor_page(request: Request, vid: int, user: User = Depends(require_scheduler),
                 session: Session = Depends(get_session)):
    v = _get(session, user, vid)
    if not v:
        flash(request, "That visitor isn't available to you.", "error")
        return redirect("/visitors")
    got = arrivals(session, [v])
    s, e, tz = _when(session, v)
    return render(request, "visitor.html", user, v=v, s=s, e=e, site=session.get(Site, v.site_id),
                  state=state(v, got.get(v.id)), states=STATES, doors=_door_names(session, v),
                  arrived=to_local(got[v.id], tz) if v.id in got else None,
                  checked_in=to_local(v.checked_in_at, tz) if v.checked_in_at else None,
                  checked_out=to_local(v.checked_out_at, tz) if v.checked_out_at else None,
                  mail_ready=mailer.configured(session))


_qr_cache: dict = {}


@router.get("/visitors/{vid}/qr.png")
def visitor_qr(vid: int, user: User = Depends(require_scheduler), session: Session = Depends(get_session)):
    v = _get(session, user, vid)
    if not v or not v.use_qr or not v.unifi_id:
        return Response(status_code=404)
    if v.unifi_id not in _qr_cache:
        try:
            _qr_cache[v.unifi_id] = unifi.get_client(session.get(Site, v.site_id)).visitor_qr_png(v.unifi_id)
        except unifi.UnifiError:
            return Response(status_code=404)
    return Response(_qr_cache[v.unifi_id], media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@router.post("/visitors/{vid}/send")
def visitor_send(request: Request, vid: int, user: User = Depends(require_scheduler),
                 session: Session = Depends(get_session)):
    v = _get(session, user, vid)
    if not v or not v.email:
        flash(request, "There's no email address for this visitor.", "error")
        return redirect(f"/visitors/{vid}" if v else "/visitors")
    try:
        email_pass(session, v)
        audit.record(session, "visitor.send", f"Emailed the pass to {v.name}", user=user, door_ids=v.doors)
        session.commit()
        flash(request, f"Pass emailed to {v.email}.")
    except mailer.MailError as e:
        flash(request, str(e), "error")
    return redirect(f"/visitors/{vid}")


@router.post("/visitors/{vid}/cancel")
def visitor_cancel(request: Request, vid: int, user: User = Depends(require_scheduler),
                   session: Session = Depends(get_session)):
    v = _get(session, user, vid)
    if not v:
        return redirect("/visitors")
    if v.unifi_id:
        try:
            unifi.get_client(session.get(Site, v.site_id)).delete_visitor(v.unifi_id)
        except unifi.UnifiError as e:
            if "not" not in str(e).lower():  # already gone on the console is fine
                flash(request, f"UniFi Access didn't remove the pass: {e}", "error")
                return redirect(f"/visitors/{vid}")
    v.status = "cancelled"
    session.add(v)
    audit.record(session, "visitor.cancel", f"Cancelled the pass for {v.name}", user=user, door_ids=v.doors)
    session.commit()
    flash(request, f"Pass for {v.name} cancelled. Their PIN and QR code no longer work.")
    return redirect("/visitors")


@router.post("/visitors/{vid}/check")
async def visitor_check(request: Request, vid: int, user: User = Depends(require_scheduler),
                        session: Session = Depends(get_session)):
    v = _get(session, user, vid)
    if not v:
        return redirect("/visitors")
    what = (await request.form()).get("what", "in")
    if what == "in":
        v.checked_in_at, v.checked_out_at = utcnow(), None
    elif what == "out":
        v.checked_out_at = utcnow()
    else:
        v.checked_in_at = v.checked_out_at = None
    session.add(v)
    session.commit()
    back = request.headers.get("referer", "")
    return redirect(f"/visitors/{vid}" if f"/visitors/{vid}" in back else "/visitors")
