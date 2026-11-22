"""Devices: is every console, reader, hub and camera online?"""
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, Request
from sqlmodel import Session, select

from . import protect, unifi
from .auth import managed_site_ids
from .db import get_session
from .models import Door, Site, User
from fastapi.responses import JSONResponse

from .web import render, require_admin, require_manager

router = APIRouter()

TYPES = {"UA-G2-MINI": "G2 Mini reader", "UA-G3-PRO": "G3 Pro reader", "UA-INT-VIEWER": "Intercom viewer",
         "UA-G2-PRO": "G2 Pro reader", "UAH-DOOR": "Door hub", "UAH": "Hub", "UAH-DOOR": "Door hub", "UAH-ENT": "Enterprise hub", "UA-HUB-GATE": "Gate hub",
         "UGT": "Gate hub", "UDA": "Door hub", "UA-G2-PRO": "G2 Pro reader", "UA-G2": "G2 reader",
         "UA-G3": "G3 reader", "UA-LITE": "Reader Lite", "UA-PRO": "Reader Pro", "UA-INTERCOM": "Intercom",
         "UA-ULTRA": "Ultra reader", "UVC": "Camera"}
_cache: dict = {}
CACHE_SECONDS = 45


def _type(t: str) -> str:
    u = (t or "").upper()
    if u in TYPES:
        return TYPES[u]
    for k, v in TYPES.items():
        if u.startswith(k):
            return v
    return t or "Device"


def site_devices(site: Site) -> dict:
    """{console_ok, console_msg, access: [...], cameras: [...], errors: [...]} for one site."""
    hit = _cache.get(site.id)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    out = {"access": [], "cameras": [], "errors": []}
    try:
        out["access"] = unifi.get_client(site).list_devices()
        out["console_ok"] = True
    except unifi.UnifiError as e:
        out["console_ok"] = False
        out["errors"].append(f"UniFi Access: {e}")
    if protect.enabled(site):
        try:
            out["cameras"] = protect.cameras(site)
        except Exception as e:  # noqa: BLE001 - Protect unreachable shouldn't hide the Access devices
            out["errors"].append(f"UniFi Protect: {e}")
    _cache[site.id] = (time.monotonic(), out)
    return out


@router.get("/devices/raw")
def devices_raw(site: int, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    """Exactly what the console says about its devices, to compare with the UniFi app (admins only)."""
    st = session.get(Site, site)
    if not st:
        return JSONResponse({"error": "no such site"}, status_code=404)
    try:
        data = unifi.get_client(st).raw_devices()
    except unifi.UnifiError as e:
        data = {"error": str(e)}
    return JSONResponse(data, headers={"Content-Disposition": f'inline; filename="devices-{st.id}.json"'})


@router.get("/devices")
def devices_page(request: Request, refresh: int = 0, user: User = Depends(require_manager),
                 session: Session = Depends(get_session)):
    ids = managed_site_ids(session, user)
    sites = [x for x in session.exec(select(Site).where(Site.enabled == True).order_by(Site.name)).all()  # noqa: E712
             if x.id in ids]
    if refresh:
        for x in sites:
            _cache.pop(x.id, None)
    with ThreadPoolExecutor(max_workers=6) as pool:
        found = dict(zip([x.id for x in sites], pool.map(site_devices, sites)))
    rows, offline, total = [], 0, 0
    for site in sites:
        doors = {d.remote_id: d for d in session.exec(select(Door).where(Door.site_id == site.id)).all()}
        info = found[site.id]
        items = []
        for d in info["access"]:
            door = doors.get(d["door_id"])
            kind = _type(d["type"])
            name, sub_name = d["name"], ""
            if not d.get("alias", d["name"]) and door:  # unnamed reader: call it by its door, like the UniFi app
                name, sub_name = f"{door.name} · {kind}", d.get("model_name") or ""
            adopted = d.get("adopted", True)
            items.append({"name": name, "sub": sub_name, "kind": kind, "where": door.name if door else "",
                          "door_id": door.id if door else None, "online": d["online"] if adopted else None,
                          "note": "" if adopted else "UniFi says this isn't adopted", "firmware": d["firmware"]})
        for c in info["cameras"]:
            st = (c.get("state") or "").upper()
            items.append({"name": c["name"], "sub": "", "note": "", "kind": "Camera", "where": "", "door_id": None,
                          "online": True if st == "CONNECTED" else (False if st else None), "firmware": ""})
        items.sort(key=lambda i: (i["online"] is not False, i["kind"] != "Hub", i["name"].lower()))
        bad = sum(1 for i in items if i["online"] is False)
        offline += bad + (0 if info.get("console_ok") else 1)
        total += len(items)
        rows.append({"site": site, "devs": items, "bad": bad, "console_ok": info.get("console_ok"),
                     "errors": info["errors"]})
    return render(request, "devices.html", user, rows=rows, offline=offline, total=total)
