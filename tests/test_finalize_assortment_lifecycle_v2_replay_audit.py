import csv
import json
from datetime import date
from pathlib import Path

from app.services.assortment_lifecycle_replay_store import AssortmentLifecycleReplayStore
from tasks.finalize_assortment_lifecycle_v2_replay_audit import finalize_replay_audit


def test_streaming_replay_audit_writes_diff_and_latest_summary(tmp_path: Path) -> None:
    store = AssortmentLifecycleReplayStore(tmp_path / "store.sqlite3")
    dataset = store.put_dataset(
        scope="Дисплеи",
        observation_from=date(2026, 2, 1),
        observation_to=date(2026, 2, 1),
        facts=[
            {
                "business_date": "2026-02-01",
                "nomenclature_code": "SKU-1",
                "fact_type": "item",
                "payload": {"name": "Дисплей"},
            }
        ],
    )
    legacy = store.put_trajectory(
        dataset_hash=dataset.key,
        model_version="legacy",
        policy_hash="legacy-policy",
        period_from=date(2026, 2, 1),
        period_to=date(2026, 2, 1),
        rows=[
            {
                "business_date": "2026-02-01",
                "nomenclature_code": "SKU-1",
                "status": "sale",
                "reason_codes": ["old"],
            }
        ],
    )
    target = store.put_trajectory(
        dataset_hash=dataset.key,
        model_version="v2",
        policy_hash="v2-policy",
        period_from=date(2026, 2, 1),
        period_to=date(2026, 2, 1),
        rows=[
            {
                "business_date": "2026-02-01",
                "nomenclature_code": "SKU-1",
                "previous_status": "",
                "status": "working",
                "demand_state": "stable",
                "reason_codes": ["new"],
                "first_supplier_order_at": "2025-12-31",
                "first_receipt_at": "2026-01-01",
                "first_sale_at": "2026-01-02",
                "history_age_days": 31,
                "sales_30": "1",
                "sales_90": "2",
                "sales_180": "3",
                "available_days_30": 30,
                "available_days_90": 90,
                "available_days_180": 180,
            }
        ],
    )

    summary = finalize_replay_audit(
        store=store,
        legacy_trajectory_hash=legacy.key,
        v2_trajectory_hash=target.key,
        output_dir=tmp_path / "report",
    )

    assert summary["daily_row_count"] == 1
    assert summary["latest"]["exits_from_growing"] == 1
    assert summary["historical_validation"]["status"] == "passed"
    assert summary["historical_validation"]["daily_row_count"] == 1
    assert summary["production_action"] == "none_read_only"
    with (tmp_path / "report" / "stage-diff.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["old_status"] == "sale"
    assert rows[0]["new_status"] == "working"
    stored_summary = json.loads((tmp_path / "report" / "summary.json").read_text(encoding="utf-8"))
    assert stored_summary == summary


def test_historical_validation_flags_active_regression_and_bad_chronology(
    tmp_path: Path,
) -> None:
    store = AssortmentLifecycleReplayStore(tmp_path / "store.sqlite3")
    dataset = store.put_dataset(
        scope="Дисплеи",
        observation_from=date(2026, 2, 1),
        observation_to=date(2026, 2, 2),
        facts=[
            {
                "business_date": "2026-02-01",
                "nomenclature_code": "SKU-1",
                "fact_type": "item",
                "payload": {"name": "Дисплей"},
            }
        ],
    )
    legacy_rows = [
        {"business_date": day, "nomenclature_code": "SKU-1", "status": "sale"}
        for day in ("2026-02-01", "2026-02-02")
    ]
    target_rows = [
        {
            "business_date": "2026-02-01",
            "nomenclature_code": "SKU-1",
            "previous_status": "",
            "status": "working",
            "demand_state": "stable",
            "first_supplier_order_at": "2026-02-02",
            "first_receipt_at": "2026-01-01",
            "first_sale_at": "2026-01-02",
            "history_age_days": 31,
        },
        {
            "business_date": "2026-02-02",
            "nomenclature_code": "SKU-1",
            "previous_status": "working",
            "status": "sales_start",
            "demand_state": "initial",
            "first_supplier_order_at": "2026-02-02",
            "first_receipt_at": "2026-01-01",
            "first_sale_at": "2026-01-02",
            "history_age_days": 32,
        },
    ]
    legacy = store.put_trajectory(
        dataset_hash=dataset.key,
        model_version="legacy",
        policy_hash="legacy-policy",
        period_from=date(2026, 2, 1),
        period_to=date(2026, 2, 2),
        rows=legacy_rows,
    )
    target = store.put_trajectory(
        dataset_hash=dataset.key,
        model_version="v2",
        policy_hash="v2-policy",
        period_from=date(2026, 2, 1),
        period_to=date(2026, 2, 2),
        rows=target_rows,
    )

    summary = finalize_replay_audit(
        store=store,
        legacy_trajectory_hash=legacy.key,
        v2_trajectory_hash=target.key,
        output_dir=tmp_path / "report",
    )["historical_validation"]

    assert summary["status"] == "needs_revision"
    assert summary["transition_count"] == 1
    assert summary["active_to_sales_start"] == {"transition_count": 1, "sku_count": 1}
    assert summary["data_quality"]["chronology_issue_sku_count"] == 0
    assert summary["data_quality"]["chronology_without_blocker_sku_count"] == 0
    assert summary["data_quality"]["by_issue_sku_count"]["receipt_before_first_order"] == 1
    assert summary["month_end_snapshots"][-1]["date"] == "2026-02-02"
