"""History: who changed what, and when.

Every change made in OpnDoor is written here as one plain-English sentence,
plus a list of what changed (before -> after) where that helps.
"""
import json
from datetime import timedelta
from typing import Iterable, Optional

from sqlmodel import Session, select

from .models import AuditLog, Door, Schedule, ScheduleDoor, Site, User

AUTOMATIC = "OpnDoor (automatic)"

# action -> (category, plain label shown in History)
ACTIONS = {
    "schedule.create": ("schedule", "Added a schedule"),
    "schedule.update": ("schedule", "Changed a schedule"),
    "schedule.delete": ("schedule", "Deleted a schedule"),
    "schedule.quick": ("schedule", "Unlocked now"),
    "door.open": ("schedule", "Opened a door"),
    "schedule.locknow": ("schedule", "Locked now"),
    "door.unlock": ("door", "Door unlocked"),
    "door.lock": ("door", "Door kept locked"),
    "door.reset": ("door", "Door back to normal"),
    "door.error": ("door", "Problem reaching a door"),
    "user.setup": ("access", "First setup"),
    "user.create": ("access", "Added a person"),
    "user.update": ("access", "Changed a person"),
    "site.create": ("site", "Added a site"),
    "site.update": ("site", "Changed a site"),
    "site.delete": ("site", "Removed a site"),
    "site.error": ("site", "Problem reaching a site"),
    "site.emergency": ("site", "Lockdown or evacuation"),
    "building.update": ("site", "Changed buildings"),
    "floorplan.update": ("site", "Changed a floor plan"),
    "holiday.update": ("schedule", "Changed holidays"),
    "calendar.approve": ("calendar", "Approved or skipped a calendar event"),
    "calendar.create": ("calendar", "Connected a calendar"),
    "calendar.update": ("calendar", "Changed a calendar"),
    "calendar.delete": ("calendar", "Disconnected a calendar"),
    "calendar.sync": ("calendar", "Calendar updated schedules"),
    "calendar.error": ("calendar", "Problem reading a calendar"),
    "signin.ok": ("signin", "Signed in"),
    "signin.fail": ("signin", "Sign-in refused"),
    "account.update": ("access", "Changed own account"),
    "visitor.create": ("visitor", "Made a visitor pass"),
    "visitor.send": ("visitor", "Emailed a visitor pass"),
    "visitor.cancel": ("visitor", "Cancelled a visitor pass"),
    "screen.create": ("site", "Added a lobby or room screen"),
    "screen.delete": ("site", "Removed a lobby or room screen"),
    "screen.newlink": ("site", "Made a new screen link"),
    "settings.email": ("site", "Changed email settings"),
    "settings.branding": ("site", "Changed the branding"),
    "settings.backup": ("site", "Made a backup"),
    "settings.restore": ("site", "Restored a backup"),
    "settings.update": ("site", "Started an update"),
    "unifi.pin": ("access", "Gave someone a new PIN in UniFi Access"),
    "unifi.active": ("access", "Turned someone's UniFi Access on or off"),
}

CATEGORIES = [
    ("schedule", "Schedules"),
    ("access", "People and access"),
    ("site", "Sites"),
    ("calendar", "Calendars"),
    ("visitor", "Visitors"),
    ("signin", "Sign-ins"),
    ("door", "Automatic door actions"),
]


def label(action: str) -> str:
    return ACTIONS.get(action, ("", action))[1]


def _ids(values: Optional[Iterable]) -> str:
    vals = [str(v) for v in (values or []) if v is not None and v != ""]
    return f",{','.join(vals)}," if vals else ""


def record(session: Session, action: str, detail: str = "", user: Optional[User] = None, actor: str = "",
           changes: Optional[list] = None, site_ids: Optional[Iterable] = None,
           door_ids: Optional[Iterable] = None, schedule_id: Optional[int] = None) -> None:
    door_ids = list(door_ids or [])
    if door_ids and not site_ids:
        site_ids = {int(d.split(":", 1)[0]) for d in door_ids if ":" in d}
    session.add(AuditLog(
        user_id=user.id if user else None,
        actor=actor or (user.display if user else AUTOMATIC),
        action=action,
        category=ACTIONS.get(action, (action.split(".")[0], ""))[0],
        detail=detail,
        changes=json.dumps(changes) if changes else "",
        site_ids=_ids(site_ids),
        door_ids=_ids(door_ids),
        schedule_id=schedule_id,
    ))


