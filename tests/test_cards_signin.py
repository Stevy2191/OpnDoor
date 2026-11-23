"""Adding NFC cards through OpnDoor, and setting up Microsoft sign-in in the web."""
from app import unifi
from tests.test_web import client, setup_admin  # noqa: F401


def person(fake):
    fake.users = [{"id": "u1", "first_name": "Kim", "last_name": "Diaz", "status": "ACTIVE", "nfc_cards": []},
                  {"id": "u2", "first_name": "Lee", "last_name": "Ray", "status": "ACTIVE",
                   "nfc_cards": [{"id": "000777", "token": "taken"}]}]


def test_enroll_a_card_by_tapping(client, fake):
    setup_admin(client)
    person(fake)
    page = client.get("/unifi-people/1/u1/card").text
    assert "Add a card" in page and "Front Entrance reader" in page
    r = client.post("/unifi-people/1/u1/card/start", data={"device": "rdr-demo-front"}).json()
    assert r["state"] == "waiting"
    sid = r["session"]
    fake.next_card = "abc123def456"
    assert client.get(f"/unifi-people/1/u1/card/{sid}").json()["state"] == "waiting"
    done = client.get(f"/unifi-people/1/u1/card/{sid}").json()
    assert done["state"] == "done"
    assert fake.users[0]["nfc_cards"][0]["token"] == "abc123def456"
    # that session is finished: polling it again is refused
    assert client.get(f"/unifi-people/1/u1/card/{sid}").status_code == 404
    # remove it again
    client.post("/unifi-people/1/u1/card-remove", data={"token": "abc123def456"})
    assert fake.users[0]["nfc_cards"] == []


def test_card_already_owned_and_bad_reader(client, fake):
    setup_admin(client)
    person(fake)
    assert client.post("/unifi-people/1/u1/card/start", data={"device": "nope"}).status_code == 400
    sid = client.post("/unifi-people/1/u1/card/start", data={"device": "rdr-demo-front"}).json()["session"]
    fake.next_card = "taken"
    client.get(f"/unifi-people/1/u1/card/{sid}")
    r = client.get(f"/unifi-people/1/u1/card/{sid}").json()
    assert r["state"] == "error" and "already belongs" in r["message"]
    # someone else's session can't be polled
    assert client.get("/unifi-people/1/u1/card/notmine").status_code == 404


def test_real_client_tries_both_card_paths():
    calls = []

    def fake_call(method, path, **kw):
        calls.append((method, path))
        if path.startswith("/credentials/"):
            raise unifi.UnifiError("UniFi Access error 404: not found")
        if method == "POST":
            return {"session_id": "s1"}
        return {"token": "tok", "card_id": "123456"}
    c = unifi.UnifiClient.__new__(unifi.UnifiClient)
    c._call = fake_call
    assert c.card_session_start("dev") == "s1"
    assert c.card_session_poll("s1") == {"token": "tok", "card_id": "123456"}
    assert ("POST", "/credentials/nfc_cards/sessions") in calls and ("POST", "/nfc_cards/sessions") in calls


def test_microsoft_signin_set_up_in_the_web(client):
    setup_admin(client)
    client.post("/logout")
    assert "/auth/microsoft" not in client.get("/login").text  # off until set up
    client.post("/login", data={"email": "ada@example.com", "password": "password123"})
    page = client.get("/settings/signin").text
    assert "/auth/microsoft/callback" in page and "Not set up" in page
    bad = client.post("/settings/signin", data={"tenant": "nope", "client_id": "x"}, follow_redirects=True)
    assert "look like" in bad.text
    tenant, cid = "11111111-2222-3333-4444-555555555555", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    r = client.post("/settings/signin", data={"tenant": tenant, "client_id": cid, "secret": "s3cret",
                                              "public_url": "doors.example.org", "on": "true"}, follow_redirects=True)
    assert "Microsoft sign-in is on" in r.text and "s3cret" not in r.text
    assert "https://doors.example.org/auth/microsoft/callback" in r.text
    go = client.get("/auth/microsoft", follow_redirects=False)
    assert go.status_code == 303 and f"/{tenant}/oauth2/v2.0/authorize" in go.headers["location"] and cid in go.headers["location"]
    client.post("/logout")
    assert "/auth/microsoft" in client.get("/login").text
