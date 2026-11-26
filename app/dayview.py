"""Day preview: every door's hours on one date, after holidays, events and UniFi's own schedules."""
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlmodel import Session, select

from . import engine
from .db import get_session
from .models import Site, User
from .schedules import event_name, now_local, to_local, to_utc
from .web import current_site_id, doors_for, render, require_scheduler

router = APIRouter()


def _pct(t: datetime, day0: datetime) -> float:
    return round(max(0.0, min(1.0, (t - day0).total_seconds() / 86400)) * 100, 2)


def door_day(session: Session, door, day: date, tz, hols: dict, unifi_pats: list) -> dict:
    """{blocks: [{kind, start, end, label, left, width}], holiday} for one door on one local day."""
    day0 = datetime.combine(day, datetime.min.time())
    segs = engine.door_timeline(session, door, to_utc(day0, tz), to_utc(day0 + timedelta(days=1), tz), holidays=hols)
    blocks = []
    for g in segs:
        if g.state not in ("unlock", "lock"):
            continue
        a, b = to_local(g.start, tz), to_local(g.end, tz)
        s = g.schedule
        label = (s.label or event_name(s)) if s else ""
        if blocks and blocks[-1]["kind"] == g.state and blocks[-1]["label"] == label and blocks[-1]["end"] == a:
            blocks[-1]["end"] = b
        else:
            blocks.append({"kind": g.state, "start": a, "end": b, "label": label})
    for p in unifi_pats:
        if day.weekday() in p["days"]:
            blocks.append({"kind": "unifi", "start": datetime.combine(day, p["start"]),
                           "end": datetime.combine(day, p["end"]), "label": "UniFi's own schedule"})
    for bl in blocks:
        bl["left"] = _pct(bl["start"], day0)
        bl["width"] = max(0.6, round(_pct(bl["end"], day0) - bl["left"], 2))
    blocks.sort(key=lambda bl: bl["start"])
    return {"blocks": blocks, "holiday": hols.get(day)}


@router.get("/day")
def day_page(request: Request, d: str = "", user: User = Depends(require_scheduler),
             session: Session = Depends(get_session)):
    from .unifiwatch import patterns_for
    try:
        day = date.fromisoformat(d) if d else now_local().date()
    except ValueError:
        day = now_local().date()
    sid = current_site_id(request, session, user)
    doors = [x for x in doors_for(session, user) if not sid or x.site_id == sid]
    sites = {x.id: x for x in session.exec(select(Site)).all()}
    pats = patterns_for(session, doors)
    rows, site_hol = {}, {}
    for door in doors:
        tz = engine.site_tz(sites.get(door.site_id))
        rows[door.id] = door_day(session, door, day, tz, engine.holidays_for(session, door.site_id, door.id),
                                 pats.get(door.id, []))
        if door.site_id not in site_hol:
            site_hol[door.site_id] = engine.holidays_for(session, door.site_id).get(day)
    on_hol = sorted({r["holiday"].name for r in rows.values() if r["holiday"]})
    return render(request, "day.html", user, day=day, doors=doors, rows=rows, on_hol=on_hol,
                  prev=day - timedelta(days=1), next=day + timedelta(days=1), today=now_local().date(),
                  hours=[0, 3, 6, 9, 12, 15, 18, 21], dsites=sites,
                  unlocked=sum(1 for r in rows.values() if any(b["kind"] != "lock" for b in r["blocks"])))
