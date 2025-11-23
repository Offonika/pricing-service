from decimal import Decimal
from typing import List

from pydantic import BaseModel


class RecommendationResponse(BaseModel):
    sku: str
    recommended_price: Decimal
    floor_price: Decimal
    reasons: List[str]
