"""HTML routes."""
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from sqlmodel import Session, func, select

from . import audit, calendars, config, engine, unifi
from .auth import (AdminRequired, LoginRequired, allowed_door_ids, can_manage_person, current_user,
                   fully_managed, hash_password, login_user, managed_site_ids, person_site_ids, safe_next,
                   verify_password, visible_site_ids)
from .db import get_session, session_scope
from .models import (AuditLog, Building, CalendarFeed, CalendarRule, Door, DoorState, Schedule,
                     ScheduleDoor, Site, User, UserDoor, UserSite, utcnow)
from .schedules import (EVENT_TYPES, WEEKDAYS, describe_kind, describe_repeat, event_icon, event_name,
                        expand, now_local, series_last_date, to_local, weekdays_of)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _time(dt: datetime) -> str:
    return dt.strftime("%-I:%M %p").replace(":00 ", " ")


def _day(d) -> str:
    today = now_local().date()
    dd = d.date() if isinstance(d, datetime) else d
    if dd == today:
        return "Today"
    if dd == today + timedelta(days=1):
        return "Tomorrow"
    fmt = "%a, %b %-d" if dd.year == today.year else "%a, %b %-d, %Y"
    return dd.strftime(fmt)


def _span(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return f"{_time(start)} – {_time(end)}"
    return f"{_time(start)} – {_day(end)} {_time(end)}"


templates.env.filters.update(time=_time, day=_day, local=to_local)
templates.env.globals.update(span=_span, minutes=lambda m: timedelta(minutes=m), WEEKDAYS=WEEKDAYS,
                             describe_repeat=describe_repeat, describe_kind=describe_kind, config=config,
                             EVENT_TYPES=EVENT_TYPES, event_icon=event_icon, event_name=event_name)


# ---------- helpers ----------

def door_tree(doors) -> list:
    """Group doors for display: [(site_id, [(Building or None, [doors...]), ...]), ...].

    Buildings in name order, doors not in a building last ("Other doors")."""
    doors = list(doors)
    with session_scope() as s:
        blds = {b.id: b for b in s.exec(select(Building)).all()}
        for b in blds.values():
            s.expunge(b)
    tree: dict[int, dict] = {}
    for d in doors:
        tree.setdefault(d.site_id, {}).setdefault(d.building_id if d.building_id in blds else None, []).append(d)
    out = []
    for site_id in sorted(tree):
        groups = tree[site_id]
        keys = sorted((k for k in groups if k is not None), key=lambda k: blds[k].name.lower())
        rows = [(blds[k], sorted(groups[k], key=lambda d: d.name.lower())) for k in keys]
        if None in groups:
            rows.append((None, sorted(groups[None], key=lambda d: d.name.lower())))
        out.append((site_id, rows))
    return out


templates.env.globals["door_tree"] = door_tree


def _asset_version() -> str:
    """Fingerprint of the stylesheet and scripts, so browsers fetch new ones after an update."""
    import hashlib
    h = hashlib.sha256()
    try:
        for name in ("style.css", "live.js", "planzoom.js"):
            h.update((Path(__file__).parent / "static" / name).read_bytes())
        return h.hexdigest()[:10]
    except OSError:
        return config.VERSION


templates.env.globals["asset_v"] = _asset_version()
templates.env.globals["live_on"] = lambda: bool(config.LIVE_VIDEO_URL)

def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, msg])


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def render(request: Request, name: str, user: Optional[User], **ctx) -> HTMLResponse:
    msgs = request.session.pop("flash", [])
    with session_scope() as s:
        sites = {x.id: x for x in s.exec(select(Site).order_by(Site.name)).all()}
        if user and not user.is_admin:  # door operators only ever see their own sites
            mine = visible_site_ids(s, user)
            sites = {k: v for k, v in sites.items() if k in mine}
        managed_ids = managed_site_ids(s, user) if user else set()
        nav_unlocked = nav_pending = 0
        if user and user.is_manager:
            nav_pending = len(s.exec(select(Schedule.id).where(Schedule.status == "pending")).all())
        if user:
            allowed = allowed_door_ids(s, user)
            now = utcnow()
            nav_unlocked = sum(1 for st in s.exec(select(DoorState)).all()
                               if st.door_id in allowed and st.mode == "unlock" and st.until and st.until > now)
    demo = config.DRY_RUN or any(x.is_demo for x in sites.values())
    ctx.setdefault("emergencies", [x for x in sites.values() if x.emergency])
    ctx.setdefault("managed_ids", managed_ids)
    ctx.setdefault("nav_unlocked", nav_unlocked)
    ctx.setdefault("nav_pending", nav_pending)
    ctx.setdefault("sites", sites)
    ctx.setdefault("nav_sites", sites)
    ctx.setdefault("current_site", request.session.get("site") if request.session.get("site") in sites else None)
    return templates.TemplateResponse(request, name, {"user": user, "flashes": msgs, "demo": demo, **ctx})


def require_user(request: Request, session: Session = Depends(get_session)) -> User:
    user = current_user(request, session)
    if not user:
        raise LoginRequired()
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise AdminRequired()
    return user


def require_scheduler(user: User = Depends(require_user)) -> User:
    """Everyone but door openers."""
    if not user.schedules:
        raise AdminRequired()
    return user


def require_manager(user: User = Depends(require_user)) -> User:
    """Admins and site admins."""
    if not user.is_manager:
        raise AdminRequired()
    return user


def doors_for(session: Session, user: User) -> list[Door]:
    allowed = allowed_door_ids(session, user)
    return [d for d in all_doors(session) if d.id in allowed]


def all_doors(session: Session) -> list[Door]:
    """Present doors, ordered by site then name (pickers group them by building with door_tree)."""
    return list(session.exec(select(Door).where(Door.present == True, Door.hidden == False)  # noqa: E712
                             .order_by(Door.site_id, Door.name)).all())


def door_status(session: Session, doors: list[Door]) -> dict[str, dict]:
    """door_id -> {mode, until (site-local), label, error} for doors OpnDoor is currently holding."""
    states = {st.door_id: st for st in session.exec(select(DoorState)).all()}
    sites = {x.id: x for x in session.exec(select(Site)).all()}
    now = utcnow()
    out = {}
    for d in doors:
        st = states.get(d.id)
        if not st:
            continue
        info = {"mode": "", "error": st.last_error}
        if st.mode == "lock" or (st.mode == "unlock" and st.until and st.until > now):
            sched = session.get(Schedule, st.schedule_id) if st.schedule_id else None
            info.update(mode=st.mode, label=sched.label if sched else "",
                        held=bool(st.until and st.until - now > engine.HOLD_AFTER),
                        until=to_local(st.until, engine.site_tz(sites.get(d.site_id))) if st.until else None)
        out[d.id] = info
    return out


