"""Home with many sites: a quiet Emergency bar with steps, one row per site, home site at sign-in."""
from datetime import timedelta

from app.db import session_scope
from app.models import Door, Site, User, utcnow
from tests.test_web import client, setup_admin  # noqa: F401


def two_sites(client):
    setup_admin(client)
    client.post("/sites", data={"name": "North", "host": "demo", "timezone": "America/Chicago"})


def test_one_site_keeps_its_emergency_buttons(client, fake):
    setup_admin(client)
    home = client.get("/").text
    assert "Start lockdown" in home and "Emergency…" not in home


def test_several_sites_get_one_quiet_emergency_bar(client, fake):
    two_sites(client)
    home = client.get("/").text
    assert home.count("Start lockdown") == 0 and "Emergency…" in home and 'action="/emergency"' in home
    assert "Nothing happens until you confirm" in home
    # without the last step's confirmation nothing is started
    r = client.post("/emergency", data={"mode": "lockdown", "site_ids": ["1", "2"], "next": "/home"}, follow_redirects=True)
    assert "Nothing was started" in r.text and ("emergency", "lockdown") not in fake.calls
    r = client.post("/emergency", data={"mode": "lockdown", "site_ids": ["1"], "confirm": "evacuation"}, follow_redirects=True)
    assert "Nothing was started" in r.text
    r = client.post("/emergency", data={"mode": "lockdown", "site_ids": ["1", "2"], "confirm": "lockdown", "next": "/home"},
                    follow_redirects=True)
    assert "Lockdown started at Demo site, North" in r.text
    assert fake.calls.count(("emergency", "lockdown")) == 2
    with session_scope() as s:
        assert s.get(Site, 1).emergency == "lockdown" and s.get(Site, 2).emergency == "lockdown"
    assert "Lockdown is on at North" in r.text  # the banner on every page
    assert "started lockdown at North" in client.get("/history").text


def test_doors_count_as_standing_open_after_a_few_minutes(client, fake):
    setup_admin(client)
    with session_scope() as s:
        brief, long = s.get(Door, "1:demo-front"), s.get(Door, "1:demo-side")
        brief.position_status, brief.position_since = "open", utcnow() - timedelta(seconds=30)
        long.position_status, long.position_since = "open", utcnow() - timedelta(minutes=20)
        s.add(brief)
        s.add(long)
        s.commit()
    home = client.get("/").text
    now = home[home.index('id="now"'):home.index('id="today"')]
    assert "Side Door" in now and "Front Entrance" not in now and "open since" in now


def test_home_site_is_picked_at_sign_in(client, fake):
    two_sites(client)
    r = client.post("/account", data={"name": "Ada Admin", "home_site": "2"}, follow_redirects=True)
    assert 'value="2" selected' in r.text
    with session_scope() as s:
        assert s.get(User, 1).home_site_id == 2
    client.post("/logout")
    client.post("/login", data={"email": "ada@example.com", "password": "password123"})
    home = client.get("/").text
    assert "North · 4 doors" in home and "Your sites" not in home
    client.get("/site?id=&next=/")
    assert "Your sites" in client.get("/").text  # can still look at every site
