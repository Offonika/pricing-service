from __future__ import annotations

import io
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from ftplib import FTP, FTP_TLS
from typing import Iterable, List, Optional, Sequence

from openpyxl import load_workbook
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    CompetitorFtpFile,
    CompetitorFtpRawRow,
    CompetitorFtpRecord,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

logger = logging.getLogger("app.import.competitor_ftp")

MSK_TZ = ZoneInfo("Europe/Moscow")
DATE_PATTERN = r"(?P<date>\d{4}\.\d{2}\.\d{2})"
REQUIRED_COLUMNS = {"group", "sku", "name", "price_opt", "price_roz", "link", "time"}


class CompetitorFtpImportError(RuntimeError):
    """Raised when FTP import fails due to config/format errors."""


@dataclass
class FtpSourceConfig:
    name: str
    directory: str
    pattern: str
    regex: re.Pattern[str]


@dataclass
class FtpFileInfo:
    source: str
    directory: str
    filename: str
    path: str
    file_date: date
    mtime: Optional[datetime]


@dataclass
class ParsedRow:
    group_name: Optional[str]
    sku: Optional[str]
    name: Optional[str]
    price_opt: Optional[Decimal]
    price_roz: Optional[Decimal]
    link: Optional[str]
    stock: Optional[bool]
    amount: Optional[int]
    observed_at: Optional[datetime]
    error: Optional[str]

    @property
    def is_valid(self) -> bool:
        return self.error is None


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    if "{date}" not in pattern:
        raise CompetitorFtpImportError("pattern must contain {date}")
    escaped = re.escape(pattern).replace(r"\{date\}", DATE_PATTERN)
    return re.compile(f"^{escaped}$", re.IGNORECASE)


def parse_sources(raw: Optional[str]) -> List[FtpSourceConfig]:
    if not raw:
        return []
    sources: List[FtpSourceConfig] = []
    for entry in raw.split(","):
        item = entry.strip()
        if not item:
            continue
        parts = item.split(":", 2)
        if len(parts) != 3:
            logger.warning("ftp source skipped: expected name:directory:pattern", extra={"entry": item})
            continue
        name, directory, pattern = (part.strip() for part in parts)
        if not name or not directory or not pattern:
            logger.warning("ftp source skipped: empty name/directory/pattern", extra={"entry": item})
            continue
        try:
            regex = _compile_pattern(pattern)
        except CompetitorFtpImportError as exc:
            logger.warning("ftp source skipped: %s", exc, extra={"entry": item})
            continue
        sources.append(FtpSourceConfig(name=name, directory=directory, pattern=pattern, regex=regex))
    return sources


def _parse_file_date(filename: str, source: FtpSourceConfig) -> Optional[date]:
    match = source.regex.match(filename)
    if not match:
        return None
    date_str = match.group("date")
    try:
        return datetime.strptime(date_str, "%Y.%m.%d").date()
    except ValueError:
        return None


def _parse_mtime_from_mdtm(response: str) -> Optional[datetime]:
    # MDTM response: "213 20251130001510"
    try:
        ts = response.split()[-1]
        parsed = datetime.strptime(ts, "%Y%m%d%H%M%S")
        return parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def list_matching_files(
    ftp: FTP,
    source: FtpSourceConfig,
) -> List[FtpFileInfo]:
    """
    Lists files for the source directory that match the pattern with {date}.
    """
    try:
        entries = ftp.nlst(source.directory)
    except Exception as exc:  # pragma: no cover - network-dependent
        raise CompetitorFtpImportError(f"failed to list directory {source.directory}: {exc}") from exc
    files: List[FtpFileInfo] = []
    for path in entries:
        filename = os.path.basename(path)
        file_date = _parse_file_date(filename, source)
        if not file_date:
            continue
        mtime: Optional[datetime] = None
        try:
            resp = ftp.sendcmd(f"MDTM {path}")
            mtime = _parse_mtime_from_mdtm(resp)
        except Exception:
            pass
        files.append(
            FtpFileInfo(
                source=source.name,
                directory=source.directory,
                filename=filename,
                path=path,
                file_date=file_date,
                mtime=mtime,
            )
        )
    files.sort(key=lambda f: (f.file_date, f.mtime or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return files


def download_file(ftp: FTP, path: str) -> bytes:
    buffer = io.BytesIO()
    try:
        ftp.retrbinary(f"RETR {path}", buffer.write)
    except Exception as exc:  # pragma: no cover - network-dependent
        raise CompetitorFtpImportError(f"failed to download {path}: {exc}") from exc
    return buffer.getvalue()


def _normalize_bool(value: object) -> Optional[bool]:
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


def _normalize_decimal(value: object) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except Exception:
        return None


def _normalize_int(value: object) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(str(value).split(".")[0])
    except Exception:
        return None


def _normalize_datetime(value: object) -> Optional[datetime]:
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


def parse_ftp_xlsx(content: bytes, file_date: date, source: str) -> tuple[List[ParsedRow], bool]:
    workbook = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    sheet = workbook.active
    rows = list(sheet.rows)
    if not rows:
        logger.warning("ftp xlsx is empty", extra={"source": source})
        return [], False
    header_map = _extract_header_map((cell.value for cell in rows[0]))
    parsed: List[ParsedRow] = []
    date_mismatch = False
    for row in rows[1:]:
        values = [cell.value for cell in row]
        sku = _row_value(values, header_map, "sku")
        link = _row_value(values, header_map, "link")
        observed_at = _normalize_datetime(_row_value(values, header_map, "time"))
        amount = _normalize_int(_row_value(values, header_map, "amount")) if "amount" in header_map else None
        stock = _normalize_bool(_row_value(values, header_map, "stock")) if "stock" in header_map else None

        error: Optional[str] = None
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
        record = CompetitorFtpRecord(
            raw_row=raw,
            file=file_row,
            source=info.source,
            file_date=info.file_date,
            group_name=row.group_name,
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

