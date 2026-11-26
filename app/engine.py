"""Background work: sites and door sync, the reconcile loop, audit helper."""
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from . import config, unifi
from .audit import record as audit
from .db import session_scope
from .models import Door, DoorState, Schedule, ScheduleDoor, Site, User, utcnow
from .schedules import Segment, current_plan, timeline, to_local

log = logging.getLogger(__name__)
_lock = threading.Lock()

# Re-send an unlock when the desired end moves by more than this.
TOLERANCE = timedelta(seconds=90)
HORIZON = timedelta(days=14)


def site_tz(site: Optional[Site]):
    try:
        return ZoneInfo(site.timezone) if site and site.timezone else config.TZ
    except Exception:
        return config.TZ


def ensure_sites() -> None:
    """First start: create a site from the console the installer saved in .env.

    With no console configured, no site is made: the main admin is asked to add
    one after first sign-in. A demo site is only made when DRY_RUN is on.
    """
    with session_scope() as s:
        if s.exec(select(Site)).first():
            return
        if config.UNIFI_HOST and config.UNIFI_TOKEN not in {"", "changeme"}:
            site = Site(name=config.UNIFI_SITE_NAME, host=config.UNIFI_HOST, token=config.UNIFI_TOKEN,
                        verify_tls=config.UNIFI_VERIFY_TLS, timezone=config.TZ_NAME)
        elif config.DRY_RUN:
            site = Site(name="Demo site", host="demo", timezone=config.TZ_NAME)
        else:
            return
        s.add(site)
        s.commit()


def sync_site(site_id: int) -> str:
    """Pull one console's door list. Returns a status line."""
    with session_scope() as s:
        site = s.get(Site, site_id)
        if not site:
            return "missing"
        try:
            remote = unifi.get_client(site).list_doors()
        except unifi.UnifiError as e:
            status = f"Error: {e}"
        else:
            seen = set()
            for rd in remote:
                did = f"{site.id}:{rd.id}"
                seen.add(did)
                door = s.get(Door, did) or Door(id=did, site_id=site.id, remote_id=rd.id, name=rd.name)
                door.name, door.full_name = rd.name, rd.full_name
                door.lock_status, door.position_status = rd.lock_status, rd.position_status
                door.present, door.updated_at = True, utcnow()
                s.add(door)
            for door in s.exec(select(Door).where(Door.site_id == site.id)).all():
                if door.id not in seen and door.present:
                    door.present = False
                    s.add(door)
            status = f"OK: {len(remote)} doors"
            from . import protect
            if protect.enabled(site):
                s.flush()
                try:
                    protect.match(s, site)
                except protect.ProtectError as e:
                    log.warning("cameras at %s: %s", site.name, e)
        if status != site.last_sync_status and status.startswith("Error"):
            audit(s, "site.error", f"{site.name}: {status}", site_ids=[site.id])
        site.last_sync_at, site.last_sync_status = utcnow(), status[:500]
        s.add(site)
        s.commit()
        return status


def sync_doors() -> None:
    with session_scope() as s:
        ids = s.exec(select(Site.id).where(Site.enabled == True)).all()  # noqa: E712
    for sid in ids:
        sync_site(sid)


def door_schedules(session: Session, door_ids: Optional[list[str]] = None) -> dict[str, list[Schedule]]:
    q = (select(Schedule, ScheduleDoor.door_id).join(ScheduleDoor, ScheduleDoor.schedule_id == Schedule.id)
         .where(Schedule.status == ""))  # waiting-for-approval and skipped calendar events don't count
    if door_ids is not None:
        q = q.where(ScheduleDoor.door_id.in_(door_ids))
    out: dict[str, list[Schedule]] = {}
    for sched, door_id in session.exec(q).all():
        out.setdefault(door_id, []).append(sched)
    return out


def holiday_rows(session: Session, site_id: Optional[int]) -> list:
    from .models import Holiday
    q = select(Holiday)
    q = q.where((Holiday.site_id == None) | (Holiday.site_id == site_id)) if site_id else q.where(Holiday.site_id == None)  # noqa: E711
    rows = [h for h in session.exec(q).all() if h.site_id is not None or h.for_site(site_id)]
    # most specific wins (it goes last): one site > a few sites > every site, then some doors > all doors
    return sorted(rows, key=lambda h: (h.site_id is not None, bool(h.site_ids), bool(h.doors)))


