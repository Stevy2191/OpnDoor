"""What UniFi Access does to doors by itself, so OpnDoor schedules don't step on it.

UniFi's API doesn't hand out a door's own Unlock Schedule, but it does say what each door is doing right now
(GET /doors/{id}/lock_rule). OpnDoor asks every minute, records each stretch when UniFi (not OpnDoor) has a
door unlocked on its schedule or set in the UniFi app, and works out the weekly pattern from what it saw."""
import logging
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Optional

from sqlmodel import Session, select

from . import engine, unifi
from .db import session_scope
from .models import Door, DoorState, Site, UnifiRuleSeen, utcnow
from .schedules import to_local, to_utc

log = logging.getLogger(__name__)
KEEP_DAYS = 60
LOOK_BACK = 28  # days of watching used for the weekly pattern
STALE = timedelta(minutes=5)
errors: dict = {}  # site id -> why the console can't say (shown on the door page)
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def classify(rule_type: str, ours: bool) -> Optional[str]:
    t = (rule_type or "").lower()
    if t == "schedule":
        return "schedule"
    if t in ("keep_unlock", "custom") and not ours:
        return "app_unlock"
    if t in ("keep_lock", "lock_now", "lock_early") and not ours:
        return "app_lock"
    return None


def watch_all() -> None:
    now = utcnow()
    with session_scope() as s:
        states = {st.door_id: st for st in s.exec(select(DoorState)).all()}
        for site in s.exec(select(Site).where(Site.enabled == True)).all():  # noqa: E712
            client = unifi.get_client(site)
            doors = s.exec(select(Door).where(Door.site_id == site.id, Door.present == True)).all()  # noqa: E712
            for door in doors:
                try:
                    rule = client.lock_rule(door.remote_id)
                except unifi.UnifiError as e:
                    errors[site.id] = str(e)[:200]
                    break
                errors.pop(site.id, None)
                st = states.get(door.id)
                ours = bool(st and st.mode and st.until and st.until > now)
                kind = classify(rule.get("type", ""), ours)
                record(s, door.id, kind, now)
                if kind != "schedule" and not ours and not site.emergency:
                    note_miss(s, door, now)
        for seg in s.exec(select(UnifiRuleSeen).where(UnifiRuleSeen.end == None)).all():  # noqa: E711
            if now - seg.last_seen > STALE:  # OpnDoor was off for a while: end it where we last saw it
                seg.end = seg.last_seen
                s.add(seg)
        for seg in s.exec(select(UnifiRuleSeen).where(UnifiRuleSeen.start < now - timedelta(days=KEEP_DAYS))).all():
            s.delete(seg)
        s.commit()


MISS_MARGIN = timedelta(minutes=15)


def note_miss(session: Session, door: Door, now: datetime) -> None:
    """The learned schedule says UniFi should be unlocking this door now, but it isn't: the schedule was changed
    or removed in UniFi. Note it once a day, so that day drops out of the pattern straight away."""
    tz = engine.site_tz(session.get(Site, door.site_id))
    local = to_local(now, tz)
    for p in pattern(session, door, now):
        if local.weekday() not in p["days"]:
            continue
        a, b = datetime.combine(local.date(), p["start"]), datetime.combine(local.date(), p["end"])
        if a + MISS_MARGIN <= local <= b - MISS_MARGIN:
            day0 = to_utc(datetime.combine(local.date(), time()), tz)
            if not session.exec(select(UnifiRuleSeen).where(UnifiRuleSeen.door_id == door.id,
                                                            UnifiRuleSeen.kind == "schedule_miss",
                                                            UnifiRuleSeen.start >= day0)).first():
                session.add(UnifiRuleSeen(door_id=door.id, kind="schedule_miss", start=now, end=now, last_seen=now))
            return


def forget_schedules(session: Session, door_ids: list[str]) -> int:
    """Drop what OpnDoor learned about UniFi's own unlock schedules on these doors. It learns again as it watches."""
    rows = session.exec(select(UnifiRuleSeen).where(UnifiRuleSeen.door_id.in_(door_ids),
                                                    UnifiRuleSeen.kind.in_(["schedule", "schedule_miss"]))).all()
    for r in rows:
        session.delete(r)
    return len({r.door_id for r in rows if r.kind == "schedule"})


def record(session: Session, door_id: str, kind: Optional[str], now: datetime) -> None:
    open_seg = session.exec(select(UnifiRuleSeen).where(UnifiRuleSeen.door_id == door_id,
                                                         UnifiRuleSeen.end == None)).first()  # noqa: E711
    if open_seg and open_seg.kind == kind and now - open_seg.last_seen <= STALE:
        open_seg.last_seen = now
        session.add(open_seg)
        return
    if open_seg:
        open_seg.end = now if now - open_seg.last_seen <= STALE else open_seg.last_seen
        session.add(open_seg)
    if kind:
        session.add(UnifiRuleSeen(door_id=door_id, kind=kind, start=now, last_seen=now))


def _t(x: time) -> str:
    return f"{x.hour % 12 or 12}{':%02d' % x.minute if x.minute else ''} {'AM' if x.hour < 12 else 'PM'}"


