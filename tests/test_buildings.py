import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import session_scope
from app.main import app
from app.models import Building, Door, Site


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.post("/setup", data={"name": "Ada", "email": "ada@example.com", "password": "password123"})
        yield c


def fairgrounds():
    """A site whose UniFi names are 'Fairgrounds - <building> - <door>'."""
    layout = {"Event Center": ["North", "South", "East", "West"], "Sales Building": ["Front", "Back", "Office", "Dock"],
              "VIP Building": ["Entrance", "Lounge", "Patio"]}
    with session_scope() as s:
        site = Site(name="Fairgrounds", host="demo", timezone="America/Chicago")
        s.add(site)
        s.flush()
        for b, names in layout.items():
            for n in names:
                rid = f"{b[:3]}-{n}".lower()
                s.add(Door(id=f"{site.id}:{rid}", site_id=site.id, remote_id=rid, name=f"{n} Door",
                           full_name=f"Fairgrounds - {b} - {n} Door"))
        s.add(Door(id=f"{site.id}:gate", site_id=site.id, remote_id="gate", name="Main Gate", full_name="Main Gate"))
        s.commit()
        return site.id


def test_auto_group_from_unifi_names_and_pickers(client):
    sid = fairgrounds()
    page = client.get(f"/buildings?site={sid}").text
    assert "Group by UniFi floor name" in page
    r = client.post("/buildings/auto", data={"site": str(sid), "part": "1"})
    assert "Put 11 doors into buildings" in r.text
    with session_scope() as s:
        names = sorted(b.name for b in s.exec(select(Building).where(Building.site_id == sid)).all())
        assert names == ["Event Center", "Sales Building", "VIP Building"]
        assert s.get(Door, f"{sid}:gate").building_id is None  # no building in its UniFi name

    form = client.get(f"/schedules/new?door={sid}:gate").text
    assert 'data-name="Event Center"' in form and 'data-name="VIP Building"' in form and "Other doors" in form
    assert "Pick doors" in form and "4 doors" in form
    doors = client.get(f"/site?id={sid}&next=/doors").text
    assert "Event Center" in doors and "Organize buildings" in doors
    assert "VIP Building" in client.get("/").text  # unlock dialog groups by building

    # a schedule on a whole building is just those doors
    ec = [f"{sid}:eve-{n}" for n in ("north", "south", "east", "west")]
    client.post("/schedules", data={"label": "Fair week", "door_ids": ec, "day": "2030-08-01",
                                    "start": "08:00", "end": "20:00"})
    assert "Fair week" in client.get(f"/doors/{ec[0]}").text


def test_manual_buildings_rename_move_remove(client):
    sid = fairgrounds()
    client.post("/buildings", data={"site": str(sid), "new_buildings": "Grandstand\nBarns"})
    with session_scope() as s:
        b = {x.name: x.id for x in s.exec(select(Building)).all()}
    client.post("/buildings", data={"site": str(sid), f"name_{b['Barns']}": "Horse Barns",
                                    f"door_{sid}:gate": str(b["Grandstand"])})
    with session_scope() as s:
        assert s.get(Door, f"{sid}:gate").building_id == b["Grandstand"]
        assert s.get(Building, b["Barns"]).name == "Horse Barns"
    assert "changed buildings at Fairgrounds" in client.get("/history").text
    client.post("/buildings", data={"site": str(sid), f"delete_{b['Grandstand']}": "on",
                                    f"name_{b['Grandstand']}": "Grandstand"})
    with session_scope() as s:
        assert s.get(Building, b["Grandstand"]) is None
        assert s.get(Door, f"{sid}:gate").building_id is None  # door kept, just ungrouped

    # removing the site removes its buildings too
    assert "Removed Fairgrounds" in client.post(f"/sites/{sid}/delete").text
    with session_scope() as s:
        assert not s.exec(select(Building)).all()


def test_site_admin_manages_only_their_buildings(client):
    sid = fairgrounds()
    client.post("/users", data={"name": "Sid", "email": "sid@example.com", "role": "site_admin",
                                "signin": "password", "password": "password123", "site_ids": ["1"]})
    client.post("/logout")
    client.post("/login", data={"email": "sid@example.com", "password": "password123"})
    page = client.get(f"/buildings?site={sid}").text
    assert "Fairgrounds" not in page  # falls back to their own site
    client.post("/buildings", data={"site": str(sid), "new_buildings": "Sneaky"})
    with session_scope() as s:
        assert not s.exec(select(Building).where(Building.name == "Sneaky", Building.site_id == sid)).first()


def test_site_page_tabs_and_comma_separated_buildings(client):
    page = client.get("/sites/1").text
    for tab in ('data-tab="buildings"', 'data-tab="floorplans"', 'data-tab="console"', 'data-panel="console"'):
        assert tab in page
    client.post("/buildings", data={"site": "1", "new_buildings": "North Hall, South Hall"})
    with session_scope() as s:
        names = {b.name for b in s.exec(select(Building).where(Building.site_id == 1)).all()}
    assert {"North Hall", "South Hall"} <= names
