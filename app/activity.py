"""Door activity: openings and denied attempts copied from each console's UniFi Access log,
turned into busy hours, openings per day, busiest doors, and a searchable list with CSV export."""
import csv
import io
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlmodel import Session, select

from . import engine, unifi
from .auth import allowed_door_ids
from .db import get_session, session_scope
from .models import Building, Door, DoorEvent, Site, User, utcnow
from .schedules import to_local
from .web import current_site_id, render, require_user

log = logging.getLogger(__name__)
router = APIRouter()

FIRST_DAYS = 30  # how far back the first copy reaches
MAX_PAGES = 60

METHODS = {"NFC": "Card", "PIN_CODE": "PIN", "PIN": "PIN", "MOBILE_TAP": "Phone", "MOBILE_BUTTON": "Phone",
           "MOBILE": "Phone", "REMOTE": "Remote", "REMOTE_THROUGH_UAH": "Remote", "FACE": "Face", "QR_CODE": "QR code",
           "TOUCH_PASS": "Touch Pass", "LICENSE_PLATE": "Plate", "BUTTON": "Exit button", "DOORBELL": "Doorbell"}


def _method(provider: str) -> str:
    p = (provider or "").upper()
    if p in METHODS:
        return METHODS[p]
    for k, v in METHODS.items():
        if p.startswith(k):
            return v
    return p.replace("_", " ").title() if p else ""


def sync_site(session: Session, site: Site) -> int:
    """Copy new door-log entries for one console. Returns how many were added."""
    now = utcnow()
    since = (site.log_until - timedelta(minutes=5)) if site.log_until else now - timedelta(days=FIRST_DAYS)
    s_epoch = int(since.replace(tzinfo=timezone.utc).timestamp())
    u_epoch = int(now.replace(tzinfo=timezone.utc).timestamp())
    client = unifi.get_client(site)
    known = set(session.exec(select(DoorEvent.unifi_id).where(DoorEvent.site_id == site.id,
                                                             DoorEvent.at >= since - timedelta(hours=1))).all())
    doors = {d.remote_id: d for d in session.exec(select(Door).where(Door.site_id == site.id)).all()}
    added = 0
    try:
        for page in range(1, MAX_PAGES + 1):
            hits, total = client.door_log(s_epoch, u_epoch, page)
            for h in hits:
                uid = str(h.get("_id") or "")
                src = h.get("_source") or {}
                ev = src.get("event") or {}
                if not uid or uid in known or not ev.get("published"):
                    continue
                known.add(uid)
                tgt = next((t for t in src.get("target") or [] if t.get("type") == "door"), {})
                door = doors.get(tgt.get("id", ""))
                actor = src.get("actor") or {}
                granted = (ev.get("result") or "").upper() == "ACCESS" or ".success" in (ev.get("log_key") or "")
                session.add(DoorEvent(
                    site_id=site.id, unifi_id=uid, door_id=door.id if door else None,
                    door_name=door.name if door else (tgt.get("display_name") or ""),
                    at=datetime.fromtimestamp(ev["published"] / 1000, timezone.utc).replace(tzinfo=None),
                    who=actor.get("display_name") or ("" if granted else "Unknown credential"),
                    method=_method((src.get("authentication") or {}).get("credential_provider", "")),
                    granted=granted, message=ev.get("display_message") or ""))
                added += 1
            if page * 100 >= total or not hits:
                break
    except unifi.UnifiError as e:
        if site.log_status != str(e):
            site.log_status = str(e)[:300]
            session.add(site)
        return added
    site.log_until, site.log_status = now, ""
    session.add(site)
    # old entries are removed by the nightly data cleanup (Settings → Data cleanup)
    return added


def sync_all() -> None:
    with session_scope() as s:
        for site in s.exec(select(Site).where(Site.enabled == True)).all():  # noqa: E712
            try:
                sync_site(s, site)
            except Exception:  # noqa: BLE001 - one console's bad entry must not stop the rest
                log.exception("door log at %s", site.name)
        s.commit()


# ---------- turning events into numbers ----------

SHADES = ["#f1f3f6", "#fbe3bd", "#f5c47e", "#e9a046", "#c66f12", "#8f4a00"]  # OpnDoor amber: none .. busiest
DAYNAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
FULLDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _hour_label(h: int) -> str:
    return "12 AM" if h == 0 else f"{h} AM" if h < 12 else "12 PM" if h == 12 else f"{h - 12} PM"


def hour_span(h: int) -> str:
    """8 -> "8–9 AM", 11 -> "11 AM–12 PM"."""
    a, b = _hour_label(h), _hour_label((h + 1) % 24)
    return f"{a.split()[0]}–{b}" if a.split()[1] == b.split()[1] else f"{a}–{b}"


