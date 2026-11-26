"""Data cleanup: how long old schedules, history, door activity, visitors and alerts are kept.
Set by the main admin (Settings → Data cleanup); runs every night and on demand."""
import logging
import os
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from sqlmodel import Session, delete, select

from . import audit, config
from .admin import get, put
from .db import get_session, session_scope
from .models import AlertSent, AuditLog, DoorEvent, Schedule, User, Visitor, utcnow
from .schedules import now_local, series_last_date
from .web import AdminRequired, flash, redirect, render, require_admin

log = logging.getLogger(__name__)
router = APIRouter()

# key: (title, what it is, choices in days (0 = keep forever), default)
KINDS = {
    "schedules": ("Finished schedules", "One-time events and repeating schedules whose last day has passed.",
                  [0, 90, 180, 365, 730], 0),
    "visitors": ("Visitor passes", "Passes whose time is over (cancelled ones too).", [0, 90, 180, 365, 730], 0),
    "activity": ("Door activity", "Openings and turned-away attempts copied from UniFi Access.",
                 [30, 60, 90, 120, 180, 365, 730], 120),
    "history": ("Change history", "Who changed what (History page).", [0, 365, 730, 1095, 1825], 0),
    "alerts": ("Sent alerts", "The record of alerts that went out.", [30, 90, 180, 365], 90),
}


def days(session: Session, kind: str) -> int:
    v = get(session, f"retain.{kind}", "")
    return int(v) if v.isdigit() else KINDS[kind][3]


def label(n: int) -> str:
    if not n:
        return "Keep forever"
    if n % 365 == 0:
        return f"{n // 365} year{'' if n == 365 else 's'}"
    return f"{n} days"


def require_owner(user: User = Depends(require_admin)) -> User:
    if not user.is_owner:
        raise AdminRequired()
    return user


def old_counts(session: Session) -> dict:
    """{kind: how many would go right now}."""
    out = {}
    now, today = utcnow(), now_local().date()
    for kind in KINDS:
        n = days(session, kind)
        if not n:
            out[kind] = 0
            continue
        cut = now - timedelta(days=n)
        if kind == "schedules":
            out[kind] = sum(1 for s in session.exec(select(Schedule)).all()
                            if series_last_date(s) < today - timedelta(days=n))
        elif kind == "visitors":
            out[kind] = len(session.exec(select(Visitor.id).where(Visitor.end < cut)).all())
        elif kind == "activity":
            out[kind] = len(session.exec(select(DoorEvent.id).where(DoorEvent.at < cut)).all())
        elif kind == "history":
            out[kind] = len(session.exec(select(AuditLog.id).where(AuditLog.at < cut)).all())
        elif kind == "alerts":
            out[kind] = len(session.exec(select(AlertSent.id).where(AlertSent.at < cut)).all())
    return out


def clean(session: Session) -> dict:
    """Delete what's older than the settings allow. Returns {kind: how many}."""
    from .calendars import delete_schedule
    gone = {}
    now, today = utcnow(), now_local().date()
    for kind in KINDS:
        n = days(session, kind)
        if not n:
            gone[kind] = 0
            continue
        cut = now - timedelta(days=n)
        if kind == "schedules":
            old = [s for s in session.exec(select(Schedule)).all() if series_last_date(s) < today - timedelta(days=n)]
            for s in old:
                delete_schedule(session, s)
            gone[kind] = len(old)
        else:
            model, col = {"visitors": (Visitor, Visitor.end), "activity": (DoorEvent, DoorEvent.at),
                          "history": (AuditLog, AuditLog.at), "alerts": (AlertSent, AlertSent.at)}[kind]
            gone[kind] = session.exec(delete(model).where(col < cut)).rowcount or 0
    session.commit()
    return gone


def nightly() -> None:
    with session_scope() as s:
        gone = clean(s)
        if any(gone.values()):
            audit.record(s, "settings.cleanup", "Nightly data cleanup removed "
                         + ", ".join(f"{v} {KINDS[k][0].lower()}" for k, v in gone.items() if v), actor="OpnDoor (automatic)")
            s.commit()
            log.info("cleanup: %s", gone)


def _db_size() -> str:
    url = config.DATABASE_URL
    if not url.startswith("sqlite:///"):
        return ""
    try:
        n = os.path.getsize(url.removeprefix("sqlite:///"))
    except OSError:
        return ""
    return f"{n / 1024 / 1024:.1f} MB"


@router.get("/settings/cleanup")
def cleanup_page(request: Request, user: User = Depends(require_owner), session: Session = Depends(get_session)):
    return render(request, "settings_cleanup.html", user, tab="cleanup", kinds=KINDS, cur={k: days(session, k) for k in KINDS},
                  label=label, counts=old_counts(session), size=_db_size())


@router.post("/settings/cleanup")
async def cleanup_save(request: Request, user: User = Depends(require_owner), session: Session = Depends(get_session)):
    f = await request.form()
    before = {KINDS[k][0]: label(days(session, k)) for k in KINDS}
    for k, (_, _, choices, _) in KINDS.items():
        v = str(f.get(k, ""))
        if v.isdigit() and int(v) in choices:
            put(session, f"retain.{k}", v)
    after = {KINDS[k][0]: label(days(session, k)) for k in KINDS}
    audit.record(session, "settings.cleanup", f"{user.display} changed how long old data is kept", user=user,
                 changes=audit.diff(before, after))
    session.commit()
    flash(request, "Saved. Old data is cleaned up every night, or click Clean up now.")
    return redirect("/settings/cleanup")


@router.post("/settings/cleanup/now")
def cleanup_now(request: Request, user: User = Depends(require_owner), session: Session = Depends(get_session)):
    gone = clean(session)
    total = sum(gone.values())
    audit.record(session, "settings.cleanup", f"{user.display} cleaned up old data: "
                 + (", ".join(f"{v} {KINDS[k][0].lower()}" for k, v in gone.items() if v) or "nothing to remove"), user=user)
    session.commit()
    flash(request, f"Removed {total} old record{'' if total == 1 else 's'}." if total else "Nothing old enough to remove.")
    return redirect("/settings/cleanup")
