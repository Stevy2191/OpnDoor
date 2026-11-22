"""Settings for admins: branding (logo, name, colours), automatic backups and one-button updates."""
import io
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlmodel import Session

from . import audit, config
from .db import get_session, session_scope
from .models import Setting, User
from .web import flash, redirect, render, require_admin, templates

log = logging.getLogger(__name__)
router = APIRouter()


def get(session: Session, key: str, default: str = "") -> str:
    row = session.get(Setting, key)
    return row.value if row else default


def put(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key) or Setting(key=key)
    row.value = value
    session.add(row)


@router.get("/settings")
def settings_home(user: User = Depends(require_admin)):
    return redirect("/settings/branding")


# ---------- branding ----------

BRAND_DIR = config.DATA_DIR / "brand"
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_brand_cache: dict = {"at": 0.0, "value": None}


def brand() -> dict:
    """Name, colours and logo for every page (cached for a few seconds)."""
    if _brand_cache["value"] is not None and time.monotonic() - _brand_cache["at"] < 10:
        return _brand_cache["value"]
    try:
        with session_scope() as s:
            value = {"name": get(s, "brand.name"), "accent": get(s, "brand.accent"), "nav": get(s, "brand.nav"),
                     "logo": get(s, "brand.logo")}
    except Exception:  # noqa: BLE001 - before the database exists
        value = {"name": "", "accent": "", "nav": "", "logo": ""}
    if value["logo"] and not (BRAND_DIR / value["logo"]).exists():
        value["logo"] = ""
    _brand_cache.update(at=time.monotonic(), value=value)
    return value


def _forget_brand() -> None:
    _brand_cache.update(at=0.0, value=None)


templates.env.globals["brand"] = brand


