"""People in UniFi Access: give someone a new PIN, or turn their access off and on, without the UniFi app."""
import time
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from . import audit, mailer, unifi
from .auth import managed_site_ids
from .db import get_session
from .models import Site, User
from .web import current_site_id, flash, redirect, render, require_manager

router = APIRouter()
_cache: dict = {}


def _people(site: Site, fresh: bool = False) -> list[dict]:
    hit = _cache.get(site.id)
    if hit and not fresh and time.monotonic() - hit[0] < 60:
        return hit[1]
    rows = []
    for u in unifi.get_client(site).list_users():
        name = (u.get("full_name") or f"{u.get('first_name', '')} {u.get('last_name', '')}").strip() or u.get("id", "")
        status = str(u.get("status") or "ACTIVE").upper()
        pin = u.get("pin_code") or {}
        rows.append({"id": u.get("id", ""), "name": name, "email": u.get("user_email") or u.get("email") or "",
                     "status": status, "active": status == "ACTIVE",
                     "has_pin": bool(pin.get("token") if isinstance(pin, dict) else pin),
                     "cards": len(u.get("nfc_cards") or []), "employee": u.get("employee_number") or "",
                     "card_list": [{"id": str(c.get("id") or c.get("display_id") or ""), "token": c.get("token", "")}
                                   for c in u.get("nfc_cards") or [] if isinstance(c, dict)]})
    rows.sort(key=lambda r: (not r["active"], r["name"].lower()))
    _cache[site.id] = (time.monotonic(), rows)
    return rows


def _site(request: Request, session: Session, user: User, site_id: Optional[int]) -> tuple[list[Site], Optional[Site]]:
    ids = managed_site_ids(session, user)
    sites = [x for x in session.exec(select(Site).order_by(Site.name)).all() if x.id in ids]
    pick = site_id or current_site_id(request, session, user)
    site = next((x for x in sites if x.id == pick), sites[0] if sites else None)
    return sites, site


@router.get("/unifi-people")
def people_page(request: Request, site: Optional[int] = None, q: str = "", user: User = Depends(require_manager),
                session: Session = Depends(get_session)):
    sites, st = _site(request, session, user, site)
    rows, error = [], ""
    if st:
        try:
            rows = _people(st)
        except unifi.UnifiError as e:
            error = str(e)
    if q:
        t = q.lower()
        rows = [r for r in rows if t in r["name"].lower() or t in r["email"].lower() or t in r["employee"].lower()]
    new_pin = request.session.pop("new_pin", None)
    return render(request, "unifi_people.html", user, sites=sites, st=st, rows=rows, error=error, q=q,
                  new_pin=new_pin, mail_ready=mailer.configured(session))


def _find(st: Site, uid: str) -> Optional[dict]:
    return next((r for r in _people(st) if r["id"] == uid), None)


@router.post("/unifi-people/{site_id}/{uid}/pin")
def new_pin(request: Request, site_id: int, uid: str, send: bool = Form(False), user: User = Depends(require_manager),
            session: Session = Depends(get_session)):
    st = session.get(Site, site_id)
    back = f"/unifi-people?site={site_id}"
    if not st or site_id not in managed_site_ids(session, user):
        return redirect("/unifi-people")
    person = _find(st, uid)
    if not person:
        flash(request, "That person isn't in UniFi Access any more.", "error")
        return redirect(back)
    client = unifi.get_client(st)
    try:
        pin = client.new_pin()
        client.set_user_pin(uid, pin)
    except unifi.UnifiError as e:
        flash(request, f"UniFi Access didn't change the PIN: {e}", "error")
        return redirect(back)
    _cache.pop(st.id, None)
    audit.record(session, "unifi.pin", f"Gave {person['name']} a new PIN at {st.name}", user=user, site_ids=[st.id])
    session.commit()
    sent = ""
    if send and person["email"]:
        try:
            mailer.send(mailer.settings(session), person["email"], f"Your new door PIN for {st.name}",
                        f"Hi {person['name'].split()[0]},\n\nYour new door PIN for {st.name} is {pin}.\n"
                        "Your old PIN no longer works.")
            sent = person["email"]
        except mailer.MailError as e:
            flash(request, f"The PIN changed, but it wasn't emailed. {e}", "error")
    request.session["new_pin"] = {"name": person["name"], "pin": pin, "sent": sent}
    return redirect(back)


@router.post("/unifi-people/{site_id}/{uid}/active")
def set_active(request: Request, site_id: int, uid: str, active: bool = Form(False),
               user: User = Depends(require_manager), session: Session = Depends(get_session)):
    st = session.get(Site, site_id)
    back = f"/unifi-people?site={site_id}"
    if not st or site_id not in managed_site_ids(session, user):
        return redirect("/unifi-people")
    person = _find(st, uid)
    if not person:
        return redirect(back)
    try:
        unifi.get_client(st).set_user_active(uid, active)
    except unifi.UnifiError as e:
        flash(request, f"UniFi Access didn't make the change: {e}", "error")
        return redirect(back)
    _cache.pop(st.id, None)
    what = "Turned access back on for" if active else "Turned off access for"
    audit.record(session, "unifi.active", f"{what} {person['name']} at {st.name}", user=user, site_ids=[st.id])
    session.commit()
    flash(request, f"{person['name']} can open doors again." if active else
          f"{person['name']} can't open any doors at {st.name} now. Their cards, PIN and phone stop working.")
    return redirect(back)


# ---------- adding NFC cards ----------

CARD_WAIT = 60  # seconds a reader waits for a card


