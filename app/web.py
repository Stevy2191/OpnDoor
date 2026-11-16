"""HTML routes."""
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from sqlmodel import Session, func, select

from . import calendars, config, engine
from .auth import (AdminRequired, LoginRequired, allowed_door_ids, current_user,
                   hash_password, verify_password)
from .db import get_session
from .models import (AuditLog, CalendarFeed, CalendarRule, Door, DoorState, Schedule,
                     ScheduleDoor, User, UserDoor, utcnow)
from .schedules import (WEEKDAYS, describe_repeat, expand, now_local, series_last_date,
                        to_local, weekdays_of)

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
templates.env.globals.update(span=_span, minutes=lambda m: timedelta(minutes=m), WEEKDAYS=WEEKDAYS, describe_repeat=describe_repeat,
                             config=config)


# ---------- helpers ----------

def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, msg])


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def render(request: Request, name: str, user: Optional[User], **ctx) -> HTMLResponse:
    msgs = request.session.pop("flash", [])
    return templates.TemplateResponse(request, name, {"user": user, "flashes": msgs, **ctx})


def require_user(request: Request, session: Session = Depends(get_session)) -> User:
    user = current_user(request, session)
    if not user:
        raise LoginRequired()
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise AdminRequired()
    return user


def doors_for(session: Session, user: User) -> list[Door]:
    allowed = allowed_door_ids(session, user)
    doors = session.exec(select(Door).where(Door.present == True).order_by(Door.name)).all()  # noqa: E712
    return [d for d in doors if d.id in allowed]


