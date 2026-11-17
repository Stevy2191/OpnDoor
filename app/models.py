"""Database tables.

Times: schedule start times are stored as naive wall-clock datetimes and are
read in the timezone of each door's site, so a weekly 9:00 schedule stays at
9:00 local time across DST changes and across sites. Everything else (audit
log, door state, sync times) is stored as naive UTC.
"""
from datetime import date, datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(SQLModel, table=True):
    """A person.

    Roles: Admin (is_admin): everything. Site admin (is_site_admin): manages the
    sites in their UserSite rows: those doors, door operators there, calendars
    and history for those sites. Door operator: schedules the doors they're given.
    Door opener (can_schedule False): opens and unlocks their doors, no schedules.
    Operators and openers get their doors either picked in OpnDoor (door_source
    "opndoor") or copied from their UniFi Access policies ("unifi", see UnifiGrant).
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    username: Optional[str] = Field(default=None, index=True)  # optional; sign in with this or the email
    name: str = ""
    password_hash: str = ""  # "" = no password; signs in with Microsoft only
    is_admin: bool = False
    is_owner: bool = False  # the main admin: the account made at first setup; others can't change it
    is_site_admin: bool = False
    active: bool = True
    created_by: Optional[int] = None
    ms_oid: Optional[str] = Field(default=None, index=True)  # Microsoft account id, bound on first sign-in
    last_login_at: Optional[datetime] = None
    theme: str = "auto"  # auto | light | dark
    can_schedule: bool = True  # False = door opener: may open/unlock doors but not make schedules
    door_source: str = "opndoor"  # opndoor | unifi (doors follow their UniFi Access permissions)
    start_page: str = ""  # where they land: "" (automatic), home, doors, map, schedules
    unifi_note: str = ""  # result of the last UniFi Access permission check, shown to admins
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def role_name(self) -> str:
        if self.is_owner:
            return "Main admin"
        if self.is_admin:
            return "Admin"
        if self.is_site_admin:
            return "Site admin"
        return "Door operator" if self.can_schedule else "Door opener"

    @property
    def role(self) -> str:
        if self.is_admin:
            return "admin"
        if self.is_site_admin:
            return "site_admin"
        return "operator" if self.can_schedule else "opener"

    @property
    def schedules(self) -> bool:
        """May make and change schedules (everyone but door openers)."""
        return self.is_admin or self.is_site_admin or self.can_schedule

    @property
    def start_path(self) -> str:
        page = self.start_page or ("doors" if not self.schedules else "home")
        if page == "schedules" and not self.schedules:
            page = "doors"
        return {"doors": "/doors", "map": "/map", "schedules": "/schedules"}.get(page, "/home")

    @property
    def follows_unifi(self) -> bool:
        return not (self.is_admin or self.is_site_admin) and self.door_source == "unifi"

    @property
    def is_manager(self) -> bool:
        """Admins and site admins: can open People, Calendars and History."""
        return self.is_admin or self.is_site_admin

    @property
    def display(self) -> str:
        return self.name or self.email


class Site(SQLModel, table=True):
    """One UniFi Access console (controller)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    host: str = ""  # https://192.168.1.1:12445, or "demo" for a fake console
    token: str = ""
    verify_tls: bool = False
    timezone: str = "America/Chicago"
    enabled: bool = True
    last_sync_at: Optional[datetime] = None
    last_sync_status: str = ""
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def is_demo(self) -> bool:
        return self.host.strip().lower() in {"", "demo"}


class Building(SQLModel, table=True):
    """A group of doors within a site (e.g. "Event Center"), set up in OpnDoor."""

    id: Optional[int] = Field(default=None, primary_key=True)
    site_id: int = Field(foreign_key="site.id", index=True)
    name: str
    created_at: datetime = Field(default_factory=utcnow)


class Door(SQLModel, table=True):
    id: str = Field(primary_key=True)  # "<site_id>:<unifi door id>"
    site_id: int = Field(foreign_key="site.id", index=True)
    building_id: Optional[int] = Field(default=None, foreign_key="building.id", index=True)
    remote_id: str  # UniFi Access door id on that console
    name: str
    full_name: str = ""
    present: bool = True  # False once the door disappears from the controller
    lock_status: str = ""  # last seen relay status ("lock"/"unlock")
    position_status: str = ""  # last seen door position ("open"/"close")
    updated_at: datetime = Field(default_factory=utcnow)


