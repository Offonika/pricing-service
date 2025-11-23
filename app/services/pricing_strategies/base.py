from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass
class PricingContext:
    purchase_price: Decimal
    min_margin_pct: Decimal = Decimal("0.1")  # 10% по умолчанию
    competitor_min_price: Optional[Decimal] = None


@dataclass
class PricingResult:
    recommended_price: Decimal
    floor_price: Decimal
    reasons: list[str]


def calculate_base_price(context: PricingContext) -> PricingResult:
    reasons: list[str] = []
    floor_price = context.purchase_price * (Decimal("1.0") + context.min_margin_pct)
    reasons.append(f"floor = purchase * (1 + min_margin_pct) = {floor_price}")

    market_price = context.competitor_min_price
    if market_price is None:
        reasons.append("no competitor prices; using floor price")
        return PricingResult(recommended_price=floor_price, floor_price=floor_price, reasons=reasons)

    if floor_price > market_price:
        reasons.append("floor above market; using floor to keep margin")
        recommended = floor_price
    else:
        recommended = market_price
        reasons.append("market above floor; using competitor min price")

    return PricingResult(recommended_price=recommended, floor_price=floor_price, reasons=reasons)
