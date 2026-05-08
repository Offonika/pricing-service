from __future__ import annotations

from datetime import datetime

from app.models import CompetitorItem
from app.models.competitor_item_compatibility import CompetitorItemCompatibility
from tasks.report_competitor_matching_quality import build_report


def test_quality_report_counts_only_actionable_missing_compatibility(db_session):
    display = CompetitorItem(
        competitor="moba",
        external_id="LCD-IP17",
        name="Дисплей для iPhone 17 Pro OLED черный",
        item_type="display",
        first_seen_at=datetime(2026, 5, 2, 10, 0, 0),
        attrs_json={"item_type": "display"},
    )
    notebook_flex = CompetitorItem(
        competitor="moba",
        external_id="FPC-LP-ASUS",
        name="Шлейф матрицы для ноутбука Asus X512DK",
        item_type="flex",
        first_seen_at=datetime(2026, 5, 2, 10, 0, 0),
        attrs_json={"item_type": "flex"},
    )
    tool = CompetitorItem(
        competitor="liberti",
        external_id="TOOL-1",
        name="Пинцет YAXUN 15ESD изогнутый",
        item_type="other",
        first_seen_at=datetime(2026, 5, 2, 10, 0, 0),
        attrs_json={"item_type": "other"},
    )
    matched_battery = CompetitorItem(
        competitor="moba",
        external_id="BTT-IP16",
        name="Аккумулятор для iPhone 16 Plus",
        item_type="battery",
        first_seen_at=datetime(2026, 5, 2, 10, 0, 0),
        attrs_json={"item_type": "battery"},
    )
    db_session.add_all([display, notebook_flex, tool, matched_battery])
    db_session.flush()
    db_session.add(
        CompetitorItemCompatibility(
            competitor_item_id=matched_battery.id,
            device_brand="apple",
            device_model="iphone 16 plus",
            source="test",
        )
    )
    db_session.commit()

    report = build_report(db_session, first_seen_after="2026-05-01")

    assert report["new_items"] == 4
    assert report["new_without_compatibility_total"] == 3
    assert report["new_without_compatibility"] == 1
    assert report["new_without_compatibility_ignored"] == 2
    assert report["new_without_compatibility_reason_target_device_group"] == 1
    assert report["new_without_compatibility_reason_non_phone_device_group_notebook"] == 1
    assert report["new_without_compatibility_reason_non_target_item_type_other"] == 1
