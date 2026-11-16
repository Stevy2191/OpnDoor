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
                                    "door_ids": ["demo-side"]})
    assert r.status_code == 200 and "Bob" in r.text

    # admin schedules a weekly door
    r = client.post("/schedules", data={"label": "Choir practice", "door_ids": ["demo-hall"], "day": "2030-01-02",
                                        "start": "18:00", "end": "20:30", "repeat": "weekly", "weekdays": ["2"]})
    assert r.status_code == 200
    assert "Choir practice" in client.get("/schedules").text

    for path in ["/users", "/users/new", "/calendars", "/activity", "/calendars/new", "/doors", "/account"]:
        assert client.get(path).status_code == 200, path

    client.post("/logout")
    r = client.post("/login", data={"email": "bob@example.com", "password": "password123"})
    assert r.url.path == "/"
    assert "Side Door" in r.text and "Front Entrance" not in r.text
    assert "Choir practice" not in client.get("/schedules").text  # not his door
    assert client.get("/users").status_code == 403

    # Bob can't sneak another door in
    client.post("/schedules", data={"label": "Sneaky", "door_ids": ["demo-front"], "day": "2030-01-02",
                                    "start": "18:00", "end": "19:00"})
    assert "Sneaky" not in client.get("/schedules").text

    # Quick unlock his own door: reconciled right away
    r = client.post("/quick-unlock", data={"door_ids": ["demo-side"], "minutes": "30"})
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
    r = client.post("/schedules", data={"label": "Old", "door_ids": ["demo-front"], "day": "2020-01-01",
                                        "start": "10:00", "end": "11:00"})
    assert "already passed" in r.text


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_multi_door_preselect(client):
    import re
    setup_admin(client)
    r = client.get("/schedules/new?door=demo-front&door=demo-hall")
    checked = set(re.findall(r'name="door_ids" value="([^"]+)" checked', r.text))
    assert checked == {"demo-front", "demo-hall"}
    # a door the user can't use is never offered, even if passed in the URL
    client.post("/users", data={"name": "Bob", "email": "bob@example.com", "password": "password123",
                                "door_ids": ["demo-side"]})
    client.post("/logout")
    client.post("/login", data={"email": "bob@example.com", "password": "password123"})
    r = client.get("/schedules/new?door=demo-front&door=demo-side")
    assert set(re.findall(r'name="door_ids" value="([^"]+)" checked', r.text)) == {"demo-side"}
    assert "demo-front" not in r.text
