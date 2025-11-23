from __future__ import annotations

from decimal import Decimal
from typing import Iterable, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models import PriceRecommendation, Product, ProductStock


def _latest_recommendations_query(session: Session):
    subq = (
        session.query(
            PriceRecommendation.product_id,
            func.max(PriceRecommendation.created_at).label("max_created_at"),
        )
        .group_by(PriceRecommendation.product_id)
        .subquery()
    )
    return (
        session.query(PriceRecommendation, Product)
        .join(Product, PriceRecommendation.product_id == Product.id)
        .join(
            subq,
            (PriceRecommendation.product_id == subq.c.product_id)
            & (PriceRecommendation.created_at == subq.c.max_created_at),
        )
    )


def _apply_filters(query, brands: Optional[Iterable[str]], categories: Optional[Iterable[str]], search: Optional[str]):
    if brands:
        query = query.filter(Product.brand.in_(brands))
    if categories:
        query = query.filter(Product.category.in_(categories))
    if search:
        like = f"%{search}%"
        query = query.filter(or_(Product.sku.ilike(like), Product.name.ilike(like)))
    return query


def get_today_items(
    session: Session,
    limit: int = 20,
    brands: Optional[list[str]] = None,
    categories: Optional[list[str]] = None,
    search: Optional[str] = None,
):
    query = _latest_recommendations_query(session)
    query = _apply_filters(query, brands, categories, search)
    query = query.order_by(PriceRecommendation.created_at.desc())
    if limit:
        query = query.limit(limit)

    items: List[dict] = []
    for rec, product in query.all():
        stock: Optional[ProductStock] = product.stock
        purchase = Decimal(stock.purchase_price) if stock and stock.purchase_price is not None else None
        delta = None
        if purchase is not None:
            delta = Decimal(rec.recommended_price) - purchase
        items.append(
            {
                "sku": product.sku,
                "name": product.name,
                "brand": product.brand,
                "category": product.category,
                "recommended_price": rec.recommended_price,
                "purchase_price": purchase,
                "delta": delta,
                "reasons": rec.reasons or [],
            }
        )
    return items


def get_alerts(session: Session, limit: int = 20) -> List[dict]:
    """
    Простейшие алерты:
    - рекомендованная цена ниже закупки
    - нет закупочной цены, но есть рекомендация
    """
    query = _latest_recommendations_query(session).order_by(PriceRecommendation.created_at.desc())
    if limit:
        query = query.limit(limit * 2)

    alerts: List[dict] = []
    for rec, product in query.all():
        stock: Optional[ProductStock] = product.stock
        purchase = Decimal(stock.purchase_price) if stock and stock.purchase_price is not None else None
        if purchase is None:
            alerts.append(
                {
                    "sku": product.sku,
                    "name": product.name,
                    "brand": product.brand,
                    "category": product.category,
                    "recommended_price": rec.recommended_price,
                    "purchase_price": purchase,
                    "delta": None,
                    "reasons": rec.reasons or [],
                    "alert_reason": "no_purchase_price",
                }
            )
        elif rec.recommended_price < purchase:
            alerts.append(
                {
                    "sku": product.sku,
                    "name": product.name,
                    "brand": product.brand,
                    "category": product.category,
                    "recommended_price": rec.recommended_price,
                    "purchase_price": purchase,
                    "delta": rec.recommended_price - purchase,
                    "reasons": rec.reasons or [],
                    "alert_reason": "recommended_below_purchase",
                }
            )
        if len(alerts) >= limit:
            break
    return alerts
