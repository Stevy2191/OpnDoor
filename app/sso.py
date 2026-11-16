"""Sign in with Microsoft (Microsoft Entra ID, OpenID Connect).

Only people an admin has already added can sign in: the Microsoft account's
email must match a person in OpnDoor. On first sign-in the Microsoft account
id (oid) is remembered, so the link survives a later email change.

The ID token comes straight from Microsoft's token endpoint over HTTPS (the
authorization-code flow, with PKCE), so per OpenID Connect Core 3.1.3.7 the
TLS connection vouches for it; we still check issuer, audience, tenant,
expiry and nonce.
"""
import base64
import hashlib
import json
import secrets
import time
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from . import config
from .audit import record
from .auth import login_user, safe_next
from .db import get_session
from .models import User

router = APIRouter()
AUTHORITY = "https://login.microsoftonline.com"


class SSOError(Exception):
    pass


def redirect_uri(request: Request) -> str:
    base = config.PUBLIC_URL or str(request.base_url).rstrip("/")
    return f"{base}/auth/microsoft/callback"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def decode_jwt_payload(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as e:
        raise SSOError("Microsoft sent a sign-in token we couldn't read.") from e


def exchange_code(code: str, verifier: str, redirect: str) -> dict:
    resp = httpx.post(
        f"{AUTHORITY}/{config.MS_TENANT_ID}/oauth2/v2.0/token",
        data={"client_id": config.MS_CLIENT_ID, "client_secret": config.MS_CLIENT_SECRET,
              "grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
              "code_verifier": verifier, "scope": "openid profile email"},
        timeout=15,
    )
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code != 200 or "id_token" not in body:
        raise SSOError(f"Microsoft sign-in failed: {body.get('error_description') or resp.status_code}")
    return body


def validate_claims(claims: dict, nonce: str) -> None:
    tid = claims.get("tid", "")
    if claims.get("aud") != config.MS_CLIENT_ID:
        raise SSOError("This sign-in was meant for a different app.")
    if claims.get("iss") != f"{AUTHORITY}/{tid}/v2.0":
        raise SSOError("Unexpected sign-in issuer.")
    if config.MS_TENANT_ID.count("-") == 4 and tid.lower() != config.MS_TENANT_ID.lower():
        raise SSOError("That Microsoft account isn't from your organization.")
    if claims.get("nonce") != nonce:
        raise SSOError("The sign-in expired. Please try again.")
    if int(claims.get("exp", 0)) < time.time() - 60:
        raise SSOError("The sign-in expired. Please try again.")


def find_user(session: Session, claims: dict) -> Optional[User]:
    oid = claims.get("oid")
    if oid:
        user = session.exec(select(User).where(User.ms_oid == oid)).first()
        if user:
            return user
    email = (claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    if not email:
        return None
    user = session.exec(select(User).where(User.email == email)).first()
    if user and user.ms_oid and oid and user.ms_oid != oid:
        return None  # email now belongs to a different Microsoft account
    return user


@router.get("/auth/microsoft")
def start(request: Request):
    if not config.SSO_ENABLED:
        return RedirectResponse("/login", status_code=303)
    verifier = _b64url(secrets.token_bytes(32))
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    request.session["sso"] = {"state": state, "nonce": nonce, "verifier": verifier,
                              "next": safe_next(request.query_params.get("next"))}
    params = {
        "client_id": config.MS_CLIENT_ID, "response_type": "code", "redirect_uri": redirect_uri(request),
        "response_mode": "query", "scope": "openid profile email", "state": state, "nonce": nonce,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256", "prompt": "select_account",
    }
    return RedirectResponse(f"{AUTHORITY}/{config.MS_TENANT_ID}/oauth2/v2.0/authorize?{urlencode(params)}",
                            status_code=303)


@router.get("/auth/microsoft/callback")
def callback(request: Request, session: Session = Depends(get_session)):
    from .web import flash
    pending = request.session.pop("sso", None)
    q = request.query_params
    try:
        if not config.SSO_ENABLED:
            raise SSOError("Microsoft sign-in isn't set up.")
        if q.get("error"):
            raise SSOError(q.get("error_description") or "Microsoft sign-in was cancelled.")
        if not pending or q.get("state") != pending["state"]:
            raise SSOError("The sign-in expired. Please try again.")
        tokens = exchange_code(q.get("code", ""), pending["verifier"], redirect_uri(request))
        claims = decode_jwt_payload(tokens["id_token"])
        validate_claims(claims, pending["nonce"])
    except (SSOError, httpx.HTTPError) as e:
        flash(request, str(e) if isinstance(e, SSOError) else "Couldn't reach Microsoft. Please try again.", "error")
        return RedirectResponse("/login", status_code=303)

    user = find_user(session, claims)
    who = claims.get("email") or claims.get("preferred_username") or claims.get("name", "unknown")
    if not user or not user.active:
        record(session, "signin.fail", f"{who} tried to sign in with Microsoft but "
               + ("their account is turned off." if user else "isn't set up in OpnDoor."), actor=who)
        session.commit()
        flash(request, "Your Microsoft account isn't set up in OpnDoor yet. Ask an administrator to add you."
              if not user else "Your OpnDoor account is turned off. Ask an administrator.", "error")
        return RedirectResponse("/login", status_code=303)
    if not user.ms_oid:
        user.ms_oid = claims.get("oid")
    if not user.name and claims.get("name"):
        user.name = claims["name"]
    login_user(request, session, user, "Microsoft")
    return RedirectResponse(pending.get("next") or "/", status_code=303)
