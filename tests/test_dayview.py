"""Day preview: every door's hours on one date."""
from datetime import timedelta

from app.schedules import now_local
from tests.test_web import client, setup_admin  # noqa: F401


def test_day_preview_shows_hours_and_holiday(client, fake):
    setup_admin(client)
    day = now_local().date() + timedelta(days=3)
    client.post("/schedules", data={"mode": "default", "label": "Office hours", "door_ids": ["1:demo-front", "1:demo-side"],
                                    "day": now_local().date().isoformat(), "start": "08:00", "end": "17:00",
                                    "weekdays": [str(i) for i in range(7)], "holidays_field": "1", "follow_holidays": "1"})
    client.post("/holidays", data={"name": "Staff day", "start": day.isoformat(), "mode": "closed", "site": "1",
                                   "scope": "some", "doors": ["1:demo-side"]})
    r = client.get(f"/day?d={day.isoformat()}")
    assert r.status_code == 200
    assert "★ <b>Staff day</b>: 1 of" in r.text and "locked all day (badges still work)" in r.text
    assert "8:00 AM–5:00 PM · Office hours" in r.text  # front door keeps its hours
    assert "12:00 AM–11:59 PM · ★ Staff day" in r.text  # side door is held locked for the holiday
    assert "<s>Office hours 8:00 AM–5:00 PM</s> · skipped for the holiday" in r.text
    assert client.get("/day?d=bad").status_code == 200