def _managed(session: Session, user: User, site_id: int) -> Optional[Site]:
    st = session.get(Site, site_id)
    return st if st and site_id in managed_site_ids(session, user) else None


def _readers(session: Session, st: Site) -> list[dict]:
    from .models import Door
    doors = {d.remote_id: d.name for d in session.exec(select(Door).where(Door.site_id == st.id)).all()}
    out = []
    for d in unifi.get_client(st).list_devices():
        if d.get("can_enroll") and d.get("online") is not False:
            where = doors.get(d.get("door_id", ""), "")
            label = d.get("alias") or (f"{where} reader" if where else d["name"])
            out.append({"id": d["id"], "label": label, "sub": d.get("model_name") or d["name"]})
    return sorted(out, key=lambda r: r["label"].lower())


@router.get("/unifi-people/{site_id}/{uid}/card")
def card_page(request: Request, site_id: int, uid: str, user: User = Depends(require_manager),
              session: Session = Depends(get_session)):
    st = _managed(session, user, site_id)
    person = _find(st, uid) if st else None
    if not person:
        return redirect("/unifi-people")
    readers, error = [], ""
    try:
        readers = _readers(session, st)
    except unifi.UnifiError as e:
        error = str(e)
    return render(request, "unifi_card.html", user, st=st, person=person, readers=readers, error=error,
                  last_reader=request.session.get("card_reader", ""))


@router.post("/unifi-people/{site_id}/{uid}/card/start")
def card_start(request: Request, site_id: int, uid: str, device: str = Form(...), user: User = Depends(require_manager),
               session: Session = Depends(get_session)):
    st = _managed(session, user, site_id)
    if not st or not _find(st, uid):
        return JSONResponse({"state": "error", "message": "That person isn't available."}, status_code=404)
    if device not in {r["id"] for r in _readers(session, st)}:
        return JSONResponse({"state": "error", "message": "Pick one of the readers."}, status_code=400)
    try:
        sid = unifi.get_client(st).card_session_start(device)
    except unifi.UnifiError as e:
        return JSONResponse({"state": "error", "message": str(e)})
    pending = request.session.get("cards", {})
    pending = {k: v for k, v in pending.items() if time.time() - v["at"] < CARD_WAIT * 2}
    pending[sid] = {"site": site_id, "uid": uid, "at": time.time()}
    request.session["cards"] = pending
    request.session["card_reader"] = device
    return JSONResponse({"state": "waiting", "session": sid, "seconds": CARD_WAIT})


@router.get("/unifi-people/{site_id}/{uid}/card/{sid}")
def card_poll(request: Request, site_id: int, uid: str, sid: str, user: User = Depends(require_manager),
              session: Session = Depends(get_session)):
    mine = request.session.get("cards", {}).get(sid)
    st = _managed(session, user, site_id)
    if not st or not mine or mine["site"] != site_id or mine["uid"] != uid:
        return JSONResponse({"state": "error", "message": "That card reading isn't yours or has ended."}, status_code=404)
    client = unifi.get_client(st)

    def finish():
        pending = request.session.get("cards", {})
        pending.pop(sid, None)
        request.session["cards"] = pending
    if time.time() - mine["at"] > CARD_WAIT:
        client.card_session_cancel(sid)
        finish()
        return JSONResponse({"state": "error", "message": "No card was tapped in time. Try again."})
    try:
        got = client.card_session_poll(sid)
    except unifi.UnifiError as e:
        finish()
        return JSONResponse({"state": "error", "message": str(e)})
    if not got:
        return JSONResponse({"state": "waiting"})
    finish()
    person = _find(st, uid)
    try:
        client.add_user_card(uid, got["token"])
    except unifi.UnifiError as e:
        return JSONResponse({"state": "error", "message": str(e)})
    _cache.pop(st.id, None)
    audit.record(session, "unifi.card", f"Added card {got['card_id'] or ''} for {person['name'] if person else uid} "
                 f"at {st.name}".replace("  ", " "), user=user, site_ids=[st.id])
    session.commit()
    return JSONResponse({"state": "done", "card": got["card_id"]})


@router.post("/unifi-people/{site_id}/{uid}/card/{sid}/cancel")
def card_cancel(request: Request, site_id: int, uid: str, sid: str, user: User = Depends(require_manager),
                session: Session = Depends(get_session)):
    pending = request.session.get("cards", {})
    st = _managed(session, user, site_id)
    if st and sid in pending:
        unifi.get_client(st).card_session_cancel(sid)
        pending.pop(sid, None)
        request.session["cards"] = pending
    return JSONResponse({"state": "cancelled"})


@router.post("/unifi-people/{site_id}/{uid}/card-remove")
def card_remove(request: Request, site_id: int, uid: str, token: str = Form(...), user: User = Depends(require_manager),
                session: Session = Depends(get_session)):
    st = _managed(session, user, site_id)
    person = _find(st, uid) if st else None
    back = f"/unifi-people/{site_id}/{uid}/card"
    if not person or token not in {c["token"] for c in person["card_list"]}:
        return redirect(back if person else "/unifi-people")
    card = next(c["id"] for c in person["card_list"] if c["token"] == token)
    try:
        unifi.get_client(st).remove_user_card(uid, token)
    except unifi.UnifiError as e:
        flash(request, f"UniFi Access didn't remove the card: {e}", "error")
        return redirect(back)
    _cache.pop(st.id, None)
    audit.record(session, "unifi.card", f"Removed card {card} from {person['name']} at {st.name}", user=user, site_ids=[st.id])
    session.commit()
    flash(request, f"Card {card} removed. It no longer opens doors for {person['name']}.")
    return redirect(back)
