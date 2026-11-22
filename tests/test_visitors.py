"""Visitor passes, email, and lobby/room screens."""
import re
from datetime import datetime, timedelta

from sqlmodel import select

from app import mailer
from app.db import session_scope
from app.models import Schedule, ScheduleDoor, Screen, Setting, Visitor
from app.schedules import now_local, to_local
from tests.test_web import client, setup_admin  # noqa: F401


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def mail_on(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    with session_scope() as s:
        for k, v in {"host": "smtp.example.com", "port": "587", "security": "starttls", "sender": "doors@example.com"}.items():
            s.add(Setting(key="mail." + k, value=v))
        s.commit()


def new_pass(client, **extra):
    day = now_local().date() + timedelta(days=1)
    data = {"first_name": "Jane", "last_name": "Smith", "email": "jane@example.com", "company": "Acme",
            "host": "Ada", "date": day.isoformat(), "start": "09:00", "end": "11:00", "doors": ["1:demo-front"],
            "creds": "both", "send": "on", "tell_me": "on"}
    data.update(extra)
    return client.post("/visitors/new", data=data, follow_redirects=True)


def test_pass_is_made_in_unifi_and_emailed(client, fake, monkeypatch):
    setup_admin(client)
    mail_on(monkeypatch)
    r = new_pass(client)
    assert "Pass made for Jane Smith" in r.text and "Emailed to jane@example.com" in r.text
    with session_scope() as s:
        v = s.exec(select(Visitor)).one()
        assert v.unifi_id in fake.visitors and v.pin and fake.visitors[v.unifi_id]["pin"] == v.pin
        assert fake.visitors[v.unifi_id]["doors"] == ["demo-front"] and fake.visitors[v.unifi_id]["qr"]
        assert fake.visitors[v.unifi_id]["end"] - fake.visitors[v.unifi_id]["start"] == 7200
        vid, pin = v.id, v.pin
    msg = FakeSMTP.sent[-1]
    assert msg["To"] == "jane@example.com" and pin in msg.get_body(("plain",)).get_content()
    assert any(p.get_content_type() == "image/png" for p in msg.walk())  # the QR code
    assert client.get(f"/visitors/{vid}/qr.png").headers["content-type"] == "image/png"
    page = client.get("/visitors").text
    assert "Jane Smith" in page and "Coming up" in page


def test_pass_needs_doors_at_one_site_and_email_when_sending(client, fake):
    setup_admin(client)
    assert "Pick at least one door" in new_pass(client, doors=[]).text
    r = new_pass(client)  # email isn't set up: the pass is still made
    assert "Pass made for Jane Smith" in r.text and "The pass is ready, but it" in r.text
    assert "Add their email address" in new_pass(client, email="").text


def test_cancel_removes_the_visitor_from_unifi(client, fake):
    setup_admin(client)
    new_pass(client, send="")
    with session_scope() as s:
        v = s.exec(select(Visitor)).one()
        vid, uid = v.id, v.unifi_id
    client.post(f"/visitors/{vid}/cancel")
    assert uid not in fake.visitors
    with session_scope() as s:
        assert s.get(Visitor, vid).status == "cancelled"


def test_lobby_screen_checks_visitors_in(client, fake, monkeypatch):
    setup_admin(client)
    mail_on(monkeypatch)
    today = now_local()
    new_pass(client, date=today.date().isoformat(), start="00:01", end="23:58", send="")
    client.post("/screens", data={"kind": "lobby", "site_id": "1", "name": "Front iPad", "checkin": "true"})
    with session_scope() as s:
        token = s.exec(select(Screen)).one().token
        vid = s.exec(select(Visitor)).one().id
    client.post("/logout")  # screens need no sign-in
    page = client.get(f"/screen/{token}").text
    assert "Jane S." in page and "Smith" not in page and "Check in" in page
    r = client.post(f"/screen/{token}/checkin", data={"visitor": vid}, follow_redirects=True)
    assert "Thanks, Jane" in r.text
    with session_scope() as s:
        assert s.get(Visitor, vid).checked_in_at
    assert FakeSMTP.sent and "Jane Smith has arrived" in FakeSMTP.sent[-1]["Subject"]
    assert "doesn't work any more" in client.get("/screen/nope").text
    # a screen can't check in someone who isn't today's visitor at its site
    r = client.post(f"/screen/{token}/checkin", data={"visitor": 999}, follow_redirects=False)
    assert r.status_code == 303 and "done" not in r.headers["location"]


def test_room_screen_shows_free_or_in_use(client, fake):
    setup_admin(client)
    client.post("/screens", data={"kind": "room", "door_id": "1:demo-front", "name": "Board Room"})
    with session_scope() as s:
        token = s.exec(select(Screen)).one().token
    assert "Available" in client.get(f"/screen/{token}").text
    now = now_local().replace(second=0, microsecond=0)
    with session_scope() as s:
        m = Schedule(label="Budget review", kind="override", action="unlock", start_local=now - timedelta(minutes=10),
                     duration_minutes=60)
        s.add(m)
        s.flush()
        s.add(ScheduleDoor(schedule_id=m.id, door_id="1:demo-front"))
        s.commit()
    page = client.get(f"/screen/{token}").text
    assert "In use" in page and "Budget review" in page
    # managers can make a new link; the old one stops working
    sid = re.search(r'id="screen-(\d+)"', client.get("/screens").text).group(1)
    client.post(f"/screens/{sid}/newlink")
    assert "doesn't work any more" in client.get(f"/screen/{token}").text


def test_email_settings_presets_and_test(client, monkeypatch):
    setup_admin(client)
    FakeSMTP.sent = []
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    r = client.post("/settings/email", data={"preset": "m365", "username": "doors@example.com", "password": "pw",
                                             "sender": "doors@example.com", "test_to": "ada@example.com"},
                    follow_redirects=True)
    assert "test email was sent" in r.text
    with session_scope() as s:
        cfg = mailer.settings(s)
    assert cfg["host"] == "smtp.office365.com" and cfg["password"] == "pw"
    assert "Saved · leave blank" in client.get("/settings/email").text and "pw" not in re.findall(r'value="([^"]*)"', client.get("/settings/email").text)
