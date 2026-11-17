import pytest
from fastapi.testclient import TestClient

from app import engine
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def setup_admin(c):
    r = c.post("/setup", data={"name": "Ada Admin", "email": "ada@example.com", "password": "password123"})
    assert r.status_code == 200 and "Ada" in r.text
    return c


def test_first_run_redirects_to_setup(client):
    r = client.get("/", follow_redirects=True)
    assert r.url.path == "/setup"
    setup_admin(client)
    # Setup is closed after first user
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"


def test_admin_flow_and_permissions(client, fake):
    setup_admin(client)
    # dashboard lists demo doors
    r = client.get("/")
    assert "Front Entrance" in r.text and "Unlock now" in r.text

    # create a user who may only use the side door
    r = client.post("/users", data={"name": "Bob", "email": "bob@example.com", "password": "password123",
                                    "door_ids": ["1:demo-side"]})
    assert r.status_code == 200 and "Bob" in r.text

    # admin schedules a weekly door
    r = client.post("/schedules", data={"label": "Choir practice", "door_ids": ["1:demo-hall"], "day": "2030-01-02",
                                        "start": "18:00", "end": "20:30", "repeat": "weekly", "weekdays": ["2"]})
    assert r.status_code == 200
    assert "Choir practice" in client.get("/schedules?view=list").text

    for path in ["/users", "/users/new", "/calendars", "/activity", "/calendars/new", "/doors", "/account"]:
        assert client.get(path).status_code == 200, path

    client.post("/logout")
    r = client.post("/login", data={"email": "bob@example.com", "password": "password123"})
    assert r.url.path == "/"
    assert "Side Door" in r.text and "Front Entrance" not in r.text
    assert "Choir practice" not in client.get("/schedules").text  # not his door
    assert client.get("/users").status_code == 403

    # Bob can't sneak another door in
    client.post("/schedules", data={"label": "Sneaky", "door_ids": ["1:demo-front"], "day": "2030-01-02",
                                    "start": "18:00", "end": "19:00"})
    assert "Sneaky" not in client.get("/schedules").text

    # Quick unlock his own door: reconciled right away
    r = client.post("/quick-unlock", data={"door_ids": ["1:demo-side"], "minutes": "30"})
    assert "Unlocking Side Door for 30 minutes" in r.text
    assert ("unlock", "demo-side", 31) in fake.calls


def test_bad_login(client):
    setup_admin(client)
    client.post("/logout")
    r = client.post("/login", data={"email": "ada@example.com", "password": "nope"})
    assert "match" in r.text and r.url.path == "/login"


def test_open_redirect_blocked(client):
    setup_admin(client)
    client.post("/logout")
    r = client.post("/login?next=//evil.example", data={"email": "ada@example.com", "password": "password123"},
                    follow_redirects=False)
    assert r.headers["location"] == "/"


def test_past_one_off_rejected(client):
    setup_admin(client)
    r = client.post("/schedules", data={"label": "Old", "door_ids": ["1:demo-front"], "day": "2020-01-01",
                                        "start": "10:00", "end": "11:00"})
    assert "already passed" in r.text


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_multi_door_preselect(client):
    import re
    setup_admin(client)
    r = client.get("/schedules/new?door=1:demo-front&door=1:demo-hall")
    checked = set(re.findall(r'name="door_ids" value="([^"]+)"\s+checked', r.text))
    assert checked == {"1:demo-front", "1:demo-hall"}
    # a door the user can't use is never offered, even if passed in the URL
    client.post("/users", data={"name": "Bob", "email": "bob@example.com", "password": "password123",
                                "door_ids": ["1:demo-side"]})
    client.post("/logout")
    client.post("/login", data={"email": "bob@example.com", "password": "password123"})
    r = client.get("/schedules/new?door=1:demo-front&door=1:demo-side")
    assert set(re.findall(r'name="door_ids" value="([^"]+)"\s+checked', r.text)) == {"1:demo-side"}
    assert 'name="door_ids" value="1:demo-front"' not in r.text


