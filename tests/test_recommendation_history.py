from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, PriceRecommendation, Product, ProductStock
from app.services.pricing import (
    calculate_recommendation,
    get_or_create_strategy_version,
    record_recommendation,
)


def setup_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def test_record_recommendation_creates_history() -> None:
    engine = setup_db()
    with Session(engine) as session:
        product = Product(sku="SKU-1", name="Prod 1")
        product.stock = ProductStock(purchase_price=Decimal("100"))
        session.add(product)
        session.commit()

        strategy = get_or_create_strategy_version(session, name="base_v1")
        result = calculate_recommendation(
            product,
            competitor_min_price=Decimal("120"),
            min_margin_pct=Decimal("0.1"),
        )
        record_recommendation(
            session,
            product,
            result,
            strategy_version=strategy,
            competitor_min_price=Decimal("120"),
            min_margin_pct=Decimal("0.1"),
        )
        session.commit()

        stored = session.query(PriceRecommendation).one()
        assert stored.recommended_price == result.recommended_price
        assert stored.floor_price == result.floor_price
        assert stored.strategy_version_id == strategy.id
        assert stored.competitor_min_price == Decimal("120")
        assert stored.min_margin_pct == Decimal("0.1")
        assert stored.reasons


def test_get_or_create_strategy_version_idempotent() -> None:
    engine = setup_db()
    with Session(engine) as session:
        first = get_or_create_strategy_version(session, name="base_v1")
        second = get_or_create_strategy_version(session, name="base_v1")
        assert first.id == second.id
