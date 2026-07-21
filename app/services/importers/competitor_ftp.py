from __future__ import annotations

import io
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from openpyxl import load_workbook
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    CompetitorFtpFile,
    CompetitorFtpRawRow,
    CompetitorFtpRecord,
)
from app.services.competitor_category import canonicalize_category

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

logger = logging.getLogger("app.import.competitor_ftp")

MSK_TZ = ZoneInfo("Europe/Moscow")
REQUIRED_COLUMNS = {"group", "sku", "name", "price_opt", "price_roz", "link", "time"}


class CompetitorFtpImportError(RuntimeError):
    """Raised when a competitor XLSX fails format validation."""


@dataclass
class FtpFileInfo:
    source: str
    directory: str
    filename: str
    path: str
    file_date: date
    mtime: datetime | None


@dataclass
class ParsedRow:
    group_name: str | None
    sku: str | None
    name: str | None
    price_opt: Decimal | None
    price_roz: Decimal | None
    link: str | None
    stock: bool | None
    amount: int | None
    observed_at: datetime | None
    error: str | None

    @property
    def is_valid(self) -> bool:
        return self.error is None


def _normalize_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    str_val = str(value).strip().lower()
    if not str_val:
        return None
    if str_val in {"1", "true", "yes", "y", "да"}:
        return True
    if str_val in {"0", "false", "no", "n", "нет"}:
        return False
    return None


def _normalize_decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except Exception:
        return None


def _normalize_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).split(".")[0])
    except Exception:
        return None


def _normalize_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.strptime(text, "%Y.%m.%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK_TZ)
    else:
        dt = dt.astimezone(MSK_TZ)
    return dt


def _extract_header_map(first_row: Iterable[object]) -> dict[str, int]:
    header_map: dict[str, int] = {}
    for idx, value in enumerate(first_row):
        if value is None:
            continue
        header_map[str(value).strip().lower()] = idx
    missing = REQUIRED_COLUMNS - set(header_map.keys())
    if missing:
        raise CompetitorFtpImportError(f"missing columns: {', '.join(sorted(missing))}")
    return header_map


def _row_value(row: Sequence[object], header_map: dict[str, int], column: str) -> object:
    idx = header_map[column]
    return row[idx] if idx < len(row) else None


def parse_ftp_xlsx(content: bytes, file_date: date, source: str) -> tuple[list[ParsedRow], bool]:
    workbook = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    sheet = workbook.active
    rows = list(sheet.rows)
    if not rows:
        logger.warning("ftp xlsx is empty", extra={"source": source})
        return [], False
    header_map = _extract_header_map(cell.value for cell in rows[0])
    parsed: list[ParsedRow] = []
    date_mismatch = False
    for row in rows[1:]:
        values = [cell.value for cell in row]
        sku = _row_value(values, header_map, "sku")
        link = _row_value(values, header_map, "link")
        observed_at = _normalize_datetime(_row_value(values, header_map, "time"))
        amount = (
            _normalize_int(_row_value(values, header_map, "amount"))
            if "amount" in header_map
            else None
        )
        stock = (
            _normalize_bool(_row_value(values, header_map, "stock"))
            if "stock" in header_map
            else None
        )

        error: str | None = None
        if not sku or not link:
            error = "missing sku or link"
        elif observed_at is None:
            error = "time is not parseable"
        elif amount is None and stock is None:
            error = "missing amount/stock"

        if observed_at and observed_at.date() != file_date:
            date_mismatch = True

        parsed.append(
            ParsedRow(
                group_name=_row_value(values, header_map, "group") or None,
                sku=str(sku).strip() if sku else None,
                name=_row_value(values, header_map, "name") or None,
                price_opt=_normalize_decimal(_row_value(values, header_map, "price_opt")),
                price_roz=_normalize_decimal(_row_value(values, header_map, "price_roz")),
                link=str(link).strip() if link else None,
                stock=stock,
                amount=amount,
                observed_at=observed_at,
                error=error,
            )
        )
    return parsed, date_mismatch


def _ensure_file_record(
    session: Session,
    info: FtpFileInfo,
) -> CompetitorFtpFile:
    existing = session.execute(
        select(CompetitorFtpFile).where(
            CompetitorFtpFile.source == info.source,
            CompetitorFtpFile.file_date == info.file_date,
        )
    ).scalar_one_or_none()
    if existing:
        session.execute(
            delete(CompetitorFtpRecord).where(CompetitorFtpRecord.file_id == existing.id)
        )
        session.execute(
            delete(CompetitorFtpRawRow).where(CompetitorFtpRawRow.file_id == existing.id)
        )
        existing.filename = info.filename
        existing.file_path = info.path
        existing.mtime = info.mtime
        return existing
    file_row = CompetitorFtpFile(
        source=info.source,
        filename=info.filename,
        file_path=info.path,
        file_date=info.file_date,
        mtime=info.mtime,
    )
    session.add(file_row)
    session.flush()
    return file_row


def ingest_ftp_file(
    session: Session,
    info: FtpFileInfo,
    content: bytes,
) -> dict:
    rows, date_mismatch = parse_ftp_xlsx(content, file_date=info.file_date, source=info.source)
    file_row = _ensure_file_record(session, info)
    file_row.rows_total = len(rows)
    file_row.rows_valid = 0
    file_row.rows_invalid = 0
    file_row.date_mismatch = date_mismatch

    for idx, row in enumerate(rows, start=2):  # start=2 to reflect Excel row numbers
        raw = CompetitorFtpRawRow(
            file_id=file_row.id,
            row_index=idx,
            source=info.source,
            file_date=info.file_date,
            group_name=row.group_name,
            sku=row.sku,
            name=row.name,
            price_opt=row.price_opt,
            price_roz=row.price_roz,
            link=row.link,
            stock=row.stock,
            amount=row.amount,
            observed_at=row.observed_at,
            error=row.error,
            is_valid=row.is_valid,
        )
        session.add(raw)
        if not row.is_valid:
            file_row.rows_invalid += 1
            continue

        in_stock = (row.amount or 0) > 0 if row.amount is not None else bool(row.stock)
        normalized_group = canonicalize_category(row.group_name)
        record = CompetitorFtpRecord(
            raw_row=raw,
            file=file_row,
            source=info.source,
            file_date=info.file_date,
            group_name=normalized_group,
            sku=row.sku or "",
            name=row.name,
            price_opt=row.price_opt,
            price_roz=row.price_roz,
            link=row.link,
            in_stock=in_stock,
            amount=row.amount,
            observed_at=row.observed_at or datetime.now(tz=MSK_TZ),
        )
        session.add(record)
        file_row.rows_valid += 1

    session.flush()
    return {
        "source": info.source,
        "file": info.filename,
        "file_date": info.file_date.isoformat(),
        "rows_total": file_row.rows_total,
        "rows_valid": file_row.rows_valid,
        "rows_invalid": file_row.rows_invalid,
        "date_mismatch": file_row.date_mismatch,
    }
