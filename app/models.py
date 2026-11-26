"""Database tables.

Times: schedule start times are stored as naive wall-clock datetimes and are
read in the timezone of each door's site, so a weekly 9:00 schedule stays at
9:00 local time across DST changes and across sites. Everything else (audit
log, door state, sync times) is stored as naive UTC.
"""
from datetime import date, datetime, timedelta, timezone
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
    door_view: str = ""  # Doors page layout: "" (automatic), tiles, list
    start_page: str = ""  # where they land: "" (automatic), home, doors, map, schedules
    home_site_id: Optional[int] = None  # the site picked for them when they sign in (None = all their sites)
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
    emergency: str = ""  # "lockdown" | "evacuation" | "" (normal), as UniFi Access reports it
    emergency_by: str = ""
    emergency_at: Optional[datetime] = None
    log_until: Optional[datetime] = None  # door log copied up to here (naive UTC)
    log_status: str = ""  # "" or why the door log can't be read
    protect_key: str = ""  # UniFi OS API key for UniFi Protect (camera pictures); optional
    network_host: str = ""  # console running UniFi Network, if not the Access console; optional
    network_key: str = ""  # UniFi OS API key for UniFi Network (device online check); optional
    network_on: bool = False  # ask UniFi Network whether Access devices are online
    protect_host: str = ""  # Protect console address if not the same console as Access
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
    position_since: Optional[datetime] = None  # when the position last changed (naive UTC)
    camera_id: str = ""  # UniFi Protect camera showing this door (see protect.py)
    hidden: bool = False  # an admin hid it (e.g. a gate hub's unused side door): nobody sees or uses it
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
    # "" = in effect; "pending" = a calendar event waiting for approval; "skipped" = an admin said no
    status: str = Field(default="", index=True)
    status_note: str = ""  # why it waits, e.g. "outside 6 AM to 10 PM"
    on_holidays: str = "skip"  # repeating schedules: "skip" = follow holidays, "run" = as usual