class FloorPlan(SQLModel, table=True):
    """A floor plan image for a building (one per floor)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    building_id: int = Field(foreign_key="building.id", index=True)
    name: str = "Main floor"
    filename: str  # stored in DATA_DIR/floorplans
    created_at: datetime = Field(default_factory=utcnow)


class DoorPin(SQLModel, table=True):
    """Where a door is on a floor plan, as percentages of the image (0-100)."""

    door_id: str = Field(foreign_key="door.id", primary_key=True)
    plan_id: int = Field(foreign_key="floorplan.id", index=True)
    x: float
    y: float


class UnifiGrant(SQLModel, table=True):
    """A door a person may use because of their UniFi Access policies (door_source "unifi").

    hours: JSON {"monday": [["08:00", "17:00"], ...], ...} from the policy's schedule, or "" for any time.
    """

    user_id: int = Field(foreign_key="user.id", primary_key=True)
    door_id: str = Field(foreign_key="door.id", primary_key=True)
    hours: str = ""
    policies: str = ""  # policy names, for showing where access came from


class UserDoor(SQLModel, table=True):
    """Access to one door."""

    user_id: int = Field(foreign_key="user.id", primary_key=True)
    door_id: str = Field(foreign_key="door.id", primary_key=True)


class UserSite(SQLModel, table=True):
    """Access to every door at a site, including doors added later."""

    user_id: int = Field(foreign_key="user.id", primary_key=True)
    site_id: int = Field(foreign_key="site.id", primary_key=True)


class Schedule(SQLModel, table=True):
    """A door schedule.

    kind="default":  the door's regular unlocked hours (always unlock).
    kind="override": while active, replaces the default entirely and sets the
                     door to `action` ("unlock" or "lock"). Newest override wins.
    Times are wall-clock, interpreted in each door's site timezone.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    label: str
    kind: str = "override"  # default | override
    action: str = "unlock"  # unlock | lock (overrides only)
    start_local: datetime  # first occurrence start, naive wall-clock time
    duration_minutes: int
    # Weekly repeat: comma-separated weekday numbers (0=Mon .. 6=Sun); "" = one-off.
    repeat_weekdays: str = ""
    repeat_until: Optional[date] = None
    notes: str = ""
    event_type: str = "event"  # see schedules.EVENT_TYPES
    contact: str = ""  # who to call about it
    source: str = "manual"  # manual | quick | calendar
    feed_id: Optional[int] = Field(default=None, foreign_key="calendarfeed.id", index=True)
    source_uid: Optional[str] = Field(default=None, index=True)
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)
    updated_by: Optional[int] = Field(default=None, foreign_key="user.id")
    updated_at: Optional[datetime] = None


class ScheduleDoor(SQLModel, table=True):
    schedule_id: int = Field(foreign_key="schedule.id", primary_key=True)
    door_id: str = Field(foreign_key="door.id", primary_key=True)


class DoorState(SQLModel, table=True):
    """What OpnDoor last told the controller for a door."""

    door_id: str = Field(primary_key=True)
    mode: str = ""  # "unlock" | "lock" | "" (door on its normal UniFi rule)
    until: Optional[datetime] = None  # UTC end of the current mode
    schedule_id: Optional[int] = None  # schedule that put the door in this mode
    applied_at: Optional[datetime] = None
    last_error: str = ""


class CalendarFeed(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    url: str
    enabled: bool = True
    # Doors used when no rule matches (comma-separated door ids; "" = skip unmatched events).
    default_door_ids: str = ""
    lead_minutes: int = 15  # unlock this long before the event starts
    lag_minutes: int = 15  # lock this long after it ends
    include_all_day: bool = False
    last_sync_at: Optional[datetime] = None
    last_sync_status: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class CalendarRule(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    feed_id: int = Field(foreign_key="calendarfeed.id", index=True)
    field: str = "any"  # any | title | location | description
    contains: str
    door_ids: str  # comma-separated
    position: int = 0


class AuditLog(SQLModel, table=True):
    """History: who did what, when. Written by app/audit.py."""

    id: Optional[int] = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=utcnow, index=True)
    user_id: Optional[int] = Field(default=None, index=True)  # None = automatic
    actor: str = ""  # name at the time, or "OpnDoor (automatic)" / "Calendar sync"
    action: str  # e.g. schedule.update; see audit.ACTIONS
    category: str = Field(default="", index=True)  # schedule | door | access | site | calendar | signin
    detail: str = ""  # one plain-English sentence
    changes: str = ""  # JSON [[field, before, after], ...]
    site_ids: str = ""  # ",1,2," for filtering
    door_ids: str = ""  # ",1:abc,2:def,"
    schedule_id: Optional[int] = None
