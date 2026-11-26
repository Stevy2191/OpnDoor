"""Holidays keep doors locked, and every view says so: week, day, door page, UniFi's own schedule."""
from datetime import date, timedelta

from sqlalchemy import text
from sqlmodel import select

from app import db, unifiwatch
from app.db import session_scope
from app.models import Door, Holiday
from app.schedules import now_local
from tests.test_unifiwatch import seen_weekdays
from tests.test_web import client, setup_admin  # noqa: F401


def next_weekday():
    d = now_local().date() + timedelta(days=7)
    return d - timedelta(days=d.weekday())  # Monday next week


def test_week_view_shows_the_holiday_and_what_it_skipped(client, fake):
    setup_admin(client)
    mon = next_weekday()
    client.post("/schedules", data={"mode": "unlock", "label": "Choir", "door_ids": ["1:demo-front"],
                                    "day": (now_local().date() + timedelta(days=1)).isoformat(), "start": "19:00", "end": "20:00", "repeat": "weekly",
                                    "weekdays": [str(i) for i in range(7)], "holidays_field": "1", "follow_holidays": "1"})
    client.post("/holidays", data={"name": "Founders Day", "start": mon.isoformat(), "mode": "allday", "site": "1"})
    week = client.get(f"/schedules?view=week&week={mon.isoformat()}").text
    assert "★ Founders Day" in week and "Locked all day · All doors" in week
    assert "Skipped for the holiday" in week


def test_unifi_schedule_is_cut_where_a_holiday_holds_the_door(client, fake):
    setup_admin(client)
    seen_weekdays("1:demo-front")
    mon = next_weekday()
    with session_scope() as s:
        door = s.get(Door, "1:demo-front")
        plain = unifiwatch.week_blocks(s, [door], mon)
        assert mon in plain
        hol = Holiday(name="Half day", start_date=mon, end_date=mon, locked="12:00-23:59")
        cut = unifiwatch.week_blocks(s, [door], mon, hol_maps={door.id: {mon: hol}})
        assert [(b["start"].hour, b["end"].hour) for b in cut[mon]] == [(8, 12)]
        whole = unifiwatch.week_blocks(s, [door], mon, hol_maps={door.id: {mon: Holiday(name="X", start_date=mon,
                                                                                            end_date=mon)}})
        assert mon not in whole


def test_part_day_holiday_form_and_door_page(client, fake):
    setup_admin(client)
    today = now_local().date()
    r = client.post("/holidays", data={"name": "Early close", "start": today.isoformat(), "mode": "part",
                                       "locked_from": "00:00", "locked_to": "23:59", "site": "1"}, follow_redirects=True)
    assert "Added Early close" in r.text
    with session_scope() as s:
        h = s.exec(select(Holiday)).one()
        assert h.locked == "" and h.when_text() == "All day"  # the whole day picked = all day
        h.locked = "14:00-23:59"
        s.add(h)
        s.commit()
        assert h.when_text() == "From 2 PM"
    assert "Locked from 2 PM" in client.get("/holidays").text
    r = client.post("/holidays", data={"name": "Bad", "start": today.isoformat(), "mode": "part",
                                       "locked_from": "15:00", "locked_to": "09:00", "site": "1"}, follow_redirects=True)
    assert "has to end after it starts" in r.text
    door = client.get("/doors/1:demo-front").text
    assert "★ Early close (holiday)" in door


def test_old_shorter_hours_become_locked_from_closing_time(fresh_db):
    with session_scope() as s:
        s.add(Holiday(name="Christmas Eve", start_date=date(2030, 12, 24), end_date=date(2030, 12, 24), hours="08:00-12:00"))
        s.commit()
    db.init_db()
    db.init_db()  # only once
    with session_scope() as s:
        h = s.exec(select(Holiday)).one()
        assert (h.hours, h.locked, h.when_text()) == ("", "12:00-23:59", "From 12 PM")
        assert s.exec(text("SELECT count(*) FROM holiday")).one()[0] == 1


def test_month_view_list_holidays_and_day_tab(client, fake):
    setup_admin(client)
    tomorrow = now_local().date() + timedelta(days=1)
    client.post("/schedules", data={"mode": "unlock", "label": "Board meeting", "door_ids": ["1:demo-front"],
                                    "day": tomorrow.isoformat(), "start": "18:00", "end": "20:00"})
    client.post("/holidays", data={"name": "Founders Day", "start": tomorrow.isoformat(), "mode": "allday", "site": "1"})
    month = client.get(f"/schedules?view=month&month={tomorrow:%Y-%m}").text
    assert tomorrow.strftime("%B %Y") in month and "★ Founders Day" in month and "Board meeting" in month
    assert 'class="month fit"' in month and client.get("/schedules?view=month&month=bad").status_code == 200
    # the list shows upcoming holidays too
    lst = client.get("/schedules?view=list").text
    assert "Holidays" in lst and "★ Founders Day" in lst and "Locked all day" in lst and "All doors · locked, badges still work" in lst
    assert "Founders Day" not in client.get("/schedules?view=list&type=default").text
    # the Day view is gone: old links open that day in the month, and Month is the default
    r = client.get(f"/day?d={tomorrow.isoformat()}", follow_redirects=False)
    assert r.headers["location"] == f"/schedules?view=month&month={tomorrow:%Y-%m}&d={tomorrow.isoformat()}"
    assert "☀ Day" not in month
    assert 'class="on">☰ List' in client.get("/schedules?view=list").text
