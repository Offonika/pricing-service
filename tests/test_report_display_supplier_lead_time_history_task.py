from __future__ import annotations

from datetime import date
from pathlib import Path

from tasks.report_display_supplier_lead_time_history import (
    DEFAULT_RECEIPT_MAPPING_JSON,
    DEFAULT_SUPPLIER_ORDER_MAPPING_JSON,
    RECEIPT_MAPPING_UNRESOLVED,
    SUPPLIER_ORDER_MAPPING_UNRESOLVED,
    _load_document_line_mapping,
    aggregate_lead_time_rows,
    build_lead_time_detail_rows,
    build_seasonality_summary,
    build_summary,
    build_weekly_seasonality_rows,
    mark_lead_time_outliers,
)


def test_display_supplier_lead_time_cron_uses_active_release() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    cron = (repo_root / "infra/cron/display_supplier_lead_time_refresh.cron").read_text(
        encoding="utf-8"
    )
    active_release = "/opt/MM/pricing-service-task43-current"

    assert f"REPO_DIR={active_release}" in cron
    assert f"{active_release}/infra/cron/display_supplier_lead_time_refresh.sh" in cron
    assert "/opt/MM/pricing-service/infra/cron/display_supplier_lead_time_refresh.sh" not in cron


def test_display_receipt_mapping_uses_actual_line_quantity() -> None:
    mapping = _load_document_line_mapping(
        DEFAULT_RECEIPT_MAPPING_JSON,
        error_code=RECEIPT_MAPPING_UNRESOLVED,
    )

    assert mapping.line_quantity_column == "_Fld4514"
    assert mapping.line_number_column == "_LineNo4508"
    assert mapping.line_supplier_order_column == "_Fld4525RRef"


def test_display_supplier_order_mapping_carries_line_identity_and_quantity() -> None:
    mapping = _load_document_line_mapping(
        DEFAULT_SUPPLIER_ORDER_MAPPING_JSON,
        error_code=SUPPLIER_ORDER_MAPPING_UNRESOLVED,
    )

    assert mapping.line_number_column == "_LineNo2516"
    assert mapping.line_quantity_column == "_Fld2520"


def test_display_supplier_lead_time_matches_nearest_receipt_after_cargo() -> None:
    detail_rows = build_lead_time_detail_rows(
        [
            {
                "supplier_name": "Supplier A",
                "supplier_code": "SUP-A",
                "supplier_ref": "0x01",
                "supplier_order_number": "РБ0001",
                "supplier_order_ref": "0x10",
                "supplier_order_created_at": "2026-01-01",
                "cargo_handoff_at": "2026-01-08",
                "expected_receipt_at": "2026-01-25",
                "nomenclature_ref": "0xAAA",
                "nomenclature_code": "RB1",
                "name": "Дисплей для Samsung A125 Galaxy A12 + тачскрин (черный)",
                "qty": "3",
                "price": "10",
                "amount": "30",
            }
        ],
        [
            {
                "nomenclature_ref": "0xAAA",
                "receipt_at": "2026-01-07",
                "receipt_number": "ПТУ0",
                "receipt_ref": "0x20",
            },
            {
                "nomenclature_ref": "0xAAA",
                "receipt_at": "2026-01-20",
                "receipt_number": "ПТУ1",
                "receipt_ref": "0x21",
            },
        ],
    )

    assert len(detail_rows) == 1
    row = detail_rows[0]
    assert row["supplier_prepare_days"] == "7"
    assert row["warehouse_receipt_at"] == "2026-01-20"
    assert row["logistics_receiving_days"] == "12"
    assert row["total_arrival_days"] == "19"
    assert row["receipt_match_confidence"] == "same_sku_after_cargo"
    assert row["display_group_key"] == "samsung a125 galaxy a12"


def test_display_supplier_lead_time_carries_order_responsible() -> None:
    detail_rows = build_lead_time_detail_rows(
        [
            {
                "supplier_name": "Supplier A",
                "supplier_code": "SUP-A",
                "supplier_ref": "0x01",
                "responsible_name": "Бочаров Омар",
                "responsible_code": "USR-1",
                "responsible_ref": "0xRESP1",
                "supplier_order_number": "РБ0001",
                "supplier_order_ref": "0x10",
                "supplier_order_created_at": "2026-01-01",
                "cargo_handoff_at": "2026-01-08",
                "nomenclature_ref": "0xAAA",
                "nomenclature_code": "RB1",
                "name": "Дисплей для Samsung A125 Galaxy A12 + тачскрин",
                "qty": "3",
                "amount": "30",
            },
            {
                "supplier_name": "Supplier A",
                "supplier_code": "SUP-A",
                "supplier_ref": "0x01",
                "responsible_name": "Лисовенко Вячеслав",
                "responsible_code": "USR-2",
                "responsible_ref": "0xRESP2",
                "supplier_order_number": "РБ0002",
                "supplier_order_ref": "0x11",
                "supplier_order_created_at": "2026-01-02",
                "cargo_handoff_at": "2026-01-09",
                "nomenclature_ref": "0xAAA",
                "nomenclature_code": "RB1",
                "name": "Дисплей для Samsung A125 Galaxy A12 + тачскрин",
                "qty": "1",
                "amount": "10",
            },
        ],
        [{"nomenclature_ref": "0xAAA", "receipt_at": "2026-01-20"}],
    )
    aggregate_rows = aggregate_lead_time_rows(detail_rows)

    assert detail_rows[0]["responsible_name"] == "Бочаров Омар"
    assert aggregate_rows[0]["responsible_count"] == 2
    assert aggregate_rows[0]["responsible_name"] in {
        "Бочаров Омар",
        "Лисовенко Вячеслав",
    }


