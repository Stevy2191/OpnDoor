"""Door access report: who can get into the doors you pick, downloaded as CSV or Excel."""
import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlmodel import Session, select

from . import audit, xlsx
from .auth import allowed_door_ids, managed_site_ids
from .db import get_session
from .models import Building, Door, Site, User
from .web import all_doors, flash, redirect, render, require_manager

router = APIRouter()
HEADER = ["Site", "Building", "Door", "Person", "Email", "Access from", "Policy or role", "When"]


def _doors(session: Session, user: User) -> list[Door]:
    managed = managed_site_ids(session, user)
    allowed = allowed_door_ids(session, user)
    return [d for d in all_doors(session) if d.site_id in managed and d.id in allowed]


def access_rows(session: Session, doors: list[Door], unifi_people: bool = True, app_people: bool = True) -> tuple[list, list]:
    """(rows, errors) for the access report."""
    from .whocan import site_access
    sites = {x.id: x for x in session.exec(select(Site)).all()}
    blds = {b.id: b.name for b in session.exec(select(Building)).all()}
    rows, errors, found = [], [], {}
    if unifi_people:
        for sid in sorted({d.site_id for d in doors}):
            data, err = site_access(sites[sid])
            found[sid] = data
            if err:
                errors.append(f"{sites[sid].name}: {err}")
    users = [u for u in session.exec(select(User).where(User.active == True).order_by(User.name)).all()] if app_people else []  # noqa: E712
    user_doors = {u.id: allowed_door_ids(session, u) for u in users}
    for d in sorted(doors, key=lambda d: (sites[d.site_id].name.lower(), blds.get(d.building_id, "").lower(), d.name.lower())):
        base = [sites[d.site_id].name, blds.get(d.building_id, ""), d.name]
        before = len(rows)
        for p in found.get(d.site_id, {}).get(d.remote_id, []):
            rows.append(base + [p["name"], p["email"], "UniFi Access", p["policies"], p["hours"]])
        for u in users:
            if d.id in user_doors[u.id]:
                rows.append(base + [u.display, u.email, "OpnDoor", u.role_name, "Any time" if not u.follows_unifi else "Their UniFi hours"])
        if len(rows) == before:
            rows.append(base + ["(nobody)", "", "", "", ""])
    return rows, errors


def _safe(v: str) -> str:
    """Stop spreadsheet apps from running a cell as a formula."""
    v = str(v or "")
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


@router.get("/reports/access")
def access_page(request: Request, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    doors = _doors(session, user)
    sites = {x.id: x for x in session.exec(select(Site)).all()}
    return render(request, "report_access.html", user, doors=doors, dsites=sites)


@router.get("/reports/access/download")
def access_download(request: Request, door: list[str] = Query([]), fmt: str = "xlsx", unifi: int = 0, app: int = 0,
                    user: User = Depends(require_manager), session: Session = Depends(get_session)):
    mine = {d.id: d for d in _doors(session, user)}
    picked = [mine[x] for x in door if x in mine]
    if not picked:
        flash(request, "Pick at least one door.", "error")
        return redirect("/reports/access")
    if not unifi and not app:
        flash(request, "Tick UniFi Access, OpnDoor, or both.", "error")
        return redirect("/reports/access")
    rows, errors = access_rows(session, picked, bool(unifi), bool(app))
    audit.record(session, "report.access", f"{user.display} downloaded a door access report ({len(picked)} door"
                 f"{'' if len(picked) == 1 else 's'})", user=user, door_ids=[d.id for d in picked])
    session.commit()
    stamp = datetime.now().strftime("%Y-%m-%d")
    note = [["Note: " + e] for e in errors]
    if fmt == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(HEADER)
        for r in rows:
            w.writerow([_safe(c) for c in r])
        for n in note:
            w.writerow(n)
        return Response("﻿" + out.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="door-access-{stamp}.csv"'})
    data = xlsx.build("Door access", HEADER, rows + ([[""]] + note if note else []))
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="door-access-{stamp}.xlsx"'})
