"""Holidays, day-by-day regular hours, calendar approvals, PDF floor plans, crop and rotate."""
import io
from datetime import date, datetime, time, timedelta

from sqlmodel import select

from app import engine
from app.db import session_scope
from app.holidays import us_holidays
from app.models import CalendarFeed, Door, DoorPin, FloorPlan, Holiday, Schedule
from app.schedules import expand, holiday_changes, now_local, timeline
from tests.test_calendars import event, ics, run, setup_feed
from tests.test_maps import building_with_plan
from tests.test_web import client, setup_admin  # noqa: F401


def sched(**kw):
    base = dict(label="Office hours", kind="default", action="unlock", start_local=datetime(2030, 1, 1, 8, 0),
                duration_minutes=9 * 60, repeat_weekdays="0,1,2,3,4,5,6")
    base.update(kw)
    return Schedule(**base)


def test_holidays_skip_or_cut_repeating_schedules():
    xmas = date(2030, 12, 25)
    closed = Holiday(name="Christmas", start_date=xmas, end_date=xmas)
    afternoon = Holiday(name="Christmas", start_date=xmas, end_date=xmas, locked="12:00-23:59")
    lo, hi = datetime(2030, 12, 24), datetime(2030, 12, 27)
    office = sched(id=1)
    assert [o.start.date() for o in expand([office], lo, hi, {xmas: closed})] == [date(2030, 12, 24), date(2030, 12, 26)]
    # a holiday for part of the day: regular hours run until it starts
    occ = [o for o in expand([office], lo, hi, {xmas: afternoon}) if o.start.date() == xmas]
    assert [(o.start.time(), o.end.time()) for o in occ] == [(time(8), time(12))]
    # a schedule set to run on holidays, and one-off events, are not touched
    assert len(expand([sched(id=2, on_holidays="run")], lo, hi, {xmas: closed})) == 3
    party = sched(id=3, kind="override", repeat_weekdays="", start_local=datetime(2030, 12, 25, 18), duration_minutes=60)
    assert len(expand([party], lo, hi, {xmas: closed})) == 1
    # weekly events that follow holidays are cut the same way
    choir = sched(id=4, kind="override", start_local=datetime(2030, 1, 1, 19), duration_minutes=60)
    assert xmas not in [o.start.date() for o in expand([choir], lo, hi, {xmas: afternoon})]
    changes = holiday_changes([office, choir], lo, hi, {xmas: afternoon})
    assert [(c["o"].schedule.id, c["skipped"]) for c in changes] == [(1, False), (4, True)]


def test_holiday_keeps_doors_locked_in_the_timeline():
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("UTC")
    xmas = date(2030, 12, 25)
    eve = Holiday(id=9, name="Christmas Eve", start_date=xmas, end_date=xmas, locked="12:00-23:59")
    party = sched(id=3, kind="override", repeat_weekdays="", start_local=datetime(2030, 12, 25, 18), duration_minutes=60,
                  label="Party")
    segs = timeline([sched(id=1), party], datetime(2030, 12, 25), datetime(2030, 12, 26), tz, {xmas: eve})
    got = [(s.start.hour, s.end.hour if s.end.day == 25 else 24, s.state, s.label) for s in segs]
    assert got == [(0, 8, None, ""), (8, 12, "unlock", "Office hours"), (12, 18, "lock", "★ Christmas Eve"),
                   (18, 19, "unlock", "Party"), (19, 24, "lock", "★ Christmas Eve")]
    # with no schedules at all, the holiday still holds the door locked (over UniFi's own schedule)
    segs = timeline([], datetime(2030, 12, 25), datetime(2030, 12, 26), tz, {xmas: Holiday(name="X", start_date=xmas,
                                                                                                 end_date=xmas)})
    assert [(s.state, s.label) for s in segs] == [("lock", "★ X")]


def test_us_holidays():
    got = dict(us_holidays(2027))
    assert got["Thanksgiving"] == date(2027, 11, 25) and got["Memorial Day"] == date(2027, 5, 31)
    assert got["Labor Day"] == date(2027, 9, 6) and got["Martin Luther King Jr. Day"] == date(2027, 1, 18)


