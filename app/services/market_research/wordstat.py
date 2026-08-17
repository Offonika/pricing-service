from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

import httpx

from app.core.config import get_settings


class WordstatClient:
    """Клиент Wordstat API в составе Yandex Search API."""

    _DEVICE_NAMES = {
        "all": "DEVICE_ALL",
        "desktop": "DEVICE_DESKTOP",
        "phone": "DEVICE_PHONE",
        "tablet": "DEVICE_TABLET",
    }
    _PERIOD_NAMES = {
        "daily": "PERIOD_DAILY",
        "weekly": "PERIOD_WEEKLY",
        "monthly": "PERIOD_MONTHLY",
    }

    def __init__(
        self,
        api_key: str,
        folder_id: str,
        base_url: str = "https://searchapi.api.cloud.yandex.net",
        devices: list[str] | None = None,
        rps_limit: float | None = None,
        timeout: float = 10.0,
        http_client: httpx.Client | None = None,
    ):
        self.api_key = api_key
        self.folder_id = folder_id
        self.base_url = base_url.rstrip("/")
        self.devices = self._normalize_devices(devices or ["all"])
        self.rps_limit = rps_limit
        self.timeout = timeout
        self.logger = logging.getLogger("app.services.wordstat")
        self._client = http_client or httpx.Client(timeout=self.timeout)
        self._last_call_ts: float | None = None

    @classmethod
    def _normalize_devices(cls, devices: list[str]) -> list[str]:
        normalized = {
            cls._DEVICE_NAMES.get(device.strip().lower(), device.strip().upper())
            for device in devices
            if device.strip()
        }
        supported = set(cls._DEVICE_NAMES.values())
        normalized &= supported
        if not normalized or "DEVICE_ALL" in normalized:
            return ["DEVICE_ALL"]
        return sorted(normalized)

    def _throttle(self) -> None:
        if not self.rps_limit:
            return
        min_interval = 1.0 / self.rps_limit
        if self._last_call_ts:
            elapsed = time.perf_counter() - self._last_call_ts
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)

    def get_stats(self, phrases: list[str], region: str) -> list:
        """
        Возвращает агрегированные метрики по фразам через /v2/wordstat/topRequests.

        Используем totalCount как частотность запросов за последние 30 дней.
        """
        from app.services.market_research.yandex_direct import YandexKeywordStat

        if not self.api_key or not self.folder_id:
            self.logger.warning("wordstat credentials missing, skipping request")
            return []
        stats: list[YandexKeywordStat] = []
        for phrase in phrases:
            payload = {
                "phrase": phrase,
                "numPhrases": "1",
                "regions": [str(region)],
                "devices": self.devices,
                "folderId": self.folder_id,
            }
            headers = {
                "Authorization": f"Api-Key {self.api_key}",
                "Content-Type": "application/json",
            }
            try:
                self._throttle()
                resp = self._client.post(
                    f"{self.base_url}/v2/wordstat/topRequests",
                    headers=headers,
                    json=payload,
                )
                self._last_call_ts = time.perf_counter()
                if resp.status_code in {401, 403}:
                    self.logger.error(
                        "wordstat authorization failed",
                        extra={"phrase": phrase, "status_code": resp.status_code},
                    )
                    continue
                resp.raise_for_status()
            except httpx.HTTPError:
                self.logger.exception(
                    "failed to call wordstat", extra={"phrase": phrase, "region": region}
                )
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
                    source="wordstat",
                )
            )
        return stats

    def get_dynamics_response(
        self,
        *,
        phrase: str,
        region: str,
        from_date: date,
        to_date: date,
        period: str = "monthly",
    ) -> dict[str, Any] | None:
        """Возвращает сырой ответ ``/v2/wordstat/dynamics`` без секретов запроса."""

        if not self.api_key or not self.folder_id:
            self.logger.warning("wordstat credentials missing, skipping request")
            return None
        normalized_period = self._PERIOD_NAMES.get(period.strip().lower(), period.strip().upper())
        if normalized_period not in set(self._PERIOD_NAMES.values()):
            raise ValueError(f"unsupported_wordstat_period:{period}")
        payload = {
            "phrase": phrase,
            "period": normalized_period,
            "fromDate": f"{from_date.isoformat()}T00:00:00Z",
            "toDate": f"{to_date.isoformat()}T23:59:59Z",
            "regions": [str(region)],
            "devices": self.devices,
            "folderId": self.folder_id,
        }
        headers = {
            "Authorization": f"Api-Key {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            self._throttle()
            response = self._client.post(
                f"{self.base_url}/v2/wordstat/dynamics",
                headers=headers,
                json=payload,
            )
            self._last_call_ts = time.perf_counter()
            if response.status_code in {401, 403}:
                self.logger.error(
                    "wordstat authorization failed",
                    extra={"phrase": phrase, "status_code": response.status_code},
                )
                return None
            response.raise_for_status()
        except httpx.HTTPError:
            self.logger.exception(
                "failed to call wordstat dynamics",
                extra={"phrase": phrase, "region": region},
            )
            return None
        try:
            data = response.json()
        except ValueError:
            self.logger.exception(
                "invalid JSON from wordstat dynamics", extra={"body": response.text}
            )
            return None
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            self.logger.error("invalid wordstat dynamics contract", extra={"phrase": phrase})
            return None
        return data


def build_wordstat_client_from_settings() -> WordstatClient:
    settings = get_settings()
    devices: list[str] = []
    raw_devices = settings.yandex_wordstat_devices
    if raw_devices:
        devices = [d.strip() for d in raw_devices.split(",") if d.strip()]
    if not devices:
        devices = ["all"]
    return WordstatClient(
        api_key=settings.yandex_wordstat_api_key or "",
        folder_id=settings.yandex_wordstat_folder_id or "",
        base_url=settings.yandex_wordstat_base_url,
        devices=devices,
        rps_limit=settings.yandex_wordstat_rps_limit,
        timeout=settings.yandex_wordstat_timeout,
    )
