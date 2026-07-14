from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


@dataclass
class RawNewsItem:
    title: str
    description: str | None
    url: str
    published_at: datetime | None
    source_name: str
    raw: dict[str, Any]


@dataclass
class NormalizedReleaseCandidate:
    is_phone_announcement: bool
    brand: str | None
    model: str | None
    announcement_date: date | None
    release_status: str | None
    models: list[str] | None = None
    market_release_date: date | None = None
    market_release_date_ru: date | None = None
    summary_ru: str | None = None