@router.get("/brand/logo")
def brand_logo():
    b = brand()
    if not b["logo"]:
        return Response(status_code=404)
    return FileResponse(BRAND_DIR / b["logo"], media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/settings/branding")
def branding_page(request: Request, user: User = Depends(require_admin)):
    return render(request, "settings_branding.html", user, b=brand(), tab="branding")


@router.post("/settings/branding")
async def branding_save(request: Request, name: str = Form(""), accent: str = Form(""), nav: str = Form(""),
                        reset_colors: bool = Form(False), remove_logo: bool = Form(False),
                        logo: Optional[UploadFile] = File(None), user: User = Depends(require_admin),
                        session: Session = Depends(get_session)):
    put(session, "brand.name", name.strip()[:60])
    if reset_colors:
        put(session, "brand.accent", "")
        put(session, "brand.nav", "")
    else:
        put(session, "brand.accent", accent if COLOR.match(accent or "") else "")
        put(session, "brand.nav", nav if COLOR.match(nav or "") else "")
    old = get(session, "brand.logo")
    if logo is not None and logo.filename:
        data = await logo.read(5 * 1024 * 1024 + 1)
        if len(data) > 5 * 1024 * 1024:
            flash(request, "That logo is too big. Use a picture under 5 MB.", "error")
            return redirect("/settings/branding")
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            img.load()
            img = img.convert("RGBA")
            img.thumbnail((800, 300))
        except Exception:  # noqa: BLE001 - not a picture
            flash(request, "That isn't a picture OpnDoor can read. Use a PNG, JPG or WebP.", "error")
            return redirect("/settings/branding")
        BRAND_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"logo-{int(time.time())}.png"
        img.save(BRAND_DIR / fname, "PNG")
        put(session, "brand.logo", fname)
        if old and old != fname:
            (BRAND_DIR / old).unlink(missing_ok=True)
    elif remove_logo and old:
        (BRAND_DIR / old).unlink(missing_ok=True)
        put(session, "brand.logo", "")
    audit.record(session, "settings.branding", "Changed the logo, name or colours", user=user)
    session.commit()
    _forget_brand()
    flash(request, "Branding saved.")
    return redirect("/settings/branding")


def logo_png() -> Optional[bytes]:
    b = brand()
    return (BRAND_DIR / b["logo"]).read_bytes() if b["logo"] else None


# ---------- backups ----------

BACKUP_DIR = config.DATA_DIR / "backups"
RESTORE_FILE = config.DATA_DIR / "restore-pending.zip"
NAME = re.compile(r"^opndoor-[\w.-]+\.zip$")


def _db_path() -> Optional[Path]:
    url = config.DATABASE_URL
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        return Path(url.removeprefix("sqlite:///"))
    return None


def _folders() -> list[Path]:
    return [config.DATA_DIR / "floorplans", BRAND_DIR]


def make_backup(note: str = "") -> Path:
    """A zip with a consistent copy of the database plus floor plans and the logo."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(config.TZ).strftime("%Y-%m-%d-%H%M%S")
    dest = BACKUP_DIR / f"opndoor-{stamp}{('-' + note) if note else ''}.zip"
    tmp_db = BACKUP_DIR / f".snapshot-{stamp}.db"
    src = _db_path()
    try:
        if src and src.exists():
            with sqlite3.connect(src) as a, sqlite3.connect(tmp_db) as b:
                a.backup(b)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            if tmp_db.exists():
                z.write(tmp_db, "opndoor.db")
            for folder in _folders():
                if folder.exists():
                    for f in folder.rglob("*"):
                        if f.is_file():
                            z.write(f, f"{folder.name}/{f.relative_to(folder)}")
            z.writestr("backup.json", json.dumps({"made": stamp, "version": config.VERSION,
                                                  "commit": config.COMMIT}))
    finally:
        tmp_db.unlink(missing_ok=True)
    os.chmod(dest, 0o600)  # it holds API tokens
    return dest


def list_backups() -> list[dict]:
    if not BACKUP_DIR.exists():
        return []
    out = []
    for f in sorted(BACKUP_DIR.glob("opndoor-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = f.stat()
        out.append({"name": f.name, "size": st.st_size, "at": datetime.fromtimestamp(st.st_mtime, config.TZ)})
    return out


def prune(keep: int) -> None:
    auto = [b for b in list_backups() if "-before-" not in b["name"]]
    for b in auto[keep:]:
        (BACKUP_DIR / b["name"]).unlink(missing_ok=True)


def nightly_backup() -> None:
    with session_scope() as s:
        if get(s, "backup.enabled", "1") != "1":
            return
        keep = int(get(s, "backup.keep", "14") or 14)
    make_backup()
    prune(max(1, keep))


def check_backup(data: bytes) -> str:
    """'' if this zip is an OpnDoor backup, else why not."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return "That isn't a backup file (.zip) from OpnDoor."
    names = z.namelist()
    if "opndoor.db" not in names:
        return "That zip doesn't have an OpnDoor database in it."
    if any(n.startswith("/") or ".." in Path(n).parts for n in names):
        return "That zip has odd file paths, so it wasn't used."
    tmp = config.DATA_DIR / ".restore-check.db"
    try:
        tmp.write_bytes(z.read("opndoor.db"))
        with sqlite3.connect(tmp) as c:
            c.execute('SELECT count(*) FROM "user"').fetchone()
    except sqlite3.Error:
        return "The database in that backup can't be read."
    finally:
        tmp.unlink(missing_ok=True)
    return ""


def apply_pending_restore() -> None:
    """At startup, before the database opens: swap in a backup that an admin chose to restore."""
    if not RESTORE_FILE.exists():
        return
    src = _db_path()
    try:
        make_backup("before-restore")
        with zipfile.ZipFile(RESTORE_FILE) as z:
            if src:
                for extra in ("-wal", "-shm"):
                    Path(str(src) + extra).unlink(missing_ok=True)
                src.write_bytes(z.read("opndoor.db"))
            for folder in _folders():
                if any(n.startswith(folder.name + "/") for n in z.namelist()):
                    shutil.rmtree(folder, ignore_errors=True)
            for n in z.namelist():
                top = n.split("/", 1)[0]
                if top in {f.name for f in _folders()} and not n.endswith("/"):
                    dest = config.DATA_DIR / n
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(z.read(n))
        log.warning("Restored a backup")
    except Exception:  # noqa: BLE001 - keep running on the current data
        log.exception("Restoring the backup failed; kept the current data")
    finally:
        RESTORE_FILE.unlink(missing_ok=True)


def restart_soon() -> None:
    """Stop OpnDoor so Docker starts it again (restart: unless-stopped)."""
    def later():
        time.sleep(1.5)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=later, daemon=True).start()


