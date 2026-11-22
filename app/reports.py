"""Reports: a daily, weekly or monthly email summary for the people someone picked."""
import logging
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from . import audit, config, engine, mailer
from .auth import can_manage_person, managed_site_ids
from .db import get_session, session_scope
from .models import AlertSent, AuditLog, Door, DoorEvent, Report, Site, User, Visitor, utcnow
from .schedules import expand, now_local, to_local, to_utc
from .web import flash, redirect, render, require_manager, templates

log = logging.getLogger(__name__)
router = APIRouter()

SECTIONS = {
    "activity": ("Door activity", "Openings, turned-away attempts, busiest doors and times."),
    "visitors": ("Visitors", "Passes made, who came and who didn't."),
    "alerts": ("Alerts", "Alerts that went out in the period."),
    "devices": ("Devices", "Anything offline when the report was made."),
    "changes": ("Changes", "Schedules, people and settings that changed, and who changed them."),
    "upcoming": ("Coming up", "Events and closures in the next 7 days."),
}
FREQ = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _ids(csv: str) -> list:
    return [x for x in (csv or "").split(",") if x]


def period(rep: Report, today: Optional[date] = None) -> tuple[date, date, str]:
    """[start, end) dates covered by the report sent today, and a label."""
    today = today or now_local().date()
    if rep.frequency == "daily":
        s = today - timedelta(days=1)
        return s, today, f"{s.strftime('%A, %B')} {s.day}"
    if rep.frequency == "monthly":
        e = today.replace(day=1)
        s = (e - timedelta(days=1)).replace(day=1)
        return s, e, s.strftime("%B %Y")
    s = today - timedelta(days=7)
    last = today - timedelta(days=1)
    return s, today, f"{s.strftime('%b')} {s.day} – {last.strftime('%b')} {last.day}"


def _sites(session: Session, rep: Report) -> list[Site]:
    owner = session.get(User, rep.created_by) if rep.created_by else None
    allowed = managed_site_ids(session, owner) if owner else set()
    return [s for s in session.exec(select(Site).order_by(Site.name)).all()
            if s.id in allowed and (rep.site_id is None or s.id == rep.site_id)]


def build(session: Session, rep: Report, today: Optional[date] = None) -> dict:
    """Everything the report shows."""
    s_day, e_day, label = period(rep, today)
    tz = config.TZ
    lo, hi = to_utc(datetime.combine(s_day, datetime.min.time()), tz), to_utc(datetime.combine(e_day, datetime.min.time()), tz)
    sites = _sites(session, rep)
    site_ids = {s.id for s in sites}
    secs = set(_ids(rep.sections))
    out = {"rep": rep, "label": label, "sites": sites, "secs": secs, "freq": FREQ.get(rep.frequency, "")}
    if "activity" in secs:
        rows = [e for e in session.exec(select(DoorEvent).where(DoorEvent.at >= lo, DoorEvent.at < hi)).all()
                if e.site_id in site_ids]
        ok = [e for e in rows if e.granted]
        tzs = {s.id: engine.site_tz(s) for s in sites}
        hours = Counter(to_local(e.at, tzs.get(e.site_id)).hour for e in ok)
        days = Counter(to_local(e.at, tzs.get(e.site_id)).date() for e in ok)
        bh = hours.most_common(1)[0][0] if hours else None
        bd = days.most_common(1)[0] if days else None
        denied_doors = Counter(e.door_name for e in rows if not e.granted)
        out["activity"] = {
            "opened": len(ok), "denied": len(rows) - len(ok),
            "doors": Counter(e.door_name or "Unknown door" for e in ok).most_common(6),
            "denied_doors": denied_doors.most_common(4),
            "people": len({e.who for e in ok if e.who}),
            "busy_hour": None if bh is None else f"{bh % 12 or 12} {'AM' if bh < 12 else 'PM'}",
            "busy_day": None if not bd else (f"{bd[0].strftime('%a %b')} {bd[0].day}", bd[1]),
        }
    if "visitors" in secs:
        from .visitors import arrivals
        vs = [v for v in session.exec(select(Visitor).where(Visitor.start < hi, Visitor.end >= lo)).all()
              if v.site_id in site_ids and v.status != "cancelled"]
        got = arrivals(session, vs)
        came = [v for v in vs if v.checked_in_at or v.id in got]
        out["visitors"] = {"total": len(vs), "came": len(came),
                           "noshow": [v for v in vs if v not in came and v.end < utcnow()][:10],
                           "list": [(to_local(v.start, config.TZ), v, v in came) for v in sorted(vs, key=lambda v: v.start)][:15]}
    if "alerts" in secs:
        from .models import AlertRule
        mine = {r.id: r.name for r in session.exec(select(AlertRule)).all() if r.site_id in site_ids or r.site_id is None}
        sent = [a for a in session.exec(select(AlertSent).where(AlertSent.at >= lo, AlertSent.at < hi)).all()
                if a.rule_id in mine and not a.error.startswith("Outside")]
        out["alerts"] = {"total": len(sent), "top": Counter(a.subject for a in sent).most_common(8)}
    if "devices" in secs:
        from .devices import reliable, site_devices
        down = []
        for s in sites:
            info = site_devices(s)
            if not info.get("console_ok"):
                down.append((s.name, "UniFi Access console"))
            down += [(s.name, d["name"]) for d in info["access"] if d["online"] is False and reliable(d)]
            down += [(s.name, c["name"]) for c in info["cameras"] if (c.get("state") or "").upper() not in ("", "CONNECTED")]
        out["devices"] = {"down": down}
    if "changes" in secs:
        rows = session.exec(select(AuditLog).where(AuditLog.at >= lo, AuditLog.at < hi, AuditLog.category != "door",
                                                   AuditLog.category != "signin")).all()
        rows = [a for a in rows if not a.site_ids or site_ids & {int(x) for x in a.site_ids.split(",") if x.isdigit()}]
        out["changes"] = {"total": len(rows), "kinds": Counter(audit.label(a.action) for a in rows).most_common(8),
                          "people": Counter(a.actor for a in rows).most_common(5)}
    if "upcoming" in secs:
        doors = [d for d in session.exec(select(Door).where(Door.present == True)).all() if d.site_id in site_ids]  # noqa: E712
        names = {d.id: d.name for d in doors}
        scheds = engine.door_schedules(session, [d.id for d in doors])
        seen, items = set(), []
        now = now_local()
        for door_id, ss in scheds.items():
            for o in expand([x for x in ss if x.kind == "override"], now, now + timedelta(days=7)):
                k = (o.schedule.id, o.start)
                if k in seen:
                    continue
                seen.add(k)
                items.append({"start": o.start, "end": o.end, "label": o.schedule.label, "lock": o.schedule.action == "lock",
                              "doors": [names[d] for d, x in scheds.items() if o.schedule in x and d in names]})
        items.sort(key=lambda i: i["start"])
        out["upcoming"] = {"total": len(items), "rows": items[:12]}
    return out


