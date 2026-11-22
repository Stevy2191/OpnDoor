"""Holidays: days when repeating schedules that follow holidays don't run, or run shorter hours."""
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlmodel import Session, select

from . import audit
from .auth import managed_site_ids
from .db import get_session
from .models import Holiday, Site, User
from .web import flash, kick_reconcile, redirect, render, require_manager

router = APIRouter()


def _nth(year: int, month: int, weekday: int, n: int) -> date:
    """n-th weekday (0=Mon) of a month; n=-1 for the last."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def us_holidays(year: int) -> list[tuple[str, date]]:
    """US federal holidays for a year (the dates themselves, not the observed day off)."""
    return [
        ("New Year's Day", date(year, 1, 1)),
        ("Martin Luther King Jr. Day", _nth(year, 1, 0, 3)),
        ("Presidents' Day", _nth(year, 2, 0, 3)),
        ("Memorial Day", _nth(year, 5, 0, -1)),
        ("Juneteenth", date(year, 6, 19)),
        ("Independence Day", date(year, 7, 4)),
        ("Labor Day", _nth(year, 9, 0, 1)),
        ("Columbus Day", _nth(year, 10, 0, 2)),
        ("Veterans Day", date(year, 11, 11)),
        ("Thanksgiving", _nth(year, 11, 3, 4)),
        ("Day after Thanksgiving", _nth(year, 11, 3, 4) + timedelta(days=1)),
        ("Christmas Eve", date(year, 12, 24)),
        ("Christmas Day", date(year, 12, 25)),
        ("New Year's Eve", date(year, 12, 31)),
    ]


US_DEFAULT = {"New Year's Day", "Memorial Day", "Independence Day", "Labor Day", "Thanksgiving", "Christmas Day"}


def _can(user: User, managed: set, site_id: Optional[int]) -> bool:
    return user.is_admin if site_id is None else site_id in managed


@router.get("/holidays")
def holidays_page(request: Request, year: Optional[int] = None, user: User = Depends(require_manager),
                  session: Session = Depends(get_session)):
    managed = managed_site_ids(session, user)
    sites = {x.id: x for x in session.exec(select(Site).order_by(Site.name)).all() if x.id in managed}
    today = date.today()
    rows = [h for h in session.exec(select(Holiday).order_by(Holiday.start_date)).all()
            if h.site_id is None or h.site_id in managed]
    upcoming = [h for h in rows if h.end_date >= today]
    past = [h for h in rows if h.end_date < today][-10:]
    year = year or (today.year + (1 if today.month == 12 else 0))
    have = {(h.name, h.start_date) for h in rows}
    us = [(n, d, (n, d) in have) for n, d in us_holidays(year)]
    return render(request, "holidays.html", user, upcoming=upcoming, past=past, hsites=sites, us=us, year=year,
                  us_default=US_DEFAULT, today=today)


@router.post("/holidays")
def holiday_add(request: Request, bg: BackgroundTasks, name: str = Form(...), start: str = Form(...), end: str = Form(""),
                site: str = Form(""), mode: str = Form("closed"), open_from: str = Form(""), open_to: str = Form(""),
                user: User = Depends(require_manager), session: Session = Depends(get_session)):
    managed = managed_site_ids(session, user)
    site_id = int(site) if site.isdigit() else None
    if not _can(user, managed, site_id):
        flash(request, "Only admins can add holidays for every site. Pick one of your sites.", "error")
        return redirect("/holidays")
    try:
        d0 = date.fromisoformat(start)
        d1 = date.fromisoformat(end) if end else d0
    except ValueError:
        flash(request, "Pick the holiday's date.", "error")
        return redirect("/holidays")
    if d1 < d0:
        d0, d1 = d1, d0
    hours = ""
    if mode == "hours":
        try:
            a, b = datetime.strptime(open_from, "%H:%M").time(), datetime.strptime(open_to, "%H:%M").time()
        except ValueError:
            flash(request, "Set the shorter hours (from and to).", "error")
            return redirect("/holidays")
        if b <= a:
            flash(request, "The shorter hours have to end after they start.", "error")
            return redirect("/holidays")
        hours = f"{a:%H:%M}-{b:%H:%M}"
    h = Holiday(site_id=site_id, name=name.strip() or "Holiday", start_date=d0, end_date=d1, hours=hours,
                created_by=user.id)
    session.add(h)
    audit.record(session, "holiday.update", f"{user.display} added the holiday “{h.name}” ({_when(h)})", user=user,
                 site_ids=[site_id] if site_id else None)
    session.commit()
    kick_reconcile(bg)
    flash(request, f"Added {h.name}.")
    return redirect("/holidays")


@router.post("/holidays/us")
def holiday_us(request: Request, bg: BackgroundTasks, year: int = Form(...), names: list[str] = Form([]),
               site: str = Form(""), user: User = Depends(require_manager), session: Session = Depends(get_session)):
    managed = managed_site_ids(session, user)
    site_id = int(site) if site.isdigit() else None
    if not _can(user, managed, site_id):
        flash(request, "Only admins can add holidays for every site. Pick one of your sites.", "error")
        return redirect(f"/holidays?year={year}")
    have = {(h.name, h.start_date, h.site_id) for h in session.exec(select(Holiday)).all()}
    added = []
    for n, d in us_holidays(year):
        if n in names and (n, d, site_id) not in have:
            session.add(Holiday(site_id=site_id, name=n, start_date=d, end_date=d, created_by=user.id))
            added.append(n)
    if added:
        audit.record(session, "holiday.update", f"{user.display} added {len(added)} US holidays for {year}", user=user,
                     changes=[["Holidays", "", ", ".join(added)]], site_ids=[site_id] if site_id else None)
    session.commit()
    kick_reconcile(bg)
    flash(request, f"Added {len(added)} holiday{'' if len(added) == 1 else 's'} for {year}." if added
          else "Those holidays were already there.")
    return redirect("/holidays")


@router.post("/holidays/{hid}/delete")
def holiday_delete(request: Request, hid: int, bg: BackgroundTasks, user: User = Depends(require_manager),
                   session: Session = Depends(get_session)):
    h = session.get(Holiday, hid)
    if h and _can(user, managed_site_ids(session, user), h.site_id):
        audit.record(session, "holiday.update", f"{user.display} removed the holiday “{h.name}” ({_when(h)})",
                     user=user, site_ids=[h.site_id] if h.site_id else None)
        session.delete(h)
        session.commit()
        kick_reconcile(bg)
        flash(request, f"Removed {h.name}.")
    return redirect("/holidays")


def _when(h: Holiday) -> str:
    s = f"{h.start_date:%a %b %-d, %Y}"
    if h.end_date != h.start_date:
        s += f" to {h.end_date:%a %b %-d}"
    return s + (f", open {h.hours.replace('-', '–')}" if h.hours else ", closed")
