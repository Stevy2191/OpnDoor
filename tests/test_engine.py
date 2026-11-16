from datetime import timedelta

from sqlmodel import select

from app import engine, unifi
from app.db import session_scope
from app.models import Door, DoorState, Schedule, ScheduleDoor, Site, utcnow
from app.schedules import to_local

FRONT = "1:demo-front"


def setup_site():
    engine.ensure_sites()
    engine.sync_doors()


def add(start_offset_min, duration, door=FRONT, kind="override", action="unlock", tz=None):
    now = utcnow()
    with session_scope() as s:
        sch = Schedule(label=f"{kind}-{action}", kind=kind, action=action,
                       start_local=to_local(now + timedelta(minutes=start_offset_min), tz), duration_minutes=duration)
        s.add(sch)
        s.flush()
        s.add(ScheduleDoor(schedule_id=sch.id, door_id=door))
        s.commit()
        return sch.id


def delete(sid):
    with session_scope() as s:
        for row in s.exec(select(ScheduleDoor).where(ScheduleDoor.schedule_id == sid)).all():
            s.delete(row)
        s.delete(s.get(Schedule, sid))
        s.commit()


def test_demo_site_and_door_sync(fake):
    setup_site()
    with session_scope() as s:
        assert [x.name for x in s.exec(select(Site)).all()] == ["Demo site"]
        assert s.get(Door, FRONT).remote_id == "demo-front"
    fake.doors.pop()
    engine.sync_doors()
    with session_scope() as s:
        assert sum(d.present for d in s.exec(select(Door)).all()) == 3


def test_unlock_then_idempotent_then_reset_on_delete(fake):
    setup_site()
    sid = add(-5, 65)
    engine.reconcile()
    assert fake.calls == [("unlock", "demo-front", 60)]
    engine.reconcile()
    assert len(fake.calls) == 1
    delete(sid)
    engine.reconcile()
    assert fake.calls[-1] == ("reset", "demo-front")


def test_lock_override_beats_default_then_default_resumes(fake):
    setup_site()
    add(-60, 8 * 60, kind="default")  # regular hours, open now
    engine.reconcile()
    assert fake.calls[-1][0] == "unlock"
    lock = add(-1, 30, action="lock")  # holiday closure starts now
    engine.reconcile()
    assert fake.calls[-1] == ("lock", "demo-front")
    with session_scope() as s:
        assert s.get(DoorState, FRONT).mode == "lock"
    engine.reconcile()
    assert fake.calls[-1] == ("lock", "demo-front") and len(fake.calls) == 2  # no repeat
    delete(lock)
    engine.reconcile()
    assert fake.calls[-1][0] == "unlock"  # back to regular hours


def test_unlock_override_extends_past_regular_hours(fake):
    setup_site()
    add(-60, 90, kind="default")  # ends in 30 min
    add(20, 60)  # event starts in 20 min, runs to +80
    engine.reconcile()
    assert fake.calls == [("unlock", "demo-front", 80)]


def test_lock_override_ending_with_nothing_after_resets(fake):
    setup_site()
    sid = add(-5, 30, action="lock")
    engine.reconcile()
    assert fake.calls == [("lock", "demo-front")]
    delete(sid)
    engine.reconcile()
    assert fake.calls[-1] == ("reset", "demo-front")


def test_future_schedule_does_nothing_and_expiry_is_silent(fake):
    setup_site()
    add(30, 30)
    engine.reconcile()
    assert fake.calls == []
    with session_scope() as s:
        s.add(DoorState(door_id="1:demo-side", mode="unlock", until=utcnow() - timedelta(minutes=1)))
        s.commit()
    engine.reconcile()
    assert fake.calls == []
    with session_scope() as s:
        assert s.get(DoorState, "1:demo-side").mode == ""


def test_restart_catch_up(fake):
    setup_site()
    add(-120, 180)
    engine.reconcile()
    assert fake.calls == [("unlock", "demo-front", 60)]


def test_controller_error_is_recorded_and_retried(fake):
    setup_site()
    add(-5, 65)
    real = fake.unlock_for
    fake.unlock_for = lambda *a: (_ for _ in ()).throw(unifi.UnifiError("offline"))
    engine.reconcile()
    with session_scope() as s:
        assert s.get(DoorState, FRONT).last_error == "offline"
    fake.unlock_for = real
    engine.reconcile()
    assert fake.calls == [("unlock", "demo-front", 60)]


def test_each_site_uses_its_own_console():
    consoles = {}

    def factory(site):
        return consoles.setdefault(site.id, unifi.FakeUnifiClient(site.name))

    unifi.set_factory(factory)
    engine.ensure_sites()
    with session_scope() as s:
        s.add(Site(name="North", host="https://north.example:12445", token="t", timezone="America/New_York"))
        s.commit()
    engine.sync_doors()
    from zoneinfo import ZoneInfo
    add(-5, 65, door="2:demo-hall", tz=ZoneInfo("America/New_York"))  # wall-clock at that site
    engine.reconcile()
    assert consoles[1].calls == []
    assert consoles[2].calls == [("unlock", "demo-hall", 60)]
