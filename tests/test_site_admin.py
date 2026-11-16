import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def login(c, email, pw="password123"):
    c.post("/logout")
    return c.post("/login", data={"email": email, "password": pw})


def add(c, name, email, **data):
    data.setdefault("signin", "password")
    data.setdefault("password", "password123")
    return c.post("/users", data={"name": name, "email": email, **data})


@pytest.fixture
def world(client):
    """Admin, two sites, a site admin for North, an operator at each site."""
    c = client
    c.post("/setup", data={"name": "Ada Admin", "email": "ada@example.com", "password": "password123"})
    c.post("/sites", data={"name": "North Campus", "host": "demo", "timezone": "America/New_York"})
    add(c, "Sid Siteadmin", "sid@example.com", role="site_admin", site_ids=["2"])
    add(c, "Nora North", "nora@example.com", role="operator", site_ids=["2"])
    add(c, "Dan Demo", "dan@example.com", role="operator", door_ids=["1:demo-front"])
    add(c, "Bo Both", "bo@example.com", role="operator", door_ids=["1:demo-side", "2:demo-side"])
    login(c, "sid@example.com")
    return c


def test_site_admin_sees_only_their_site(world):
    c = world
    assert "2:demo-front" in c.get("/doors").text and "1:demo-front" not in c.get("/doors").text
    for path in ["/users", "/calendars", "/history"]:
        assert c.get(path).status_code == 200, path
    assert c.get("/sites").status_code == 403
    nav = c.get("/").text
    assert 'href="/users"' in nav and 'href="/sites"' not in nav and "Site admin" in nav


def test_site_admin_people_list_and_limits(world):
    c = world
    people = c.get("/users").text
    assert "Nora North" in people and "Bo Both" in people
    assert "Dan Demo" not in people  # only works at the other site
    assert "Ada Admin" not in people
    assert "+ other sites" in people  # Bo also works elsewhere, without naming the site
    assert "Demo site" not in people
    # can't open admins, other site admins, or other sites' people
    assert "Only an admin" in c.get("/users/1", follow_redirects=True).text
    assert "Only an admin" in c.get("/users/4", follow_redirects=True).text  # Dan


def test_site_admin_adds_operator_only_and_cannot_grant_other_sites(world):
    c = world
    r = add(c, "Eve", "eve@example.com", role="admin", site_ids=["1", "2"], door_ids=["1:demo-hall"])
    assert "Saved Eve" in r.text
    login(c, "ada@example.com")
    people = c.get("/users").text
    row = people[people.index("<strong>Eve<"):people.index("<strong>Eve<") + 600]
    assert "Door operator" in row and "All doors at North Campus" in row
    assert "Demo site" not in row and "Fellowship Hall" not in row


def test_partial_edit_keeps_access_at_other_sites(world):
    c = world
    bo = c.get("/users/5").text
    assert "also works at other sites" in bo and "readonly" in bo
    # site admin removes Bo's North door; Bo keeps the Demo site door, name can't change
    c.post("/users/5", data={"name": "Hacked", "email": "bo@example.com", "role": "operator", "signin": "password"})
    login(c, "ada@example.com")
    people = c.get("/users").text
    row = people[people.index("Bo Both"):people.index("Bo Both") + 600]
    assert "Side Door" in row and "Hacked" not in people


def test_existing_operator_from_other_site_gets_access_added(world):
    c = world
    add(c, "Dan Demo", "dan@example.com", role="operator", door_ids=["2:demo-hall"])
    login(c, "ada@example.com")
    people = c.get("/users").text
    row = people[people.index("Dan Demo"):people.index("Dan Demo") + 700]
    assert "Front Entrance" in row and "Fellowship Hall" in row


def test_site_admin_history_is_scoped(world):
    c = world
    login(c, "ada@example.com")
    c.post("/schedules", data={"label": "Demo-site party", "door_ids": ["1:demo-hall"], "day": "2030-01-01",
                               "start": "10:00", "end": "11:00"})
    c.post("/schedules", data={"label": "North picnic", "door_ids": ["2:demo-hall"], "day": "2030-01-01",
                               "start": "10:00", "end": "11:00"})
    login(c, "sid@example.com")
    h = c.get("/history").text
    assert "North picnic" in h and "Demo-site party" not in h
    assert "Nora North" in h  # people changes at their site
    assert "Dan Demo" not in h
    assert "Demo-site party" not in c.get("/history.csv").text


def test_site_admin_calendars_limited_to_their_doors(world):
    c = world
    login(c, "ada@example.com")
    c.post("/calendars", data={"name": "Demo bookings", "url": "http://127.0.0.1:9/x.ics", "enabled": "on",
                               "default_doors": ["1:demo-front"]})
    login(c, "sid@example.com")
    assert "Demo bookings" not in c.get("/calendars").text
    assert c.get("/calendars/1", follow_redirects=True).url.path == "/calendars"
    # a calendar using another site's door has that door dropped
    r = c.post("/calendars", data={"name": "North bookings", "url": "http://127.0.0.1:9/n.ics", "enabled": "on",
                                   "default_doors": ["2:demo-front", "1:demo-side"]}, follow_redirects=True)
    assert "North bookings" in c.get("/calendars").text
    from app.db import session_scope
    from app.models import CalendarFeed
    from sqlmodel import select
    with session_scope() as s:
        feed = s.exec(select(CalendarFeed).where(CalendarFeed.name == "North bookings")).one()
        assert feed.default_door_ids == "2:demo-front"


def test_site_admin_needs_a_site(client):
    client.post("/setup", data={"name": "Ada", "email": "ada@example.com", "password": "password123"})
    r = add(client, "Sid", "sid@example.com", role="site_admin")
    assert "Pick at least one site" in r.text


def test_operator_cannot_reach_manager_pages(world):
    c = world
    login(c, "nora@example.com")
    for path in ["/users", "/calendars", "/history", "/sites"]:
        assert c.get(path).status_code == 403, path


def test_main_admin_is_protected(client):
    c = client
    c.post("/setup", data={"name": "Ada", "email": "ada@example.com", "password": "password123"})
    assert "Main admin" in c.get("/users").text
    add(c, "Ben", "ben@example.com", role="admin")
    login(c, "ben@example.com")
    assert "Only an admin" in c.get("/users/1", follow_redirects=True).text
    c.post("/users/1", data={"name": "x", "email": "ada@example.com", "role": "operator", "signin": "password"})
    login(c, "ada@example.com")
    r = c.get("/users").text
    assert "★ Main admin" in r and ">x<" not in r


def test_setup_code_required(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "SETUP_CODE", "ABCD-1234")
    r = client.post("/setup", data={"name": "Mal", "email": "mal@example.com", "password": "password123",
                                    "setup_code": "nope"})
    assert "setup code isn" in r.text
    r = client.post("/setup", data={"name": "Ada", "email": "ada@example.com", "password": "password123",
                                    "setup_code": "abcd-1234"})
    assert "Hi, Ada" in r.text
