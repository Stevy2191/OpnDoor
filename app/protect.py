"""Camera pictures from UniFi Protect, shown with each door.

UniFi Access's API has no camera feeds, so OpnDoor asks UniFi Protect (on the same
or another UniFi console) through its official integration API:
  GET /proxy/protect/integration/v1/cameras                 list cameras
  GET /proxy/protect/integration/v1/cameras/{id}/snapshot   a JPEG of right now
with a UniFi OS API key (Settings → Control Plane → Integrations) in X-API-KEY.
Doors are matched to cameras by name (Access readers with cameras show up in Protect
named after their door) or picked by hand on the door's page.
"""
import logging
import re
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

import httpx
from sqlmodel import Session, select

from . import config
from .models import Door, Site

log = logging.getLogger(__name__)

_cache: dict[tuple, tuple[float, bytes, str]] = {}
_lock = threading.Lock()
CACHE_SECONDS = 1.5  # many people looking at one door don't multiply requests


class ProtectError(Exception):
    pass


def enabled(site: Optional[Site]) -> bool:
    return bool(site and (site.protect_key or _demo(site)))


def _demo(site: Site) -> bool:
    return site.is_demo or config.DRY_RUN


def base_url(site: Site) -> str:
    if site.protect_host:
        h = site.protect_host.rstrip("/")
        return h if h.startswith(("http://", "https://")) else "https://" + h
    parts = urlsplit(site.host)
    return f"https://{parts.hostname}" if parts.hostname else ""


def _get(site: Site, path: str, **kw) -> httpx.Response:
    try:
        r = httpx.get(f"{base_url(site)}/proxy/protect/integration{path}", headers={"X-API-KEY": site.protect_key},
                      verify=site.verify_tls, timeout=8.0, **kw)
    except httpx.HTTPError as e:
        raise ProtectError(f"Can't reach UniFi Protect at {base_url(site)}: {e}") from e
    if r.status_code in (401, 403):
        raise ProtectError("UniFi Protect refused the API key. Make one in UniFi OS: Settings → Control Plane → "
                           "Integrations (a UniFi OS API key, not the Access token).")
    if r.status_code >= 400:
        raise ProtectError(f"UniFi Protect error {r.status_code}")
    return r


def cameras(site: Site) -> list[dict]:
    """[{id, name}] for the site's Protect cameras."""
    if _demo(site) and not site.protect_key:
        from .unifi import get_client
        return [{"id": f"cam-{d.id}", "name": d.name} for d in get_client(site).list_doors()]
    data = _get(site, "/v1/cameras").json()
    if isinstance(data, dict):
        data = data.get("data") or []
    return [{"id": c["id"], "name": c.get("name") or c["id"]} for c in data if c.get("id")]


def snapshot(site: Site, camera_id: str, door_name: str = "") -> tuple[bytes, str]:
    key = (site.id, camera_id)
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1], hit[2]
    if _demo(site) and not site.protect_key:
        body, ctype = _demo_picture(door_name or camera_id), "image/svg+xml"
    else:
        r = _get(site, f"/v1/cameras/{camera_id}/snapshot", params={"highQuality": "false"})
        body, ctype = r.content, r.headers.get("content-type", "image/jpeg")
    with _lock:
        _cache[key] = (time.monotonic(), body, ctype)
    return body, ctype


_streams: dict[tuple, tuple[float, str]] = {}
STREAM_CACHE_SECONDS = 600


