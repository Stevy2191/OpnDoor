"""Sending email (visitor passes, check-in alerts) through the email server set up under Email settings."""
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from sqlmodel import Session

from . import audit
from .db import get_session, session_scope
from .models import Setting, User
from .web import flash, redirect, render, require_admin

log = logging.getLogger(__name__)
router = APIRouter()

KEYS = ("host", "port", "security", "username", "password", "sender", "sender_name")
PRESETS = {
    "m365": {"host": "smtp.office365.com", "port": "587", "security": "starttls"},
    "gmail": {"host": "smtp.gmail.com", "port": "587", "security": "starttls"},
}


class MailError(Exception):
    pass


def settings(session: Session) -> dict:
    out = {k: "" for k in KEYS}
    for k in KEYS:
        row = session.get(Setting, "mail." + k)
        if row:
            out[k] = row.value
    out["port"] = out["port"] or "587"
    out["security"] = out["security"] or "starttls"
    return out


def configured(session: Session) -> bool:
    s = settings(session)
    return bool(s["host"] and s["sender"])


def _save(session: Session, values: dict) -> None:
    for k, v in values.items():
        row = session.get(Setting, "mail." + k) or Setting(key="mail." + k)
        row.value = v
        session.add(row)


def send(cfg: dict, to: str, subject: str, text: str, html: str = "",
         images: Optional[dict] = None) -> None:
    """Send one message. images: {name: png bytes} shown inline in the html as cid:name."""
    if not (cfg.get("host") and cfg.get("sender")):
        raise MailError("Email isn't set up yet. An admin can add an email server under Manage → Email.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg.get("sender_name") or "OpnDoor", cfg["sender"]))
    msg["To"] = to
    msg.set_content(text)
    if html:
        cids = {name: make_msgid(domain="opndoor")[1:-1] for name in (images or {})}
        for name, cid in cids.items():
            html = html.replace(f"cid:{name}", f"cid:{cid}")
        msg.add_alternative(html, subtype="html")
        part = msg.get_payload()[1]
        for name, data in (images or {}).items():
            part.add_related(data, maintype="image", subtype="png", cid=f"<{cids[name]}>", filename=f"{name}.png")
    port = int(cfg.get("port") or 587)
    try:
        if cfg.get("security") == "ssl":
            server = smtplib.SMTP_SSL(cfg["host"], port, timeout=20, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg["host"], port, timeout=20)
        with server:
            if cfg.get("security") == "starttls":
                server.starttls(context=ssl.create_default_context())
            if cfg.get("username"):
                server.login(cfg["username"], cfg.get("password", ""))
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise MailError("The email server didn't accept the username or password. For Microsoft 365 or "
                        "Gmail you may need an app password.") from e
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"Couldn't send email: {e}") from e


def send_now(to: str, subject: str, text: str, html: str = "", images: Optional[dict] = None) -> None:
    with session_scope() as s:
        cfg = settings(s)
    send(cfg, to, subject, text, html, images)


# ---------- settings page ----------

@router.get("/settings/email")
def email_page(request: Request, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    return render(request, "email_settings.html", user, mail=settings(session), tab="email")


@router.post("/settings/email")
def email_save(request: Request, preset: str = Form(""), host: str = Form(""), port: str = Form("587"),
               security: str = Form("starttls"), username: str = Form(""), password: str = Form(""),
               sender: str = Form(""), sender_name: str = Form(""), clear_password: bool = Form(False),
               test_to: str = Form(""), user: User = Depends(require_admin),
               session: Session = Depends(get_session)):
    values = {"host": host.strip(), "port": port.strip() if port.strip().isdigit() else "587",
              "security": security if security in ("starttls", "ssl", "none") else "starttls",
              "username": username.strip(), "sender": sender.strip(), "sender_name": sender_name.strip()}
    if preset in PRESETS:
        values.update(PRESETS[preset])
    if password:
        values["password"] = password
    elif clear_password:
        values["password"] = ""
    _save(session, values)
    audit.record(session, "settings.email", f"Email server: {values['host'] or 'none'}", user=user)
    session.commit()
    if test_to.strip():
        try:
            send(settings(session), test_to.strip(), "OpnDoor test email",
                 "This is a test from OpnDoor. Email is working, so visitor passes can be sent.")
            flash(request, f"Saved. A test email was sent to {test_to.strip()}.")
        except MailError as e:
            flash(request, f"Saved, but the test didn't go through. {e}", "error")
    else:
        flash(request, "Email settings saved.")
    return redirect("/settings/email")
