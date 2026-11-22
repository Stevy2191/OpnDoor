"""Door activity copied from UniFi Access: sync, numbers, busy hours, permissions, CSV."""
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app import activity, unifi
from app.db import session_scope
from app.models import DoorEvent, Site
from tests.test_access import login
from tests.test_web import client, setup_admin  # noqa: F401


def hit(n, door, when, who="J. Rivera", ok=True, how="NFC"):
    return {"_id": f"h{n}", "_source": {
        "actor": {"display_name": who if ok else "", "type": "user"},
        "authentication": {"credential_provider": how},
        "event": {"display_message": "Access Granted" if ok else "Access Denied",
                  "published": int(when.replace(tzinfo=timezone.utc).timestamp() * 1000),
                  "result": "ACCESS" if ok else "BLOCKED", "log_key": "access.door.unlock." + ("success" if ok else "failed")},
        "target": [{"type": "door", "id": door, "display_name": door}]}}


def sync():
    with session_scope() as s:
        for site in s.exec(select(Site)).all():
            activity.sync_site(s, site)
        s.commit()


def test_copy_door_log_and_show_it(client, fake):
    setup_admin(client)
    now = datetime.utcnow().replace(microsecond=0)
    fake.log_hits = [hit(1, "demo-front", now - timedelta(hours=1)),
                     hit(2, "demo-front", now - timedelta(hours=2), who="M. Chen", how="MOBILE_TAP"),
                     hit(3, "demo-side", now - timedelta(hours=3), ok=False),
                     hit(4, "gone", now - timedelta(hours=4))]
    sync()
    sync()  # nothing copied twice
    with session_scope() as s:
        rows = s.exec(select(DoorEvent).order_by(DoorEvent.at.desc())).all()
        assert len(rows) == 4
        assert rows[0].door_id == "1:demo-front" and rows[0].method == "Card"
        assert rows[1].method == "Phone" and not rows[2].granted and rows[2].who == "Unknown credential"
        assert rows[3].door_id is None
    page = client.get("/door-activity").text
    assert "Busy hours" in page and "M. Chen" in page and "Turned away" in page
    assert "Front Entrance" in page  # busiest doors
    assert "Busy hours" in client.get("/home").text
    assert "J. Rivera" in client.get("/doors/1:demo-front").text
    csv = client.get("/door-activity.csv?q=chen")
    assert csv.headers["content-type"].startswith("text/csv")
    lines = csv.text.strip().splitlines()
    assert lines[0].startswith("When,Site,Door") and len(lines) == 2 and "M. Chen" in lines[1]


def test_people_only_see_their_doors(client, fake):
    setup_admin(client)
    now = datetime.utcnow()
    fake.log_hits = [hit(1, "demo-front", now - timedelta(hours=1), who="Front person"),
                     hit(2, "demo-side", now - timedelta(hours=1), who="Side person")]
    sync()
    client.post("/users", data={"name": "Gus", "email": "gus@example.com", "password": "password123",
                                "signin": "password", "role": "opener", "door_ids": ["1:demo-side"]})
    login(client, "gus@example.com")
    page = client.get("/door-activity").text
    assert "Side person" in page and "Front person" not in page
    assert "Front person" not in client.get("/door-activity.csv").text


def test_token_without_log_permission(client, fake, monkeypatch):
    setup_admin(client)

    def refuse(*a, **k):
        raise unifi.UnifiError("The API token can't read the door log. Add System Log: View to the token.")
    monkeypatch.setattr(fake, "door_log", refuse)
    sync()
    assert "System Log: View" in client.get("/door-activity").text


def test_busy_grid_counts():
    with session_scope() as s:
        s.add(Site(id=1, name="X", host="demo", timezone="UTC"))
        s.flush()
        monday_7 = datetime(2030, 1, 7, 7, 15)
        evs = [DoorEvent(site_id=1, unifi_id=str(i), door_name="Bay 1", at=monday_7, granted=True) for i in range(5)]
        evs.append(DoorEvent(site_id=1, unifi_id="x", door_name="Bay 1", at=monday_7, granted=False))
        st = activity.summarize(s, evs, 30)
    assert st["busiest"] == "Mon 7 AM" and st["busiest_n"] == 5 and st["denied"] == 1
    assert st["grid"][0]["cells"][7]["denied"] == 1 and st["doors"][0] == {"name": "Bay 1", "count": 5, "pct": 100}


def test_squares_show_dates_and_open_the_people_behind_them(client, fake):
    from app.schedules import to_local
    setup_admin(client)
    now = datetime.utcnow().replace(microsecond=0)
    a, b = now - timedelta(days=7, hours=1), now - timedelta(days=14, hours=1)  # same weekday and hour
    fake.log_hits = [hit(1, "demo-front", a, who="Early Bird"), hit(2, "demo-front", b, who="Late Owl"),
                     hit(3, "demo-front", now - timedelta(days=2, hours=5), who="Someone Else")]
    sync()
    la, lb = to_local(a, None), to_local(b, None)
    wd, hr = la.weekday(), la.hour
    page = client.get("/door-activity").text
    assert f"{la.strftime('%b')} {la.day}: 1" in page and f"{lb.strftime('%b')} {lb.day}: 1" in page
    assert f"&wd={wd}&hr={hr}#recent" in page  # the square is a link
    page = client.get(f"/door-activity?days=30&wd={wd}&hr={hr}").text
    assert "Early Bird" in page and "Late Owl" in page and "Someone Else" not in page
    assert "Clear" in page and f"on={la.date().isoformat()}" in page  # a chip for each date
    page = client.get(f"/door-activity?days=30&on={la.date().isoformat()}&hr={hr}").text
    assert "Early Bird" in page and "Late Owl" not in page and "Whole day" in page
    csv = client.get(f"/door-activity.csv?wd={wd}&hr={hr}").text
    assert "Early Bird" in csv and "Someone Else" not in csv
    assert client.get("/door-activity?wd=9&hr=99&on=nope").status_code == 200  # junk is ignored


def test_hour_span():
    assert activity.hour_span(8) == "8–9 AM" and activity.hour_span(11) == "11 AM–12 PM"
    assert activity.hour_span(23) == "11 PM–12 AM" and activity.hour_span(12) == "12–1 PM"


def test_filters_dont_jump_to_the_list(client):
    # a form with no action keeps the page's #recent, so changing the period scrolled to the bottom
    setup_admin(client)
    assert '<form method="get" action="/door-activity" class="card actfilters">' in client.get("/door-activity").text
