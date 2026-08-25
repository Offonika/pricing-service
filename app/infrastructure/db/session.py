"""Session factories and explicit read-only session scope."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .engines import build_application_engine, get_application_engine


@lru_cache(maxsize=8)
def _session_factory_for_engine(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def get_application_session_factory() -> sessionmaker[Session]:
    return _session_factory_for_engine(get_application_engine())


get_application_session_factory.cache_clear = _session_factory_for_engine.cache_clear  # type: ignore[attr-defined]


@contextmanager
def session_scope(*, read_only: bool = False, database_url: str | None = None) -> Iterator[Session]:
    """Open a session and close it deterministically.

    Write scopes commit on normal exit and roll back on error.  Read-only scopes
    never commit and are rolled back before close so an accidental mutation cannot
    leak into the database. ``database_url`` is an application-DB override for
    tests and one-off CLI runs; its dedicated engine is disposed with the scope.
    """

    temporary_engine = build_application_engine(database_url) if database_url else None
    session_factory = (
        sessionmaker(
            bind=temporary_engine,
            class_=Session,
            expire_on_commit=False,
        )
        if temporary_engine is not None
        else get_application_session_factory()
    )
    session = session_factory()
    try:
        yield session
        if read_only:
            session.rollback()
        else:
            session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        try:
            session.close()
        finally:
            if temporary_engine is not None:
                temporary_engine.dispose()
