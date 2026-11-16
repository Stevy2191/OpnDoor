"""Create door schedules from calendar bookings (iCal / ICS feeds).

Works with anything that publishes an .ics URL: Google Calendar ("Secret
address in iCal format"), Outlook / Microsoft 365 (published calendar ICS
link), and most booking systems.

Each feed has rules: if an event's title/location/description contains some
text, it unlocks those doors. Events matching no rule use the feed's default
doors (or are skipped if there are none).
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
import icalendar
import recurring_ical_events
from sqlmodel import Session, select

from . import config
from .db import session_scope
from .engine import audit, site_tz
from .models import CalendarFeed, CalendarRule, Door, Schedule, ScheduleDoor, Site, utcnow
from .schedules import now_local

log = logging.getLogger(__name__)


@dataclass
class Booking:
    uid: str
    title: str
    location: str
    description: str
    start: datetime  # tz-aware, or naive for floating/all-day times
    end: datetime
    all_day: bool = False
    door_ids: list[str] = field(default_factory=list)
    start_local: Optional[datetime] = None  # app-timezone wall clock, for display
    end_local: Optional[datetime] = None


def split_ids(s: str) -> list[str]:
    return [x for x in (p.strip() for p in s.split(",")) if x]


def wall(dt: datetime, tz) -> datetime:
    """Booking time as naive wall-clock in tz (floating/all-day times stay as written)."""
    return dt.astimezone(tz).replace(tzinfo=None) if dt.tzinfo else dt


def _local(v) -> tuple[datetime, bool]:
    if isinstance(v, datetime):
        return v, False
    if isinstance(v, date):
        return datetime.combine(v, datetime.min.time()), True
    raise ValueError(f"Unsupported date value {v!r}")


def fetch_ics(url: str) -> bytes:
    if url.startswith("webcal://"):
        url = "https://" + url.removeprefix("webcal://")
    resp = httpx.get(url, timeout=20, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def parse_bookings(ics: bytes, start: datetime, end: datetime) -> list[Booking]:
    cal = icalendar.Calendar.from_ical(ics)
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        s, all_day = _local(ev.decoded("DTSTART"))
        if "DTEND" in ev:
            e, _ = _local(ev.decoded("DTEND"))
        elif "DURATION" in ev:
            e = s + ev.decoded("DURATION")
        else:
            e = s + (timedelta(days=1) if all_day else timedelta(hours=1))
        uid = str(ev.get("UID", "")) or f"{ev.get('SUMMARY', '')}"
        out.append(
            Booking(
                uid=f"{uid}@{s.isoformat()}",
                title=str(ev.get("SUMMARY", "")).strip() or "(untitled booking)",
                location=str(ev.get("LOCATION", "")).strip(),
                description=str(ev.get("DESCRIPTION", "")).strip(),
                start=s,
                end=e,
                all_day=all_day,
            )
        )
    return out


def match_doors(b: Booking, rules: list[CalendarRule], default_door_ids: list[str]) -> list[str]:
    doors: list[str] = []
    for r in sorted(rules, key=lambda r: (r.position, r.id or 0)):
        needle = r.contains.strip().lower()
        if not needle:
            continue
        haystacks = {
            "title": [b.title],
            "location": [b.location],
            "description": [b.description],
        }.get(r.field, [b.title, b.location, b.description])
        if any(needle in h.lower() for h in haystacks):
            doors.extend(d for d in split_ids(r.door_ids) if d not in doors)
    return doors or list(default_door_ids)


def plan_feed(session: Session, feed: CalendarFeed, ics: bytes) -> list[Booking]:
    """Parse a feed and work out which doors each booking unlocks (no DB writes)."""
    now = now_local()
    bookings = parse_bookings(ics, now - timedelta(days=1), now + timedelta(days=config.CALENDAR_LOOKAHEAD_DAYS))
    for b in bookings:  # for display in the preview
        b.start_local, b.end_local = wall(b.start, config.TZ), wall(b.end, config.TZ)
    rules = session.exec(select(CalendarRule).where(CalendarRule.feed_id == feed.id)).all()
    known = set(session.exec(select(Door.id)).all())
    defaults = split_ids(feed.default_door_ids)
    for b in bookings:
        b.door_ids = [d for d in match_doors(b, rules, defaults) if d in known]
    return [b for b in bookings if feed.include_all_day or not b.all_day]


def apply_feed(session: Session, feed: CalendarFeed, bookings: list[Booking]) -> dict:
    existing = {
        s.source_uid: s
        for s in session.exec(select(Schedule).where(Schedule.feed_id == feed.id)).all()
    }
    seen = set()
    created = updated = 0
    door_site = dict(session.exec(select(Door.id, Door.site_id)).all())
    tzs = {site.id: site_tz(site) for site in session.exec(select(Site)).all()}
    for b in bookings:
        by_site: dict[int, list[str]] = {}
        for d in b.door_ids:
            by_site.setdefault(door_site[d], []).append(d)
        for site_id, door_ids in by_site.items():
            uid = f"{b.uid}#site{site_id}"
            seen.add(uid)
            tz = tzs.get(site_id, config.TZ)
            b_start, b_end = wall(b.start, tz), wall(b.end, tz)
            start = b_start - timedelta(minutes=feed.lead_minutes)
            duration = int((b_end - b_start).total_seconds() // 60) + feed.lead_minutes + feed.lag_minutes
            label = b.title + (f" · {b.location}" if b.location else "")
            sched = existing.get(uid)
            if sched is None:
                sched = Schedule(label=label, kind="override", action="unlock", start_local=start,
                                 duration_minutes=duration, source="calendar", feed_id=feed.id, source_uid=uid,
                                 notes=f"From calendar “{feed.name}”")
                session.add(sched)
                session.flush()
                set_schedule_doors(session, sched.id, door_ids)
                created += 1
                continue
            changed = False
            if (sched.label, sched.start_local, sched.duration_minutes) != (label, start, duration):
                sched.label, sched.start_local, sched.duration_minutes = label, start, duration
                session.add(sched)
                changed = True
            if set_schedule_doors(session, sched.id, door_ids):
                changed = True
            updated += changed

    # Bookings that vanished from the calendar (cancelled/moved): drop unless already over.
    now = now_local()
    removed = 0
    for uid, sched in existing.items():
        if uid in seen:
            continue
        if sched.start_local + timedelta(minutes=sched.duration_minutes) > now:
            delete_schedule(session, sched)
            removed += 1
    return {"created": created, "updated": updated, "removed": removed}


def set_schedule_doors(session: Session, schedule_id: int, door_ids: list[str]) -> bool:
    """Replace a schedule's doors. Returns True if anything changed."""
    rows = session.exec(select(ScheduleDoor).where(ScheduleDoor.schedule_id == schedule_id)).all()
    if {r.door_id for r in rows} == set(door_ids):
        return False
    for r in rows:
        session.delete(r)
    session.flush()
    for d in door_ids:
        session.add(ScheduleDoor(schedule_id=schedule_id, door_id=d))
    return True


