"""UniFi Access's own schedules and holidays, shown in OpnDoor so its schedules don't step on them."""
from datetime import datetime, timedelta

from sqlmodel import select

from app import unifiwatch
from app.db import session_scope
from app.models import Door, Holiday, UnifiRuleSeen
from app.schedules import to_utc
from tests.test_web import client, setup_admin  # noqa: F401


def seen_weekdays(door_id, weeks=2):
    """Pretend OpnDoor watched UniFi unlock a door Mon–Fri 8 AM–5 PM (site time) for a couple of weeks."""
    from app.config import TZ
    today = datetime.now(TZ).date()
    with session_scope() as s:
        for k in range(1, 7 * weeks + 1):
            d = today - timedelta(days=k)
            if d.weekday() < 5:
                a = to_utc(datetime(d.year, d.month, d.day, 8, 1), TZ)
                b = to_utc(datetime(d.year, d.month, d.day, 16, 59), TZ)
                s.add(UnifiRuleSeen(door_id=door_id, kind="schedule", start=a, end=b, last_seen=b))
        s.commit()


def test_watching_records_unifi_doing_things_by_itself(client, fake):
    setup_admin(client)
    fake.lock_rules = {"demo-front": "schedule", "demo-side": "keep_unlock"}
    unifiwatch.watch_all()
    unifiwatch.watch_all()
    with session_scope() as s:
        segs = s.exec(select(UnifiRuleSeen)).all()
        kinds = {g.door_id: g.kind for g in segs}
        assert kinds == {"1:demo-front": "schedule", "1:demo-side": "app_unlock"} and len(segs) == 2
    fake.lock_rules = {}
    unifiwatch.watch_all()
    with session_scope() as s:
        assert all(g.end for g in s.exec(select(UnifiRuleSeen)).all())  # both stretches closed


def test_opndoor_own_unlocks_are_not_counted_as_unifi(client, fake):
    setup_admin(client)
    client.post("/quick-unlock", data={"door_ids": "1:demo-side", "minutes": "30", "next": "/"})
    fake.lock_rules = {"demo-side": "custom"}
    unifiwatch.watch_all()
    with session_scope() as s:
        assert not s.exec(select(UnifiRuleSeen)).all()


def test_pattern_shows_on_door_doors_week_and_form(client, fake):
    setup_admin(client)
    seen_weekdays("1:demo-front")
    with session_scope() as s:
        door = s.get(Door, "1:demo-front")
        pat = unifiwatch.pattern(s, door)
        assert unifiwatch.describe(pat) == "Mon–Fri 8 AM–5 PM"
    assert "Mon–Fri 8 AM–5 PM" in client.get("/doors/1:demo-front").text
    assert "UniFi schedule" in client.get("/doors").text
    week = client.get("/schedules?view=week").text
    assert "UniFi unlock schedule" in week and "Front Entrance" in week
    form = client.get("/schedules/new").text
    assert '"1:demo-front": "Mon\\u2013Fri 8 AM\\u20135 PM"' in form  # the JSON the form's warning reads


def test_import_holidays_from_unifi(client, fake):
    setup_admin(client)
    from app.config import TZ
    y = datetime.now(TZ).year + 1
    start = int(datetime(y, 12, 25, 12, tzinfo=TZ).timestamp())
    fake.holidays = [{"id": "g1", "name": "County holidays", "holidays": [
        {"name": "Christmas Day", "start_time": start, "end_time": start + 3600, "is_all_day": True},
        {"name": "New Year's Day", "start_time": f"{y + 1}-01-01T00:00:00Z", "end_time": f"{y + 1}-01-01T23:59:00Z"}]}]
    page = client.get("/holidays/unifi").text
    assert "County holidays" in page and "Christmas Day" in page and "Add 2 to OpnDoor" in page
    client.post("/holidays/unifi", data={"site": "1", "group": "g1"})
    with session_scope() as s:
        names = {(h.name, h.start_date.isoformat()) for h in s.exec(select(Holiday)).all()}
    assert ("Christmas Day", f"{y}-12-25") in names and any(n == "New Year's Day" for n, _ in names)
    assert "All added" in client.get("/holidays/unifi").text  # importing twice adds nothing


def test_raw_views_for_admins(client, fake):
    setup_admin(client)
    fake.lock_rules = {"demo-front": "schedule"}
    rules = client.get("/devices/raw?site=1&what=rules").json()
    assert rules["Front Entrance"] == {"type": "schedule"}
