import csv
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Competitor, CompetitorPrice, Product
from app.services.importers.competitors import ImportResult, import_competitor_prices_from_csv


def write_csv(tmp_path: Path, rows: List[Dict]) -> Path:
    path = tmp_path / "competitors.csv"
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_import_competitor_prices(tmp_path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    csv_path = write_csv(
        tmp_path,
        [
            {"sku": "SKU-1", "competitor": "CompA", "price": "100.50", "in_stock": "1"},
            {"sku": "SKU-1", "competitor": "CompA", "price": "95.00", "in_stock": "0"},
            {"sku": "SKU-2", "competitor": "CompB", "price": "50.00", "in_stock": "yes"},
        ],
    )

    with Session(engine) as session:
        session.add(Product(sku="SKU-1", name="Product 1"))
        session.add(Product(sku="SKU-2", name="Product 2"))
        session.commit()

        result: ImportResult = import_competitor_prices_from_csv(csv_path, session)

        assert result.created_competitors == 2
        assert result.created_prices == 3
        assert result.errors == 0

        prices = session.query(CompetitorPrice).order_by(CompetitorPrice.price).all()
        assert len(prices) == 3
        assert prices[0].product.sku == "SKU-2"
        assert prices[0].competitor.name == "CompB"


def test_import_competitor_prices_skips_missing_product(tmp_path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    csv_path = write_csv(
        tmp_path,
        [
            {"sku": "MISSING", "competitor": "CompA", "price": "10"},
        ],
    )
    with Session(engine) as session:
        result = import_competitor_prices_from_csv(csv_path, session)

        assert result.errors == 1
        assert session.query(Competitor).count() == 0
        assert session.query(CompetitorPrice).count() == 0
