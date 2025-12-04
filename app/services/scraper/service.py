from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime
from typing import List, Optional, Tuple
from urllib.parse import urlencode, urlparse

from playwright.async_api import async_playwright

from app.core.config import get_settings
from app.services.proxy_client import ProxyHttpClient, get_proxy_client
from app.services.scraper.models import CompetitorOffer
from app.services.scraper.parsers import parse_offers

logger = logging.getLogger("app.scraper.service")


def _to_decimal_safe(value: object) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, AttributeError):
        return Decimal("0")


def _green_spark_api_url(catalog_url: str, per_page: int) -> Optional[str]:
    """
    Конвертирует URL каталога в API-запрос GreenSpark:
    https://green-spark.ru/catalog/a/b/c/ -> /local/api/catalog/products/?path[]=a&path[]=b&path[]=c
    """
    parsed = urlparse(catalog_url)
    segments = [seg for seg in parsed.path.split("/") if seg and seg != "catalog"]
    if not segments:
        return None
    params = []
    for seg in segments:
        params.append(("path[]", seg))
    params.extend(
        [
            ("orderBy", "quantity"),
            ("orderDirection", "desc"),
            ("perPage", str(per_page)),
        ]
    )
    return f"https://green-spark.ru/local/api/catalog/products/?{urlencode(params)}"


def _proxy_options(proxy_url: Optional[str]):
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        return None
    return {
        "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
        "username": parsed.username,
        "password": parsed.password,
    }


async def _playwright_fetch_json(api_url: str, referer: str) -> Tuple[Optional[int], str]:
    """
    Загружает JSON через настоящий браузер (Chromium headless) с теми же заголовками и куками.
    Возвращает (status, body_text).
    """
    settings = get_settings()
    proxy = _proxy_options(settings.proxy_api_url)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, proxy=proxy)
        context = await browser.new_context(
            user_agent=settings.competitor_user_agent,
            extra_http_headers={
                "Accept-Language": settings.competitor_accept_language,
                "Accept": "*/*",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        if settings.competitor_cookies:
            cookies = []
            for item in settings.competitor_cookies.split(";"):
                if "=" not in item:
                    continue
                name, value = item.split("=", 1)
                cookies.append({"name": name.strip(), "value": value.strip(), "domain": ".green-spark.ru", "path": "/"})
            if cookies:
                await context.add_cookies(cookies)
        page = await context.new_page()
        # Загружаем любую страницу домена, чтобы куки были применены
        await page.goto(referer, wait_until="domcontentloaded", timeout=20000)
        fetch_script = f"""
        async () => {{
            const resp = await fetch("{api_url}", {{
                method: "GET",
                credentials: "include",
                headers: {{
                    "Accept": "*/*",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                    "X-Requested-With": "XMLHttpRequest",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin",
                    "Accept-Language": "{settings.competitor_accept_language}",
                    "Sec-CH-UA": "\\"Chromium\\";v=\\"142\\", \\"Google Chrome\\";v=\\"142\\", \\"Not_A Brand\\";v=\\"99\\"",
                    "Sec-CH-UA-Platform": "macOS",
                    "Sec-CH-UA-Mobile": "?0",
                    "Origin": "https://green-spark.ru",
                }}
            }});
            const text = await resp.text();
            return {{ status: resp.status, body: text }};
        }}
        """
        result = await page.evaluate(fetch_script)
        await browser.close()
        return result.get("status"), result.get("body", "")


def _scrape_green_spark(
    client: ProxyHttpClient,
    url: str,
    limit: int,
    collected_at: datetime,
) -> List[CompetitorOffer]:
    api_url = _green_spark_api_url(url, per_page=limit or 20)
    if not api_url:
        return []
    settings = get_settings()
    headers = {
        "Accept": "*/*",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": url,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Requested-With": "XMLHttpRequest",
        "Accept-Language": settings.competitor_accept_language,
        "User-Agent": settings.competitor_user_agent,
    }
    if settings.competitor_cookies:
        headers["Cookie"] = settings.competitor_cookies
    try:
        response = client.request(api_url, headers=headers)
        payload = response.json()
    except Exception:
        # если через http-клиент получили заглушку, пробуем Playwright
        status, body = asyncio.run(_playwright_fetch_json(api_url, referer=url))
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            logger.warning(
                "green-spark playwright fetch returned non-JSON",
                extra={"status": status, "length": len(body) if body else 0},
            )
            return []
    items: Optional[List] = None
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in ("items", "data", "products"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
        if items is None and isinstance(payload.get("data"), dict):
            if isinstance(payload["data"].get("items"), list):
                items = payload["data"]["items"]
    if not items:
        return []
    offers: List[CompetitorOffer] = []
    for item in items:
        try:
            offers.append(
                CompetitorOffer(
                    competitor="green-spark",
                    external_sku=str(item.get("id") or item.get("sku") or ""),
                    name=item.get("name") or "",
                    price_roz=_to_decimal_safe(
                        item.get("price") or item.get("discountPrice") or item.get("price_roz")
                    ),
                    price_opt=None,
                    availability=bool(item.get("quantity") or item.get("available") or True),
                    url=item.get("url") or "",
                    category=item.get("category") or None,
                    collected_at=collected_at,
                )
            )
        except Exception:  # noqa: BLE001
            continue
        if limit and len(offers) >= limit:
            break
    return offers


def scrape_competitor_pages(
    competitor: str,
    urls: List[str],
    limit: int,
    proxy_client: Optional[ProxyHttpClient] = None,
) -> List[CompetitorOffer]:
    """
    Забирает страницы конкурентов через Proxy API, парсит в единый формат.
    Лимит ограничивает общее количество собранных позиций (для отладки).
    """
    client = proxy_client or get_proxy_client()
    collected: List[CompetitorOffer] = []
    for url in urls:
        if limit and len(collected) >= limit:
            break
        try:
            collected_at = datetime.utcnow()
            if competitor == "green-spark":
                offers = _scrape_green_spark(client, url, limit, collected_at)
            else:
                response = client.request(url)
                offers = parse_offers(response.text, competitor, collected_at)
            logger.info(
                "scraped competitor page",
                extra={
                    "competitor": competitor,
                    "url": url,
                    "offers_found": len(offers),
                },
            )
            for offer in offers:
                if limit and len(collected) >= limit:
                    break
                collected.append(offer)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to scrape %s: %s", url, exc)
            continue
    return collected
