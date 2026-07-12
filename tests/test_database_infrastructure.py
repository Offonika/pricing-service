from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.infrastructure.db.engines import build_application_engine
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork


def _factory():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE event (id INTEGER PRIMARY KEY, value TEXT)"))
    return engine, sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def test_unit_of_work_commits_complete_command() -> None:
    engine, factory = _factory()

    with SqlAlchemyUnitOfWork(factory) as uow:
        assert uow.session is not None
        uow.session.execute(text("INSERT INTO event (id, value) VALUES (1, 'ok')"))

    with engine.connect() as connection:
        assert connection.execute(text("SELECT value FROM event WHERE id = 1")).scalar_one() == "ok"


def test_unit_of_work_rolls_back_failed_command() -> None:
    engine, factory = _factory()

    with pytest.raises(RuntimeError, match="stop"):
        with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            uow.session.execute(text("INSERT INTO event (id, value) VALUES (1, 'partial')"))
            raise RuntimeError("stop")

    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM event")).scalar_one() == 0


def test_application_engine_enables_pre_ping_without_forcing_pool_overrides(tmp_path) -> None:
    engine = build_application_engine(f"sqlite:///{tmp_path / 'pool.db'}")
    try:
        assert engine.pool._pre_ping is True
    finally:
        engine.dispose()
