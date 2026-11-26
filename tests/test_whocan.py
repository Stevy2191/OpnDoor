"""Who can get into a door."""
from app import whocan
from tests.test_access import ALWAYS, unifi_people
from tests.test_web import client, setup_admin  # noqa: F401


def test_who_can_get_in(client, fake):
    setup_admin(client)
    unifi_people(fake)
    whocan._cache.clear()
    r = client.get("/doors/1:demo-hall/who").text  # through a nested door group
    assert "Uma" in r and "Staff" in r and "Any time" in r
    assert "Off" not in r  # deactivated in UniFi
    assert "Ada Admin" in r and "Main admin" in r  # OpnDoor users
    assert "No one in UniFi Access" in client.get("/doors/1:demo-side/who").text
    assert "Who can get in" in client.get("/doors/1:demo-hall").text


def test_hours_text():
    assert whocan.hours_text(None) == "Any time"
    week = {d: [["07:00", "18:00"]] for d in whocan.DAYS[:5]} | {"saturday": [], "sunday": []}
    assert whocan.hours_text(week) == "Mon–Fri 07:00–18:00"
    assert ALWAYS["id"]
