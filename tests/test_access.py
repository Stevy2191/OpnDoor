"""Door openers, 'Open now', delete from home, and doors that follow UniFi Access permissions."""
import json
from datetime import datetime

from sqlmodel import select

from app import access
from app.db import session_scope
from app.models import Schedule, UnifiGrant, User
from tests.test_web import client, setup_admin  # noqa: F401

ALWAYS = {"id": "s-always", "weekly": {d: [{"start_time": "00:00:00", "end_time": "23:59:59"}] for d in access.DAYS}}
NEVER = {"id": "s-never", "weekly": {d: [] for d in access.DAYS}}


def login(c, email):
    c.post("/logout")
    c.post("/login", data={"email": email, "password": "password123"})


def test_open_now_is_momentary_and_logged(client, fake):
    setup_admin(client)
    r = client.post("/quick-unlock", data={"door_ids": "1:demo-front", "mode": "open", "next": "/"})
    assert "Opened Front Entrance" in r.text
    assert ("open", "demo-front") in fake.calls
    with session_scope() as s:
        assert not s.exec(select(Schedule)).all()  # nothing to undo: UniFi relocks by itself
    assert "Front Entrance" in client.get("/history").text and "opened" in client.get("/history").text


def test_delete_from_home(client):
    setup_admin(client)
    client.post("/schedules", data={"label": "Board meeting", "door_ids": ["1:demo-hall"], "day": "2030-01-02",
                                    "start": "18:00", "end": "20:00"})
    with session_scope() as s:
        sid = s.exec(select(Schedule)).first().id
    r = client.post(f"/schedules/{sid}/delete", data={"next": "/"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    with session_scope() as s:
        assert s.get(Schedule, sid) is None


def test_door_opener_cannot_schedule(client, fake):
    setup_admin(client)
    client.post("/users", data={"name": "Gus", "email": "gus@example.com", "password": "password123",
                                "signin": "password", "role": "opener", "door_ids": ["1:demo-side"]})
    login(client, "gus@example.com")
    home = client.get("/").text
    assert "Door opener" in home and "Open now" in home and "+ New schedule" not in home
    assert 'href="/schedules"' not in home
    assert client.get("/schedules/new").status_code == 403
    client.post("/schedules", data={"label": "Sneaky", "door_ids": ["1:demo-side"], "day": "2030-01-02",
                                    "start": "18:00", "end": "19:00"})
    with session_scope() as s:
        assert not s.exec(select(Schedule).where(Schedule.label == "Sneaky")).all()
    client.post("/quick-unlock", data={"door_ids": "1:demo-side", "mode": "open"})
    client.post("/quick-unlock", data={"door_ids": "1:demo-front", "mode": "open"})  # not theirs
    assert ("open", "demo-side") in fake.calls and ("open", "demo-front") not in fake.calls


def unifi_people(fake, schedule=ALWAYS):
    fake.users = [{"id": "u-1", "first_name": "Uma", "user_email": "UMA@example.com", "status": "ACTIVE"},
                  {"id": "u-2", "first_name": "Off", "user_email": "off@example.com", "status": "DEACTIVATED"}]
    fake.groups = {"g-1": {"id": "g-1", "name": "Front", "resources": [{"id": "demo-front", "type": "door"},
                                                                      {"id": "g-2", "type": "door_group"}]},
                   "g-2": {"id": "g-2", "name": "Nested", "resources": [{"id": "demo-hall", "type": "door"}]}}
    fake.schedules = {schedule["id"]: schedule}
    fake.policies = {"p-1": {"id": "p-1", "name": "Staff", "schedule_id": schedule["id"],
                             "resources": [{"id": "g-1", "type": "door_group"}]}}
    fake.user_policy_ids = {"u-1": ["p-1"], "u-2": ["p-1"]}


def test_doors_follow_unifi_access(client, fake):
    setup_admin(client)
    unifi_people(fake)
    client.post("/users", data={"name": "Uma", "email": "uma@example.com", "password": "password123",
                                "signin": "password", "role": "opener", "door_source": "unifi",
                                "door_ids": ["1:demo-side"]})  # ignored: doors come from UniFi
    with session_scope() as s:
        u = s.exec(select(User).where(User.email == "uma@example.com")).one()
        assert u.follows_unifi and not u.can_schedule
        assert {g.door_id for g in s.exec(select(UnifiGrant)).all()} == {"1:demo-front", "1:demo-hall"}
        assert "2 doors" in u.unifi_note
    assert "From UniFi Access" in client.get("/users").text
    login(client, "uma@example.com")
    home = client.get("/").text
    assert "Front Entrance" in home and "Fellowship Hall" in home and "Side Door" not in home

    # access removed in UniFi: gone after the next check
    login(client, "ada@example.com")
    fake.user_policy_ids = {"u-1": []}
    client.post("/users/unifi-check")
    with session_scope() as s:
        assert not s.exec(select(UnifiGrant)).all()


def test_unifi_hours_limit_opening(client, fake):
    setup_admin(client)
    unifi_people(fake, NEVER)
    client.post("/users", data={"name": "Uma", "email": "uma@example.com", "password": "password123",
                                "signin": "password", "role": "operator", "door_source": "unifi"})
    login(client, "uma@example.com")
    r = client.post("/quick-unlock", data={"door_ids": "1:demo-front", "mode": "open"})
    assert "hours don" in r.text and ("open", "demo-front") not in fake.calls
    r = client.post("/quick-unlock", data={"door_ids": "1:demo-front", "minutes": "15"})
    assert "hours don" in r.text


def test_hours_check():
    today = access.DAYS[datetime.now().weekday()]
    hours = {d: [] for d in access.DAYS}
    hours[today] = [["00:00", "23:59"]]

    class U:
        follows_unifi, id = True, 1
    with session_scope() as s:
        from app.models import Door, Site
        s.add(Site(id=1, name="X", host="h", token="t"))
        s.flush()
        s.add(Door(id="1:a", site_id=1, remote_id="a", name="A"))
        s.add(User(id=1, email="x@example.com"))
        s.flush()
        s.add(UnifiGrant(user_id=1, door_id="1:a", hours=json.dumps(hours)))
        s.flush()
        assert access.open_hours_ok(s, U, s.get(Door, "1:a"))[0]
        hours[today] = []
        s.get(UnifiGrant, (1, "1:a")).hours = json.dumps(hours)
        assert not access.open_hours_ok(s, U, s.get(Door, "1:a"))[0]


def test_start_page_and_open_from_doors_list(client, fake):
    setup_admin(client)
    client.post("/sites", data={"name": "Fairgrounds", "host": "https://10.0.0.2:12445", "token": "t",
                                "timezone": "America/Chicago"})
    client.post("/users", data={"name": "Gus", "email": "gus@example.com", "password": "password123",
                                "signin": "password", "role": "opener", "site_ids": ["1", "2"]})
    login(client, "gus@example.com")
    # door openers land on Doors; with two sites they pick one first
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/doors"
    page = client.get("/doors").text
    assert "Fairgrounds" in page and 'href="/site?id=2' in page and 'data-open=' not in page  # site picker
    client.get("/site?id=1&next=/doors")
    page = client.get("/doors").text
    assert 'data-open="1:demo-front"' in page and "Open or unlock" in page
    r = client.post("/quick-unlock", data={"door_ids": "1:demo-front", "mode": "open", "next": "/doors"},
                    follow_redirects=False)
    assert r.headers["location"] == "/doors" and ("open", "demo-front") in fake.calls
    # they can choose another start page themselves
    client.post("/account", data={"name": "Gus", "start_page": "map"})
    assert client.get("/", follow_redirects=False).headers["location"] == "/map"
    assert client.get("/home").status_code == 200

    # admins keep Home unless changed
    login(client, "ada@example.com")
    assert client.get("/").status_code == 200
