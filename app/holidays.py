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


def _can_h(user: User, managed: set, h: Holiday) -> bool:
    """Can change it: admins anything; others only holidays for sites they all run."""
    return user.is_admin if not h.sites else all(s in managed for s in h.sites)


def _sees(managed: set, h: Holiday) -> bool:
    return not h.sites or any(s in managed for s in h.sites)


@router.get("/holidays")
def holidays_page(request: Request, year: Optional[int] = None, user: User = Depends(require_manager),
                  session: Session = Depends(get_session)):
    managed = managed_site_ids(session, user)
    sites = {x.id: x for x in session.exec(select(Site).order_by(Site.name)).all() if x.id in managed}
    today = date.today()
    rows = [h for h in session.exec(select(Holiday).order_by(Holiday.start_date)).all() if _sees(managed, h)]
    nxt = {h.id: h.next_span(today) for h in rows}
    upcoming = sorted([h for h in rows if nxt[h.id]], key=lambda h: nxt[h.id][0])
    past = [h for h in rows if not nxt[h.id]][-10:]
    year = year or (today.year + (1 if today.month == 12 else 0))
    have = {(h.name, a) for h in rows for a, _ in h.spans(year)}
    us = [(n, d, (n, d) in have) for n, d in us_holidays(year)]
    site_doors = _site_doors(session, user, managed)
    return render(request, "holidays.html", user, upcoming=upcoming, past=past, hsites=sites, us=us, year=year,
                  site_doors=site_doors, h=None, hol_where={x.id: _where(session, x, site_doors) for x in rows},
                  can_edit={x.id for x in rows if _can_h(user, managed, x)}, nxt=nxt,
                  us_default=US_DEFAULT, today=today)


def _site_doors(session: Session, user: User, managed: set) -> dict:
    """{site id: [doors]} for the sites this person manages (doors they can see)."""
    from .auth import allowed_door_ids
    from .models import Door
    allowed = allowed_door_ids(session, user)
    out: dict = {}
    for d in session.exec(select(Door).where(Door.present == True, Door.hidden == False).order_by(Door.name)).all():  # noqa: E712
        if d.site_id in managed and d.id in allowed:
            out.setdefault(d.site_id, []).append(d)
    return out


async def _read(request: Request, session: Session, user: User, h: Holiday) -> str:
    """Fill h from the form. Returns '' or what's wrong."""
    f = await request.form()
    managed = managed_site_ids(session, user)
    if "where" in f or "every" in f or "sites" in f:  # the form's site ticks (where = sent even with none ticked)
        every = bool(f.get("every"))
        sites = [] if every else sorted({int(x) for x in f.getlist("sites") if str(x).isdigit()})
    else:  # one site picked from a list ("" = every site)
        site = str(f.get("site", ""))
        sites = [int(site)] if site.isdigit() else []
        every = not sites
    if every and not user.is_admin:
        return "Only admins can add holidays for every site. Tick your sites instead."
    if not every and not sites:
        return "Tick at least one site."
    if any(s not in managed for s in sites):
        return "You can only add holidays for sites you look after."
    try:
        d0 = date.fromisoformat(str(f.get("start", "")))
        end = str(f.get("end", ""))
        d1 = date.fromisoformat(end) if end else d0
    except ValueError:
        return "Pick the holiday's date."
    if d1 < d0:
        d0, d1 = d1, d0
    hours = ""
    if f.get("mode") == "hours":
        try:
            a = datetime.strptime(str(f.get("open_from", "")), "%H:%M").time()
            b = datetime.strptime(str(f.get("open_to", "")), "%H:%M").time()
        except ValueError:
            return "Set the shorter hours (from and to)."
        if b <= a:
            return "The shorter hours have to end after they start."
        hours = f"{a:%H:%M}-{b:%H:%M}"
    door_ids: list = []
    all_doors = _site_doors(session, user, managed)
    ticked = set(f.getlist("doors"))
    for sid in sites:
        scope = f.get(f"scope_{sid}") or (f.get("scope") if len(sites) == 1 else "")
        if scope != "some":
            continue
        here = {d.id for d in all_doors.get(sid, [])}
        picked = sorted(ticked & here)
        if not picked:
            name = session.get(Site, sid).name if session.get(Site, sid) else "that site"
            return f"Tick at least one door at {name}, or choose All doors."
        if set(picked) != here:  # every door ticked = all doors (including ones added later)
            door_ids += picked
    h.site_id = sites[0] if len(sites) == 1 else None
    h.site_ids = ",".join(str(s) for s in sites) if len(sites) > 1 else ""
    h.name, h.start_date, h.end_date = (str(f.get("name", "")).strip() or "Holiday")[:80], d0, d1
    h.hours, h.door_ids = hours, ",".join(door_ids)
    rep = str(f.get("yearly", ""))
    if rep in ("date", "nth", "last"):
        h.yearly = rep
    elif not (rep == "keep" and h.yearly.startswith("us:")):
        h.yearly = ""
    return ""