def test_display_supplier_lead_time_aggregate_counts_missing_dates() -> None:
    detail_rows = build_lead_time_detail_rows(
        [
            {
                "supplier_name": "Supplier A",
                "supplier_ref": "0x01",
                "supplier_order_number": "РБ0001",
                "supplier_order_created_at": "2026-01-01",
                "cargo_handoff_at": "2026-01-06",
                "nomenclature_ref": "0xAAA",
                "nomenclature_code": "RB1",
                "name": "Дисплей для Samsung A125 Galaxy A12 + тачскрин",
                "qty": "2",
                "amount": "20",
            },
            {
                "supplier_name": "Supplier A",
                "supplier_ref": "0x01",
                "supplier_order_number": "РБ0002",
                "supplier_order_created_at": "2026-02-01",
                "cargo_handoff_at": "",
                "nomenclature_ref": "0xAAA",
                "nomenclature_code": "RB1",
                "name": "Дисплей для Samsung A125 Galaxy A12 + тачскрин",
                "qty": "1",
                "amount": "10",
            },
        ],
        [
            {
                "nomenclature_ref": "0xAAA",
                "receipt_at": "2026-01-16",
                "receipt_number": "ПТУ1",
            }
        ],
    )
    mark_lead_time_outliers(detail_rows)
    aggregate_rows = aggregate_lead_time_rows(detail_rows)

    assert len(aggregate_rows) == 1
    row = aggregate_rows[0]
    assert row["order_line_count"] == 2
    assert row["ordered_qty"] == "3"
    assert row["order_amount"] == "30"
    assert row["missing_cargo_count"] == 1
    assert row["missing_receipt_after_cargo_count"] == 0
    assert row["supplier_prepare_days_median"] == "5"
    assert row["logistics_receiving_days_median"] == "10"
    assert row["recommended_supplier_prepare_days"] == "5"
    assert row["recommended_logistics_days"] == "10"
    assert row["lead_time_confidence"] == "low"


def test_display_supplier_lead_time_summary_recommends_overall_defaults() -> None:
    detail_rows = build_lead_time_detail_rows(
        [
            {
                "supplier_name": "Supplier A",
                "supplier_ref": "0x01",
                "supplier_order_created_at": "2026-01-01",
                "cargo_handoff_at": "2026-01-08",
                "nomenclature_ref": "0xAAA",
                "nomenclature_code": "RB1",
                "name": "Дисплей для Xiaomi Redmi 9 + тачскрин",
            },
            {
                "supplier_name": "Supplier A",
                "supplier_ref": "0x01",
                "supplier_order_created_at": "2026-02-01",
                "cargo_handoff_at": "2026-02-11",
                "nomenclature_ref": "0xAAA",
                "nomenclature_code": "RB1",
                "name": "Дисплей для Xiaomi Redmi 9 + тачскрин",
            },
            {
                "supplier_name": "Supplier B",
                "supplier_ref": "0x02",
                "supplier_order_created_at": "2026-03-01",
                "cargo_handoff_at": "2026-03-12",
                "nomenclature_ref": "0xBBB",
                "nomenclature_code": "RB2",
                "name": "Дисплей для Xiaomi Redmi 10 + тачскрин",
            },
        ],
        [
            {"nomenclature_ref": "0xAAA", "receipt_at": "2026-01-20"},
            {"nomenclature_ref": "0xAAA", "receipt_at": "2026-02-25"},
            {"nomenclature_ref": "0xBBB", "receipt_at": "2026-03-28"},
        ],
    )
    mark_lead_time_outliers(detail_rows)
    aggregate_rows = aggregate_lead_time_rows(detail_rows)
    summary = build_summary(
        detail_rows,
        aggregate_rows=aggregate_rows,
        source_counts={"nomenclature_rows": 2, "supplier_order_rows": 3, "receipt_rows": 3},
        history_start=date(2026, 1, 1),
        as_of=date(2026, 4, 1),
        outlier_thresholds={},
    )

    assert summary["detail_rows"] == 3
    assert summary["aggregate_rows"] == 2
    assert summary["recommended_defaults"] == {
        "supplier_prepare_days": "10",
        "logistics_days": "14",
        "lead_time_days": "24",
    }


