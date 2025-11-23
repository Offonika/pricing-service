from datetime import datetime

from app.models import Competitor, CompetitorPrice, Product, ProductStock


def test_db_models_smoke(db_session) -> None:
    product = Product(sku="SKU-INT-1", name="Integration Product")
    product.stock = ProductStock(quantity=5, purchase_price=10.5)
    competitor = Competitor(name="Integration Competitor")
    price = CompetitorPrice(
        product=product,
        competitor=competitor,
        price=99.99,
        in_stock=True,
        collected_at=datetime.utcnow(),
    )

    db_session.add_all([product, competitor, price])
    db_session.commit()

    rows = db_session.query(CompetitorPrice).all()
    assert len(rows) == 1
    assert rows[0].product.sku == "SKU-INT-1"
    assert rows[0].product.stock.quantity == 5
    assert rows[0].competitor.name == "Integration Competitor"
