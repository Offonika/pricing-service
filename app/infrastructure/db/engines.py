"""Canonical SQLAlchemy engine factories.

Application Postgres and read-only 1C MSSQL are intentionally separate.  The
factories are process-local and cached so API handlers, workers and CLI commands
reuse a pool instead of creating an engine per request.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import Pool

from app.core.config import get_settings


class DatabaseNotConfiguredError(RuntimeError):
    """Raised when a required database source is not configured."""


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
    return create_engine(database_url, **engine_options)


def build_onec_engine(
    database_url: str,
    *,
    query_timeout_seconds: int | float,
    login_timeout_seconds: int | float,
    poolclass: type[Pool] | None = None,
) -> Engine:
    """Build the read-only 1C MSSQL engine with bounded connection waits."""

    engine_options: dict[str, object] = {
        "connect_args": {
            "timeout": float(query_timeout_seconds),
            "login_timeout": float(login_timeout_seconds),
        },
        "pool_pre_ping": True,
    }
    if poolclass is not None:
        engine_options["poolclass"] = poolclass
    return create_engine(database_url, **engine_options)


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
