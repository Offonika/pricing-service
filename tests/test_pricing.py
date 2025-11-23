from decimal import Decimal

from app.models import Product, ProductStock
from app.services.pricing import calculate_recommendation


def test_pricing_uses_floor_when_no_competitors() -> None:
    product = Product(sku="SKU", name="Test")
    product.stock = ProductStock(purchase_price=Decimal("100"))

    result = calculate_recommendation(product, competitor_min_price=None, min_margin_pct=Decimal("0.2"))

    assert result.recommended_price == Decimal("120")
    assert "no competitor prices" in " ".join(result.reasons)


def test_pricing_uses_market_above_floor() -> None:
    product = Product(sku="SKU", name="Test")
    product.stock = ProductStock(purchase_price=Decimal("100"))

    result = calculate_recommendation(product, competitor_min_price=Decimal("150"), min_margin_pct=Decimal("0.1"))

    assert result.recommended_price == Decimal("150")
    assert any("market above floor" in r for r in result.reasons)


def test_pricing_uses_floor_if_market_below_floor() -> None:
    product = Product(sku="SKU", name="Test")
    product.stock = ProductStock(purchase_price=Decimal("100"))

    result = calculate_recommendation(product, competitor_min_price=Decimal("105"), min_margin_pct=Decimal("0.1"))

    assert result.recommended_price == Decimal("110")
    assert any("floor above market" in r for r in result.reasons)
