"""Live camera video for doors.

Browsers can't play a camera's RTSPS stream, so a small helper container, go2rtc,
turns it into fragmented MP4 that a <video> element plays (Media Source Extensions).
go2rtc isn't reachable from outside: the browser talks only to OpnDoor, which checks
that the person may use the door, tells go2rtc which stream to pull, and relays the
WebSocket between the two.
"""
import asyncio
import hashlib
import logging
import time
from urllib.parse import quote, urlsplit

import httpx
import websockets
from fastapi import APIRouter, WebSocket
from fastapi.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from . import config, protect
from .auth import allowed_door_ids, current_user
from .db import session_scope
from .models import Door, Site

log = logging.getLogger(__name__)
router = APIRouter()


def enabled() -> bool:
    return bool(config.LIVE_VIDEO_URL)


def stream_name(door: Door) -> str:
    return "door_" + hashlib.sha1(f"{door.site_id}:{door.camera_id}".encode()).hexdigest()[:16]


_registered: dict[str, tuple[float, str]] = {}


def ensure_stream(site: Site, door: Door) -> str:
    """Tell go2rtc where the door camera's stream is (idempotent). Returns the stream name."""
    name = stream_name(door)
    src = protect.rtsp_url(site, door.camera_id)
    hit = _registered.get(name)
    if hit and hit[1] == src and time.monotonic() - hit[0] < 300:
        return name
    try:
        r = httpx.put(f"{config.LIVE_VIDEO_URL}/api/streams", params={"name": name, "src": src}, timeout=5.0)
    except httpx.HTTPError as e:
        log.warning("live video helper at %s: %s", config.LIVE_VIDEO_URL, e)
        raise protect.ProtectError("Live video isn't available right now.") from e
    if r.status_code >= 400:
        raise protect.ProtectError(f"The live video helper refused the stream ({r.status_code})")
    _registered[name] = (time.monotonic(), src)
    return name


def _same_origin(ws: WebSocket) -> bool:
    """Block other websites from opening this socket with the person's cookie."""
    origin = ws.headers.get("origin")
    if not origin:
        return True  # not a browser
    host = ws.headers.get("host", "")
    return urlsplit(origin).netloc == host or (config.PUBLIC_URL and origin == config.PUBLIC_URL)


@router.websocket("/doors/{door_id}/live")
async def live(ws: WebSocket, door_id: str):
    if not enabled() or not _same_origin(ws):
        await ws.close(code=1008)
        return
    with session_scope() as s:
        user = current_user(ws, s)
        door = s.get(Door, door_id)
        if not user or not door or not door.camera_id or door_id not in allowed_door_ids(s, user):
            await ws.close(code=1008)
            return
        site = s.get(Site, door.site_id)
        s.expunge_all()
    try:
        name = await run_in_threadpool(ensure_stream, site, door)
    except protect.ProtectError as e:
        await ws.accept()
        await ws.send_json({"type": "error", "value": str(e)})
        await ws.close()
        return
    up_url = config.LIVE_VIDEO_URL.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    try:
        upstream = await websockets.connect(f"{up_url}/api/ws?src={quote(name)}", max_size=None, open_timeout=5)
    except Exception as e:  # noqa: BLE001
        log.warning("live video helper at %s: %s", config.LIVE_VIDEO_URL, e)
        await ws.accept()
        await ws.send_json({"type": "error", "value": "Live video isn't available right now."})
        await ws.close()
        return
    await ws.accept()

    async def browser_to_helper():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("text") is not None:
                await upstream.send(msg["text"])
            elif msg.get("bytes") is not None:
                await upstream.send(msg["bytes"])

    async def helper_to_browser():
        async for data in upstream:
            if isinstance(data, bytes):
                await ws.send_bytes(data)
            else:
                await ws.send_text(data)

    tasks = [asyncio.create_task(browser_to_helper()), asyncio.create_task(helper_to_browser())]
    try:
        done, _ = await asyncio.wait(tasks, timeout=config.LIVE_MAX_MINUTES * 60,
                                     return_when=asyncio.FIRST_COMPLETED)
        if not done:
            await ws.send_json({"type": "timeout", "value": "Live view paused"})
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    finally:
        for t in tasks:
            t.cancel()
        await upstream.close()
        try:
            await ws.close()
        except Exception:  # noqa: BLE001 - already closed
            pass
