from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.infrastructure.db.engines import (
    build_application_engine,
    build_onec_engine,
    build_readonly_postgres_engine,
)
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


def test_readonly_postgres_engine_enforces_server_side_read_only(monkeypatch) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_create_engine(database_url: str, **options):
        captured.update({"database_url": database_url, **options})
        return sentinel

    monkeypatch.setattr(
        "app.infrastructure.db.engines.sqlalchemy_create_engine", fake_create_engine
    )

    result = build_readonly_postgres_engine(
        "postgresql+psycopg2://readonly:secret@localhost/pricing"
    )

    assert result is sentinel
    assert captured["pool_pre_ping"] is True
    assert captured["pool_size"] == 1
    assert captured["max_overflow"] == 0
    assert captured["connect_args"] == {"options": "-c default_transaction_read_only=on"}


def test_readonly_postgres_engine_rejects_other_dialects() -> None:
    with pytest.raises(ValueError, match="must use PostgreSQL"):
        build_readonly_postgres_engine("sqlite:///unsafe.db")


def test_onec_pytds_engine_passes_distinct_query_and_login_timeouts(monkeypatch) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_create_engine(database_url: str, **options):
        captured.update({"database_url": database_url, **options})
        return sentinel

    monkeypatch.setattr(
        "app.infrastructure.db.engines.sqlalchemy_create_engine", fake_create_engine
    )

    result = build_onec_engine(
        "mssql+pytds://readonly:secret@onec/db",
        query_timeout_seconds=30,
        login_timeout_seconds=6,
    )

    assert result is sentinel
    assert captured["connect_args"] == {"timeout": 30.0, "login_timeout": 6.0}


def test_onec_engine_omits_mssql_timeouts_for_sqlite_fixture(monkeypatch) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_create_engine(database_url: str, **options):
        captured.update({"database_url": database_url, **options})
        return sentinel

    monkeypatch.setattr(
        "app.infrastructure.db.engines.sqlalchemy_create_engine", fake_create_engine
    )

    result = build_onec_engine(
        "sqlite:///:memory:",
        query_timeout_seconds=30,
        login_timeout_seconds=6,
    )

    assert result is sentinel
    assert captured == {
        "database_url": "sqlite:///:memory:",
        "pool_pre_ping": True,
    }


def test_onec_engine_rejects_unsupported_dialect() -> None:
    with pytest.raises(ValueError, match="1C source must use MSSQL"):
        build_onec_engine(
            "postgresql://readonly:secret@onec/db",
            query_timeout_seconds=30,
            login_timeout_seconds=6,
        )


def test_onec_pyodbc_engine_uses_login_timeout_and_cursor_query_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}
    listeners: list[tuple[object, str, object]] = []
    sentinel = object()

    def fake_create_engine(database_url: str, **options):
        captured.update({"database_url": database_url, **options})
        return sentinel

    def fake_listens_for(target, event_name):
        def register(listener):
            listeners.append((target, event_name, listener))
            return listener

        return register

    monkeypatch.setattr(
        "app.infrastructure.db.engines.sqlalchemy_create_engine", fake_create_engine
    )
    monkeypatch.setattr("app.infrastructure.db.engines.event.listens_for", fake_listens_for)

    result = build_onec_engine(
        "mssql+pyodbc://readonly:secret@onec/db?driver=ODBC+Driver+18+for+SQL+Server",
        query_timeout_seconds=30,
        login_timeout_seconds=6,
    )

    assert result is sentinel
    assert captured["connect_args"] == {"timeout": 6}
    assert len(listeners) == 1
    assert listeners[0][:2] == (sentinel, "before_cursor_execute")
    cursor = type("Cursor", (), {})()
    listeners[0][2](None, cursor, None, None, None, None)
    assert cursor.timeout == 30


@pytest.mark.parametrize(
    ("query_timeout", "login_timeout"),
    ((float("nan"), 6), (30, float("inf")), (0, 6), (30, -1), (True, 6)),
)
def test_onec_engine_rejects_non_finite_or_non_positive_timeouts(
    query_timeout: float,
    login_timeout: float,
) -> None:
    with pytest.raises(ValueError, match="timeouts must be finite and positive"):
        build_onec_engine(
            "mssql+pytds://readonly:secret@onec/db",
            query_timeout_seconds=query_timeout,
            login_timeout_seconds=login_timeout,
        )


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
