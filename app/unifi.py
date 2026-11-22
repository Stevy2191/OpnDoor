"""UniFi Access developer API client (one per site/console).

Uses the developer API on each console (port 12445):
  GET /api/v1/developer/doors                      list doors
  PUT /api/v1/developer/doors/{id}/lock_rule       temporary lock rule
      {"type": "custom", "interval": <minutes>}   unlock, relock after N minutes
      {"type": "keep_lock"}                       stay locked (overrides UniFi's own schedules)
      {"type": "reset"}                           back to the door's normal rule

  PUT /api/v1/developer/doors/{id}/unlock          open now (like a badge tap; relocks by itself)
  GET /api/v1/developer/users                      people in UniFi Access (for "follow UniFi permissions")
  GET /api/v1/developer/users/{id}/access_policies their policies, including ones from user groups
  GET /api/v1/developer/door_groups/{id}           doors in a door group
  GET /api/v1/developer/access_policies/schedules/{id}  when a policy applies

A "custom" rule counts down on the console itself, so a door relocks on time
even if OpnDoor is down.
"""
import logging
import math
from dataclasses import dataclass

import httpx

from . import config

log = logging.getLogger(__name__)


class UnifiError(Exception):
    pass


@dataclass
class RemoteDoor:
    id: str
    name: str
    full_name: str = ""
    lock_status: str = ""
    position_status: str = ""


