from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel


class BIProduct(BaseModel):
    sku: str
    name: str
    brand: Optional[str] = None
    category: Optional[str] = None
    abc_class: Optional[str] = None
    xyz_class: Optional[str] = None
    is_active: bool
    stock_quantity: Optional[int] = None
    purchase_price: Optional[float] = None


class BIRecommendation(BaseModel):
    sku: str
    recommended_price: Decimal
    floor_price: Decimal
    competitor_min_price: Optional[Decimal] = None
    min_margin_pct: Decimal
    strategy_name: Optional[str] = None
    created_at: datetime
    reasons: List[str]


class BICompetitorPrice(BaseModel):
    sku: str
    competitor: str
    price: Decimal
    in_stock: bool
    collected_at: datetime
