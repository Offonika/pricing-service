from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from app.core.config import get_settings

logger = logging.getLogger("app.proxy")


@dataclass
class ProxyConfig:
    base_url: str
    token: Optional[str]
    timeout_seconds: float
    max_retries: int
    rps_limit: Optional[float]
    default_headers: Optional[Dict[str, str]] = None


class ProxyHttpClient:
    def __init__(
        self,
        config: ProxyConfig,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        if not config.base_url:
            raise ValueError("Proxy base URL is required")
        self.base_url = config.base_url
        self.max_retries = config.max_retries
        self.rps_limit = config.rps_limit
        self._last_request_at: Optional[float] = None

        headers = {k: str(v) for k, v in (config.default_headers or {}).items() if k.lower() != "cookie"}
        self.proxy_mode = "api" if config.token else "http_proxy"
        self.default_cookie = None

        if config.token:
            headers["Authorization"] = f"Bearer {config.token}"
            self.client = httpx.Client(
                timeout=config.timeout_seconds,
                headers=headers,
                transport=transport,
                follow_redirects=True,
            )
        else:
            proxy_url = None if transport else config.base_url
            self.client = httpx.Client(
                timeout=config.timeout_seconds,
                headers=headers,
                transport=transport,
                proxy=proxy_url,
                follow_redirects=True,
            )

    def _respect_rps(self) -> None:
        if not self.rps_limit or self.rps_limit <= 0:
            return
        min_interval = 1 / self.rps_limit
        now = time.monotonic()
        if self._last_request_at is not None:
            elapsed = now - self._last_request_at
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def request(
        self,
        target_url: str,
        method: str = "GET",
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, Any]] = None,
        data: Any = None,
        json_body: Any = None,
    ) -> httpx.Response:
        if not target_url:
            raise ValueError("target_url is required")

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                self._respect_rps()
                if self.proxy_mode == "api":
                    payload: Dict[str, Any] = {
                        "url": target_url,
                        "method": method.upper(),
                        "params": params,
                        "headers": headers,
                        "data": data,
                        "json": json_body,
                    }
                    payload = {k: v for k, v in payload.items() if v is not None}
                    response = self.client.post(self.base_url, json=payload)
                else:
                    merged_headers = dict(self.client.headers)
                    if headers:
                        for k, v in headers.items():
                            merged_headers[k] = str(v)
                    response = self.client.request(
                        method=method,
                        url=target_url,
                        params=params,
                        headers=merged_headers,
                        data=data,
                        json=json_body,
                    )
                logger.debug(
                    "proxy response",
                    extra={
                        "target_url": target_url,
                        "status": response.status_code,
                        "location": response.headers.get("location"),
                    },
                )
                response.raise_for_status()
                return response
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning(
                    "proxy request failed (attempt %s/%s): %s",
                    attempt,
                    self.max_retries,
                    exc,
                )
                if attempt == self.max_retries:
                    break
                time.sleep(min(2 ** (attempt - 1) * 0.5, 2))

        raise last_exc if last_exc else RuntimeError("proxy request failed")


def get_proxy_client(transport: Optional[httpx.BaseTransport] = None) -> ProxyHttpClient:
    settings = get_settings()
    if not settings.proxy_api_url:
        # фоллбек: прямое соединение без прокси
        headers = {
            "User-Agent": settings.competitor_user_agent,
            "Accept-Language": settings.competitor_accept_language,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Cache-Control": "max-age=0",
            "Sec-CH-UA": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": "macOS",
            "Upgrade-Insecure-Requests": "1",
        }
        dummy_config = ProxyConfig(
            base_url="",
            token=None,
            timeout_seconds=settings.proxy_timeout_seconds,
            max_retries=settings.proxy_max_retries,
            rps_limit=settings.proxy_rps_limit,
            default_headers=headers,
        )
        client = ProxyHttpClient.__new__(ProxyHttpClient)
        client.base_url = ""
        client.max_retries = dummy_config.max_retries
        client.rps_limit = dummy_config.rps_limit
        client._last_request_at = None
        client.proxy_mode = "direct"
        client.default_cookie = None
        client.client = httpx.Client(
            timeout=dummy_config.timeout_seconds,
            headers=headers,
            transport=transport,
            follow_redirects=True,
        )
        return client
    base_headers: Dict[str, str] = {
        "User-Agent": settings.competitor_user_agent,
        "Accept-Language": settings.competitor_accept_language,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Cache-Control": "max-age=0",
        "Sec-CH-UA": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": "macOS",
        "Upgrade-Insecure-Requests": "1",
    }
    config = ProxyConfig(
        base_url=settings.proxy_api_url or "",
        token=settings.proxy_api_token,
        timeout_seconds=settings.proxy_timeout_seconds,
        max_retries=settings.proxy_max_retries,
        rps_limit=settings.proxy_rps_limit,
        default_headers=base_headers,
    )
    return ProxyHttpClient(config, transport=transport)
