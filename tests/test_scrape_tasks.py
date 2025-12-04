from __future__ import annotations

from datetime import datetime
from typing import List

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Competitor, Product
from app.services.proxy_client import ProxyConfig, ProxyHttpClient
from app.services.scraper.service import scrape_competitor_pages
from app.workers.scrape_tasks import run_scrape


def make_mock_proxy(responses: dict[str, str]) -> ProxyHttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        return httpx.Response(200, text=responses[url])

    transport = httpx.MockTransport(handler)
    return ProxyHttpClient(
        ProxyConfig(
            base_url="https://proxy.test",
            token=None,
            timeout_seconds=5,
            max_retries=1,
            rps_limit=None,
        ),
        transport=transport,
    )


def test_run_scrape_saves_prices(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Product(sku="A1", name="ItemA"))
        session.commit()

    responses = {
        "https://example.com/page1": '[{"sku":"A1","name":"ItemA","price": "10","availability":"1","url":"u1","category":"Cat"}]',
    }

    mock_client = make_mock_proxy(responses)
    monkeypatch.setattr(
        "app.workers.scrape_tasks.scrape_competitor_pages",
        lambda competitor, urls, limit: scrape_competitor_pages(
            competitor, urls, limit, proxy_client=mock_client
        ),
    )

    stats = run_scrape({"CompX": list(responses.keys())}, limit=5, database_url="sqlite:///:memory:")
    assert stats["offers_saved"] >= 1