def test_holidays_page_and_engine(client, fake):
    setup_admin(client)
    today = now_local().date()
    client.post("/schedules", data={"mode": "default", "label": "Office hours", "door_ids": ["1:demo-front"],
                                    "day": today.isoformat(), "start": "00:00", "end": "23:59",
                                    "weekdays": [str(i) for i in range(7)], "holidays_field": "1", "follow_holidays": "1"})
    engine.reconcile()
    assert ("unlock", "demo-front") in [c[:2] for c in fake.calls]
    r = client.post("/holidays", data={"name": "Snow day", "start": today.isoformat(), "mode": "closed"},
                    follow_redirects=True)
    assert "Added Snow day" in r.text and "Locked all day" in r.text
    engine.reconcile()
    # regular hours don't run today: the door goes back to its normal UniFi rule (locked, badges work).
    # A holiday never sends keep_lock: UniFi treats that as a lockdown and refuses badges.
    assert fake.calls[-1] == ("reset", "demo-front")
    assert not [c for c in fake.calls if c[0] == "lock"]
    r = client.post("/holidays/us", data={"year": "2031", "names": ["Thanksgiving", "Christmas Day"]}, follow_redirects=True)
    assert "Added 2 holidays for 2031" in r.text
    assert "already there" in client.post("/holidays/us", data={"year": "2031", "names": ["Thanksgiving"]},
                                          follow_redirects=True).text
    with session_scope() as s:
        hid = s.exec(select(Holiday).where(Holiday.name == "Snow day")).one().id
    client.post(f"/holidays/{hid}/delete")
    engine.reconcile()
    assert fake.calls[-1][:2] == ("unlock", "demo-front")
    assert "added the holiday" in client.get("/history").text


def test_different_hours_on_different_days(client):
    setup_admin(client)
    starts = ["08:00", "08:00", "08:00", "08:00", "08:00", "09:00", "08:00"]
    ends = ["17:00", "17:00", "17:00", "17:00", "12:00", "13:00", "17:00"]
    r = client.post("/schedules", data={"mode": "default", "label": "Office hours", "door_ids": ["1:demo-front"],
                                        "day": date.today().isoformat(), "start": "08:00", "end": "17:00",
                                        "perday": "1", "pd_on": ["0", "1", "2", "3", "4", "5"],
                                        "pd_start": starts, "pd_end": ends}, follow_redirects=True)
    assert "3 sets of hours" in r.text
    with session_scope() as s:
        got = {(x.repeat_weekdays, x.start_local.strftime("%H:%M"), x.duration_minutes)
               for x in s.exec(select(Schedule)).all()}
    assert got == {("0,1,2,3", "08:00", 540), ("4", "08:00", 240), ("5", "09:00", 240)}


def test_calendar_approvals(client):
    setup_admin(client)
    fid = setup_feed()
    with session_scope() as s:
        f = s.get(CalendarFeed, fid)
        f.approve_after = "22:00"
        s.add(f)
        s.commit()
    day = (datetime.now() + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    run(fid, ics(event("a", "Choir", day.replace(hour=18), location="Hall"),
                 event("b", "Lock-in", day.replace(hour=21), hours=5, location="Hall")))
    with session_scope() as s:
        st = {x.label.split(" ·")[0]: (x.status, x.status_note) for x in s.exec(select(Schedule)).all()}
    assert st["Choir"] == ("", "") and st["Lock-in"][0] == "pending" and "10 PM" in st["Lock-in"][1]
    with session_scope() as s:
        live = {x.label.split(" ·")[0] for v in engine.door_schedules(s).values() for x in v}
    assert live == {"Choir"}  # waiting events unlock nothing
    page = client.get("/calendars").text
    assert "Needs approval" in page and "Lock-in" in page and "to approve" in client.get("/home").text
    with session_scope() as s:
        lid = s.exec(select(Schedule).where(Schedule.status == "pending")).one().id
    client.post("/calendars/approve", data={"ids": [lid], "action": "skip"})
    with session_scope() as s:
        assert s.get(Schedule, lid).status == "skipped"
    client.post("/calendars/approve", data={"ids": [lid], "action": "approve"})
    with session_scope() as s:
        assert s.get(Schedule, lid).status == ""
    # new and changed events wait when the calendar asks for that
    with session_scope() as s:
        f = s.get(CalendarFeed, fid)
        f.approve_new = True
        s.add(f)
        s.commit()
    run(fid, ics(event("a", "Choir", day.replace(hour=19), location="Hall"),
                 event("b", "Lock-in", day.replace(hour=21), hours=5, location="Hall")))
    with session_scope() as s:
        choir = [x for x in s.exec(select(Schedule)).all() if x.label.startswith("Choir")][0]
        assert choir.status == "pending" and "changed" in choir.status_note.lower()


def _pdf_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (400, 200), "white").save(buf, "PDF")
    return buf.getvalue()