@router.get("/settings/backups")
def backups_page(request: Request, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    return render(request, "settings_backups.html", user, tab="backups", backups=list_backups(),
                  enabled=get(session, "backup.enabled", "1") == "1", keep=get(session, "backup.keep", "14"))


@router.post("/settings/backups")
def backups_save(request: Request, enabled: bool = Form(False), keep: str = Form("14"),
                 user: User = Depends(require_admin), session: Session = Depends(get_session)):
    put(session, "backup.enabled", "1" if enabled else "0")
    put(session, "backup.keep", str(min(365, max(1, int(keep)))) if keep.isdigit() else "14")
    session.commit()
    flash(request, "Backup settings saved.")
    return redirect("/settings/backups")


@router.post("/settings/backups/now")
def backups_now(request: Request, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    f = make_backup()
    audit.record(session, "settings.backup", f"Made a backup ({f.name})", user=user)
    session.commit()
    flash(request, "Backup made.")
    return redirect("/settings/backups")


@router.get("/settings/backups/{name}")
def backups_download(name: str, user: User = Depends(require_admin)):
    f = BACKUP_DIR / name
    if not NAME.match(name) or not f.exists():
        return Response(status_code=404)
    return FileResponse(f, media_type="application/zip", filename=name)


@router.post("/settings/backups/{name}/delete")
def backups_delete(request: Request, name: str, user: User = Depends(require_admin)):
    if NAME.match(name):
        (BACKUP_DIR / name).unlink(missing_ok=True)
        flash(request, "Backup deleted.")
    return redirect("/settings/backups")


@router.post("/settings/restore")
async def restore(request: Request, name: str = Form(""), file: Optional[UploadFile] = File(None),
                  user: User = Depends(require_admin), session: Session = Depends(get_session)):
    if file is not None and file.filename:
        data = await file.read()
    elif NAME.match(name) and (BACKUP_DIR / name).exists():
        data = (BACKUP_DIR / name).read_bytes()
    else:
        flash(request, "Pick a backup to restore.", "error")
        return redirect("/settings/backups")
    why = check_backup(data)
    if why:
        flash(request, why, "error")
        return redirect("/settings/backups")
    RESTORE_FILE.write_bytes(data)
    audit.record(session, "settings.restore", "Restored a backup (OpnDoor restarted)", user=user)
    session.commit()
    restart_soon()
    return render(request, "restarting.html", user, what="Restoring the backup", back="/settings/backups")


# ---------- updates ----------

UPDATE_REQUEST = config.DATA_DIR / "update.request"
UPDATE_STATUS = config.DATA_DIR / "update.status"
UPDATER_ALIVE = config.DATA_DIR / "updater.alive"
_latest: dict = {"at": 0.0, "value": None}


def updater_ready() -> bool:
    try:
        return time.time() - UPDATER_ALIVE.stat().st_mtime < 90
    except OSError:
        return False


def update_status() -> dict:
    try:
        lines = UPDATE_STATUS.read_text().splitlines()
    except OSError:
        return {}
    state, _, when = (lines[0] if lines else "").partition(" ")
    return {"state": state, "when": when, "detail": "\n".join(lines[1:])[-2000:]}


def latest() -> Optional[dict]:
    """The newest change on GitHub, to compare with the running version (cached for an hour)."""
    if not config.UPDATE_REPO:
        return None
    if time.monotonic() - _latest["at"] < 3600 and _latest["at"]:
        return _latest["value"]
    value = None
    try:
        r = httpx.get(f"https://api.github.com/repos/{config.UPDATE_REPO}/commits/main", timeout=6,
                      headers={"Accept": "application/vnd.github+json", "User-Agent": "OpnDoor"})
        if r.status_code == 200:
            j = r.json()
            value = {"sha": j["sha"], "date": j["commit"]["committer"]["date"][:10],
                     "message": j["commit"]["message"].splitlines()[0][:120]}
    except (httpx.HTTPError, KeyError, ValueError):
        value = None
    _latest.update(at=time.monotonic(), value=value)
    return value


def request_update() -> None:
    UPDATE_REQUEST.write_text(datetime.now(config.TZ).isoformat())


def nightly_update() -> None:
    with session_scope() as s:
        if get(s, "update.auto", "0") == "1" and updater_ready():
            request_update()


@router.get("/settings/updates")
def updates_page(request: Request, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    new = latest()
    behind = bool(new and config.COMMIT and not new["sha"].startswith(config.COMMIT[:7])
                  and not config.COMMIT.startswith(new["sha"][:7]))
    return render(request, "settings_updates.html", user, tab="updates", ready=updater_ready(),
                  status=update_status(), pending=UPDATE_REQUEST.exists(), new=new, behind=behind,
                  auto=get(session, "update.auto", "0") == "1", commit=config.COMMIT[:7])


@router.post("/settings/updates/now")
def updates_now(request: Request, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    if not updater_ready():
        flash(request, "The update helper isn't running. Run the installer again and turn on the Update button.",
              "error")
        return redirect("/settings/updates")
    make_backup("before-update")
    request_update()
    audit.record(session, "settings.update", "Started an update", user=user)
    session.commit()
    return render(request, "restarting.html", user, what="Checking for an update",
                  note="If there's a newer version, OpnDoor downloads it and restarts. That takes a minute or two.",
                  back="/settings/updates")


@router.post("/settings/updates")
def updates_save(request: Request, auto: bool = Form(False), user: User = Depends(require_admin),
                 session: Session = Depends(get_session)):
    put(session, "update.auto", "1" if auto else "0")
    session.commit()
    flash(request, "Saved.")
    return redirect("/settings/updates")
