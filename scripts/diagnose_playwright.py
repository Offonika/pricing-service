from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from app.core.config import get_settings


def _proxy_options(proxy_url: str | None):
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


async def diagnose(urls: List[str], output: Path) -> Dict[str, Any]:
    """
    Загружает URL-ы в headless Chromium, логирует запросы/ответы, set-cookie, статусы.
    """
    settings = get_settings()
    proxy = _proxy_options(settings.proxy_api_url)
    output.mkdir(parents=True, exist_ok=True)
    responses: List[Dict[str, Any]] = []
    cookies_snapshots: List[Dict[str, Any]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, proxy=proxy)
        context = await browser.new_context(
            user_agent=settings.competitor_user_agent,
            extra_http_headers={
                "Accept-Language": settings.competitor_accept_language,
            },
        )
        # Подключаем куки, если заданы
        if settings.competitor_cookies:
            cookies = []
            for item in settings.competitor_cookies.split(";"):
                if "=" not in item:
                    continue
                name, value = item.split("=", 1)
                cookies.append(
                    {"name": name.strip(), "value": value.strip(), "domain": ".green-spark.ru", "path": "/"}
                )
            if cookies:
                await context.add_cookies(cookies)

        page = await context.new_page()

        page.on(
            "response",
            lambda resp: responses.append(
                {
                    "url": resp.url,
                    "status": resp.status,
                    "headers": resp.headers,
                    "request_headers": resp.request.headers,
                    "set_cookie": resp.headers.get("set-cookie"),
                    "content_length": resp.headers.get("content-length"),
                }
            ),
        )

        for idx, url in enumerate(urls):
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            html = await page.content()
            (output / f"page_{idx}.html").write_text(html, encoding="utf-8")
            await page.screenshot(path=output / f"page_{idx}.png", full_page=True)
            local_storage = await page.evaluate("() => Object.assign({}, window.localStorage)")
            session_storage = await page.evaluate("() => Object.assign({}, window.sessionStorage)")
            cookies_snapshots.append(
                {
                    "url": url,
                    "cookies": await context.cookies(),
                    "localStorage": local_storage,
                    "sessionStorage": session_storage,
                    "status": resp.status if resp else None,
                    "final_url": page.url,
                }
            )

        await browser.close()

    report = {
        "generated_at": datetime.utcnow().isoformat(),
        "urls": urls,
        "responses": responses,
        "cookies": cookies_snapshots,
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description="Diagnose anti-bot via Playwright")
    parser.add_argument("urls", nargs="+", help="Target URLs")
    parser.add_argument("--output", default="playwright_diag", help="Output directory")
    args = parser.parse_args()
    output = Path(args.output)
    asyncio.run(diagnose(args.urls, output))
    print(f"Saved diagnostics to {output}")


if __name__ == "__main__":
    main()
