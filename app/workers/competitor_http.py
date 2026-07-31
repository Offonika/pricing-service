from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db import get_application_engine
from app.services.importers.competitor_ftp import (
    CompetitorFtpImportError,
    FtpFileInfo,
    ingest_ftp_file,
)

logger = logging.getLogger("app.workers.competitor_http")


@dataclass(frozen=True)
class HttpSourceConfig:
    name: str
    url_pattern: str


def parse_http_sources(raw: str | None) -> list[HttpSourceConfig]:
    if not raw:
        return []
    sources: list[HttpSourceConfig] = []
    for entry in raw.split(","):
        item = entry.strip()
        if not item:
            continue
        name, separator, url_pattern = item.partition(":")
        name = name.strip()
        url_pattern = url_pattern.strip()
        if not separator or not name or not url_pattern or "{date}" not in url_pattern:
            logger.warning("http source skipped: expected name:https-url-with-{date}")
            continue
        parsed = urlparse(url_pattern)
        if parsed.scheme != "https" or not parsed.netloc:
            logger.warning("http source skipped: only absolute HTTPS URLs are allowed")
            continue
        sources.append(HttpSourceConfig(name=name, url_pattern=url_pattern))
    return sources


def _candidate_dates(limit: int, today: date | None = None) -> list[date]:
    current = today or datetime.now().astimezone().date()
    return [current - timedelta(days=offset) for offset in range(max(0, limit))]


def _response_mtime(response: httpx.Response) -> datetime | None:
    value = response.headers.get("Last-Modified")
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None


def run_competitor_http_import(session: Session | None = None) -> dict:
    settings = get_settings()
    if not settings.competitor_http_import_enabled:
        return {"skipped": True, "reason": "disabled"}

    sources = parse_http_sources(settings.competitor_http_sources)
    if not sources:
        return {"skipped": True, "reason": "missing_sources"}

    owns_session = session is None
    if owns_session:
        session = Session(get_application_engine())

    processed_files = rows_total = rows_valid = rows_invalid = errors = 0
    details: list[dict] = []
    try:
        for source in sources:
            entry: dict = {"name": source.name, "files": []}
            for file_date in _candidate_dates(settings.competitor_http_max_files_per_source):
                date_text = file_date.strftime("%Y.%m.%d")
                url = source.url_pattern.format(date=date_text)
                filename = urlparse(url).path.rsplit("/", 1)[-1]
                try:
                    response = httpx.get(
                        url,
                        timeout=settings.competitor_http_timeout_sec,
                    )
                    if response.status_code == 404:
                        entry["files"].append({"file": filename, "skipped": "not_found"})
                        continue
                    response.raise_for_status()
                    file_info = FtpFileInfo(
                        source=source.name,
                        directory=url.rsplit("/", 1)[0],
                        filename=filename,
                        path=url,
                        file_date=file_date,
                        mtime=_response_mtime(response),
                    )
                    stats = ingest_ftp_file(session, file_info, response.content)
                    session.commit()
                    entry["files"].append(stats)
                    processed_files += 1
                    rows_total += stats["rows_total"]
                    rows_valid += stats["rows_valid"]
                    rows_invalid += stats["rows_invalid"]
                except (httpx.HTTPError, CompetitorFtpImportError) as exc:
                    session.rollback()
                    errors += 1
                    entry["files"].append({"file": filename, "error": str(exc)})
                    logger.warning("competitor HTTPS import failed for %s", filename)
                except Exception:
                    session.rollback()
                    errors += 1
                    entry["files"].append({"file": filename, "error": "unexpected_error"})
                    logger.exception("competitor HTTPS import failed for %s", filename)
            details.append(entry)
    finally:
        if owns_session and session is not None:
            session.close()

    return {
        "skipped": False,
        "processed_files": processed_files,
        "rows_total": rows_total,
        "rows_valid": rows_valid,
        "rows_invalid": rows_invalid,
        "errors": errors,
        "sources": details,
    }


__all__ = ["HttpSourceConfig", "parse_http_sources", "run_competitor_http_import"]