def test_regular_hours_and_override_via_forms(client):
    from app.schedules import now_local
    setup_admin(client)
    today = now_local().date().isoformat()
    r = client.post("/schedules", data={"kind": "default", "label": "Office hours", "door_ids": ["1:demo-front"],
                                        "day": today, "start": "08:00", "end": "17:00", "weekdays": ["0", "1", "2", "3", "4"]})
    assert "Saved" in r.text
    r = client.post("/schedules", data={"kind": "override", "action": "lock", "label": "Closed for holiday",
                                        "door_ids": ["1:demo-front"], "day": "2030-12-25", "start": "00:00", "end": "23:59"})
    assert "Saved" in r.text
    page = client.get("/schedules?view=list").text
    assert "Office hours" not in page and "Kept locked" in page  # regular hours live on door pages
    page = client.get("/schedules?view=list&type=default").text
    assert "Office hours" in page and "Closed for holiday" not in page
    door = client.get("/doors/1:demo-front").text
    assert "Next 7 days" in door and "Closed for holiday" in door and "Office hours" in door

    # regular hours need days
    r = client.post("/schedules", data={"kind": "default", "label": "x", "door_ids": ["1:demo-front"],
                                        "day": today, "start": "08:00", "end": "17:00"})
    assert "Pick the days" in r.text


def test_add_second_site_groups_doors(client):
    setup_admin(client)
    r = client.post("/sites", data={"name": "North Campus", "host": "demo", "timezone": "America/New_York"})
    assert "Saved North Campus" in r.text and "OK: 4 doors" in r.text
    page = client.get("/doors").text  # two sites: the Doors page starts with site cards
    assert "North Campus" in page and "Demo site" in page and "Pick a site" in page
    assert "2:demo-front" in client.get("/site?id=2&next=/doors").text
    client.get("/site?id=&next=/")
    r = client.post("/sites", data={"name": "Bad", "host": "demo", "timezone": "Mars/Base"})
    assert "Unknown timezone" in r.text
    # remove it again
    r = client.post("/sites/2/delete")
    assert "Removed North Campus" in r.text and "2:demo-front" not in client.get("/doors").text


def test_deleting_things_that_have_schedules(client):
    """Regression: deletes must remove dependent rows first (SQLite enforces foreign keys)."""
    from app.db import session_scope
    from app.models import CalendarFeed, Schedule, ScheduleDoor
    from datetime import datetime
    setup_admin(client)
    client.post("/sites", data={"name": "North", "host": "demo", "timezone": "America/New_York"})
    for door in ["1:demo-front", "2:demo-front"]:
        client.post("/schedules", data={"label": f"S {door}", "door_ids": [door], "day": "2030-01-01",
                                        "start": "10:00", "end": "11:00"})
    client.post("/quick-unlock", data={"door_ids": ["1:demo-side"], "minutes": "15"})
    with session_scope() as s:
        feed = CalendarFeed(name="Cal", url="http://127.0.0.1:9/x.ics", default_door_ids="1:demo-hall")
        s.add(feed)
        s.flush()
        sch = Schedule(label="Booking", start_local=datetime(2030, 1, 2, 9), duration_minutes=60,
                       source="calendar", feed_id=feed.id, source_uid="b1")
        s.add(sch)
        s.flush()
        s.add(ScheduleDoor(schedule_id=sch.id, door_id="1:demo-hall"))
        s.commit()
        fid = feed.id

    assert "Deleted" in client.post("/schedules/1/delete").text
    assert "Deleted" in client.post("/schedules/3/delete").text  # the unlock-now
    assert "Removed North" in client.post("/sites/2/delete").text  # site with a schedule
    r = client.post(f"/calendars/{fid}/delete")
    assert r.status_code == 200 and "Removed" in r.text  # calendar with a schedule
    for path in ["/", "/schedules", "/doors", "/history"]:
        assert client.get(path).status_code == 200


def test_home_lock_now_and_site_switcher(client, fake):
    setup_admin(client)
    client.post("/sites", data={"name": "North", "host": "demo", "timezone": "America/Chicago"})
    client.post("/quick-unlock", data={"door_ids": ["1:demo-side"], "minutes": "30"})
    home = client.get("/").text
    assert "Right now" in home and "Lock now" in home and "1%3Ademo-side/lock-now" in home

    r = client.post("/doors/1:demo-side/lock-now")
    assert "Locking Side Door" in r.text
    assert fake.calls[-1] == ("lock", "demo-side")
    assert "locked Side Door early" in client.get("/history").text
    assert "already locked" in client.post("/doors/1:demo-front/lock-now").text

    # site switcher filters Home to one site
    client.get("/site?id=2&next=/")
    home = client.get("/").text
    assert "4 doors" in home and "1%3Ademo-side/lock-now" not in home
    client.get("/site?id=&next=/")
    assert "8 doors" in client.get("/").text



