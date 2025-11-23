from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel


class TelegramItem(BaseModel):
    sku: str
    name: str
    brand: Optional[str] = None
    category: Optional[str] = None
    recommended_price: Decimal
    purchase_price: Optional[Decimal] = None
    delta: Optional[Decimal] = None
    reasons: List[str]


class TelegramAlert(TelegramItem):
    alert_reason: str
