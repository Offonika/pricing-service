from __future__ import annotations

import sys
from datetime import date
from types import SimpleNamespace

import tasks.report_display_supplier_lead_time_history as lead_time_report
from tasks.report_display_supplier_lead_time_history import (
    aggregate_lead_time_rows,
    build_lead_time_detail_rows,
    build_seasonality_summary,
    build_summary,
    build_weekly_seasonality_rows,
    mark_lead_time_outliers,
)


def test_display_supplier_lead_time_defaults_to_three_year_history(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["report_display_supplier_lead_time_history"])

    args = lead_time_report._parse_args()

    assert args.history_months == 36


def test_display_supplier_lead_time_main_uses_bounded_onec_engine_lifecycle(
    monkeypatch, tmp_path
) -> None:
    class FakeEngine:
        def __init__(self) -> None:
            self.disposed = False

        def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()
    factory_calls: list[dict[str, object]] = []

    def fake_build_onec_engine(
        database_url: str,
        *,
        query_timeout_seconds: int | float,
        login_timeout_seconds: int | float,
    ) -> FakeEngine:
        factory_calls.append(
            {
                "database_url": database_url,
                "query_timeout_seconds": query_timeout_seconds,
                "login_timeout_seconds": login_timeout_seconds,
            }
        )
        return engine

    args = SimpleNamespace(
        folder="дисплеи",
        history_months=36,
        as_of=date(2026, 8, 29),
        limit=100,
        onec_database_url="mssql://override",
        supplier_order_mapping_json=tmp_path / "supplier.json",
        receipt_mapping_json=tmp_path / "receipt.json",
        output_csv=tmp_path / "aggregate.csv",
        output_detail_csv=tmp_path / "detail.csv",
        output_json=None,
        output_seasonality_csv=tmp_path / "seasonality.csv",
        output_seasonality_json=None,
        json=True,
    )
    settings = SimpleNamespace(
        onec_database_url="mssql://settings",
        onec_query_timeout_seconds=41,
        onec_login_timeout_seconds=13,
    )
    monkeypatch.setattr(lead_time_report, "_parse_args", lambda: args)
    monkeypatch.setattr(lead_time_report, "get_settings", lambda: settings)
    monkeypatch.setattr(
        lead_time_report, "default_history_start", lambda *_args, **_kwargs: date(2023, 8, 29)
    )
    monkeypatch.setattr(
        lead_time_report, "_load_document_line_mapping", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(lead_time_report, "build_onec_engine", fake_build_onec_engine)
    monkeypatch.setattr(
        lead_time_report,
        "fetch_display_supplier_lead_time_source_rows",
        lambda source_engine, **_kwargs: (
            {
                "nomenclature_rows": [],
                "supplier_order_rows": [],
                "receipt_rows": [],
            }
            if source_engine is engine
            else None
        ),
    )
    monkeypatch.setattr(lead_time_report, "build_lead_time_detail_rows", lambda *_args: [])
    monkeypatch.setattr(lead_time_report, "mark_lead_time_outliers", lambda _rows: {})
    monkeypatch.setattr(lead_time_report, "aggregate_lead_time_rows", lambda _rows: [])
    monkeypatch.setattr(lead_time_report, "build_weekly_seasonality_rows", lambda _rows: [])
    monkeypatch.setattr(
        lead_time_report,
        "build_seasonality_summary",
        lambda *_args, **_kwargs: {"weeks": 0},
    )
    monkeypatch.setattr(
        lead_time_report,
        "build_summary",
        lambda *_args, **_kwargs: {"detail_rows": 0},
    )
    monkeypatch.setattr(lead_time_report, "write_csv", lambda *_args, **_kwargs: None)

    assert lead_time_report.main() == 0
    assert factory_calls == [
        {
            "database_url": "mssql://override",
            "query_timeout_seconds": 41,
            "login_timeout_seconds": 13,
        }
    ]
    assert engine.disposed is True


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


def test_display_supplier_lead_time_uses_latest_posted_price_with_currency() -> None:
    detail_rows = build_lead_time_detail_rows(
        [
            {
                "supplier_name": "Supplier A",
                "supplier_ref": "0x01",
                "supplier_order_number": "РБ0001",
                "supplier_order_created_at": "2026-01-01",
                "cargo_handoff_at": "2026-01-05",
                "nomenclature_ref": "0xAAA",
                "nomenclature_code": "RB1",
                "name": "Дисплей тест",
                "qty": "2",
                "price": "110",
                "amount": "220",
                "price_currency_ref": "0xUSD",
                "price_currency_code": "840",
                "price_currency_name": "USD",
            },
            {
                "supplier_name": "Supplier A",
                "supplier_ref": "0x01",
                "supplier_order_number": "РБ0002",
                "supplier_order_created_at": "2026-02-01",
                "cargo_handoff_at": "2026-02-05",
                "nomenclature_ref": "0xAAA",
                "nomenclature_code": "RB1",
                "name": "Дисплей тест",
                "qty": "2",
                "price": "100",
                "amount": "200",
                "price_currency_ref": "0xUSD",
                "price_currency_code": "840",
                "price_currency_name": "USD",
            },
        ],
        [{"nomenclature_ref": "0xAAA", "receipt_at": "2026-02-10"}],
    )

    row = aggregate_lead_time_rows(detail_rows)[0]

    assert row["latest_purchase_price"] == "100"
    assert row["latest_purchase_price_at"] == "2026-02-01"
    assert row["price_currency_ref"] == "0xusd"
    assert row["price_currency_code"] == "840"


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