def delete_schedule(session: Session, sched: Schedule) -> None:
    for row in session.exec(select(ScheduleDoor).where(ScheduleDoor.schedule_id == sched.id)).all():
        session.delete(row)
    session.delete(sched)


def sync_feed(feed_id: int) -> str:
    with session_scope() as s:
        feed = s.get(CalendarFeed, feed_id)
        if not feed:
            return "missing"
        try:
            bookings = plan_feed(s, feed, fetch_ics(feed.url))
            result = apply_feed(s, feed, bookings)
            skipped = sum(1 for b in bookings if not b.door_ids)
            status = (f"OK: {len(bookings)} bookings, {result['created']} new, "
                      f"{result['updated']} changed, {result['removed']} removed"
                      + (f", {skipped} matched no door" if skipped else ""))
            if result["created"] or result["updated"] or result["removed"]:
                audit(s, "calendar.sync", f"{feed.name}: {status}", actor="calendar")
        except Exception as e:  # network, HTTP, or parse errors
            log.warning("Calendar %s sync failed: %s", feed.name, e)
            status = f"Error: {e}"
            if feed.last_sync_status != status:
                audit(s, "calendar.error", f"{feed.name}: {e}", actor="calendar")
        feed.last_sync_at = utcnow()
        feed.last_sync_status = status[:500]
        s.add(feed)
        s.commit()
        return status


def sync_all() -> None:
    with session_scope() as s:
        ids = s.exec(select(CalendarFeed.id).where(CalendarFeed.enabled == True)).all()  # noqa: E712
    for fid in ids:
        sync_feed(fid)
