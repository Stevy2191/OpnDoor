"""Security headers, refusing other sites' posts, sign-in throttling and safe calendar fetches."""
import pytest

from app import security
from tests.test_web import client, setup_admin  # noqa: F401


def test_every_page_has_security_headers(client):
    r = client.get("/login")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_posts_from_other_sites_are_refused(client):
    setup_admin(client)
    evil = client.post("/quick-unlock", data={"door_ids": "1:demo-front", "mode": "open"},
                       headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403 and "another website" in evil.text
    ok = client.post("/quick-unlock", data={"door_ids": "1:demo-front", "mode": "open", "next": "/"},
                     headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert ok.status_code in (200, 303)
    # behind a proxy that drops the port from Host, or sets X-Forwarded-Host
    assert security.same_site({"origin": "https://10.245.10.25:8443", "host": "10.245.10.25"})
    assert security.same_site({"origin": "https://door.example.com", "host": "opndoor:8080",
                               "x-forwarded-host": "door.example.com"})
    assert not security.same_site({"referer": "https://evil.example/x", "host": "door.example.com"})


def test_sign_in_is_throttled(client):
    setup_admin(client)
    client.post("/logout")
    security._fails.clear()
    for _ in range(5):
        client.post("/login", data={"email": "ada@example.com", "password": "wrong-password"})
    r = client.post("/login", data={"email": "ada@example.com", "password": "password123"}, follow_redirects=True)
    assert "Too many wrong tries" in r.text  # even the right password waits now
    security._fails.clear()
    r = client.post("/login", data={"email": "ada@example.com", "password": "password123"}, follow_redirects=True)
    assert "Too many" not in r.text and "Sign out" in r.text


@pytest.mark.parametrize("url", ["http://localhost:1984/api", "http://127.0.0.1/", "http://169.254.169.254/latest",
                                 "http://go2rtc:1984/", "file:///etc/passwd", "ftp://x.example/cal.ics"])
def test_calendar_links_cannot_reach_inside_the_server(url):
    with pytest.raises(security.FetchError):
        security.fetch(url)


def test_email_headers_are_one_line(monkeypatch):
    from app import mailer
    from tests.test_visitors import FakeSMTP
    FakeSMTP.sent = []
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    mailer.send({"host": "h", "sender": "a@example.com", "security": "none"}, "b@example.com\nBcc: x@evil.example",
                "Hi\r\nBcc: x@evil.example", "body")
    msg = FakeSMTP.sent[-1]
    assert "\n" not in msg["Subject"] and msg["Bcc"] is None
