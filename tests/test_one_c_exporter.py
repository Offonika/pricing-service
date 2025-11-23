from decimal import Decimal
from pathlib import Path

from app.services.exporters.one_c import ExportRow, export_prices_to_csv, export_recommendations
from app.services.pricing_strategies.base import PricingResult


def test_export_prices_to_csv(tmp_path: Path):
    rows = [
        ExportRow(sku="SKU1", price=100.5, comment="auto"),
        ExportRow(sku="SKU2", price=200.0, comment="manual"),
    ]
    output = export_prices_to_csv(rows, tmp_path / "prices.csv")

    assert output.exists()
    content = output.read_text(encoding="utf-8").splitlines()
    assert content[0] == "SKU,PRICE,COMMENT"
    assert "SKU1,100.5,auto" in content[1]


def test_export_recommendations(tmp_path: Path):
    recs = {
        "SKU1": PricingResult(recommended_price=Decimal("110"), floor_price=Decimal("100"), reasons=[]),
        "SKU2": PricingResult(recommended_price=Decimal("220"), floor_price=Decimal("200"), reasons=[]),
    }
    output = export_recommendations(recs, tmp_path / "rec.csv", price_comment="strategy:A")
    lines = output.read_text(encoding="utf-8").splitlines()

    assert any("SKU1,110.0,strategy:A" in line for line in lines)
    assert any("SKU2,220.0,strategy:A" in line for line in lines)
