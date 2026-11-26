"""Reports to download: pick a type, then download Excel or CSV."""
import io
import zipfile
from datetime import timedelta

from app import whocan
from app.downloads import KINDS
from app.schedules import now_local
from tests.test_access import unifi_people
from tests.test_web import client, setup_admin  # noqa: F401


def test_new_report_lists_types(client):
    setup_admin(client)
    page = client.get("/reports/new").text
    assert "What kind of report?" in page and "Email summary" in page
    for k, (title, _, _) in KINDS.items():
        assert title in page
        assert client.get(f"/reports/new/{k}").status_code == 200
    assert "New email summary" in client.get("/reports/new/summary").text
    assert client.get("/reports/access", follow_redirects=False).headers["location"] == "/reports/new/access"


def test_access_report_csv_and_excel(client, fake):
    setup_admin(client)
    unifi_people(fake)
    whocan._cache.clear()
    r = client.get("/reports/download/access?door=1:demo-hall&door=1:demo-side&fmt=csv&unifi=1&app=1")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    text = r.content.decode("utf-8-sig")
    assert text.startswith("Site,Building,Door,Person")
    assert "Fellowship Hall,Uma,UMA@example.com,UniFi Access,Staff,Any time" in text.replace(",,", ",")
    assert "Ada Admin" in text and "Main admin" in text
    x = client.get("/reports/download/access?door=1:demo-hall&fmt=xlsx&unifi=1")
    sheet = zipfile.ZipFile(io.BytesIO(x.content)).read("xl/worksheets/sheet1.xml").decode()
    assert "Uma" in sheet and "Ada Admin" not in sheet  # OpnDoor people not ticked
    assert "Pick at least one door" in client.get("/reports/download/access?fmt=csv&unifi=1", follow_redirects=True).text


def test_every_report_downloads(client, fake):
    setup_admin(client)
    day = (now_local() + timedelta(days=1)).date().isoformat()
    client.post("/schedules", data={"mode": "unlock", "label": "Rodeo", "door_ids": ["1:demo-front"], "day": day,
                                    "start": "17:00", "end": "22:00"})
    client.post("/holidays", data={"name": "Snow day", "start": day, "mode": "closed", "site": "1"})
    for k in KINDS:
        if k == "access":
            continue
        for fmt in ("csv", "xlsx"):
            r = client.get(f"/reports/download/{k}?fmt={fmt}")
            assert r.status_code == 200, k
            assert "attachment" in r.headers["content-disposition"], k
    text = client.get("/reports/download/schedules?fmt=csv").content.decode("utf-8-sig")
    assert "Rodeo" in text and "Front Entrance" in text
    assert "Snow day" in client.get("/reports/download/holidays?fmt=csv").content.decode("utf-8-sig")
    assert "Ada Admin" in client.get("/reports/download/people?fmt=csv").content.decode("utf-8-sig")
    assert "downloaded the" in client.get("/history").text


def test_summary_downloads(client):
    setup_admin(client)
    client.post("/reports/save", data={"frequency": "weekly", "sections": ["activity"], "users": ["1"], "enabled": "1",
                                       "name": "Weekly"})
    r = client.get("/reports/1/download")
    assert r.status_code == 200 and ".html" in r.headers["content-disposition"] and "<html" in r.text.lower()


def test_formula_cells_are_defused():
    from app.accessreport import _safe
    assert _safe("=HYPERLINK(1)") == "'=HYPERLINK(1)" and _safe("Jo") == "Jo"