def schedule_doors(session: Session, ids: list[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {i: [] for i in ids}
    if ids:
        for row in session.exec(select(ScheduleDoor).where(ScheduleDoor.schedule_id.in_(ids))).all():
            out[row.schedule_id].append(row.door_id)
    return out


def visible_schedules(session: Session, user: User) -> list[Schedule]:
    if user.is_admin:
        return list(session.exec(select(Schedule)).all())
    allowed = allowed_door_ids(session, user)
    if not allowed:
        return []
    return list(session.exec(
        select(Schedule).join(ScheduleDoor, ScheduleDoor.schedule_id == Schedule.id)
        .where(ScheduleDoor.door_id.in_(allowed)).distinct()
    ).all())


def can_edit(session: Session, user: User, sched: Schedule, door_ids: list[str]) -> bool:
    if sched.source == "calendar":
        return False
    return user.is_admin or set(door_ids) <= allowed_door_ids(session, user)


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
          session: Session = Depends(get_session)):
    if has_users(session):
        return redirect("/login")
    if len(password) < 8:
        flash(request, "Password must be at least 8 characters.", "error")
        return redirect("/setup")
    user = User(name=name.strip(), email=email.strip().lower(), password_hash=hash_password(password), is_admin=True)
    session.add(user)
    session.commit()
    engine.audit(session, "user.setup", f"Created first admin {user.email}", user=user)
    session.commit()
    request.session["uid"] = user.id
    try:
        engine.sync_doors()
        flash(request, "Welcome! Your doors were loaded from UniFi Access.")
    except Exception as e:
        flash(request, f"Account created, but loading doors failed: {e}", "error")
    return redirect("/")


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
    user = session.exec(select(User).where(User.email == email.strip().lower())).first()
    if not user or not user.active or not verify_password(password, user.password_hash):
        flash(request, "That email and password didn't match.", "error")
        return redirect("/login")
    request.session.clear()
    request.session["uid"] = user.id
    nxt = request.query_params.get("next") or "/"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    return redirect(nxt)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@router.get("/account")
def account_page(request: Request, user: User = Depends(require_user)):
    return render(request, "account.html", user)


@router.post("/account")
def account(request: Request, name: str = Form(""), current_password: str = Form(""),
            new_password: str = Form(""), user: User = Depends(require_user),
            session: Session = Depends(get_session)):
    user.name = name.strip()
    if new_password:
        if not verify_password(current_password, user.password_hash):
            flash(request, "Current password is wrong.", "error")
            return redirect("/account")
        if len(new_password) < 8:
            flash(request, "New password must be at least 8 characters.", "error")
            return redirect("/account")
        user.password_hash = hash_password(new_password)
    session.add(user)
    session.commit()
    flash(request, "Account updated.")
    return redirect("/account")


# ---------- dashboard ----------

@router.get("/")
def dashboard(request: Request, user: User = Depends(require_user), session: Session = Depends(get_session)):
    doors = doors_for(session, user)
    door_names = {d.id: d.name for d in session.exec(select(Door)).all()}
    allowed = {d.id for d in doors}
    scheds = visible_schedules(session, user)
    sd = schedule_doors(session, [s.id for s in scheds])
    now = now_local()
    occ = expand(scheds, now, now + timedelta(days=14))

    def entry(o):
        ids = [d for d in sd[o.schedule.id] if user.is_admin or d in allowed]
        return {"o": o, "doors": [door_names.get(d, d) for d in ids],
                "editable": can_edit(session, user, o.schedule, sd[o.schedule.id])}

    happening = [entry(o) for o in occ if o.start <= now]
    upcoming: dict[date, list] = {}
    for o in occ:
        if o.start > now:
            upcoming.setdefault(o.start.date(), []).append(entry(o))

    states = {s.door_id: s for s in session.exec(select(DoorState)).all()}
    now_utc = utcnow()
    open_now = {d.id: to_local(states[d.id].unlocked_until) for d in doors
                if d.id in states and states[d.id].unlocked_until and states[d.id].unlocked_until > now_utc}
    errors = {d.id: states[d.id].last_error for d in doors if d.id in states and states[d.id].last_error}
    return render(request, "dashboard.html", user, doors=doors, happening=happening, upcoming=upcoming,
                  open_now=open_now, errors=errors)


@router.post("/quick-unlock")
def quick_unlock(request: Request, bg: BackgroundTasks, door_ids: list[str] = Form([]),
                 minutes: int = Form(...), user: User = Depends(require_user),
                 session: Session = Depends(get_session)):
    allowed = allowed_door_ids(session, user)
    door_ids = [d for d in door_ids if d in allowed]
    if not door_ids:
        flash(request, "Pick at least one door.", "error")
        return redirect("/")
    minutes = max(1, min(minutes, 12 * 60))
    start = now_local().replace(second=0, microsecond=0)
    sched = Schedule(label=f"Quick unlock by {user.name or user.email}", start_local=start,
                     duration_minutes=minutes + 1, source="quick", created_by=user.id)
    session.add(sched)
    session.flush()
    for d in door_ids:
        session.add(ScheduleDoor(schedule_id=sched.id, door_id=d))
    names = ", ".join(session.get(Door, d).name for d in door_ids)
    engine.audit(session, "schedule.quick", f"{names} for {minutes} min", user=user)
    session.commit()
    kick_reconcile(bg)
    flash(request, f"Unlocking {names} for {minutes} minutes.")
    return redirect("/")


# ---------- schedules ----------

@router.get("/schedules")
def schedules_list(request: Request, user: User = Depends(require_user), session: Session = Depends(get_session)):
    today = now_local().date()
    scheds = [s for s in visible_schedules(session, user) if series_last_date(s) >= today - timedelta(days=1)]
    scheds.sort(key=lambda s: s.start_local)
    sd = schedule_doors(session, [s.id for s in scheds])
    names = {d.id: d.name for d in session.exec(select(Door)).all()}
    feeds = {f.id: f.name for f in session.exec(select(CalendarFeed)).all()}
    rows = [{"s": s, "doors": [names.get(d, d) for d in sd[s.id]],
             "editable": can_edit(session, user, s, sd[s.id]), "feed": feeds.get(s.feed_id)} for s in scheds]
    return render(request, "schedules.html", user, rows=rows)


def _form_ctx(session, user, sched: Optional[Schedule], selected: list[str]):
    now = now_local()
    if sched:
        start = sched.start_local
        end = start + timedelta(minutes=sched.duration_minutes)
    else:
        start = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=2)
    return dict(sched=sched, doors=doors_for(session, user), selected=set(selected),
                date_val=start.date().isoformat(), start_val=start.strftime("%H:%M"),
                end_val=end.strftime("%H:%M"), days=weekdays_of(sched) if sched else [])


