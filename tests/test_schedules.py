from datetime import date, datetime, timedelta

from app.models import Schedule
from app.schedules import occurrences, to_utc, unlocked_until

T = datetime(2026, 11, 16, 12, 0)


def w(a, b):
    return (T + timedelta(minutes=a), T + timedelta(minutes=b))


def test_unlocked_until_inside_window():
    assert unlocked_until([w(-10, 30)], T) == T + timedelta(minutes=30)


def test_unlocked_until_outside():
    assert unlocked_until([w(10, 30), w(-60, -1)], T) is None


def test_end_is_exclusive():
    assert unlocked_until([w(-30, 0)], T) is None


def test_back_to_back_windows_chain():
    assert unlocked_until([w(60, 120), w(-10, 30), w(30, 60), w(200, 300)], T) == T + timedelta(minutes=120)


def test_overlap_takes_longest():
    assert unlocked_until([w(-10, 30), w(-5, 90)], T) == T + timedelta(minutes=90)


def test_one_off_occurrence():
    s = Schedule(label="x", start_local=datetime(2026, 11, 20, 18), duration_minutes=90)
    occ = occurrences(s, datetime(2026, 11, 1), datetime(2026, 12, 1))
    assert [(o.start, o.end) for o in occ] == [(datetime(2026, 11, 20, 18), datetime(2026, 11, 20, 19, 30))]


def test_weekly_with_until_and_overnight():
    # Sundays and Wednesdays 22:00 for 4 hours, until Nov 30
    s = Schedule(label="x", start_local=datetime(2026, 11, 15, 22), duration_minutes=240,
                 repeat_weekdays="2,6", repeat_until=date(2026, 11, 30))
    occ = occurrences(s, datetime(2026, 11, 1), datetime(2027, 1, 1))
    assert [o.start.date().isoformat() for o in occ] == ["2026-11-15", "2026-11-18", "2026-11-22",
                                                          "2026-11-25", "2026-11-29"]
    assert occ[0].end == datetime(2026, 11, 16, 2)


def test_weekly_keeps_wall_clock_time_across_dst():
    s = Schedule(label="x", start_local=datetime(2026, 10, 25, 9), duration_minutes=60, repeat_weekdays="6")
    occ = occurrences(s, datetime(2026, 10, 20), datetime(2026, 11, 10))
    assert all(o.start.hour == 9 for o in occ)
    # 9:00 CDT is 14:00 UTC, 9:00 CST is 15:00 UTC
    assert to_utc(occ[0].start).hour == 14 and to_utc(occ[-1].start).hour == 15


def test_overnight_occurrence_from_previous_day_is_found():
    s = Schedule(label="x", start_local=datetime(2026, 11, 15, 22), duration_minutes=240, repeat_weekdays="6")
    occ = occurrences(s, datetime(2026, 11, 16, 1), datetime(2026, 11, 16, 3))
    assert len(occ) == 1
