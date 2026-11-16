from datetime import timedelta

from sqlmodel import select

from app import calendars, engine
from app.db import session_scope
from app.models import CalendarFeed, CalendarRule, Schedule, ScheduleDoor
from app.schedules import now_local


def ics(*events):
    body = "\r\n".join(events)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n{body}\r\nEND:VCALENDAR\r\n".encode()


def event(uid, title, start, hours=2, location="", extra=""):
    fmt = "%Y%m%dT%H%M%S"
    return (f"BEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{title}\r\nLOCATION:{location}\r\n"
            f"DTSTART;TZID=America/Chicago:{start.strftime(fmt)}\r\n"
            f"DTEND;TZID=America/Chicago:{(start + timedelta(hours=hours)).strftime(fmt)}\r\n{extra}END:VEVENT")


def setup_feed(default=""):
    engine.ensure_sites()
    engine.sync_doors()
    with session_scope() as s:
        f = CalendarFeed(name="Bookings", url="https://example.invalid/x.ics", default_door_ids=default,
                         lead_minutes=15, lag_minutes=10)
        s.add(f)
        s.flush()
        s.add(CalendarRule(feed_id=f.id, field="location", contains="hall", door_ids="1:demo-hall,1:demo-side"))
        s.add(CalendarRule(feed_id=f.id, field="title", contains="staff", door_ids="1:demo-office"))
        s.commit()
        return f.id


def run(fid, data):
    with session_scope() as s:
        feed = s.get(CalendarFeed, fid)
        result = calendars.apply_feed(s, feed, calendars.plan_feed(s, feed, data))
        s.commit()
        return result


def doors_by_label():
    with session_scope() as s:
        out = {}
        for sch in s.exec(select(Schedule)).all():
            out[sch.label] = sorted(s.exec(select(ScheduleDoor.door_id).where(ScheduleDoor.schedule_id == sch.id)).all())
        return out


def test_rules_defaults_padding_and_idempotency():
    fid = setup_feed(default="1:demo-front")
    t = (now_local() + timedelta(days=2)).replace(hour=18, minute=0, second=0, microsecond=0)
    data = ics(event("a", "Smith Wedding", t, location="Fellowship Hall"),
               event("b", "Staff meeting in the Hall", t + timedelta(days=1), location="Fellowship Hall"),
               event("c", "Yoga", t + timedelta(days=2)))
    assert run(fid, data) == {"created": 3, "updated": 0, "removed": 0}
    d = doors_by_label()
    assert d["Smith Wedding · Fellowship Hall"] == ["1:demo-hall", "1:demo-side"]
    assert d["Staff meeting in the Hall · Fellowship Hall"] == ["1:demo-hall", "1:demo-office", "1:demo-side"]
    assert d["Yoga"] == ["1:demo-front"]
    with session_scope() as s:
        sch = s.exec(select(Schedule).where(Schedule.label == "Yoga")).one()
        assert sch.start_local == t + timedelta(days=2, minutes=-15)
        assert sch.duration_minutes == 120 + 15 + 10
        assert (sch.kind, sch.action) == ("override", "unlock")

    assert run(fid, data) == {"created": 0, "updated": 0, "removed": 0}

    # Booking cancelled, another moved rooms
    data2 = ics(event("a", "Smith Wedding", t, location="Room 2"),
                event("c", "Yoga", t + timedelta(days=2), extra="STATUS:CANCELLED\r\n"))
    assert run(fid, data2) == {"created": 0, "updated": 1, "removed": 2}
    assert doors_by_label() == {"Smith Wedding · Room 2": ["1:demo-front"]}


def test_unmatched_skipped_without_defaults_and_recurring_expanded():
    fid = setup_feed(default="")
    t = (now_local() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    data = ics(event("r", "Staff standup", t, hours=1, extra="RRULE:FREQ=WEEKLY;COUNT=3\r\n"),
               event("x", "Dentist", t))
    res = run(fid, data)
    assert res["created"] == 3
    assert set(doors_by_label()) == {"Staff standup"}


def test_all_day_events_skipped_by_default():
    fid = setup_feed(default="1:demo-front")
    d = (now_local() + timedelta(days=3)).date()
    data = ics(f"BEGIN:VEVENT\r\nUID:ad\r\nSUMMARY:Holiday\r\nDTSTART;VALUE=DATE:{d:%Y%m%d}\r\nEND:VEVENT")
    assert run(fid, data)["created"] == 0
