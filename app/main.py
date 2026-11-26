"""OpnDoor: door unlock scheduling for UniFi Access."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import calendars, config, engine
from .auth import AdminRequired, LoginRequired
from .db import init_db
from . import access, activity, admin, dayview, templates_, whocan, pwa, alerts, devices, reports, unifiwatch, history, holidays, live, mailer, maps, screens, sso, unifipeople, video, visitors
from .security import SecurityMiddleware
from .web import render, router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("opndoor")


def start_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=config.TZ)
    opts = dict(max_instances=1, coalesce=True)
    sched.add_job(engine.safe(engine.reconcile), "interval", seconds=config.RECONCILE_SECONDS, id="reconcile", **opts)
    sched.add_job(engine.safe(engine.sync_doors), "interval", minutes=config.DOOR_SYNC_MINUTES, id="doors", **opts)
    sched.add_job(engine.safe(calendars.sync_all), "interval", minutes=config.CALENDAR_SYNC_MINUTES,
                  id="calendars", **opts)
    sched.add_job(engine.safe(engine.refresh_states), "interval", seconds=config.RECONCILE_SECONDS, id="states", **opts)
    sched.add_job(engine.safe(activity.sync_all), "interval", minutes=2, id="doorlog", **opts)
    sched.add_job(engine.safe(access.sync_all), "interval", minutes=config.DOOR_SYNC_MINUTES, id="access", **opts)
    sched.add_job(engine.safe(unifiwatch.watch_all), "interval", minutes=1, id="unifiwatch", **opts)
    sched.add_job(engine.safe(alerts.check_all), "interval", minutes=1, id="alerts", **opts)
    sched.add_job(engine.safe(reports.send_due), "cron", minute=5, id="reports", **opts)
    sched.add_job(engine.safe(admin.nightly_backup), "cron", hour=2, minute=17, id="backup", **opts)
    sched.add_job(engine.safe(admin.nightly_update), "cron", hour=3, minute=41, id="update", **opts)
    sched.start()
    return sched


@asynccontextmanager
async def lifespan(app: FastAPI):
    admin.apply_pending_restore()  # a backup an admin chose to restore, before the database opens
    init_db()
    sched = None
    if config.RUN_SCHEDULER:
        engine.ensure_sites()
        if config.DRY_RUN:
            log.warning("DRY_RUN is on: all sites use demo consoles; nothing is sent to UniFi Access.")
        engine.safe(engine.sync_doors)()
        engine.safe(calendars.sync_all)()
        engine.safe(engine.reconcile)()  # catch up after a restart
        sched = start_scheduler()
    yield
    if sched:
        sched.shutdown(wait=False)


app = FastAPI(title="OpnDoor", version=config.VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(SecurityMiddleware)
app.add_middleware(SessionMiddleware, secret_key=config.secret_key(), session_cookie="opndoor_session",
                   max_age=14 * 24 * 3600, same_site="lax", https_only=config.SECURE_COOKIES)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(router)
app.include_router(sso.router)
app.include_router(history.router)
app.include_router(maps.router)
app.include_router(video.router)
app.include_router(live.router)
app.include_router(holidays.router)
app.include_router(dayview.router)
app.include_router(templates_.router)
app.include_router(whocan.router)
app.include_router(pwa.router)
app.include_router(activity.router)
app.include_router(visitors.router)
app.include_router(screens.router)
app.include_router(mailer.router)
app.include_router(admin.router)
app.include_router(devices.router)
app.include_router(unifipeople.router)
app.include_router(alerts.router)
app.include_router(reports.router)


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired):
    nxt = request.url.path if request.method == "GET" and request.url.path != "/" else ""
    return RedirectResponse("/login" + (f"?next={nxt}" if nxt else ""), status_code=303)


@app.exception_handler(AdminRequired)
async def _admin_required(request: Request, exc: AdminRequired):
    resp: HTMLResponse = render(request, "error.html", None, message="That page is for administrators.")
    resp.status_code = 403
    return resp


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": config.VERSION, "tz": config.TZ_NAME, "dry_run": config.DRY_RUN}

