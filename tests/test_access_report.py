"""Door access report: CSV and Excel downloads."""
import io
import zipfile

from app import whocan
from tests.test_access import unifi_people
from tests.test_web import client, setup_admin  # noqa: F401


def test_access_report_csv_and_excel(client, fake):
    setup_admin(client)
    unifi_people(fake)
    whocan._cache.clear()
    page = client.get("/reports/access")
    assert page.status_code == 200 and "Door access report" in page.text
    r = client.get("/reports/access/download?door=1:demo-hall&door=1:demo-side&fmt=csv&unifi=1&app=1")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    text = r.content.decode("utf-8-sig")
    assert text.startswith("Site,Building,Door,Person")
    assert "Fellowship Hall,Uma,UMA@example.com,UniFi Access,Staff,Any time" in text.replace(",,", ",")
    assert "Ada Admin" in text and "Main admin" in text
    x = client.get("/reports/access/download?door=1:demo-hall&fmt=xlsx&unifi=1")
    z = zipfile.ZipFile(io.BytesIO(x.content))
    sheet = z.read("xl/worksheets/sheet1.xml").decode()
    assert "Uma" in sheet and "Ada Admin" not in sheet  # OpnDoor people not ticked
    assert "door access report" in client.get("/history").text
    # nothing picked
    assert "Pick at least one door" in client.get("/reports/access/download?fmt=csv&unifi=1", follow_redirects=True).text


def test_formula_cells_are_defused():
    from app.accessreport import _safe
    assert _safe("=HYPERLINK(1)") == "'=HYPERLINK(1)" and _safe("Jo") == "Jo"