class Holiday(SQLModel, table=True):
    """A day (or days) when the doors it covers are kept locked.

    While it's on, OpnDoor holds those doors locked (over UniFi's own schedules too); repeating schedules
    that follow holidays don't run then. One-time events, and repeating ones set to run on holidays, still do.
    locked "" = all day; "HH:MM-HH:MM" = only that part of each day ("23:59" = to the end of the day).
    site_id None = every site."""

    id: Optional[int] = Field(default=None, primary_key=True)
    site_id: Optional[int] = Field(default=None, foreign_key="site.id", index=True)
    name: str
    start_date: date = Field(index=True)
    end_date: date
    hours: str = ""  # old installs only: "open only these hours". Turned into `locked` on start-up.
    locked: str = ""
    door_ids: str = ""  # comma separated; empty = every door. Only restricts the sites that have doors listed.
    site_ids: str = ""  # several sites (site_id is then None); empty = site_id says where
    # "" = just these dates; every year: "date" (same date), "nth" (e.g. 4th Thursday of November),
    # "last" (e.g. last Monday of May), "us:<name>" (a US holiday, worked out each year)
    yearly: str = ""
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def doors(self) -> list[str]:
        return [d for d in (self.door_ids or "").split(",") if d]

    @property
    def sites(self) -> list[int]:
        """The sites it's for; [] = every site."""
        if self.site_ids:
            return [int(x) for x in self.site_ids.split(",") if x.isdigit()]
        return [self.site_id] if self.site_id else []

    def for_site(self, site_id: Optional[int]) -> bool:
        return not self.sites or site_id in self.sites

    def site_doors(self, site_id) -> list[str]:
        """The doors picked at one site ([] = all its doors)."""
        return [d for d in self.doors if d.split(":", 1)[0] == str(site_id)]

    def covers(self, door_id: Optional[str], site_id: Optional[int] = None) -> bool:
        """Does this holiday apply to the door? door_id None = asking for the whole site (site_id):
        only if every door there is in."""
        if not self.doors:
            return True
        if door_id is None:
            return site_id is not None and not self.site_doors(site_id)
        mine = self.site_doors(door_id.split(":", 1)[0])
        return not mine or door_id in mine

    def date_in(self, year: int) -> Optional[date]:
        """Where it falls in a year (yearly holidays)."""
        import calendar
        s = self.start_date
        if self.yearly.startswith("us:"):
            from .holidays import us_holidays
            return next((d for n, d in us_holidays(year) if n == self.yearly[3:]), None)
        if self.yearly == "date":
            return date(year, s.month, min(s.day, calendar.monthrange(year, s.month)[1]))
        if self.yearly in ("nth", "last"):
            days = [date(year, s.month, d) for d in range(1, calendar.monthrange(year, s.month)[1] + 1)
                    if date(year, s.month, d).weekday() == s.weekday()]
            if self.yearly == "last":
                return days[-1]
            n = (s.day - 1) // 7
            return days[n] if n < len(days) else None
        return None

    def spans(self, until_year: Optional[int] = None) -> list:
        """[(first day, last day)]: just its dates, or each year from its first through until_year."""
        if not self.yearly:
            return [(self.start_date, self.end_date)]
        length = self.end_date - self.start_date
        last = until_year or date.today().year + 2
        out = []
        for y in range(self.start_date.year, last + 1):
            d = self.date_in(y)
            if d and d >= self.start_date - timedelta(days=7):
                out.append((d, d + length))
        return out

    def next_span(self, today: date):
        """The next (or current) time it happens, or None when it's over."""
        return next(((a, b) for a, b in self.spans(today.year + 2) if b >= today), None)

    def repeat_text(self) -> str:
        s = self.start_date
        if self.yearly == "date":
            return f"Every year on {s:%B} {s.day}"
        if self.yearly == "nth":
            return f"Every year on the {('1st', '2nd', '3rd', '4th', '5th')[(s.day - 1) // 7]} {s:%A} of {s:%B}"
        if self.yearly == "last":
            return f"Every year on the last {s:%A} of {s:%B}"
        return ""

    def locked_range(self):
        """(from, to) times for part of the day, or None for all day."""
        if not self.locked or "-" not in self.locked:
            return None
        a, b = self.locked.split("-", 1)
        from datetime import time as _t
        try:
            return _t.fromisoformat(a), _t.fromisoformat(b)
        except ValueError:
            return None

    def window(self, day: date) -> tuple:
        """(start, end) local wall-clock it keeps doors locked on one of its days."""
        from datetime import time as _t
        r = self.locked_range()
        nxt = datetime.combine(day + timedelta(days=1), _t())
        if not r:
            return datetime.combine(day, _t()), nxt
        return datetime.combine(day, r[0]), (nxt if r[1] >= _t(23, 59) else datetime.combine(day, r[1]))

    def when_lower(self) -> str:
        """when_text for the middle of a sentence: "all day", "from 12 PM"."""
        t = self.when_text()
        return t[0].lower() + t[1:]

    def when_text(self) -> str:
        """"All day", "From 12 PM" or "12 PM–3 PM"."""
        r = self.locked_range()
        if not r:
            return "All day"

        def t(x) -> str:
            return f"{x.hour % 12 or 12}{':%02d' % x.minute if x.minute else ''} {'AM' if x.hour < 12 else 'PM'}"
        if r[1].hour == 23 and r[1].minute == 59:
            return f"From {t(r[0])}"
        if r[0].hour == 0 and r[0].minute == 0:
            return f"Until {t(r[1])}"
        return f"{t(r[0])}–{t(r[1])}"


class ScheduleDoor(SQLModel, table=True):
    schedule_id: int = Field(foreign_key="schedule.id", primary_key=True)
    door_id: str = Field(foreign_key="door.id", primary_key=True)


class DoorState(SQLModel, table=True):
    """What OpnDoor last told the controller for a door."""

    door_id: str = Field(primary_key=True)
    mode: str = ""  # "unlock" | "lock" | "" (door on its normal UniFi rule)
    until: Optional[datetime] = None  # UTC end of the current mode
    schedule_id: Optional[int] = None  # schedule that put the door in this mode
    label: str = ""  # why, e.g. the event's name or "★ Thanksgiving"
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
    # Approval: new and changed events wait for a person, and/or only unusual ones do.
    approve_new: bool = False
    approve_over_hours: int = 0  # 0 = no limit; longer events wait
    approve_before: str = ""  # "HH:MM": events starting earlier wait
    approve_after: str = ""  # "HH:MM": events ending later wait
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


class DoorEvent(SQLModel, table=True):
    """A door opening (or a denied attempt) copied from UniFi Access's own log."""

    id: Optional[int] = Field(default=None, primary_key=True)
    site_id: int = Field(foreign_key="site.id", index=True)
    unifi_id: str = Field(index=True)  # the log entry's id on that console
    door_id: Optional[str] = Field(default=None, index=True)  # our door id, if we know the door
    door_name: str = ""
    at: datetime = Field(index=True)  # naive UTC
    who: str = ""  # person, visitor or "OpnDoor (Ada)"
    method: str = ""  # NFC, PIN, mobile, remote, ...
    granted: bool = True
    message: str = ""


