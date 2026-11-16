"""UniFi Access developer API client (one per site/console).

Uses the developer API on each console (port 12445):
  GET /api/v1/developer/doors                      list doors
  PUT /api/v1/developer/doors/{id}/lock_rule       temporary lock rule
      {"type": "custom", "interval": <minutes>}   unlock, relock after N minutes
      {"type": "keep_lock"}                       stay locked (overrides UniFi's own schedules)
      {"type": "reset"}                           back to the door's normal rule

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
