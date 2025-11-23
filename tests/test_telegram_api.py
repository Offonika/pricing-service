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
    Product,
    ProductStock,
    PriceRecommendation,
)


def setup_db():
    fd, path = tempfile.mkstemp(prefix="tg_test_", suffix=".db")
    os.close(fd)
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return engine, path


def seed_data(session: Session) -> None:
    prod_ok = Product(sku="OK-1", name="Good", brand="BrandA", category="Cat1")
    prod_ok.stock = ProductStock(quantity=5, purchase_price=Decimal("100"))

    prod_alert = Product(sku="AL-1", name="Bad", brand="BrandB", category="Cat2")
    prod_alert.stock = ProductStock(quantity=5, purchase_price=Decimal("150"))

    session.add_all([prod_ok, prod_alert])
    session.flush()

    session.add(
        PriceRecommendation(
            product_id=prod_ok.id,
            recommended_price=Decimal("120"),
            floor_price=Decimal("110"),
            competitor_min_price=None,
            min_margin_pct=Decimal("0.1"),
            reasons=["market above floor"],
            created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    )
    session.add(
        PriceRecommendation(
            product_id=prod_alert.id,
            recommended_price=Decimal("140"),
            floor_price=Decimal("140"),
            competitor_min_price=None,
            min_margin_pct=Decimal("0.1"),
            reasons=["below purchase"],
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


def test_today_and_alerts_filters() -> None:
    engine, path = setup_db()
    with Session(engine) as session:
        seed_data(session)

    app.dependency_overrides = {get_db: override_db(engine)}
    client = TestClient(app)

    resp = client.get("/api/telegram/today?brand=BrandA")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["sku"] == "OK-1"
    assert Decimal(str(items[0]["purchase_price"])) == Decimal("100")
    assert Decimal(str(items[0]["delta"])) == Decimal("20")

    alerts = client.get("/api/telegram/alerts").json()
    assert len(alerts) == 1
    assert alerts[0]["sku"] == "AL-1"
    assert alerts[0]["alert_reason"] == "recommended_below_purchase"

    app.dependency_overrides = {}
    engine.dispose()
    if os.path.exists(path):
        os.remove(path)