class Visitor(SQLModel, table=True):
    """A visitor pass: a PIN and/or QR code in UniFi Access for some doors, for a set time."""

    id: Optional[int] = Field(default=None, primary_key=True)
    site_id: int = Field(foreign_key="site.id", index=True)
    first_name: str
    last_name: str = ""
    email: str = ""
    phone: str = ""
    company: str = ""
    reason: str = ""  # free text shown to staff ("Interview", "Board meeting")
    host_id: Optional[int] = Field(default=None, foreign_key="user.id")
    host_name: str = ""
    start: datetime = Field(index=True)  # naive UTC
    end: datetime = Field(index=True)  # naive UTC
    door_ids: str = ""  # comma separated
    use_pin: bool = True
    use_qr: bool = True
    unifi_id: str = ""  # the visitor on the console
    pin: str = ""
    status: str = ""  # "" active | "cancelled"
    error: str = ""  # why UniFi Access refused it
    emailed_at: Optional[datetime] = None
    checked_in_at: Optional[datetime] = None
    checked_out_at: Optional[datetime] = None
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def short_name(self) -> str:
        """"Jane S." for screens anyone can see."""
        return self.first_name + (f" {self.last_name[0]}." if self.last_name else "")

    @property
    def doors(self) -> list[str]:
        return [d for d in self.door_ids.split(",") if d]


class Screen(SQLModel, table=True):
    """A lobby or room screen: a page on a private link for a tablet or TV, no sign-in."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    site_id: int = Field(foreign_key="site.id", index=True)
    kind: str = "lobby"  # lobby | room
    door_id: Optional[str] = None  # room screens: the room's door
    token: str = Field(index=True, unique=True)
    message: str = ""  # lobby: welcome line
    checkin: bool = True  # lobby: visitors can tap their name to check in
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)
    last_seen: Optional[datetime] = None


class AlertRule(SQLModel, table=True):
    """Something someone asked to be told about. Every rule is made by a person and starts off."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    kind: str  # see alerts.KINDS
    enabled: bool = False
    enabled_at: Optional[datetime] = None  # only things after this count, so turning on doesn't flood
    site_id: Optional[int] = Field(default=None, foreign_key="site.id", index=True)  # None = all your sites
    door_ids: str = ""  # comma separated; empty = every door at the site(s)
    params: str = "{}"  # JSON: thresholds like {"minutes": 10}
    user_ids: str = ""  # comma separated OpnDoor people to email
    emails: str = ""  # extra addresses (e.g. email-to-text), comma separated
    days: str = "0123456"  # weekdays it's active (0 = Monday)
    start: str = ""  # "HH:MM"; empty start and end = all day
    end: str = ""
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)


class AlertSent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    rule_id: int = Field(foreign_key="alertrule.id", index=True)
    key: str = Field(index=True)  # what it was about, so the same thing isn't sent twice
    at: datetime = Field(default_factory=utcnow, index=True)
    subject: str = ""
    sent_to: str = ""
    error: str = ""


class Report(SQLModel, table=True):
    """An emailed summary (daily, weekly or monthly) for the people someone picked."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    enabled: bool = True
    site_id: Optional[int] = Field(default=None, foreign_key="site.id", index=True)  # None = all your sites
    frequency: str = "weekly"  # daily | weekly | monthly
    weekday: int = 0  # weekly: 0 = Monday
    hour: int = 7  # local hour it goes out
    sections: str = "activity,visitors,alerts,devices,changes,upcoming"
    attach: str = ""  # spreadsheets attached to each email: report types from downloads.KINDS, comma separated
    attach_fmt: str = "xlsx"  # xlsx | csv
    user_ids: str = ""
    emails: str = ""
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)
    last_sent_at: Optional[datetime] = None
    last_error: str = ""


class UnifiRuleSeen(SQLModel, table=True):
    """A stretch of time when UniFi Access itself (not OpnDoor) was unlocking or holding a door: its own unlock
    schedule, or someone in the UniFi app. OpnDoor can't read UniFi's door schedules, so it watches for them."""

    id: Optional[int] = Field(default=None, primary_key=True)
    door_id: str = Field(index=True)
    kind: str  # schedule | app_unlock | app_lock
    start: datetime = Field(index=True)  # naive UTC
    end: Optional[datetime] = None  # None while it's still going
    last_seen: datetime = Field(default_factory=utcnow)


class Setting(SQLModel, table=True):
    """Simple app-wide settings (email server, ...)."""

    key: str = Field(primary_key=True)
    value: str = ""


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


class EventTemplate(SQLModel, table=True):
    """A saved schedule to reuse: the doors, times and details, without the date."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    label: str
    kind: str = "override"
    action: str = "unlock"
    event_type: str = "event"
    contact: str = ""
    notes: str = ""
    start_time: str = "09:00"  # HH:MM
    duration_minutes: int = 120
    repeat_weekdays: str = ""
    on_holidays: str = "skip"
    door_ids: str = ""  # comma separated
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def doors(self) -> list[str]:
        return [d for d in self.door_ids.split(",") if d]
