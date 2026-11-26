"""Event templates: save a schedule's doors, times and details, and start a new schedule from it."""
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from sqlmodel import Session, select

from . import audit
from .auth import allowed_door_ids
from .db import get_session
from .models import EventTemplate, Schedule, User
from .web import flash, redirect, render, require_scheduler

router = APIRouter()


def templates_for(session: Session, user: User) -> list[EventTemplate]:
    """Templates this person can use: ones with at least one door they can schedule."""
    allowed = allowed_door_ids(session, user)
    return [t for t in session.exec(select(EventTemplate).order_by(EventTemplate.name)).all()
            if any(d in allowed for d in t.doors)]


def usable(session: Session, user: User, tid: int) -> Optional[EventTemplate]:
    return next((t for t in templates_for(session, user) if t.id == tid), None)


def as_schedule(t: EventTemplate, day: date) -> Schedule:
    """An unsaved schedule with the template's settings, on a day."""
    hh, mm = (int(x) for x in t.start_time.split(":"))
    return Schedule(label=t.label, kind=t.kind, action=t.action, event_type=t.event_type, contact=t.contact,
                    notes=t.notes, start_local=datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm),
                    duration_minutes=t.duration_minutes, repeat_weekdays=t.repeat_weekdays, on_holidays=t.on_holidays)


def _can_change(user: User, t: EventTemplate) -> bool:
    return user.is_manager or t.created_by == user.id


@router.get("/templates")
def templates_page(request: Request, user: User = Depends(require_scheduler), session: Session = Depends(get_session)):
    from .models import Door
    rows = templates_for(session, user)
    names = {d.id: d.name for d in session.exec(select(Door)).all()}
    info = {}
    for t in rows:
        s = as_schedule(t, date.today())
        end = s.start_local + timedelta(minutes=t.duration_minutes)
        doors = [names.get(d, "a removed door") for d in t.doors]
        info[t.id] = {"time": f"{s.start_local:%-I:%M %p}–{end:%-I:%M %p}",
                      "doors": ", ".join(doors[:4]) + (f" +{len(doors) - 4} more" if len(doors) > 4 else ""),
                      "what": "Regular hours" if t.kind == "default" else ("Keep locked" if t.action == "lock" else "Unlock"),
                      "can": _can_change(user, t)}
    return render(request, "templates.html", user, rows=rows, info=info)


@router.post("/templates")
def template_save(request: Request, sid: int = Form(...), name: str = Form(""), user: User = Depends(require_scheduler),
                  session: Session = Depends(get_session)):
    from .web import can_edit, schedule_doors
    s = session.get(Schedule, sid)
    doors = schedule_doors(session, [sid]).get(sid, []) if s else []
    if not s or not can_edit(session, user, s, doors):
        flash(request, "You can't use that schedule.", "error")
        return redirect("/schedules")
    t = EventTemplate(name=(name.strip() or s.label)[:80], label=s.label, kind=s.kind, action=s.action,
                      event_type=s.event_type, contact=s.contact, notes=s.notes,
                      start_time=s.start_local.strftime("%H:%M"), duration_minutes=s.duration_minutes,
                      repeat_weekdays=s.repeat_weekdays, on_holidays=s.on_holidays, door_ids=",".join(doors),
                      created_by=user.id)
    session.add(t)
    audit.record(session, "template.create", f"{user.display} saved the template “{t.name}”", user=user, door_ids=doors)
    session.commit()
    flash(request, f"Saved the template “{t.name}”. Find it under New schedule.")
    return redirect(f"/schedules/{sid}")


@router.post("/templates/{tid}/delete")
def template_delete(request: Request, tid: int, user: User = Depends(require_scheduler),
                    session: Session = Depends(get_session)):
    t = session.get(EventTemplate, tid)
    if t and _can_change(user, t):
        audit.record(session, "template.delete", f"{user.display} removed the template “{t.name}”", user=user)
        session.delete(t)
        session.commit()
        flash(request, f"Removed the template “{t.name}”.")
    return redirect("/templates")
