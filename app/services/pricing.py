from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.models import (
    PriceRecommendation,
    PricingStrategyVersion,
    Product,
    ProductStock,
)
from app.services.pricing_strategies.base import PricingContext, PricingResult, calculate_base_price


def calculate_recommendation(
    product: Product,
    competitor_min_price: Optional[Decimal],
    min_margin_pct: Decimal = Decimal("0.1"),
    demand_score: Optional[float] = None,
) -> PricingResult:
    stock: Optional[ProductStock] = product.stock
    purchase = stock.purchase_price if stock and stock.purchase_price else Decimal("0")
    context = PricingContext(
        purchase_price=purchase,
        min_margin_pct=min_margin_pct,
        competitor_min_price=competitor_min_price,
        demand_score=demand_score,
    )
    return calculate_base_price(context)


def get_or_create_strategy_version(
    session: Session,
    name: str = "base_v1",
    description: str = "Base margin strategy",
    parameters: Optional[dict] = None,
) -> PricingStrategyVersion:
    existing = session.query(PricingStrategyVersion).filter_by(name=name).first()
    if existing:
        return existing
    version = PricingStrategyVersion(name=name, description=description, parameters=parameters or {})
    session.add(version)
    session.flush()
    return version


def record_recommendation(
    session: Session,
    product: Product,
    result: PricingResult,
    strategy_version: Optional[PricingStrategyVersion] = None,
    competitor_min_price: Optional[Decimal] = None,
    min_margin_pct: Decimal = Decimal("0.1"),
) -> PriceRecommendation:
    """
    Сохраняет историю расчёта цены для продукта.
    """
    rec = PriceRecommendation(
        product_id=product.id,
        strategy_version_id=strategy_version.id if strategy_version else None,
        recommended_price=result.recommended_price,
        floor_price=result.floor_price,
        competitor_min_price=competitor_min_price,
        min_margin_pct=min_margin_pct,
        reasons=result.reasons,
    )
    session.add(rec)
    session.flush()
    return rec
