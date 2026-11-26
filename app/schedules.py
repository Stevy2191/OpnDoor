"""Turning schedules into what a door should be doing, and when.

Rules for a door at any moment:
  1. If one or more *override* schedules are active, the most recently added
     one decides: the door is unlocked or kept locked, and defaults are ignored.
  2. Otherwise, if a *default* schedule is active, the door is unlocked.
  3. Otherwise, if a holiday covers the door right now, it is kept locked.
  4. Otherwise OpnDoor leaves the door on its normal UniFi rule (locked).

Repeating schedules that follow holidays simply don't run while a holiday is on (they are cut around
it), so on a holiday only one-time events and schedules set to run on holidays can open a door.
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


def holiday_windows(holidays: Optional[dict], first: date, last: date) -> list[tuple[datetime, datetime, object]]:
    """[(start, end, Holiday)] local wall-clock when holidays keep doors locked, for days first..last."""
    out: list = []
    d = first
    while holidays and d <= last:
        h = holidays.get(d)
        if h is not None:
            a, b = h.window(d)
            if out and out[-1][2] is h and out[-1][1] == a:
                out[-1] = (out[-1][0], b, h)  # several days in a row: one stretch
            else:
                out.append((a, b, h))
        d += timedelta(days=1)
    return out


def cut(start: datetime, end: datetime, windows: list) -> list[tuple[datetime, datetime]]:
    """What's left of start..end once the windows [(a, b, ...)] are taken out."""
    pieces = [(start, end)]
    for w in windows:
        a, b = w[0], w[1]
        nxt = []
        for x, y in pieces:
            if b <= x or a >= y:
                nxt.append((x, y))
                continue
            if x < a:
                nxt.append((x, a))
            if b < y:
                nxt.append((b, y))
        pieces = nxt
    return pieces


def follows_holidays(s: Schedule) -> bool:
    return bool(weekdays_of(s)) and s.on_holidays != "run"


def _raw(s: Schedule, range_start: datetime, range_end: datetime) -> list[Occurrence]:
    dur = timedelta(minutes=s.duration_minutes)
    days = weekdays_of(s)
    if not days:
        start = s.start_local
        end = start + dur
        return [Occurrence(s, start, end)] if start < range_end and end > range_start else []
    out = []
    d = max(s.start_local.date(), (range_start - dur).date())
    last = min(series_last_date(s), range_end.date())
    t: time = s.start_local.time()
    while d <= last:
        if d.weekday() in days:
            start = datetime.combine(d, t)
            end = start + dur
            if start < range_end and end > range_start:
                out.append(Occurrence(s, start, end))
        d += timedelta(days=1)
    return out


def _hol_cut(o: Occurrence, holidays: Optional[dict]) -> tuple[list[tuple[datetime, datetime]], Optional[object]]:
    """The parts of an occurrence a holiday leaves, and the holiday (None when it isn't touched)."""
    wins = holiday_windows(holidays, o.start.date() - timedelta(days=1), o.end.date())
    wins = [w for w in wins if w[0] < o.end and w[1] > o.start]
    if not wins:
        return [(o.start, o.end)], None
    return cut(o.start, o.end, wins), wins[0][2]


def occurrences(s: Schedule, range_start: datetime, range_end: datetime,
                holidays: Optional[dict] = None) -> list[Occurrence]:
    """Occurrences of s overlapping [range_start, range_end) (naive wall-clock).

    holidays: {date: Holiday}. A repeating schedule that follows holidays doesn't run while one is on:
    it's skipped (all-day holiday) or cut short (a holiday for part of the day)."""
    raw = _raw(s, range_start, range_end)
    if not holidays or not follows_holidays(s):
        return raw
    out = []
    for o in raw:
        for a, b in _hol_cut(o, holidays)[0]:
            if a < range_end and b > range_start:
                out.append(Occurrence(s, a, b))
    return out


def holiday_changes(schedules: Iterable[Schedule], range_start: datetime, range_end: datetime,
                    holidays: Optional[dict]) -> list[dict]:
    """What holidays take away: [{o: the usual Occurrence, holiday, skipped: bool}] (cut short when not skipped)."""
    out = []
    if not holidays:
        return out
    for s in schedules:
        if not follows_holidays(s):
            continue
        for o in _raw(s, range_start, range_end):
            left, h = _hol_cut(o, holidays)
            if h is not None:
                out.append({"o": o, "holiday": h, "skipped": not left})
    out.sort(key=lambda c: c["o"].start)
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
    holiday: Optional[object] = None  # or the holiday keeping it locked

    @property
    def label(self) -> str:
        if self.schedule:
            return self.schedule.label
        return f"★ {self.holiday.name}" if self.holiday else ""


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
    llo, lhi = to_local(lo, tz) - pad, to_local(hi, tz) + pad
    occ = [(to_utc(o.start, tz), to_utc(o.end, tz), o.schedule) for o in expand(schedules, llo, lhi, holidays)]
    occ = [(s, e, sch) for s, e, sch in occ if s < hi and e > lo]
    hol = [(to_utc(a, tz), to_utc(b, tz), h) for a, b, h in holiday_windows(holidays, llo.date(), lhi.date())]
    hol = [x for x in hol if x[0] < hi and x[1] > lo]
    cuts = sorted({lo, hi} | {t for s, e, _ in occ + hol for t in (s, e) if lo < t < hi})
    segs: list[Segment] = []
    for a, b in zip(cuts, cuts[1:]):
        state, sched = _decide([x for x in occ if x[0] <= a < x[1]])
        h = None
        if state is None:
            h = next((x[2] for x in hol if x[0] <= a < x[1]), None)
            state = "lock" if h is not None else None
        prev = segs[-1] if segs else None
        if (prev and prev.state == state and (prev.schedule and prev.schedule.id) == (sched and sched.id)
                and prev.holiday is h):
            prev.end = b
        else:
            segs.append(Segment(a, b, state, sched, h))
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
    return Segment(first.start, until, first.state, first.schedule, first.holiday)
