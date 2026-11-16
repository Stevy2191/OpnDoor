"""Background work: door sync, the unlock/lock reconcile loop, audit helper."""
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from . import unifi
from .db import session_scope
from .models import AuditLog, Door, DoorState, Schedule, ScheduleDoor, User, utcnow
from .schedules import expand, now_local, to_local, unlocked_until

log = logging.getLogger(__name__)
_lock = threading.Lock()

# Re-send an unlock when the desired end moves by more than this.
TOLERANCE = timedelta(seconds=90)


def audit(session: Session, action: str, detail: str = "", user: Optional[User] = None, actor: str = "") -> None:
    session.add(
        AuditLog(
            user_id=user.id if user else None,
            actor=actor or (user.name or user.email if user else "system"),
            action=action,
            detail=detail,
        )
    )


def sync_doors() -> int:
    """Pull the door list from the controller. Returns the number of doors."""
    remote = unifi.get_client().list_doors()
    with session_scope() as s:
        seen = set()
        for rd in remote:
            seen.add(rd.id)
            door = s.get(Door, rd.id) or Door(id=rd.id, name=rd.name)
            door.name = rd.name
            door.full_name = rd.full_name
            door.lock_status = rd.lock_status
            door.position_status = rd.position_status
            door.present = True
            door.updated_at = utcnow()
            s.add(door)
        for door in s.exec(select(Door)).all():
            if door.id not in seen and door.present:
                door.present = False
                s.add(door)
        s.commit()
    return len(remote)


def desired_state(session: Session, now_utc: datetime) -> dict[str, datetime]:
    """door_id -> UTC time the door should stay unlocked until, for doors that should be unlocked now."""
    now_l = to_local(now_utc)
    lo, hi = now_l - timedelta(days=2), now_l + timedelta(days=14)
    rows = session.exec(
        select(Schedule, ScheduleDoor.door_id)
        .join(ScheduleDoor, ScheduleDoor.schedule_id == Schedule.id)
        .where(Schedule.start_local < hi)
    ).all()
    by_door: dict[str, list[Schedule]] = {}
    for sched, door_id in rows:
        by_door.setdefault(door_id, []).append(sched)

    out = {}
    for door_id, scheds in by_door.items():
        windows = [(o.start_utc, o.end_utc) for o in expand(scheds, lo, hi)]
        until = unlocked_until(windows, now_utc)
        if until:
            out[door_id] = until
    return out


def reconcile(now_utc: Optional[datetime] = None) -> None:
    """Make each door match the schedules. Safe to call any time; idempotent."""
    if not _lock.acquire(blocking=False):
        return
    try:
        now = now_utc or utcnow()
        client = unifi.get_client()
        with session_scope() as s:
            desired = desired_state(s, now)
            present = {d.id: d for d in s.exec(select(Door)).all()}
            states = {st.door_id: st for st in s.exec(select(DoorState)).all()}

            for door_id in set(desired) | set(states):
                door = present.get(door_id)
                name = door.name if door else door_id
                st = states.get(door_id) or DoorState(door_id=door_id)
                want = desired.get(door_id)
                have = st.unlocked_until if st.unlocked_until and st.unlocked_until > now else None
                try:
                    if want and (have is None or abs(want - have) > TOLERANCE):
                        client.unlock_for(door_id, (want - now).total_seconds())
                        audit(s, "door.unlock", f"{name} unlocked until {to_local(want):%a %b %-d %-I:%M %p}", actor="scheduler")
                        st.unlocked_until, st.applied_at, st.last_error = want, now, ""
                    elif not want and have is not None:
                        # Schedule was cancelled or shortened while the door was open.
                        client.reset(door_id)
                        audit(s, "door.lock", f"{name} locked (schedule ended early)", actor="scheduler")
                        st.unlocked_until, st.applied_at, st.last_error = None, now, ""
                    elif not want and st.unlocked_until is not None:
                        st.unlocked_until = None  # countdown expired on the controller
                    else:
                        continue
                except unifi.UnifiError as e:
                    if st.last_error != str(e):
                        audit(s, "door.error", f"{name}: {e}", actor="scheduler")
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
