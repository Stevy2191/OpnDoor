"""Event templates: save a schedule and start a new one from it."""
from datetime import timedelta

from sqlmodel import select

from app.db import session_scope
from app.models import EventTemplate, Schedule
from app.schedules import now_local
from tests.test_web import client, setup_admin  # noqa: F401


def test_save_and_use_a_template(client):
    setup_admin(client)
    day = (now_local() + timedelta(days=2)).date().isoformat()
    client.post("/schedules", data={"mode": "unlock", "label": "Rodeo", "door_ids": ["1:demo-front", "1:demo-side"],
                                    "day": day, "start": "17:30", "end": "22:00", "contact": "Jo, 555-0100"})
    with session_scope() as s:
        sid = s.exec(select(Schedule)).one().id
    r = client.post("/templates", data={"sid": sid, "name": "Rodeo night"}, follow_redirects=True)
    assert "Saved the template" in r.text
    with session_scope() as s:
        t = s.exec(select(EventTemplate)).one()
        assert (t.start_time, t.duration_minutes, t.door_ids) == ("17:30", 270, "1:demo-front,1:demo-side")
    assert "Rodeo night" in client.get("/schedules/new").text
    page = client.get(f"/schedules/new?template={t.id}").text
    assert "Started from the template" in page and 'value="Rodeo"' in page and 'value="17:30"' in page
    assert 'value="22:00"' in page and "Jo, 555-0100" in page and 'action="/schedules"' in page
    assert "Rodeo night" in client.get("/templates").text
    client.post(f"/templates/{t.id}/delete")
    with session_scope() as s:
        assert not s.exec(select(EventTemplate)).all()
