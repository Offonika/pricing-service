from __future__ import annotations

import logging
from datetime import date
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import ReleaseStatus, SmartphoneRelease, SourceType
from app.core.config import get_settings
from app.services.smartphone_releases.news_client import SmartphoneNewsClient, build_news_client_from_settings
from app.services.smartphone_releases.normalizer import (
    SmartphoneReleaseNormalizer,
    build_normalizer_from_settings,
)
from app.services.smartphone_releases.types import NormalizedReleaseCandidate, RawNewsItem
from app.services.smartphone_releases.gsmarena_client import build_gsmarena_client_from_settings

HIGHLIGHT_LIMIT = 5
HIGHLIGHT_SUMMARY_MAX_LEN = 240


class SmartphoneReleaseRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_source(self, source_name: str, source_url: str) -> Optional[SmartphoneRelease]:
        return (
            self.db.query(SmartphoneRelease)
            .filter(
                SmartphoneRelease.source_name == source_name,
                SmartphoneRelease.source_url == source_url,
            )
            .first()
        )

    def get_by_identity(
        self, brand: str, model: str, announcement_date: Optional[date]
    ) -> Optional[SmartphoneRelease]:
        query = self.db.query(SmartphoneRelease).filter(
            SmartphoneRelease.brand == brand,
            SmartphoneRelease.model == model,
        )
        if announcement_date is None:
            query = query.filter(SmartphoneRelease.announcement_date.is_(None))
        else:
            query = query.filter(SmartphoneRelease.announcement_date == announcement_date)
        return query.first()

    def create(self, payload: Dict) -> SmartphoneRelease:
        release = SmartphoneRelease(**payload)
        self.db.add(release)
        self.db.flush()
        return release

    def update(self, release: SmartphoneRelease, payload: Dict) -> SmartphoneRelease:
        for key, value in payload.items():
            # Не затираем существующие данные пустыми значениями, кроме raw_payload.
            if value is None and getattr(release, key, None) is not None and key != "raw_payload":
                continue
            setattr(release, key, value)
        self.db.add(release)
        self.db.flush()
        return release


