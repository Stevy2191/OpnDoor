import struct
import zlib

from sqlmodel import select

from app.db import session_scope
from app.maps import plan_dir
from app.models import Building, DoorPin, FloorPlan
from tests.test_web import client, setup_admin  # noqa: F401


def png(w=40, h=30):
    raw = b"".join(b"\x00" + b"\xcc" * (w * 3) for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def building_with_plan(c):
    setup_admin(c)
    c.post("/buildings", data={"site": "1", "new_buildings": "Main Hall"})
    with session_scope() as s:
        bid = s.exec(select(Building)).first().id
    c.post("/buildings", data={"site": "1", "door_1:demo-front": str(bid), "door_1:demo-side": str(bid),
                               "door_1:demo-hall": str(bid), f"name_{bid}": "Main Hall"})
    r = c.post("/floorplans", data={"building_id": str(bid), "name": "Ground floor"},
               files={"image": ("plan.png", png(), "image/png")}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/edit")
    pid = int(r.headers["location"].split("/")[2])
    c.get(r.headers["location"])  # show the "Added" message, like a browser would
    return bid, pid


def test_upload_place_and_view(client):
    bid, pid = building_with_plan(client)
    assert "Ground floor" in client.get("/sites/1").text
    edit = client.get(f"/floorplans/{pid}/edit").text
    assert "Front Entrance" in edit and "Not placed" in edit
    assert client.get(f"/floorplans/{pid}/image").content.startswith(b"\x89PNG")

    assert client.post(f"/floorplans/{pid}/pins", json={"door_id": "1:demo-front", "x": 25, "y": 140}).json()["ok"]
    client.post(f"/floorplans/{pid}/pins", json={"door_id": "1:demo-side", "x": 70, "y": 50})
    with session_scope() as s:
        assert s.get(DoorPin, "1:demo-front").y == 100  # clamped onto the plan
    page = client.get(f"/map?plan={pid}").text
    assert 'data-id="1:demo-front"' in page and 'data-id="1:demo-side"' in page
    assert "Not on the map yet: Main Hall" in page or "Main Hall" in page

    # moving and removing pins
    client.post(f"/floorplans/{pid}/pins", json={"door_id": "1:demo-front", "x": 10, "y": 10})
    client.post(f"/floorplans/{pid}/pins", json={"door_id": "1:demo-side", "remove": True})
    with session_scope() as s:
        assert s.get(DoorPin, "1:demo-front").x == 10 and s.get(DoorPin, "1:demo-side") is None
    assert "Changed a floor plan" in client.get("/history").text or "floor plan" in client.get("/history").text

    # quick unlock from the map comes back to the map
    r = client.post("/quick-unlock", data={"door_ids": "1:demo-front", "minutes": "15", "next": f"/map?plan={pid}"},
                    follow_redirects=False)
    assert r.headers["location"] == f"/map?plan={pid}"
    assert 'class="pin open"' in client.get(f"/map?plan={pid}").text


def test_rejects_non_images(client):
    bid, _ = building_with_plan(client)
    r = client.post("/floorplans", data={"building_id": str(bid), "name": "Bad"},
                    files={"image": ("x.svg", b"<svg onload=alert(1)></svg>", "image/svg+xml")}, follow_redirects=True)
    assert "PNG, JPG or WebP" in r.text
    with session_scope() as s:
        assert len(s.exec(select(FloorPlan)).all()) == 1


def test_operator_sees_only_their_pins(client):
    bid, pid = building_with_plan(client)
    for door, x in [("1:demo-front", 20), ("1:demo-side", 60)]:
        client.post(f"/floorplans/{pid}/pins", json={"door_id": door, "x": x, "y": 50})
    client.post("/users", data={"name": "Bob", "email": "bob@example.com", "password": "password123",
                                "door_ids": ["1:demo-side"]})
    client.post("/logout")
    client.post("/login", data={"email": "bob@example.com", "password": "password123"})
    page = client.get("/map").text
    assert 'data-id="1:demo-side"' in page and 'data-id="1:demo-front"' not in page
    assert "Place doors" not in page
    assert client.get(f"/floorplans/{pid}/image").status_code == 200
    assert client.get(f"/floorplans/{pid}/edit").status_code in (302, 303, 403, 404)
    r = client.post(f"/floorplans/{pid}/pins", json={"door_id": "1:demo-side", "x": 1, "y": 1})
    assert r.status_code in (403, 404)


def test_removing_building_or_site_cleans_up(client):
    bid, pid = building_with_plan(client)
    client.post(f"/floorplans/{pid}/pins", json={"door_id": "1:demo-front", "x": 20, "y": 20})
    with session_scope() as s:
        fname = s.get(FloorPlan, pid).filename
    assert (plan_dir() / fname).exists()
    client.post("/buildings", data={"site": "1", f"delete_{bid}": "on", f"name_{bid}": "Main Hall"})
    with session_scope() as s:
        assert not s.exec(select(FloorPlan)).all() and not s.exec(select(DoorPin)).all()
    assert not (plan_dir() / fname).exists()

    bid, pid = building_with_plan_again(client)
    client.post(f"/floorplans/{pid}/pins", json={"door_id": "1:demo-front", "x": 20, "y": 20})
    client.post("/sites/1/delete")
    with session_scope() as s:
        assert not s.exec(select(FloorPlan)).all() and not s.exec(select(DoorPin)).all()


def building_with_plan_again(c):
    c.post("/buildings", data={"site": "1", "new_buildings": "Annex"})
    with session_scope() as s:
        bid = s.exec(select(Building)).first().id
    r = c.post("/floorplans", data={"building_id": str(bid), "name": "1st"},
               files={"image": ("p.png", png(), "image/png")}, follow_redirects=False)
    return bid, int(r.headers["location"].split("/")[2])
