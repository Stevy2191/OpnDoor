import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import config, sso
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def setup(c):
    c.post("/setup", data={"name": "Ada Admin", "email": "ada@example.com", "password": "password123"})
    c.post("/sites", data={"name": "North Campus", "host": "demo", "timezone": "America/New_York"})


def login(c, email, pw="password123"):
    c.post("/logout")
    return c.post("/login", data={"email": email, "password": pw})


def test_site_access_and_operator_only_sees_their_site(client, fake):
    setup(client)
    client.post("/users", data={"name": "Ollie Operator", "email": "ollie@example.com", "role": "operator",
                                "signin": "password", "password": "password123", "site_ids": ["2"]})
    people = client.get("/users").text
    assert "All doors at North Campus" in people and "Door operator" in people

    login(client, "ollie@example.com")
    doors = client.get("/doors").text
    assert "2:demo-front" in doors and "1:demo-front" not in doors
    assert "Demo site" not in doors  # other site's name is not shown
    assert "That door isn" in client.get("/doors/1:demo-front", follow_redirects=True).text
    for path in ["/users", "/sites", "/history", "/calendars"]:
        assert client.get(path).status_code == 403, path

    # a door added to the site later is included automatically
    from app.unifi import RemoteDoor
    fake.doors.append(RemoteDoor("demo-new", "New Annex", "", "lock", "close"))
    from app import engine
    engine.sync_doors()
    assert "New Annex" in client.get("/doors").text


def test_admin_gets_everything_and_role_change(client):
    setup(client)
    client.post("/users", data={"name": "Bea", "email": "bea@example.com", "role": "admin",
                                "signin": "password", "password": "password123", "door_ids": ["1:demo-front"]})
    login(client, "bea@example.com")
    assert client.get("/history").status_code == 200
    assert "North Campus" in client.get("/doors").text  # several sites: pick one first
    assert "2:demo-hall" in client.get("/site?id=2&next=/doors").text


def test_cant_demote_yourself(client):
    setup(client)
    r = client.post("/users/1", data={"name": "Ada", "email": "ada@example.com", "role": "operator",
                                      "signin": "password", "active": "true"})
    assert "can&#39;t remove your own admin" in r.text or "can't remove your own admin" in r.text


def test_history_records_who_what_when_with_before_after(client):
    setup(client)
    client.post("/schedules", data={"kind": "override", "event_type": "rental", "label": "Smith wedding",
                                    "contact": "Jane 555-0123", "door_ids": ["1:demo-hall"], "day": "2030-06-01",
                                    "start": "14:00", "end": "18:00"})
    client.post("/schedules/1", data={"kind": "override", "event_type": "rental", "label": "Smith wedding",
                                      "contact": "Jane 555-0123", "door_ids": ["1:demo-hall", "1:demo-side"],
                                      "day": "2030-06-01", "start": "13:00", "end": "18:00"})
    h = client.get("/history").text
    assert "Ada Admin added override: unlocked “Smith wedding”" in h
    assert "Ada Admin changed “Smith wedding”: doors, time" in h
    assert "Fellowship Hall, Side Door" in h and "1 PM – 6 PM" in h  # after values
    assert "Signed in" in h

    # filter by door, and the door page shows its own recent changes
    assert "Smith wedding" not in client.get("/history?door=1:demo-office").text
    assert "Smith wedding" in client.get("/history?door=1:demo-side").text
    door = client.get("/doors/1:demo-hall").text
    assert "Recent changes" in door and "changed “Smith wedding”" in door
    assert "🔑" in door and "Rental" in door and "Jane 555-0123" in door

    csv = client.get("/history.csv?what=schedule")
    assert csv.headers["content-type"].startswith("text/csv")
    assert "Changed a schedule" in csv.text and "Time: 2 PM – 6 PM → 1 PM – 6 PM" in csv.text

    client.post("/schedules/1/delete")
    assert "deleted “Smith wedding”" in client.get("/history").text


def test_failed_sign_in_is_recorded(client):
    setup(client)
    login(client, "ada@example.com", "wrong-password")
    login(client, "ada@example.com")
    h = client.get("/history?what=signin").text
    assert "refused: the password was wrong" in h


# ---- Microsoft sign-in ----

def fake_id_token(**claims):
    def enc(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


TENANT = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def ms(monkeypatch):
    monkeypatch.setattr(config, "MS_CLIENT_ID", "client-123")
    monkeypatch.setattr(config, "MS_CLIENT_SECRET", "secret")
    monkeypatch.setattr(config, "MS_TENANT_ID", TENANT)
    monkeypatch.setattr(config, "SSO_ENABLED", True)
    issued = {}

    def run(client, **overrides):
        r = client.get("/auth/microsoft", follow_redirects=False)
        loc = r.headers["location"]
        assert loc.startswith(f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize")
        from urllib.parse import parse_qs, urlparse
        q = {k: v[0] for k, v in parse_qs(urlparse(loc).query).items()}
        assert q["code_challenge_method"] == "S256"
        claims = dict(aud="client-123", iss=f"https://login.microsoftonline.com/{TENANT}/v2.0", tid=TENANT,
                      nonce=q["nonce"], exp=int(time.time()) + 600, oid="oid-1",
                      preferred_username="Mo@Example.com", name="Mo Microsoft")
        claims.update(overrides)
        issued["claims"] = claims
        monkeypatch.setattr(sso, "exchange_code", lambda code, verifier, redirect: {"id_token": fake_id_token(**claims)})
        return client.get(f"/auth/microsoft/callback?code=abc&state={q['state']}")
    return run


def test_microsoft_unknown_person_refused(client, ms):
    setup(client)
    client.post("/logout")
    r = ms(client)
    assert r.url.path == "/login" and "set up in OpnDoor yet" in r.text
    login(client, "ada@example.com")
    assert "tried to sign in with Microsoft" in client.get("/history?what=signin").text


def test_microsoft_known_person_signs_in_and_is_linked(client, ms):
    setup(client)
    client.post("/users", data={"name": "", "email": "mo@example.com", "role": "operator", "signin": "microsoft",
                                "site_ids": ["1"]})
    assert "Microsoft" in client.get("/users").text
    client.post("/logout")
    r = ms(client)
    assert r.url.path == "/" and "Hi, Mo" in r.text
    # password login doesn't work for a Microsoft-only account
    r = login(client, "mo@example.com", "anything")
    assert "signs in with Microsoft" in r.text
    # linked by Microsoft id: still works after the email changes in Microsoft
    r = ms(client, preferred_username="mo.new@example.com")
    assert r.url.path == "/"


@pytest.mark.parametrize("bad", [{"aud": "other-app"}, {"nonce": "x"}, {"tid": "99999999-2222-3333-4444-555555555555",
                                  "iss": "https://login.microsoftonline.com/99999999-2222-3333-4444-555555555555/v2.0"},
                                 {"exp": 1000}])
def test_microsoft_bad_tokens_rejected(client, ms, bad):
    setup(client)
    client.post("/users", data={"name": "Mo", "email": "mo@example.com", "role": "operator", "signin": "microsoft"})
    client.post("/logout")
    r = ms(client, **bad)
    assert r.url.path == "/login"


def test_callback_without_start_is_rejected(client, ms):
    setup(client)
    client.post("/logout")
    r = client.get("/auth/microsoft/callback?code=abc&state=forged")
    assert r.url.path == "/login" and "expired" in r.text
