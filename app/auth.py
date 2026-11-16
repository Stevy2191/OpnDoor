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
    """Sites a user may see: admins all; others their whole-site grants plus sites of single doors."""
    if user.is_admin:
        from .models import Site
        return set(session.exec(select(Site.id)).all())
    sites = set(session.exec(select(UserSite.site_id).where(UserSite.user_id == user.id)).all())
    single = set(session.exec(select(UserDoor.door_id).where(UserDoor.user_id == user.id)).all())
    if single:
        sites |= set(session.exec(select(Door.site_id).where(Door.id.in_(single))).all())
    return sites


def managed_site_ids(session: Session, user: User) -> set[int]:
    """Sites whose people, calendars and history this user manages."""
    if user.is_admin:
        from .models import Site
        return set(session.exec(select(Site.id)).all())
    if user.is_site_admin:
        return set(session.exec(select(UserSite.site_id).where(UserSite.user_id == user.id)).all())
    return set()


def person_site_ids(session: Session, person: User) -> set[int]:
    """Every site a person has any access at."""
    return visible_site_ids(session, person) if not person.is_admin else set()


def can_manage_person(session: Session, manager: User, person: User) -> bool:
    """Admins manage everyone. Site admins manage door operators who work at their sites
    (or who have no access anywhere yet)."""
    if manager.is_admin:
        return True
    if not manager.is_site_admin or person.is_admin or person.is_site_admin:
        return False
    theirs = person_site_ids(session, person)
    return not theirs or bool(theirs & managed_site_ids(session, manager))


def fully_managed(session: Session, manager: User, person: User) -> bool:
    """True if all of the person's access is inside the manager's sites, so the manager may
    change their name, email, sign-in and on/off. Otherwise only access at the manager's
    own sites can be changed."""
    return manager.is_admin or person_site_ids(session, person) <= managed_site_ids(session, manager)


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
    record(session, "signin.ok", f"{user.display} signed in with {method}", user=user,
           site_ids=person_site_ids(session, user) or None)
    session.commit()


def safe_next(nxt: Optional[str]) -> str:
    """Only allow redirects to paths on this site."""
    if not nxt or not nxt.startswith("/") or nxt.startswith("//") or nxt.startswith("/\\"):
        return "/"
    return nxt
