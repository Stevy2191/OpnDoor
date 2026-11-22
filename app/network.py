"""UniFi Network as a second opinion on whether Access devices are online.

UniFi Access's developer API sometimes reports a device's online state wrong (on one console it showed an
adopted Intercom Viewer with 122 days of uptime as offline and not adopted). UniFi Network knows which
devices are actually connected, by MAC address. Access device ids are their MAC addresses, so the two
line up. Uses the UniFi Network integration API with a UniFi OS API key (read-only)."""
import re
import time
from typing import Optional
from urllib.parse import urlsplit

import httpx

from .models import Site

_cache: dict = {}
CACHE_SECONDS = 45


class NetworkError(Exception):
    pass


def enabled(site: Optional[Site]) -> bool:
    return bool(site and site.network_on and (site.network_key or site.protect_key or site.is_demo))


def mac(value: str) -> str:
    """'58:d6:1f:29:f9:b5' or '58d61f29f9b5' -> '58d61f29f9b5' ('' if it isn't a MAC)."""
    v = re.sub(r"[^0-9a-f]", "", (value or "").lower())
    return v if len(v) == 12 else ""


def base_url(site: Site) -> str:
    h = (site.network_host or "").rstrip("/")
    if h:
        return h if h.startswith(("http://", "https://")) else "https://" + h
    parts = urlsplit(site.host)
    return f"https://{parts.hostname}" if parts.hostname else ""


def _get(site: Site, path: str, **params) -> dict:
    key = site.network_key or site.protect_key
    try:
        r = httpx.get(f"{base_url(site)}/proxy/network/integration/v1{path}", params=params or None,
                      headers={"X-API-KEY": key, "Accept": "application/json"}, verify=site.verify_tls, timeout=8.0)
    except httpx.HTTPError as e:
        raise NetworkError(f"Can't reach UniFi Network at {base_url(site)}: {e}") from e
    if r.status_code in (401, 403):
        raise NetworkError("UniFi Network refused the API key. Make one in UniFi OS on the console that runs "
                           "UniFi Network: Settings → Control Plane → Integrations.")
    if r.status_code == 404:
        raise NetworkError(f"UniFi Network isn't running at {base_url(site)}. Enter the address of the console "
                           "that runs it (for example your gateway).")
    if r.status_code >= 400:
        raise NetworkError(f"UniFi Network error {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise NetworkError("UniFi Network sent something unexpected.") from e


def clients(site: Site) -> dict[str, dict]:
    """{mac: {"ip", "name"}} for everything connected right now, across the console's Network sites."""
    hit = _cache.get(site.id)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    if site.is_demo and not site.network_key:
        from .unifi import get_client
        out = {mac(d["id"]) or d["id"]: {"ip": f"10.0.0.{i + 10}", "name": d["name"]}
               for i, d in enumerate(get_client(site).list_devices()) if d["online"]}
    else:
        out = {}
        sites = _get(site, "/sites", limit=200).get("data") or []
        for s in sites:
            offset = 0
            while True:
                page = _get(site, f"/sites/{s['id']}/clients", offset=offset, limit=200)
                rows = page.get("data") or []
                for c in rows:
                    m = mac(c.get("macAddress", ""))
                    if m:
                        out[m] = {"ip": c.get("ipAddress") or "", "name": c.get("name") or ""}
                offset += len(rows)
                if not rows or offset >= int(page.get("totalCount") or 0) or offset > 20000:
                    break
    _cache[site.id] = (time.monotonic(), out)
    return out


def forget(site_id: int) -> None:
    _cache.pop(site_id, None)
