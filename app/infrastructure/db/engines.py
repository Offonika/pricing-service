"""Canonical SQLAlchemy engine factories.

Application Postgres and read-only 1C MSSQL are intentionally separate.  The
factories are process-local and cached so API handlers, workers and CLI commands
reuse a pool instead of creating an engine per request.
"""

from __future__ import annotations

from functools import lru_cache
from math import ceil, isfinite
from typing import Any

from sqlalchemy import create_engine as sqlalchemy_create_engine
from sqlalchemy import event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import Pool

from app.core.config import get_settings


class DatabaseNotConfiguredError(RuntimeError):
    """Raised when a required database source is not configured."""


def build_engine(database_url: str, **engine_options: Any) -> Engine:
    """Compatibility factory for legacy CLI and maintenance commands.

    New application code should prefer the role-specific factories below.  The
    compatibility entrypoint keeps existing command options intact while making
    connection health checks mandatory and preventing direct SQLAlchemy engine
    construction outside this module.
    """

    engine_options.setdefault("pool_pre_ping", True)
    return sqlalchemy_create_engine(database_url, **engine_options)


def build_application_engine(
    database_url: str,
    *,
    pool_size: int | None = None,
    max_overflow: int | None = None,
    pool_timeout_seconds: float | None = None,
    pool_recycle_seconds: int | None = None,
) -> Engine:
    """Build the pricing Postgres engine with connection health checks."""

    engine_options: dict[str, object] = {"pool_pre_ping": True}
    if pool_size is not None:
        engine_options["pool_size"] = pool_size
    if max_overflow is not None:
        engine_options["max_overflow"] = max_overflow
    if pool_timeout_seconds is not None:
        engine_options["pool_timeout"] = pool_timeout_seconds
    if pool_recycle_seconds is not None:
        engine_options["pool_recycle"] = pool_recycle_seconds
    return sqlalchemy_create_engine(database_url, **engine_options)


def build_readonly_postgres_engine(
    database_url: str,
    *,
    pool_size: int = 1,
    max_overflow: int = 0,
) -> Engine:
    """Build a fail-closed PostgreSQL engine for a secondary read-only source."""

    if make_url(database_url).get_backend_name() != "postgresql":
        raise ValueError("read-only source must use PostgreSQL")
    return sqlalchemy_create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        connect_args={"options": "-c default_transaction_read_only=on"},
    )


def build_onec_engine(
    database_url: str,
    *,
    query_timeout_seconds: int | float,
    login_timeout_seconds: int | float,
    poolclass: type[Pool] | None = None,
) -> Engine:
    """Build the read-only 1C MSSQL engine with bounded connection waits."""

    if isinstance(query_timeout_seconds, bool) or isinstance(login_timeout_seconds, bool):
        raise ValueError("1C query and login timeouts must be finite and positive")
    query_timeout = float(query_timeout_seconds)
    login_timeout = float(login_timeout_seconds)
    if (
        not isfinite(query_timeout)
        or not isfinite(login_timeout)
        or query_timeout <= 0
        or login_timeout <= 0
    ):
        raise ValueError("1C query and login timeouts must be finite and positive")
    driver_name = make_url(database_url).drivername
    if driver_name.endswith("+pyodbc") or driver_name == "mssql":
        # pyodbc's connect-level ``timeout`` is a login timeout.  Its statement
        # timeout belongs to the cursor and is installed below.
        connect_args = {"timeout": max(1, ceil(login_timeout))}
    else:
        # python-tds documents ``timeout`` as the query timeout and
        # ``login_timeout`` as the connection/login timeout.
        connect_args = {
            "timeout": query_timeout,
            "login_timeout": login_timeout,
        }
    engine_options: dict[str, object] = {
        "connect_args": connect_args,
        "pool_pre_ping": True,
    }
    if poolclass is not None:
        engine_options["poolclass"] = poolclass
    engine = sqlalchemy_create_engine(database_url, **engine_options)
    if driver_name.endswith("+pyodbc") or driver_name == "mssql":
        statement_timeout = max(1, ceil(query_timeout))

        @event.listens_for(engine, "before_cursor_execute")
        def _set_pyodbc_statement_timeout(
            _connection,
            cursor,
            _statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            cursor.timeout = statement_timeout

    return engine


def build_onec_engine_from_settings(*, poolclass: type[Pool] | None = None) -> Engine:
    settings = get_settings()
    if not settings.onec_database_url:
        raise DatabaseNotConfiguredError("ONEC_DATABASE_URL is not configured")
    return build_onec_engine(
        settings.onec_database_url,
        query_timeout_seconds=settings.onec_query_timeout_seconds,
        login_timeout_seconds=settings.onec_login_timeout_seconds,
        poolclass=poolclass,
    )


@lru_cache(maxsize=8)
def _get_application_engine_cached(
    database_url: str,
    pool_size: int | None,
    max_overflow: int | None,
    pool_timeout_seconds: float | None,
    pool_recycle_seconds: int | None,
) -> Engine:
    return build_application_engine(
        database_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout_seconds=pool_timeout_seconds,
        pool_recycle_seconds=pool_recycle_seconds,
    )


def get_application_engine() -> Engine:
    settings = get_settings()
    return _get_application_engine_cached(
        settings.database_url,
        settings.database_pool_size,
        settings.database_max_overflow,
        settings.database_pool_timeout_seconds,
        settings.database_pool_recycle_seconds,
    )


get_application_engine.cache_clear = _get_application_engine_cached.cache_clear  # type: ignore[attr-defined]


@lru_cache(maxsize=8)
def _get_onec_engine_cached(
    database_url: str,
    query_timeout_seconds: int | float,
    login_timeout_seconds: int | float,
) -> Engine:
    return build_onec_engine(
        database_url,
        query_timeout_seconds=query_timeout_seconds,
        login_timeout_seconds=login_timeout_seconds,
    )


def get_onec_engine() -> Engine:
    settings = get_settings()
    if not settings.onec_database_url:
        raise DatabaseNotConfiguredError("ONEC_DATABASE_URL is not configured")
    return _get_onec_engine_cached(
        settings.onec_database_url,
        settings.onec_query_timeout_seconds,
        settings.onec_login_timeout_seconds,
    )


get_onec_engine.cache_clear = _get_onec_engine_cached.cache_clear  # type: ignore[attr-defined]