def _doors_text(h: Holiday, sid: int, site_doors: dict, names: dict) -> str:
    """"All doors", "Only North Doors, South Doors" or, when most doors are in, "All doors except Box Office"."""
    picked = h.site_doors(sid)
    if not picked:
        return "All doors"
    left_out = [d.name for d in site_doors.get(sid, []) if d.id not in picked]
    if left_out and len(left_out) < len(picked):
        return "All doors except " + ", ".join(left_out)
    return "Only " + ", ".join(names.get(d, "a removed door") for d in picked)


def _where(session: Session, h: Holiday, site_doors: dict) -> list:
    """[{site, count ('' = all doors, else '3 of 20 doors'), detail ('Only …' / 'All doors except …')}]."""
    if not h.sites:
        return [{"site": "Every site", "count": "", "detail": ""}]
    names = {d.id: d.name for ds in site_doors.values() for d in ds}
    from .models import Door
    for d in session.exec(select(Door).where(Door.id.in_(h.doors))).all() if h.doors else []:
        names.setdefault(d.id, d.name)
    out = []
    for sid in h.sites:
        st = session.get(Site, sid)
        picked, total = len(h.site_doors(sid)), len(site_doors.get(sid, []))
        count = "" if not picked else (f"{picked} of {total} doors" if total else f"{picked} door{'' if picked == 1 else 's'}")
        out.append({"site": st.name if st else "A removed site", "count": count,
                    "detail": _doors_text(h, sid, site_doors, names) if picked else ""})
    return sorted(out, key=lambda r: r["site"].lower())


def _snap(h: Holiday) -> dict:
    return {"Name": h.name, "When": _when(h), "Sites": ", ".join(map(str, h.sites)) or "every site",
            "Doors": f"{len(h.doors)} picked" if h.doors else "all"}


@router.post("/holidays")
async def holiday_add(request: Request, bg: BackgroundTasks, user: User = Depends(require_manager),
                      session: Session = Depends(get_session)):
    h = Holiday(name="", start_date=date.today(), end_date=date.today(), created_by=user.id)
    why = await _read(request, session, user, h)
    if why:
        flash(request, why, "error")
        return redirect("/holidays")
    session.add(h)
    audit.record(session, "holiday.update", f"{user.display} added the holiday “{h.name}” ({_when(h)})", user=user,
                 site_ids=h.sites or None)
    session.commit()
    kick_reconcile(bg)
    flash(request, f"Added {h.name}.")
    return redirect("/holidays")


def _editable(session: Session, user: User, hid: int) -> Optional[Holiday]:
    h = session.get(Holiday, hid)
    return h if h and _can_h(user, managed_site_ids(session, user), h) else None