def test_schedules_page_week_list_filters(client):
    from app.schedules import now_local
    setup_admin(client)
    today = now_local().date().isoformat()
    client.post("/schedules", data={"kind": "default", "label": "Office hours", "door_ids": ["1:demo-front"],
                                    "day": today, "start": "08:00", "end": "17:00", "weekdays": [str(i) for i in range(7)]})
    client.post("/schedules", data={"kind": "override", "event_type": "rental", "label": "Gym rental", "contact": "Coach Lee",
                                    "door_ids": ["1:demo-hall", "1:demo-side"], "day": today, "start": "23:00", "end": "23:30"})
    client.post("/schedules", data={"kind": "override", "action": "lock", "label": "Holiday", "door_ids": ["1:demo-office"],
                                    "day": "2030-12-25", "start": "00:00", "end": "23:59"})
    week = client.get("/schedules?view=week").text
    assert "Office hours" not in week and "Gym rental" in week and "Holiday" not in week and "Today" in week
    assert "Office hours" in client.get("/schedules?view=week&type=all").text
    nxt = client.get("/schedules?view=week&week=2030-12-23").text
    assert "Holiday" in nxt and "Gym rental" not in nxt

    lst = client.get("/schedules?view=list").text
    assert "Holiday" in lst and "Next" in lst and "Coach Lee" in lst
    # the choice of view is remembered
    assert "Holiday" in client.get("/schedules").text
    assert "Holiday" not in client.get("/schedules?type=unlock").text
    assert "Gym rental" in client.get("/schedules?q=coach").text
    assert "Office hours" not in client.get("/schedules?q=coach&type=all").text
    assert "Fellowship Hall" in client.get("/schedules?q=fellowship").text  # door names are searchable


def test_unlock_until_custom_time(client, fake):
    from datetime import timedelta
    from app.schedules import now_local
    setup_admin(client)
    now = now_local()
    later = (now + timedelta(hours=2, minutes=10)).strftime("%H:%M")
    r = client.post("/quick-unlock", data={"door_ids": ["1:demo-side"], "until": later, "mode": "until"})
    assert "Unlocking Side Door until" in r.text
    unlock = [c for c in fake.calls if c[0] == "unlock"][-1]
    assert unlock[1] == "demo-side" and 128 <= unlock[2] <= 131

    earlier = (now - timedelta(hours=1)).strftime("%H:%M")  # already passed: means tomorrow
    client.post("/quick-unlock", data={"door_ids": ["1:demo-hall"], "until": earlier, "mode": "until"})
    unlock = [c for c in fake.calls if c[0] == "unlock"][-1]
    assert unlock[1] == "demo-hall" and 22 * 60 <= unlock[2] <= 23 * 60 + 2

    assert "doesn" in client.post("/quick-unlock", data={"door_ids": ["1:demo-front"], "until": "25:99",
                                                           "mode": "until"}).text
    r = client.post("/quick-unlock", data={"door_ids": ["1:demo-front"], "minutes": "240"})
    assert "for 4 hours" in r.text
    assert "Or unlock until" in client.get("/").text


def test_theme_setting_and_doors_list(client):
    setup_admin(client)
    assert "data-theme" not in client.get("/").text.split("<head>")[0]  # Auto by default
    client.post("/theme", data={"theme": "dark", "next": "/doors"})
    page = client.get("/doors").text
    assert '<html lang="en" data-theme="dark">' in page
    client.post("/theme", data={"theme": "light"})
    assert 'data-theme="light"' in client.get("/").text
    client.post("/theme", data={"theme": "purple"})  # unknown values fall back to Auto
    assert 'data-theme=' not in client.get("/").text.split("<head>")[0]

    client.post("/schedules", data={"kind": "default", "label": "Hours", "door_ids": ["1:demo-front"],
                                    "day": "2030-01-07", "start": "08:00", "end": "17:00", "weekdays": ["0", "1", "2", "3", "4"]})
    doors = client.get("/doors").text
    assert 'class="drow"' in doors and "Next event" in doors
    assert "Regular hours</span>" not in doors and "No regular hours" not in doors


def test_stylesheet_link_changes_with_its_content(client):
    import hashlib
    from pathlib import Path
    import app.web
    setup_admin(client)
    want = hashlib.sha256((Path(app.web.__file__).parent / "static" / "style.css").read_bytes()).hexdigest()[:10]
    assert f'/static/style.css?v={want}' in client.get("/").text
    assert client.get(f"/static/style.css?v={want}").status_code == 200
