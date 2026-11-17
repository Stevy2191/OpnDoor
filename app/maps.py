"""Site map: floor plans per building with door pins.

Viewing: anyone, for the doors they have access to.
Uploading plans and placing pins: admins and that site's site admins.
"""
import uuid
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlmodel import Session, select

from . import audit, config, engine
from .auth import allowed_door_ids, managed_site_ids, visible_site_ids
from .db import get_session
from .models import Building, Door, DoorPin, FloorPlan, Site, User
from .schedules import expand, now_local
from .web import (current_site_id, door_status, flash, redirect, render, require_manager, require_user,
                  site_summaries)

router = APIRouter()
MAX_BYTES = 15 * 1024 * 1024
KINDS = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg"}


def plan_dir():
    d = config.DATA_DIR / "floorplans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def image_kind(head: bytes) -> Optional[str]:
    for magic, ext in KINDS.items():
        if head.startswith(magic):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def plan_site_id(session: Session, plan: FloorPlan) -> Optional[int]:
    b = session.get(Building, plan.building_id)
    return b.site_id if b else None


def delete_plan(session: Session, plan: FloorPlan) -> None:
    """Remove a plan, its pins and its image file."""
    for pin in session.exec(select(DoorPin).where(DoorPin.plan_id == plan.id)).all():
        session.delete(pin)
    session.flush()
    try:
        (plan_dir() / plan.filename).unlink(missing_ok=True)
    except OSError:
        pass
    session.delete(plan)
    session.flush()


def delete_building_plans(session: Session, building_id: int) -> None:
    for plan in session.exec(select(FloorPlan).where(FloorPlan.building_id == building_id)).all():
        delete_plan(session, plan)


def next_events(session: Session, doors: list[Door]) -> dict:
    out = {}
    now = now_local()
    for door_id, scheds in engine.door_schedules(session, [d.id for d in doors]).items():
        occ = [o for o in expand([x for x in scheds if x.kind == "override"], now, now + timedelta(days=30))
               if o.start > now]  # what's coming, not what's already running
        if occ:
            out[door_id] = occ[0]
    return out


# ---------- viewing ----------

@router.get("/map")
def map_page(request: Request, plan: Optional[int] = None, user: User = Depends(require_user),
             session: Session = Depends(get_session)):
    visible = visible_site_ids(session, user)
    site_id = current_site_id(request, session, user)
    if site_id is None and len(visible) > 1:
        return render(request, "doors_sites.html", user, summaries=site_summaries(session, user, visible),
                      next_path="/map", page_title="Map", page_hint="Pick a site to see its floor plans.")
    site_id = site_id or (min(visible) if visible else None)
    site = session.get(Site, site_id) if site_id else None
    allowed = allowed_door_ids(session, user)
    buildings = list(session.exec(select(Building).where(Building.site_id == site_id).order_by(Building.name)).all()) if site else []
    plans = list(session.exec(select(FloorPlan).where(FloorPlan.building_id.in_([b.id for b in buildings]))
                              .order_by(FloorPlan.name)).all()) if buildings else []
    # people only see buildings where they have at least one door (managers see every plan)
    manager = site_id in managed_site_ids(session, user) if site_id else False
    if not manager:
        mine = {d.building_id for d in session.exec(select(Door).where(Door.id.in_(allowed))).all()} if allowed else set()
        plans = [p for p in plans if p.building_id in mine]
    bnames = {b.id: b.name for b in buildings}
    plans.sort(key=lambda p: (bnames.get(p.building_id, "").lower(), p.name.lower()))
    current = next((p for p in plans if p.id == plan), plans[0] if plans else None)
    pins, doors, status, nextev, unplaced = [], [], {}, {}, []
    if current:
        rows = session.exec(select(DoorPin, Door).join(Door, Door.id == DoorPin.door_id)
                            .where(DoorPin.plan_id == current.id)).all()
        pins = [(pin, d) for pin, d in rows if d.id in allowed and d.present]
        doors = [d for _, d in pins]
        status = door_status(session, doors)
        nextev = next_events(session, doors)
        placed = {d.id for d in session.exec(select(Door).join(DoorPin, DoorPin.door_id == Door.id)).all()}
        unplaced = [d for d in session.exec(select(Door).where(Door.building_id == current.building_id,
                                                               Door.present == True)).all()  # noqa: E712
                    if d.id in allowed and d.id not in placed]
    return render(request, "map.html", user, site=site, plans=plans, current=current, bnames=bnames,
                  pins=pins, status=status, nextev=nextev, unplaced=unplaced, manager=manager, now=now_local())


