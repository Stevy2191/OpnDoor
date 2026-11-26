"""Reports to download right away (Excel or CSV): pick a report type, the sites, doors and dates, and download."""
import csv
import io
from collections import Counter
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlmodel import Session, select

from . import audit, engine, xlsx
from .accessreport import _doors as report_doors
from .accessreport import _safe, access_rows
from .auth import can_manage_person, managed_site_ids
from .db import get_session
from .models import AuditLog, Building, Door, DoorEvent, Holiday, Schedule, Site, User, Visitor
from .schedules import now_local, series_last_date, to_local, to_utc
from .web import flash, redirect, render, require_manager

router = APIRouter()

# key: (title, what it shows, filters it uses)
KINDS = {
    "access": ("Door access", "Who can get into the doors you pick: UniFi Access policies and OpnDoor users.",
               {"doors", "include"}),
    "activity": ("Door activity", "Every opening and turned-away attempt, with who, how and when.",
                 {"doors", "dates", "denied"}),
    "busiest": ("Busiest doors", "Openings and turned-away attempts per door, with each door's busiest day and hour.",
                {"sites", "dates"}),
    "visitors": ("Visitors", "Visitor passes: who, host, doors, times, and whether they came.", {"sites", "dates"}),
    "schedules": ("Schedules", "Every current and upcoming schedule: doors, days, times and who made it.", {"sites"}),
    "holidays": ("Holidays", "Holidays and closures: dates, hours, sites and doors.", {"sites"}),
    "changes": ("Change history", "Who changed what and when: schedules, people, settings.", {"sites", "dates"}),
    "people": ("People and roles", "Everyone in OpnDoor: role, sign-in, sites and doors, last sign-in.", {"sites"}),
    "devices": ("Devices", "Readers, hubs and cameras, and whether each is online right now.", {"sites"}),
}


def _sites(session: Session, user: User, site: str) -> list[Site]:
    mine = managed_site_ids(session, user)
    rows = [s for s in session.exec(select(Site).order_by(Site.name)).all() if s.id in mine]
    return [s for s in rows if not site.isdigit() or s.id == int(site)]


def _range(frm: str, to: str) -> tuple[date, date]:
    today = now_local().date()
    try:
        a = date.fromisoformat(frm)
    except ValueError:
        a = today - timedelta(days=30)
    try:
        b = date.fromisoformat(to)
    except ValueError:
        b = today
    return (a, b) if a <= b else (b, a)


def _utc_bounds(a: date, b: date, tz) -> tuple[datetime, datetime]:
    return (to_utc(datetime.combine(a, datetime.min.time()), tz),
            to_utc(datetime.combine(b + timedelta(days=1), datetime.min.time()), tz))


def _fmt(dt, tz=None) -> str:
    return to_local(dt, tz).strftime("%Y-%m-%d %H:%M") if dt else ""


