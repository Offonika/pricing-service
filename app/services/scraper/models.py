from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional


@dataclass
class CompetitorOffer:
    competitor: str
    external_sku: str
    name: str
    price_roz: Decimal
    price_opt: Optional[Decimal]
    availability: bool
    url: str
    category: Optional[str]
    collected_at: datetime
