import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tasks.report_display_family_regular_topup_backtest import (
    _assert_monotonic_served_sales,
)
from tasks.report_display_family_regular_topup_holdout import (
    _assert_holdout_not_consumed,
    _load_training_freeze,
)


def _write_training_freeze(tmp_path) -> None:
    (tmp_path / "run-manifest.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "schema": "display_family_regular_topup_backtest.v2",
                "topup_actual_arrival_quantile": "p75",
                "baseline_order_trajectory_protected": True,
                "holdout_consumed": False,
                "best_passing_scenario": "regular-topup-c100",
                "dataset_hash": "dataset-1",
                "coverage_fractions": ["0.25", "0.50", "0.75", "1.00"],
                "order_cadence_days": 7,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "scenario-summary.csv").write_text(
        "scenario_id,coverage_fraction,passes_current_guardrails,gmroi\n"
        "current-family-order-pool,0,1,2.6\n"
        "regular-topup-c100,1.00,1,2.7\n",
        encoding="utf-8",
    )


def test_load_training_freeze_keeps_winner_and_training_gmroi(tmp_path) -> None:
    _write_training_freeze(tmp_path)

    manifest, gmroi_hurdle, cadence_days = _load_training_freeze(
        tmp_path,
        expected_dataset_hash="dataset-1",
    )

    assert manifest["best_passing_scenario"] == "regular-topup-c100"
    assert gmroi_hurdle == Decimal("2.6")
    assert cadence_days == 7


def test_load_training_freeze_rejects_changed_dataset(tmp_path) -> None:
    _write_training_freeze(tmp_path)

    with pytest.raises(ValueError, match="does not match"):
        _load_training_freeze(tmp_path, expected_dataset_hash="dataset-2")


def test_load_training_freeze_rejects_old_p50_contour(tmp_path) -> None:
    _write_training_freeze(tmp_path)
    manifest_path = tmp_path / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema"] = "display_family_regular_topup_backtest.v1"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="predates the corrected P75 contour"):
        _load_training_freeze(tmp_path, expected_dataset_hash="dataset-1")


def test_regular_topup_monotonicity_rejects_lost_service() -> None:
    current = SimpleNamespace(model={"SKU-1": SimpleNamespace(served_observed_qty=Decimal("10"))})
    candidate = SimpleNamespace(model={"SKU-1": SimpleNamespace(served_observed_qty=Decimal("9"))})

    with pytest.raises(RuntimeError, match="reduced served sales"):
        _assert_monotonic_served_sales(current=current, candidate=candidate)


def test_holdout_runner_refuses_second_completed_run(tmp_path) -> None:
    manifest_path = tmp_path / "run-manifest.json"
    manifest_path.write_text(json.dumps({"holdout_consumed": True}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="already been consumed"):
        _assert_holdout_not_consumed(manifest_path)