def build(session: Session, user: User, kind: str, p: dict) -> tuple[list, list, str]:
    """(header, rows, title) for a report."""
    sites = _sites(session, user, p.get("site", ""))
    sids = {s.id for s in sites}
    tzs = {s.id: engine.site_tz(s) for s in sites}
    a, b = _range(p.get("from", ""), p.get("to", ""))
    span = f"{a:%b %-d, %Y} – {b:%b %-d, %Y}"
    blds = {x.id: x.name for x in session.exec(select(Building)).all()}
    names = {s.id: s.name for s in session.exec(select(Site)).all()}
    doors_all = {d.id: d for d in session.exec(select(Door)).all()}

    if kind == "access":
        mine = {d.id: d for d in report_doors(session, user)}
        picked = [mine[x] for x in p.get("doors", []) if x in mine]
        rows, errors = access_rows(session, picked, bool(p.get("unifi")), bool(p.get("app")))
        rows += [["Note: " + e] for e in errors]
        return ["Site", "Building", "Door", "Person", "Email", "Access from", "Policy or role", "When"], rows, "Door access"

    if kind == "activity":
        mine = {d.id for d in report_doors(session, user)}
        picked = [x for x in p.get("doors", []) if x in mine]
        rows = []
        for s in _sites(session, user, ""):
            lo, hi = _utc_bounds(a, b, tzs.get(s.id) or engine.site_tz(s))
            q = select(DoorEvent).where(DoorEvent.site_id == s.id, DoorEvent.at >= lo, DoorEvent.at < hi)
            if p.get("denied"):
                q = q.where(DoorEvent.granted == False)  # noqa: E712
            for e in session.exec(q.order_by(DoorEvent.at)).all():
                if picked and e.door_id not in picked:
                    continue
                if not picked and e.door_id and e.door_id not in mine:
                    continue
                d = doors_all.get(e.door_id)
                rows.append([_fmt(e.at, engine.site_tz(s)), s.name, blds.get(d.building_id, "") if d else "",
                             d.name if d else e.door_name, e.who, e.method, "Opened" if e.granted else "Turned away",
                             e.message])
        rows.sort(key=lambda r: r[0])
        return (["When", "Site", "Building", "Door", "Who", "How", "Result", "Details"], rows,
                ("Turned away " if p.get("denied") else "Door activity ") + span)

    if kind == "busiest":
        rows = []
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        for s in sites:
            tz = tzs[s.id]
            lo, hi = _utc_bounds(a, b, tz)
            per: dict = {}
            for e in session.exec(select(DoorEvent).where(DoorEvent.site_id == s.id, DoorEvent.at >= lo,
                                                          DoorEvent.at < hi)).all():
                key = e.door_id or e.door_name
                c = per.setdefault(key, {"ok": 0, "no": 0, "day": Counter(), "hour": Counter(), "how": Counter(),
                                         "name": e.door_name})
                loc = to_local(e.at, tz)
                if e.granted:
                    c["ok"] += 1
                    c["day"][loc.weekday()] += 1
                    c["hour"][loc.hour] += 1
                    if e.method:
                        c["how"][e.method] += 1
                else:
                    c["no"] += 1
            for key, c in per.items():
                d = doors_all.get(key)
                hr = c["hour"].most_common(1)
                rows.append([s.name, blds.get(d.building_id, "") if d else "", d.name if d else c["name"], c["ok"], c["no"],
                             days[c["day"].most_common(1)[0][0]] if c["day"] else "",
                             f"{hr[0][0] % 12 or 12} {'AM' if hr[0][0] < 12 else 'PM'}" if hr else "",
                             c["how"].most_common(1)[0][0] if c["how"] else ""])
        rows.sort(key=lambda r: -r[3])
        return (["Site", "Building", "Door", "Openings", "Turned away", "Busiest day", "Busiest hour", "Most used"],
                rows, "Busiest doors " + span)

    if kind == "visitors":
        from .visitors import arrivals, state
        rows = []
        for s in sites:
            lo, hi = _utc_bounds(a, b, tzs[s.id])
            vs = session.exec(select(Visitor).where(Visitor.site_id == s.id, Visitor.start < hi, Visitor.end >= lo)
                              .order_by(Visitor.start)).all()
            got = arrivals(session, list(vs))
            for v in vs:
                st = state(v, got.get(v.id))
                came = v.checked_in_at or got.get(v.id)
                rows.append([s.name, v.name, v.company, v.email, v.phone, v.reason, v.host_name,
                             ", ".join(doors_all[x].name for x in v.doors if x in doors_all),
                             _fmt(v.start, tzs[s.id]), _fmt(v.end, tzs[s.id]), _fmt(came, tzs[s.id]),
                             {"gone": "Done", "here": "Here", "expected": "Expected", "cancelled": "Cancelled",
                              "problem": "Problem"}.get(st, st) if not (st == "gone" and not came) else "Didn't come"])
        return (["Site", "Visitor", "Company", "Email", "Phone", "Reason", "Host", "Doors", "From", "Until", "Arrived",
                 "Status"], rows, "Visitors " + span)

    if kind == "schedules":
        from .models import ScheduleDoor
        today = now_local().date()
        rows = []
        for s in session.exec(select(Schedule).where(Schedule.status == "").order_by(Schedule.start_local)).all():
            ids = session.exec(select(ScheduleDoor.door_id).where(ScheduleDoor.schedule_id == s.id)).all()
            ds = [doors_all[x] for x in ids if x in doors_all and doors_all[x].site_id in sids]
            if not ds or series_last_date(s) < today:
                continue
            snap = audit.schedule_snapshot(session, s)
            who = session.get(User, s.created_by) if s.created_by else None
            rows.append([", ".join(sorted({names.get(d.site_id, "") for d in ds})), snap["Event"], snap["Type"],
                         snap["Doors"], snap["When"], snap["Time"], snap["On holidays"],
                         {"calendar": "Calendar", "quick": "Quick unlock"}.get(s.source, "OpnDoor"),
                         who.display if who else "", snap["Contact"], snap["Notes"]])
        return (["Site", "Name", "Type", "Doors", "When", "Time", "On holidays", "From", "Made by", "Contact", "Notes"],
                rows, "Schedules")

    if kind == "holidays":
        from .holidays import _when, _where, _site_doors
        sd = _site_doors(session, user, managed_site_ids(session, user))
        rows = []
        for h in session.exec(select(Holiday).order_by(Holiday.start_date)).all():
            if h.sites and not any(x in sids for x in h.sites):
                continue
            nxt = h.next_span(now_local().date())
            where = _where(session, h, sd)
            rows.append([h.name, f"{nxt[0]:%Y-%m-%d}" if nxt else f"{h.start_date:%Y-%m-%d}",
                         f"{nxt[1]:%Y-%m-%d}" if nxt else f"{h.end_date:%Y-%m-%d}",
                         h.when_text(), h.repeat_text() if h.yearly else "Once",
                         "; ".join(w["site"] + (f": {w['detail']}" if w["detail"] else "") for w in where),
                         "Upcoming" if nxt else "Over"])
        return ["Holiday", "From", "Until", "Locked", "Repeats", "Where", "Status"], rows, "Holidays"

    if kind == "changes":
        from .history import _filtered
        params = {"from": a.isoformat(), "to": b.isoformat(), "auto": "1"}
        if p.get("site", "").isdigit():
            params["site"] = p["site"]
        rows = []
        for e in session.exec(_filtered(session, params, user).limit(50_000)).all():
            ch = "; ".join(f"{k}: {x or '—'} → {y or '—'}" for k, x, y in audit.changes_of(e))
            rows.append([_fmt(e.at), e.actor, audit.label(e.action), e.detail, ch])
        return ["When", "Who", "What", "Details", "Changes"], rows, "Change history " + span

    if kind == "people":
        rows = []
        for u in session.exec(select(User).order_by(User.name)).all():
            if not (user.is_admin or u.id == user.id or can_manage_person(session, user, u)):
                continue
            snap = audit.user_snapshot(session, u, None if user.is_admin else sids)
            sites_key = "Manages sites" if u.is_site_admin else "Sites (all doors)"
            rows.append([snap["Name"], snap["Email"], snap["Role"], snap["Signs in with"], snap["Doors from"],
                         snap.get(sites_key, ""), snap["Single doors"], snap["Active"],
                         _fmt(u.last_login_at) or "Never"])
        return (["Name", "Email", "Role", "Signs in with", "Doors from", "Sites", "Single doors", "Active",
                 "Last sign-in"], rows, "People and roles")

    if kind == "devices":
        from .devices import _type, reliable, site_devices
        rows = []
        for s in sites:
            info = site_devices(s)
            dmap = {d.remote_id: d for d in doors_all.values() if d.site_id == s.id}
            for d in info["access"]:
                door = dmap.get(d["door_id"])
                ok = d["online"] if reliable(d) else None
                rows.append([s.name, d.get("alias") or d["name"], _type(d["type"]), door.name if door else "",
                             "Online" if ok else ("Offline" if ok is False else "Unknown"), d.get("firmware", ""),
                             d.get("ip", "")])
            for c in info["cameras"]:
                st = (c.get("state") or "").upper()
                rows.append([s.name, c["name"], "Camera", "", "Online" if st == "CONNECTED" else ("Offline" if st else "Unknown"),
                             "", ""])
            if not info.get("console_ok"):
                rows.append([s.name, "Console", "UniFi Access", "", "Not reachable", "", "; ".join(info["errors"])])
        return ["Site", "Device", "Type", "Door", "Status", "Firmware", "IP address"], rows, "Devices"
    return [], [], ""


