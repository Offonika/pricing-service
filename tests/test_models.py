from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Competitor, CompetitorPrice, Product


def test_models_relationships_and_constraints() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        product = Product(
            article="SKU123",
            planned_sku="F5-DSP-IPH11-OLED-BLK-AAA",
            name="Test Product",
            brand="Brand",
            category="Cat",
        )
        competitor = Competitor(name="Test Competitor", website="https://example.com")
        price = CompetitorPrice(
            product=product,
            competitor=competitor,
            price=123.45,
            in_stock=True,
            collected_at=datetime.utcnow(),
        )

        session.add_all([product, competitor, price])
        session.commit()

        stored_price = session.query(CompetitorPrice).first()
        assert stored_price is not None
        assert stored_price.product.article == "SKU123"
        assert stored_price.product.planned_sku == "F5-DSP-IPH11-OLED-BLK-AAA"
        assert stored_price.competitor.name == "Test Competitor"
