import os
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.main import app
from app.models import (
    Base,
    Competitor,
    CompetitorPrice,
    PriceRecommendation,
    PricingStrategyVersion,
    Product,
    ProductStock,
)


def setup_db():
    fd, path = tempfile.mkstemp(prefix="bi_test_", suffix=".db")
    os.close(fd)
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return engine, path


def seed_sample_data(session: Session) -> None:
    product = Product(sku="SKU-1", name="Prod 1", brand="BrandA", category="CatA", abc_class="A", xyz_class="X")
    product.stock = ProductStock(quantity=5, purchase_price=Decimal("100"))
    competitor = Competitor(name="CompA", website="https://compa.example.com")
    session.add_all([product, competitor])
    session.flush()

    session.add(
        CompetitorPrice(
            product_id=product.id,
            competitor_id=competitor.id,
            price=Decimal("120"),
            in_stock=True,
            collected_at=datetime.now(timezone.utc),
        )
    )
    strategy = PricingStrategyVersion(name="base_v1", description="Base", parameters={"min_margin": 0.1})
    session.add(strategy)
    session.flush()
    session.add(
        PriceRecommendation(
            product_id=product.id,
            strategy_version_id=strategy.id,
            recommended_price=Decimal("120"),
            floor_price=Decimal("110"),
            competitor_min_price=Decimal("120"),
            min_margin_pct=Decimal("0.1"),
            reasons=["market above floor"],
            created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    )
    session.commit()


def override_db(engine):
    def _override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    return _override


def test_bi_products_and_recommendations() -> None:
    engine, path = setup_db()
    with Session(engine) as session:
        seed_sample_data(session)

    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    resp = client.get("/api/bi/products")
    assert resp.status_code == 200
    products = resp.json()
    assert len(products) == 1
    assert products[0]["sku"] == "SKU-1"
    assert products[0]["purchase_price"] == 100.0

    resp_rec = client.get("/api/bi/recommendations")
    assert resp_rec.status_code == 200
    recs = resp_rec.json()
    assert len(recs) == 1
    assert recs[0]["sku"] == "SKU-1"
    assert Decimal(str(recs[0]["recommended_price"])) == Decimal("120")
    assert recs[0]["strategy_name"] == "base_v1"

    resp_prices = client.get("/api/bi/competitor-prices")
    assert resp_prices.status_code == 200
    prices = resp_prices.json()
    assert len(prices) == 1
    assert prices[0]["competitor"] == "CompA"

    app.dependency_overrides = {}
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)
