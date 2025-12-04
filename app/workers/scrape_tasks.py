from __future__ import annotations

import logging
from typing import Dict, List, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Competitor, CompetitorPrice, Product
from app.services.scraper.service import scrape_competitor_pages

logger = logging.getLogger("app.workers.scrape")


def get_engine():
    settings = get_settings()
    return create_engine(settings.database_url)


def _ensure_competitor(session: Session, name: str) -> Competitor:
    competitor = session.query(Competitor).filter_by(name=name).first()
    if competitor:
        return competitor
    competitor = Competitor(name=name)
    session.add(competitor)
    session.commit()
    return competitor


def run_scrape(
    competitors: Dict[str, List[str]],
    limit: int,
    database_url: Optional[str] = None,
) -> dict:
    """
    competitors: {"CompName": ["url1", "url2", ...]}
    limit: max offers total (for quick debugging)
    """
    settings = get_settings()
    engine = create_engine(database_url or settings.database_url)
    stats = {"competitors": 0, "offers_saved": 0, "errors": 0}
    with Session(engine) as session:
        # ensure schema exists for in-memory/testing
        from app.models import Base

        Base.metadata.create_all(engine)

        for name, urls in competitors.items():
            try:
                competitor = _ensure_competitor(session, name)
                offers = scrape_competitor_pages(
                    competitor=name,
                    urls=urls,
                    limit=limit,
                )
                # simple SKU-based link to Product if exists
                products = {p.sku: p for p in session.query(Product).filter(Product.sku.in_([o.external_sku for o in offers]))}
                for offer in offers:
                    product = products.get(offer.external_sku)
                    price = CompetitorPrice(
                        product_id=product.id if product else None,
                        competitor_id=competitor.id,
                        price=offer.price_roz,
                        in_stock=offer.availability,
                        collected_at=offer.collected_at,
                    )
                    session.add(price)
                    stats["offers_saved"] += 1
                session.commit()
                stats["competitors"] += 1
                logger.info(
                    "scrape completed",
                    extra={
                        "competitor": name,
                        "offers_saved": stats["offers_saved"],
                        "offers_in_batch": len(offers),
                    },
                )
            except Exception:
                session.rollback()
                logger.exception("scrape failed for %s", name)
                stats["errors"] += 1
    return stats


def scrape_default(limit: Optional[int] = None) -> dict:
    """
    Пример запуска: использует COMPETITOR_PARSE_LIMIT и несколько тестовых URL-ов (заполнить позже).
    """
    settings = get_settings()
    effective_limit = limit if limit is not None else settings.competitor_parse_limit
    competitors: Dict[str, List[str]] = {
        "moba": ["https://moba.ru/catalog/"],
        # Указываем конкретный раздел, чтобы бить в API /local/api/catalog/products
        "green-spark": [
            "https://green-spark.ru/catalog/komplektuyushchie_dlya_remonta/zapchasti_dlya_mobilnykh_ustroystv/displei_2/"
        ],
        "ultra-details": ["https://ultra-details.ru/catalog/"],
        "memstech": ["https://memstech.ru/catalog/"],
    }
    return run_scrape(competitors, effective_limit)
