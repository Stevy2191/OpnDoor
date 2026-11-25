"""A holiday for a few sites (not all of them), each with all doors or only some."""
from datetime import date

from sqlmodel import select

from app import engine
from app.db import session_scope
from app.models import Door, Holiday, Site
from tests.test_web import client, setup_admin  # noqa: F401


def two_more_sites():
    with session_scope() as s:
        ids = []
        for name in ("Annex", "Warehouse"):
            st = Site(name=name, host="demo", timezone="UTC")
            s.add(st)
            s.flush()
            for n in ("Front", "Back"):
                s.add(Door(id=f"{st.id}:{n.lower()}", site_id=st.id, remote_id=n.lower(), name=f"{n} Door"))
            ids.append(st.id)
        s.commit()
        return ids


def test_holiday_for_two_of_three_sites(client):
    setup_admin(client)
    annex, warehouse = two_more_sites()
    today = date.today()
    r = client.post("/holidays", data={"name": "Inventory", "start": today.isoformat(), "mode": "closed",
                                       "sites": ["1", str(annex)], f"scope_{annex}": "some",
                                       "doors": [f"{annex}:back", f"{warehouse}:back"]}, follow_redirects=True)
    assert "Added Inventory" in r.text
    with session_scope() as s:
        h = s.exec(select(Holiday)).one()
        assert (h.site_id, h.site_ids, h.door_ids) == (None, f"1,{annex}", f"{annex}:back")  # other site's door ignored
        assert today in engine.holidays_for(s, 1)  # all doors at site 1
        assert today in engine.holidays_for(s, 1, "1:demo-front")
        assert today in engine.holidays_for(s, annex, f"{annex}:back")
        assert today not in engine.holidays_for(s, annex, f"{annex}:front")  # left out
        assert today not in engine.holidays_for(s, annex)  # not every door there
        assert today not in engine.holidays_for(s, warehouse, f"{warehouse}:back")  # site not ticked
    assert "All doors" in r.text and "Only Back Door" in r.text


def test_edit_keeps_sites_and_only_admins_pick_every_site(client):
    setup_admin(client)
    annex, warehouse = two_more_sites()
    today = date.today().isoformat()
    client.post("/holidays", data={"name": "Day", "start": today, "mode": "closed", "sites": [str(annex), str(warehouse)]})
    with session_scope() as s:
        hid = s.exec(select(Holiday)).one().id
    page = client.get(f"/holidays/{hid}/edit").text
    assert f'name="sites" value="{annex}" class="holsite" checked' in page
    assert 'name="sites" value="1" class="holsite" >' in page or 'value="1" class="holsite" ' in page
    client.post(f"/holidays/{hid}", data={"name": "Day", "start": today, "mode": "closed", "sites": [str(warehouse)]})
    with session_scope() as s:
        h = s.get(Holiday, hid)
        assert (h.site_id, h.site_ids) == (warehouse, "")
    client.post(f"/holidays/{hid}", data={"name": "Day", "start": today, "mode": "closed", "every": "1"})
    with session_scope() as s:
        h = s.get(Holiday, hid)
        assert (h.site_id, h.site_ids, h.sites) == (None, "", [])
    r = client.post(f"/holidays/{hid}", data={"name": "Day", "start": today, "mode": "closed", "where": "1"},
                    follow_redirects=True)
    assert "Tick at least one site" in r.text


def test_most_specific_holiday_wins():
    h_all = Holiday(name="All", start_date=date.today(), end_date=date.today(), hours="")
    h_two = Holiday(name="Two", site_ids="1,2", start_date=date.today(), end_date=date.today(), hours="09:00-12:00")
    rows = sorted([h_two, h_all], key=lambda h: (h.site_id is not None, bool(h.site_ids), bool(h.doors)))
    assert engine.holiday_map(rows, None, 1)[date.today()].name == "Two"
    assert h_two.for_site(2) and not h_two.for_site(3)
