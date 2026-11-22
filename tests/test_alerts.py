"""Alerts (all off until someone makes one) and emailed reports."""
from datetime import datetime, timedelta

from sqlmodel import select

from app import alerts, reports
from app.db import session_scope
from app.models import AlertRule, AlertSent, Door, DoorEvent, Report, Site, utcnow
from tests.test_visitors import FakeSMTP, mail_on
from tests.test_web import client, setup_admin  # noqa: F401


def make_rule(client, **extra):
    data = {"kind": "held_open", "minutes": "10", "site_id": "1", "users": ["1"], "days": list("0123456"),
            "allday": "1", "name": "Bay doors left open", "enabled": "1"}
    data.update(extra)
    return client.post("/alerts/save", data=data, follow_redirects=True)


def test_nothing_is_on_by_default(client, fake, monkeypatch):
    setup_admin(client)
    mail_on(monkeypatch)
    page = client.get("/alerts").text
    assert "Door left open" in page and "Nothing is sent until you add an alert" in page
    form = client.get("/alerts/new?kind=held_open").text
    assert 'name="minutes" value="10"' in form and 'name="enabled" value="1" >' in form  # starts unticked
    with session_scope() as s:
        assert not s.exec(select(AlertRule)).all()
    # a new alert left unticked stays off
    make_rule(client, enabled="")
    with session_scope() as s:
        assert s.exec(select(AlertRule)).one().enabled is False


def test_door_left_open_alert_goes_to_the_picked_people_once(client, fake, monkeypatch):
    setup_admin(client)
    mail_on(monkeypatch)
    make_rule(client, emails="5551234567@vtext.com")
    with session_scope() as s:
        d = s.get(Door, "1:demo-front")
        d.position_status, d.position_since = "open", utcnow() - timedelta(minutes=12)
        s.add(d)
        rule = s.exec(select(AlertRule)).one()
        rule.enabled_at = utcnow() - timedelta(minutes=30)
        s.add(rule)
        s.commit()
    alerts.check_all()
    alerts.check_all()  # the same open door is only reported once
    sent = [m for m in FakeSMTP.sent if "Front Entrance has been open" in m["Subject"]]
    assert sorted(m["To"] for m in sent) == ["5551234567@vtext.com", "ada@example.com"]
    with session_scope() as s:
        assert len(s.exec(select(AlertSent)).all()) == 1


def test_alert_hours_and_days(client):
    rule = AlertRule(name="x", kind="opened", days="01234", start="18:00", end="06:00")
    import zoneinfo
    tz = zoneinfo.ZoneInfo("UTC")
    assert alerts.active_now(rule, tz, datetime(2026, 11, 23, 22, 0))  # Monday 10 PM
    assert alerts.active_now(rule, tz, datetime(2026, 11, 24, 5, 0))  # Tuesday 5 AM (overnight)
    assert not alerts.active_now(rule, tz, datetime(2026, 11, 23, 12, 0))  # Monday noon
    assert not alerts.active_now(rule, tz, datetime(2026, 11, 22, 22, 0))  # Sunday
    assert alerts.describe_when(rule) == "Weekdays, 6 PM to 6 AM"


def test_turned_away_burst_alert(client, fake, monkeypatch):
    setup_admin(client)
    mail_on(monkeypatch)
    make_rule(client, kind="denied", count="3", minutes="10", doors=["1:demo-side"])
    with session_scope() as s:
        rule = s.exec(select(AlertRule)).one()
        rule.enabled_at = utcnow() - timedelta(minutes=30)
        s.add(rule)
        for i in range(3):
            s.add(DoorEvent(site_id=1, unifi_id=f"x{i}", door_id="1:demo-side", door_name="Side Door",
                            at=utcnow() - timedelta(minutes=i), granted=False))
        s.add(DoorEvent(site_id=1, unifi_id="y", door_id="1:demo-front", door_name="Front Entrance",
                        at=utcnow(), granted=False))
        s.commit()
    alerts.check_all()
    alerts.check_all()
    sent = [m["Subject"] for m in FakeSMTP.sent]
    assert sent.count("OpnDoor: 3 turned-away attempts at Side Door") == 1
    assert not any("Front Entrance" in x for x in sent)  # not one of the picked doors


def test_alert_switch_test_and_delete(client, fake, monkeypatch):
    setup_admin(client)
    mail_on(monkeypatch)
    make_rule(client)
    with session_scope() as s:
        rid = s.exec(select(AlertRule)).one().id
    assert "Test sent to ada@example.com" in client.post(f"/alerts/{rid}/test", follow_redirects=True).text
    client.post(f"/alerts/{rid}/toggle")
    with session_scope() as s:
        assert s.get(AlertRule, rid).enabled is False
    client.post(f"/alerts/{rid}/delete")
    with session_scope() as s:
        assert s.get(AlertRule, rid) is None


def test_weekly_report_preview_and_send(client, fake, monkeypatch):
    setup_admin(client)
    mail_on(monkeypatch)
    with session_scope() as s:
        for i in range(4):
            s.add(DoorEvent(site_id=1, unifi_id=f"r{i}", door_id="1:demo-front", door_name="Front Entrance",
                            at=utcnow() - timedelta(days=2, minutes=i), who="M. Chen", granted=i != 3))
        s.commit()
    r = client.post("/reports/save", data={"frequency": "weekly", "weekday": "0", "hour": "7", "site_id": "1",
                                           "sections": list(reports.SECTIONS), "users": ["1"], "enabled": "1",
                                           "name": "Station weekly"}, follow_redirects=True)
    assert "Station weekly" in r.text and "Every Monday at 7 AM" in r.text
    with session_scope() as s:
        rid = s.exec(select(Report)).one().id
    page = client.get(f"/reports/{rid}/preview").text
    assert "Door activity" in page and "Front Entrance" in page and "turned away" in page and "Coming up" in page
    assert "Sent to ada@example.com" in client.post(f"/reports/{rid}/send", follow_redirects=True).text
    assert FakeSMTP.sent[-1]["Subject"].startswith("Station weekly:")


def test_report_is_due_once_at_its_time():
    rep = Report(name="x", frequency="weekly", weekday=0, hour=7)
    assert reports.due(rep, datetime(2026, 11, 23, 7, 5))  # Monday 7:05
    assert not reports.due(rep, datetime(2026, 11, 23, 8, 5))
    assert not reports.due(rep, datetime(2026, 11, 24, 7, 5))
    rep.frequency = "monthly"
    assert reports.due(rep, datetime(2026, 12, 1, 7, 5)) and not reports.due(rep, datetime(2026, 12, 2, 7, 5))
    s, e, label = reports.period(rep, datetime(2026, 12, 1).date())
    assert (s.day, s.month, e.month, label) == (1, 11, 12, "November 2026")
