"""Branding, backups, updates, devices and people in UniFi Access."""
import io
import zipfile

from PIL import Image
from sqlmodel import select

from app import admin, config
from app.db import session_scope
from app.models import Site
from tests.test_web import client, setup_admin  # noqa: F401


def png(color=(200, 30, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (300, 80), color).save(out, "PNG")
    return out.getvalue()


def test_branding_logo_name_and_colours(client):
    setup_admin(client)
    r = client.post("/settings/branding", data={"name": "Station 2", "accent": "#aa0000", "nav": "#101010"},
                    files={"logo": ("logo.png", png(), "image/png")}, follow_redirects=True)
    assert "Branding saved" in r.text and "Station 2" in r.text and "--accent: #aa0000" in r.text
    assert ":root:not(#_)" in r.text  # beats the dark-mode colours too
    assert client.get("/brand/logo").headers["content-type"] == "image/png"
    client.post("/logout")
    assert "/brand/logo" in client.get("/login").text  # the sign-in page shows it too
    setup_admin  # noqa: B018


def test_branding_rejects_bad_files_and_colours(client):
    setup_admin(client)
    r = client.post("/settings/branding", data={"name": "", "accent": "red;}body{x", "nav": ""},
                    files={"logo": ("x.png", b"not a picture", "image/png")}, follow_redirects=True)
    assert "a picture OpnDoor can read" in r.text and "red;}" not in r.text


def test_backup_download_and_restore(client, monkeypatch, tmp_path):
    setup_admin(client)
    db = tmp_path / "db.sqlite"
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setattr(admin, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(admin, "RESTORE_FILE", tmp_path / "restore-pending.zip")
    r = client.post("/settings/backups/now", follow_redirects=True)
    assert "Backup made" in r.text
    name = admin.list_backups()[0]["name"]
    data = client.get(f"/settings/backups/{name}").content
    assert "opndoor.db" in zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert admin.check_backup(data) == ""
    assert admin.check_backup(b"nope")
    assert client.get("/settings/backups/..%2Fsecret_key").status_code == 404
    monkeypatch.setattr(admin, "restart_soon", lambda: None)
    r = client.post("/settings/restore", files={"file": ("b.zip", data, "application/zip")})
    assert "Restoring the backup" in r.text and (tmp_path / "restore-pending.zip").exists()
    with session_scope() as s:  # rename the site, then restoring brings the old name back
        site = s.exec(select(Site)).first()
        old = site.name
    target = tmp_path / "target.sqlite"  # restore into a separate file, not the database the tests use
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{target}")
    admin.apply_pending_restore()
    assert not (tmp_path / "restore-pending.zip").exists()
    import sqlite3
    with sqlite3.connect(target) as c:
        assert c.execute("select name from site").fetchone()[0] == old
    assert any("before-restore" in b["name"] for b in admin.list_backups())


def test_update_button_needs_the_helper(client, monkeypatch, tmp_path):
    setup_admin(client)
    for k in ("UPDATE_REQUEST", "UPDATE_STATUS", "UPDATER_ALIVE"):
        monkeypatch.setattr(admin, k, tmp_path / k)
    monkeypatch.setattr(admin, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(admin, "latest", lambda: None)
    assert "run the installer again" in client.get("/settings/updates").text
    r = client.post("/settings/updates/now", follow_redirects=True)
    assert "helper isn" in r.text and not (tmp_path / "UPDATE_REQUEST").exists()
    (tmp_path / "UPDATER_ALIVE").write_text("")
    (tmp_path / "UPDATE_STATUS").write_text("current 2026-11-22 10:00\n")
    page = client.get("/settings/updates").text
    assert "Update now" in page and "Already up to date" in page
    assert "Checking for an update" in client.post("/settings/updates/now").text
    assert (tmp_path / "UPDATE_REQUEST").exists()


def test_devices_page_flags_offline(client, fake):
    setup_admin(client)
    page = client.get("/devices").text
    assert "Access Hub" in page and "Side Door Reader" in page and "offline" in page


def test_unifi_people_new_pin_and_turn_off(client, fake):
    setup_admin(client)
    fake.users = [{"id": "u1", "first_name": "Kim", "last_name": "Diaz", "user_email": "kim@example.com",
                   "status": "ACTIVE", "pin_code": None, "nfc_cards": [{"id": "c"}]}]
    page = client.get("/unifi-people").text
    assert "Kim Diaz" in page and "1 card" in page
    r = client.post("/unifi-people/1/u1/pin", follow_redirects=True)
    pin = fake.users[0]["pin_code"]["token"]
    assert pin in r.text and "only time" in r.text
    assert pin not in client.get("/unifi-people").text  # shown once
    r = client.post("/unifi-people/1/u1/active", follow_redirects=True)
    assert fake.users[0]["status"] == "DEACTIVATED" and "Turn back on" in r.text
    client.post("/unifi-people/1/u1/active", data={"active": "true"})
    assert fake.users[0]["status"] == "ACTIVE"


def test_devices_raw_is_for_admins(client, fake):
    setup_admin(client)
    data = client.get("/devices/raw?site=1").json()
    assert data and data[0][0]["name"] == "Access Hub"


def test_devices_use_door_names_and_skip_not_adopted(client, fake, monkeypatch):
    """From a real console: an unnamed reader, and a viewer UniFi lists as not adopted."""
    from app import unifi
    raw = [[{"alias": "Lobby Viewer", "id": "v1", "is_adopted": False, "is_online": False,
             "location_id": "x", "name": "UA Intercom Viewer 4e5f", "type": "UA-Int-Viewer"}],
           [{"alias": "", "id": "r1", "is_adopted": True, "is_online": True, "location_id": "demo-front",
             "name": "UA-G2-7075", "type": "UA-G2-MINI"}],
           [{"alias": "UA Int Viewer 9999", "id": "v2", "is_adopted": True, "is_online": True,
             "location_id": "x", "name": "UA-Int-Viewer-9999", "type": "UA-Int-Viewer"}]]
    real = unifi.UnifiClient.__new__(unifi.UnifiClient)
    real._call = lambda method, path, **kw: raw
    monkeypatch.setattr(fake, "list_devices", real.list_devices)
    from app import devices
    devices._cache.clear()
    setup_admin(client)
    page = client.get("/devices").text
    assert "Front Entrance · G2 Mini reader" in page and "UA-G2-7075" in page
    assert "Status not available from UniFi" in page and "offline</b>" not in page
    assert "isn&#39;t adopted" not in page


def test_unifi_network_decides_online(client, fake, monkeypatch):
    """Access's API calls an adopted, online viewer offline; UniFi Network sees it connected, so it's online."""
    import httpx
    from app import devices, network, unifi
    raw = [[{"alias": "Lobby Viewer", "id": "0a1b2c3d4e5f", "is_adopted": False, "is_online": False,
             "location_id": "x", "name": "UA Intercom Viewer 4e5f", "type": "UA-Int-Viewer"}],
           [{"alias": "UA Int Viewer 9999", "id": "0a1b2c3d9999", "is_adopted": True, "is_online": True,
             "location_id": "x", "name": "UA-Int-Viewer-9999", "type": "UA-Int-Viewer"}]]
    real = unifi.UnifiClient.__new__(unifi.UnifiClient)
    real._call = lambda method, path, **kw: raw
    monkeypatch.setattr(fake, "list_devices", real.list_devices)
    seen = []

    def fake_get(url, params=None, headers=None, **kw):
        seen.append((url, headers.get("X-API-KEY")))
        if url.endswith("/sites"):
            body = {"data": [{"id": "s1", "name": "Default"}]}
        else:
            body = {"data": [{"macAddress": "0a:1b:2c:3d:4e:5f", "ipAddress": "192.168.20.49", "name": "viewer"}],
                    "totalCount": 1}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))
    monkeypatch.setattr(network.httpx, "get", fake_get)
    setup_admin(client)
    with session_scope() as s:
        site = s.get(Site, 1)
        site.network_on, site.network_key, site.network_host = True, "netkey", "192.168.1.1"
        s.add(site)
        s.commit()
    devices._cache.clear()
    network._cache.clear()
    page = client.get("/devices").text
    assert "Checked with UniFi Network" in page and "192.168.20.49" in page
    sholl = page.split("Lobby Viewer")[1].split("</div>")[0]
    cecc = page.split("UA Int Viewer 9999")[1].split("</div>")[0]
    assert "offline" not in sholl and "offline" in cecc
    assert seen[0] == ("https://192.168.1.1/proxy/network/integration/v1/sites", "netkey")
