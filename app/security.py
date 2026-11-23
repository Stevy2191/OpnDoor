"""Web security: response headers, a same-site check on form posts, sign-in throttling, and safe outside fetches."""
import ipaddress
import socket
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

import httpx

from . import config

# ---------- headers and same-site posts ----------

HEADERS = [
    # Nobody can show OpnDoor inside a frame on another site and trick someone into clicking "Open".
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def _host(value: str) -> str:
    """'https://door.example.com:8443/x' or 'door.example.com:8443' -> 'door.example.com'."""
    v = (value or "").strip()
    if "://" not in v:
        v = "//" + v
    return (urlsplit(v).hostname or "").lower()


def same_site(headers: dict) -> bool:
    """Did a browser send this from OpnDoor's own pages? Compares host names (not ports, which some
    proxies drop). Requests without Origin or Referer (scripts, old browsers) are let through: the
    session cookie is SameSite=Lax, so other sites' posts don't carry it anyway."""
    origin = headers.get("origin", "")
    if origin == "null":
        return True  # privacy modes and sandboxes; the Lax cookie still guards these
    src = origin or headers.get("referer", "")
    if not src:
        return True
    want = {_host(headers.get("host", "")), _host(headers.get("x-forwarded-host", "").split(",")[0])}
    if config.PUBLIC_URL:
        want.add(_host(config.PUBLIC_URL))
    want.discard("")
    return _host(src) in want


class SecurityMiddleware:
    """Adds the headers above to every page and refuses form posts from other sites."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["method"] in UNSAFE:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            if not same_site(headers):
                body = b"This request came from another website, so OpnDoor refused it."
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8")] + HEADERS})
                await send({"type": "http.response.body", "body": body})
                return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                have = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [h for h in HEADERS if h[0] not in have]
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ---------- sign-in throttling ----------

WINDOW = 15 * 60
PER_ACCOUNT = 5  # wrong passwords for one account in 15 minutes
PER_ADDRESS = 20  # wrong passwords from one address in 15 minutes
_fails: dict = {}
_lock = threading.Lock()


def _recent(key: str, now: float) -> list:
    return [t for t in _fails.get(key, []) if now - t < WINDOW]


def login_blocked(email: str, ip: str) -> int:
    """Minutes to wait before trying again (0 = go ahead)."""
    now = time.time()
    with _lock:
        waits = []
        for key, limit in ((f"a:{email.lower()}", PER_ACCOUNT), (f"i:{ip}", PER_ADDRESS)):
            hits = _recent(key, now)
            if len(hits) >= limit:
                waits.append(WINDOW - (now - hits[-limit]))
        return int(max(waits) // 60) + 1 if waits else 0


def login_failed(email: str, ip: str) -> None:
    now = time.time()
    with _lock:
        for key in (f"a:{email.lower()}", f"i:{ip}"):
            _fails[key] = _recent(key, now) + [now]
        if len(_fails) > 10000:  # don't grow forever
            for k in [k for k, v in _fails.items() if not _recent(k, now)]:
                _fails.pop(k, None)


def login_ok(email: str) -> None:
    with _lock:
        _fails.pop(f"a:{email.lower()}", None)


# ---------- fetching outside addresses (calendar links) ----------

MAX_FETCH = 10 * 1024 * 1024
INTERNAL_NAMES = {"localhost", "go2rtc", "opndoor", "caddy", "updater", "metadata.google.internal"}


class FetchError(Exception):
    pass


def _check_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("Use a link that starts with https:// (or webcal://).")
    host = parts.hostname.lower()
    if host in INTERNAL_NAMES or host.endswith(".localhost"):
        raise FetchError("That address points inside this server, so OpnDoor won't fetch it.")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as e:
        raise FetchError(f"Can't find {host}.") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # Private LAN addresses are fine (a calendar server on the network); this server itself,
        # link-local (cloud metadata) and other special addresses are not.
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            raise FetchError("That address points inside this server, so OpnDoor won't fetch it.")
    return url


def fetch(url: str, timeout: float = 20, max_bytes: int = MAX_FETCH, verify: bool = True) -> bytes:
    """GET an outside link safely: http(s) only, no internal addresses (checked on every redirect), size capped."""
    current: Optional[str] = url
    with httpx.Client(timeout=timeout, follow_redirects=False, verify=verify) as client:
        for _ in range(6):
            _check_url(current)
            with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    current = str(resp.url.join(resp.headers.get("location", "")))
                    continue
                resp.raise_for_status()
                data = bytearray()
                for chunk in resp.iter_bytes():
                    data += chunk
                    if len(data) > max_bytes:
                        raise FetchError("That calendar is too big (over 10 MB).")
                return bytes(data)
    raise FetchError("That link redirects too many times.")
