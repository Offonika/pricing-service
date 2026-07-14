from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from io import BytesIO

from openpyxl import load_workbook


@dataclass
class CompetitorCatalogRecord:
    competitor: str
    external_id: str
    name: str
    category: str | None
    price_opt: Decimal | None
    price_roz: Decimal | None
    availability: bool | None
    url: str | None
    scraped_at: datetime


def _parse_bool(value) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    val = str(value).strip().lower()
    if val in {"true", "1", "yes", "y", "да"}:
        return True
    if val in {"false", "0", "no", "n", "нет"}:
        return False
    return None


def _parse_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except Exception:
        return None


def parse_zenlogs_xlsx(
    content: bytes,
    competitor: str,
    scraped_at: datetime,
) -> list[CompetitorCatalogRecord]:
    wb = load_workbook(BytesIO(content), read_only=True)
    ws = wb.active
    records: list[CompetitorCatalogRecord] = []
    rows = list(ws.rows)
    if not rows:
        return records
    headers = [str(cell.value).strip().lower() if cell.value else "" for cell in rows[0]]
    header_index = {name: idx for idx, name in enumerate(headers)}

    def get(row, key):
        idx = header_index.get(key)
        if idx is None or idx >= len(row):
            return None
        return row[idx].value

    for row in rows[1:]:
        sku = get(row, "sku")
        name = get(row, "name")
        if not sku or not name:
            continue
        record = CompetitorCatalogRecord(
            competitor=competitor,
            external_id=str(sku).strip(),
            name=str(name).strip(),
            category=(get(row, "group") or get(row, "category")),
            price_opt=_parse_decimal(get(row, "price_opt")),
            price_roz=_parse_decimal(get(row, "price_roz")),
            availability=_parse_bool(get(row, "stock") or get(row, "availability")),
            url=(str(get(row, "link")).strip() if get(row, "link") else None),
            scraped_at=scraped_at,
        )
        records.append(record)
    return records


__all__ = ["CompetitorCatalogRecord", "parse_zenlogs_xlsx"]