@router.get("/reports/new")
def report_kinds(request: Request, user: User = Depends(require_manager)):
    return render(request, "report_kinds.html", user, kinds=KINDS)


@router.get("/reports/access")
def old_access():
    return redirect("/reports/new/access")


@router.get("/reports/new/{kind}")
def report_options(request: Request, kind: str, user: User = Depends(require_manager),
                   session: Session = Depends(get_session)):
    if kind == "summary":
        from .reports import report_new_summary
        return report_new_summary(request, user, session)
    if kind not in KINDS:
        return redirect("/reports/new")
    title, desc, filters = KINDS[kind]
    sites = _sites(session, user, "")
    today = now_local().date()
    return render(request, "report_download.html", user, kind=kind, title=title, desc=desc, filters=filters,
                  rsites=sites, doors=report_doors(session, user) if "doors" in filters else [],
                  dsites={s.id: s for s in sites}, frm=(today - timedelta(days=30)).isoformat(), to=today.isoformat())


@router.get("/reports/download/{kind}")
def report_download(request: Request, kind: str, door: list[str] = Query([]), fmt: str = "xlsx",
                    user: User = Depends(require_manager), session: Session = Depends(get_session)):
    if kind not in KINDS:
        return redirect("/reports/new")
    q = request.query_params
    p = {"site": q.get("site", ""), "from": q.get("from", ""), "to": q.get("to", ""), "doors": door,
         "unifi": q.get("unifi"), "app": q.get("app"), "denied": q.get("denied")}
    if kind == "access":
        if not door:
            flash(request, "Pick at least one door.", "error")
            return redirect("/reports/new/access")
        if not p["unifi"] and not p["app"]:
            flash(request, "Tick UniFi Access, OpnDoor, or both.", "error")
            return redirect("/reports/new/access")
    header, rows, title = build(session, user, kind, p)
    audit.record(session, "report.download", f"{user.display} downloaded the {KINDS[kind][0].lower()} report", user=user,
                 door_ids=door if kind in ("access", "activity") else None)
    session.commit()
    stem = f"{kind}-{now_local():%Y-%m-%d}"
    if not rows:
        rows = [["Nothing found for these choices."]]
    if fmt == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(header)
        for r in rows:
            w.writerow([_safe(c) for c in r])
        return Response("﻿" + out.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{stem}.csv"'})
    return Response(xlsx.build(title[:31] or "Report", header, rows),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{stem}.xlsx"'})