def rtsp_url(site: Site, camera_id: str) -> str:
    """The camera's low-quality RTSPS stream, turned on in Protect if it isn't yet."""
    key = (site.id, camera_id)
    hit = _streams.get(key)
    if hit and time.monotonic() - hit[0] < STREAM_CACHE_SECONDS:
        return hit[1]
    if _demo(site) and not site.protect_key:
        return f"demo://{camera_id}"  # pretend console: nothing real to stream
    streams = _get(site, f"/v1/cameras/{camera_id}/rtsps-stream").json() or {}
    url = _pick(streams)
    if not url:
        try:
            r = httpx.post(f"{base_url(site)}/proxy/protect/integration/v1/cameras/{camera_id}/rtsps-stream",
                           headers={"X-API-KEY": site.protect_key}, json={"qualities": ["low"]},
                           verify=site.verify_tls, timeout=8.0)
        except httpx.HTTPError as e:
            raise ProtectError(f"Can't reach UniFi Protect at {base_url(site)}: {e}") from e
        if r.status_code >= 400:
            raise ProtectError(f"UniFi Protect couldn't start a stream for that camera ({r.status_code})")
        url = _pick(r.json() or {})
    if not url:
        raise ProtectError("UniFi Protect has no live stream for that camera")
    # go2rtc can't use Protect's SRTP variant, and "rtspx" means RTSPS without checking the
    # console's self-signed certificate.
    url = url.replace("?enableSrtp", "")
    if url.startswith("rtsps://") and not site.verify_tls:
        url = "rtspx://" + url[len("rtsps://"):]
    _streams[key] = (time.monotonic(), url)
    return url


def _pick(streams: dict) -> str:
    for q in ("low", "medium", "high"):
        if isinstance(streams.get(q), str) and streams[q]:
            return streams[q]
    return ""


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def match(session: Session, site: Site, cams: Optional[list[dict]] = None) -> int:
    """Give doors without a camera the right one. Returns how many were matched.

    Best: the door's own reader. UniFi Access says which readers serve which door, and a
    reader with a camera shows up in Protect under the reader's name (e.g. door
    "EMS2-DORM-KIT-HUB", reader and camera "EMS2 - DORM KITCHEN"). That needs the
    Access token's Device: View permission; without it, doors and cameras are matched
    by name (exactly, or a camera named like "<door> - Entry").
    """
    from .unifi import UnifiError, get_client
    cams = cams if cams is not None else cameras(site)
    by_name: dict[str, list[dict]] = {}
    for c in cams:
        by_name.setdefault(_norm(c["name"]), []).append(c)
    try:
        readers = get_client(site).door_devices()
    except (UnifiError, AttributeError) as e:
        log.info("reader list at %s unavailable (%s); matching cameras by name", site.name, e)
        readers = {}
    n = 0
    for d in session.exec(select(Door).where(Door.site_id == site.id)).all():
        if d.camera_id:
            continue
        found: list[dict] = []
        names = readers.get(d.remote_id, [])
        # entry readers first: they face the person at the door
        for dev in sorted(names, key=lambda x: (0 if "entry" in x.lower() else 1 if "exit" not in x.lower() else 2)):
            found = by_name.get(_norm(dev), [])
            if found:
                break
        key = _norm(d.name)
        if not found:
            found = by_name.get(key, [])
        if not found:  # e.g. door "Front Door", camera "Front Door Reader" or "Front Door - Entry"
            found = [c for c in cams if key and _norm(c["name"]).startswith(key)]
            if len(found) > 1:
                entry = [c for c in found if "entry" in c["name"].lower()]
                found = entry or found
        if found and (len(found) == 1 or names):
            d.camera_id = found[0]["id"]
            session.add(d)
            n += 1
    session.flush()
    return n


def _demo_picture(name: str) -> bytes:
    t = time.strftime("%-I:%M:%S %p")
    hue = sum(map(ord, name)) % 360
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180" width="320" height="180">
<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="hsl({hue},35%,62%)"/>
<stop offset=".62" stop-color="hsl({hue},20%,78%)"/><stop offset=".62" stop-color="#8a8f98"/><stop offset="1" stop-color="#5d626b"/></linearGradient></defs>
<rect width="320" height="180" fill="url(#g)"/><rect x="118" y="46" width="84" height="76" fill="#3b4048" opacity=".85"/>
<rect x="124" y="52" width="72" height="70" fill="#20242b"/><circle cx="188" cy="90" r="3" fill="#c9ccd1"/>
<rect x="8" y="8" width="150" height="18" rx="3" fill="rgba(0,0,0,.45)"/>
<text x="14" y="21" font-family="sans-serif" font-size="11" fill="#fff">Demo camera · {t}</text></svg>""".encode()
