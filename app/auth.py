"""Password hashing (stdlib scrypt) and login helpers."""
import base64
import hashlib
import hmac
import secrets
from typing import Optional

from fastapi import Request
from sqlmodel import Session, select

from .models import Door, User, UserDoor, UserSite

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}".format(
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
    except ValueError:
        return False
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=len(expected))
    return hmac.compare_digest(dk, expected)


def current_user(request: Request, session: Session) -> Optional[User]:
    uid = request.session.get("uid")
    if not uid:
        return None
    user = session.get(User, uid)
    if not user or not user.active:
        request.session.clear()
        return None
    return user


def allowed_door_ids(session: Session, user: User) -> set[str]:
    """Doors this user may see and schedule.

    Admins: every door. Door operators: every door at their sites (including doors
    added later) plus any single doors they were given.
    """
    if user.is_admin:
        return set(session.exec(select(Door.id)).all())
    site_ids = set(session.exec(select(UserSite.site_id).where(UserSite.user_id == user.id)).all())
    doors = set(session.exec(select(UserDoor.door_id).where(UserDoor.user_id == user.id)).all())
    if site_ids:
        doors |= set(session.exec(select(Door.id).where(Door.site_id.in_(site_ids))).all())
    return doors


def visible_site_ids(session: Session, user: User) -> set[int]:
    """Sites a user may see: admins all, operators only sites where they have doors."""
    if user.is_admin:
        from .models import Site
        return set(session.exec(select(Site.id)).all())
    ids = allowed_door_ids(session, user)
    if not ids:
        return set()
    return set(session.exec(select(Door.site_id).where(Door.id.in_(ids))).all())


class LoginRequired(Exception):
    pass


class AdminRequired(Exception):
    pass


def login_user(request: Request, session: Session, user: User, method: str) -> None:
    """Start a session for user and note it in History."""
    from .audit import record
    from .models import utcnow
    request.session.clear()
    request.session["uid"] = user.id
    user.last_login_at = utcnow()
    session.add(user)
    record(session, "signin.ok", f"{user.display} signed in with {method}", user=user)
    session.commit()


def safe_next(nxt: Optional[str]) -> str:
    """Only allow redirects to paths on this site."""
    if not nxt or not nxt.startswith("/") or nxt.startswith("//") or nxt.startswith("/\\"):
        return "/"
    return nxt
