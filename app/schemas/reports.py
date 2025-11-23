from decimal import Decimal
from typing import List

from pydantic import BaseModel


class SummaryReport(BaseModel):
    products_total: int
    products_active: int
    competitors_total: int
    competitor_prices_total: int


class PriceChangeItem(BaseModel):
    sku: str
    recommended_price: Decimal
    purchase_price: Decimal
    delta: Decimal
    reasons: List[str]
