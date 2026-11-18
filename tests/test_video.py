"""Door tiles, camera pictures from UniFi Protect, and matching doors to cameras."""
import httpx
from sqlmodel import select

from app import engine, protect
from app.db import session_scope
from app.models import Door, Site
from tests.test_access import login
from tests.test_web import client, setup_admin  # noqa: F401


def test_demo_cameras_and_tiles(client):
    setup_admin(client)
    with session_scope() as s:
        assert s.get(Door, "1:demo-front").camera_id == "cam-demo-front"  # matched by name on sync
    r = client.get("/doors/1:demo-front/snapshot")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/")
    client.post("/doors/view", data={"view": "tiles"})
    page = client.get("/doors").text
    assert 'class="dtiles"' in page and "Open<span class=\"dopname\"> Front Entrance" in page and "/doors/1%3Ademo-front/snapshot" in page
    client.post("/doors/view", data={"view": "list"})
    assert 'class="dtiles"' not in client.get("/doors").text


def test_snapshot_only_for_your_doors(client):
    setup_admin(client)
    client.post("/users", data={"name": "Gus", "email": "gus@example.com", "password": "password123",
                                "signin": "password", "role": "opener", "door_ids": ["1:demo-side"]})
    login(client, "gus@example.com")
    assert client.get("/doors/1:demo-side/snapshot").status_code == 200
    assert client.get("/doors/1:demo-front/snapshot").status_code == 404
    assert 'class="dtiles"' in client.get("/doors").text  # door openers get tiles by default
    r = client.post("/doors/1:demo-side/camera", data={"camera_id": ""}, follow_redirects=False)
    with session_scope() as s:
        assert s.get(Door, "1:demo-side").camera_id  # openers can't change cameras


def test_admin_picks_a_camera(client):
    setup_admin(client)
    client.post("/doors/1:demo-hall/camera", data={"camera_id": "cam-demo-front"})
    with session_scope() as s:
        assert s.get(Door, "1:demo-hall").camera_id == "cam-demo-front"
    assert "Camera for this door" in client.get("/doors/1:demo-hall").text
    assert "changed the camera for Fellowship Hall" in client.get("/history").text


def test_real_protect_requests(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, **kw):
        calls.append((url, headers, params))
        req = httpx.Request("GET", url)
        if url.endswith("/v1/cameras"):
            return httpx.Response(200, json=[{"id": "c1", "name": "EMS2-BAY-DOOR1"},
                                             {"id": "c2", "name": "Front Door Reader"},
                                             {"id": "c3", "name": "Parking Lot"}], request=req)
        return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"}, request=req)
    monkeypatch.setattr(protect.httpx, "get", fake_get)
    monkeypatch.setattr(protect.config, "DRY_RUN", False)
    with session_scope() as s:
        site = Site(name="EMS2", host="https://10.1.2.3:12445", token="t", protect_key="KEY")
        s.add(site)
        s.flush()
        for rid, name in [("a", "EMS2-BAY-DOOR1"), ("b", "Front Door"), ("c", "Gym")]:
            s.add(Door(id=f"{site.id}:{rid}", site_id=site.id, remote_id=rid, name=name))
        s.flush()
        assert protect.match(s, site) == 2
        assert s.get(Door, f"{site.id}:a").camera_id == "c1" and s.get(Door, f"{site.id}:b").camera_id == "c2"
        assert not s.get(Door, f"{site.id}:c").camera_id
        body, ctype = protect.snapshot(site, "c1")
        assert body.startswith(b"\xff\xd8") and ctype == "image/jpeg"
    assert calls[0][0] == "https://10.1.2.3/proxy/protect/integration/v1/cameras"  # same console, port 443
    assert calls[0][1] == {"X-API-KEY": "KEY"}
    assert calls[1][0].endswith("/v1/cameras/c1/snapshot")


