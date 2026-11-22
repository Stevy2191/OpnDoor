"""People in UniFi Access: give someone a new PIN, or turn their access off and on, without the UniFi app."""
import time
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
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
                     "cards": len(u.get("nfc_cards") or []), "employee": u.get("employee_number") or ""})
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
