"""Holidays that come back every year."""
from datetime import date

from sqlmodel import select

from app import engine
from app.db import session_scope
from app.models import Holiday
from tests.test_web import client, setup_admin  # noqa: F401


def test_rules_work_out_each_year():
    thanks = Holiday(name="Thanksgiving", start_date=date(2026, 11, 26), end_date=date(2026, 11, 27), yearly="nth")
    assert thanks.date_in(2027) == date(2027, 11, 25)  # 4th Thursday
    assert (date(2027, 11, 25), date(2027, 11, 26)) in thanks.spans(2027)  # keeps its 2 days
    memorial = Holiday(name="Memorial", start_date=date(2026, 5, 25), end_date=date(2026, 5, 25), yearly="last")
    assert memorial.date_in(2027) == date(2027, 5, 31)
    xmas = Holiday(name="Christmas", start_date=date(2026, 12, 25), end_date=date(2026, 12, 25), yearly="date")
    assert xmas.date_in(2030) == date(2030, 12, 25)
    after = Holiday(name="Day after Thanksgiving", start_date=date(2026, 11, 27), end_date=date(2026, 11, 27),
                    yearly="us:Day after Thanksgiving")
    assert after.date_in(2030) == date(2030, 11, 29)  # Thanksgiving 2030 is the 28th
    assert "4th Thursday of November" in thanks.repeat_text()


def test_yearly_holiday_counts_next_year_and_lists_next_date(client):
    setup_admin(client)
    this = date.today().year
    r = client.post("/holidays", data={"name": "Founders Day", "start": f"{this - 1}-03-02", "mode": "closed",
                                       "site": "1", "yearly": "date"}, follow_redirects=True)
    assert "Founders Day" in r.text and "Every year on March 2" in r.text
    with session_scope() as s:
        hols = engine.holidays_for(s, 1)
        assert date(this + 1, 3, 2) in hols and date(this - 1, 3, 2) in hols


def test_us_holidays_every_year(client):
    setup_admin(client)
    year = date.today().year
    client.post("/holidays/us", data={"year": str(year), "names": ["Thanksgiving"], "site": "", "every_year": "1"})
    with session_scope() as s:
        h = s.exec(select(Holiday)).one()
        assert h.yearly == "us:Thanksgiving"
        from app.holidays import us_holidays
        nxt = dict(us_holidays(year + 1))["Thanksgiving"]
        assert nxt in engine.holidays_for(s, 1)
    # editing keeps the US rule
    r = client.get(f"/holidays/{h.id}/edit").text
    assert 'value="keep" selected' in r
