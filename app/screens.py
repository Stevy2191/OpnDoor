"""Lobby and room screens: pages on a private link for a tablet or TV, with no sign-in.

A lobby screen welcomes today's visitors (first name and last initial only) and lets them tap their
name to check in. A room screen shows whether a room is free or in use, from the schedules and
calendar events on the room's door. Neither can open a door or change anything else."""
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from . import audit, engine
from .auth import allowed_door_ids, managed_site_ids
from .db import get_session
from .models import Door, Screen, Site, User, Visitor, utcnow
from .schedules import event_name, expand, to_local
from .visitors import arrivals, state, tell_host
from .web import flash, redirect, render, require_manager, templates

router = APIRouter()


def _site_name(session: Session, sid: int) -> str:
    s = session.get(Site, sid)
    return s.name if s else ""


# ---------- managing screens ----------

@router.get("/screens")
def screens_page(request: Request, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    sites = managed_site_ids(session, user)
    screens = [x for x in session.exec(select(Screen).order_by(Screen.name)).all() if x.site_id in sites]
    allowed = allowed_door_ids(session, user)
    doors = [d for d in session.exec(select(Door).where(Door.present == True, Door.hidden == False)  # noqa: E712
                                     .order_by(Door.name)).all() if d.id in allowed and d.site_id in sites]
    site_list = [x for x in session.exec(select(Site).order_by(Site.name)).all() if x.id in sites]
    door_names = {d.id: d.name for d in doors}
    base = str(request.base_url).rstrip("/")
    return render(request, "screens.html", user, screens=screens, doors=doors, site_list=site_list,
                  door_names=door_names, base=base, now=utcnow())


@router.post("/screens")
def screen_create(request: Request, name: str = Form(""), kind: str = Form("lobby"), site_id: int = Form(0),
                  door_id: str = Form(""), message: str = Form(""), checkin: bool = Form(False),
                  user: User = Depends(require_manager), session: Session = Depends(get_session)):
    if kind == "room":
        door = session.get(Door, door_id)
        if not door or door.id not in allowed_door_ids(session, user):
            flash(request, "Pick the room's door.", "error")
            return redirect("/screens")
        site_id = door.site_id
        name = name.strip() or door.name
    elif site_id not in managed_site_ids(session, user):
        flash(request, "Pick a site.", "error")
        return redirect("/screens")
    else:
        name = name.strip() or f"{_site_name(session, site_id)} lobby"
        door_id = ""
    sc = Screen(name=name[:80], site_id=site_id, kind="room" if kind == "room" else "lobby", door_id=door_id or None,
                token=secrets.token_urlsafe(18), message=message.strip()[:160], checkin=checkin, created_by=user.id)
    session.add(sc)
    audit.record(session, "screen.create", f"Added the screen {sc.name}", user=user, site_ids=[site_id])
    session.commit()
    flash(request, f"Screen “{sc.name}” added. Open its link on the tablet.")
    return redirect(f"/screens#screen-{sc.id}")


def _managed_screen(session: Session, user: User, sid: int):
    sc = session.get(Screen, sid)
    return sc if sc and sc.site_id in managed_site_ids(session, user) else None


@router.post("/screens/{sid}/delete")
def screen_delete(request: Request, sid: int, user: User = Depends(require_manager),
                  session: Session = Depends(get_session)):
    sc = _managed_screen(session, user, sid)
    if sc:
        audit.record(session, "screen.delete", f"Removed the screen {sc.name}", user=user, site_ids=[sc.site_id])
        session.delete(sc)
        session.commit()
        flash(request, f"Screen “{sc.name}” removed. Its link no longer works.")
    return redirect("/screens")


@router.post("/screens/{sid}/newlink")
def screen_newlink(request: Request, sid: int, user: User = Depends(require_manager),
                   session: Session = Depends(get_session)):
    sc = _managed_screen(session, user, sid)
    if sc:
        sc.token = secrets.token_urlsafe(18)
        session.add(sc)
        audit.record(session, "screen.newlink", f"Made a new link for the screen {sc.name}", user=user,
                     site_ids=[sc.site_id])
        session.commit()
        flash(request, "New link made. The old one stopped working, so open the new one on the tablet.")
    return redirect(f"/screens#screen-{sid}")


# ---------- what the screens show ----------

def lobby_data(session: Session, sc: Screen) -> dict:
    site = session.get(Site, sc.site_id)
    tz = engine.site_tz(site)
    now = utcnow()
    today = to_local(now, tz).date()
    rows = session.exec(select(Visitor).where(Visitor.site_id == sc.site_id, Visitor.status == "",
                                              Visitor.end >= now - timedelta(hours=12),
                                              Visitor.start <= now + timedelta(hours=24))
                        .order_by(Visitor.start)).all()
    rows = [v for v in rows if to_local(v.start, tz).date() <= today <= to_local(v.end, tz).date()]
    got = arrivals(session, rows)
    people = []
    for v in rows:
        st = state(v, got.get(v.id), now)
        if st in ("gone", "cancelled", "problem"):
            continue
        people.append({"id": v.id, "name": v.short_name, "at": to_local(v.start, tz), "here": st == "here",
                       "host": v.host_name})
    return {"site": site, "now": to_local(now, tz), "people": people}


def room_data(session: Session, sc: Screen) -> dict:
    door = session.get(Door, sc.door_id) if sc.door_id else None
    site = session.get(Site, sc.site_id)
    tz = engine.site_tz(site)
    now = to_local(utcnow(), tz)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    meetings = []
    if door:
        scheds = [s for s in engine.door_schedules(session, [door.id]).get(door.id, [])
                  if s.kind == "override" and s.action == "unlock" and s.source != "quick"]
        for o in expand(scheds, day0, day0 + timedelta(days=1), engine.holidays_for(session, door.site_id)):
            if o.end > now:
                meetings.append({"title": o.schedule.label or event_name(o.schedule), "start": o.start, "end": o.end,
                                 "contact": o.schedule.contact, "now": o.start <= now < o.end})
    current = next((m for m in meetings if m["now"]), None)
    upcoming = [m for m in meetings if not m["now"]]
    return {"site": site, "door": door, "now": now, "current": current, "upcoming": upcoming,
            "free_until": upcoming[0]["start"] if upcoming and not current else None}


def _screen(session: Session, token: str):
    sc = session.exec(select(Screen).where(Screen.token == token)).first() if token else None
    if sc:
        sc.last_seen = utcnow()
        session.add(sc)
        session.commit()
    return sc


def _page(request: Request, name: str, **ctx) -> HTMLResponse:
    resp = templates.TemplateResponse(request, name, ctx)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@router.get("/screen/{token}")
def screen_show(request: Request, token: str, done: str = "", session: Session = Depends(get_session)):
    sc = _screen(session, token)
    if not sc:
        return _page(request, "screen_gone.html")
    if sc.kind == "room":
        return _page(request, "screen_room.html", sc=sc, d=room_data(session, sc))
    return _page(request, "screen_lobby.html", sc=sc, d=lobby_data(session, sc), done=done[:40])


@router.post("/screen/{token}/checkin")
def screen_checkin(request: Request, token: str, visitor: int = Form(0), session: Session = Depends(get_session)):
    sc = _screen(session, token)
    if not sc or sc.kind != "lobby" or not sc.checkin:
        return redirect(f"/screen/{token}")
    if visitor in {p["id"] for p in lobby_data(session, sc)["people"]}:  # only today's visitors for this site
        v = session.get(Visitor, visitor)
        if not v.checked_in_at:
            v.checked_in_at = utcnow()
            session.add(v)
            session.commit()
            tell_host(session, v)
        return redirect(f"/screen/{token}?done={v.first_name}")
    return redirect(f"/screen/{token}")
