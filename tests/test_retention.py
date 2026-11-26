"""Data cleanup: the main admin sets how long old records are kept."""
from datetime import timedelta

from sqlmodel import select

from app import retention
from app.db import session_scope
from app.models import AuditLog, DoorEvent, Schedule, ScheduleDoor, User, Visitor, utcnow
from app.schedules import now_local
from tests.test_web import client, setup_admin  # noqa: F401


def test_only_the_main_admin_sees_it(client):
    setup_admin(client)
    page = client.get("/settings/cleanup").text
    assert "Data cleanup" in page and "Keep forever" in page
    client.post("/users", data={"name": "Bo", "email": "bo@example.com", "password": "password123", "signin": "password",
                                "role": "admin"})
    client.post("/logout")
    client.post("/login", data={"email": "bo@example.com", "password": "password123"})
    assert "Data cleanup" not in client.get("/settings/backups").text
    assert client.get("/settings/cleanup", follow_redirects=False).status_code != 200


def test_nothing_is_deleted_until_chosen_then_old_things_go(client):
    setup_admin(client)
    old_day = now_local() - timedelta(days=400)
    with session_scope() as s:
        old = Schedule(label="Old fair", start_local=old_day, duration_minutes=60)
        new = Schedule(label="Next fair", start_local=now_local() + timedelta(days=3), duration_minutes=60)
        s.add(old)
        s.add(new)
        s.flush()
        s.add(ScheduleDoor(schedule_id=old.id, door_id="1:demo-front"))
        s.add(ScheduleDoor(schedule_id=new.id, door_id="1:demo-front"))
        s.add(Visitor(site_id=1, first_name="Old", start=utcnow() - timedelta(days=400), end=utcnow() - timedelta(days=399)))
        s.add(DoorEvent(site_id=1, unifi_id="o1", at=utcnow() - timedelta(days=200), who="x"))
        s.add(DoorEvent(site_id=1, unifi_id="n1", at=utcnow() - timedelta(days=5), who="y"))
        s.add(AuditLog(action="x", at=utcnow() - timedelta(days=800), detail="ancient"))
        s.commit()
    client.post("/settings/cleanup/now")
    with session_scope() as s:  # defaults: schedules, visitors and history kept; activity older than 120 days goes
        assert len(s.exec(select(Schedule)).all()) == 2 and s.exec(select(Visitor)).all()
        assert [e.unifi_id for e in s.exec(select(DoorEvent)).all()] == ["n1"]
        assert s.exec(select(AuditLog).where(AuditLog.detail == "ancient")).first()
    client.post("/settings/cleanup", data={"schedules": "365", "visitors": "365", "history": "730", "activity": "120",
                                           "alerts": "90"})
    assert "1 is older than this now" in client.get("/settings/cleanup").text
    r = client.post("/settings/cleanup/now", follow_redirects=True)
    assert "Removed" in r.text
    with session_scope() as s:
        assert [x.label for x in s.exec(select(Schedule)).all()] == ["Next fair"]
        assert not s.exec(select(Visitor)).all()
        assert not s.exec(select(AuditLog).where(AuditLog.detail == "ancient")).first()
        assert len(s.exec(select(ScheduleDoor)).all()) == 1
    assert "cleaned up old data" in client.get("/history").text
    retention.nightly()  # runs without anything left to do
