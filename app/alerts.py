"""Alerts: email the people someone picked when something happens at their doors.

Nothing is on by default. Each alert is made by a person, starts switched off, and goes only to the
people (and extra addresses) chosen for it, at the sites, doors, days and hours chosen for it."""
import json
import logging
from datetime import datetime, time, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlmodel import Session, select

from . import audit, config, engine, mailer
from .auth import allowed_door_ids, can_manage_person, managed_site_ids
from .db import get_session, session_scope
from .models import AlertRule, AlertSent, AuditLog, Door, DoorEvent, Site, User, Visitor, utcnow
from .schedules import to_local
from .web import flash, redirect, render, require_manager

log = logging.getLogger(__name__)
router = APIRouter()

# kind: label, what it means, does it use doors, its settings (name, label, default, unit)
KINDS = {
    "held_open": ("Door left open", "A door's sensor says it has stood open longer than you allow.", True,
                  [("minutes", "Open for at least", 10, "minutes")]),
    "denied": ("Turned-away badges", "Several turned-away attempts at one door in a short time.", True,
               [("count", "At least", 3, "attempts"), ("minutes", "Within", 10, "minutes")]),
    "opened": ("Someone opens a door", "Every opening of the doors you pick. Set the hours below to make it "
               "an after-hours alert.", True, []),
    "device_offline": ("Device offline", "A reader, hub or camera stops answering.", False,
                       [("minutes", "Offline for at least", 5, "minutes")]),
    "console": ("Console unreachable", "OpnDoor can't reach the site's UniFi Access console.", False,
                [("minutes", "For at least", 10, "minutes")]),
    "emergency": ("Lockdown or evacuation", "Someone starts a lockdown or evacuation, in OpnDoor or the UniFi app.",
                  False, []),
    "schedule_error": ("Door didn't follow its schedule", "OpnDoor couldn't apply a schedule to a door, for example "
                       "because the console refused it.", True, []),
    "visitor_here": ("Visitor arrived", "A visitor checks in on a lobby screen or first uses their pass.", False, []),
    "visitor_noshow": ("Visitor didn't come", "A visitor pass ended without being used.", False, []),
}
POLL = {"device_offline", "console"}
_since: dict = {}  # (site id, device id) -> first seen offline (in memory; a restart starts over)


def params(rule: AlertRule) -> dict:
    try:
        p = json.loads(rule.params or "{}")
    except ValueError:
        p = {}
    for name, _, default, _ in KINDS.get(rule.kind, ("", "", False, []))[3]:
        try:
            p[name] = max(1, int(p.get(name, default)))
        except (TypeError, ValueError):
            p[name] = default
    return p


def _ids(csv: str) -> list:
    return [x for x in (csv or "").split(",") if x]


def active_now(rule: AlertRule, tz, now: Optional[datetime] = None) -> bool:
    local = to_local(now or utcnow(), tz)
    if str(local.weekday()) not in (rule.days or ""):
        return False
    if not rule.start or not rule.end:
        return True
    try:
        a, b = time.fromisoformat(rule.start), time.fromisoformat(rule.end)
    except ValueError:
        return True
    t = local.time()
    return a <= t < b if a < b else (t >= a or t < b)  # overnight windows like 10 PM to 6 AM


def describe_when(rule: AlertRule) -> str:
    days = "".join(sorted(set(rule.days or "")))
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    d = "Every day" if days == "0123456" else "Weekdays" if days == "01234" else "Weekends" if days == "56" \
        else ", ".join(names[int(x)] for x in days) or "No days"
    if rule.start and rule.end:
        def t(x):
            h, m = map(int, x.split(":"))
            return f"{h % 12 or 12}{':%02d' % m if m else ''} {'AM' if h < 12 else 'PM'}"
        return f"{d}, {t(rule.start)} to {t(rule.end)}"
    return f"{d}, all day"


# ---------- finding things to tell people ----------

