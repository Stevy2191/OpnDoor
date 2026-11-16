"""UniFi Access developer API client.

Uses the documented developer API on the controller (port 12445):
  GET /api/v1/developer/doors                      list doors
  PUT /api/v1/developer/doors/{id}/lock_rule       temporary lock rule
      {"type": "custom", "interval": <minutes>}   unlock, relock after N minutes
      {"type": "reset"}                           back to the door's normal rule

A "custom" rule counts down on the controller itself, so a door relocks on
time even if OpnDoor is down.
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
            base_url=f"{host}/api/v1/developer",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            verify=verify_tls,
            timeout=timeout,
        )

    def _call(self, method: str, path: str, **kw):
        try:
            resp = self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise UnifiError(f"Cannot reach UniFi Access controller: {e}") from e
        if resp.status_code == 401:
            raise UnifiError("UniFi Access rejected the API token (401)")
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

    def unlock_for(self, door_id: str, seconds: float) -> None:
        minutes = max(1, math.ceil(seconds / 60))
        self._call("PUT", f"/doors/{door_id}/lock_rule", json={"type": "custom", "interval": minutes})

    def reset(self, door_id: str) -> None:
        self._call("PUT", f"/doors/{door_id}/lock_rule", json={"type": "reset"})


class FakeUnifiClient:
    """Dry-run stand-in: records calls, never touches a controller."""

    def __init__(self):
        self.doors = [
            RemoteDoor("demo-front", "Front Entrance", "Main - 1F - Front Entrance", "lock", "close"),
            RemoteDoor("demo-side", "Side Door", "Main - 1F - Side Door", "lock", "close"),
            RemoteDoor("demo-hall", "Fellowship Hall", "Main - 1F - Fellowship Hall", "lock", "close"),
            RemoteDoor("demo-office", "Office Suite", "Main - 2F - Office Suite", "lock", "close"),
        ]
        self.calls: list[tuple] = []

    def list_doors(self) -> list[RemoteDoor]:
        return list(self.doors)

    def unlock_for(self, door_id: str, seconds: float) -> None:
        minutes = max(1, math.ceil(seconds / 60))
        self.calls.append(("unlock", door_id, minutes))
        log.info("[dry run] unlock %s for %s min", door_id, minutes)

    def reset(self, door_id: str) -> None:
        self.calls.append(("reset", door_id))
        log.info("[dry run] reset %s", door_id)


_client = None


def get_client():
    global _client
    if _client is None:
        if config.DRY_RUN:
            _client = FakeUnifiClient()
        else:
            _client = UnifiClient(config.UNIFI_HOST, config.UNIFI_TOKEN, config.UNIFI_VERIFY_TLS)
    return _client


def set_client(client) -> None:
    global _client
    _client = client