def holiday_map(rows: list, door_id: Optional[str] = None, site_id: Optional[int] = None) -> dict:
    """{date: Holiday} from holiday rows, keeping only holidays that apply to the door (or to every door)."""
    out: dict = {}
    for h in rows:
        if not h.covers(door_id, site_id):
            continue
        for d, last in h.spans():
            while d <= last:
                out[d] = h
                d += timedelta(days=1)
    return out


def holidays_for(session: Session, site_id: Optional[int], door_id: Optional[str] = None) -> dict:
    """{date: Holiday} for a site (its own holidays plus the ones for every site). With a door, only holidays
    that include that door; without one, only holidays for every door at the site."""
    return holiday_map(holiday_rows(session, site_id), door_id, site_id)


def door_timeline(session: Session, door: Door, lo: datetime, hi: datetime,
                  scheds: Optional[list[Schedule]] = None, holidays: Optional[dict] = None) -> list[Segment]:
    if scheds is None:
        scheds = door_schedules(session, [door.id]).get(door.id, [])
    if holidays is None:
        holidays = holidays_for(session, door.site_id, door.id)
    return timeline(scheds, lo, hi, site_tz(session.get(Site, door.site_id)), holidays)


def desired_state(session: Session, now_utc: datetime) -> dict[str, Segment]:
    """door_id -> what the door should be doing right now (doors on their normal rule are omitted)."""
    out = {}
    all_scheds = door_schedules(session)
    hols: dict = {}
    for door in session.exec(select(Door)).all():
        if door.site_id not in hols:
            hols[door.site_id] = holiday_rows(session, door.site_id)
        scheds = all_scheds.get(door.id, [])
        hmap = holiday_map(hols[door.site_id], door.id)
        if not scheds and not hmap:
            continue
        plan = current_plan(door_timeline(session, door, now_utc, now_utc + HORIZON, scheds, hmap))
        # A holiday only stops OpnDoor's unlocks: the door stays on its normal UniFi rule (locked, badges work).
        # It never sends keep_lock, which UniFi treats as a lockdown of that door (badges refused).
        if plan and not (plan.holiday is not None and plan.schedule is None):
            out[door.id] = plan
    return out


def door_holiday_maps(session: Session, doors: list[Door]) -> dict[str, dict]:
    """door id -> {date: Holiday} for each door (only the holidays that cover it)."""
    rows: dict = {}
    out = {}
    for d in doors:
        if d.site_id not in rows:
            rows[d.site_id] = holiday_rows(session, d.site_id)
        out[d.id] = holiday_map(rows[d.site_id], d.id)
    return out