def render_html(session: Session, rep: Report, logo_src: str, today: Optional[date] = None) -> tuple[str, dict]:
    data = build(session, rep, today)
    from .admin import brand
    html = templates.get_template("report_email.html").render(d=data, sections=SECTIONS, logo_src=logo_src,
                                                              brand=brand(), base=config.PUBLIC_URL)
    return html, data


def recipients(session: Session, rep: Report) -> list[str]:
    out = []
    for uid in _ids(rep.user_ids):
        u = session.get(User, int(uid)) if uid.isdigit() else None
        if u and u.active and "@" in (u.email or ""):
            out.append(u.email)
    out += [e.strip() for e in (rep.emails or "").split(",") if "@" in e]
    return sorted(set(out))


def send(session: Session, rep: Report, today: Optional[date] = None) -> str:
    """Send one report now. Returns '' or what went wrong."""
    to = recipients(session, rep)
    if not to:
        return "Nobody to send it to."
    if not mailer.configured(session):
        return "Email isn't set up (Settings → Email)."
    from .admin import logo_png
    logo = logo_png()
    html, data = render_html(session, rep, "cid:logo" if logo else "", today)
    text = f"{rep.name}: {data['label']}. Open this email in a mail app that shows pictures and colours to see the report."
    cfg = mailer.settings(session)
    errors = []
    for addr in to:
        try:
            mailer.send(cfg, addr, f"{rep.name}: {data['label']}", text, html, {"logo": logo} if logo else None)
        except mailer.MailError as e:
            errors.append(str(e))
    return "; ".join(sorted(set(errors)))


def due(rep: Report, now: datetime) -> bool:
    if not rep.enabled or now.hour != rep.hour:
        return False
    if rep.last_sent_at and to_local(rep.last_sent_at, config.TZ).date() == now.date():
        return False
    if rep.frequency == "weekly":
        return now.weekday() == rep.weekday
    if rep.frequency == "monthly":
        return now.day == 1
    return True


def send_due() -> None:
    now = now_local()
    with session_scope() as s:
        for rep in s.exec(select(Report).where(Report.enabled == True)).all():  # noqa: E712
            if due(rep, now):
                try:
                    rep.last_error = send(s, rep)
                except Exception as e:  # noqa: BLE001
                    log.exception("report %s", rep.id)
                    rep.last_error = str(e)[:300]
                rep.last_sent_at = utcnow()
                s.add(rep)
                s.commit()


# ---------- pages ----------

def _people(session: Session, user: User) -> list[User]:
    return [u for u in session.exec(select(User).order_by(User.name)).all()
            if u.active and "@" in (u.email or "") and (u.id == user.id or can_manage_person(session, user, u))]


