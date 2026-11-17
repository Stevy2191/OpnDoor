"""Door camera pictures (from UniFi Protect) and the Doors page layout switch."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlmodel import Session

from . import audit, protect
from .auth import allowed_door_ids, managed_site_ids
from .db import get_session
from .models import Door, Site, User
from .web import flash, redirect, require_user, safe_next

router = APIRouter()


@router.get("/doors/{door_id}/snapshot")
def door_snapshot(door_id: str, user: User = Depends(require_user), session: Session = Depends(get_session)):
    door = session.get(Door, door_id)
    if not door or not door.camera_id or door_id not in allowed_door_ids(session, user):
        return Response(status_code=404)
    site = session.get(Site, door.site_id)
    try:
        body, ctype = protect.snapshot(site, door.camera_id, door.name)
    except protect.ProtectError:
        return Response(status_code=502)
    return Response(body, media_type=ctype, headers={"Cache-Control": "no-store"})


@router.post("/doors/{door_id}/camera")
def door_camera(request: Request, door_id: str, camera_id: str = Form(""), user: User = Depends(require_user),
                session: Session = Depends(get_session)):
    door = session.get(Door, door_id)
    if not door or door.site_id not in managed_site_ids(session, user):
        flash(request, "Only admins and site admins can change a door's camera.", "error")
        return redirect("/doors")
    old = door.camera_id
    door.camera_id = camera_id.strip()
    session.add(door)
    if old != door.camera_id:
        audit.record(session, "site.update", f"{user.display} changed the camera for {door.name}", user=user,
                     door_ids=[door.id], site_ids=[door.site_id],
                     changes=[["Camera", old or "none", door.camera_id or "none"]])
    session.commit()
    flash(request, f"Saved the camera for {door.name}.")
    return redirect(f"/doors/{door_id}")


@router.post("/doors/view")
def doors_view(request: Request, view: str = Form("tiles"), next: str = Form("/doors"),
               user: User = Depends(require_user), session: Session = Depends(get_session)):
    user.door_view = view if view in {"tiles", "list"} else ""
    session.add(user)
    session.commit()
    return redirect(safe_next(next))
