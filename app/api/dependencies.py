from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings


def get_engine():
    settings = get_settings()
    return create_engine(settings.database_url)


def get_db() -> Generator[Session, None, None]:
    engine = get_engine()
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
