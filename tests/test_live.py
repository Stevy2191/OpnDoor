"""Live video: Protect stream lookup, go2rtc registration, the WebSocket relay and its permission checks."""
import json
import threading

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect
from websockets.sync.server import serve

from app import config, live, protect
from app.db import session_scope
from app.models import Site
from tests.test_access import login
from tests.test_web import client, setup_admin  # noqa: F401


@pytest.fixture
def helper(monkeypatch):
    """A stand-in for go2rtc: records stream registrations and answers the MSE handshake."""
    seen = {"put": [], "ws": []}

    def handler(conn):
        seen["ws"].append(conn.request.path)
        hello = json.loads(conn.recv())
        assert hello["type"] == "mse" and "avc1" in hello["value"]
        conn.send(json.dumps({"type": "mse", "value": 'video/mp4; codecs="avc1.640029"'}))
        for chunk in (b"\x00\x00\x00\x18ftypiso5", b"moof-1", b"moof-2"):
            conn.send(chunk)
        try:
            conn.recv(timeout=5)
        except Exception:  # noqa: BLE001 - browser went away
            pass

    server = serve(handler, "127.0.0.1", 0)
    port = server.socket.getsockname()[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real_put = httpx.put

    def fake_put(url, params=None, **kw):
        if "/api/streams" in url:
            seen["put"].append(params)
            return httpx.Response(200, request=httpx.Request("PUT", url))
        return real_put(url, params=params, **kw)

    def fake_get(url, headers=None, params=None, **kw):
        req = httpx.Request("GET", url)
        if url.endswith("/rtsps-stream"):
            return httpx.Response(200, json={"high": "rtsps://10.0.0.1:7441/HIGH?enableSrtp",
                                             "low": "rtsps://10.0.0.1:7441/LOW?enableSrtp"}, request=req)
        return httpx.Response(200, content=b"jpeg", headers={"content-type": "image/jpeg"}, request=req)

    monkeypatch.setattr(live.httpx, "put", fake_put)
    monkeypatch.setattr(protect.httpx, "get", fake_get)
    monkeypatch.setattr(config, "LIVE_VIDEO_URL", f"http://127.0.0.1:{port}")
    live._registered.clear()
    protect._streams.clear()
    yield seen
    server.shutdown()


def with_key():
    with session_scope() as s:
        site = s.get(Site, 1)
        site.protect_key = "KEY"
        s.add(site)
        s.commit()


def test_relay_streams_video_to_people_who_may_use_the_door(client, helper):
    setup_admin(client)
    with_key()
    with client.websocket_connect("/doors/1:demo-front/live") as ws:
        ws.send_text(json.dumps({"type": "mse", "value": "avc1.640029,mp4a.40.2"}))
        assert json.loads(ws.receive_text())["value"].startswith("video/mp4")
        assert ws.receive_bytes().startswith(b"\x00\x00\x00\x18ftyp")
        assert ws.receive_bytes() == b"moof-1"
    reg = helper["put"][0]
    assert reg["src"] == "rtspx://10.0.0.1:7441/LOW"  # low quality, no SRTP, self-signed cert OK
    assert reg["name"].startswith("door_") and helper["ws"][0] == f"/api/ws?src={reg['name']}"


def test_relay_refuses_others(client, helper):
    setup_admin(client)
    with_key()
    client.post("/users", data={"name": "Gus", "email": "gus@example.com", "password": "password123",
                                "signin": "password", "role": "opener", "door_ids": ["1:demo-side"]})
    login(client, "gus@example.com")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/doors/1:demo-front/live"):
            pass  # not his door
    with client.websocket_connect("/doors/1:demo-side/live") as ws:  # his door is fine
        ws.send_text(json.dumps({"type": "mse", "value": "avc1.640029"}))
        assert json.loads(ws.receive_text())["type"] == "mse"
    with pytest.raises(WebSocketDisconnect):  # another website can't borrow his sign-in
        with client.websocket_connect("/doors/1:demo-side/live", headers={"origin": "https://evil.example"}):
            pass
    client.post("/logout")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/doors/1:demo-side/live"):
            pass


def test_live_off_and_protect_errors(client, helper, monkeypatch):
    setup_admin(client)
    with_key()
    monkeypatch.setattr(protect.httpx, "get", lambda url, **kw: httpx.Response(401, request=httpx.Request("GET", url)))
    with client.websocket_connect("/doors/1:demo-front/live") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error" and "Control Plane" in msg["value"]
    monkeypatch.setattr(config, "LIVE_VIDEO_URL", "")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/doors/1:demo-front/live"):
            pass
    assert "Watch live" not in client.get("/doors/1:demo-front").text


def test_turns_on_a_stream_when_protect_has_none(monkeypatch):
    posted = []
    monkeypatch.setattr(protect.httpx, "get", lambda url, **kw: httpx.Response(200, json={},
                                                                              request=httpx.Request("GET", url)))

    def fake_post(url, json=None, **kw):
        posted.append(json)
        return httpx.Response(200, json={"low": "rtsps://nvr:7441/abc?enableSrtp"}, request=httpx.Request("POST", url))
    monkeypatch.setattr(protect.httpx, "post", fake_post)
    protect._streams.clear()
    site = Site(id=5, name="X", host="https://nvr:12445", protect_key="K", verify_tls=True)
    assert protect.rtsp_url(site, "cam1") == "rtsps://nvr:7441/abc"  # certificate checked: keep rtsps
    assert posted == [{"qualities": ["low"]}]


def test_open_without_leaving_the_page(client, fake, helper):
    setup_admin(client)
    r = client.post("/quick-unlock", data={"door_ids": "1:demo-front", "mode": "open"}, headers={"X-OpnDoor": "fetch"})
    assert r.status_code == 200 and r.json()["ok"] and "Opened Front Entrance" in r.json()["message"]
    assert ("open", "demo-front") in fake.calls
    page = client.get("/doors/1:demo-front").text
    assert "Watch live" in page and "/static/live.js" in page
