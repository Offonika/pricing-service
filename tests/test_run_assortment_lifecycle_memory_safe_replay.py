from datetime import date
from pathlib import Path

import pytest

from app.services.assortment_lifecycle_replay_store import (
    AssortmentLifecycleReplayStore,
    stable_hash,
)
from app.services.assortment_lifecycle_v2_policy import DemandStatePolicy
from app.services.assortment_lifecycle_v2_replay import build_assortment_lifecycle_v2_trajectory
from tasks.report_assortment_lifecycle_v2_historical_backtest import replay_inputs_from_facts
from tasks.run_assortment_lifecycle_memory_safe_replay import build_memory_safe_trajectory


def _facts() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, code in enumerate(("SKU-1", "SKU-2"), start=1):
        rows.extend(
            [
                {
                    "business_date": "2025-01-01",
                    "nomenclature_code": code,
                    "fact_type": "item",
                    "payload": {"name": f"Дисплей {index}", "created_at": "2025-01-01"},
                },
                {
                    "business_date": "2025-01-02",
                    "nomenclature_code": code,
                    "fact_type": "supplier_order",
                    "payload": {},
                },
                {
                    "business_date": "2025-01-03",
                    "nomenclature_code": code,
                    "fact_type": "receipt",
                    "payload": {},
                },
                {
                    "business_date": "2026-01-01",
                    "nomenclature_code": code,
                    "fact_type": "sale_observation",
                    "payload": {
                        "quantity": str(index),
                        "document_id": f"doc-{index}",
                        "customer_id": f"customer-{index}",
                        "sales_point_id": f"point-{index}",
                    },
                },
                {
                    "business_date": "2026-01-01",
                    "nomenclature_code": code,
                    "fact_type": "available",
                    "payload": {"available": True},
                },
            ]
        )
    return rows


def _dataset(store: AssortmentLifecycleReplayStore) -> tuple[str, list[dict[str, object]]]:
    facts = _facts()
    dataset = store.put_dataset(
        scope="Дисплеи",
        observation_from=date(2025, 1, 1),
        observation_to=date(2026, 1, 3),
        facts=facts,
    )
    return dataset.key, facts


def test_memory_safe_v2_matches_monolithic_trajectory(tmp_path: Path) -> None:
    store = AssortmentLifecycleReplayStore(tmp_path / "replay.sqlite3")
    dataset_hash, facts = _dataset(store)
    policy = DemandStatePolicy(confirmation_days=7)

    ready, metadata = build_memory_safe_trajectory(
        store=store,
        dataset_hash=dataset_hash,
        model="v2",
        demand_policy=policy,
        history_start=date(2026, 1, 1),
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 3),
        batch_size=1,
    )
    items, sales, availability, orders, receipts = replay_inputs_from_facts(facts)
    expected = build_assortment_lifecycle_v2_trajectory(
        items=items,
        sales_observations_by_code=sales,
        availability_by_code=availability,
        supplier_orders_by_code=orders,
        receipts_by_code=receipts,
        history_start=date(2026, 1, 1),
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 3),
        demand_policy=policy,
    )

    assert ready.content_sha256 == stable_hash(expected)
    assert len(store.load_trajectory_rows(ready.trajectory_hash)) == len(expected)
    assert metadata["completed_sku_count"] == 2
    assert metadata["trajectory_reused"] is False


def test_memory_safe_replay_resumes_after_partition_failure(tmp_path: Path) -> None:
    store = AssortmentLifecycleReplayStore(tmp_path / "replay.sqlite3")
    dataset_hash, _facts_payload = _dataset(store)
    policy = DemandStatePolicy(confirmation_days=7)

    def stop_after_first_checkpoint(_payload):
        raise RuntimeError("simulated_process_stop")

    with pytest.raises(RuntimeError, match="simulated_process_stop"):
        build_memory_safe_trajectory(
            store=store,
            dataset_hash=dataset_hash,
            model="v2",
            demand_policy=policy,
            history_start=date(2026, 1, 1),
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 3),
            batch_size=1,
            progress=stop_after_first_checkpoint,
        )

    builds = store.manifest()["trajectory_builds"]
    assert builds[0]["completed_sku_count"] == 1
    assert store.manifest()["trajectories"] == []

    ready, metadata = build_memory_safe_trajectory(
        store=store,
        dataset_hash=dataset_hash,
        model="v2",
        demand_policy=policy,
        history_start=date(2026, 1, 1),
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 3),
        batch_size=1,
    )

    assert metadata["resumed"] is True
    assert ready.row_count == 6
    assert store.manifest()["trajectory_builds"] == []
