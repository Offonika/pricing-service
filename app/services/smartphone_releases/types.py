from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from typing import Any, Dict, Optional, List


@dataclass
class RawNewsItem:
    title: str
    description: Optional[str]
    url: str
    published_at: Optional[datetime]
    source_name: str
    raw: Dict[str, Any]


@dataclass
class NormalizedReleaseCandidate:
    is_phone_announcement: bool
    brand: Optional[str]
    model: Optional[str]
    announcement_date: Optional[date]
    release_status: Optional[str]
    models: Optional[List[str]] = None
    market_release_date: Optional[date] = None
    market_release_date_ru: Optional[date] = None
    summary_ru: Optional[str] = None
