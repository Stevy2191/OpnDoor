"""Password hashing (stdlib scrypt) and login helpers."""
import base64
import hashlib
import hmac
import secrets
from typing import Optional

from fastapi import Request
from sqlmodel import Session, select

from .models import Door, User, UserDoor

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
    """Doors this user may schedule. Admins get every door."""
    if user.is_admin:
        return set(session.exec(select(Door.id)).all())
    return set(session.exec(select(UserDoor.door_id).where(UserDoor.user_id == user.id)).all())


class LoginRequired(Exception):
    pass


class AdminRequired(Exception):
    pass
