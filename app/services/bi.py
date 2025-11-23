from __future__ import annotations

from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    Competitor,
    CompetitorPrice,
    PriceRecommendation,
    PricingStrategyVersion,
    Product,
    ProductStock,
)


def _purchase_for_product(product: Product) -> Optional[float]:
    stock: Optional[ProductStock] = product.stock
    if stock and stock.purchase_price is not None:
        return float(stock.purchase_price)
    return None


def get_products_dataset(session: Session, limit: int = 100) -> List[dict]:
    query = session.query(Product).order_by(Product.id)
    if limit:
        query = query.limit(limit)
    products = []
    for product in query.all():
        products.append(
            {
                "sku": product.sku,
                "name": product.name,
                "brand": product.brand,
                "category": product.category,
                "abc_class": product.abc_class,
                "xyz_class": product.xyz_class,
                "is_active": product.is_active,
                "stock_quantity": product.stock.quantity if product.stock else None,
                "purchase_price": _purchase_for_product(product),
            }
        )
    return products


def get_latest_recommendations(session: Session, limit: int = 100) -> List[dict]:
    subq = (
        session.query(
            PriceRecommendation.product_id,
            func.max(PriceRecommendation.created_at).label("max_created_at"),
        )
        .group_by(PriceRecommendation.product_id)
        .subquery()
    )
    query = (
        session.query(
            PriceRecommendation,
            Product,
            PricingStrategyVersion,
        )
        .join(Product, PriceRecommendation.product_id == Product.id)
        .outerjoin(PricingStrategyVersion, PriceRecommendation.strategy_version_id == PricingStrategyVersion.id)
        .join(
            subq,
            (PriceRecommendation.product_id == subq.c.product_id)
            & (PriceRecommendation.created_at == subq.c.max_created_at),
        )
        .order_by(PriceRecommendation.created_at.desc())
    )
    if limit:
        query = query.limit(limit)

    rows: List[dict] = []
    for rec, product, strategy in query.all():
        rows.append(
            {
                "sku": product.sku,
                "recommended_price": rec.recommended_price,
                "floor_price": rec.floor_price,
                "competitor_min_price": rec.competitor_min_price,
                "min_margin_pct": rec.min_margin_pct,
                "strategy_name": strategy.name if strategy else None,
                "created_at": rec.created_at,
                "reasons": rec.reasons,
            }
        )
    return rows


def get_competitor_prices(session: Session, limit: int = 100) -> List[dict]:
    query = (
        session.query(CompetitorPrice, Product, Competitor)
        .join(Product, CompetitorPrice.product_id == Product.id)
        .join(Competitor, CompetitorPrice.competitor_id == Competitor.id)
        .order_by(CompetitorPrice.collected_at.desc())
    )
    if limit:
        query = query.limit(limit)

    rows: List[dict] = []
    for price, product, competitor in query.all():
        rows.append(
            {
                "sku": product.sku,
                "competitor": competitor.name,
                "price": price.price,
                "in_stock": price.in_stock,
                "collected_at": price.collected_at,
            }
        )
    return rows
