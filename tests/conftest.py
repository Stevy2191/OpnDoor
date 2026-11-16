import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="opndoor-test-")
os.environ.update(DATA_DIR=_tmp, DATABASE_URL=f"sqlite:///{_tmp}/test.db", RUN_SCHEDULER="false",
                  DRY_RUN="true", TZ="America/Chicago", SECRET_KEY="test")

import pytest  # noqa: E402
from sqlmodel import SQLModel, create_engine  # noqa: E402

from app import db, unifi  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/db.sqlite", connect_args={"check_same_thread": False})
    db.set_engine(engine)
    SQLModel.metadata.create_all(engine)
    fake = unifi.FakeUnifiClient()
    unifi.set_factory(lambda site: fake)  # every site shares one recorder
    yield fake
    unifi.set_factory(None)
    engine.dispose()


@pytest.fixture
def fake(fresh_db):
    return fresh_db