@router.get("/holidays/{hid:int}/edit")
def holiday_edit(request: Request, hid: int, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    h = _editable(session, user, hid)
    if not h:
        flash(request, "That holiday isn't available to you.", "error")
        return redirect("/holidays")
    managed = managed_site_ids(session, user)
    sites = {x.id: x for x in session.exec(select(Site).order_by(Site.name)).all() if x.id in managed}
    return render(request, "holiday_edit.html", user, h=h, hsites=sites, site_doors=_site_doors(session, user, managed),
                  today=date.today())


@router.post("/holidays/{hid:int}")
async def holiday_save(request: Request, hid: int, bg: BackgroundTasks, user: User = Depends(require_manager),
                       session: Session = Depends(get_session)):
    h = _editable(session, user, hid)
    if not h:
        return redirect("/holidays")
    before = _snap(h)
    why = await _read(request, session, user, h)
    if why:
        flash(request, why, "error")
        return redirect(f"/holidays/{hid}/edit")
    session.add(h)
    audit.record(session, "holiday.update", f"{user.display} changed the holiday “{h.name}”", user=user,
                 changes=audit.diff(before, _snap(h)), site_ids=h.sites or None)
    session.commit()
    kick_reconcile(bg)
    flash(request, f"Saved {h.name}.")
    return redirect("/holidays")


@router.post("/holidays/us")
def holiday_us(request: Request, bg: BackgroundTasks, year: int = Form(...), names: list[str] = Form([]),
               site: str = Form(""), every_year: bool = Form(False), user: User = Depends(require_manager),
               session: Session = Depends(get_session)):
    managed = managed_site_ids(session, user)
    site_id = int(site) if site.isdigit() else None
    if not _can(user, managed, site_id):
        flash(request, "Only admins can add holidays for every site. Pick one of your sites.", "error")
        return redirect(f"/holidays?year={year}")
    have = {(h.name, a, h.site_id) for h in session.exec(select(Holiday)).all() for a, _ in h.spans(year)}
    added = []
    for n, d in us_holidays(year):
        if n in names and (n, d, site_id) not in have:
            session.add(Holiday(site_id=site_id, name=n, start_date=d, end_date=d, created_by=user.id,
                                yearly=f"us:{n}" if every_year else ""))
            added.append(n)
    if added:
        audit.record(session, "holiday.update", f"{user.display} added {len(added)} US holidays for {year}", user=user,
                     changes=[["Holidays", "", ", ".join(added)]], site_ids=[site_id] if site_id else None)
    session.commit()
    kick_reconcile(bg)
    flash(request, (f"Added {len(added)} holiday{'' if len(added) == 1 else 's'}"
                    + (", every year from now on." if every_year else f" for {year}.")) if added
          else "Those holidays were already there.")
    return redirect("/holidays")


@router.post("/holidays/{hid}/delete")
def holiday_delete(request: Request, hid: int, bg: BackgroundTasks, user: User = Depends(require_manager),
                   session: Session = Depends(get_session)):
    h = session.get(Holiday, hid)
    if h and _can_h(user, managed_site_ids(session, user), h):
        audit.record(session, "holiday.update", f"{user.display} removed the holiday “{h.name}” ({_when(h)})",
                     user=user, site_ids=h.sites or None)
        session.delete(h)
        session.commit()
        kick_reconcile(bg)
        flash(request, f"Removed {h.name}.")
    return redirect("/holidays")


def _when(h: Holiday) -> str:
    if h.yearly:
        return h.repeat_text() or f"Every year ({h.name})"
    s = f"{h.start_date:%a %b %-d, %Y}"
    if h.end_date != h.start_date:
        s += f" to {h.end_date:%a %b %-d}"
    return s + (f", open {h.hours.replace('-', '–')}" if h.hours else ", closed")


# ---------- holidays already set up in UniFi Access ----------

def _day(value, tz) -> Optional[date]:
    """A UniFi holiday's start or end: unix seconds or milliseconds, or an ISO date/time string."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            n = float(value)
            n = n / 1000 if n > 1e11 else n
            from datetime import timezone
            return datetime.fromtimestamp(n, timezone.utc).astimezone(tz).date()
        s = str(value).replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return (d.astimezone(tz) if d.tzinfo else d).date()
    except (ValueError, OSError, OverflowError):
        return None


def unifi_holidays(session: Session, site: Site) -> tuple[list[dict], str]:
    """([{id, name, holidays: [{name, start, end}]}], error) from the site's UniFi Access console."""
    from . import engine, unifi
    tz = engine.site_tz(site)
    client = unifi.get_client(site)
    try:
        groups = client.holiday_groups()
        out = []
        for g in groups:
            full = client.holiday_group(g.get("id", "")) if not g.get("holidays") else g
            items = []
            for h in (full.get("holidays") or []):
                s = _day(h.get("start_time", h.get("start", h.get("start_date"))), tz)
                e = _day(h.get("end_time", h.get("end", h.get("end_date"))), tz) or s
                if s:
                    if e < s:
                        e = s
                    items.append({"name": (h.get("name") or g.get("name") or "Holiday")[:80], "start": s, "end": e})
            out.append({"id": g.get("id", ""), "name": g.get("name") or "Holidays", "holidays": items})
        return out, ""
    except unifi.UnifiError as e:
        return [], str(e)


@router.get("/holidays/unifi")
def unifi_page(request: Request, site: Optional[int] = None, user: User = Depends(require_manager),
               session: Session = Depends(get_session)):
    managed = managed_site_ids(session, user)
    sites = [x for x in session.exec(select(Site).order_by(Site.name)).all() if x.id in managed]
    st = next((x for x in sites if x.id == site), sites[0] if sites else None)
    groups, error = unifi_holidays(session, st) if st else ([], "")
    have = {(h.name, h.start_date) for h in session.exec(select(Holiday)).all()
            if h.for_site(st.id if st else None)}
    for g in groups:
        for h in g["holidays"]:
            h["have"] = (h["name"], h["start"]) in have
    return render(request, "holidays_unifi.html", user, sites=sites, st=st, groups=groups, error=error,
                  today=date.today())


@router.post("/holidays/unifi")
def unifi_import(request: Request, bg: BackgroundTasks, site: int = Form(...), group: str = Form(...),
                 user: User = Depends(require_manager), session: Session = Depends(get_session)):
    st = session.get(Site, site)
    if not st or site not in managed_site_ids(session, user):
        return redirect("/holidays")
    groups, error = unifi_holidays(session, st)
    g = next((x for x in groups if x["id"] == group), None)
    if not g:
        flash(request, error or "That holiday group isn't in UniFi Access any more.", "error")
        return redirect(f"/holidays/unifi?site={site}")
    have = {(h.name, h.start_date) for h in session.exec(select(Holiday)).all() if h.for_site(site)}
    added = 0
    for h in g["holidays"]:
        if (h["name"], h["start"]) in have or h["end"] < date.today():
            continue
        session.add(Holiday(site_id=site, name=h["name"], start_date=h["start"], end_date=h["end"], created_by=user.id))
        added += 1
    audit.record(session, "holiday.update", f"Imported {added} holiday(s) from UniFi Access ({g['name']}) for {st.name}",
                 user=user, site_ids=[site])
    session.commit()
    kick_reconcile(bg)
    flash(request, f"Added {added} holiday{'s' if added != 1 else ''} from “{g['name']}”." if added else
          "Nothing new to add: those holidays are already here or already over.")
    return redirect("/holidays")