def _doors(session: Session, rule: AlertRule, site_ids: set) -> list[Door]:
    picked = set(_ids(rule.door_ids))
    return [d for d in session.exec(select(Door).where(Door.present == True)).all()  # noqa: E712
            if d.site_id in site_ids and (not picked or d.id in picked)]


def _sites(session: Session, rule: AlertRule) -> list[Site]:
    owner = session.get(User, rule.created_by) if rule.created_by else None
    allowed = managed_site_ids(session, owner) if owner else set()
    return [s for s in session.exec(select(Site).where(Site.enabled == True)).all()  # noqa: E712
            if s.id in allowed and (rule.site_id is None or s.id == rule.site_id)]


def find(session: Session, rule: AlertRule, now: Optional[datetime] = None) -> list[tuple[str, str, str, int]]:
    """[(key, subject, text, site id)] that this rule should tell people about right now."""
    now = now or utcnow()
    p = params(rule)
    sites = {s.id: s for s in _sites(session, rule)}
    if not sites:
        return []
    since = max(rule.enabled_at or now, now - timedelta(hours=6))
    out = []
    if rule.kind == "held_open":
        for d in _doors(session, rule, set(sites)):
            if d.position_status == "open" and d.position_since and now - d.position_since >= timedelta(minutes=p["minutes"]):
                mins = int((now - d.position_since).total_seconds() // 60)
                out.append((f"{d.id}|{d.position_since.isoformat()}", f"{d.name} has been open for {mins} minutes",
                            f"{d.name} at {sites[d.site_id].name} has been standing open since "
                            f"{_t(d.position_since, sites[d.site_id])}.", d.site_id))
    elif rule.kind in ("denied", "opened"):
        doors = {d.id: d for d in _doors(session, rule, set(sites))}
        lo = max(since, now - timedelta(minutes=p.get("minutes", 15)))
        rows = session.exec(select(DoorEvent).where(DoorEvent.at >= lo, DoorEvent.granted == (rule.kind == "opened"))
                            .order_by(DoorEvent.at)).all()
        rows = [e for e in rows if e.door_id in doors]
        if rule.kind == "opened":
            for e in rows:
                d = doors[e.door_id]
                out.append((f"{e.id}", f"{d.name} opened" + (f" by {e.who}" if e.who else ""),
                            f"{d.name} at {sites[d.site_id].name} was opened at {_t(e.at, sites[d.site_id])}"
                            + (f" by {e.who}" if e.who else "") + (f" ({e.method})" if e.method else "") + ".", d.site_id))
        else:
            per: dict = {}
            for e in rows:
                per.setdefault(e.door_id, []).append(e)
            for door_id, evs in per.items():
                if len(evs) >= p["count"]:
                    d = doors[door_id]
                    out.append((f"{door_id}|{evs[0].id}", f"{len(evs)} turned-away attempts at {d.name}",
                                f"{len(evs)} badge, PIN or phone attempts were turned away at {d.name} "
                                f"({sites[d.site_id].name}) in the last {p['minutes']} minutes, the latest at "
                                f"{_t(evs[-1].at, sites[d.site_id])}.", d.site_id))
    elif rule.kind in POLL:
        from .devices import reliable, site_devices
        for site in sites.values():
            info = site_devices(site)
            down = []
            if rule.kind == "console" and not info.get("console_ok"):
                down.append(("console", f"the UniFi Access console for {site.name}"))
            if rule.kind == "device_offline":
                down += [(d["id"], f"{d['name']}") for d in info["access"] if d["online"] is False and reliable(d)]
                down += [(c["id"], c["name"]) for c in info["cameras"] if (c.get("state") or "").upper() not in ("", "CONNECTED")]
            seen = set()
            for dev_id, name in down:
                k = (rule.kind, site.id, dev_id)
                seen.add(k)
                first = _since.setdefault(k, now)
                if now - first >= timedelta(minutes=p["minutes"]):
                    out.append((f"{site.id}|{dev_id}|{first.isoformat()}",
                                f"Can't reach {name}" if rule.kind == "console" else f"{name} is offline",
                                f"{name[0].upper() + name[1:]} at {site.name} has not answered since "
                                f"{_t(first, site)}.", site.id))
            for k in [k for k in _since if k[0] == rule.kind and k[1] == site.id and k not in seen]:
                _since.pop(k, None)
    elif rule.kind == "emergency":
        for site in sites.values():
            if site.emergency and site.emergency_at and site.emergency_at >= since - timedelta(minutes=1):
                out.append((f"{site.id}|{site.emergency_at.isoformat()}", f"{site.emergency.capitalize()} at {site.name}",
                            f"A {site.emergency} was started at {site.name} by {site.emergency_by or 'someone'} at "
                            f"{_t(site.emergency_at, site)}. It stays on until someone ends it.", site.id))
    elif rule.kind == "schedule_error":
        doors = {d.id: d for d in _doors(session, rule, set(sites))}
        for a in session.exec(select(AuditLog).where(AuditLog.action == "door.error", AuditLog.at >= since)).all():
            ids = [x for x in (a.door_ids or "").split(",") if x]
            hit = next((doors[i] for i in ids if i in doors), None)
            if hit:
                out.append((f"{a.id}", f"{hit.name} didn't follow its schedule",
                            f"OpnDoor couldn't apply a schedule to {hit.name} at {sites[hit.site_id].name}: {a.detail}",
                            hit.site_id))
    elif rule.kind in ("visitor_here", "visitor_noshow"):
        from .visitors import arrivals
        rows = session.exec(select(Visitor).where(Visitor.status == "", Visitor.site_id.in_(list(sites)),
                                                  Visitor.end >= since - timedelta(days=1))).all()
        got = arrivals(session, rows)
        for v in rows:
            site = sites[v.site_id]
            came = v.checked_in_at or got.get(v.id)
            if rule.kind == "visitor_here" and came and came >= since - timedelta(minutes=2):
                out.append((f"{v.id}", f"{v.name} has arrived",
                            f"{v.name}{' from ' + v.company if v.company else ''} arrived at {site.name} at "
                            f"{_t(came, site)}" + (f" to see {v.host_name}" if v.host_name else "") + ".", site.id))
            if rule.kind == "visitor_noshow" and not came and since <= v.end <= now:
                out.append((f"{v.id}", f"{v.name} didn't come",
                            f"The visitor pass for {v.name} at {site.name} ended at {_t(v.end, site)} without being "
                            "used.", site.id))
    return out


def _t(utc: datetime, site: Site) -> str:
    local = to_local(utc, engine.site_tz(site))
    today = to_local(utcnow(), engine.site_tz(site)).date()
    t = local.strftime("%-I:%M %p")
    return t if local.date() == today else f"{local.strftime('%a %b')} {local.day}, {t}"


# ---------- sending ----------

def recipients(session: Session, rule: AlertRule) -> list[str]:
    out = []
    for uid in _ids(rule.user_ids):
        u = session.get(User, int(uid)) if uid.isdigit() else None
        if u and u.active and "@" in (u.email or ""):
            out.append(u.email)
    out += [e.strip() for e in (rule.emails or "").split(",") if "@" in e]
    return sorted(set(out))


def deliver(session: Session, rule: AlertRule, subject: str, text: str) -> tuple[str, str]:
    """(sent to, error)."""
    to = recipients(session, rule)
    if not to:
        return "", "Nobody to send it to."
    if not mailer.configured(session):
        return ", ".join(to), "Email isn't set up (Settings → Email)."
    cfg = mailer.settings(session)
    errors = []
    for addr in to:
        try:
            mailer.send(cfg, addr, f"OpnDoor: {subject}",
                        f"{text}\n\n— {rule.name}\nYou get this because someone added you to this alert in OpnDoor.")
        except mailer.MailError as e:
            errors.append(str(e))
    return ", ".join(to), "; ".join(sorted(set(errors)))


def check_all() -> None:
    with session_scope() as s:
        rules = s.exec(select(AlertRule).where(AlertRule.enabled == True)).all()  # noqa: E712
        now = utcnow()
        for rule in rules:
            try:
                found = find(s, rule, now)
            except Exception:  # noqa: BLE001 - one bad rule mustn't stop the others
                log.exception("alert %s", rule.id)
                continue
            for key, subject, text, site_id in found:
                if s.exec(select(AlertSent.id).where(AlertSent.rule_id == rule.id, AlertSent.key == key)).first():
                    continue
                if rule.kind == "denied":  # one alert per burst at a door
                    door = key.split("|")[0]
                    recent = s.exec(select(AlertSent).where(AlertSent.rule_id == rule.id,
                                                            AlertSent.at >= now - timedelta(minutes=params(rule)["minutes"]))).all()
                    if any(r.key.split("|")[0] == door for r in recent):
                        continue
                site = s.get(Site, site_id)
                if not active_now(rule, engine.site_tz(site), now):
                    s.add(AlertSent(rule_id=rule.id, key=key, subject=subject, error="Outside this alert's hours"))
                    continue
                sent_to, err = deliver(s, rule, subject, text)
                s.add(AlertSent(rule_id=rule.id, key=key, subject=subject, sent_to=sent_to, error=err))
            s.commit()
        old = s.exec(select(AlertSent).where(AlertSent.at < now - timedelta(days=90))).all()
        for r in old:
            s.delete(r)
        s.commit()


# ---------- pages ----------

def _people(session: Session, user: User) -> list[User]:
    return [u for u in session.exec(select(User).order_by(User.name)).all()
            if u.active and "@" in (u.email or "") and (u.id == user.id or can_manage_person(session, user, u))]


def _mine(session: Session, user: User, rid: int) -> Optional[AlertRule]:
    r = session.get(AlertRule, rid)
    if not r:
        return None
    ok = user.is_admin or r.created_by == user.id or (r.site_id and r.site_id in managed_site_ids(session, user))
    return r if ok else None


@router.get("/alerts")
def alerts_page(request: Request, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    rules = [r for r in session.exec(select(AlertRule).order_by(AlertRule.name)).all() if _mine(session, user, r.id)]
    names = {s.id: s.name for s in session.exec(select(Site)).all()}
    doors = {d.id: d.name for d in session.exec(select(Door)).all()}
    users = {u.id: u.display for u in session.exec(select(User)).all()}
    ids = [r.id for r in rules]
    recent = session.exec(select(AlertSent).where(AlertSent.rule_id.in_(ids)).order_by(AlertSent.at.desc())
                          .limit(40)).all() if ids else []
    info = {r.id: {"doors": ", ".join(doors[d] for d in _ids(r.door_ids) if d in doors),
                   "to": ", ".join([users[int(u)] for u in _ids(r.user_ids) if u.isdigit() and int(u) in users]
                                   + [e.strip() for e in r.emails.split(",") if e.strip()])} for r in rules}
    return render(request, "alerts.html", user, rules=rules, kinds=KINDS, site_names=names, info=info, recent=recent, rule_names={r.id: r.name for r in rules},
                  describe_when=describe_when, mail_ready=mailer.configured(session), params=params)


def _form(request: Request, user: User, session: Session, rule: AlertRule):
    sites = [s for s in session.exec(select(Site).order_by(Site.name)).all() if s.id in managed_site_ids(session, user)]
    allowed = allowed_door_ids(session, user)
    doors = [d for d in session.exec(select(Door).where(Door.present == True, Door.hidden == False)  # noqa: E712
                                     .order_by(Door.name)).all() if d.id in allowed and d.site_id in {s.id for s in sites}]
    return render(request, "alert_form.html", user, r=rule, prm=params(rule), kinds=KINDS, sites=sites,
                  site_map={s.id: s for s in sites}, doors=doors, people=_people(session, user),
                  picked_doors=_ids(rule.door_ids), picked_users=_ids(rule.user_ids),
                  mail_ready=mailer.configured(session))


@router.get("/alerts/new")
def alert_new(request: Request, kind: str = "held_open", user: User = Depends(require_manager),
              session: Session = Depends(get_session)):
    kind = kind if kind in KINDS else "held_open"
    return _form(request, user, session, AlertRule(name="", kind=kind, user_ids=str(user.id)))


@router.get("/alerts/{rid}")
def alert_edit(request: Request, rid: int, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    r = _mine(session, user, rid)
    return _form(request, user, session, r) if r else redirect("/alerts")


@router.post("/alerts/save")
async def alert_save(request: Request, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    f = await request.form()
    rid = str(f.get("id", ""))
    r = _mine(session, user, int(rid)) if rid.isdigit() else AlertRule(name="", kind="", created_by=user.id)
    if r is None:
        return redirect("/alerts")
    kind = str(f.get("kind", ""))
    if kind not in KINDS:
        flash(request, "Pick what to be told about.", "error")
        return redirect("/alerts/new")
    mine = managed_site_ids(session, user)
    site = str(f.get("site_id", ""))
    r.kind = kind
    r.site_id = int(site) if site.isdigit() and int(site) in mine else None
    allowed = allowed_door_ids(session, user)
    r.door_ids = ",".join(d for d in f.getlist("doors") if d in allowed) if KINDS[kind][2] else ""
    p = {}
    for name, _, default, _ in KINDS[kind][3]:
        v = str(f.get(name, default))
        p[name] = max(1, min(10000, int(v))) if v.isdigit() else default
    r.params = json.dumps(p)
    people = {str(u.id) for u in _people(session, user)}
    r.user_ids = ",".join(x for x in f.getlist("users") if x in people)
    r.emails = ", ".join(e.strip() for e in str(f.get("emails", "")).replace(";", ",").split(",") if "@" in e)[:500]
    r.days = "".join(sorted(d for d in f.getlist("days") if d in "0123456")) or "0123456"
    r.start, r.end = ("", "") if f.get("allday") else (str(f.get("start", ""))[:5], str(f.get("end", ""))[:5])
    r.name = (str(f.get("name", "")).strip() or KINDS[kind][0])[:80]
    turn_on = bool(f.get("enabled"))
    if turn_on and not r.enabled:
        r.enabled_at = utcnow()
    r.enabled = turn_on
    if not r.user_ids and not r.emails:
        flash(request, "Pick at least one person or add an email address to send it to.", "error")
        r.enabled = False
    session.add(r)
    audit.record(session, "alert.save", f"Saved the alert {r.name} ({'on' if r.enabled else 'off'})", user=user,
                 site_ids=[r.site_id] if r.site_id else None)
    session.commit()
    flash(request, f"Alert “{r.name}” saved{' and switched on' if r.enabled else ' (switched off)'}.")
    return redirect("/alerts")


@router.post("/alerts/{rid}/toggle")
def alert_toggle(request: Request, rid: int, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    r = _mine(session, user, rid)
    if r:
        r.enabled = not r.enabled
        if r.enabled:
            r.enabled_at = utcnow()
        session.add(r)
        audit.record(session, "alert.save", f"Switched the alert {r.name} {'on' if r.enabled else 'off'}", user=user)
        session.commit()
    return redirect("/alerts")


@router.post("/alerts/{rid}/test")
def alert_test(request: Request, rid: int, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    r = _mine(session, user, rid)
    if r:
        sent_to, err = deliver(session, r, f"Test of “{r.name}”",
                               f"This is a test of the OpnDoor alert “{r.name}” ({KINDS[r.kind][0]}). "
                               "Real alerts look like this.")
        flash(request, err or f"Test sent to {sent_to}.", "error" if err else "ok")
    return redirect("/alerts")


@router.post("/alerts/{rid}/delete")
def alert_delete(request: Request, rid: int, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    r = _mine(session, user, rid)
    if r:
        for x in session.exec(select(AlertSent).where(AlertSent.rule_id == r.id)).all():
            session.delete(x)
        session.flush()
        audit.record(session, "alert.delete", f"Deleted the alert {r.name}", user=user)
        session.delete(r)
        session.commit()
        flash(request, "Alert deleted.")
    return redirect("/alerts")
