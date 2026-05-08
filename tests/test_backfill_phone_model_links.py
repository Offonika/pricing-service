from __future__ import annotations

from datetime import datetime, timezone

from app.models import (
    CompetitorItem,
    CompetitorItemCompatibility,
    PhoneModel,
    Product,
    ProductCompatibility,
    ProductPhoneModel,
)
from tasks.backfill_phone_model_links import run_backfill


def test_backfill_can_run_products_only(db_session):
    product = Product(article="P-IP17", name="Дисплей для Apple iPhone 17")
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductCompatibility(product_id=product.id, value="Apple iPhone 17", source="onec")
    )
    db_session.commit()

    stats = run_backfill(
        db_session,
        batch_size=10,
        process_products=True,
        process_competitors=False,
        progress_every=0,
    )

    assert stats["products_processed"] == 1
    assert stats["product_links_created"] == 1
    assert stats["competitor_items_processed"] == 0
    link = db_session.query(ProductPhoneModel).one()
    assert link.product_id == product.id


def test_backfill_competitors_only_respects_first_seen_filter(db_session):
    old_model = PhoneModel(brand="apple", model_name="iphone 11")
    new_model = PhoneModel(brand="apple", model_name="iphone 17")
    old_item = CompetitorItem(
        competitor="moba",
        external_id="OLD",
        name="Дисплей iPhone 11",
        first_seen_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )
    new_item = CompetitorItem(
        competitor="moba",
        external_id="NEW",
        name="Дисплей iPhone 17",
        first_seen_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
    )
    db_session.add_all([old_model, new_model, old_item, new_item])
    db_session.flush()
    db_session.add_all(
        [
            CompetitorItemCompatibility(
                competitor_item_id=old_item.id,
                device_brand="apple",
                device_model="iphone 11",
                source="test",
            ),
            CompetitorItemCompatibility(
                competitor_item_id=new_item.id,
                device_brand="apple",
                device_model="iphone 17",
                source="test",
            ),
        ]
    )
    db_session.commit()

    stats = run_backfill(
        db_session,
        batch_size=10,
        process_products=False,
        process_competitors=True,
        competitor_first_seen_after=datetime(2026, 5, 1, tzinfo=timezone.utc).date(),
        progress_every=0,
    )

    assert stats["products_processed"] == 0
    assert stats["competitor_items_processed"] == 1
    compats = db_session.query(CompetitorItemCompatibility).order_by(CompetitorItemCompatibility.id)
    old_compat, new_compat = compats.all()
    assert old_compat.phone_model_id is None
    assert new_compat.phone_model_id == new_model.id
