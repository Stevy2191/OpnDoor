"""Lockdown / evacuation, keeping a door unlocked until set back, door position, the new look."""
from sqlmodel import select

from app import engine
from app.db import session_scope
from app.models import Door, DoorState, Schedule, Site
from tests.test_access import login
from tests.test_web import client, setup_admin  # noqa: F401


def test_lockdown_holds_doors_and_blocks_opening(client, fake):
    setup_admin(client)
    r = client.post("/sites/1/emergency", data={"mode": "lockdown", "next": "/home"}, follow_redirects=True)
    assert "Lockdown started" in r.text and "Lockdown is on at" in r.text and "End lockdown" in r.text
    assert ("emergency", "lockdown") in fake.calls
    # nobody can open or unlock while it's on, and schedules don't touch the doors
    r = client.post("/quick-unlock", data={"door_ids": "1:demo-front", "mode": "open"}, follow_redirects=True)
    assert "Lockdown is on" in r.text and ("open", "demo-front") not in fake.calls
    client.post("/quick-unlock", data={"door_ids": "1:demo-front", "minutes": "15"})
    engine.reconcile()
    assert not [c for c in fake.calls if c[0] == "unlock"]
    # ending it puts doors back on their schedules
    r = client.post("/sites/1/emergency", data={"mode": "normal", "next": "/home"}, follow_redirects=True)
    assert "Lockdown ended" in r.text and "Lockdown is on at" not in r.text
    assert ("emergency", "normal") in fake.calls
    assert "started lockdown at" in client.get("/history").text


def test_only_managers_can_start_an_emergency(client, fake):
    setup_admin(client)
    client.post("/users", data={"name": "Gus", "email": "gus@example.com", "password": "password123",
                                "signin": "password", "role": "operator", "site_ids": ["1"]})
    login(client, "gus@example.com")
    assert "Start lockdown" not in client.get("/home").text
    client.post("/sites/1/emergency", data={"mode": "lockdown"})
    assert ("emergency", "lockdown") not in fake.calls


def test_lockdown_started_in_unifi_shows_up(client, fake):
    setup_admin(client)
    fake.emergency = "evacuation"
    engine.refresh_states()
    page = client.get("/home").text
    assert "Evacuation is on at Demo site" in page and "UniFi Access app" in page
    fake.emergency = ""
    engine.refresh_states()
    assert "Evacuation is on" not in client.get("/home").text


def test_door_position_and_standing_open(client, fake):
    setup_admin(client)
    fake.doors[0].position_status = "open"
    engine.refresh_states()
    with session_scope() as s:
        assert s.get(Door, "1:demo-front").position_status == "open"
    home = client.get("/home").text
    assert "Standing open" in home
    assert "Standing open" in client.get("/doors").text


def test_keep_unlocked_until_set_back(client, fake):
    setup_admin(client)
    r = client.post("/quick-unlock", data={"door_ids": "1:demo-hall", "mode": "hold"}, follow_redirects=True)
    assert "stays unlocked until someone sets it back" in r.text
    engine.reconcile()
    assert ("hold", "demo-hall") in fake.calls  # UniFi's keep-unlocked rule, not a timer
    n = len(fake.calls)
    engine.reconcile()
    assert len(fake.calls) == n  # held doors aren't re-sent every pass
    client.post("/doors/1:demo-hall/lock-now")
    engine.reconcile()
    assert fake.calls[-1] == ("reset", "demo-hall")
    with session_scope() as s:
        assert not s.get(DoorState, "1:demo-hall").mode


def test_new_look_assets(client):
    setup_admin(client)
    page = client.get("/home").text
    assert "fonts.googleapis.com/css2?family=Outfit" in page and 'class="sidebar"' in page
