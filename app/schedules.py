"""Turning schedules into concrete unlock windows."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Optional

from . import config
from .models import Schedule

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def to_utc(local_naive: datetime) -> datetime:
    """Naive local (app tz) -> naive UTC."""
    return local_naive.replace(tzinfo=config.TZ).astimezone(timezone.utc).replace(tzinfo=None)


def to_local(utc_naive: datetime) -> datetime:
    """Naive UTC -> naive local (app tz)."""
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(config.TZ).replace(tzinfo=None)


def now_local() -> datetime:
    return datetime.now(config.TZ).replace(tzinfo=None)


def weekdays_of(s: Schedule) -> list[int]:
    return sorted({int(x) for x in s.repeat_weekdays.split(",") if x.strip() != ""})


def series_last_date(s: Schedule) -> date:
    if weekdays_of(s):
        return s.repeat_until or date.max
    return s.start_local.date()


def describe_repeat(s: Schedule) -> str:
    days = weekdays_of(s)
    if not days:
        return ""
    names = "Every day" if len(days) == 7 else "Every " + ", ".join(WEEKDAYS[d] for d in days)
    if s.repeat_until:
        names += f" until {s.repeat_until:%b %-d, %Y}"
    return names


@dataclass
class Occurrence:
    schedule: Schedule
    start: datetime  # naive local
    end: datetime  # naive local

    @property
    def start_utc(self) -> datetime:
        return to_utc(self.start)

    @property
    def end_utc(self) -> datetime:
        return to_utc(self.end)


def occurrences(s: Schedule, range_start: datetime, range_end: datetime) -> list[Occurrence]:
    """Occurrences of s overlapping [range_start, range_end) (naive local times)."""
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
    while d <= last:
        if d.weekday() in days:
            start = datetime.combine(d, t)
            end = start + dur
            if start < range_end and end > range_start:
                out.append(Occurrence(s, start, end))
        d += timedelta(days=1)
    return out


def expand(schedules: Iterable[Schedule], range_start: datetime, range_end: datetime) -> list[Occurrence]:
    out = []
    for s in schedules:
        out.extend(occurrences(s, range_start, range_end))
    out.sort(key=lambda o: (o.start, o.end))
    return out


def unlocked_until(windows: list[tuple[datetime, datetime]], now: datetime) -> Optional[datetime]:
    """If `now` falls inside a window, return when the (merged) unlock ends.

    Windows that overlap or touch are chained, so back-to-back bookings keep the
    door open without a relock in between. All values are naive UTC.
    """
    until = max((end for start, end in windows if start <= now < end), default=None)
    if until is None:
        return None
    changed = True
    while changed:
        changed = False
        for start, end in windows:
            if start <= until < end:
                until = end
                changed = True
    return until