def test_bad_protect_key_message(monkeypatch):
    monkeypatch.setattr(protect.httpx, "get",
                        lambda url, **kw: httpx.Response(401, request=httpx.Request("GET", url)))
    monkeypatch.setattr(protect.config, "DRY_RUN", False)
    site = Site(id=9, name="X", host="https://h:12445", protect_key="bad", protect_host="nvr.local")
    assert protect.base_url(site) == "https://nvr.local"
    try:
        protect.cameras(site)
        raise AssertionError("expected an error")
    except protect.ProtectError as e:
        assert "Control Plane" in str(e)
    assert engine  # imported for side effects only


def test_match_through_the_doors_readers(monkeypatch):
    """Door names often differ from camera names; the reader bound to the door links them."""
    from app import unifi

    class Console:
        def door_devices(self):
            return {"kit": ["EMS2-DORM-KIT-EXIT", "EMS2 - DORM KITCHEN"], "o2": ["EMS2-O2RM-HUB - Exit", "EMS2-O2RM - Entry"]}
    monkeypatch.setattr(unifi, "get_client", lambda site: Console())
    cams = [{"id": "c-kit", "name": "EMS2 - DORM KITCHEN"}, {"id": "c-o2", "name": "EMS2-O2RM - Entry"},
            {"id": "c-conf-in", "name": "EMS2-CONF-EXIT-HUB - Entry"}, {"id": "c-conf-out", "name": "EMS2-CONF-EXIT-HUB - Exit"},
            {"id": "c-bay", "name": "Apparatus Bay"}]
    with session_scope() as s:
        site = Site(name="EMS 2", host="https://10.4.60.10:12445", token="t", protect_key="K")
        s.add(site)
        s.flush()
        for rid, name in [("kit", "EMS2-DORM-KIT-HUB"), ("o2", "EMS2-O2RM-HUB"), ("conf", "EMS2-CONF-EXIT-HUB"),
                          ("bay", "EMS2-BAY-DOOR1")]:
            s.add(Door(id=f"{site.id}:{rid}", site_id=site.id, remote_id=rid, name=name))
        s.flush()
        assert protect.match(s, site, cams) == 3
        got = {d.remote_id: d.camera_id for d in s.exec(select(Door).where(Door.site_id == site.id)).all()}
        assert got == {"kit": "c-kit", "o2": "c-o2", "conf": "c-conf-in", "bay": ""}  # bay: pick by hand


def test_hide_and_show_a_door(client, fake):
    setup_admin(client)
    client.post("/users", data={"name": "Gus", "email": "gus@example.com", "password": "password123",
                                "signin": "password", "role": "operator", "site_ids": ["1"]})
    assert "Hide this door" in client.get("/doors/1:demo-side").text
    r = client.post("/doors/1:demo-side/hide", data={"hide": "true"}, follow_redirects=True)
    assert "Hid Side Door" in r.text
    assert 'data-open="1:demo-side"' not in client.get("/doors").text
    assert client.get("/doors/1:demo-side", follow_redirects=False).headers["location"] == "/doors"
    site = client.get("/sites/1").text
    assert "Hidden doors" in site and "Show again" in site

    # nobody can use it: not the operator with the whole site, not even through the API
    login(client, "gus@example.com")
    assert "Side Door" not in client.get("/doors").text
    client.post("/quick-unlock", data={"door_ids": "1:demo-side", "mode": "open"})
    assert ("open", "demo-side") not in fake.calls
    assert client.post("/doors/1:demo-side/hide", data={"hide": "false"}, follow_redirects=True).status_code == 200
    with session_scope() as s:
        assert s.get(Door, "1:demo-side").hidden  # operators can't un-hide

    login(client, "ada@example.com")
    client.post("/doors/1:demo-side/hide", data={"hide": "false"})
    assert 'data-open="1:demo-side"' in client.get("/doors").text
    assert "hid the door Side Door" in client.get("/history").text
