"""Day preview: every door's hours on one date, after holidays, events and UniFi's own schedules."""
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlmodel import Session, select

from . import engine
from .db import get_session
from .models import Site, User
from .schedules import cut, event_name, holiday_changes, holiday_windows, now_local, to_local, to_utc
from .web import current_site_id, doors_for, render, require_scheduler

router = APIRouter()


def _pct(t: datetime, day0: datetime) -> float:
    return round(max(0.0, min(1.0, (t - day0).total_seconds() / 86400)) * 100, 2)


def door_day(session: Session, door, day: date, tz, hols: dict, unifi_pats: list, scheds: list = None) -> dict:
    """{blocks: [{kind, start, end, label, left, width}], holiday, skipped} for one door on one local day.
    kind: unlock | lock | hol (a holiday keeping it locked) | unifi (UniFi's own schedule, where nothing overrides it)."""
    day0 = datetime.combine(day, datetime.min.time())
    if scheds is None:
        scheds = engine.door_schedules(session, [door.id]).get(door.id, [])
    segs = engine.door_timeline(session, door, to_utc(day0, tz), to_utc(day0 + timedelta(days=1), tz), scheds, hols)
    blocks = []
    for g in segs:
        if g.state not in ("unlock", "lock"):
            continue
        a, b = to_local(g.start, tz), to_local(g.end, tz)
        s = g.schedule
        kind = "hol" if g.holiday is not None else g.state
        label = (s.label or event_name(s)) if s else (g.holiday.name if g.holiday is not None else "")
        if blocks and blocks[-1]["kind"] == kind and blocks[-1]["label"] == label and blocks[-1]["end"] == a:
            blocks[-1]["end"] = b
        else:
            blocks.append({"kind": kind, "start": a, "end": b, "label": label})
    # UniFi's own schedule only matters where OpnDoor isn't holding the door either way
    held = [(bl["start"], bl["end"]) for bl in blocks]
    for p in unifi_pats:
        if day.weekday() in p["days"]:
            for a, b in cut(datetime.combine(day, p["start"]), datetime.combine(day, p["end"]), held):
                blocks.append({"kind": "unifi", "start": a, "end": b, "label": "UniFi's own schedule"})
    for bl in blocks:
        bl["left"] = _pct(bl["start"], day0)
        bl["width"] = max(0.6, round(_pct(bl["end"], day0) - bl["left"], 2))
    blocks.sort(key=lambda bl: bl["start"])
    skipped = [{"label": c["o"].schedule.label or event_name(c["o"].schedule), "start": c["o"].start,
                "end": c["o"].end, "skipped": c["skipped"]}
               for c in holiday_changes(scheds, day0, day0 + timedelta(days=1), hols) if c["o"].start.date() == day]
    return {"blocks": blocks, "holiday": hols.get(day), "skipped": skipped}


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
    request.session["sched_view"] = "day"
    pats = patterns_for(session, doors)
    hmaps = engine.door_holiday_maps(session, doors)
    all_scheds = engine.door_schedules(session, [d.id for d in doors])
    rows = {}
    on_hol: dict = {}
    for door in doors:
        tz = engine.site_tz(sites.get(door.site_id))
        rows[door.id] = r = door_day(session, door, day, tz, hmaps[door.id], pats.get(door.id, []),
                                     all_scheds.get(door.id, []))
        if r["holiday"] is not None:
            on_hol.setdefault(r["holiday"].id, {"h": r["holiday"], "n": 0})["n"] += 1
    holidays_today = [{"h": v["h"], "scope": "every door" if v["n"] == len(doors) else
                       f"{v['n']} of {len(doors)} doors"} for v in on_hol.values()]
    return render(request, "day.html", user, day=day, doors=doors, rows=rows, on_hol=holidays_today,
                  prev=day - timedelta(days=1), next=day + timedelta(days=1), today=now_local().date(),
                  hours=[0, 3, 6, 9, 12, 15, 18, 21], dsites=sites,
                  view="day", unlocked=sum(1 for r in rows.values() if any(b["kind"] in ("unlock", "unifi") for b in r["blocks"])))