def schedule_doors(session: Session, ids: list[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {i: [] for i in ids}
    if ids:
        for row in session.exec(select(ScheduleDoor).where(ScheduleDoor.schedule_id.in_(ids))).all():
            out[row.schedule_id].append(row.door_id)
    return out


def visible_schedules(session: Session, user: User) -> list[Schedule]:
    if user.is_admin:
        return list(session.exec(select(Schedule).where(Schedule.status == "")).all())
    allowed = allowed_door_ids(session, user)
    if not allowed:
        return []
    return list(session.exec(
        select(Schedule).join(ScheduleDoor, ScheduleDoor.schedule_id == Schedule.id)
        .where(ScheduleDoor.door_id.in_(allowed), Schedule.status == "").distinct()
    ).all())


def can_edit(session: Session, user: User, sched: Schedule, door_ids: list[str]) -> bool:
    if sched.source == "calendar" or not user.schedules:
        return False
    return user.is_admin or set(door_ids) <= allowed_door_ids(session, user)


def group_by_site(session: Session, door_ids: list[str]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for d in door_ids:
        door = session.get(Door, d)
        if door:
            out.setdefault(door.site_id, []).append(d)
    return out


def kick_reconcile(bg: BackgroundTasks) -> None:
    bg.add_task(engine.safe(engine.reconcile))


# ---------- auth ----------

def has_users(session: Session) -> bool:
    return session.exec(select(func.count()).select_from(User)).one() > 0


@router.get("/setup")
def setup_page(request: Request, session: Session = Depends(get_session)):
    if has_users(session):
        return redirect("/login")
    return render(request, "setup.html", None)


@router.post("/setup")
def setup(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...),
          setup_code: str = Form(""), session: Session = Depends(get_session)):
    if has_users(session):
        return redirect("/login")
    import hmac
    if config.SETUP_CODE and not hmac.compare_digest(setup_code.strip().upper(), config.SETUP_CODE.upper()):
        flash(request, "That setup code isn't right. It was shown at the end of the install "
              "(and is saved in the .env file as SETUP_CODE).", "error")
        return redirect("/setup")
    if len(password) < 8:
        flash(request, "Password must be at least 8 characters.", "error")
        return redirect("/setup")
    user = User(name=name.strip(), email=email.strip().lower(), password_hash=hash_password(password), is_admin=True,
                is_owner=True)
    session.add(user)
    session.commit()
    audit.record(session, "user.setup", f"{user.display} set up OpnDoor and became the main admin", user=user)
    login_user(request, session, user, "a password")
    engine.ensure_sites()
    engine.sync_doors()
    if not session.exec(select(Site)).first():
        flash(request, "Welcome! Next step: connect your UniFi Access console so OpnDoor can see your doors.")
        return redirect("/sites/new")
    flash(request, "Welcome! Add your UniFi Access consoles under Sites." if demo_only(session)
          else "Welcome! Your doors were loaded from UniFi Access.")
    return redirect("/")


def demo_only(session: Session) -> bool:
    sites = session.exec(select(Site)).all()
    return bool(sites) and all(x.is_demo for x in sites)


@router.get("/login")
def login_page(request: Request, session: Session = Depends(get_session)):
    if not has_users(session):
        return redirect("/setup")
    if current_user(request, session):
        return redirect("/")
    return render(request, "login.html", None)


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          session: Session = Depends(get_session)):
    """Sign in with a password, using the email address or the username."""
    email = email.strip().lower()
    if "@" in email:
        user = session.exec(select(User).where(User.email == email)).first()
    else:
        user = session.exec(select(User).where(User.username == email)).first()
    if not user or not user.active or not user.password_hash or not verify_password(password, user.password_hash):
        reason = ("their account is turned off" if user and not user.active
                  else "their account signs in with Microsoft" if user and not user.password_hash
                  else "the password was wrong" if user else "there's no such person")
        audit.record(session, "signin.fail", f"Password sign-in for {email} refused: {reason}", actor=email)
        session.commit()
        flash(request, "This account signs in with Microsoft. Use the Microsoft button."
              if user and user.active and not user.password_hash and config.SSO_ENABLED
              else "That email or username and password didn't match.", "error")
        return redirect("/login")
    login_user(request, session, user, "a password")
    return redirect(safe_next(request.query_params.get("next")))


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@router.post("/theme")
def set_theme(request: Request, theme: str = Form("auto"), next: str = Form("/"),
              user: User = Depends(require_user), session: Session = Depends(get_session)):
    user.theme = theme if theme in {"auto", "light", "dark"} else "auto"
    session.add(user)
    session.commit()
    return redirect(safe_next(next))


@router.get("/account")
def account_page(request: Request, user: User = Depends(require_user)):
    return render(request, "account.html", user)


START_PAGES = {"home", "doors", "map", "schedules"}


@router.post("/account")
def account(request: Request, name: str = Form(""), current_password: str = Form(""),
            new_password: str = Form(""), start_page: str = Form(""), user: User = Depends(require_user),
            session: Session = Depends(get_session)):
    user.start_page = start_page if start_page in START_PAGES else ""
    old_name = user.name
    user.name = name.strip()
    if old_name != user.name:
        audit.record(session, "account.update", f"{user.display} changed their name",
                     changes=[["Name", old_name, user.name]], user=user)
    if new_password:
        if not verify_password(current_password, user.password_hash):
            flash(request, "Current password is wrong.", "error")
            return redirect("/account")
        if len(new_password) < 8:
            flash(request, "New password must be at least 8 characters.", "error")
            return redirect("/account")
        user.password_hash = hash_password(new_password)
        audit.record(session, "account.update", f"{user.display} changed their password", user=user)
    session.add(user)
    session.commit()
    flash(request, "Account updated.")
    return redirect("/account")


# ---------- dashboard ----------

def current_site_id(request: Request, session: Session, user: User) -> Optional[int]:
    """The site picked in the sidebar switcher (None = all sites)."""
    sid = request.session.get("site")
    if not sid:
        return None
    if sid not in visible_site_ids(session, user):
        request.session.pop("site", None)
        return None
    return sid


@router.get("/site")
def pick_site(request: Request, id: str = "", next: str = "/", user: User = Depends(require_user),
              session: Session = Depends(get_session)):
    if id.isdigit() and int(id) in visible_site_ids(session, user):
        request.session["site"] = int(id)
    else:
        request.session.pop("site", None)
    return redirect(safe_next(next))


class DoorNamer:
    """Short, readable door lists: whole buildings collapse to "Event Center (all 4)"."""

    def __init__(self, session: Session, doors: list[Door]):
        self.doors = {d.id: d for d in doors}
        self.sites = {x.id: x.name for x in session.exec(select(Site)).all()}
        self.buildings = {b.id: b.name for b in session.exec(select(Building)).all()}
        self.totals: dict[int, int] = {}
        for bid, n in session.exec(select(Door.building_id, func.count()).where(
                Door.building_id != None, Door.present == True, Door.hidden == False)  # noqa: E711,E712
                .group_by(Door.building_id)).all():
            self.totals[bid] = n

    def __call__(self, ids, limit: int = 4) -> str:
        doors = [self.doors[d] for d in ids if d in self.doors]
        multi_site = len({d.site_id for d in doors}) > 1
        groups: dict = {}
        for d in doors:
            groups.setdefault(d.building_id if d.building_id in self.buildings else None, []).append(d)
        parts = []
        for bid in sorted((k for k in groups if k is not None), key=lambda k: self.buildings[k].lower()):
            ds = groups[bid]
            tag = f", {self.sites.get(ds[0].site_id, '')}" if multi_site else ""
            if len(ds) > 1 and len(ds) == self.totals.get(bid):
                parts.append(f"{self.buildings[bid]} (all {len(ds)}{tag})")
            else:
                parts.extend(f"{d.name}" + (f" ({self.sites.get(d.site_id, '')})" if multi_site else "")
                             for d in sorted(ds, key=lambda d: d.name.lower()))
        parts.extend(f"{d.name}" + (f" ({self.sites.get(d.site_id, '')})" if multi_site else "")
                     for d in sorted(groups.get(None, []), key=lambda d: d.name.lower()))
        more = len(parts) - limit
        return ", ".join(parts[:limit]) + (f" +{more} more" if more > 0 else "")


@router.get("/")
def start(request: Request, user: User = Depends(require_user), session: Session = Depends(get_session)):
    """Each person lands on their start page (Home unless they or an admin chose another)."""
    page = user.start_path
    if page != "/home":
        return redirect(page)
    return dashboard(request, user, session)


@router.get("/home")
def dashboard(request: Request, user: User = Depends(require_user), session: Session = Depends(get_session)):
    site_id = current_site_id(request, session, user)
    doors = [d for d in doors_for(session, user) if site_id is None or d.site_id == site_id]
    in_view = {d.id for d in doors}
    names = {d.id: d.name for d in doors}
    sites = {x.id: x for x in session.exec(select(Site)).all()}
    status = door_status(session, doors)

    # Doors that aren't simply locked on their normal rule
    right_now = []
    for d in doors:
        st = status.get(d.id, {})
        if st.get("mode") or st.get("error"):
            right_now.append({"door": d, "site": sites.get(d.site_id), "st": st,
                              "can_lock": st.get("mode") == "unlock"})
    right_now.sort(key=lambda r: (not r["st"].get("error"), r["st"].get("mode") != "unlock",
                                  r["site"].name if r["site"] else "", r["door"].name))

    # Events (overrides) today and in the next 7 days
    now = now_local()
    today = now.date()
    scheds = [x for x in visible_schedules(session, user) if x.kind == "override"]
    sd = schedule_doors(session, [x.id for x in scheds])
    namer = DoorNamer(session, doors)
    agenda_today, agenda_week = [], {}
    hols = engine.holidays_for(session, site_id)
    for o in expand(scheds, now.replace(hour=0, minute=0, second=0, microsecond=0), now + timedelta(days=7), hols):
        ids = [d for d in sd[o.schedule.id] if d in in_view]
        if not ids:
            continue
        row = {"o": o, "doors": namer(ids), "count": len(ids),
               "editable": can_edit(session, user, o.schedule, sd[o.schedule.id]),
               "live": o.start <= now < o.end, "past": o.end <= now}
        if o.start.date() <= today:
            agenda_today.append(row)
        else:
            agenda_week.setdefault(o.start.date(), []).append(row)

    tiles = {
        "unlocked": sum(1 for r in right_now if r["st"].get("mode") == "unlock"),
        "locked": sum(1 for r in right_now if r["st"].get("mode") == "lock"),
        "problems": sum(1 for r in right_now if r["st"].get("error")),
        "events": sum(1 for r in agenda_today if not r["past"]),
        "open": sum(1 for d in doors if d.position_status == "open"),
    }
    managed = managed_site_ids(session, user)
    em_sites = [x for x in session.exec(select(Site).order_by(Site.name)).all()
                if x.id in managed and (site_id is None or x.id == site_id)]
    standing_open = [d for d in doors if d.position_status == "open"]
    from .activity import busy_hours_for
    busy = busy_hours_for(session, in_view)
    return render(request, "dashboard.html", user, doors=doors, right_now=right_now, tiles=tiles,
                  em_sites=em_sites, standing_open=standing_open, busy=busy,
                  agenda_today=agenda_today, agenda_week=agenda_week, site_id=site_id,
                  today_label=now.strftime("%A, %B %-d"), default_until=_default_until(now))


def _default_until(now: datetime) -> str:
    return (now + timedelta(minutes=75)).replace(minute=((now.minute + 75) % 60) // 15 * 15).strftime("%H:%M")


@router.post("/doors/{door_id}/lock-now")
def lock_now(request: Request, door_id: str, bg: BackgroundTasks, next: str = Form("/"),
             user: User = Depends(require_user), session: Session = Depends(get_session)):
    """Lock a door that a schedule has unlocked, until that unlock would have ended.

    Adds a short "keep locked" override (newest wins), so the schedule itself is untouched
    and the door goes back to normal afterwards."""
    from .schedules import current_plan
    door = session.get(Door, door_id)
    if not door or door_id not in allowed_door_ids(session, user):
        flash(request, "That door isn't available to you.", "error")
        return redirect(safe_next(next))
    now_utc = utcnow()
    plan = current_plan(engine.door_timeline(session, door, now_utc, now_utc + engine.HORIZON))
    if not plan or plan.state != "unlock":
        flash(request, f"{door.name} is already locked.")
        return redirect(safe_next(next))
    site = session.get(Site, door.site_id)
    if site and site.emergency:
        flash(request, f"{site.emergency.capitalize()} is on at {site.name}, so doors can't be changed until it ends.", "error")
        return redirect(safe_next(next))
    tz = engine.site_tz(site)
    if plan.schedule and plan.schedule.source == "quick":
        # an "unlock now" or "keep unlocked": just end it here
        q = plan.schedule
        q.duration_minutes = max(0, int((now_local(tz) - q.start_local).total_seconds() // 60))
        if q.duration_minutes <= 0:
            calendars.delete_schedule(session, q)
        else:
            session.add(q)
        audit.record(session, "schedule.locknow", f"{user.display} set {door.name} back to normal (ended “{q.label}”)",
                     user=user, door_ids=[door.id])
        session.commit()
        kick_reconcile(bg)
        flash(request, f"{door.name} is back to normal and locks now.")
        return redirect(safe_next(next))
    minutes = max(1, int((plan.end - now_utc).total_seconds() // 60) + 1)
    why = plan.schedule.label if plan.schedule else "its schedule"
    sched = Schedule(label=f"Locked early by {user.display}", kind="override", action="lock", event_type="other",
                     start_local=now_local(tz).replace(second=0, microsecond=0), duration_minutes=minutes,
                     source="quick", created_by=user.id, notes=f"Locked during “{why}”")
    session.add(sched)
    session.flush()
    calendars.set_schedule_doors(session, sched.id, [door.id])
    audit.record(session, "schedule.locknow", f"{user.display} locked {door.name} early (it was unlocked for “{why}”)",
                 user=user, door_ids=[door.id], schedule_id=sched.id)
    session.commit()
    kick_reconcile(bg)
    flash(request, f"Locking {door.name}. It goes back to its normal schedule afterwards.")
    return redirect(safe_next(next))


def _until_minutes(until: str, tz) -> Optional[int]:
    """Minutes from now until HH:MM at a site (today, or tomorrow if that time has passed)."""
    try:
        t = datetime.strptime(until.strip(), "%H:%M").time()
    except ValueError:
        return None
    now = now_local(tz).replace(second=0, microsecond=0)
    end = datetime.combine(now.date(), t)
    if end <= now:
        end += timedelta(days=1)
    return int((end - now).total_seconds() // 60)


def _duration_text(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} hour{'s' if h > 1 else ''} {m} min"
    return f"{h} hour{'s' if h > 1 else ''}" if h else f"{m} minutes"


@router.post("/quick-unlock")
def quick_unlock(request: Request, bg: BackgroundTasks, door_ids: list[str] = Form([]),
                 minutes: Optional[int] = Form(None), until: str = Form(""), next: str = Form("/"),
                 mode: str = Form(""), user: User = Depends(require_user), session: Session = Depends(get_session)):
    """Open doors (momentary, like a badge tap), or unlock them for a number of minutes or until a time."""
    from .access import open_hours_ok
    allowed = allowed_door_ids(session, user)
    door_ids = [d for d in door_ids if d in allowed]
    if not door_ids:
        flash(request, "Pick at least one door.", "error")
        return redirect(safe_next(next))
    as_json = request.headers.get("x-opndoor") == "fetch"  # the page opens a door and stays put
    refused = []
    for did in list(door_ids):
        st = session.get(Site, session.get(Door, did).site_id)
        if st and st.emergency:
            refused.append(f"{st.emergency.capitalize()} is on at {st.name}, so its doors can't be opened or unlocked from OpnDoor.")
            door_ids.remove(did)
    for did in list(door_ids):
        ok, why = open_hours_ok(session, user, session.get(Door, did))
        if not ok:
            refused.append(why)
            door_ids.remove(did)
    if as_json and (refused or not door_ids):
        return JSONResponse({"ok": False, "message": " ".join(refused) or "Pick at least one door."}, status_code=403)
    for why in refused:
        flash(request, why, "error")
    if not door_ids:
        return redirect(safe_next(next))
    if mode == "open":
        return _open_doors(request, session, user, door_ids, next, as_json)
    if mode == "hold":
        return _hold_doors(request, bg, session, user, door_ids, next)
    if minutes is None and not until.strip():
        flash(request, "Choose how long, or pick a time to unlock until.", "error")
        return redirect(safe_next(next))
    groups = group_by_site(session, door_ids)
    plans = []
    for site_id, ids in groups.items():
        tz = engine.site_tz(session.get(Site, site_id))
        mins = minutes if minutes is not None else _until_minutes(until, tz)
        if mins is None:
            flash(request, "That time doesn't look right.", "error")
            return redirect(safe_next(next))
        plans.append((tz, ids, max(1, min(mins, 24 * 60))))
    for tz, ids, mins in plans:
        start = now_local(tz).replace(second=0, microsecond=0)
        sched = Schedule(label=f"Unlock now by {user.display}", kind="override", action="unlock",
                         event_type="other", start_local=start, duration_minutes=mins + (1 if minutes is not None else 0),
                         source="quick", created_by=user.id)
        session.add(sched)
        session.flush()
        calendars.set_schedule_doors(session, sched.id, ids)
    names = DoorNamer(session, [session.get(Door, d) for d in door_ids])(door_ids, limit=6)
    if minutes is not None:
        how = f"for {_duration_text(plans[0][2])}"
    else:
        end = datetime.strptime(until.strip(), "%H:%M")
        how = f"until {end.strftime('%-I:%M %p').replace(':00 ', ' ')}"
    audit.record(session, "schedule.quick", f"{user.display} unlocked {names} {how}", user=user, door_ids=door_ids)
    session.commit()
    kick_reconcile(bg)
    flash(request, f"Unlocking {names} {how}.")
    return redirect(safe_next(next))


def _hold_doors(request: Request, bg: BackgroundTasks, session: Session, user: User, door_ids: list[str], next: str):
    """Keep doors unlocked until someone sets them back (Lock now / Set back to normal)."""
    for site_id, ids in group_by_site(session, door_ids).items():
        tz = engine.site_tz(session.get(Site, site_id))
        sched = Schedule(label=f"Kept unlocked by {user.display}", kind="override", action="unlock", event_type="other",
                         start_local=now_local(tz).replace(second=0, microsecond=0), duration_minutes=HOLD_MINUTES,
                         source="quick", created_by=user.id, notes="Until someone sets it back to normal")
        session.add(sched)
        session.flush()
        calendars.set_schedule_doors(session, sched.id, ids)
    names = DoorNamer(session, [session.get(Door, d) for d in door_ids])(door_ids, limit=6)
    audit.record(session, "schedule.quick", f"{user.display} kept {names} unlocked until set back", user=user,
                 door_ids=door_ids)
    session.commit()
    kick_reconcile(bg)
    flash(request, f"{names} stays unlocked until someone sets it back to normal (Lock now).")
    return redirect(safe_next(next))


HOLD_MINUTES = 365 * 24 * 60


def _open_doors(request: Request, session: Session, user: User, door_ids: list[str], next: str,
                as_json: bool = False):
    """Momentary unlock: UniFi opens the lock for the door's own unlock time, then relocks."""
    opened, failed = [], []
    for did in door_ids:
        door = session.get(Door, did)
        try:
            unifi.get_client(session.get(Site, door.site_id)).open_door(door.remote_id, f"opndoor-{user.id}",
                                                                          f"{user.display} (OpnDoor)")
            opened.append(did)
        except unifi.UnifiError as e:
            failed.append(f"{door.name}: {e}")
    message = ""
    if opened:
        names = DoorNamer(session, [session.get(Door, d) for d in opened])(opened, limit=6)
        audit.record(session, "door.open", f"{user.display} opened {names}", user=user, door_ids=opened)
        session.commit()
        message = f"Opened {names}. It locks again by itself in a few seconds."
    if as_json:
        return JSONResponse({"ok": bool(opened) and not failed,
                             "message": " ".join([message] + [f"Couldn't open {f}" for f in failed]).strip()},
                            status_code=200 if opened else 502)
    if message:
        flash(request, message)
    for f in failed:
        flash(request, f"Couldn't open {f}", "error")
    return redirect(safe_next(next))


# ---------- schedules ----------

# The Schedules page is about events and overrides; regular hours live on each door's page
# and only show here when asked for ("Regular hours" or "Everything").
SCHEDULE_TYPES = [("", "Events and overrides"), ("unlock", "Events (unlocked)"), ("lock", "Kept locked"),
                  ("calendar", "From calendars"), ("default", "Regular hours"), ("all", "Everything")]


def _sched_type_ok(s: Schedule, t: str) -> bool:
    return ((not t and s.kind == "override") or t == "all" or (t == "default" and s.kind == "default")
            or (t == "unlock" and s.kind == "override" and s.action == "unlock")
            or (t == "lock" and s.kind == "override" and s.action == "lock")
            or (t == "calendar" and s.source == "calendar"))


@router.get("/schedules")
def schedules_list(request: Request, view: str = "", week: str = "", q: str = "", type: str = "",
                   building: str = "", past: str = "", user: User = Depends(require_user),
                   session: Session = Depends(get_session)):
    if view in ("week", "list"):
        request.session["sched_view"] = view
    view = request.session.get("sched_view", "week")
    site_id = current_site_id(request, session, user)
    doors = [d for d in doors_for(session, user) if site_id is None or d.site_id == site_id]
    in_view = {d.id for d in doors}
    namer = DoorNamer(session, doors)
    buildings = sorted({d.building_id for d in doors if d.building_id} & set(namer.buildings),
                       key=lambda b: namer.buildings[b].lower())
    bdoors = {d.id for d in doors if building.isdigit() and d.building_id == int(building)}

    scheds = visible_schedules(session, user)
    sd = schedule_doors(session, [x.id for x in scheds])
    feeds = {f.id: f.name for f in session.exec(select(CalendarFeed)).all()}
    door_text = {d.id: d.name.lower() for d in doors}
    needle = q.strip().lower()
    today = now_local().date()

    def keep(x: Schedule) -> bool:
        ids = [d for d in sd[x.id] if d in in_view]
        if not ids or not _sched_type_ok(x, type):
            return False
        if building.isdigit() and not (set(ids) & bdoors):
            return False
        if needle:
            hay = " ".join([x.label, x.contact, x.notes] + [door_text[d] for d in ids]).lower()
            if needle not in hay:
                return False
        return True

    scheds = [x for x in scheds if keep(x)]

    def card(x: Schedule, o=None) -> dict:
        ids = [d for d in sd[x.id] if d in in_view]
        return {"s": x, "o": o, "doors": namer(ids), "ndoors": len(ids), "feed": feeds.get(x.feed_id),
                "editable": can_edit(session, user, x, sd[x.id])}

    ctx = dict(view=view, q=q, type=type, building=building, past=past, types=SCHEDULE_TYPES,
               building_opts=[(b, namer.buildings[b]) for b in buildings], total=len(scheds))
    if view == "week":
        try:
            ref = date.fromisoformat(week) if week else today
        except ValueError:
            ref = today
        start = ref - timedelta(days=ref.weekday())
        days = [start + timedelta(days=k) for k in range(7)]
        lo = datetime.combine(start, datetime.min.time())
        by_day: dict[date, list] = {d: [] for d in days}
        hols = engine.holidays_for(session, current_site_id(request, session, user))
        for o in expand(scheds, lo, lo + timedelta(days=7), hols):
            day = max(o.start.date(), start)
            if day in by_day:
                by_day[day].append(card(o.schedule, o))
        for d in days:
            by_day[d].sort(key=lambda c: (c["s"].kind != "default", c["o"].start))
        ctx.update(days=days, by_day=by_day, week_start=start, today=today, holidays=hols,
                   prev_week=(start - timedelta(days=7)).isoformat(), next_week=(start + timedelta(days=7)).isoformat(),
                   is_this_week=start <= today < start + timedelta(days=7))
    else:
        now = now_local()
        rows = []
        for x in scheds:
            nxt = expand([x], now, now + timedelta(days=400))
            ended = not nxt and series_last_date(x) < today
            if ended and not past:
                continue
            c = card(x)
            c["next"] = nxt[0] if nxt else None
            c["ended"] = ended
            rows.append(c)
        far = datetime.max
        rows.sort(key=lambda c: (c["next"].start if c["next"] else far, c["s"].label.lower()))
        ctx.update(defaults=[r for r in rows if r["s"].kind == "default"],
                   overrides=[r for r in rows if r["s"].kind != "default"])
    return render(request, "schedules.html", user, **ctx)


def _form_ctx(session, user, sched: Optional[Schedule], selected: list[str], kind: str = "override",
              site_id: Optional[int] = None):
    now = now_local()
    if sched:
        kind = sched.kind
        start = sched.start_local
        end = start + timedelta(minutes=sched.duration_minutes)
        days = weekdays_of(sched)
    elif kind == "default":
        start = now.replace(hour=8, minute=0, second=0, microsecond=0)
        end = start.replace(hour=17)
        days = [0, 1, 2, 3, 4]
    else:
        start = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=2)
        days = []
    people = {}
    if sched:
        for uid in {sched.created_by, sched.updated_by} - {None}:
            u = session.get(User, uid)
            people[uid] = u.display if u else "someone"
    doors = doors_for(session, user)
    if site_id is not None:  # follow the sidebar site switcher, but never hide doors already picked
        doors = [d for d in doors if d.site_id == site_id or d.id in set(selected)]
    action = sched.action if sched else "unlock"
    mode = "default" if kind == "default" else ("lock" if action == "lock" else "unlock")
    return dict(sched=sched, doors=doors, selected=set(selected), kind=kind, mode=mode,
                action=action, people=people,
                until_val=sched.repeat_until.isoformat() if sched and sched.repeat_until else "",
                event_type=sched.event_type if sched else "event",
                date_val=start.date().isoformat(), start_val=start.strftime("%H:%M"),
                end_val=end.strftime("%H:%M"), days=days)


@router.get("/schedules/new")
def schedule_new(request: Request, door: list[str] = Query([]), kind: str = "override", day: str = "",
                 user: User = Depends(require_scheduler), session: Session = Depends(get_session)):
    """`?door=a&door=b` preselects doors; `?kind=default` starts regular hours; `?day=2030-01-31` sets the date."""
    kind = "default" if kind == "default" else "override"
    ctx = _form_ctx(session, user, None, door, kind, current_site_id(request, session, user))
    try:
        ctx["date_val"] = date.fromisoformat(day).isoformat()
    except ValueError:
        pass
    return render(request, "schedule_form.html", user, **ctx)


@router.get("/schedules/{sid}")
def schedule_edit(request: Request, sid: int, user: User = Depends(require_scheduler),
                  session: Session = Depends(get_session)):
    sched = session.get(Schedule, sid)
    if not sched:
        return redirect("/schedules")
    doors = schedule_doors(session, [sid])[sid]
    if not can_edit(session, user, sched, doors):
        flash(request, "You can't edit that schedule." if sched.source != "calendar"
              else "That schedule comes from a calendar. Change or cancel the booking in the calendar instead.", "error")
        return redirect("/schedules")
    return render(request, "schedule_form.html", user, **_form_ctx(session, user, sched, doors))


def _parse_schedule(request, label, door_ids, day, start, end, repeat, weekdays, until, allowed,
                    kind="override", action="unlock", event_type="event", contact=""):
    errors = []
    kind = "default" if kind == "default" else "override"
    action = "lock" if kind == "override" and action == "lock" else "unlock"
    if kind == "default":
        repeat = "weekly"
        if not [w for w in weekdays if w.isdigit()]:
            errors.append("Pick the days for the regular hours.")
    label = label.strip()
    if not label:
        errors.append("Give the schedule a name.")
    door_ids = [d for d in door_ids if d in allowed]
    if not door_ids:
        errors.append("Pick at least one door.")
    try:
        start_dt = datetime.combine(date.fromisoformat(day), datetime.strptime(start, "%H:%M").time())
        end_dt = datetime.combine(start_dt.date(), datetime.strptime(end, "%H:%M").time())
    except ValueError:
        errors.append("Check the date and times.")
        return None, errors
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)  # e.g. 10 PM – 2 AM runs overnight
    days = sorted({int(w) for w in weekdays if w.isdigit() and 0 <= int(w) <= 6}) if repeat == "weekly" else []
    if repeat == "weekly" and not days:
        days = [start_dt.weekday()]
    until_d = None
    if days and until:
        try:
            until_d = date.fromisoformat(until)
        except ValueError:
            errors.append("Check the repeat-until date.")
        else:
            if until_d < start_dt.date():
                errors.append("Repeat-until date is before the start date.")
    if not days and end_dt < now_local():
        errors.append("That time has already passed.")
    if event_type not in EVENT_TYPES:
        event_type = "other"
    return dict(label=label, kind=kind, action=action, event_type=event_type, contact=contact.strip(),
                door_ids=door_ids, start_local=start_dt,
                duration_minutes=int((end_dt - start_dt).total_seconds() // 60),
                repeat_weekdays=",".join(map(str, days)), repeat_until=until_d), errors


def _save_perday(request, bg, session, user, label, door_ids, day, until, allowed, pd_on, pd_start, pd_end,
                 on_holidays, notes):
    """Regular hours that differ by day: one schedule per distinct time range."""
    groups: dict[tuple[str, str], list[str]] = {}
    for d in pd_on:
        if d.isdigit() and int(d) < len(pd_start) and int(d) < len(pd_end):
            groups.setdefault((pd_start[int(d)], pd_end[int(d)]), []).append(d)
    if not groups:
        flash(request, "Tick at least one day.", "error")
        return redirect("/schedules/new?kind=default")
    parsed = []
    for (s, e), wd in groups.items():
        data, errors = _parse_schedule(request, label, door_ids, day, s, e, "weekly", wd, until, allowed, "default")
        if errors:
            for x in errors:
                flash(request, x, "error")
            return redirect("/schedules/new?kind=default")
        parsed.append(data)
    for data in parsed:
        ids = data.pop("door_ids")
        sched = Schedule(**data, on_holidays=on_holidays, notes=notes.strip(), created_by=user.id)
        session.add(sched)
        session.flush()
        calendars.set_schedule_doors(session, sched.id, ids)
        session.flush()
        after = audit.schedule_snapshot(session, sched)
        audit.record(session, "schedule.create", f"{user.display} added regular hours “{sched.label}”: "
                     f"{after['Doors']}, {after['When']}, {after['Time']}", user=user,
                     changes=audit.diff({}, after), door_ids=ids, schedule_id=sched.id)
    session.commit()
    kick_reconcile(bg)
    flash(request, f"Saved “{label.strip()}” ({len(parsed)} set{'' if len(parsed) == 1 else 's'} of hours).")
    return redirect("/")


@router.post("/schedules")
@router.post("/schedules/{sid}")
def schedule_save(request: Request, bg: BackgroundTasks, sid: Optional[int] = None,
                  label: str = Form(""), door_ids: list[str] = Form([]), day: str = Form(...),
                  start: str = Form(...), end: str = Form(...), repeat: str = Form("none"),
                  weekdays: list[str] = Form([]), until: str = Form(""), notes: str = Form(""),
                  kind: str = Form("override"), action: str = Form("unlock"), event_type: str = Form("event"),
                  contact: str = Form(""), mode: str = Form(""), holidays_field: str = Form(""),
                  follow_holidays: str = Form(""), perday: str = Form(""), pd_on: list[str] = Form([]),
                  pd_start: list[str] = Form([]), pd_end: list[str] = Form([]),
                  user: User = Depends(require_scheduler), session: Session = Depends(get_session)):
    # The form asks one question, "what do you want to do?": unlock / lock / default (regular hours)
    if mode in ("unlock", "lock"):
        kind, action = "override", mode
    elif mode == "default":
        kind, action = "default", "unlock"
    allowed = allowed_door_ids(session, user)
    sched, old_doors = None, []
    if sid is not None:
        sched = session.get(Schedule, sid)
        old_doors = schedule_doors(session, [sid]).get(sid, []) if sched else []
        if not sched or not can_edit(session, user, sched, schedule_doors(session, [sid])[sid]):
            flash(request, "You can't edit that schedule.", "error")
            return redirect("/schedules")
    on_holidays = ("skip" if follow_holidays else "run") if holidays_field else (sched.on_holidays if sched else "skip")
    if perday and kind == "default" and sched is None:
        return _save_perday(request, bg, session, user, label, door_ids, day, until, allowed, pd_on, pd_start, pd_end,
                            on_holidays, notes)
    data, errors = _parse_schedule(request, label, door_ids, day, start, end, repeat, weekdays, until, allowed,
                                   kind, action, event_type, contact)
    if errors:
        for e in errors:
            flash(request, e, "error")
        return redirect(f"/schedules/{sid}" if sid else "/schedules/new")
    ids = data.pop("door_ids")
    data["on_holidays"] = on_holidays
    before = audit.schedule_snapshot(session, sched) if sched else {}
    if sched is None:
        sched = Schedule(**data, notes=notes.strip(), created_by=user.id)
        session.add(sched)
        session.flush()
        verb = "schedule.create"
    else:
        for k, v in data.items():
            setattr(sched, k, v)
        sched.notes = notes.strip()
        sched.updated_by, sched.updated_at = user.id, utcnow()
        session.add(sched)
        verb = "schedule.update"
    calendars.set_schedule_doors(session, sched.id, ids)
    session.flush()
    after = audit.schedule_snapshot(session, sched)
    if verb == "schedule.create":
        audit.record(session, verb, f"{user.display} added {describe_kind(sched).lower()} “{sched.label}”: "
                     f"{after['Doors']}, {after['When']}, {after['Time']}", user=user,
                     changes=audit.diff({}, after), door_ids=ids, schedule_id=sched.id)
    else:
        changed = audit.diff(before, after)
        if changed:
            audit.record(session, verb, f"{user.display} changed “{sched.label}”: "
                         + ", ".join(c[0].lower() for c in changed), user=user, changes=changed,
                         door_ids=sorted(set(ids) | set(old_doors)), schedule_id=sched.id)
    session.commit()
    kick_reconcile(bg)
    flash(request, f"Saved “{sched.label}”.")
    return redirect("/")


@router.post("/schedules/{sid}/delete")
def schedule_delete(request: Request, sid: int, bg: BackgroundTasks, next: str = Form("/schedules"),
                    user: User = Depends(require_scheduler),
                    session: Session = Depends(get_session)):
    sched = session.get(Schedule, sid)
    door_ids = schedule_doors(session, [sid])[sid] if sched else []
    if sched and can_edit(session, user, sched, door_ids):
        snap = audit.schedule_snapshot(session, sched)
        audit.record(session, "schedule.delete", f"{user.display} deleted “{sched.label}” ({snap['Doors']})",
                     user=user, changes=[[k, v, ""] for k, v in snap.items() if v], door_ids=door_ids,
                     schedule_id=sched.id)
        calendars.delete_schedule(session, sched)
        session.commit()
        kick_reconcile(bg)
        flash(request, f"Deleted “{sched.label}”.")
    return redirect(safe_next(next))


# ---------- doors ----------

@router.get("/doors")
def doors_page(request: Request, user: User = Depends(require_user), session: Session = Depends(get_session)):
    site_id = current_site_id(request, session, user)
    visible = visible_site_ids(session, user)
    if site_id is None and len(visible) > 1:
        return render(request, "doors_sites.html", user, summaries=site_summaries(session, user, visible))
    if user.is_admin:
        doors = list(session.exec(select(Door).where(Door.hidden == False)  # noqa: E712
                                  .order_by(Door.site_id, Door.name)).all())
    else:
        doors = doors_for(session, user)
    doors = [d for d in doors if site_id is None or d.site_id == site_id]
    status = door_status(session, doors)
    # each door's next event (override) in the coming 30 days
    nextev: dict[str, object] = {}
    now = now_local()
    for door_id, scheds in engine.door_schedules(session, [d.id for d in doors]).items():
        occ = expand([x for x in scheds if x.kind == "override"], now, now + timedelta(days=30))
        if occ:
            nextev[door_id] = occ[0]
    state = {}
    for d in doors:
        st = status.get(d.id, {})
        state[d.id] = "error" if st.get("error") else (st.get("mode") or "normal")
    usable = [d for d in doors if d.present]
    counts = {
        "unlock": sum(1 for d in usable if state[d.id] == "unlock"),
        "lock": sum(1 for d in usable if state[d.id] == "lock"),
        "error": sum(1 for d in usable if state[d.id] == "error"),
        "open": sum(1 for d in usable if d.position_status == "open"),
    }
    managed = managed_site_ids(session, user)
    em_sites = [x for x in session.exec(select(Site).order_by(Site.name)).all()
                if x.id in managed and (site_id is None or x.id == site_id)]
    return render(request, "doors.html", user, doors=doors, status=status, state=state, em_sites=em_sites,
                  counts=counts, nextev=nextev, now=now, site_id=site_id,
                  multi_site=len(visible) > 1, default_until=_default_until(now),
                  view=user.door_view or ("list" if user.schedules else "tiles"))


@router.post("/doors/sync")
def doors_sync(request: Request, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    engine.sync_doors()
    for site in session.exec(select(Site).where(Site.enabled == True)).all():  # noqa: E712
        session.refresh(site)
        flash(request, f"{site.name}: {site.last_sync_status}", "error" if site.last_sync_status.startswith("Error") else "ok")
    return redirect("/doors")


@router.get("/doors/{door_id}")
def door_page(request: Request, door_id: str, user: User = Depends(require_user),
              session: Session = Depends(get_session)):
    door = session.get(Door, door_id)
    if not door or door_id not in allowed_door_ids(session, user):
        flash(request, "That door isn't available to you.", "error")
        return redirect("/doors")
    site = session.get(Site, door.site_id)
    tz = engine.site_tz(site)
    scheds = engine.door_schedules(session, [door.id]).get(door.id, [])
    now = utcnow()
    days: dict[date, list] = {}
    for seg in engine.door_timeline(session, door, now, now + timedelta(days=7), scheds):
        start, end = to_local(seg.start, tz), to_local(seg.end, tz)
        days.setdefault(start.date(), []).append({"seg": seg, "start": start, "end": end})
    sd = schedule_doors(session, [x.id for x in scheds])
    today = now_local(tz).date()
    listed = sorted([x for x in scheds if series_last_date(x) >= today - timedelta(days=1)],
                    key=lambda x: (x.kind != "default", x.start_local))
    rows = [{"s": x, "editable": can_edit(session, user, x, sd[x.id])} for x in listed]
    from . import protect
    cams, cam_error = [], ""
    if door.site_id in managed_site_ids(session, user) and protect.enabled(site):
        try:
            cams = protect.cameras(site)
        except protect.ProtectError as e:
            cam_error = str(e)
    from .activity import recent_for_door
    return render(request, "door.html", user, door=door, site=site, days=days, rows=rows,
                  openings=recent_for_door(session, door),
                  cams=cams, cam_error=cam_error, protect_on=protect.enabled(site),
                  can_history=user.is_manager and door.site_id in managed_site_ids(session, user),
                  history=audit.for_door(session, door.id), audit_mod=audit,
                  status=door_status(session, [door]).get(door.id, {}))


# ---------- sites (admin) ----------

def site_summaries(session: Session, user: User, site_ids) -> list[dict]:
    """Per site: doors, buildings and what's happening, for site cards."""
    out = []
    allowed = allowed_door_ids(session, user)
    for site in session.exec(select(Site).where(Site.id.in_(list(site_ids))).order_by(Site.name)).all():
        doors = [d for d in session.exec(select(Door).where(Door.site_id == site.id, Door.present == True,  # noqa: E712
                                                            Door.hidden == False)).all()  # noqa: E712
                 if d.id in allowed]
        st = door_status(session, doors)
        blds = session.exec(select(Building).where(Building.site_id == site.id).order_by(Building.name)).all()
        out.append({
            "site": site, "doors": len(doors), "buildings": blds,
            "bcount": {b.id: sum(1 for d in doors if d.building_id == b.id) for b in blds},
            "unlocked": sum(1 for v in st.values() if v.get("mode") == "unlock"),
            "locked": sum(1 for v in st.values() if v.get("mode") == "lock"),
            "problems": sum(1 for v in st.values() if v.get("error")),
        })
    return out


COMMON_TZ = ["America/New_York", "America/Chicago", "America/Denver", "America/Phoenix", "America/Los_Angeles",
             "America/Anchorage", "Pacific/Honolulu", "America/Toronto", "Europe/London", "Europe/Berlin",
             "Australia/Sydney", "UTC"]


@router.get("/sites")
def sites_page(request: Request, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    return render(request, "sites.html", user, summaries=site_summaries(session, user, managed_site_ids(session, user)))


@router.get("/sites/new")
def site_new(request: Request, user: User = Depends(require_admin)):
    return render(request, "site_form.html", user, site=None, timezones=COMMON_TZ)


@router.get("/sites/{site_id}")
def site_page(request: Request, site_id: int, user: User = Depends(require_manager),
              session: Session = Depends(get_session)):
    """A site: its buildings and doors (admins and its site admins), and its console connection (admins)."""
    site = session.get(Site, site_id)
    if not site or site_id not in managed_site_ids(session, user):
        flash(request, "That site isn't available to you.", "error")
        return redirect("/sites")
    buildings = list(session.exec(select(Building).where(Building.site_id == site.id).order_by(Building.name)).all())
    doors = list(session.exec(select(Door).where(Door.site_id == site.id, Door.present == True, Door.hidden == False)  # noqa: E712
                              .order_by(Door.name)).all())
    counts = {b.id: sum(1 for d in doors if d.building_id == b.id) for b in buildings}
    from .models import DoorPin, FloorPlan
    plans = session.exec(select(FloorPlan).where(FloorPlan.building_id.in_([b.id for b in buildings]))
                         .order_by(FloorPlan.name)).all() if buildings else []
    plans_by_b: dict[int, list] = {}
    for fp in plans:
        plans_by_b.setdefault(fp.building_id, []).append(fp)
    pin_counts = dict(session.exec(select(DoorPin.plan_id, func.count()).group_by(DoorPin.plan_id)).all())
    hidden = list(session.exec(select(Door).where(Door.site_id == site.id, Door.hidden == True)  # noqa: E712
                               .order_by(Door.name)).all())
    return render(request, "site.html", user, site=site, buildings=buildings, doors=doors, counts=counts,
                  timezones=COMMON_TZ, plans_by_b=plans_by_b, pin_counts=pin_counts, hidden_doors=hidden)


@router.post("/sites")
@router.post("/sites/{site_id}")
def site_save(request: Request, bg: BackgroundTasks, site_id: Optional[int] = None, name: str = Form(...),
              host: str = Form(""), token: str = Form(""), timezone: str = Form("America/Chicago"),
              verify_tls: bool = Form(False), enabled: bool = Form(False), protect_key: str = Form(""),
              protect_host: str = Form(""), protect_clear: bool = Form(False), user: User = Depends(require_admin),
              session: Session = Depends(get_session)):
    from zoneinfo import ZoneInfo
    back = f"/sites/{site_id}" if site_id else "/sites/new"
    try:
        ZoneInfo(timezone.strip())
    except Exception:
        flash(request, f"Unknown timezone “{timezone}”. Use a name like America/Chicago.", "error")
        return redirect(back)
    host = host.strip().rstrip("/")
    if host and host != "demo" and not host.startswith(("http://", "https://")):
        host = "https://" + host
    site = session.get(Site, site_id) if site_id else Site(name=name)
    before = audit.site_snapshot(site) if site_id else {}
    if not site_id and not token.strip() and host != "demo":
        flash(request, "Enter the console's API token.", "error")
        return redirect(back)
    site.name, site.host, site.timezone = name.strip(), host or "demo", timezone.strip()
    site.verify_tls, site.enabled = verify_tls, enabled if site_id else True
    if token.strip():
        site.token = token.strip()
    site.protect_host = protect_host.strip().rstrip("/")
    if protect_clear:
        site.protect_key = ""
    elif protect_key.strip():
        site.protect_key = protect_key.strip()
    session.add(site)
    session.flush()
    changed = audit.diff(before, audit.site_snapshot(site))
    if token.strip() and site_id:
        changed.append(["API token", "(hidden)", "(new token)"])
    if protect_key.strip() or protect_clear:
        changed.append(["UniFi Protect key", "(hidden)", "(removed)" if protect_clear else "(new key)"])
    if changed or not site_id:
        audit.record(session, "site.update" if site_id else "site.create",
                     f"{user.display} {'changed' if site_id else 'added'} site “{site.name}”",
                     user=user, changes=changed, site_ids=[site.id] if site.id else None)
    session.commit()
    status = engine.sync_site(site.id) if site.enabled else "Saved (disabled)."
    kick_reconcile(bg)
    flash(request, f"Saved {site.name}. {status}", "error" if status.startswith("Error") else "ok")
    return redirect(f"/sites/{site.id}")


@router.post("/sites/{site_id}/emergency")
def site_emergency(request: Request, site_id: int, bg: BackgroundTasks, mode: str = Form(...), next: str = Form("/"),
                   user: User = Depends(require_manager), session: Session = Depends(get_session)):
    """Lockdown (every door held locked), evacuation (every door held unlocked) or back to normal."""
    site = session.get(Site, site_id)
    if not site or site_id not in managed_site_ids(session, user) or mode not in {"lockdown", "evacuation", "normal"}:
        flash(request, "Only admins and site admins can do that.", "error")
        return redirect(safe_next(next))
    try:
        unifi.get_client(site).set_emergency(mode)
    except unifi.UnifiError as e:
        flash(request, f"UniFi Access didn't accept it: {e}", "error")
        return redirect(safe_next(next))
    was = site.emergency
    site.emergency = "" if mode == "normal" else mode
    site.emergency_by, site.emergency_at = (user.display, utcnow()) if site.emergency else ("", None)
    session.add(site)
    if mode == "normal":
        engine.forget_states(session, site.id)
        msg = f"{user.display} ended {was or 'the emergency'} at {site.name}"
    else:
        msg = f"{user.display} started {mode} at {site.name}"
    audit.record(session, "site.emergency", msg, user=user, site_ids=[site.id])
    session.commit()
    kick_reconcile(bg)
    flash(request, {"lockdown": f"Lockdown started at {site.name}. Every door is held locked until you end it.",
                    "evacuation": f"Evacuation started at {site.name}. Every door is held unlocked until you end it.",
                    "normal": f"{(was or 'Emergency').capitalize()} ended at {site.name}. Doors are back on their schedules."}[mode],
          "error" if mode != "normal" else "ok")
    return redirect(safe_next(next))


@router.post("/sites/{site_id}/delete")
def site_delete(request: Request, site_id: int, user: User = Depends(require_admin),
                session: Session = Depends(get_session)):
    site = session.get(Site, site_id)
    if not site:
        return redirect("/sites")
    door_ids = list(session.exec(select(Door.id).where(Door.site_id == site_id)).all())
    for row in session.exec(select(UserDoor).where(UserDoor.door_id.in_(door_ids))).all():
        session.delete(row)
    touched = set()
    for row in session.exec(select(ScheduleDoor).where(ScheduleDoor.door_id.in_(door_ids))).all():
        touched.add(row.schedule_id)
        session.delete(row)
    from .maps import delete_building_plans
    from .models import DoorPin
    for row in session.exec(select(DoorPin).where(DoorPin.door_id.in_(door_ids))).all():
        session.delete(row)
    for b in session.exec(select(Building).where(Building.site_id == site_id)).all():
        delete_building_plans(session, b.id)
    for row in session.exec(select(DoorState).where(DoorState.door_id.in_(door_ids))).all():
        if row.mode:  # release anything OpnDoor is holding on that console
            try:
                from . import unifi
                unifi.get_client(site).reset(session.get(Door, row.door_id).remote_id)
            except Exception as e:
                flash(request, f"Couldn't reset {row.door_id} on the console: {e}", "error")
        session.delete(row)
    session.flush()
    for sid in touched:  # schedules left with no doors
        if not session.exec(select(ScheduleDoor).where(ScheduleDoor.schedule_id == sid)).first():
            session.delete(session.get(Schedule, sid))
    session.flush()
    for d in door_ids:
        session.delete(session.get(Door, d))
    session.flush()  # doors must go before their site (foreign key)
    for b in session.exec(select(Building).where(Building.site_id == site_id)).all():
        session.delete(b)
    session.flush()
    audit.record(session, "site.delete", f"{user.display} removed site “{site.name}” and its {len(door_ids)} doors",
                 user=user, site_ids=[site.id])
    session.delete(site)
    session.commit()
    flash(request, f"Removed {site.name} and its doors. Doors were left on their normal UniFi rules.")
    return redirect("/sites")


# ---------- buildings (admins and site admins) ----------

def _building_site(request: Request, session: Session, user: User, site: str = "") -> Optional[Site]:
    managed = managed_site_ids(session, user)
    sid = int(site) if site.isdigit() else (current_site_id(request, session, user) or (min(managed) if managed else None))
    return session.get(Site, sid) if sid in managed else None


@router.get("/buildings")
def buildings_page(request: Request, site: str = "", user: User = Depends(require_manager),
                   session: Session = Depends(get_session)):
    """Buildings are managed on each site's page now."""
    st = _building_site(request, session, user, site)
    return redirect(f"/sites/{st.id}#buildings" if st else "/sites")


@router.post("/buildings")
async def buildings_save(request: Request, user: User = Depends(require_manager),
                         session: Session = Depends(get_session)):
    form = await request.form()
    st = _building_site(request, session, user, str(form.get("site", "")))
    if not st:
        return redirect("/sites")
    back = f"/sites/{st.id}#buildings"
    buildings = {b.id: b for b in session.exec(select(Building).where(Building.site_id == st.id)).all()}
    changes = []
    # renames and deletes
    for bid, b in list(buildings.items()):
        if form.get(f"delete_{bid}") == "on":
            from .maps import delete_building_plans
            delete_building_plans(session, bid)
            for d in session.exec(select(Door).where(Door.building_id == bid)).all():
                d.building_id = None
                session.add(d)
            session.flush()
            session.delete(b)
            changes.append([f"Building “{b.name}”", "existed", "removed"])
            del buildings[bid]
            continue
        new = str(form.get(f"name_{bid}", b.name)).strip()
        if new and new != b.name:
            changes.append(["Building name", b.name, new])
            b.name = new
            session.add(b)
    # new buildings
    for raw in str(form.get("new_buildings", "")).replace(",", "\n").splitlines():
        name = raw.strip()
        if name and name.lower() not in {b.name.lower() for b in buildings.values()}:
            b = Building(site_id=st.id, name=name)
            session.add(b)
            session.flush()
            buildings[b.id] = b
            changes.append(["New building", "", name])
    # door assignments: door_<door id> = building id or ""
    for d in session.exec(select(Door).where(Door.site_id == st.id)).all():
        key = f"door_{d.id}"
        if key not in form:
            continue
        val = str(form.get(key, ""))
        target = int(val) if val.isdigit() and int(val) in buildings else None
        if target != d.building_id:
            old = buildings[d.building_id].name if d.building_id in buildings else "—"
            changes.append([d.name, old, buildings[target].name if target else "—"])
            d.building_id = target
            session.add(d)
    if changes:
        audit.record(session, "building.update", f"{user.display} changed buildings at {st.name}", user=user,
                     changes=changes[:60], site_ids=[st.id])
    session.commit()
    flash(request, "Buildings saved." if changes else "Nothing changed.")
    return redirect(back)


@router.post("/buildings/auto")
def buildings_auto(request: Request, site: str = Form(""), part: int = Form(0), user: User = Depends(require_manager),
                   session: Session = Depends(get_session)):
    """Create buildings from UniFi's names ("Building - Floor - Door") and put unassigned doors in them."""
    st = _building_site(request, session, user, site)
    if not st:
        return redirect("/sites")
    buildings = {b.name.lower(): b for b in session.exec(select(Building).where(Building.site_id == st.id)).all()}
    moved = 0
    for d in session.exec(select(Door).where(Door.site_id == st.id, Door.building_id == None)).all():  # noqa: E711
        parts = [p.strip() for p in (d.full_name or "").split(" - ")]
        if len(parts) < 3 or not parts[part]:
            continue
        name = parts[part]
        b = buildings.get(name.lower())
        if not b:
            b = Building(site_id=st.id, name=name)
            session.add(b)
            session.flush()
            buildings[name.lower()] = b
        d.building_id = b.id
        session.add(d)
        moved += 1
    if moved:
        audit.record(session, "building.update", f"{user.display} grouped {moved} doors into buildings at {st.name} "
                     f"using UniFi's {'building' if part == 0 else 'floor'} names", user=user, site_ids=[st.id])
    session.commit()
    flash(request, f"Put {moved} doors into buildings. Check them below and adjust if needed." if moved
          else "No doors to group: they're already in buildings, or UniFi's names don't include that part.")
    return redirect(f"/sites/{st.id}#buildings")


# ---------- people (admins and site admins) ----------

def people_for(session: Session, manager: User) -> list[User]:
    users = session.exec(select(User).order_by(User.active.desc(), User.is_admin.desc(),
                                               User.is_site_admin.desc(), User.name)).all()
    return [u for u in users if can_manage_person(session, manager, u) or u.id == manager.id or manager.is_admin]


@router.get("/users")
def users_page(request: Request, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    managed = managed_site_ids(session, user)
    names = {d.id: (d.name, d.site_id) for d in session.exec(select(Door)).all()}
    site_names = {x.id: x.name for x in session.exec(select(Site)).all()}
    users = people_for(session, user)
    access: dict[int, list[str]] = {}
    elsewhere: set[int] = set()
    for us in session.exec(select(UserSite)).all():
        if us.site_id in managed:
            access.setdefault(us.user_id, []).append(f"All doors at {site_names.get(us.site_id, '?')}")
        else:
            elsewhere.add(us.user_id)
    for ud in session.exec(select(UserDoor)).all():
        name, site_id = names.get(ud.door_id, (ud.door_id, None))
        if site_id in managed:
            access.setdefault(ud.user_id, []).append(name)
        else:
            elsewhere.add(ud.user_id)
    from .models import UnifiGrant
    unifi_counts: dict[int, int] = {}
    for g in session.exec(select(UnifiGrant)).all():
        unifi_counts[g.user_id] = unifi_counts.get(g.user_id, 0) + 1
    return render(request, "users.html", user, users=users, access=access, elsewhere=elsewhere,
                  unifi_counts=unifi_counts, any_unifi=any(u.follows_unifi for u in users),
                  editable={u.id for u in users if can_manage_person(session, user, u)},
                  redirect_url=sso_redirect(request))


def sso_redirect(request: Request) -> str:
    from .sso import redirect_uri
    return redirect_uri(request)


@router.get("/users/{uid}")
@router.get("/users/new")
def user_form(request: Request, uid: Optional[int] = None, user: User = Depends(require_manager),
              session: Session = Depends(get_session)):
    target = session.get(User, uid) if uid else None
    if uid and (not target or not can_manage_person(session, user, target)):
        flash(request, "Only an admin can change that person.", "error")
        return redirect("/users")
    managed = managed_site_ids(session, user)
    sel_doors = set(session.exec(select(UserDoor.door_id).where(UserDoor.user_id == uid)).all()) if uid else set()
    sel_sites = set(session.exec(select(UserSite.site_id).where(UserSite.user_id == uid)).all()) if uid else set()
    sites = {k: v for k, v in session.exec(select(Site.id, Site)).all() if k in managed}
    unifi_doors = []
    if target and target.follows_unifi:
        from .models import UnifiGrant
        unifi_doors = sorted(d.name for d in session.exec(select(Door).join(UnifiGrant, UnifiGrant.door_id == Door.id)
                                                          .where(UnifiGrant.user_id == target.id)).all())
    return render(request, "user_form.html", user, target=target, sites=sites, unifi_doors=unifi_doors,
                  doors=[d for d in all_doors(session) if d.site_id in managed],
                  selected=sel_doors, selected_sites=sel_sites,
                  full=target is None or fully_managed(session, user, target),
                  elsewhere=bool(target and person_site_ids(session, target) - managed))


@router.post("/users/unifi-check")
def users_unifi_check(request: Request, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    from . import access
    access.sync(session)
    session.commit()
    flash(request, "Checked everyone's UniFi Access permissions.")
    return redirect("/users")


@router.post("/users")
@router.post("/users/{uid}")
def user_save(request: Request, uid: Optional[int] = None, name: str = Form(""), email: str = Form(...),
              role: str = Form("operator"), signin: str = Form("microsoft"), password: str = Form(""),
              username: str = Form(""), door_source: str = Form("opndoor"), start_page: str = Form(""),
              active: bool = Form(False), door_ids: list[str] = Form([]), site_ids: list[int] = Form([]),
              user: User = Depends(require_manager), session: Session = Depends(get_session)):
    email = email.strip().lower()
    back = f"/users/{uid}" if uid else "/users/new"
    managed = managed_site_ids(session, user)
    if not user.is_admin and role not in {"operator", "opener"}:
        role = "operator"  # site admins add and change door operators and openers only
    role = role if role in {"admin", "site_admin", "operator", "opener"} else "operator"
    door_source = "unifi" if door_source == "unifi" and role in {"operator", "opener"} else "opndoor"
    use_password = signin == "password" or not config.SSO_ENABLED

    target = session.get(User, uid) if uid else None
    if uid and (not target or not can_manage_person(session, user, target)):
        flash(request, "Only an admin can change that person.", "error")
        return redirect("/users")
    clash = session.exec(select(User).where(User.email == email)).first()
    if clash and clash.id != uid:
        if target is None and not user.is_admin and not clash.is_admin and not clash.is_site_admin:
            # A door operator who already exists (e.g. works at another site): a site admin
            # "adding" them just gives them access at the site admin's sites.
            target = clash
        else:
            flash(request, "Another person already has that email." + (" Find them in the list and edit them."
                  if user.is_admin else ""), "error")
            return redirect(back)
    full = target is None or fully_managed(session, user, target)
    import re
    username = username.strip().lower()
    if username and full:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,31}", username):
            flash(request, "Usernames are 2–32 letters, numbers, dots, dashes or underscores (no spaces or @).", "error")
            return redirect(back)
        taken = session.exec(select(User).where(User.username == username)).first()
        if taken and (target is None or taken.id != target.id):
            flash(request, f"The username “{username}” is already taken.", "error")
            return redirect(back)

    if target and target.id == user.id and user.is_admin and (role != "admin" or not active):
        flash(request, "You can't remove your own admin role or turn off your own account.", "error")
        return redirect(back)
    if role == "site_admin" and not site_ids:
        flash(request, "Pick at least one site for the site admin to manage.", "error")
        return redirect(back)
    if full:
        if use_password and password and len(password) < 8:
            flash(request, "Passwords need at least 8 characters.", "error")
            return redirect(back)
        if use_password and not password and not (target and target.password_hash):
            flash(request, "Set a password (at least 8 characters), or choose Microsoft sign-in.", "error")
            return redirect(back)

    before = audit.user_snapshot(session, target, managed if not user.is_admin else None) if target else {}
    sites_before = person_site_ids(session, target) if target else set()
    if target is None:
        target = User(email=email, created_by=user.id, active=True)
        active = True
    if full:
        target.name, target.email, target.active = name.strip(), email, active
        target.username = username or None
        if not use_password:
            target.password_hash = ""
        elif password:
            target.password_hash = hash_password(password)
    if target.is_owner:
        role = "admin"  # the main admin always stays an admin
    target.is_admin, target.is_site_admin = role == "admin", role == "site_admin"
    target.can_schedule = role != "opener"
    if full:
        target.door_source = door_source
        target.start_page = start_page if start_page in START_PAGES else ""
    session.add(target)
    session.flush()

    # Access. Admins need none. Grants outside the manager's sites are kept as they are.
    for row in session.exec(select(UserSite).where(UserSite.user_id == target.id)).all():
        if row.site_id in managed:
            session.delete(row)
    for row in session.exec(select(UserDoor).where(UserDoor.user_id == target.id)).all():
        door = session.get(Door, row.door_id)
        if not door or door.site_id in managed:
            session.delete(row)
    session.flush()
    if role == "site_admin" or (role != "admin" and not target.follows_unifi):
        whole = set(site_ids) & managed
        for sid in whole:
            session.add(UserSite(user_id=target.id, site_id=sid))
        if role in {"operator", "opener"} and not target.follows_unifi:
            for d in set(door_ids):
                door = session.get(Door, d)
                if door and door.site_id in managed and door.site_id not in whole:
                    session.add(UserDoor(user_id=target.id, door_id=d))
    session.flush()
    if target.follows_unifi:
        from . import access
        try:
            access.sync(session, [target])
        except Exception as e:  # noqa: BLE001 - never lose the save over a lookup
            target.unifi_note = f"Couldn't check UniFi Access yet: {e}"

    after = audit.user_snapshot(session, target, managed if not user.is_admin else None)
    changed = audit.diff(before, after)
    if password and before and use_password and full:
        changed.append(["Password", "", "(reset)"])
    if changed:
        audit.record(session, "user.update" if before else "user.create",
                     f"{user.display} {'changed' if before else 'added'} {target.display} ({target.role_name})",
                     user=user, changes=changed,
                     site_ids=(sites_before | person_site_ids(session, target)) or None)
    session.commit()
    flash(request, f"Saved {target.display}.")
    return redirect("/users")


# ---------- calendars (admins and site admins) ----------

def feed_door_ids(session: Session, feed: CalendarFeed) -> set[str]:
    ids = set(calendars.split_ids(feed.default_door_ids))
    for r in session.exec(select(CalendarRule).where(CalendarRule.feed_id == feed.id)).all():
        ids |= set(calendars.split_ids(r.door_ids))
    return ids


def can_manage_feed(session: Session, user: User, feed: Optional[CalendarFeed]) -> bool:
    """Admins: any calendar. Site admins: calendars that only use doors at their sites."""
    if user.is_admin:
        return True
    if not user.is_site_admin or feed is None:
        return False
    ids = feed_door_ids(session, feed)
    return bool(ids) and ids <= allowed_door_ids(session, user)


@router.get("/calendars")
def calendars_page(request: Request, user: User = Depends(require_manager), session: Session = Depends(get_session)):
    feeds = [f for f in session.exec(select(CalendarFeed).order_by(CalendarFeed.name)).all()
             if can_manage_feed(session, user, f)]
    counts = dict(session.exec(select(Schedule.feed_id, func.count()).where(Schedule.feed_id != None)  # noqa: E711
                               .group_by(Schedule.feed_id)).all())
    mine = {f.id: f for f in feeds}
    waiting = [x for x in session.exec(select(Schedule).where(Schedule.status.in_(["pending", "skipped"]))
                                       .order_by(Schedule.start_local)).all()
               if x.feed_id in mine and x.start_local + timedelta(minutes=x.duration_minutes) > now_local()]
    sd = schedule_doors(session, [x.id for x in waiting])
    namer = DoorNamer(session, all_doors(session))
    rows = [{"s": x, "feed": mine[x.feed_id], "doors": namer(sd.get(x.id, []), limit=4)} for x in waiting]
    return render(request, "calendars.html", user, feeds=feeds, counts=counts,
                  pending=[r for r in rows if r["s"].status == "pending"],
                  skipped=[r for r in rows if r["s"].status == "skipped"])


@router.post("/calendars/approve")
def calendars_approve(request: Request, bg: BackgroundTasks, ids: list[int] = Form([]), action: str = Form("approve"),
                      user: User = Depends(require_manager), session: Session = Depends(get_session)):
    """Approve calendar events that are waiting (they start unlocking doors) or skip them."""
    done = []
    for sid in ids:
        s = session.get(Schedule, sid)
        feed = session.get(CalendarFeed, s.feed_id) if s and s.feed_id else None
        if not s or not feed or not can_manage_feed(session, user, feed) or s.status not in ("pending", "skipped"):
            continue
        s.status = "" if action == "approve" else "skipped"
        session.add(s)
        done.append(s.label)
    if done:
        verb = "approved" if action == "approve" else "skipped"
        audit.record(session, "calendar.approve", f"{user.display} {verb} {len(done)} calendar event"
                     f"{'' if len(done) == 1 else 's'}: " + ", ".join(done[:5]) + (" …" if len(done) > 5 else ""),
                     user=user)
        session.commit()
        kick_reconcile(bg)
        flash(request, f"{verb.capitalize()} {len(done)} event{'' if len(done) == 1 else 's'}.")
    return redirect("/calendars")


@router.get("/calendars/{fid}")
@router.get("/calendars/new")
def calendar_form(request: Request, fid: Optional[int] = None, user: User = Depends(require_manager),
                  session: Session = Depends(get_session)):
    feed = session.get(CalendarFeed, fid) if fid else None
    if fid and not can_manage_feed(session, user, feed):
        return redirect("/calendars")
    rules = session.exec(select(CalendarRule).where(CalendarRule.feed_id == fid)
                         .order_by(CalendarRule.position)).all() if fid else []
    doors = doors_for(session, user)
    upcoming = []
    if feed:
        now = now_local()
        scheds = session.exec(select(Schedule).where(Schedule.feed_id == fid)).all()
        sd = schedule_doors(session, [s.id for s in scheds])
        names = {d.id: d.name for d in doors}
        upcoming = [{"o": o, "doors": [names.get(d, d) for d in sd[o.schedule.id]]}
                    for o in expand(scheds, now, now + timedelta(days=config.CALENDAR_LOOKAHEAD_DAYS))][:50]
    return render(request, "calendar_form.html", user, feed=feed, rules=rules, doors=doors,
                  defaults=set(calendars.split_ids(feed.default_door_ids)) if feed else set(),
                  upcoming=upcoming, preview=None)


async def _rules_from_form(request: Request) -> list[dict]:
    form = await request.form()
    rules = []
    for i in range(50):
        contains = str(form.get(f"rule_{i}_contains", "")).strip()
        if f"rule_{i}_field" not in form:
            continue
        doors = [str(d) for d in form.getlist(f"rule_{i}_doors")]
        if contains and doors:
            rules.append({"field": str(form.get(f"rule_{i}_field", "any")), "contains": contains,
                          "door_ids": ",".join(doors), "position": len(rules)})
    return rules


@router.post("/calendars")
@router.post("/calendars/{fid}")
async def calendar_save(request: Request, fid: Optional[int] = None, user: User = Depends(require_manager),
                        session: Session = Depends(get_session)):
    form = await request.form()
    if fid and not can_manage_feed(session, user, session.get(CalendarFeed, fid)):
        return redirect("/calendars")
    mine = allowed_door_ids(session, user)
    name, url = str(form.get("name", "")).strip(), str(form.get("url", "")).strip()
    if not name or not url.startswith(("http://", "https://", "webcal://")):
        flash(request, "Give the calendar a name and an http(s) or webcal ICS link.", "error")
        return redirect(f"/calendars/{fid}" if fid else "/calendars/new")
    feed = session.get(CalendarFeed, fid) if fid else CalendarFeed(name=name, url=url)
    feed.name, feed.url = name, url
    feed.enabled = form.get("enabled") == "on"
    feed.include_all_day = form.get("include_all_day") == "on"
    feed.approve_new = form.get("approve_new") == "on"
    try:
        feed.approve_over_hours = max(0, int(form.get("approve_over_hours") or 0))
    except ValueError:
        feed.approve_over_hours = 0
    feed.approve_before = str(form.get("approve_before") or "")[:5]
    feed.approve_after = str(form.get("approve_after") or "")[:5]
    feed.default_door_ids = ",".join(str(d) for d in form.getlist("default_doors") if str(d) in mine)
    try:
        feed.lead_minutes = max(0, int(form.get("lead_minutes") or 0))
        feed.lag_minutes = max(0, int(form.get("lag_minutes") or 0))
    except ValueError:
        flash(request, "Before/after minutes must be numbers.", "error")
        return redirect(f"/calendars/{fid}" if fid else "/calendars/new")
    session.add(feed)
    session.flush()
    for r in session.exec(select(CalendarRule).where(CalendarRule.feed_id == feed.id)).all():
        session.delete(r)
    for r in await _rules_from_form(request):
        r["door_ids"] = ",".join(d for d in r["door_ids"].split(",") if d in mine)
        if r["door_ids"]:
            session.add(CalendarRule(feed_id=feed.id, **r))
    session.flush()
    if not user.is_admin and not feed_door_ids(session, feed):
        flash(request, "Pick at least one door for this calendar.", "error")
        session.rollback()
        return redirect(f"/calendars/{fid}" if fid else "/calendars/new")
    audit.record(session, "calendar.update" if fid else "calendar.create",
                 f"{user.display} {'changed' if fid else 'connected'} calendar “{feed.name}”", user=user,
                 door_ids=sorted(set(calendars.split_ids(feed.default_door_ids))
                                 | {d for r in await _rules_from_form(request) for d in r["door_ids"].split(",")}))
    session.commit()
    status = (await run_in_threadpool(calendars.sync_feed, feed.id) if feed.enabled
              else "Saved (disabled, not synced).")
    await run_in_threadpool(engine.safe(engine.reconcile))
    flash(request, f"Saved “{feed.name}”. {status}", "error" if status.startswith("Error") else "ok")
    return redirect(f"/calendars/{feed.id}")


@router.post("/calendars/{fid}/sync")
def calendar_sync(request: Request, fid: int, bg: BackgroundTasks, user: User = Depends(require_manager),
                  session: Session = Depends(get_session)):
    if not can_manage_feed(session, user, session.get(CalendarFeed, fid)):
        return redirect("/calendars")
    status = calendars.sync_feed(fid)
    kick_reconcile(bg)
    flash(request, status, "error" if status.startswith("Error") else "ok")
    return redirect(f"/calendars/{fid}")


@router.post("/calendars/{fid}/preview")
def calendar_preview(request: Request, fid: int, user: User = Depends(require_manager),
                     session: Session = Depends(get_session)):
    feed = session.get(CalendarFeed, fid)
    if not feed or not can_manage_feed(session, user, feed):
        return redirect("/calendars")
    try:
        bookings = calendars.plan_feed(session, feed, calendars.fetch_ics(feed.url))
    except Exception as e:
        flash(request, f"Couldn't read the calendar: {e}", "error")
        return redirect(f"/calendars/{fid}")
    doors = doors_for(session, user)
    names = {d.id: d.name for d in all_doors(session)}
    preview = [{"b": b, "doors": [names[d] for d in b.door_ids]} for b in bookings]
    rules = session.exec(select(CalendarRule).where(CalendarRule.feed_id == fid).order_by(CalendarRule.position)).all()
    return render(request, "calendar_form.html", user, feed=feed, rules=rules, doors=doors,
                  defaults=set(calendars.split_ids(feed.default_door_ids)), upcoming=[], preview=preview)


@router.post("/calendars/{fid}/delete")
def calendar_delete(request: Request, fid: int, bg: BackgroundTasks, user: User = Depends(require_manager),
                    session: Session = Depends(get_session)):
    feed = session.get(CalendarFeed, fid)
    if feed and can_manage_feed(session, user, feed):
        for s in session.exec(select(Schedule).where(Schedule.feed_id == fid)).all():
            calendars.delete_schedule(session, s)
        for r in session.exec(select(CalendarRule).where(CalendarRule.feed_id == fid)).all():
            session.delete(r)
        session.flush()  # schedules and rules must go before the calendar (foreign key)
        session.delete(feed)
        audit.record(session, "calendar.delete", f"{user.display} disconnected calendar “{feed.name}”", user=user)
        session.commit()
        kick_reconcile(bg)
        flash(request, f"Removed “{feed.name}” and its schedules.")
    return redirect("/calendars")
