from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Competitor, Product, ProductMatch


def match_by_sku(session: Session, sku_pairs: Iterable[tuple[str, str]]) -> int:
    """
    Простой матчинга по SKU:
    - sku_pairs: пары (our_sku, competitor_sku)
    - создаёт ProductMatch (product_id, competitor_id, competitor_sku)
    Возвращает количество созданных связок.
    """
    created = 0
    grouped: dict[str, list[str]] = defaultdict(list)
    for ours, competitor in sku_pairs:
        grouped[ours].append(competitor)

    products = {
        p.sku: p
        for p in session.execute(
            select(Product).where(Product.sku.in_(grouped.keys()))
        ).scalars()
    }
    competitors = {c.name: c for c in session.execute(select(Competitor)).scalars()}

    for our_sku, competitor_skus in grouped.items():
        product = products.get(our_sku)
        if not product:
            continue
        for comp_sku in competitor_skus:
            competitor = competitors.get(comp_sku)
            if not competitor:
                competitor = Competitor(name=comp_sku)
                session.add(competitor)
                session.flush()
                competitors[comp_sku] = competitor

            exists = session.execute(
                select(ProductMatch).where(
                    ProductMatch.product_id == product.id,
                    ProductMatch.competitor_id == competitor.id,
                )
            ).scalar_one_or_none()
            if exists:
                continue

            match = ProductMatch(
                product=product,
                competitor=competitor,
                competitor_sku=comp_sku,
                confidence=1.0,
                is_manual=False,
            )
            session.add(match)
            created += 1

    session.commit()
    return created
