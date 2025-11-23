import csv
from pathlib import Path
from typing import Dict, List

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Product
from app.services.importers.topcontrol import ImportResult, import_products_from_csv


def write_csv(tmp_path: Path, rows: List[Dict]) -> Path:
    path = tmp_path / "topcontrol.csv"
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_import_products_from_csv(tmp_path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    rows = [
        {
            "KODTOV": "SKU-1",
            "TOVAR": "Product 1",
            "BRAND": "BrandA",
            "GRUPPA": "CatA",
            "ABC_CLASS": "A",
            "XYZ_CLASS": "X",
            "QUANTITY": "10",
            "PURCHASE_PRICE": "12.50",
        },
        {
            "KODTOV": "SKU-2",
            "TOVAR": "Product 2",
            "BRAND": "BrandB",
            "GRUPPA": "CatB",
            "QUANTITY": "5",
        },
    ]
    csv_path = write_csv(tmp_path, rows)

    with Session(engine) as session:
        result: ImportResult = import_products_from_csv(csv_path, session)

        assert result.created == 2
        assert result.updated == 0
        assert result.stock_updated == 2
        assert result.errors == 0

        products = session.query(Product).order_by(Product.sku).all()
        assert len(products) == 2
        assert products[0].stock.quantity == 10
        assert str(products[0].stock.purchase_price) == "12.50"


def test_import_products_from_csv_updates_existing(tmp_path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    csv_path = write_csv(
        tmp_path,
        [
            {"KODTOV": "SKU-1", "TOVAR": "Product 1", "BRAND": "BrandA"},
        ],
    )
    with Session(engine) as session:
        import_products_from_csv(csv_path, session)

        csv_path2 = write_csv(
            tmp_path,
            [
                {"KODTOV": "SKU-1", "TOVAR": "Product 1 updated", "BRAND": "BrandA"},
            ],
        )
        result = import_products_from_csv(csv_path2, session)
        product = session.query(Product).filter_by(sku="SKU-1").one()

        assert result.created == 0
        assert result.updated == 1
        assert product.name == "Product 1 updated"