def _md(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def summarize(session: Session, events: list[DoorEvent], days: int) -> dict:
    """Busy-hours grid, per-day bars, busiest doors and headline numbers (in each site's own time)."""
    tzs = {x.id: engine.site_tz(x) for x in session.exec(select(Site)).all()}
    heat = [[0] * 24 for _ in range(7)]
    heat_denied = [[0] * 24 for _ in range(7)]
    today = to_local(utcnow(), None).date()
    per_day = {today - timedelta(days=k): [0, 0] for k in range(days - 1, -1, -1)}
    cell_days: dict = {}  # (weekday, hour) -> {date: [opened, denied]}
    per_door: dict[str, int] = {}
    granted = denied = 0
    for e in events:
        local = to_local(e.at, tzs.get(e.site_id))
        d = local.date()
        cd = cell_days.setdefault((local.weekday(), local.hour), {}).setdefault(d, [0, 0])
        cd[0 if e.granted else 1] += 1
        if e.granted:
            granted += 1
            heat[local.weekday()][local.hour] += 1
            key = e.door_name or "Unknown door"
            per_door[key] = per_door.get(key, 0) + 1
        else:
            denied += 1
            heat_denied[local.weekday()][local.hour] += 1
        if d in per_day:
            per_day[d][0 if e.granted else 1] += 1
    top = max((v for row in heat for v in row), default=0)

    def shade(v):
        if v == 0 or top == 0:
            return SHADES[0]
        return SHADES[min(5, 1 + int(4.999 * v / top))]

    dates_by_wd = {w: [x for x in per_day if x.weekday() == w] for w in range(7)}

    def tip(d, h):
        head = f"{DAYNAMES[d]} {hour_span(h)} · {heat[d][h]} opened" + (
            f", {heat_denied[d][h]} turned away" if heat_denied[d][h] else "")
        seen = cell_days.get((d, h), {})
        lines = []
        for x in dates_by_wd[d]:
            o, n = seen.get(x, (0, 0))
            lines.append(f"{_md(x)}: {o}" + (f" (+{n} turned away)" if n else ""))
        if lines:
            head += "\n" + "\n".join(lines)
        if heat[d][h] or heat_denied[d][h]:
            head += "\nClick to see who"
        return head

    grid = [{"day": DAYNAMES[d], "wd": d, "cells": [{"h": h, "n": heat[d][h], "denied": heat_denied[d][h],
                                                      "c": shade(heat[d][h]), "tip": tip(d, h)}
                                                     for h in range(24)]} for d in range(7)]
    bd, bh = max(((d, h) for d in range(7) for h in range(24)), key=lambda x: heat[x[0]][x[1]])
    day_rows = [{"d": d, "n": v[0], "denied": v[1]} for d, v in per_day.items()]
    peak_day = max(day_rows, key=lambda r: r["n"], default=None)
    day_max = max((r["n"] + r["denied"] for r in day_rows), default=0) or 1
    doors = sorted(per_door.items(), key=lambda kv: -kv[1])[:8]
    door_max = doors[0][1] if doors else 1
    total = granted + denied
    return {
        "grid": grid, "granted": granted, "denied": denied, "total": total,
        "per_day_avg": round(granted / max(1, days)),
        "denied_pct": round(100 * denied / total) if total else 0,
        "busiest": f"{DAYNAMES[bd]} {_hour_label(bh)}" if top else "—", "busiest_n": top,
        "peak_day": peak_day if peak_day and peak_day["n"] else None,
        "days": day_rows, "day_max": day_max,
        "doors": [{"name": n, "count": c, "pct": round(100 * c / door_max)} for n, c in doors],
        "legend": SHADES,
    }


def parse_on(on: str) -> Optional[date]:
    try:
        return date.fromisoformat(on) if on else None
    except ValueError:
        return None


def narrow(session: Session, rows: list[DoorEvent], wd: Optional[int], hr: Optional[int],
           on: Optional[date]) -> list[DoorEvent]:
    """Keep the events on one weekday / hour / date (in each site's own time)."""
    if wd is None and hr is None and on is None:
        return rows
    tzs = {x.id: engine.site_tz(x) for x in session.exec(select(Site)).all()}
    out = []
    for e in rows:
        local = to_local(e.at, tzs.get(e.site_id))
        if (wd is None or local.weekday() == wd) and (hr is None or local.hour == hr) \
                and (on is None or local.date() == on):
            out.append(e)
    return out


def _slot(wd: str, hr: str) -> tuple[Optional[int], Optional[int]]:
    w = int(wd) if wd.isdigit() and int(wd) < 7 else None
    h = int(hr) if hr.isdigit() and int(hr) < 24 else None
    return w, h


def _events(session: Session, user: User, request: Request, days: int, door: str = "", building: str = "",
            q: str = "") -> list[DoorEvent]:
    allowed = allowed_door_ids(session, user)
    site_id = current_site_id(request, session, user)
    since = utcnow() - timedelta(days=days)
    query = select(DoorEvent).where(DoorEvent.at >= since).order_by(DoorEvent.at.desc())
    if site_id:
        query = query.where(DoorEvent.site_id == site_id)
    rows = [e for e in session.exec(query).all() if (e.door_id in allowed) or (e.door_id is None and user.is_admin)]
    if door:
        rows = [e for e in rows if e.door_id == door]
    elif building.isdigit():
        ids = set(session.exec(select(Door.id).where(Door.building_id == int(building))).all())
        rows = [e for e in rows if e.door_id in ids]
    if q:
        t = q.lower()
        rows = [e for e in rows if t in (e.who or "").lower() or t in (e.door_name or "").lower()
                or t in (e.method or "").lower() or t in (e.message or "").lower()]
    return rows


@router.get("/door-activity")
def activity_page(request: Request, days: int = 30, door: str = "", building: str = "", q: str = "", more: int = 0,
                  wd: str = "", hr: str = "", on: str = "",
                  user: User = Depends(require_user), session: Session = Depends(get_session)):
    days = days if days in (7, 30, 90) else 30
    rows = _events(session, user, request, days, door, building, q)
    stats = summarize(session, rows, days)
    w, h, day = *_slot(wd, hr), parse_on(on)
    picked = None
    if w is not None or h is not None or day is not None:
        if day is not None:
            w = None
        rows = narrow(session, rows, w, h, day)
        if day is not None:
            label = day.strftime("%A") + f", {_md(day)}"
        else:
            label = (FULLDAYS[w] + "s" if w is not None else "Every day")
        if h is not None:
            label += " · " + hour_span(h)
        chips = []
        if w is not None and day is None:  # each date this weekday, for this hour
            for r in stats["days"]:
                if r["d"].weekday() == w:
                    chips.append({"d": r["d"], "label": _md(r["d"])})
        denied = sum(1 for e in rows if not e.granted)
        picked = {"wd": w, "hr": h, "on": day, "label": label, "count": len(rows) - denied, "denied": denied,
                  "chips": chips}
    allowed = allowed_door_ids(session, user)
    site_id = current_site_id(request, session, user)
    door_opts = [d for d in session.exec(select(Door).where(Door.present == True, Door.hidden == False)  # noqa: E712
                                         .order_by(Door.name)).all()
                 if d.id in allowed and (site_id is None or d.site_id == site_id)]
    bids = {d.building_id for d in door_opts if d.building_id}
    buildings = [b for b in session.exec(select(Building).order_by(Building.name)).all() if b.id in bids]
    tzs = {x.id: engine.site_tz(x) for x in session.exec(select(Site)).all()}
    problems = [x for x in session.exec(select(Site)).all() if x.log_status and (site_id is None or x.id == site_id)]
    limit = 500 if more else 50
    recent = [{"e": e, "local": to_local(e.at, tzs.get(e.site_id))} for e in rows[:limit]]
    if picked and picked["chips"]:
        counts: dict = {}
        for e in rows:
            k = to_local(e.at, tzs.get(e.site_id)).date()
            counts[k] = counts.get(k, 0) + 1
        for c in picked["chips"]:
            c["n"] = counts.get(c["d"], 0)
    return render(request, "activity.html", user, stats=stats, days=days, door=door, building=building, q=q,
                  door_opts=door_opts, buildings=buildings, recent=recent, problems=problems, more=more,
                  total_rows=len(rows), picked=picked)


@router.get("/door-activity.csv")
def activity_csv(request: Request, days: int = 30, door: str = "", building: str = "", q: str = "",
                 wd: str = "", hr: str = "", on: str = "",
                 user: User = Depends(require_user), session: Session = Depends(get_session)):
    days = days if days in (7, 30, 90) else 30
    rows = _events(session, user, request, days, door, building, q)
    w, h, day = *_slot(wd, hr), parse_on(on)
    rows = narrow(session, rows, None if day else w, h, day)
    tzs = {x.id: engine.site_tz(x) for x in session.exec(select(Site)).all()}
    sites = {x.id: x.name for x in session.exec(select(Site)).all()}
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["When", "Site", "Door", "Who", "How", "Result", "Details"])
    for e in rows:
        w.writerow([to_local(e.at, tzs.get(e.site_id)).strftime("%Y-%m-%d %H:%M:%S"), sites.get(e.site_id, ""),
                    e.door_name, e.who, e.method, "Opened" if e.granted else "Denied", e.message])
    name = f"door-activity-{date.today().isoformat()}.csv"
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def busy_hours_for(session: Session, door_ids: set, days: int = 30) -> Optional[dict]:
    """The Home page's small busy-hours card (None when there's no activity yet)."""
    since = utcnow() - timedelta(days=days)
    rows = [e for e in session.exec(select(DoorEvent).where(DoorEvent.at >= since)).all() if e.door_id in door_ids]
    return summarize(session, rows, days) if rows else None


def recent_for_door(session: Session, door: Door, limit: int = 8) -> list[dict]:
    tz = engine.site_tz(session.get(Site, door.site_id))
    rows = session.exec(select(DoorEvent).where(DoorEvent.door_id == door.id)
                        .order_by(DoorEvent.at.desc()).limit(limit)).all()
    return [{"e": e, "local": to_local(e.at, tz)} for e in rows]
