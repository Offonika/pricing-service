from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.main import app
from app.models import Base, Product, ProductStock


def setup_db(path):
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        p1 = Product(sku="SKU-R1", name="Report 1")
        p1.stock = ProductStock(purchase_price=Decimal("50"))
        p2 = Product(sku="SKU-R2", name="Report 2")
        p2.stock = ProductStock(purchase_price=Decimal("80"))
        session.add_all([p1, p2])
        session.commit()
    return engine


def test_summary_and_price_changes(tmp_path):
    engine = setup_db(tmp_path / "reports.db")

    def override_get_db():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides = {get_db: override_get_db}
    client = TestClient(app)

    summary = client.get("/api/reports/summary")
    assert summary.status_code == 200
    assert summary.json()["products_total"] == 2

    changes = client.get("/api/reports/price-changes?limit=1")
    assert changes.status_code == 200
    data = changes.json()
    assert len(data) == 1
    assert data[0]["sku"] in {"SKU-R1", "SKU-R2"}

    app.dependency_overrides = {}