def _mine(session: Session, user: User, rid: int) -> Optional[Report]:
    r = session.get(Report, rid)
    if not r:
        return None
    ok = user.is_admin or r.created_by == user.id or (r.site_id and r.site_id in managed_site_ids(session, user))
    return r if ok else None


def when_text(r: Report) -> str:
    h = f"{r.hour % 12 or 12} {'AM' if r.hour < 12 else 'PM'}"
    if r.frequency == "daily":
        return f"Every day at {h}"
    if r.frequency == "monthly":
        return f"On the 1st of each month at {h}"
    return f"Every {DAYS[r.weekday]} at {h}"


@router.get("/reports")
def reports_page(request: Request, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    reps = [r for r in session.exec(select(Report).order_by(Report.name)).all() if _mine(session, user, r.id)]
    names = {s.id: s.name for s in session.exec(select(Site)).all()}
    users = {u.id: u.display for u in session.exec(select(User)).all()}
    info = {r.id: ", ".join([users[int(u)] for u in _ids(r.user_ids) if u.isdigit() and int(u) in users]
                            + [e.strip() for e in r.emails.split(",") if e.strip()]) for r in reps}
    return render(request, "reports.html", user, reps=reps, site_names=names, info=info, when_text=when_text,
                  sections=SECTIONS, mail_ready=mailer.configured(session))


def _form(request: Request, user: User, session: Session, r: Report):
    sites = [s for s in session.exec(select(Site).order_by(Site.name)).all() if s.id in managed_site_ids(session, user)]
    return render(request, "report_form.html", user, r=r, sites=sites, people=_people(session, user),
                  picked_users=_ids(r.user_ids), picked=_ids(r.sections), sections=SECTIONS, freq=FREQ, days=DAYS,
                  mail_ready=mailer.configured(session))


@router.get("/reports/new")
def report_new(request: Request, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    return _form(request, user, session, Report(name="", user_ids=str(user.id)))


@router.get("/reports/{rid}")
def report_edit(request: Request, rid: int, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    r = _mine(session, user, rid)
    return _form(request, user, session, r) if r else redirect("/reports")


@router.post("/reports/save")
async def report_save(request: Request, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    f = await request.form()
    rid = str(f.get("id", ""))
    r = _mine(session, user, int(rid)) if rid.isdigit() else Report(name="", created_by=user.id)
    if r is None:
        return redirect("/reports")
    site = str(f.get("site_id", ""))
    r.site_id = int(site) if site.isdigit() and int(site) in managed_site_ids(session, user) else None
    r.frequency = str(f.get("frequency", "weekly")) if f.get("frequency") in FREQ else "weekly"
    wd, hr = str(f.get("weekday", "0")), str(f.get("hour", "7"))
    r.weekday = int(wd) if wd.isdigit() and int(wd) < 7 else 0
    r.hour = int(hr) if hr.isdigit() and int(hr) < 24 else 7
    r.sections = ",".join(x for x in f.getlist("sections") if x in SECTIONS) or "activity"
    people = {str(u.id) for u in _people(session, user)}
    r.user_ids = ",".join(x for x in f.getlist("users") if x in people)
    r.emails = ", ".join(e.strip() for e in str(f.get("emails", "")).replace(";", ",").split(",") if "@" in e)[:500]
    r.enabled = bool(f.get("enabled"))
    r.name = (str(f.get("name", "")).strip() or f"{FREQ[r.frequency]} door report")[:80]
    if not r.user_ids and not r.emails:
        flash(request, "Pick at least one person or add an email address to send it to.", "error")
        r.enabled = False
    session.add(r)
    audit.record(session, "report.save", f"Saved the report {r.name}", user=user, site_ids=[r.site_id] if r.site_id else None)
    session.commit()
    flash(request, f"Report “{r.name}” saved.")
    return redirect("/reports")


@router.get("/reports/{rid}/preview", response_class=HTMLResponse)
def report_preview(rid: int, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    r = _mine(session, user, rid)
    if not r:
        return redirect("/reports")
    from .admin import brand
    html, _ = render_html(session, r, "/brand/logo" if brand()["logo"] else "")
    return HTMLResponse(html)


@router.post("/reports/{rid}/send")
def report_send(request: Request, rid: int, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    r = _mine(session, user, rid)
    if r:
        err = send(session, r)
        flash(request, err or f"Sent to {', '.join(recipients(session, r))}.", "error" if err else "ok")
    return redirect("/reports")


@router.post("/reports/{rid}/delete")
def report_delete(request: Request, rid: int, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    r = _mine(session, user, rid)
    if r:
        audit.record(session, "report.delete", f"Deleted the report {r.name}", user=user)
        session.delete(r)
        session.commit()
        flash(request, "Report deleted.")
    return redirect("/reports")
