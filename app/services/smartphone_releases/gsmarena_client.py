from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional
import xml.etree.ElementTree as ET

import httpx

from app.services.smartphone_releases.types import RawNewsItem
from app.core.config import get_settings


def _parse_pub_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    # Typical RSS pubDate example: Mon, 24 Nov 2025 10:00:00 +0000
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
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


class GSMArenaClient:
    def __init__(
        self,
        rss_url: str,
        timeout: float = 10.0,
        source_name: str = "GSMArena",
        max_items: Optional[int] = 40,
    ) -> None:
        self.rss_url = rss_url
        self.timeout = timeout
        self.source_name = source_name
        self.max_items = max_items
        self.logger = logging.getLogger("app.services.gsmarena")
        self._client = httpx.Client(timeout=self.timeout)

    def fetch_recent_news(self) -> List[RawNewsItem]:
        if not self.rss_url:
            self.logger.warning("GSMArena RSS URL not configured; skipping")
            return []
        try:
            resp = self._client.get(self.rss_url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            self.logger.warning("failed to fetch GSMArena RSS", extra={"error": str(exc)})
            return []
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            self.logger.warning("failed to parse GSMArena RSS XML")
            return []

        items: List[RawNewsItem] = []
        for item in root.findall(".//item"):
            if self.max_items and len(items) >= self.max_items:
                break
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find("description")
            pub_el = item.find("pubDate")
            title = title_el.text if title_el is not None else ""
            url = link_el.text if link_el is not None else ""
            if not title or not url:
                continue
            items.append(
                RawNewsItem(
                    title=title.strip(),
                    description=desc_el.text if desc_el is not None else None,
                    url=url.strip(),
                    published_at=_parse_pub_date(pub_el.text if pub_el is not None else None),
                    source_name=self.source_name,
                    raw={"source": {"name": self.source_name}, "title": title, "link": url, "description": desc_el.text if desc_el is not None else None, "pubDate": pub_el.text if pub_el is not None else None},
                )
            )
        self.logger.info("gsmarena news fetched", extra={"items": len(items)})
        return items


def build_gsmarena_client_from_settings() -> GSMArenaClient:
    settings = get_settings()
    return GSMArenaClient(
        rss_url=settings.smartphone_gsmarena_rss_url,
        timeout=10.0,
        max_items=settings.smartphone_gsmarena_max_items,
    )