class UnifiClient:
    def __init__(self, host: str, token: str, verify_tls: bool = False, timeout: float = 10.0):
        self.http = httpx.Client(
            base_url=f"{host.rstrip('/')}/api/v1/developer",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            verify=verify_tls,
            timeout=timeout,
        )

    def _call(self, method: str, path: str, **kw):
        try:
            resp = self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise UnifiError(f"Cannot reach the UniFi Access console: {e}") from e
        if resp.status_code == 401:
            raise UnifiError("UniFi Access rejected the API token (401). Make a new token in the UniFi Access "
                             "app under Settings → General → Advanced → API Token (not a UniFi OS API key), "
                             "with door permissions, and paste all of it.")
        try:
            body = resp.json()
        except ValueError:
            raise UnifiError(f"Unexpected response {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400 or body.get("code") != "SUCCESS":
            raise UnifiError(f"UniFi Access error {resp.status_code}: {body.get('msg') or body}")
        return body.get("data")

    def list_doors(self) -> list[RemoteDoor]:
        data = self._call("GET", "/doors") or []
        return [
            RemoteDoor(
                id=d["id"],
                name=d.get("name") or d["id"],
                full_name=d.get("full_name", ""),
                lock_status=d.get("door_lock_relay_status", ""),
                position_status=d.get("door_position_status", ""),
            )
            for d in data
        ]

    def _rule(self, door_id: str, body: dict) -> None:
        self._call("PUT", f"/doors/{door_id}/lock_rule", json=body)

    def unlock_for(self, door_id: str, seconds: float) -> None:
        self._rule(door_id, {"type": "custom", "interval": max(1, math.ceil(seconds / 60))})

    def keep_lock(self, door_id: str) -> None:
        self._rule(door_id, {"type": "keep_lock"})

    def reset(self, door_id: str) -> None:
        self._rule(door_id, {"type": "reset"})

    def keep_unlock(self, door_id: str) -> None:
        """Hold unlocked until reset (used for "keep unlocked until I set it back")."""
        self._rule(door_id, {"type": "keep_unlock"})

    def set_emergency(self, mode: str) -> None:
        """Site-wide: "lockdown" holds every door locked, "evacuation" every door unlocked, "normal" ends both."""
        self._call("PUT", "/doors/settings/emergency",
                   json={"lockdown": mode == "lockdown", "evacuation": mode == "evacuation"})

    # ---------- visitors ----------

    def create_visitor(self, first: str, last: str, email: str, phone: str, company: str, remarks: str,
                       start: int, end: int, door_ids: list[str]) -> str:
        """Add a visitor who may use these doors between two unix times. Returns their UniFi id."""
        body = {"first_name": first, "last_name": last or "-", "start_time": start, "end_time": end,
                "visit_reason": "Others", "remarks": remarks[:200],
                "resources": [{"id": d, "type": "door"} for d in door_ids]}
        if email:
            body["email"] = email
        if phone:
            body["mobile_phone"] = phone
        if company:
            body["visitor_company"] = company
        try:
            data = self._call("POST", "/visitors", json=body) or {}
        except UnifiError as e:
            if "403" in str(e) or "permission" in str(e).lower():
                raise UnifiError("The API token can't add visitors. Set Visitor to Edit on the token.") from e
            raise
        return str(data.get("id") or "")

    def delete_visitor(self, visitor_id: str) -> None:
        self._call("DELETE", f"/visitors/{visitor_id}", params={"is_force": "true"})

    def new_pin(self) -> str:
        """A PIN the console hasn't given anyone else."""
        for path in ("/credentials/pin_codes", "/pin_codes"):  # the path differs between Access versions
            try:
                data = self._call("POST", path)
            except UnifiError as e:
                last = e
                continue
            return str(data.get("pin_code") or data.get("token") or "") if isinstance(data, dict) else str(data or "")
        raise last

    def set_visitor_pin(self, visitor_id: str, pin: str) -> None:
        self._call("PUT", f"/visitors/{visitor_id}/pin_codes", json={"pin_code": pin})

    def set_visitor_qr(self, visitor_id: str) -> None:
        self._call("PUT", f"/visitors/{visitor_id}/qr_codes", json={})

    def visitor_qr_png(self, visitor_id: str) -> bytes:
        for path in (f"/credentials/qr_codes/download/{visitor_id}", f"/qr_codes/{visitor_id}"):
            try:
                resp = self.http.get(path)
            except httpx.HTTPError as e:
                raise UnifiError(f"Cannot reach the UniFi Access console: {e}") from e
            if resp.status_code == 200 and resp.content[:4] == b"\x89PNG":
                return resp.content
        raise UnifiError("UniFi Access didn't send the QR code. QR codes for visitors need UniFi Access 3.3.10 or later.")

    def door_log(self, since: int, until: int, page: int = 1, size: int = 100) -> tuple[list[dict], int]:
        """Door openings and denied attempts between two unix times. Returns (entries, total)."""
        try:
            resp = self.http.post("/system/logs", params={"page_num": page, "page_size": size},
                                  json={"topic": "door_openings", "since": since, "until": until})
        except httpx.HTTPError as e:
            raise UnifiError(f"Cannot reach the UniFi Access console: {e}") from e
        if resp.status_code in (401, 403):
            raise UnifiError("The API token can't read the door log. Add System Log: View to the token.")
        try:
            body = resp.json()
        except ValueError:
            raise UnifiError(f"Unexpected response {resp.status_code}")
        if resp.status_code >= 400 or body.get("code") != "SUCCESS":
            raise UnifiError(f"UniFi Access error {resp.status_code}: {body.get('msg') or body}")
        data = body.get("data") or {}
        hits = data.get("hits", []) if isinstance(data, dict) else data
        total = (body.get("pagination") or {}).get("total", len(hits))
        return list(hits), int(total or 0)

    def get_emergency(self) -> str:
        data = self._call("GET", "/doors/settings/emergency") or {}
        return "lockdown" if data.get("lockdown") else "evacuation" if data.get("evacuation") else ""

    def open_door(self, door_id: str, actor_id: str, actor_name: str) -> None:
        """Momentary unlock; UniFi relocks after the door's own unlock time."""
        try:
            self._call("PUT", f"/doors/{door_id}/unlock", json={"actor_id": actor_id, "actor_name": actor_name})
        except UnifiError as e:
            if "400" not in str(e):
                raise
            self._call("PUT", f"/doors/{door_id}/unlock")  # Access before 3.3.21 takes no actor

    def door_devices(self) -> dict[str, list[str]]:
        """{door id: [device names]} for readers and hubs bound to doors (needs Device: View)."""
        data = self._call("GET", "/devices") or []
        out: dict[str, list[str]] = {}
        for group in data:
            for dev in group if isinstance(group, list) else [group]:
                if isinstance(dev, dict) and dev.get("location_id"):
                    out.setdefault(dev["location_id"], []).append(dev.get("alias") or dev.get("name") or "")
        return out

    # --- people and permissions (needs the token's Users and Access Policies "View" permissions) ---

    def list_users(self) -> list[dict]:
        out, page = [], 1
        while True:
            data = self._call("GET", "/users", params={"page_num": page, "page_size": 200,
                                                       "expand[]": "access_policy"}) or []
            out.extend(data)
            if len(data) < 200 or page >= 50:
                return out
            page += 1

    def user_policies(self, user_id: str) -> list[dict]:
        return self._call("GET", f"/users/{user_id}/access_policies") or []

    def list_policies(self) -> list[dict]:
        return self._call("GET", "/access_policies") or []

    def door_group(self, group_id: str) -> dict:
        return self._call("GET", f"/door_groups/{group_id}") or {}

    def policy_schedule(self, schedule_id: str) -> dict:
        return self._call("GET", f"/access_policies/schedules/{schedule_id}") or {}


DEMO_DOORS = [
    ("demo-front", "Front Entrance", "1F"),
    ("demo-side", "Side Door", "1F"),
    ("demo-hall", "Fellowship Hall", "1F"),
    ("demo-office", "Office Suite", "2F"),
]


DEMO_AREAS = ["Main Entrance", "North Entrance", "South Entrance", "Gym", "Kitchen", "Library", "Office",
              "Classroom", "Music Room", "Nursery", "Storage", "Boiler Room", "Chapel", "Youth Room",
              "Conference Room", "Loading Dock", "Stairwell", "Workroom", "Lab", "Lobby"]


class FakeUnifiClient:
    """Demo/dry-run console: records calls, never touches a controller.

    DEMO_DOOR_COUNT (env) makes it report that many doors, to try OpnDoor at scale.
    """

    def __init__(self, site_name: str = "Demo"):
        import os
        self.doors = [RemoteDoor(i, n, f"{site_name} - {f} - {n}", "lock", "close") for i, n, f in DEMO_DOORS]
        extra = int(os.getenv("DEMO_DOOR_COUNT", "0") or 0) - len(self.doors)
        for k in range(max(0, extra)):
            area = DEMO_AREAS[k % len(DEMO_AREAS)]
            num = k // len(DEMO_AREAS) + 1
            name = f"{area} {num}" if k >= len(DEMO_AREAS) or area in {"Classroom", "Lab"} else area
            floor = f"{(k % 3) + 1}F"
            self.doors.append(RemoteDoor(f"demo-{k}", name, f"{site_name} - {floor} - {name}", "lock", "close"))
        self.calls: list[tuple] = []
        # demo people and permissions (tests fill these in)
        self.users: list[dict] = []
        self.policies: dict[str, dict] = {}  # id -> {id, name, resources, schedule_id}
        self.user_policy_ids: dict[str, list[str]] = {}
        self.groups: dict[str, dict] = {}
        self.schedules: dict[str, dict] = {}

    def list_doors(self) -> list[RemoteDoor]:
        return list(self.doors)

    def unlock_for(self, door_id: str, seconds: float) -> None:
        minutes = max(1, math.ceil(seconds / 60))
        self.calls.append(("unlock", door_id, minutes))
        log.info("[demo] unlock %s for %s min", door_id, minutes)

    def keep_lock(self, door_id: str) -> None:
        self.calls.append(("lock", door_id))
        log.info("[demo] keep %s locked", door_id)

    def reset(self, door_id: str) -> None:
        self.calls.append(("reset", door_id))
        log.info("[demo] reset %s", door_id)

    def keep_unlock(self, door_id: str) -> None:
        self.calls.append(("hold", door_id))

    emergency = ""

    def set_emergency(self, mode: str) -> None:
        self.calls.append(("emergency", mode))
        self.emergency = "" if mode == "normal" else mode

    def get_emergency(self) -> str:
        return self.emergency

    visitors: dict = None

    def create_visitor(self, first, last, email, phone, company, remarks, start, end, door_ids) -> str:
        if self.visitors is None:
            self.visitors = {}
        vid = f"demo-visitor-{len(self.visitors) + 1}"
        self.visitors[vid] = {"first_name": first, "last_name": last, "start": start, "end": end, "doors": door_ids}
        self.calls.append(("visitor", vid))
        return vid

    def delete_visitor(self, visitor_id: str) -> None:
        (self.visitors or {}).pop(visitor_id, None)
        self.calls.append(("visitor_delete", visitor_id))

    def new_pin(self) -> str:
        import random
        return f"{random.randint(0, 999999):06d}"

    def set_visitor_pin(self, visitor_id: str, pin: str) -> None:
        self.visitors[visitor_id]["pin"] = pin

    def set_visitor_qr(self, visitor_id: str) -> None:
        self.visitors[visitor_id]["qr"] = True

    def visitor_qr_png(self, visitor_id: str) -> bytes:
        return demo_qr_png(visitor_id)

    log_hits: list = None  # tests set entries here; the demo makes up a month of traffic

    def door_log(self, since: int, until: int, page: int = 1, size: int = 100) -> tuple[list[dict], int]:
        if self.log_hits is None:
            self.log_hits = demo_log(self.doors)
        rows = [h for h in self.log_hits if since <= h["_source"]["event"]["published"] // 1000 < until]
        return rows[(page - 1) * size: page * size], len(rows)

    def open_door(self, door_id: str, actor_id: str, actor_name: str) -> None:
        self.calls.append(("open", door_id))
        log.info("[demo] open %s for %s", door_id, actor_name)

    def list_users(self) -> list[dict]:
        return list(self.users)

    def door_devices(self) -> dict[str, list[str]]:
        return dict(getattr(self, "devices", {}))

    def user_policies(self, user_id: str) -> list[dict]:
        return [self.policies[p] for p in self.user_policy_ids.get(user_id, []) if p in self.policies]

    def list_policies(self) -> list[dict]:
        return list(self.policies.values())

    def door_group(self, group_id: str) -> dict:
        return self.groups.get(group_id, {})

    def policy_schedule(self, schedule_id: str) -> dict:
        return self.schedules.get(schedule_id, {})


def demo_qr_png(text: str) -> bytes:
    """A stand-in QR picture for demo consoles (a pattern, not a scannable code)."""
    import hashlib
    import io

    from PIL import Image
    bits = hashlib.sha256(text.encode()).digest() * 2
    n, cell = 21, 10
    img = Image.new("L", ((n + 4) * cell, (n + 4) * cell), 255)
    px = img.load()
    finders = [(0, 0), (n - 7, 0), (0, n - 7)]
    for y in range(n):
        for x in range(n):
            f = next(((x - fx, y - fy) for fx, fy in finders if 0 <= x - fx < 7 and 0 <= y - fy < 7), None)
            if f:
                on = f[0] in (0, 6) or f[1] in (0, 6) or (2 <= f[0] <= 4 and 2 <= f[1] <= 4)
            else:
                on = bits[(y * n + x) // 8 % 64] >> (x % 8) & 1
            if on:
                for dy in range(cell):
                    for dx in range(cell):
                        px[(x + 2) * cell + dx, (y + 2) * cell + dy] = 0
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def demo_log(doors: list) -> list[dict]:
    """A believable month of door traffic for demo consoles: shift changes, office hours, quiet nights
    (made in the app's own timezone, so the busy hours look right)."""
    import random
    from datetime import datetime as _dt, timedelta as _td
    rnd = random.Random(7)
    now = _dt.now(config.TZ)
    people = ["J. Rivera", "M. Chen", "A. Patel", "S. Brooks", "K. Nguyen", "T. Wallace", "R. Okafor", "L. Moreno"]
    methods = ["NFC", "NFC", "NFC", "MOBILE_TAP", "PIN_CODE", "REMOTE_THROUGH_UAH"]
    out = []
    n = 0
    for day in range(30, -1, -1):
        midnight = (now - _td(days=day)).replace(hour=0, minute=0, second=0, microsecond=0)
        weekend = midnight.weekday() >= 5
        for hour in range(24):
            busy = 9 if hour in (7, 19) else 4 if 8 <= hour <= 18 else 1 if 5 <= hour <= 22 else 0
            if weekend:
                busy = (busy + 1) // 2
            for _ in range(rnd.randint(busy, busy * 2)):
                t = midnight + _td(hours=hour, seconds=rnd.randint(0, 3599))
                if t > now:
                    continue
                d = doors[rnd.randrange(len(doors))] if doors else None
                denied = rnd.random() < 0.03
                n += 1
                out.append({"_id": f"demo-{n}", "@timestamp": "", "_source": {
                    "actor": {"display_name": "" if denied else rnd.choice(people), "type": "user"},
                    "authentication": {"credential_provider": rnd.choice(methods)},
                    "event": {"display_message": "Access Denied" if denied else "Access Granted",
                              "published": int(t.timestamp() * 1000), "result": "BLOCKED" if denied else "ACCESS",
                              "type": "access.door.unlock", "log_key": "access.door.unlock." + ("failed" if denied else "success")},
                    "target": [{"type": "door", "id": d.id if d else "", "display_name": d.name if d else ""}]}})
    return out


_clients: dict = {}
_factory = None  # tests can replace how clients are built


def get_client(site):
    """Client for a Site row; rebuilt when its connection settings change."""
    key = (site.id, site.host, site.token, site.verify_tls)
    client = _clients.get(site.id)
    if client is None or client[0] != key:
        if _factory:
            built = _factory(site)
        elif config.DRY_RUN or site.is_demo:
            built = FakeUnifiClient(site.name)
        else:
            built = UnifiClient(site.host, site.token, site.verify_tls)
        client = (key, built)
        _clients[site.id] = client
    return client[1]


def set_factory(factory) -> None:
    """Tests: build every site's client with factory(site)."""
    global _factory
    _factory = factory
    _clients.clear()
