from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.main import app
from app.models import Base, Product, ProductStock


def test_get_recommendation(tmp_path):
    db_path = tmp_path / "api.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        product = Product(sku="SKU-API", name="API Product")
        product.stock = ProductStock(purchase_price=Decimal("100"))
        session.add(product)
        session.commit()

    def override_get_db():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides = {get_db: override_get_db}
    client = TestClient(app)

    response = client.get("/api/products/SKU-API/recommendation?min_margin=0.2")

    assert response.status_code == 200
    data = response.json()
    assert data["sku"] == "SKU-API"
    assert Decimal(str(data["recommended_price"])) == Decimal("120")
    assert Decimal(str(data["floor_price"])) == Decimal("120")

    app.dependency_overrides = {}
