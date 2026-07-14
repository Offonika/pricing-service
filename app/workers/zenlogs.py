from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db import get_application_engine
from app.services.importers.competitor_catalog import upsert_catalog_records
from app.services.importers.zenlogs_moba import CompetitorCatalogRecord, parse_zenlogs_xlsx

logger = logging.getLogger("app.workers.zenlogs")

UTC = timezone.utc


def load_zenlogs_catalog(
    competitor: str,
    url: str,
    timeout: float,
    verify_ssl: bool = True,
) -> list[CompetitorCatalogRecord]:
    resp = requests.get(url, timeout=timeout, verify=verify_ssl)
    resp.raise_for_status()
    content = resp.content
    return parse_zenlogs_xlsx(
        content,
        competitor=competitor,
        scraped_at=datetime.now(UTC),
    )


@dataclass
class ZenlogsRunResult:
    processed: int = 0
    sources: list[tuple[str, int]] = None
    skipped: bool = False
    reason: str | None = None


def run_zenlogs_moba_import(session: Session | None = None) -> dict:
    settings = get_settings()
    if settings.competitor_source_mode != "zenno" or not settings.zenlogs_import_enabled:
        return {"skipped": True, "reason": "disabled_or_internal"}

    sources: list[tuple[str, str]] = []
    if settings.zenlogs_sources:
        for part in settings.zenlogs_sources.split(","):
            if not part:
                continue
            name, url = part.split(":", 1)
            sources.append((name, url))
    elif settings.zenlogs_moba_url:
        sources.append(("moba", settings.zenlogs_moba_url))
    else:
        return {"skipped": True, "reason": "no_sources"}

    created_session = False
    if session is None:
        session = Session(get_application_engine())
        created_session = True

    total_processed = 0
    source_results: list[dict] = []
    for name, url in sources:
        try:
            records = load_zenlogs_catalog(
                competitor=name,
                url=url,
                timeout=settings.zenlogs_http_timeout_sec,
                verify_ssl=settings.zenlogs_verify_ssl,
            )
            stats = upsert_catalog_records(session, records)
            total_processed += len(records)
            source_results.append(
                {"source": name, "records": len(records), "items_created": stats.items_created}
            )
        except Exception as exc:
            logger.exception("zenlogs import failed for %s: %s", name, exc)
            source_results.append({"source": name, "error": str(exc)})
    if created_session:
        session.commit()
        session.close()

    return {"skipped": False, "processed": total_processed, "sources": source_results}


__all__ = ["run_zenlogs_moba_import", "load_zenlogs_catalog"]