def reconcile(now_utc: Optional[datetime] = None) -> None:
    """Make every door match its schedules. Safe to call any time; idempotent."""
    if not _lock.acquire(blocking=False):
        return
    try:
        now = now_utc or utcnow()
        with session_scope() as s:
            desired = desired_state(s, now)
            doors = {d.id: d for d in s.exec(select(Door)).all()}
            sites = {x.id: x for x in s.exec(select(Site)).all()}
            states = {st.door_id: st for st in s.exec(select(DoorState)).all()}

            for door_id in set(desired) | set(states):
                door = doors.get(door_id)
                site = sites.get(door.site_id) if door else None
                st = states.get(door_id) or DoorState(door_id=door_id)
                if not door or not site or not site.enabled or not door.present or site.emergency:
                    continue  # during a lockdown or evacuation UniFi holds every door; schedules wait
                tz = site_tz(site)
                want = desired.get(door_id)
                active = st.mode == "lock" or (st.mode == "unlock" and st.until and st.until > now)
                label = want.label if want else ""
                sid = want.schedule.id if want and want.schedule else None
                client = unifi.get_client(site)
                try:
                    if want and want.state == "unlock":
                        held = (want.end - now > HOLD_AFTER and st.until is not None and st.until - now > HOLD_AFTER)
                        if not active or st.mode != "unlock" or (abs(want.end - st.until) > TOLERANCE and not held):
                            if want.end - now > HOLD_AFTER:
                                client.keep_unlock(door.remote_id)  # long or open-ended: until set back
                            else:
                                client.unlock_for(door.remote_id, (want.end - now).total_seconds())
                            audit(s, "door.unlock", f"{door.name} unlocked until "
                                  f"{to_local(want.end, tz):%a %b %-d %-I:%M %p} ({label})", door_ids=[door.id])
                            st.mode, st.applied_at = "unlock", now
                        elif st.schedule_id == sid and st.label == label:
                            continue
                        st.until, st.schedule_id, st.label, st.last_error = want.end, sid, label, ""
                    elif want and want.state == "lock":
                        if st.mode != "lock":
                            # Back to its normal UniFi rule: locked, badges work. Never keep_lock: UniFi treats that
                            # as a lockdown of the door and refuses badges. Only the Lockdown button locks doors down.
                            client.reset(door.remote_id)
                            audit(s, "door.lock", f"{door.name} kept locked until "
                                  f"{to_local(want.end, tz):%a %b %-d %-I:%M %p} ({label})", door_ids=[door.id])
                            st.mode, st.applied_at = "lock", now
                        st.until, st.schedule_id, st.label, st.last_error = want.end, sid, label, ""
                    elif active:
                        client.reset(door.remote_id)
                        audit(s, "door.reset", f"{door.name} back to normal (locked)", door_ids=[door.id])
                        st.mode, st.until, st.schedule_id, st.applied_at, st.last_error = "", None, None, now, ""
                        st.label = ""
                    elif st.mode or st.until:
                        st.mode, st.until, st.schedule_id = "", None, None  # countdown ran out on the console
                        st.label = ""
                    else:
                        continue
                except unifi.UnifiError as e:
                    if st.last_error != str(e):
                        audit(s, "door.error", f"{door.name}: {e}", door_ids=[door.id])
                    st.last_error = str(e)
                    log.warning("Door %s: %s", door_id, e)
                s.add(st)
            s.commit()
    finally:
        _lock.release()


HOLD_AFTER = timedelta(hours=24)


def refresh_states() -> None:
    """Every few seconds: each door's lock and door-sensor state, and whether the console is in
    lockdown or evacuation (also when someone started it in the UniFi Access app)."""
    with session_scope() as s:
        for site in s.exec(select(Site).where(Site.enabled == True)).all():  # noqa: E712
            client = unifi.get_client(site)
            try:
                remote = {rd.id: rd for rd in client.list_doors()}
                mode = client.get_emergency()
            except unifi.UnifiError as e:
                log.debug("status at %s: %s", site.name, e)
                continue
            for door in s.exec(select(Door).where(Door.site_id == site.id)).all():
                rd = remote.get(door.remote_id)
                if rd and (rd.lock_status, rd.position_status) != (door.lock_status, door.position_status):
                    if rd.position_status != door.position_status:
                        door.position_since = utcnow()
                    door.lock_status, door.position_status = rd.lock_status, rd.position_status
                    s.add(door)
            if mode != site.emergency:
                was = site.emergency
                site.emergency = mode
                if mode:  # started outside OpnDoor
                    site.emergency_by, site.emergency_at = "the UniFi Access app", utcnow()
                    audit(s, "site.emergency", f"{mode.capitalize()} started at {site.name} in the UniFi Access app",
                          site_ids=[site.id])
                else:
                    audit(s, "site.emergency", f"{was.capitalize()} ended at {site.name} in the UniFi Access app",
                          site_ids=[site.id])
                    forget_states(s, site.id)
                s.add(site)
        s.commit()


def forget_states(session: Session, site_id: int) -> None:
    """After an emergency UniFi's door rules may have changed: re-apply every schedule."""
    ids = set(session.exec(select(Door.id).where(Door.site_id == site_id)).all())
    for st in session.exec(select(DoorState)).all():
        if st.door_id in ids:
            st.mode, st.until, st.schedule_id = "", None, None
            session.add(st)


def safe(fn, *args):
    def run():
        try:
            fn(*args)
        except Exception:
            log.exception("Background job %s failed", getattr(fn, "__name__", fn))

    return run
