"""Settings read from environment variables (see .env.example)."""
import os
import secrets
from pathlib import Path
from zoneinfo import ZoneInfo


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


VERSION = os.getenv("APP_VERSION", "dev")
TZ_NAME = os.getenv("TZ", "America/Chicago")
TZ = ZoneInfo(TZ_NAME)

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'scheduler.db'}")

UNIFI_HOST = os.getenv("UNIFI_ACCESS_HOST", "").rstrip("/")
UNIFI_TOKEN = os.getenv("UNIFI_ACCESS_TOKEN", "")
UNIFI_VERIFY_TLS = _bool("UNIFI_VERIFY_TLS", False)
# Dry run: no calls to the controller; a fake set of doors is used instead.
# On automatically when no host/token is configured.
DRY_RUN = _bool("DRY_RUN", not UNIFI_HOST or UNIFI_TOKEN in {"", "changeme"})

RECONCILE_SECONDS = int(os.getenv("RECONCILE_SECONDS", "30"))
DOOR_SYNC_MINUTES = int(os.getenv("DOOR_SYNC_MINUTES", "10"))
CALENDAR_SYNC_MINUTES = int(os.getenv("CALENDAR_SYNC_MINUTES", "5"))
CALENDAR_LOOKAHEAD_DAYS = int(os.getenv("CALENDAR_LOOKAHEAD_DAYS", "60"))
# Set to false in tests to keep the background scheduler from starting.
RUN_SCHEDULER = _bool("RUN_SCHEDULER", True)
# Set true when served over HTTPS (reverse proxy) so the session cookie is Secure.
SECURE_COOKIES = _bool("SECURE_COOKIES", False)


def secret_key() -> str:
    """SECRET_KEY from env, else a random key persisted in the data dir."""
    key = os.getenv("SECRET_KEY")
    if key:
        return key
    path = DATA_DIR / "secret_key"
    try:
        if path.exists():
            return path.read_text().strip()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(48)
        path.write_text(key)
        path.chmod(0o600)
        return key
    except OSError:
        # Data dir not writable: sessions won't survive a restart, but the app works.
        return secrets.token_urlsafe(48)
