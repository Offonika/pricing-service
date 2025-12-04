from __future__ import annotations

import json
from datetime import datetime

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Competitor, CompetitorPrice, Product
from app.services.proxy_client import ProxyConfig, ProxyHttpClient
from app.services.scraper.service import scrape_competitor_pages


def make_mock_client(responses: dict[str, str]) -> ProxyHttpClient:
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


def test_scrape_competitor_pages_with_limit():
    responses = {
        "https://example.com/page1": '[{"sku":"A1","name":"ItemA","price": "10","availability":"1","url":"u1","category":"Cat"}]',
        "https://example.com/page2": '''
            <div class="offer" data-sku="B1" data-price="20" data-availability="in_stock" data-url="u2" data-category="CatB">ItemB</div>
            <div class="offer" data-sku="B2" data-price="30" data-availability="in_stock" data-url="u3" data-category="CatB">ItemB2</div>
        ''',
    }
    client = make_mock_client(responses)
    offers = scrape_competitor_pages(
        competitor="CompX",
        urls=list(responses.keys()),
        limit=2,
        proxy_client=client,
    )
    # Limit = 2, should trim total offers
    assert len(offers) == 2
    assert offers[0].external_sku == "A1"
    assert offers[1].external_sku in {"B1", "B2"}


def test_scraped_offers_can_be_saved_to_db(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        product = Product(sku="A1", name="ItemA")
        competitor = Competitor(name="CompX")
        session.add_all([product, competitor])
        session.commit()

    responses = {
        "https://example.com/page1": '[{"sku":"A1","name":"ItemA","price": "10","availability":"1","url":"u1","category":"Cat"}]',
    }
    client = make_mock_client(responses)
    offers = scrape_competitor_pages("CompX", ["https://example.com/page1"], limit=10, proxy_client=client)

    with Session(engine) as session:
        comp = session.query(Competitor).filter_by(name="CompX").one()
        prod = session.query(Product).filter_by(sku="A1").one()
        for offer in offers:
            session.add(
                CompetitorPrice(
                    product_id=prod.id,
                    competitor_id=comp.id,
                    price=offer.price_roz,
                    in_stock=offer.availability,
                    collected_at=datetime.utcnow(),
                )
            )
        session.commit()
        assert session.query(CompetitorPrice).count() == 1
