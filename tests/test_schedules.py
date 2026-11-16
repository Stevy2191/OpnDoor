from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.models import Schedule
from app.schedules import current_plan, occurrences, timeline, to_utc

CHI = ZoneInfo("America/Chicago")
_id = iter(range(1, 10_000))


def sch(kind, start, minutes, days="", action="unlock", until=None, label="s"):
    return Schedule(id=next(_id), label=label, kind=kind, action=action, start_local=start,
                    duration_minutes=minutes, repeat_weekdays=days, repeat_until=until)


def states(segs, tz=CHI):
    from app.schedules import to_local
    return [(to_local(x.start, tz).strftime("%a %H:%M"), to_local(x.end, tz).strftime("%a %H:%M"), x.state,
             x.schedule.label if x.schedule else None) for x in segs]


# Monday 2026-11-16
MON = datetime(2026, 11, 16)
WEEKDAYS_9_5 = dict(kind="default", start=datetime(2026, 11, 2, 9), minutes=8 * 60, days="0,1,2,3,4", label="hours")


def day_timeline(*scheds, day=MON):
    lo, hi = to_utc(day, CHI), to_utc(day + timedelta(days=1), CHI)
    return states(timeline(scheds, lo, hi, CHI))


def test_default_only():
    assert day_timeline(sch(**WEEKDAYS_9_5)) == [
        ("Mon 00:00", "Mon 09:00", None, None),
        ("Mon 09:00", "Mon 17:00", "unlock", "hours"),
        ("Mon 17:00", "Tue 00:00", None, None),
    ]


def test_lock_override_replaces_default_for_its_duration():
    holiday = sch("override", MON.replace(hour=12), 120, action="lock", label="closed")
    assert day_timeline(sch(**WEEKDAYS_9_5), holiday) == [
        ("Mon 00:00", "Mon 09:00", None, None),
        ("Mon 09:00", "Mon 12:00", "unlock", "hours"),
        ("Mon 12:00", "Mon 14:00", "lock", "closed"),
        ("Mon 14:00", "Mon 17:00", "unlock", "hours"),
        ("Mon 17:00", "Tue 00:00", None, None),
    ]


def test_unlock_override_outside_regular_hours():
    event = sch("override", MON.replace(hour=18), 180, label="event")
    assert day_timeline(sch(**WEEKDAYS_9_5), event)[-2:] == [
        ("Mon 18:00", "Mon 21:00", "unlock", "event"),
        ("Mon 21:00", "Tue 00:00", None, None),
    ]


def test_newest_override_wins():
    closed_all_day = sch("override", MON, 24 * 60, action="lock", label="closed")
    meeting = sch("override", MON.replace(hour=10), 60, label="meeting")  # added later -> wins
    segs = day_timeline(sch(**WEEKDAYS_9_5), closed_all_day, meeting)
    assert segs == [
        ("Mon 00:00", "Mon 10:00", "lock", "closed"),
        ("Mon 10:00", "Mon 11:00", "unlock", "meeting"),
        ("Mon 11:00", "Tue 00:00", "lock", "closed"),
    ]


def test_current_plan_runs_through_same_state():
    lo = to_utc(MON.replace(hour=16), CHI)
    event = sch("override", MON.replace(hour=17), 120, label="event")
    plan = current_plan(timeline([sch(**WEEKDAYS_9_5), event], lo, lo + timedelta(days=2), CHI))
    assert plan.state == "unlock" and plan.end == to_utc(MON.replace(hour=19), CHI)


def test_one_off_occurrence():
    s = sch("override", datetime(2026, 11, 20, 18), 90)
    occ = occurrences(s, datetime(2026, 11, 1), datetime(2026, 12, 1))
    assert [(o.start, o.end) for o in occ] == [(datetime(2026, 11, 20, 18), datetime(2026, 11, 20, 19, 30))]


def test_weekly_with_until_and_overnight():
    s = sch("override", datetime(2026, 11, 15, 22), 240, days="2,6", until=date(2026, 11, 30))
    occ = occurrences(s, datetime(2026, 11, 1), datetime(2027, 1, 1))
    assert [o.start.date().isoformat() for o in occ] == ["2026-11-15", "2026-11-18", "2026-11-22",
                                                          "2026-11-25", "2026-11-29"]
    assert occ[0].end == datetime(2026, 11, 16, 2)


def test_weekly_keeps_wall_clock_time_across_dst():
    s = sch("default", datetime(2026, 10, 25, 9), 60, days="6")
    occ = occurrences(s, datetime(2026, 10, 20), datetime(2026, 11, 10))
    assert all(o.start.hour == 9 for o in occ)
    assert to_utc(occ[0].start, CHI).hour == 14 and to_utc(occ[-1].start, CHI).hour == 15


def test_same_schedule_different_site_timezones():
    s = sch(**WEEKDAYS_9_5)
    ny = ZoneInfo("America/New_York")
    lo = to_utc(MON, ny)
    seg = [x for x in timeline([s], lo, lo + timedelta(days=1), ny) if x.state][0]
    assert seg.start == datetime(2026, 11, 16, 14)  # 9:00 New York = 14:00 UTC
    seg = [x for x in timeline([s], lo, lo + timedelta(days=1), CHI) if x.state][0]
    assert seg.start == datetime(2026, 11, 16, 15)  # 9:00 Chicago = 15:00 UTC
