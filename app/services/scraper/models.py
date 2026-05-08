from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass
class CompetitorOffer:
    competitor: str
    external_sku: str
    name: str
    price_roz: Decimal
    price_opt: Decimal | None
    availability: bool
    url: str
    category: str | None
    collected_at: datetime