class SmartphoneReleaseService:
    """Сервис сквозного обновления таблицы smartphone_releases."""

    def __init__(
        self,
        db: Session,
        news_client: SmartphoneNewsClient,
        normalizer: Optional[SmartphoneReleaseNormalizer],
        extra_sources: Optional[list] = None,
    ) -> None:
        self.db = db
        self.repo = SmartphoneReleaseRepository(db)
        self.news_client = news_client
        self.normalizer = normalizer
        self.extra_sources = extra_sources or []
        self.logger = logging.getLogger("app.services.smartphone_releases")
        self.highlight_limit = HIGHLIGHT_LIMIT

    def ingest_latest(self, batch_size: int = 20) -> Dict[str, object]:
        raw_items = self.news_client.fetch_recent_news()
        for source in self.extra_sources:
            try:
                raw_items.extend(source.fetch_recent_news())
            except Exception:
                self.logger.exception("failed to fetch from extra source", extra={"source": source.__class__.__name__})

        result: Dict[str, object] = {
            "fetched": len(raw_items),
            "processed": 0,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "errors": 0,
            "highlights": [],
        }

        if not self.normalizer:
            self.logger.warning("smartphone release normalizer disabled; nothing to process")
            result["skipped"] = len(raw_items)
            return result

        for i in range(0, len(raw_items), batch_size):
            batch = raw_items[i : i + batch_size]
            for item in batch:
                try:
                    normalized = self.normalizer.normalize(item)
                except Exception:
                    self.db.rollback()
                    result["errors"] += 1
                    self.logger.exception("failed to normalize news item", extra={"url": item.url})
                    continue
                if not normalized or not normalized.is_phone_announcement:
                    result["skipped"] += 1
                    continue
                models = self._extract_models(normalized)
                if not normalized.brand or not models:
                    self.logger.info(
                        "skip smartphone release without brand/models",
                        extra={"url": item.url, "source": item.source_name},
                    )
                    result["skipped"] += 1
                    continue

                for idx, model_name in enumerate(models):
                    payload = self._build_payload(item, normalized, model_name, idx if len(models) > 1 else None)
                    try:
                        existing = self.repo.get_by_source(payload["source_name"], payload["source_url"])
                        if not existing:
                            existing = self.repo.get_by_identity(
                                payload["brand"], payload["model"], payload["announcement_date"]
                            )
                        if existing:
                            self.repo.update(existing, payload)
                            result["updated"] = int(result["updated"]) + 1
                        else:
                            self.repo.create(payload)
                            result["created"] = int(result["created"]) + 1
                        self._add_highlight(result, payload, normalized, item)
                        result["processed"] = int(result["processed"]) + 1
                    except Exception:
                        self.db.rollback()
                        result["errors"] += 1
                        self.logger.exception("failed to upsert smartphone release", extra={"payload": payload})
                        continue
            self.db.commit()
        safe_extra = {f"smartphone_releases_{k}": v for k, v in result.items() if k != "highlights"}
        self.logger.info("smartphone releases ingestion completed", extra=safe_extra)
        return result

    def _add_highlight(
        self,
        result: Dict[str, object],
        payload: Dict,
        normalized: NormalizedReleaseCandidate,
        item: RawNewsItem,
    ) -> None:
        highlights = result.get("highlights")
        if highlights is None or not isinstance(highlights, list):
            return
        if len(highlights) >= self.highlight_limit:
            return
        summary = (normalized.summary_ru or item.description or item.title or "").strip()
        if summary and len(summary) > HIGHLIGHT_SUMMARY_MAX_LEN:
            summary = summary[: HIGHLIGHT_SUMMARY_MAX_LEN - 1].rstrip() + "…"
        title = (
            payload.get("full_name")
            or f"{payload.get('brand', '').strip()} {payload.get('model', '').strip()}".strip()
        )
        if not title:
            title = item.title
        highlights.append(
            {
                "title": title,
                "brand": payload.get("brand"),
                "model": payload.get("model"),
                "status": payload.get("release_status"),
                "status_label": self._status_label(payload.get("release_status")),
                "summary": summary,
                "source": payload.get("source_name"),
                "url": payload.get("source_url"),
                "published_at": item.published_at.isoformat() if item.published_at else None,
            }
        )

    def _status_label(self, status: Optional[str]) -> Optional[str]:
        if not status:
            return None
        normalized = str(status).lower().strip()
        mapping = {
            ReleaseStatus.RUMOR.value: "слух",
            ReleaseStatus.ANNOUNCED.value: "анонс",
            ReleaseStatus.RELEASED.value: "в продаже",
        }
        return mapping.get(normalized, normalized)

    def _build_payload(
        self,
        item: RawNewsItem,
        normalized: NormalizedReleaseCandidate,
        model_name: str,
        model_index: Optional[int] = None,
    ) -> Dict:
        status = self._safe_status(normalized.release_status)
        status_value: Optional[str]
        if isinstance(status, ReleaseStatus):
            status_value = status.value
        elif status:
            status_value = str(status).strip().lower()
        else:
            status_value = None
        full_name = item.title or f"{normalized.brand} {model_name}".strip()
        source_url = item.url
        if model_index is not None:
            source_url = f"{source_url}#{model_index + 1}"
        return {
            "brand": normalized.brand.strip(),
            "model": model_name.strip(),
            "full_name": full_name,
            "announcement_date": normalized.announcement_date,
            "release_status": status_value,
            "source_type": SourceType.NEWS_API.value,
            "source_name": item.source_name,
            "source_url": source_url,
            "published_at": item.published_at,
            "market_release_date": normalized.market_release_date,
            "market_release_date_ru": normalized.market_release_date_ru,
            "summary": item.description,
            "summary_ru": normalized.summary_ru,
            "raw_payload": item.raw,
            "is_active": True,
        }

    def _extract_models(self, normalized: NormalizedReleaseCandidate) -> List[str]:
        models = normalized.models or []
        if not models and normalized.model:
            models = self._split_model_string(normalized.model)
        cleaned = [model.strip() for model in models if model and model.strip()]
        # Deduplicate while preserving order
        seen = set()
        unique: List[str] = []
        for model in cleaned:
            key = model.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(model)
        return unique

    def _split_model_string(self, value: str) -> List[str]:
        separators = [",", "/", "|", ";"]
        normalized = value
        for token in (" and ", " And ", " AND ", " и ", " И "):
            normalized = normalized.replace(token, ",")
        for sep in separators[1:]:
            normalized = normalized.replace(sep, ",")
        parts = [part.strip(" -") for part in normalized.split(",")]
        return [part for part in parts if part]

    def _safe_status(self, value: Optional[str]) -> Optional[ReleaseStatus]:
        if isinstance(value, ReleaseStatus):
            return value
        if not value:
            return None
        normalized = str(value).strip().lower()
        try:
            return ReleaseStatus(normalized)
        except ValueError:
            return None


def build_release_service(db: Session) -> SmartphoneReleaseService:
    news_client = build_news_client_from_settings()
    normalizer = build_normalizer_from_settings()
    settings = get_settings()
    extras = []
    if settings.smartphone_gsmarena_enabled:
        extras.append(build_gsmarena_client_from_settings())
    return SmartphoneReleaseService(db=db, news_client=news_client, normalizer=normalizer, extra_sources=extras)
