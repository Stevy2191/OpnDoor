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
    q = select(Schedule, ScheduleDoor.door_id).join(ScheduleDoor, ScheduleDoor.schedule_id == Schedule.id)
    if door_ids is not None:
        q = q.where(ScheduleDoor.door_id.in_(door_ids))
    out: dict[str, list[Schedule]] = {}
    for sched, door_id in session.exec(q).all():
        out.setdefault(door_id, []).append(sched)
    return out


def door_timeline(session: Session, door: Door, lo: datetime, hi: datetime,
                  scheds: Optional[list[Schedule]] = None) -> list[Segment]:
    if scheds is None:
        scheds = door_schedules(session, [door.id]).get(door.id, [])
    return timeline(scheds, lo, hi, site_tz(session.get(Site, door.site_id)))


def desired_state(session: Session, now_utc: datetime) -> dict[str, Segment]:
    """door_id -> what the door should be doing right now (doors on their normal rule are omitted)."""
    out = {}
    doors = {d.id: d for d in session.exec(select(Door)).all()}
    for door_id, scheds in door_schedules(session).items():
        door = doors.get(door_id)
        if not door:
            continue
        plan = current_plan(door_timeline(session, door, now_utc, now_utc + HORIZON, scheds))
        if plan:
            out[door_id] = plan
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
                if not door or not site or not site.enabled or not door.present:
                    continue
                tz = site_tz(site)
                want = desired.get(door_id)
                active = st.mode == "lock" or (st.mode == "unlock" and st.until and st.until > now)
                label = want.schedule.label if want and want.schedule else ""
                client = unifi.get_client(site)
                try:
                    if want and want.state == "unlock":
                        if not active or st.mode != "unlock" or abs(want.end - st.until) > TOLERANCE:
                            client.unlock_for(door.remote_id, (want.end - now).total_seconds())
                            audit(s, "door.unlock", f"{door.name} unlocked until "
                                  f"{to_local(want.end, tz):%a %b %-d %-I:%M %p} ({label})", door_ids=[door.id])
                            st.mode, st.applied_at = "unlock", now
                        elif st.schedule_id == (want.schedule.id if want.schedule else None):
                            continue
                        st.until, st.schedule_id, st.last_error = want.end, want.schedule.id, ""
                    elif want and want.state == "lock":
                        if st.mode != "lock":
                            client.keep_lock(door.remote_id)
                            audit(s, "door.lock", f"{door.name} kept locked until "
                                  f"{to_local(want.end, tz):%a %b %-d %-I:%M %p} ({label})", door_ids=[door.id])
                            st.mode, st.applied_at = "lock", now
                        st.until, st.schedule_id, st.last_error = want.end, want.schedule.id, ""
                    elif active:
                        client.reset(door.remote_id)
                        audit(s, "door.reset", f"{door.name} back to normal (locked)", door_ids=[door.id])
                        st.mode, st.until, st.schedule_id, st.applied_at, st.last_error = "", None, None, now, ""
                    elif st.mode or st.until:
                        st.mode, st.until, st.schedule_id = "", None, None  # countdown ran out on the console
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


def safe(fn, *args):
    def run():
        try:
            fn(*args)
        except Exception:
            log.exception("Background job %s failed", getattr(fn, "__name__", fn))

    return run
