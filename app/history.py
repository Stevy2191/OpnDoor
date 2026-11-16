"""History page: who changed what, and when (admins)."""
import csv
import io
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlmodel import Session, select

from . import audit
from .db import get_session
from .models import AuditLog, Door, Site, User
from .schedules import to_local
from .web import render, require_admin

router = APIRouter()
PAGE = 100


def _filtered(session: Session, params) -> tuple:
    q = select(AuditLog)
    who, what = params.get("who", ""), params.get("what", "")
    site, door = params.get("site", ""), params.get("door", "")
    if who == "auto":
        q = q.where(AuditLog.user_id == None)  # noqa: E711
    elif who.isdigit():
        q = q.where(AuditLog.user_id == int(who))
    if what:
        q = q.where(AuditLog.category == what)
    elif params.get("auto") != "1":
        q = q.where(AuditLog.category != "door")  # hide routine automatic door actions unless asked
    if site.isdigit():
        q = q.where(AuditLog.site_ids.contains(f",{site},"))
    if door:
        q = q.where(AuditLog.door_ids.contains(f",{door},"))
    for key, op in (("from", "ge"), ("to", "lt")):
        try:
            d = date.fromisoformat(params.get(key, ""))
        except ValueError:
            continue
        # dates are picked in local time; the log is stored in UTC
        from .schedules import to_utc
        bound = to_utc(datetime.combine(d + timedelta(days=1 if op == "lt" else 0), datetime.min.time()))
        q = q.where(AuditLog.at >= bound if op == "ge" else AuditLog.at < bound)
    return q.order_by(AuditLog.at.desc(), AuditLog.id.desc())


@router.get("/activity")
def old_activity():
    return RedirectResponse("/history", status_code=301)


@router.get("/history")
def history(request: Request, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    params = dict(request.query_params)
    page = max(0, int(params.get("page", "0") or 0)) if str(params.get("page", "0")).isdigit() else 0
    rows = session.exec(_filtered(session, params).offset(page * PAGE).limit(PAGE + 1)).all()
    more = len(rows) > PAGE
    people = session.exec(select(User).order_by(User.name)).all()
    doors = session.exec(select(Door).order_by(Door.site_id, Door.name)).all()
    entries = [{"e": e, "at": to_local(e.at), "label": audit.label(e.action), "changes": audit.changes_of(e)}
               for e in rows[:PAGE]]
    query = "&".join(f"{k}={v}" for k, v in params.items() if k != "page" and v)
    return render(request, "history.html", user, entries=entries, people=people, doors=doors,
                  all_sites={x.id: x for x in session.exec(select(Site)).all()},
                  categories=audit.CATEGORIES, f=params, page=page, more=more, query=query)


@router.get("/history.csv")
def history_csv(request: Request, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    rows = session.exec(_filtered(session, dict(request.query_params)).limit(50_000)).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["When", "Who", "What", "Details", "Changes"])
    for e in rows:
        changes = "; ".join(f"{k}: {b or '—'} → {a or '—'}" for k, b, a in audit.changes_of(e))
        w.writerow([to_local(e.at).strftime("%Y-%m-%d %H:%M:%S"), e.actor, audit.label(e.action), e.detail, changes])
    name = f"opndoor-history-{date.today().isoformat()}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})
