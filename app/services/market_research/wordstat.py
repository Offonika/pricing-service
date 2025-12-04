import logging
from datetime import date
from typing import List, Optional
import time

import httpx

from app.core.config import get_settings


class WordstatClient:
    """Клиент для публичного API Wordstat (api.wordstat.yandex.net)."""

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.wordstat.yandex.net",
        devices: Optional[List[str]] = None,
        rps_limit: Optional[float] = None,
        timeout: float = 10.0,
    ):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.devices = devices or ["all"]
        self.rps_limit = rps_limit
        self.timeout = timeout
        self.logger = logging.getLogger("app.services.wordstat")
        self._client = httpx.Client(timeout=self.timeout)
        self._last_call_ts: Optional[float] = None

    def _throttle(self) -> None:
        if not self.rps_limit:
            return
        min_interval = 1.0 / self.rps_limit
        if self._last_call_ts:
            elapsed = time.perf_counter() - self._last_call_ts
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)

    def get_stats(self, phrases: List[str], region: str) -> List:
        """
        Возвращает агрегированные метрики по фразам через /v1/topRequests.

        Используем totalCount как количество показов за последние 30 дней.
        """
        from app.services.market_research.yandex_direct import YandexKeywordStat

        if not self.token:
            self.logger.warning("wordstat token missing, skipping request")
            return []
        try:
            region_id = int(region)
        except (TypeError, ValueError):
            region_id = region
        stats: List[YandexKeywordStat] = []
        for phrase in phrases:
            payload = {
                "phrase": phrase,
                "regions": [region_id],
                "devices": self.devices,
            }
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            }
            try:
                self._throttle()
                resp = self._client.post(f"{self.base_url}/v1/topRequests", headers=headers, json=payload)
                self._last_call_ts = time.perf_counter()
                if resp.status_code == 401:
                    self.logger.error("wordstat unauthorized", extra={"phrase": phrase})
                    continue
                resp.raise_for_status()
            except httpx.HTTPError:
                self.logger.exception("failed to call wordstat", extra={"phrase": phrase, "region": region})
                continue
            try:
                data = resp.json()
            except ValueError:
                self.logger.exception("invalid JSON from wordstat", extra={"body": resp.text})
                continue
            impressions = data.get("totalCount")
            if impressions is None:
                # если нет totalCount, пропускаем
                continue
            stats.append(
                YandexKeywordStat(
                    phrase=phrase,
                    region=str(region),
                    impressions=int(impressions),
                    stat_date=date.today(),
                    clicks=None,
                    ctr=None,
                    bid_metrics=None,
                )
            )
        return stats


def build_wordstat_client_from_settings() -> WordstatClient:
    settings = get_settings()
    devices: List[str] = []
    raw_devices = settings.yandex_wordstat_devices
    if raw_devices:
        devices = [d.strip() for d in raw_devices.split(",") if d.strip()]
    if not devices:
        devices = ["all"]
    return WordstatClient(
        token=settings.yandex_direct_api_token or "",
        base_url=settings.yandex_wordstat_base_url,
        devices=devices,
        rps_limit=settings.yandex_direct_rps_limit,
        timeout=settings.yandex_direct_timeout,
    )
