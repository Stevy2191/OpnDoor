"""Turning schedules into what a door should be doing, and when.

Rules for a door at any moment:
  1. If one or more *override* schedules are active, the most recently added
     one decides: the door is unlocked or kept locked, and defaults are ignored.
  2. Otherwise, if a *default* schedule is active, the door is unlocked.
  3. Otherwise OpnDoor leaves the door on its normal UniFi rule (locked).
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Iterable, Optional

from . import config
from .models import Schedule

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# What a schedule is for: key -> (icon, name). Shown everywhere a schedule appears.
EVENT_TYPES = {
    "event": ("🎉", "Event"),
    "meeting": ("👥", "Meeting"),
    "rental": ("🔑", "Rental"),
    "class": ("📚", "Class"),
    "maintenance": ("🔧", "Maintenance"),
    "closure": ("🚫", "Closure"),
    "other": ("📌", "Other"),
}


def event_icon(s: Schedule) -> str:
    if s.kind == "default":
        return "🕘"
    return EVENT_TYPES.get(s.event_type, EVENT_TYPES["other"])[0]


def event_name(s: Schedule) -> str:
    if s.kind == "default":
        return "Regular hours"
    return EVENT_TYPES.get(s.event_type, EVENT_TYPES["other"])[1]


def to_utc(local_naive: datetime, tz: Optional[tzinfo] = None) -> datetime:
    """Naive wall-clock in tz -> naive UTC."""
    return local_naive.replace(tzinfo=tz or config.TZ).astimezone(timezone.utc).replace(tzinfo=None)


def to_local(utc_naive: datetime, tz: Optional[tzinfo] = None) -> datetime:
    """Naive UTC -> naive wall-clock in tz."""
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(tz or config.TZ).replace(tzinfo=None)


def now_local(tz: Optional[tzinfo] = None) -> datetime:
    return datetime.now(tz or config.TZ).replace(tzinfo=None)


def weekdays_of(s: Schedule) -> list[int]:
    return sorted({int(x) for x in s.repeat_weekdays.split(",") if x.strip() != ""})


def series_last_date(s: Schedule) -> date:
    if weekdays_of(s):
        return s.repeat_until or date.max
    return (s.start_local + timedelta(minutes=s.duration_minutes)).date()


def describe_repeat(s: Schedule) -> str:
    days = weekdays_of(s)
    if not days:
        return ""
    if len(days) == 7:
        names = "Every day"
    elif days == [0, 1, 2, 3, 4]:
        names = "Weekdays"
    elif days == [5, 6]:
        names = "Weekends"
    else:
        names = "Every " + ", ".join(WEEKDAYS[d] for d in days)
    if s.repeat_until:
        names += f" until {s.repeat_until:%b %-d, %Y}"
    return names


def describe_kind(s: Schedule) -> str:
    if s.kind == "default":
        return "Regular hours"
    return "Override: keep locked" if s.action == "lock" else "Override: unlocked"


@dataclass
class Occurrence:
    schedule: Schedule
    start: datetime  # naive wall-clock
    end: datetime  # naive wall-clock


def occurrences(s: Schedule, range_start: datetime, range_end: datetime,
                holidays: Optional[dict] = None) -> list[Occurrence]:
    """Occurrences of s overlapping [range_start, range_end) (naive wall-clock).

    holidays: {date: Holiday}. A repeating schedule that follows holidays doesn't run on
    one; regular hours run the holiday's shorter hours instead, if it has some."""
    dur = timedelta(minutes=s.duration_minutes)
    days = weekdays_of(s)
    if not days:
        start = s.start_local
        end = start + dur
        return [Occurrence(s, start, end)] if start < range_end and end > range_start else []

    out = []
    first = max(s.start_local.date(), (range_start - dur).date())
    last = min(series_last_date(s), range_end.date())
    t: time = s.start_local.time()
    d = first
    follows = bool(holidays) and s.on_holidays != "run"
    while d <= last:
        if d.weekday() in days:
            start = datetime.combine(d, t)
            end = start + dur
            hol = holidays.get(d) if follows else None
            if hol is not None:
                hours = hol.hours_range() if s.kind == "default" else None
                if not hours:
                    d += timedelta(days=1)
                    continue  # closed: this day's occurrence doesn't happen
                start, end = datetime.combine(d, hours[0]), datetime.combine(d, hours[1])
            if start < range_end and end > range_start:
                out.append(Occurrence(s, start, end))
        d += timedelta(days=1)
    return out


def expand(schedules: Iterable[Schedule], range_start: datetime, range_end: datetime,
           holidays: Optional[dict] = None) -> list[Occurrence]:
    out = []
    for s in schedules:
        out.extend(occurrences(s, range_start, range_end, holidays))
    out.sort(key=lambda o: (o.start, o.end))
    return out


@dataclass
class Segment:
    start: datetime  # naive UTC
    end: datetime  # naive UTC
    state: Optional[str]  # "unlock" | "lock" | None (normal UniFi rule)
    schedule: Optional[Schedule]  # what decided it


def _decide(active: list[tuple[datetime, datetime, Schedule]]) -> tuple[Optional[str], Optional[Schedule]]:
    overrides = [s for _, _, s in active if s.kind == "override"]
    if overrides:
        winner = max(overrides, key=lambda s: (s.id or 0))
        return winner.action, winner
    defaults = [s for _, _, s in active if s.kind == "default"]
    if defaults:
        return "unlock", min(defaults, key=lambda s: (s.id or 0))
    return None, None


def timeline(schedules: Iterable[Schedule], lo: datetime, hi: datetime, tz: tzinfo,
             holidays: Optional[dict] = None) -> list[Segment]:
    """A door's state from lo to hi (naive UTC), as consecutive segments."""
    pad = timedelta(days=2)
    occ = [(to_utc(o.start, tz), to_utc(o.end, tz), o.schedule)
           for o in expand(schedules, to_local(lo, tz) - pad, to_local(hi, tz) + pad, holidays)]
    occ = [(s, e, sch) for s, e, sch in occ if s < hi and e > lo]
    cuts = sorted({lo, hi} | {t for s, e, _ in occ for t in (s, e) if lo < t < hi})
    segs: list[Segment] = []
    for a, b in zip(cuts, cuts[1:]):
        state, sched = _decide([x for x in occ if x[0] <= a < x[1]])
        prev = segs[-1] if segs else None
        if prev and prev.state == state and (prev.schedule and prev.schedule.id) == (sched and sched.id):
            prev.end = b
        else:
            segs.append(Segment(a, b, state, sched))
    return segs


def current_plan(segs: list[Segment]) -> Optional[Segment]:
    """The door's state right now (segs[0]) extended through following segments
    in the same state, e.g. regular hours running straight into an event."""
    if not segs or segs[0].state is None:
        return None
    first = segs[0]
    until = first.end
    for seg in segs[1:]:
        if seg.state != first.state:
            break
        until = seg.end
    return Segment(first.start, until, first.state, first.schedule)
