"""Door access report rows: who can get into the doors you pick (downloaded from Reports)."""
from sqlmodel import Session, select

from .auth import allowed_door_ids, managed_site_ids
from .models import Building, Door, Site, User
from .web import all_doors

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
