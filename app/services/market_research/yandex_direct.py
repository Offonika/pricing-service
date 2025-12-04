import json
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional
import time

import logging
import httpx
from sqlalchemy.orm import Session

from app.models import Keyword, KeywordDemand
from app.services.market_research.repositories import KeywordDemandRepository
from app.services.market_research.wordstat import WordstatClient, build_wordstat_client_from_settings
from app.core.config import get_settings


@dataclass
class YandexKeywordStat:
    phrase: str
    region: str
    impressions: int
    stat_date: Optional[date] = None
    clicks: Optional[int] = None
    ctr: Optional[float] = None
    bid_metrics: Optional[str] = None


class YandexDirectClient:
    """Клиент для keywordsresearch.hasSearchVolume Яндекс.Директа."""

    def __init__(
        self,
        token: str,
        base_url: str,
        client_login: Optional[str],
        timeout: float = 10.0,
        rps_limit: Optional[float] = None,
    ):
        self.token = token
        self.base_url = base_url
        self.client_login = client_login
        self.timeout = timeout
        self.rps_limit = rps_limit
        self.logger = logging.getLogger("app.services.yandex_direct")
        self._client = httpx.Client(timeout=self.timeout)
        self._last_call_ts: Optional[float] = None

    def get_stats(self, phrases: List[str], region: str) -> List[YandexKeywordStat]:
        """
        Вызывает API Директа и возвращает агрегированные метрики по фразам.

        Использует keywordsresearch.hasSearchVolume (YES/NO по устройствам).
        """
        if not self.token:
            self.logger.warning("Yandex Direct token is missing, stats request skipped")
            return []
        try:
            region_id = int(region)
        except (TypeError, ValueError):
            region_id = region
        payload = {
            "method": "hasSearchVolume",
            "params": {
                "SelectionCriteria": {"Keywords": phrases, "RegionIds": [region_id]},
                "FieldNames": ["Keyword", "RegionIds", "AllDevices", "MobilePhones", "Tablets", "Desktops"],
            },
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept-Language": "ru",
            "Content-Type": "application/json",
        }
        if self.client_login:
            headers["Client-Login"] = self.client_login
        try:
            if self.rps_limit:
                min_interval = 1.0 / self.rps_limit
                if self._last_call_ts:
                    elapsed = time.perf_counter() - self._last_call_ts
                    if elapsed < min_interval:
                        time.sleep(min_interval - elapsed)
            response = self._client.post(self.base_url, headers=headers, json=payload)
            response.raise_for_status()
            self._last_call_ts = time.perf_counter()
        except httpx.HTTPError:
            self.logger.exception("failed to call Yandex Direct", extra={"phrases": phrases, "region": region})
            return []

        try:
            data = response.json()
        except ValueError:
            self.logger.exception("invalid JSON from Yandex Direct", extra={"body": response.text})
            return []
        if "error" in data:
            self.logger.error(
                "yandex direct returned an error",
                extra={"phrases": phrases, "region": region, "error": data.get("error")},
            )
            return []

        stats: List[YandexKeywordStat] = []
        result_items = data.get("result", {}).get("HasSearchVolumeResults", [])
        for item in result_items:
            try:
                all_devices = str(item.get("AllDevices", "")).upper()
                device_flags = {
                    "all_devices": all_devices,
                    "mobile_phones": item.get("MobilePhones"),
                    "tablets": item.get("Tablets"),
                    "desktops": item.get("Desktops"),
                }
                stats.append(
                    YandexKeywordStat(
                        phrase=item.get("Keyword") or "",
                        region=region,
                        impressions=1 if all_devices == "YES" else 0,
                        stat_date=date.today(),
                        clicks=None,
                        ctr=None,
                        bid_metrics=json.dumps(device_flags, ensure_ascii=False),
                    )
                )
            except Exception:
                self.logger.exception("failed to parse keyword stat", extra={"item": item})
        return stats


class DemandService:
    """Сервис для обновления метрик спроса по ключевым фразам."""

    def __init__(self, db: Session, client, batch_size: int = 100):  # client: YandexDirectClient | WordstatClient
        self.db = db
        self.client = client
        self.batch_size = batch_size
        self.repo = KeywordDemandRepository(db)
        self.logger = logging.getLogger("app.services.demand")

    def update_demand_for_keywords(self, keywords: List[Keyword], region: str) -> List[KeywordDemand]:
        """
        Берёт список Keyword, запрашивает Яндекс.Директ и сохраняет KeywordDemand.
        Валидацию лимитов/регионов/ретраев обеспечивает клиент или вызывающий код.
        """
        saved: List[KeywordDemand] = []
        for i in range(0, len(keywords), self.batch_size):
            batch = keywords[i : i + self.batch_size]
            phrases = [kw.phrase for kw in batch]
            stats = self.client.get_stats(phrases=phrases, region=region)
            stat_by_phrase: Dict[str, YandexKeywordStat] = {item.phrase.lower(): item for item in stats}
            demand_records: List[KeywordDemand] = []
            for kw in batch:
                stat = stat_by_phrase.get(kw.phrase.lower())
                if not stat:
                    continue
                demand_records.append(
                    KeywordDemand(
                        keyword_id=kw.id,
                        date=stat.stat_date or date.today(),
                        region=stat.region,
                        impressions=stat.impressions,
                        clicks=stat.clicks,
                        ctr=stat.ctr,
                        bid_metrics=stat.bid_metrics,
                    )
                )
            if not demand_records:
                continue
            saved.extend(self.repo.create_many(demand_records))
            self.logger.info(
                "demand stats saved",
                extra={"batch_size": len(batch), "saved": len(demand_records), "region": region},
            )
        return saved


def build_yandex_direct_client_from_settings() -> YandexDirectClient:
    settings = get_settings()
    return YandexDirectClient(
        token=settings.yandex_direct_api_token or "",
        base_url=settings.yandex_direct_api_base_url,
        client_login=settings.yandex_direct_client_login,
        timeout=settings.yandex_direct_timeout,
        rps_limit=settings.yandex_direct_rps_limit,
    )


def build_demand_service(db: Session, batch_size: Optional[int] = None) -> DemandService:
    settings = get_settings()
    if settings.yandex_wordstat_enabled:
        client = build_wordstat_client_from_settings()
    else:
        client = build_yandex_direct_client_from_settings()
    return DemandService(
        db=db,
        client=client,
        batch_size=batch_size or settings.yandex_direct_batch_size,
    )