@router.get("/floorplans/{pid}/image")
def plan_image(pid: int, user: User = Depends(require_user), session: Session = Depends(get_session)):
    plan = session.get(FloorPlan, pid)
    if not plan or plan_site_id(session, plan) not in visible_site_ids(session, user):
        return JSONResponse({"error": "not found"}, status_code=404)
    path = plan_dir() / plan.filename
    if not path.exists():
        return JSONResponse({"error": "image missing"}, status_code=404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


# ---------- managing (admins and site admins) ----------

def _managed_plan(session: Session, user: User, pid: int) -> Optional[FloorPlan]:
    plan = session.get(FloorPlan, pid)
    if plan and plan_site_id(session, plan) in managed_site_ids(session, user):
        return plan
    return None


@router.post("/floorplans")
async def plan_upload(request: Request, building_id: int = Form(...), name: str = Form("Main floor"),
                      image: UploadFile = File(...), user: User = Depends(require_manager),
                      session: Session = Depends(get_session)):
    b = session.get(Building, building_id)
    if not b or b.site_id not in managed_site_ids(session, user):
        flash(request, "That building isn't available to you.", "error")
        return redirect("/sites")
    back = f"/sites/{b.site_id}#floorplans"
    data = await image.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        flash(request, "That image is too big. Please use one under 15 MB.", "error")
        return redirect(back)
    ext = image_kind(data[:16])
    if not ext:
        flash(request, "Please upload a PNG, JPG or WebP picture of the floor plan.", "error")
        return redirect(back)
    filename = f"{uuid.uuid4().hex}.{ext}"
    (plan_dir() / filename).write_bytes(data)
    plan = FloorPlan(building_id=b.id, name=name.strip() or "Main floor", filename=filename)
    session.add(plan)
    session.flush()
    audit.record(session, "floorplan.update", f"{user.display} added floor plan “{plan.name}” for {b.name}",
                 user=user, site_ids=[b.site_id])
    session.commit()
    flash(request, f"Added “{plan.name}”. Now place the doors on it.")
    return redirect(f"/floorplans/{plan.id}/edit")


@router.get("/floorplans/{pid}/edit")
def plan_edit(request: Request, pid: int, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    plan = _managed_plan(session, user, pid)
    if not plan:
        flash(request, "That floor plan isn't available to you.", "error")
        return redirect("/sites")
    b = session.get(Building, plan.building_id)
    site = session.get(Site, b.site_id)
    doors = list(session.exec(select(Door).where(Door.site_id == site.id, Door.present == True)).all())  # noqa: E712
    pins = {p.door_id: p for p in session.exec(select(DoorPin).where(DoorPin.door_id.in_([d.id for d in doors]))).all()}
    plan_names = {p.id: p.name for p in session.exec(select(FloorPlan)).all()}
    return render(request, "floorplan_edit.html", user, plan=plan, building=b, site=site, doors=doors, pins=pins,
                  plan_names=plan_names)


@router.post("/floorplans/{pid}/pins")
async def plan_pin(request: Request, pid: int, user: User = Depends(require_manager),
                   session: Session = Depends(get_session)):
    """JSON {door_id, x, y} places or moves a pin; {door_id, remove: true} takes it off the map."""
    plan = _managed_plan(session, user, pid)
    if not plan:
        return JSONResponse({"error": "not allowed"}, status_code=403)
    body = await request.json()
    door = session.get(Door, str(body.get("door_id", "")))
    if not door or door.site_id != plan_site_id(session, plan):
        return JSONResponse({"error": "unknown door"}, status_code=400)
    pin = session.get(DoorPin, door.id)
    if body.get("remove"):
        if pin:
            session.delete(pin)
            audit.record(session, "floorplan.update", f"{user.display} took {door.name} off the map “{plan.name}”",
                         user=user, door_ids=[door.id])
            session.commit()
        return {"ok": True}
    try:
        x = min(100.0, max(0.0, float(body["x"])))
        y = min(100.0, max(0.0, float(body["y"])))
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "bad position"}, status_code=400)
    new = pin is None or pin.plan_id != plan.id
    if pin is None:
        pin = DoorPin(door_id=door.id, plan_id=plan.id, x=x, y=y)
    pin.plan_id, pin.x, pin.y = plan.id, x, y
    session.add(pin)
    if new:  # moving a pin around isn't worth a History entry; placing it is
        audit.record(session, "floorplan.update", f"{user.display} placed {door.name} on the map “{plan.name}”",
                     user=user, door_ids=[door.id])
    session.commit()
    return {"ok": True}


@router.post("/floorplans/{pid}")
def plan_rename(request: Request, pid: int, name: str = Form(...), user: User = Depends(require_manager),
                session: Session = Depends(get_session)):
    plan = _managed_plan(session, user, pid)
    if plan and name.strip():
        plan.name = name.strip()
        session.add(plan)
        session.commit()
        flash(request, "Renamed.")
    return redirect(f"/floorplans/{pid}/edit")


@router.post("/floorplans/{pid}/delete")
def plan_delete(request: Request, pid: int, user: User = Depends(require_manager),
                session: Session = Depends(get_session)):
    plan = _managed_plan(session, user, pid)
    if not plan:
        return redirect("/sites")
    site_id = plan_site_id(session, plan)
    name = plan.name
    delete_plan(session, plan)
    audit.record(session, "floorplan.update", f"{user.display} removed floor plan “{name}”", user=user,
                 site_ids=[site_id])
    session.commit()
    flash(request, f"Removed “{name}”. The doors themselves are unchanged.")
    return redirect(f"/sites/{site_id}#floorplans")
