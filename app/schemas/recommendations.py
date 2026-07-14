from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel


class RecommendationResponse(BaseModel):
    article: str
    recommended_price: Decimal
    floor_price: Decimal
    reasons: list[str]
