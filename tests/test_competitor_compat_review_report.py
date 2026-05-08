from __future__ import annotations

from datetime import datetime

from app.models import CompetitorItem
from app.models.competitor_item_compatibility import CompetitorItemCompatibility
from tasks.report_competitor_items_needs_compat_review import build_review_items


def test_compat_review_report_keeps_only_actionable_missing_compatibility(db_session):
    actionable = CompetitorItem(
        competitor="moba",
        external_id="LCD-MULTI",
        name="Дисплей для Samsung Galaxy A10/M10",
        item_type="display",
        first_seen_at=datetime(2026, 5, 2, 10, 0, 0),
        parsed_device_brand="samsung",
        parse_notes="multi candidates; ambiguous",
    )
    noise = CompetitorItem(
        competitor="moba",
        external_id="TOOL-1",
        name="Пинцет YAXUN 15ESD",
        item_type="other",
        first_seen_at=datetime(2026, 5, 2, 10, 0, 0),
    )
    already_compatible = CompetitorItem(
        competitor="liberti",
        external_id="LCD-IP17",
        name="Дисплей для iPhone 17",
        item_type="display",
        first_seen_at=datetime(2026, 5, 2, 10, 0, 0),
    )
    db_session.add_all([actionable, noise, already_compatible])
    db_session.flush()
    db_session.add(
        CompetitorItemCompatibility(
            competitor_item_id=already_compatible.id,
            device_brand="apple",
            device_model="iphone 17",
            source="test",
        )
    )
    db_session.commit()

    items = build_review_items(db_session, first_seen_after="2026-05-01")

    assert len(items) == 1
    assert items[0]["competitor_item_id"] == actionable.id
    assert items[0]["external_id"] == "LCD-MULTI"
    assert items[0]["review_reason"] == "target_device_group"