def test_pdf_plan_crop_and_rotate(client):
    from PIL import Image
    from app.maps import plan_dir
    bid, _ = building_with_plan(client)
    r = client.post("/floorplans", data={"building_id": str(bid), "name": "Upstairs"},
                    files={"image": ("plan.pdf", _pdf_bytes(), "application/pdf")}, follow_redirects=False)
    assert r.headers["location"].endswith("/edit")
    pid = int(r.headers["location"].split("/")[2])
    client.get(r.headers["location"])
    with session_scope() as s:
        plan = s.get(FloorPlan, pid)
        assert plan.filename.endswith(".png")
        w, h = Image.open(plan_dir() / plan.filename).size
    assert w > h  # the PDF page, landscape
    client.post(f"/floorplans/{pid}/pins", json={"door_id": "1:demo-front", "x": 60, "y": 50})
    client.post(f"/floorplans/{pid}/pins", json={"door_id": "1:demo-side", "x": 5, "y": 5})
    r = client.post(f"/floorplans/{pid}/crop", data={"x": "50", "y": "25", "w": "50", "h": "50"}, follow_redirects=True)
    assert "1 door was outside the box" in r.text
    with session_scope() as s:
        plan = s.get(FloorPlan, pid)
        assert Image.open(plan_dir() / plan.filename).size == (round(w / 2), round(h / 2))
        pin = s.get(DoorPin, "1:demo-front")
        assert (round(pin.x), round(pin.y)) == (20, 50) and s.get(DoorPin, "1:demo-side") is None
    client.post(f"/floorplans/{pid}/rotate", data={"turn": "right"})
    with session_scope() as s:
        plan = s.get(FloorPlan, pid)
        assert Image.open(plan_dir() / plan.filename).size == (round(h / 2), round(w / 2))
        pin = s.get(DoorPin, "1:demo-front")
        assert (round(pin.x), round(pin.y)) == (50, 20)
    assert "PDF couldn" in client.post(
        "/floorplans", data={"building_id": str(bid)}, files={"image": ("x.pdf", b"%PDF-1.4 broken", "application/pdf")},
        follow_redirects=True).text


def test_cropped_plan_gets_a_new_image_address(client):
    """Browsers keep plan images for a day, so the address must change when the picture does."""
    import re
    bid, pid = building_with_plan(client)
    before = re.search(r'/floorplans/%d/image\?v=[0-9a-f]+' % pid, client.get(f"/floorplans/{pid}/edit").text).group(0)
    client.post(f"/floorplans/{pid}/crop", data={"x": "10", "y": "10", "w": "50", "h": "50"})
    after_edit = client.get(f"/floorplans/{pid}/edit").text
    after_map = client.get(f"/map?plan={pid}").text
    assert before not in after_edit and before not in after_map
    assert re.search(r'/floorplans/%d/image\?v=[0-9a-f]+' % pid, after_map)


def test_doors_a_holiday_left_in_lockdown_go_back_to_normal(client, fake):
    """Before this fix a holiday sent keep_lock (a per-door lockdown). After the update those doors are reset."""
    from app.models import DoorState, utcnow
    setup_admin(client)
    client.post("/holidays", data={"name": "Thanksgiving", "start": now_local().date().isoformat(), "mode": "allday"})
    with session_scope() as s:
        s.add(DoorState(door_id="1:demo-front", mode="lock", until=utcnow() + timedelta(hours=5), label="★ Thanksgiving"))
        s.commit()
    engine.reconcile()
    assert ("reset", "demo-front") in fake.calls and not [c for c in fake.calls if c[0] == "lock"]
    with session_scope() as s:
        assert s.get(DoorState, "1:demo-front").mode == ""
