from datetime import date
from decimal import Decimal
from pathlib import Path

from app.services.assortment_lifecycle_replay_store import AssortmentLifecycleReplayStore
from tasks.evaluate_assortment_lifecycle_exit_hysteresis_economics import (
    _candidate_parameters,
    apply_stored_trajectory,
    metric_deltas,
)


def test_apply_stored_trajectory_updates_only_training_facts(tmp_path: Path) -> None:
    store = AssortmentLifecycleReplayStore(tmp_path / "replay.sqlite3")
    dataset = store.put_dataset(
        scope="Дисплеи",
        observation_from=date(2026, 1, 1),
        observation_to=date(2026, 7, 1),
        facts=[
            {
                "business_date": "2026-01-01",
                "nomenclature_code": "SKU-1",
                "fact_type": "item",
                "payload": {"name": "Тестовый дисплей"},
            }
        ],
    )
    trajectory = store.put_trajectory(
        dataset_hash=dataset.key,
        model_version="test",
        policy_hash="policy",
        period_from=date(2026, 1, 1),
        period_to=date(2026, 7, 1),
        rows=[
            {
                "business_date": "2026-01-01",
                "nomenclature_code": "SKU-1",
                "previous_status": "working",
                "status": "sale",
                "demand_state": "spike",
                "sales_30": "6",
                "available_days_30": "30",
            },
            {
                "business_date": "2026-07-01",
                "nomenclature_code": "SKU-1",
                "previous_status": "sale",
                "status": "working",
                "demand_state": "stable",
            },
        ],
    )
    facts = {
        (date(2026, 1, 1), "SKU-1"): {"status": "working"},
        (date(2026, 7, 1), "SKU-1"): {"status": "sale"},
    }

    signature, applied, missing, spike_keys, spike_rates = apply_stored_trajectory(
        store=store,
        trajectory_hash=trajectory.key,
        fact_by_key=facts,
        date_to=date(2026, 6, 30),
    )

    assert len(signature) == 64
    assert applied == 1
    assert missing == 0
    assert facts[(date(2026, 1, 1), "SKU-1")] == {
        "status": "sale",
        "previous_status": "working",
    }
    assert facts[(date(2026, 7, 1), "SKU-1")]["status"] == "sale"
    assert spike_keys == {(date(2026, 1, 1), "SKU-1")}
    assert spike_rates[(date(2026, 1, 1), "SKU-1")] == Decimal("0.2")


def test_metric_deltas_include_capital_and_excess() -> None:
    baseline = {
        "served_sales_qty": "10",
        "gross_profit_rub": "100",
        "average_inventory_value_rub": "50",
        "carrying_cost_rub": "20",
        "economic_effect_rub": "80",
        "gmroi": "2",
        "ending_inventory_qty": "8",
        "ending_target_stock_qty": "6",
        "ending_excess_stock_qty": "2",
    }
    candidate = {key: Decimal(value) + 1 for key, value in baseline.items()}

    deltas = metric_deltas(candidate, baseline)

    assert set(deltas) == {f"{key}_delta" for key in baseline}
    assert set(deltas.values()) == {"1"}


def test_candidate_parameters_accept_explicit_growth_multiplier() -> None:
    parameters = _candidate_parameters("quality", growth_multiplier="1.5")

    assert parameters["growth_multiplier"] == "1.5"
    assert parameters["comparable_group_level"] == "quality"
