import csv
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Product, ProductStock

logger = logging.getLogger("app.import.topcontrol")


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    stock_updated: int = 0
    errors: int = 0


def _parse_decimal(value: Optional[str]) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except Exception:
        return None


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def import_products_from_csv(csv_path: Path, session: Session) -> ImportResult:
    """
    Импорт CSV из TopControl. Ожидаемые поля (кейсы не важны):
    - KODTOV/sku
    - TOVAR/name
    - BRAND
    - GRUPPA/category
    - ABC_CLASS, XYZ_CLASS
    - QUANTITY
    - PURCHASE_PRICE
    """
    result = ImportResult()
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                sku = row.get("KODTOV") or row.get("sku")
                name = row.get("TOVAR") or row.get("name")
                if not sku or not name:
                    result.errors += 1
                    logger.warning("skip row: missing sku or name: %s", row)
                    continue

                product = session.execute(select(Product).where(Product.sku == sku)).scalar_one_or_none()
                is_new = product is None
                if is_new:
                    product = Product(sku=sku, name=name)
                    session.add(product)
                product.name = name
                product.brand = row.get("BRAND") or product.brand
                product.category = row.get("GRUPPA") or product.category
                product.abc_class = row.get("ABC_CLASS") or product.abc_class
                product.xyz_class = row.get("XYZ_CLASS") or product.xyz_class

                qty = _parse_int(row.get("QUANTITY") or row.get("quantity"))
                purchase = _parse_decimal(row.get("PURCHASE_PRICE") or row.get("purchase_price"))

                if qty is not None or purchase is not None:
                    if product.stock:
                        if qty is not None:
                            product.stock.quantity = qty
                        if purchase is not None:
                            product.stock.purchase_price = purchase
                    else:
                        product.stock = ProductStock(
                            quantity=qty or 0,
                            purchase_price=purchase,
                        )
                    result.stock_updated += 1

                if is_new:
                    result.created += 1
                else:
                    result.updated += 1
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                result.errors += 1
                logger.exception("failed to import row: %s", row, exc_info=exc)
            else:
                session.flush()

    session.commit()
    return result
