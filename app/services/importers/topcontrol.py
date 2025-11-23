import csv
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional

from dbfread import DBF
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


def _rows_from_csv(csv_path: Path) -> Iterable[dict]:
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield row


def _rows_from_dbf(dbf_path: Path) -> Iterable[dict]:
    table = DBF(dbf_path, lowernames=True, ignore_missing_memofile=True)
    for record in table:
        yield dict(record)


def _import_rows(rows: Iterable[dict], session: Session) -> ImportResult:
    result = ImportResult()
    for row in rows:
        try:
            sku = row.get("KODTOV") or row.get("kodtov") or row.get("sku")
            name = row.get("TOVAR") or row.get("tovar") or row.get("name")
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
            product.brand = row.get("BRAND") or row.get("brand") or product.brand
            product.category = row.get("GRUPPA") or row.get("gruppa") or product.category
            product.abc_class = row.get("ABC_CLASS") or row.get("abc_class") or product.abc_class
            product.xyz_class = row.get("XYZ_CLASS") or row.get("xyz_class") or product.xyz_class

            qty = _parse_int(row.get("QUANTITY") or row.get("quantity"))
            purchase = _parse_decimal(
                row.get("PURCHASE_PRICE") or row.get("purchase_price") or row.get("purchase")
            )

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


def import_products_from_csv(csv_path: Path, session: Session) -> ImportResult:
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)
    return _import_rows(_rows_from_csv(csv_path), session)


def import_products_from_dbf(dbf_path: Path, session: Session) -> ImportResult:
    if not dbf_path.exists():
        raise FileNotFoundError(dbf_path)
    return _import_rows(_rows_from_dbf(dbf_path), session)


def import_products(path: Path, session: Session) -> ImportResult:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return import_products_from_csv(path, session)
    if suffix == ".dbf":
        return import_products_from_dbf(path, session)
    raise ValueError(f"Unsupported file extension: {suffix}")