@router.get("/schedules/new")
def schedule_new(request: Request, door: Optional[str] = None, user: User = Depends(require_user),
                 session: Session = Depends(get_session)):
    return render(request, "schedule_form.html", user, **_form_ctx(session, user, None, [door] if door else []))


@router.get("/schedules/{sid}")
def schedule_edit(request: Request, sid: int, user: User = Depends(require_user),
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


def _parse_schedule(request, label, door_ids, day, start, end, repeat, weekdays, until, allowed):
    errors = []
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
    return dict(label=label, door_ids=door_ids, start_local=start_dt,
                duration_minutes=int((end_dt - start_dt).total_seconds() // 60),
                repeat_weekdays=",".join(map(str, days)), repeat_until=until_d), errors


@router.post("/schedules")
@router.post("/schedules/{sid}")
def schedule_save(request: Request, bg: BackgroundTasks, sid: Optional[int] = None,
                  label: str = Form(""), door_ids: list[str] = Form([]), day: str = Form(...),
                  start: str = Form(...), end: str = Form(...), repeat: str = Form("none"),
                  weekdays: list[str] = Form([]), until: str = Form(""), notes: str = Form(""),
                  user: User = Depends(require_user), session: Session = Depends(get_session)):
    allowed = allowed_door_ids(session, user)
    sched = None
    if sid is not None:
        sched = session.get(Schedule, sid)
        if not sched or not can_edit(session, user, sched, schedule_doors(session, [sid])[sid]):
            flash(request, "You can't edit that schedule.", "error")
            return redirect("/schedules")
    data, errors = _parse_schedule(request, label, door_ids, day, start, end, repeat, weekdays, until, allowed)
    if errors:
        for e in errors:
            flash(request, e, "error")
        return redirect(f"/schedules/{sid}" if sid else "/schedules/new")
    ids = data.pop("door_ids")
    if sched is None:
        sched = Schedule(**data, notes=notes.strip(), created_by=user.id)
        session.add(sched)
        session.flush()
        action = "schedule.create"
    else:
        for k, v in data.items():
            setattr(sched, k, v)
        sched.notes = notes.strip()
        session.add(sched)
        action = "schedule.update"
    calendars.set_schedule_doors(session, sched.id, ids)
    names = ", ".join(session.get(Door, d).name for d in ids)
    end_dt = sched.start_local + timedelta(minutes=sched.duration_minutes)
    engine.audit(session, action, f"“{sched.label}” {_day(sched.start_local)} {_span(sched.start_local, end_dt)} · {names}"
                 + (f" · {describe_repeat(sched)}" if sched.repeat_weekdays else ""), user=user)
    session.commit()
    kick_reconcile(bg)
    flash(request, f"Saved “{sched.label}”.")
    return redirect("/")


@router.post("/schedules/{sid}/delete")
def schedule_delete(request: Request, sid: int, bg: BackgroundTasks, user: User = Depends(require_user),
                    session: Session = Depends(get_session)):
    sched = session.get(Schedule, sid)
    if sched and can_edit(session, user, sched, schedule_doors(session, [sid])[sid]):
        engine.audit(session, "schedule.delete", f"“{sched.label}”", user=user)
        calendars.delete_schedule(session, sched)
        session.commit()
        kick_reconcile(bg)
        flash(request, f"Deleted “{sched.label}”.")
    return redirect("/schedules")


# ---------- doors ----------

@router.get("/doors")
def doors_page(request: Request, user: User = Depends(require_user), session: Session = Depends(get_session)):
    if user.is_admin:
        doors = session.exec(select(Door).order_by(Door.present.desc(), Door.name)).all()
    else:
        doors = doors_for(session, user)
    counts = dict(session.exec(select(UserDoor.door_id, func.count()).group_by(UserDoor.door_id)).all())
    states = {s.door_id: s for s in session.exec(select(DoorState)).all()}
    now_utc = utcnow()
    open_now = {k: to_local(v.unlocked_until) for k, v in states.items()
                if v.unlocked_until and v.unlocked_until > now_utc}
    return render(request, "doors.html", user, doors=doors, counts=counts, open_now=open_now,
                  errors={k: v.last_error for k, v in states.items() if v.last_error})


@router.post("/doors/sync")
def doors_sync(request: Request, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    try:
        n = engine.sync_doors()
        flash(request, f"Loaded {n} doors from UniFi Access.")
    except Exception as e:
        flash(request, f"Couldn't load doors: {e}", "error")
    return redirect("/doors")


# ---------- users (admin) ----------

@router.get("/users")
def users_page(request: Request, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    users = session.exec(select(User).order_by(User.active.desc(), User.name)).all()
    names = {d.id: d.name for d in session.exec(select(Door)).all()}
    access: dict[int, list[str]] = {}
    for ud in session.exec(select(UserDoor)).all():
        access.setdefault(ud.user_id, []).append(names.get(ud.door_id, ud.door_id))
    return render(request, "users.html", user, users=users, access=access)


@router.get("/users/{uid}")
@router.get("/users/new")
def user_form(request: Request, uid: Optional[int] = None, user: User = Depends(require_admin),
              session: Session = Depends(get_session)):
    target = session.get(User, uid) if uid else None
    selected = set(session.exec(select(UserDoor.door_id).where(UserDoor.user_id == uid)).all()) if uid else set()
    doors = session.exec(select(Door).where(Door.present == True).order_by(Door.name)).all()  # noqa: E712
    return render(request, "user_form.html", user, target=target, doors=doors, selected=selected)


@router.post("/users")
@router.post("/users/{uid}")
def user_save(request: Request, uid: Optional[int] = None, name: str = Form(""), email: str = Form(...),
              password: str = Form(""), is_admin: bool = Form(False), active: bool = Form(False),
              door_ids: list[str] = Form([]), user: User = Depends(require_admin),
              session: Session = Depends(get_session)):
    email = email.strip().lower()
    clash = session.exec(select(User).where(User.email == email)).first()
    if clash and clash.id != uid:
        flash(request, "Another user already has that email.", "error")
        return redirect(f"/users/{uid}" if uid else "/users/new")
    if uid:
        target = session.get(User, uid)
        if target.id == user.id and (not is_admin or not active):
            flash(request, "You can't remove your own admin access or deactivate yourself.", "error")
            return redirect(f"/users/{uid}")
    else:
        if len(password) < 8:
            flash(request, "Set a password of at least 8 characters.", "error")
            return redirect("/users/new")
        target = User(email=email, password_hash="", active=True)
        active = True
    target.name, target.email, target.is_admin, target.active = name.strip(), email, is_admin, active
    if password:
        if len(password) < 8:
            flash(request, "Password must be at least 8 characters.", "error")
            return redirect(f"/users/{uid}")
        target.password_hash = hash_password(password)
    session.add(target)
    session.flush()
    for ud in session.exec(select(UserDoor).where(UserDoor.user_id == target.id)).all():
        session.delete(ud)
    session.flush()
    valid = set(session.exec(select(Door.id)).all())
    for d in set(door_ids) & valid:
        session.add(UserDoor(user_id=target.id, door_id=d))
    engine.audit(session, "user.update" if uid else "user.create",
                 f"{target.email} ({'admin' if is_admin else f'{len(set(door_ids) & valid)} doors'})"
                 + ("" if active else ", deactivated"), user=user)
    session.commit()
    flash(request, f"Saved {target.name or target.email}.")
    return redirect("/users")


# ---------- calendars (admin) ----------

@router.get("/calendars")
def calendars_page(request: Request, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    feeds = session.exec(select(CalendarFeed).order_by(CalendarFeed.name)).all()
    counts = dict(session.exec(select(Schedule.feed_id, func.count()).where(Schedule.feed_id != None)  # noqa: E711
                               .group_by(Schedule.feed_id)).all())
    return render(request, "calendars.html", user, feeds=feeds, counts=counts)


@router.get("/calendars/{fid}")
@router.get("/calendars/new")
def calendar_form(request: Request, fid: Optional[int] = None, user: User = Depends(require_admin),
                  session: Session = Depends(get_session)):
    feed = session.get(CalendarFeed, fid) if fid else None
    rules = session.exec(select(CalendarRule).where(CalendarRule.feed_id == fid)
                         .order_by(CalendarRule.position)).all() if fid else []
    doors = session.exec(select(Door).where(Door.present == True).order_by(Door.name)).all()  # noqa: E712
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
async def calendar_save(request: Request, fid: Optional[int] = None, user: User = Depends(require_admin),
                        session: Session = Depends(get_session)):
    form = await request.form()
    name, url = str(form.get("name", "")).strip(), str(form.get("url", "")).strip()
    if not name or not url.startswith(("http://", "https://", "webcal://")):
        flash(request, "Give the calendar a name and an http(s) or webcal ICS link.", "error")
        return redirect(f"/calendars/{fid}" if fid else "/calendars/new")
    feed = session.get(CalendarFeed, fid) if fid else CalendarFeed(name=name, url=url)
    feed.name, feed.url = name, url
    feed.enabled = form.get("enabled") == "on"
    feed.include_all_day = form.get("include_all_day") == "on"
    feed.default_door_ids = ",".join(str(d) for d in form.getlist("default_doors"))
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
        session.add(CalendarRule(feed_id=feed.id, **r))
    engine.audit(session, "calendar.update" if fid else "calendar.create", feed.name, user=user)
    session.commit()
    status = (await run_in_threadpool(calendars.sync_feed, feed.id) if feed.enabled
              else "Saved (disabled, not synced).")
    await run_in_threadpool(engine.safe(engine.reconcile))
    flash(request, f"Saved “{feed.name}”. {status}", "error" if status.startswith("Error") else "ok")
    return redirect(f"/calendars/{feed.id}")


@router.post("/calendars/{fid}/sync")
def calendar_sync(request: Request, fid: int, bg: BackgroundTasks, user: User = Depends(require_admin)):
    status = calendars.sync_feed(fid)
    kick_reconcile(bg)
    flash(request, status, "error" if status.startswith("Error") else "ok")
    return redirect(f"/calendars/{fid}")


@router.post("/calendars/{fid}/preview")
def calendar_preview(request: Request, fid: int, user: User = Depends(require_admin),
                     session: Session = Depends(get_session)):
    feed = session.get(CalendarFeed, fid)
    if not feed:
        return redirect("/calendars")
    try:
        bookings = calendars.plan_feed(session, feed, calendars.fetch_ics(feed.url))
    except Exception as e:
        flash(request, f"Couldn't read the calendar: {e}", "error")
        return redirect(f"/calendars/{fid}")
    doors = session.exec(select(Door).where(Door.present == True).order_by(Door.name)).all()  # noqa: E712
    names = {d.id: d.name for d in doors}
    preview = [{"b": b, "doors": [names[d] for d in b.door_ids]} for b in bookings]
    rules = session.exec(select(CalendarRule).where(CalendarRule.feed_id == fid).order_by(CalendarRule.position)).all()
    return render(request, "calendar_form.html", user, feed=feed, rules=rules, doors=doors,
                  defaults=set(calendars.split_ids(feed.default_door_ids)), upcoming=[], preview=preview)


@router.post("/calendars/{fid}/delete")
def calendar_delete(request: Request, fid: int, bg: BackgroundTasks, user: User = Depends(require_admin),
                    session: Session = Depends(get_session)):
    feed = session.get(CalendarFeed, fid)
    if feed:
        for s in session.exec(select(Schedule).where(Schedule.feed_id == fid)).all():
            calendars.delete_schedule(session, s)
        for r in session.exec(select(CalendarRule).where(CalendarRule.feed_id == fid)).all():
            session.delete(r)
        session.delete(feed)
        engine.audit(session, "calendar.delete", feed.name, user=user)
        session.commit()
        kick_reconcile(bg)
        flash(request, f"Removed “{feed.name}” and its schedules.")
    return redirect("/calendars")


# ---------- activity (admin) ----------

@router.get("/activity")
def activity(request: Request, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    rows = session.exec(select(AuditLog).order_by(AuditLog.at.desc()).limit(300)).all()
    return render(request, "activity.html", user, rows=rows)
