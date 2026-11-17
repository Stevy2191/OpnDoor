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
from . import history, maps, sso
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
    sched.start()
    return sched


@asynccontextmanager
async def lifespan(app: FastAPI):
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
app.add_middleware(SessionMiddleware, secret_key=config.secret_key(), session_cookie="opndoor_session",
                   max_age=14 * 24 * 3600, same_site="lax", https_only=config.SECURE_COOKIES)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(router)
app.include_router(sso.router)
app.include_router(history.router)
app.include_router(maps.router)


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

