"""People whose doors follow their UniFi Access permissions.

For each person set to "Use their UniFi Access permissions", OpnDoor looks them
up by email on every console, reads their access policies (including ones they
get through user groups), turns door groups into doors, and keeps the policy's
weekly hours. The result is stored as UnifiGrant rows, refreshed every few
minutes, so UniFi Access stays the one place access is decided.
"""
import json
import logging
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from . import engine, unifi
from .db import session_scope
from .models import Door, Site, UnifiGrant, User

log = logging.getLogger(__name__)

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _hhmm(t: str) -> str:
    return (t or "")[:5]


def _hours(sched: dict) -> Optional[dict]:
    """Weekly windows of a UniFi schedule, or None for 'any time' (or if it can't be read)."""
    weekly = sched.get("weekly") or sched.get("week_schedule")
    if not isinstance(weekly, dict):
        return None
    out = {d: [[_hhmm(w.get("start_time")), _hhmm(w.get("end_time"))] for w in weekly.get(d) or []] for d in DAYS}
    if all(out[d] == [["00:00", "23:59"]] for d in DAYS):
        return None
    return out


def _merge(a: Optional[dict], b: Optional[dict]) -> Optional[dict]:
    """Two policies give the same door: access whenever either allows it."""
    if a is None or b is None:
        return None
    return {d: a.get(d, []) + b.get(d, []) for d in DAYS}


def _door_ids(client, resources: list, seen: set) -> set[str]:
    out = set()
    for r in resources or []:
        rid, kind = r.get("id"), r.get("type")
        if kind == "door":
            out.add(rid)
        elif kind == "door_group" and rid not in seen:
            seen.add(rid)
            try:
                out |= _door_ids(client, client.door_group(rid).get("resources") or [], seen)
            except unifi.UnifiError as e:
                log.warning("door group %s: %s", rid, e)
    return out


def sync_site(session: Session, site: Site, people: list[User]) -> dict[int, tuple[dict, str]]:
    """{user id: ({door id: (hours, policy names)}, note)} for the people found on this console."""
    client = unifi.get_client(site)
    uusers = client.list_users()
    by_email: dict[str, dict] = {}
    for u in uusers:
        if str(u.get("status", "ACTIVE")).upper() not in {"ACTIVE", ""}:
            continue
        for key in ("user_email", "email"):
            e = (u.get(key) or "").strip().lower()
            if e:
                by_email.setdefault(e, u)
    all_policies = None
    schedules: dict[str, Optional[dict]] = {}
    present = {d.remote_id: d.id for d in session.exec(select(Door).where(Door.site_id == site.id,
                                                                           Door.present == True)).all()}  # noqa: E712
    out = {}
    for p in people:
        uu = by_email.get(p.email.lower())
        if not uu:
            continue
        try:
            policies = client.user_policies(uu["id"])
        except unifi.UnifiError:
            # older Access versions: only the policies given to the person directly
            if all_policies is None:
                all_policies = {x["id"]: x for x in client.list_policies()}
            ids = uu.get("access_policy_ids") or [x.get("id") for x in uu.get("access_policies") or []]
            policies = [all_policies[i] for i in ids if i in all_policies]
        doors: dict[str, tuple[Optional[dict], list[str]]] = {}
        for pol in policies:
            sid = pol.get("schedule_id") or ""
            if sid not in schedules:
                try:
                    schedules[sid] = _hours(client.policy_schedule(sid)) if sid else None
                except unifi.UnifiError:
                    schedules[sid] = None
            for rid in _door_ids(client, pol.get("resources") or [], set()):
                did = present.get(rid)
                if not did:
                    continue
                if did in doors:
                    h, names = doors[did]
                    doors[did] = (_merge(h, schedules[sid]), names + [pol.get("name", "")])
                else:
                    doors[did] = (schedules[sid], [pol.get("name", "")])
        out[p.id] = (doors, f"{site.name}: {len(doors)} door{'' if len(doors) == 1 else 's'}")
    return out


def sync(session: Session, only: Optional[list[User]] = None) -> None:
    """Refresh UnifiGrant rows for everyone following UniFi Access (or just `only`)."""
    people = only if only is not None else [u for u in session.exec(select(User)).all() if u.follows_unifi]
    people = [p for p in people if p.follows_unifi and p.active]
    if not people:
        return
    found: dict[int, dict] = {p.id: {} for p in people}
    notes: dict[int, list[str]] = {p.id: [] for p in people}
    failed: list[Site] = []
    for site in session.exec(select(Site)).all():
        try:
            res = sync_site(session, site, people)
        except unifi.UnifiError as e:
            log.warning("UniFi permissions at %s: %s", site.name, e)
            failed.append(site)
            continue
        for uid, (doors, note) in res.items():
            found[uid].update(doors)
            notes[uid].append(note)
    failed_ids = set(session.exec(select(Door.id).where(Door.site_id.in_([x.id for x in failed])))) if failed else set()
    now = datetime.now().strftime("%b %-d, %-I:%M %p")
    for p in people:
        for g in session.exec(select(UnifiGrant).where(UnifiGrant.user_id == p.id)).all():
            if g.door_id not in failed_ids:  # keep what we knew for consoles we couldn't reach
                session.delete(g)
        session.flush()
        for did, (hours, names) in found[p.id].items():
            session.add(UnifiGrant(user_id=p.id, door_id=did, hours=json.dumps(hours) if hours else "",
                                   policies=", ".join(sorted({n for n in names if n}))))
        note = "; ".join(notes[p.id]) if notes[p.id] else f"No one with the email {p.email} in UniFi Access"
        if failed:
            note += f" (couldn't check {', '.join(x.name for x in failed)})"
        p.unifi_note = f"{note} · checked {now}"
        session.add(p)
    session.flush()


def sync_all() -> None:
    with session_scope() as s:
        sync(s)
        s.commit()


def open_hours_ok(session: Session, user: User, door: Door) -> tuple[bool, str]:
    """Whether the person's UniFi Access hours allow using this door right now."""
    if not user.follows_unifi:
        return True, ""
    g = session.get(UnifiGrant, (user.id, door.id))
    if not g:
        return False, f"You don't have access to {door.name} in UniFi Access."
    if not g.hours:
        return True, ""
    hours = json.loads(g.hours)
    now = datetime.now(engine.site_tz(session.get(Site, door.site_id)))
    hm = now.strftime("%H:%M")
    for start, end in hours.get(DAYS[now.weekday()], []):
        if start <= hm < (end if end != "23:59" else "24:00"):
            return True, ""
    return False, f"Your UniFi Access hours don't include right now for {door.name}."
