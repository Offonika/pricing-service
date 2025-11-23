from decimal import Decimal
from typing import Optional

from app.models import Product, ProductStock
from app.services.pricing_strategies.base import PricingContext, PricingResult, calculate_base_price


def calculate_recommendation(
    product: Product,
    competitor_min_price: Optional[Decimal],
    min_margin_pct: Decimal = Decimal("0.1"),
) -> PricingResult:
    stock: Optional[ProductStock] = product.stock
    purchase = stock.purchase_price if stock and stock.purchase_price else Decimal("0")
    context = PricingContext(
        purchase_price=purchase,
        min_margin_pct=min_margin_pct,
        competitor_min_price=competitor_min_price,
    )
    return calculate_base_price(context)
