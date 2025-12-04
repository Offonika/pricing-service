from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from ftplib import FTP, FTP_TLS
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.importers.competitor_ftp import (
    CompetitorFtpImportError,
    FtpFileInfo,
    FtpSourceConfig,
    download_file,
    ingest_ftp_file,
    list_matching_files,
    parse_sources,
)

logger = logging.getLogger("app.workers.competitor_ftp")


@dataclass
class ImportResult:
    processed_files: int = 0
    rows_total: int = 0
    rows_valid: int = 0
    rows_invalid: int = 0
    errors: int = 0


def _get_engine():
    settings = get_settings()
    return create_engine(settings.database_url)


def _connect_ftp(settings) -> FTP:
    if not settings.competitor_ftp_host:
        raise CompetitorFtpImportError("COMPETITOR_FTP_HOST is not set")
    ftp_cls = FTP_TLS if settings.competitor_ftp_tls else FTP
    ftp: FTP = ftp_cls(timeout=settings.competitor_ftp_timeout_sec)
    ftp.connect(settings.competitor_ftp_host, settings.competitor_ftp_port)
    ftp.login(settings.competitor_ftp_user, settings.competitor_ftp_password or "")
    if isinstance(ftp, FTP_TLS):
        ftp.prot_p()
    return ftp


def _select_files(
    files: List[FtpFileInfo],
    limit: int,
) -> List[FtpFileInfo]:
    if limit <= 0:
        return []
    return files[:limit]


def run_competitor_ftp_import(session: Optional[Session] = None) -> dict:
    settings = get_settings()
    if not settings.competitor_ftp_import_enabled:
        logger.info("ftp import skipped: feature disabled")
        return {"skipped": True, "reason": "disabled"}

    sources = parse_sources(settings.competitor_ftp_sources)
    if not sources:
        logger.warning("ftp import skipped: no sources configured")
        return {"skipped": True, "reason": "missing_sources"}

    owns_session = session is None
    if owns_session:
        engine = _get_engine()
        session = Session(engine)

    totals = ImportResult()
    details: List[dict] = []
    ftp: Optional[FTP] = None

    try:
        ftp = _connect_ftp(settings)
        for source in sources:
            entry: dict = {"name": source.name, "directory": source.directory, "pattern": source.pattern}
            try:
                candidates = list_matching_files(ftp, source)
            except CompetitorFtpImportError as exc:
                entry["error"] = str(exc)
                totals.errors += 1
                details.append(entry)
                logger.warning("ftp import failed to list files", extra={"source": source.name, "error": str(exc)})
                continue

            if not candidates:
                entry["skipped"] = "no_files"
                details.append(entry)
                continue

            selected = _select_files(candidates, settings.competitor_ftp_max_files_per_source)
            entry["files"] = []
            for file_info in selected:
                try:
                    payload = download_file(ftp, file_info.path)
                    stats = ingest_ftp_file(session, file_info, payload)
                    session.commit()
                    entry["files"].append(stats)
                    totals.processed_files += 1
                    totals.rows_total += stats["rows_total"]
                    totals.rows_valid += stats["rows_valid"]
                    totals.rows_invalid += stats["rows_invalid"]
                except CompetitorFtpImportError as exc:
                    if session:
                        session.rollback()
                    totals.errors += 1
                    entry["files"].append({"file": file_info.filename, "error": str(exc)})
                    logger.warning(
                        "ftp import failed for file",
                        extra={"source": source.name, "file": file_info.filename, "error": str(exc)},
                    )
                except Exception:
                    if session:
                        session.rollback()
                    totals.errors += 1
                    entry["files"].append({"file": file_info.filename, "error": "unexpected_error"})
                    logger.exception(
                        "ftp import failed for file %s (%s)", file_info.filename, source.name
                    )

            details.append(entry)
    finally:
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:
                ftp.close()
        if owns_session and session is not None:
            session.close()

    return {
        "skipped": False,
        "processed_files": totals.processed_files,
        "rows_total": totals.rows_total,
        "rows_valid": totals.rows_valid,
        "rows_invalid": totals.rows_invalid,
        "errors": totals.errors,
        "sources": details,
    }

