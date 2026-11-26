"""Month view: click a day to see everything on it, with Edit and Delete."""
from datetime import timedelta

from sqlmodel import select

from app.db import session_scope
from app.models import Schedule
from app.schedules import now_local
from tests.test_web import client, setup_admin  # noqa: F401


def test_day_panel_lists_the_day_with_edit_and_delete(client, fake):
    setup_admin(client)
    day = now_local().date() + timedelta(days=3)
    client.post("/schedules", data={"mode": "unlock", "label": "Board meeting", "door_ids": ["1:demo-front"],
                                    "day": day.isoformat(), "start": "18:00", "end": "20:00", "contact": "Jo"})
    client.post("/holidays", data={"name": "Staff day", "start": day.isoformat(), "mode": "allday", "site": "1"})
    with session_scope() as s:
        sid = s.exec(select(Schedule)).one().id
    page = client.get(f"/schedules?d={day.isoformat()}").text  # Month is the default; d opens the day
    assert f'data-day="{day.isoformat()}"' in page and 'id="daypanel"' in page
    assert "★ Staff day" in page and "Contact: Jo" in page
    assert f'href="/schedules/{sid}">Edit</a>' in page and f'action="/schedules/{sid}/delete"' in page
    nxt = f"/schedules?view=month&month={day:%Y-%m}&d={day.isoformat()}"
    assert f'value="{nxt.replace("&", "&amp;")}"' in page
    r = client.post(f"/schedules/{sid}/delete", data={"next": nxt}, follow_redirects=False)
    assert r.headers["location"] == nxt
    with session_scope() as s:
        assert not s.exec(select(Schedule)).all()
    assert client.get("/day?d=bad", follow_redirects=False).status_code in (302, 303, 307)


def test_week_view_opens_the_same_day_panel(client, fake):
    setup_admin(client)
    day = now_local().date() + timedelta(days=2)
    client.post("/schedules", data={"mode": "unlock", "label": "Choir", "door_ids": ["1:demo-front"],
                                    "day": day.isoformat(), "start": "18:00", "end": "19:00"})
    with session_scope() as s:
        sid = s.exec(select(Schedule)).one().id
    page = client.get(f"/schedules?view=week&d={day.isoformat()}").text
    assert 'class="week"' in page and 'id="daypanel"' in page and 'class="mpanel"' in page
    assert f'action="/schedules/{sid}/delete"' in page and "view=week&amp;week=" in page
    assert f"data-day=\"{day.isoformat()}\"]" in page  # opens that day on load
