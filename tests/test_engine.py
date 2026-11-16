from datetime import timedelta

from sqlmodel import select

from app import engine
from app.db import session_scope
from app.models import DoorState, Schedule, ScheduleDoor, utcnow
from app.schedules import to_local


def add_schedule(start_offset_min, duration, door="demo-front"):
    now = utcnow()
    with session_scope() as s:
        sch = Schedule(label="t", start_local=to_local(now + timedelta(minutes=start_offset_min)),
                       duration_minutes=duration)
        s.add(sch)
        s.flush()
        s.add(ScheduleDoor(schedule_id=sch.id, door_id=door))
        s.commit()
        return sch.id


def test_sync_doors(fake):
    assert engine.sync_doors() == 4
    fake.doors.pop()
    engine.sync_doors()
    with session_scope() as s:
        from app.models import Door
        assert sum(d.present for d in s.exec(select(Door)).all()) == 3


def test_unlock_then_idempotent_then_reset_on_delete(fake):
    engine.sync_doors()
    sid = add_schedule(-5, 65)
    engine.reconcile()
    assert fake.calls == [("unlock", "demo-front", 60)]
    engine.reconcile()
    assert len(fake.calls) == 1  # nothing changed, nothing sent

    with session_scope() as s:
        for row in s.exec(select(ScheduleDoor).where(ScheduleDoor.schedule_id == sid)).all():
            s.delete(row)
        s.delete(s.get(Schedule, sid))
        s.commit()
    engine.reconcile()
    assert fake.calls[-1] == ("reset", "demo-front")


def test_extending_window_resends(fake):
    engine.sync_doors()
    add_schedule(-5, 35)
    engine.reconcile()
    add_schedule(20, 60)  # overlaps, extends to +80
    engine.reconcile()
    assert fake.calls == [("unlock", "demo-front", 30), ("unlock", "demo-front", 80)]


def test_future_schedule_does_nothing_and_expiry_is_silent(fake):
    engine.sync_doors()
    add_schedule(30, 30)
    engine.reconcile()
    assert fake.calls == []
    # Pretend we unlocked earlier and the controller's countdown already ran out
    with session_scope() as s:
        s.add(DoorState(door_id="demo-side", unlocked_until=utcnow() - timedelta(minutes=1)))
        s.commit()
    engine.reconcile()
    assert fake.calls == []
    with session_scope() as s:
        assert s.get(DoorState, "demo-side").unlocked_until is None


def test_restart_catch_up(fake):
    engine.sync_doors()
    add_schedule(-120, 180)  # started two hours ago while we were "down"
    engine.reconcile()
    assert fake.calls == [("unlock", "demo-front", 60)]


def test_controller_error_is_recorded_and_retried(fake):
    from app.unifi import UnifiError
    engine.sync_doors()
    add_schedule(-5, 65)
    real = fake.unlock_for
    fake.unlock_for = lambda *a: (_ for _ in ()).throw(UnifiError("offline"))
    engine.reconcile()
    with session_scope() as s:
        assert s.get(DoorState, "demo-front").last_error == "offline"
    fake.unlock_for = real
    engine.reconcile()
    assert fake.calls == [("unlock", "demo-front", 60)]
