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
