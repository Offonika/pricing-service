from decimal import Decimal
from typing import List

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Competitor, CompetitorPrice, Product
from app.schemas.reports import PriceChangeItem, SummaryReport
from app.services.pricing import calculate_recommendation


def build_summary(session: Session) -> SummaryReport:
    products_total = session.query(func.count(Product.id)).scalar() or 0
    active_products = (
        session.query(func.count(Product.id)).filter(Product.is_active.is_(True)).scalar() or 0
    )
    competitors_total = session.query(func.count(Competitor.id)).scalar() or 0
    competitor_prices = session.query(func.count(CompetitorPrice.id)).scalar() or 0
    return SummaryReport(
        products_total=products_total,
        products_active=active_products,
        competitors_total=competitors_total,
        competitor_prices_total=competitor_prices,
    )


def build_price_changes(session: Session, limit: int = 10) -> List[PriceChangeItem]:
    items: List[PriceChangeItem] = []
    for product in session.query(Product).limit(limit * 5).all():
        purchase = Decimal("0")
        if product.stock and product.stock.purchase_price:
            purchase = Decimal(product.stock.purchase_price)
        rec = calculate_recommendation(product, competitor_min_price=None)
        delta = rec.recommended_price - purchase
        items.append(
            PriceChangeItem(
                sku=product.sku,
                recommended_price=rec.recommended_price,
                purchase_price=purchase,
                delta=delta,
                reasons=rec.reasons,
            )
        )
    items.sort(key=lambda i: i.delta, reverse=True)
    return items[:limit]