def _round(dt: datetime) -> time:
    m = (dt.hour * 60 + dt.minute + 7) // 15 * 15
    m = min(m, 23 * 60 + 45)
    return time(m // 60, m % 60)


def pattern(session: Session, door: Door, now: Optional[datetime] = None) -> list[dict]:
    """[{days: [0..6], start: time, end: time, seen: n}] for UniFi's own unlock schedule on this door."""
    now = now or utcnow()
    tz = engine.site_tz(session.get(Site, door.site_id))
    segs = session.exec(select(UnifiRuleSeen).where(UnifiRuleSeen.door_id == door.id, UnifiRuleSeen.kind == "schedule",
                                                    UnifiRuleSeen.start >= now - timedelta(days=LOOK_BACK))).all()
    # the last day each weekday's schedule was seen running, and the last day it should have and didn't
    last_seen: dict[int, datetime] = {}
    for g in segs:
        wd = to_local(g.start, tz).weekday()
        last_seen[wd] = max(last_seen.get(wd, g.start), g.start)
    gone = set()
    for m in session.exec(select(UnifiRuleSeen).where(UnifiRuleSeen.door_id == door.id, UnifiRuleSeen.kind == "schedule_miss",
                                                      UnifiRuleSeen.start >= now - timedelta(days=LOOK_BACK))).all():
        wd = to_local(m.start, tz).weekday()
        if m.start > last_seen.get(wd, datetime.min):
            gone.add(wd)
    windows: dict[int, Counter] = defaultdict(Counter)
    for g in segs:
        end = g.end or g.last_seen
        if end - g.start < timedelta(minutes=10):
            continue
        a, b = to_local(g.start, tz), to_local(end, tz)
        if a.date() != b.date():  # crosses midnight: only the part on its first day
            b = datetime.combine(a.date(), time(23, 59))
        windows[a.weekday()][(_round(a), _round(b))] += 1
    by_window: dict = defaultdict(lambda: {"days": [], "seen": 0})
    for wd, counts in windows.items():
        if wd in gone:
            continue  # UniFi stopped unlocking on this weekday: the schedule was changed or removed there
        (win, n), = counts.most_common(1)
        by_window[win]["days"].append(wd)
        by_window[win]["seen"] += n
    out = [{"days": sorted(v["days"]), "start": w[0], "end": w[1], "seen": v["seen"]} for w, v in by_window.items()]
    return sorted(out, key=lambda r: (r["days"][0], r["start"]))


def days_text(days: list[int]) -> str:
    if days == [0, 1, 2, 3, 4]:
        return "Mon–Fri"
    if days == [5, 6]:
        return "Sat–Sun"
    if days == list(range(7)):
        return "Every day"
    if len(days) > 2 and days == list(range(days[0], days[-1] + 1)):
        return f"{DAYS[days[0]]}–{DAYS[days[-1]]}"
    return ", ".join(DAYS[d] for d in days)


def describe(pat: list[dict]) -> str:
    return "; ".join(f"{days_text(p['days'])} {_t(p['start'])}–{_t(p['end'])}" for p in pat)


def status(session: Session, door: Door) -> dict:
    """What UniFi is doing to the door now, what OpnDoor has learned, and since when it has watched."""
    now_seg = session.exec(select(UnifiRuleSeen).where(UnifiRuleSeen.door_id == door.id,
                                                       UnifiRuleSeen.end == None)).first()  # noqa: E711
    first = session.exec(select(UnifiRuleSeen.start).where(UnifiRuleSeen.door_id == door.id)
                         .order_by(UnifiRuleSeen.start)).first()
    pat = pattern(session, door)
    return {"now": now_seg.kind if now_seg else "", "pattern": pat, "text": describe(pat), "since": first,
            "error": errors.get(door.site_id, "")}


def patterns_for(session: Session, doors: list[Door]) -> dict[str, list[dict]]:
    ids = {d.id for d in doors}
    seen = set(session.exec(select(UnifiRuleSeen.door_id).where(UnifiRuleSeen.kind == "schedule")).all()) & ids
    return {d.id: p for d in doors if d.id in seen for p in [pattern(session, d)] if p}


def week_blocks(session: Session, doors: list[Door], start: date, days: int = 7,
                hol_maps: Optional[dict] = None) -> dict[date, list[dict]]:
    """UniFi's own unlock schedules expected in a week, per day, with doors that share a window grouped.
    hol_maps (door id -> {date: Holiday}): a holiday keeps the door locked over UniFi's schedule, so that
    part is left out."""
    from .schedules import cut, holiday_windows
    names = {d.id: d.name for d in doors}
    pats = patterns_for(session, doors)
    out: dict[date, dict] = defaultdict(dict)
    for door_id, pat in pats.items():
        hmap = (hol_maps or {}).get(door_id)
        for p in pat:
            for k in range(days):
                day = start + timedelta(days=k)
                if day.weekday() in p["days"]:
                    a, b = datetime.combine(day, p["start"]), datetime.combine(day, p["end"])
                    for x, y in cut(a, b, holiday_windows(hmap, day, day)):
                        out[day].setdefault((x.time(), y.time()), []).append(names[door_id])
    return {day: [{"start": datetime.combine(day, s), "end": datetime.combine(day, e), "doors": sorted(n)}
                  for (s, e), n in sorted(v.items())] for day, v in out.items()}
