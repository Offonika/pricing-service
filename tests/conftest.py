import os
import tempfile
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.main import app


@pytest.fixture(scope="session")
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def sqlite_engine() -> Generator:
    fd, path = tempfile.mkstemp(prefix="test_db_", suffix=".db")
    os.close(fd)
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


@pytest.fixture()
def db_session(sqlite_engine) -> Generator[Session, None, None]:
    from app.models import Base

    Base.metadata.create_all(sqlite_engine)
    session = Session(sqlite_engine)
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(sqlite_engine)
