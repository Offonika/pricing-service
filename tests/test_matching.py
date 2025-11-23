from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Competitor, Product, ProductMatch
from app.services.matching import match_by_sku


def test_match_by_sku_creates_links() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                Product(sku="P1", name="Product 1"),
                Product(sku="P2", name="Product 2"),
                Competitor(name="CompA"),
            ]
        )
        session.commit()

        created = match_by_sku(session, [("P1", "CompA"), ("P2", "CompB")])

        assert created == 2
        matches = session.query(ProductMatch).all()
        assert len(matches) == 2
        assert any(m.product.sku == "P1" and m.competitor.name == "CompA" for m in matches)
        assert any(m.product.sku == "P2" and m.competitor.name == "CompB" for m in matches)


def test_match_by_sku_skips_duplicates() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        product = Product(sku="P1", name="Product 1")
        competitor = Competitor(name="CompA")
        session.add_all([product, competitor])
        session.commit()
        match_by_sku(session, [("P1", "CompA")])

        created = match_by_sku(session, [("P1", "CompA")])
        assert created == 0
        assert session.query(ProductMatch).count() == 1