def test_display_supplier_lead_time_weekly_seasonality_flags_road_delay() -> None:
    detail_rows = build_lead_time_detail_rows(
        [
            {
                "supplier_name": "Supplier A",
                "supplier_ref": "0x01",
                "responsible_name": "Бочаров Омар",
                "responsible_ref": "0xRESP1",
                "supplier_order_created_at": "2025-10-01",
                "cargo_handoff_at": "2025-10-08",
                "nomenclature_ref": "0xAAA",
                "nomenclature_code": "RB1",
                "name": "Дисплей для Xiaomi Redmi 9 + тачскрин",
                "qty": "1",
                "amount": "10",
            },
            {
                "supplier_name": "Supplier A",
                "supplier_ref": "0x01",
                "responsible_name": "Бочаров Омар",
                "responsible_ref": "0xRESP1",
                "supplier_order_created_at": "2025-10-03",
                "cargo_handoff_at": "2025-10-10",
                "nomenclature_ref": "0xFFF",
                "nomenclature_code": "RB6",
                "name": "Дисплей для Xiaomi Redmi 14 + тачскрин",
                "qty": "1",
                "amount": "10",
            },
            {
                "supplier_name": "Supplier A",
                "supplier_ref": "0x01",
                "responsible_name": "Бочаров Омар",
                "responsible_ref": "0xRESP1",
                "supplier_order_created_at": "2025-10-04",
                "cargo_handoff_at": "2025-10-11",
                "nomenclature_ref": "0xGGG",
                "nomenclature_code": "RB7",
                "name": "Дисплей для Xiaomi Redmi 15 + тачскрин",
                "qty": "1",
                "amount": "10",
            },
            {
                "supplier_name": "Supplier A",
                "supplier_ref": "0x01",
                "responsible_name": "Бочаров Омар",
                "responsible_ref": "0xRESP1",
                "supplier_order_created_at": "2025-10-05",
                "cargo_handoff_at": "2025-10-12",
                "nomenclature_ref": "0xHHH",
                "nomenclature_code": "RB8",
                "name": "Дисплей для Xiaomi Redmi 16 + тачскрин",
                "qty": "1",
                "amount": "10",
            },
            {
                "supplier_name": "Supplier A",
                "supplier_ref": "0x01",
                "responsible_name": "Бочаров Омар",
                "responsible_ref": "0xRESP1",
                "supplier_order_created_at": "2025-10-02",
                "cargo_handoff_at": "2025-10-09",
                "nomenclature_ref": "0xBBB",
                "nomenclature_code": "RB2",
                "name": "Дисплей для Xiaomi Redmi 10 + тачскрин",
                "qty": "1",
                "amount": "10",
            },
            {
                "supplier_name": "Supplier A",
                "supplier_ref": "0x01",
                "responsible_name": "Бочаров Омар",
                "responsible_ref": "0xRESP1",
                "supplier_order_created_at": "2025-12-01",
                "cargo_handoff_at": "2025-12-08",
                "nomenclature_ref": "0xCCC",
                "nomenclature_code": "RB3",
                "name": "Дисплей для Xiaomi Redmi 11 + тачскрин",
                "qty": "1",
                "amount": "10",
            },
            {
                "supplier_name": "Supplier A",
                "supplier_ref": "0x01",
                "responsible_name": "Бочаров Омар",
                "responsible_ref": "0xRESP1",
                "supplier_order_created_at": "2025-12-02",
                "cargo_handoff_at": "2025-12-09",
                "nomenclature_ref": "0xDDD",
                "nomenclature_code": "RB4",
                "name": "Дисплей для Xiaomi Redmi 12 + тачскрин",
                "qty": "1",
                "amount": "10",
            },
            {
                "supplier_name": "Supplier A",
                "supplier_ref": "0x01",
                "responsible_name": "Бочаров Омар",
                "responsible_ref": "0xRESP1",
                "supplier_order_created_at": "2025-12-03",
                "cargo_handoff_at": "2025-12-10",
                "nomenclature_ref": "0xEEE",
                "nomenclature_code": "RB5",
                "name": "Дисплей для Xiaomi Redmi 13 + тачскрин",
                "qty": "1",
                "amount": "10",
            },
        ],
        [
            {"nomenclature_ref": "0xAAA", "receipt_at": "2025-10-18"},
            {"nomenclature_ref": "0xBBB", "receipt_at": "2025-10-19"},
            {"nomenclature_ref": "0xFFF", "receipt_at": "2025-10-20"},
            {"nomenclature_ref": "0xGGG", "receipt_at": "2025-10-21"},
            {"nomenclature_ref": "0xHHH", "receipt_at": "2025-10-22"},
            {"nomenclature_ref": "0xCCC", "receipt_at": "2026-01-20"},
            {"nomenclature_ref": "0xDDD", "receipt_at": "2026-01-21"},
            {"nomenclature_ref": "0xEEE", "receipt_at": "2026-01-22"},
        ],
    )

    rows = build_weekly_seasonality_rows(detail_rows)
    summary = build_seasonality_summary(
        rows,
        history_start=date(2025, 10, 1),
        as_of=date(2026, 2, 1),
    )
    december = next(row for row in rows if row["season_label"] == "pre_new_year")

    assert december["road_seasonality_signal"] == 1
    assert december["route_risk_level"] == "medium"
    assert december["top_responsible_name"] == "Бочаров Омар"
    assert summary["road_seasonality_signal_weeks"] == 1