def diff(before: dict, after: dict) -> list:
    """[[field, before, after], ...] for fields that differ (both dicts use display text)."""
    return [[k, before.get(k, ""), after.get(k, "")] for k in after if before.get(k, "") != after.get(k, "")]


def changes_of(entry: AuditLog) -> list:
    try:
        return json.loads(entry.changes) if entry.changes else []
    except ValueError:
        return []


# ---- readable snapshots of things, for before/after ----

def schedule_snapshot(session: Session, s: Schedule) -> dict:
    from .schedules import describe_kind, describe_repeat
    from .web import _span  # time formatting shared with the pages
    door_ids = session.exec(select(ScheduleDoor.door_id).where(ScheduleDoor.schedule_id == s.id)).all()
    doors = session.exec(select(Door).where(Door.id.in_(door_ids))).all() if door_ids else []
    sites = {x.id: x.name for x in session.exec(select(Site)).all()}
    multi = len({d.site_id for d in doors}) > 1
    end = s.start_local + timedelta(minutes=s.duration_minutes)
    when = describe_repeat(s) or s.start_local.strftime("%a %b %-d, %Y")
    return {
        "Event": s.label,
        "Type": describe_kind(s),
        "Doors will be": "Unlocked" if s.action != "lock" or s.kind == "default" else "Kept locked",
        "Doors": ", ".join(sorted(f"{d.name} ({sites.get(d.site_id, '')})" if multi else d.name for d in doors)),
        "When": when + (f", starting {s.start_local:%b %-d, %Y}" if s.repeat_weekdays else ""),
        "Time": _span(s.start_local, end),
        "On holidays": ("Skips them" if s.on_holidays != "run" else "Runs as usual") if s.repeat_weekdays else "",
        "Contact": s.contact,
        "Notes": s.notes,
    }


def user_snapshot(session: Session, u: User, only_sites: Optional[set] = None) -> dict:
    """Readable summary of a person. only_sites: show access at just these sites."""
    from .models import UserDoor, UserSite
    sites = session.exec(select(Site).join(UserSite, UserSite.site_id == Site.id).where(UserSite.user_id == u.id)).all()
    doors = session.exec(select(Door).join(UserDoor, UserDoor.door_id == Door.id).where(UserDoor.user_id == u.id)).all()
    if only_sites is not None:
        sites = [x for x in sites if x.id in only_sites]
        doors = [d for d in doors if d.site_id in only_sites]
    return {
        "Name": u.name,
        "Email": u.email,
        "Username": u.username or "—",
        "Role": u.role_name,
        "Start page": {"home": "Home", "doors": "Doors", "map": "Map", "schedules": "Schedules"}.get(u.start_page, "Automatic"),
        "Doors from": "UniFi Access permissions" if u.follows_unifi else "Picked in OpnDoor",
        ("Manages sites" if u.is_site_admin else "Sites (all doors)"): ", ".join(sorted(x.name for x in sites)) or "—",
        "Single doors": ", ".join(sorted(d.name for d in doors)) or "—",
        "Signs in with": ("Microsoft" if not u.password_hash else "Password") if u.id else "",
        "Active": "Yes" if u.active else "No",
    }


def site_snapshot(s: Site) -> dict:
    return {"Name": s.name, "Console address": s.host, "Timezone": s.timezone,
            "Active": "Yes" if s.enabled else "No"}


def for_door(session: Session, door_id: str, limit: int = 15) -> list[AuditLog]:
    return list(session.exec(select(AuditLog).where(AuditLog.door_ids.contains(f",{door_id},"))
                             .where(AuditLog.category != "door")
                             .order_by(AuditLog.at.desc()).limit(limit)).all())
