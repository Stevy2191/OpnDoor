"""Holidays for some doors only, and editing a holiday afterwards."""
from datetime import date, datetime, timedelta

from sqlmodel import select

from app import engine
from app.db import session_scope
from app.models import Door, Holiday
from app.schedules import now_local
from tests.test_web import client, setup_admin  # noqa: F401


def regular_hours(client, doors):
    today = now_local().date()
    client.post("/schedules", data={"mode": "default", "label": "Office hours", "door_ids": doors,
                                    "day": today.isoformat(), "start": "00:00", "end": "23:59",
                                    "weekdays": [str(i) for i in range(7)], "holidays_field": "1", "follow_holidays": "1"})


def unlocked_today(door_id):
    with session_scope() as s:
        door = s.get(Door, door_id)
        now = now_local()
        lo = datetime.combine(now.date(), datetime.min.time())
        from app.schedules import to_utc
        segs = engine.door_timeline(s, door, to_utc(lo + timedelta(hours=11)), to_utc(lo + timedelta(hours=13)))
        return any(g.state == "unlock" for g in segs)


def test_holiday_for_some_doors_leaves_the_others_open(client, fake):
    setup_admin(client)
    regular_hours(client, ["1:demo-front", "1:demo-side"])
    today = now_local().date().isoformat()
    r = client.post("/holidays", data={"name": "Staff day", "start": today, "mode": "closed", "site": "1",
                                       "scope": "some", "doors": ["1:demo-front"]}, follow_redirects=True)
    assert "Added Staff day" in r.text and "Only Front Entrance" in r.text
    assert not unlocked_today("1:demo-front")  # on the holiday
    assert unlocked_today("1:demo-side")  # left out: keeps its regular hours


def test_every_door_ticked_means_all_doors(client):
    setup_admin(client)
    with session_scope() as s:
        all_doors = [d.id for d in s.exec(select(Door).where(Door.site_id == 1)).all()]
    client.post("/holidays", data={"name": "Closed", "start": now_local().date().isoformat(), "mode": "closed", "site": "1",
                                   "scope": "some", "doors": all_doors})
    with session_scope() as s:
        assert s.exec(select(Holiday)).one().door_ids == ""  # includes doors added later


def test_edit_a_holiday(client, fake):
    setup_admin(client)
    regular_hours(client, ["1:demo-front", "1:demo-side"])
    today = now_local().date()
    client.post("/holidays", data={"name": "Snow day", "start": today.isoformat(), "mode": "closed", "site": "1"})
    with session_scope() as s:
        hid = s.exec(select(Holiday)).one().id
    page = client.get(f"/holidays/{hid}/edit").text
    assert "Edit holiday" in page and 'value="Snow day"' in page
    r = client.post(f"/holidays/{hid}", data={"name": "Snow days", "start": today.isoformat(),
                                              "end": (today + timedelta(days=1)).isoformat(), "mode": "part",
                                              "locked_from": "10:00", "locked_to": "14:00", "site": "1",
                                              "scope": "some", "doors": ["1:demo-side"]}, follow_redirects=True)
    assert "Saved Snow days" in r.text
    with session_scope() as s:
        h = s.get(Holiday, hid)
        assert (h.name, h.locked, h.door_ids, h.end_date) == ("Snow days", "10:00-14:00", "1:demo-side",
                                                             today + timedelta(days=1))
    assert unlocked_today("1:demo-front")  # no longer part of it
    assert "changed the holiday" in client.get("/history").text
    # nothing picked is refused
    r = client.post(f"/holidays/{hid}", data={"name": "X", "start": today.isoformat(), "mode": "closed", "site": "1",
                                              "scope": "some"}, follow_redirects=True)
    assert "Tick at least one door" in r.text


def test_list_says_which_doors_are_left_out(client):
    setup_admin(client)
    with session_scope() as s:
        ids = [d.id for d in s.exec(select(Door).where(Door.site_id == 1)).all() if d.id != "1:demo-side"]
    r = client.post("/holidays", data={"name": "Most", "start": now_local().date().isoformat(), "mode": "closed", "site": "1",
                                       "scope": "some", "doors": ids}, follow_redirects=True)
    assert "All doors except Side Door" in r.text
