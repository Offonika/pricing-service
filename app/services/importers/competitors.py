import csv
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Competitor, CompetitorPrice, Product

logger = logging.getLogger("app.import.competitors")


@dataclass
class ImportResult:
    created_prices: int = 0
    updated_prices: int = 0
    created_competitors: int = 0
    errors: int = 0


def _parse_decimal(value: Optional[str]) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except Exception:
        return None


def _parse_bool(value: Optional[str]) -> bool:
    if value is None:
        return False
    str_val = str(value).strip().lower()
    return str_val in {"1", "true", "yes", "y", "да", "есть", "in_stock"}


def _parse_datetime(value: Optional[str]) -> datetime:
    if not value:
        return datetime.utcnow()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return datetime.utcnow()


def _rows_from_csv(csv_path: Path) -> Iterable[dict]:
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield row


def import_competitor_prices_from_csv(csv_path: Path, session: Session) -> ImportResult:
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    result = ImportResult()
    for row in _rows_from_csv(csv_path):
        try:
            sku = row.get("SKU") or row.get("sku")
            competitor_name = row.get("competitor") or row.get("COMPETITOR")
            if not sku or not competitor_name:
                result.errors += 1
                logger.warning("skip row: missing sku or competitor: %s", row)
                continue

            product = session.execute(select(Product).where(Product.sku == sku)).scalar_one_or_none()
            if not product:
                result.errors += 1
                logger.warning("skip row: product not found for sku=%s", sku)
                continue

            competitor = (
                session.execute(select(Competitor).where(Competitor.name == competitor_name))
                .scalar_one_or_none()
            )
            if competitor is None:
                competitor = Competitor(name=competitor_name, website=row.get("url"))
                session.add(competitor)
                result.created_competitors += 1
            elif row.get("url"):
                competitor.website = row["url"]

            price_value = _parse_decimal(row.get("price") or row.get("PRICE"))
            in_stock = _parse_bool(row.get("in_stock") or row.get("availability"))
            collected_at = _parse_datetime(row.get("collected_at") or row.get("date"))

            price = CompetitorPrice(
                product=product,
                competitor=competitor,
                price=price_value or 0,
                in_stock=in_stock,
                collected_at=collected_at,
            )
            session.add(price)
            result.created_prices += 1
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            result.errors += 1
            logger.exception("failed to import competitor price row: %s", row, exc_info=exc)
        else:
            session.flush()

    session.commit()
    return result
