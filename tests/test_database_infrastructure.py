from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.infrastructure.db.engines import build_application_engine
from app.infrastructure.db.session import session_scope
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork

REPO_ROOT = Path(__file__).resolve().parents[1]


def _factory():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE event (id INTEGER PRIMARY KEY, value TEXT)"))
    return engine, sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def test_alembic_graph_has_single_head() -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))

    heads = ScriptDirectory.from_config(config).get_heads()

    assert len(heads) == 1, f"expected one Alembic head, found: {heads}"


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


def test_read_only_session_scope_rolls_back_accidental_write(monkeypatch) -> None:
    engine, factory = _factory()
    monkeypatch.setattr(
        "app.infrastructure.db.session.get_application_session_factory",
        lambda: factory,
    )

    with session_scope(read_only=True) as session:
        session.execute(text("INSERT INTO event (id, value) VALUES (1, 'must rollback')"))

    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM event")).scalar_one() == 0


def test_read_only_session_scope_database_url_override_rolls_back(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'override.db'}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE event (id INTEGER PRIMARY KEY, value TEXT)"))
    engine.dispose()

    with session_scope(database_url=database_url, read_only=True) as session:
        session.execute(text("INSERT INTO event (id, value) VALUES (1, 'must rollback')"))

    verification_engine = create_engine(database_url)
    try:
        with verification_engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM event")).scalar_one() == 0
    finally:
        verification_engine.dispose()


def test_write_session_scope_commits_complete_command(monkeypatch) -> None:
    engine, factory = _factory()
    monkeypatch.setattr(
        "app.infrastructure.db.session.get_application_session_factory",
        lambda: factory,
    )

    with session_scope() as session:
        session.execute(text("INSERT INTO event (id, value) VALUES (1, 'committed')"))

    with engine.connect() as connection:
        assert connection.execute(text("SELECT value FROM event")).scalar_one() == "committed"
