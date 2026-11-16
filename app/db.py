from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event, inspect
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
        apply_pragmas(_engine)
    return _engine


def apply_pragmas(engine) -> None:
    """Enforce foreign keys (and use WAL) on every SQLite connection."""
    @event.listens_for(engine, "connect")
    def _pragmas(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


def set_engine(engine) -> None:
    """Used by tests."""
    global _engine
    _engine = engine


def init_db() -> None:
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    with engine.begin() as conn:
        # Installs from before the "main admin" existed: the earliest admin becomes it.
        conn.exec_driver_sql(
            'UPDATE "user" SET is_owner = 1 WHERE id = (SELECT MIN(id) FROM "user" WHERE is_admin = 1) '
            'AND NOT EXISTS (SELECT 1 FROM "user" WHERE is_owner = 1)')


def _sql_literal(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _add_missing_columns(engine) -> None:
    """Simple upgrade: add columns that newer versions of a table have.

    Only handles added columns (that's all OpnDoor's upgrades do so far). Existing
    rows get the field's default value.
    """
    models = {m.__tablename__: m for m in SQLModel.__subclasses__() if hasattr(m, "__tablename__")}
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                sql = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(dialect=engine.dialect)}'
                model = models.get(table.name)
                field = model.model_fields.get(col.name) if model else None
                default = getattr(field, "default", None) if field else None
                if default is not None and isinstance(default, (str, int, float, bool)):
                    sql += f" DEFAULT {_sql_literal(default)}"
                conn.exec_driver_sql(sql)


@contextmanager
def session_scope():
    with Session(get_engine()) as s:
        yield s


def get_session():
    with Session(get_engine()) as s:
        yield s
