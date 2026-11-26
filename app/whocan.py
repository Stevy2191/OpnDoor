"""Who can get into a door: people whose UniFi Access policies include it, and OpnDoor users who can open it."""
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlmodel import Session, select

from . import unifi
from .access import DAYS, _door_ids, _hours
from .auth import allowed_door_ids, managed_site_ids
from .db import get_session
from .models import Door, Site, User
from .web import render, require_manager

router = APIRouter()
_cache: dict = {}
CACHE_SECONDS = 600
SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def hours_text(h: Optional[dict]) -> str:
    """'Any time', 'Mon–Fri 07:00–18:00', 'Never'..."""
    if h is None:
        return "Any time"
    groups: list = []  # [(windows, [day indexes])]
    for i, d in enumerate(DAYS):
        w = tuple(tuple(x) for x in h.get(d) or [])
        if groups and groups[-1][0] == w and groups[-1][1][-1] == i - 1:
            groups[-1][1].append(i)
        else:
            groups.append((w, [i]))
    parts = []
    for w, days in groups:
        if not w:
            continue
        span = SHORT[days[0]] if len(days) == 1 else f"{SHORT[days[0]]}–{SHORT[days[-1]]}"
        parts.append(span + " " + ", ".join(f"{a}–{b}" for a, b in w))
    return "; ".join(parts) or "No hours set"


def _name(u: dict) -> str:
    n = (u.get("full_name") or " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)).strip()
    return n or u.get("user_email") or u.get("email") or "Someone"


def site_access(site: Site, refresh: bool = False) -> tuple[dict, str]:
    """({remote door id: [{name, email, policies, hours}]}, error) for a console, cached for a few minutes."""
    hit = _cache.get(site.id)
    if hit and not refresh and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1], hit[2]
    client = unifi.get_client(site)
    out: dict = {}
    try:
        users = [u for u in client.list_users() if str(u.get("status", "ACTIVE")).upper() in {"ACTIVE", ""}]
        all_pol = None

        def pols(u):
            try:
                return client.user_policies(u["id"])
            except unifi.UnifiError:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            found = list(pool.map(pols, users))
        pol_doors: dict = {}
        sched_hours: dict = {}
        for u, policies in zip(users, found):
            if policies is None:  # older Access: only policies given to the person directly
                if all_pol is None:
                    all_pol = {x["id"]: x for x in client.list_policies()}
                ids = u.get("access_policy_ids") or [x.get("id") for x in u.get("access_policies") or []]
                policies = [all_pol[i] for i in ids if i in all_pol]
            per_door: dict = {}
            for p in policies:
                pid = p.get("id") or p.get("name", "")
                if pid not in pol_doors:
                    pol_doors[pid] = _door_ids(client, p.get("resources") or [], set())
                sid = p.get("schedule_id") or ""
                if sid not in sched_hours:
                    try:
                        sched_hours[sid] = hours_text(_hours(client.policy_schedule(sid))) if sid else "Any time"
                    except unifi.UnifiError:
                        sched_hours[sid] = "Hours not readable"
                for rid in pol_doors[pid]:
                    per_door.setdefault(rid, []).append((p.get("name", "Policy"), sched_hours[sid]))
            for rid, ps in per_door.items():
                out.setdefault(rid, []).append({"name": _name(u), "email": u.get("user_email") or u.get("email") or "",
                                                "policies": ", ".join(sorted({n for n, _ in ps})),
                                                "hours": "Any time" if "Any time" in {h for _, h in ps}
                                                else "; ".join(sorted({h for _, h in ps}))})
        for v in out.values():
            v.sort(key=lambda r: r["name"].lower())
        _cache[site.id] = (time.monotonic(), out, "")
        return out, ""
    except unifi.UnifiError as e:
        return {}, str(e)


@router.get("/doors/{door_id}/who")
def door_who(request: Request, door_id: str, refresh: int = 0, user: User = Depends(require_manager),
             session: Session = Depends(get_session)):
    door = session.get(Door, door_id)
    if not door or door.site_id not in managed_site_ids(session, user):
        return render(request, "_whocan.html", user, door=None, people=[], app_people=[], error="Not available.")
    site = session.get(Site, door.site_id)
    found, error = site_access(site, bool(refresh))
    app_people = [u for u in session.exec(select(User).where(User.active == True).order_by(User.name)).all()  # noqa: E712
                  if door.id in allowed_door_ids(session, u)]
    return render(request, "_whocan.html", user, door=door, people=found.get(door.remote_id, []),
                  app_people=app_people, error=error)
