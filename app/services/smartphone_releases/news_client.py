from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

import httpx

from app.core.config import get_settings
from app.services.smartphone_releases.types import RawNewsItem


def _parse_datetime(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    value = raw.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=None)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(value)
        return dt.replace(tzinfo=None)
    except Exception:
        return None


class SmartphoneNewsClient:
    """Тонкий клиент к новостному API (по умолчанию совместим с NewsAPI.org)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        languages: Sequence[str],
        query: str,
        days_back: int = 5,
        page_size: int = 20,
        max_pages: int = 1,
        max_items: Optional[int] = None,
        timeout: float = 10.0,
        source_name: str = "NewsAPI",
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.languages = [lang.strip() for lang in languages if lang.strip()]
        self.query = query
        self.days_back = days_back
        self.page_size = page_size
        self.max_pages = max_pages
        self.max_items = max_items
        self.timeout = timeout
        self.source_name = source_name
        self.logger = logging.getLogger("app.services.smartphone_news")
        self._client = httpx.Client(timeout=self.timeout)

    def fetch_recent_news(self) -> List[RawNewsItem]:
        """Возвращает свежие новости по заданному запросу и языкам."""
        if not self.base_url or not self.api_key:
            self.logger.warning("smartphone news client is not configured; skipping fetch")
            return []

        items: List[RawNewsItem] = []
        since_date = (datetime.utcnow() - timedelta(days=self.days_back)).date().isoformat()
        params_base = {
            "q": self.query,
            "from": since_date,
            "sortBy": "publishedAt",
            "pageSize": self.page_size,
        }
        headers = {"X-Api-Key": self.api_key}

        languages = self.languages or [None]
        for lang in languages:
            for page in range(1, self.max_pages + 1):
                if self.max_items and len(items) >= self.max_items:
                    break
                params = dict(params_base)
                params["page"] = page
                if lang:
                    params["language"] = lang
                try:
                    response = self._client.get(self.base_url, params=params, headers=headers)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    self.logger.warning(
                        "smartphone news request failed",
                        extra={"language": lang, "page": page, "error": str(exc)},
                    )
                    break
                try:
                    data = response.json()
                except ValueError:
                    self.logger.warning("smartphone news returned invalid JSON", extra={"language": lang, "page": page})
                    break

                articles = data.get("articles") or data.get("data") or []
                if not articles:
                    break
                for article in articles:
                    if self.max_items and len(items) >= self.max_items:
                        break
                    title = article.get("title") or ""
                    url = article.get("url") or article.get("link") or ""
                    if not title or not url:
                        continue
                    items.append(
                        RawNewsItem(
                            title=title.strip(),
                            description=(article.get("description") or article.get("content")),
                            url=url,
                            published_at=_parse_datetime(article.get("publishedAt") or article.get("published_at")),
                            source_name=article.get("source", {}).get("name") or self.source_name,
                            raw=article,
                        )
                    )
        self.logger.info(
            "smartphone news fetched",
            extra={
                "items": len(items),
                "languages": languages,
                "since": since_date,
                "max_pages": self.max_pages,
                "max_items": self.max_items,
            },
        )
        return items


def build_news_client_from_settings() -> SmartphoneNewsClient:
    settings = get_settings()
    return SmartphoneNewsClient(
        base_url=settings.smartphone_news_api_base_url or "",
        api_key=settings.smartphone_news_api_key or "",
        languages=(settings.smartphone_news_language or "").split(","),
        query=settings.smartphone_news_query,
        days_back=settings.smartphone_news_days_back,
        page_size=settings.smartphone_news_page_size,
        max_pages=settings.smartphone_news_max_pages,
        max_items=settings.smartphone_news_max_items,
        timeout=10.0,
    )
