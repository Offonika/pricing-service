from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel


class TelegramItem(BaseModel):
    article: str
    name: str
    brand: str | None = None
    category: str | None = None
    recommended_price: Decimal
    purchase_price: Decimal | None = None
    delta: Decimal | None = None
    reasons: list[str]


class TelegramAlert(TelegramItem):
    alert_reason: str
