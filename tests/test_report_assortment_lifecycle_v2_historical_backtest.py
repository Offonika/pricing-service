from datetime import date
from pathlib import Path

from app.services.assortment_lifecycle_replay_store import AssortmentLifecycleReplayStore
from app.services.assortment_lifecycle_v2_policy import DemandStatePolicy
from tasks.report_assortment_lifecycle_v2_historical_backtest import (
    build_stage_diff,
    build_v2_replay_facts,
    load_or_build_v2_trajectory,
    replay_inputs_from_facts,
    summarize_diff,
)


def _legacy_facts():
    return [
        {
            "business_date": "2025-01-01",
            "nomenclature_code": "SKU-1",
            "fact_type": "item",
            "payload": {"name": "Дисплей", "created_at": "2025-01-01"},
        },
        {
            "business_date": "2025-01-02",
            "nomenclature_code": "SKU-1",
            "fact_type": "supplier_order",
            "payload": {},
        },
        {
            "business_date": "2025-01-03",
            "nomenclature_code": "SKU-1",
            "fact_type": "receipt",
            "payload": {},
        },
        {
            "business_date": "2026-02-01",
            "nomenclature_code": "SKU-1",
            "fact_type": "sale",
            "payload": {"quantity": "2"},
        },
    ]


def _observations():
    return [
        {
            "business_date": "2026-02-01",
            "nomenclature_code": "SKU-1",
            "quantity": "1",
            "document_id": "doc-a",
            "customer_id": "customer-a",
            "sales_point_id": "point-a",
        },
        {
            "business_date": "2026-02-01",
            "nomenclature_code": "SKU-1",
            "quantity": "1",
            "document_id": "doc-b",
            "customer_id": "customer-b",
            "sales_point_id": "point-b",
        },
    ]


def test_v2_dataset_adds_detail_without_double_counting_daily_sale() -> None:
    facts = build_v2_replay_facts(legacy_facts=_legacy_facts(), sale_observations=_observations())
    _items, sales, _availability, _orders, _receipts = replay_inputs_from_facts(facts)

    assert len(sales["SKU-1"]) == 2
    assert sum(row.quantity for row in sales["SKU-1"]) == 2
    assert {row.document_id for row in sales["SKU-1"]} == {"doc-a", "doc-b"}


def test_replay_inputs_ignore_technical_empty_cargo_date() -> None:
    facts = [
        *_legacy_facts(),
        {
            "business_date": "2026-01-10",
            "nomenclature_code": "SKU-1",
            "fact_type": "supplier_order",
            "payload": {"cargo_handoff_at": "1753-01-01"},
        },
    ]

    _items, _sales, _availability, orders, _receipts = replay_inputs_from_facts(facts)

    technical_order = next(row for row in orders["SKU-1"] if row.created_at == date(2026, 1, 10))
    assert technical_order.cargo_handoff_at is None


def test_v2_trajectory_is_reused_for_exact_dataset_policy_and_period(tmp_path: Path) -> None:
    store = AssortmentLifecycleReplayStore(tmp_path / "replay.sqlite3")
    facts = build_v2_replay_facts(legacy_facts=_legacy_facts(), sale_observations=_observations())
    dataset = store.put_dataset(
        scope="Дисплеи",
        observation_from=date(2025, 1, 1),
        observation_to=date(2026, 2, 2),
        facts=facts,
    )
    common = {
        "store": store,
        "dataset_hash": dataset.key,
        "facts": facts,
        "demand_policy": DemandStatePolicy(confirmation_days=7),
        "history_start": date(2026, 1, 1),
        "date_from": date(2026, 2, 1),
        "date_to": date(2026, 2, 2),
    }

    first_rows, first = load_or_build_v2_trajectory(**common)
    second_rows, second = load_or_build_v2_trajectory(**common)

    assert first_rows == second_rows
    assert first["trajectory_hash"] == second["trajectory_hash"]
    assert first["trajectory_reused"] is False
    assert second["trajectory_reused"] is True


def test_stage_diff_reports_exits_from_growing_and_latest_counts() -> None:
    legacy = [
        {
            "business_date": "2026-02-01",
            "nomenclature_code": "SKU-1",
            "status": "sale",
        }
    ]
    target = [
        {
            "business_date": "2026-02-01",
            "nomenclature_code": "SKU-1",
            "status": "working",
            "demand_state": "stable",
            "reason_codes": ["confirmed_demand_stable"],
        }
    ]

    diff = build_stage_diff(legacy, target)
    summary = summarize_diff(diff)

    assert diff[0]["changed"] == 1
    assert diff[0]["exited_growing"] == 1
    assert summary["latest_changed_sku_count"] == 1
    assert summary["latest_exits_from_growing"] == 1
