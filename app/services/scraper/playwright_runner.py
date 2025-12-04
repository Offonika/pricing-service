from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from app.core.config import get_settings

logger = logging.getLogger("app.scraper.playwright")


def _proxy_options(proxy_url: Optional[str]):
    if not proxy_url:
        return None
    parsed = urlsplit(proxy_url)
    return {
        "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
        "username": parsed.username,
        "password": parsed.password,
    }


async def fetch_pages_with_playwright(urls: Iterable[str], output_dir: Path) -> list[dict]:
    """
    Загружает страницы как браузер (Chromium headless) через прокси и сохраняет html/screenshot для отладки.
    Возвращает список результатов: url, final_url, status_text, html_path, screenshot_path.
    """
    settings = get_settings()
    proxy = _proxy_options(settings.proxy_api_url)
    output_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, proxy=proxy)
        context = await browser.new_context(
            user_agent=settings.competitor_user_agent,
            extra_http_headers={
                "Accept-Language": settings.competitor_accept_language,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Cache-Control": "max-age=0",
            },
        )
        # Подставляем cookies, если есть (применяем к целевым доменам)
        if settings.competitor_cookies:
            cookies = []
            for item in settings.competitor_cookies.split(";"):
                if "=" not in item:
                    continue
                name, value = item.split("=", 1)
                cookies.append({"name": name.strip(), "value": value.strip(), "domain": ".moba.ru", "path": "/"})
            if cookies:
                await context.add_cookies(cookies)

        page = await context.new_page()
        for idx, url in enumerate(urls):
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)
                # если антибот ставит куки и перезагружает страницу, пробуем ещё раз
                if resp and resp.status in (301, 302, 307, 308):
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(3000)
                final_url = page.url
                status = resp.status if resp else None
                html = await page.content()
                html_path = output_dir / f"page_{idx}.html"
                html_path.write_text(html, encoding="utf-8")
                screenshot_path = output_dir / f"page_{idx}.png"
                await page.screenshot(path=screenshot_path, full_page=True)
                logger.info(
                    "playwright fetched",
                    extra={"url": url, "final_url": final_url, "status": status},
                )
                results.append(
                    {
                        "url": url,
                        "final_url": final_url,
                        "status": status,
                        "html": str(html_path),
                        "screenshot": str(screenshot_path),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("playwright fetch failed for %s", url, exc_info=exc)
                results.append({"url": url, "error": str(exc)})
        await browser.close()
    return results


def run_playwright_debug(urls: list[str], output_dir: str = "playwright_dump") -> list[dict]:
    return asyncio.run(fetch_pages_with_playwright(urls, Path(output_dir)))
