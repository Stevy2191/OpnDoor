from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from . import config
from . import models  # noqa: F401  (register tables)

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        url = config.DATABASE_URL
        if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(_engine, "connect")
        def _pragmas(conn, _):
            cur = conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    return _engine


def set_engine(engine) -> None:
    """Used by tests."""
    global _engine
    _engine = engine


def init_db() -> None:
    SQLModel.metadata.create_all(get_engine())


@contextmanager
def session_scope():
    with Session(get_engine()) as s:
        yield s


def get_session():
    with Session(get_engine()) as s:
        yield s
