import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from tasks.analyze_display_auto_order_shortage_risk import (
    _validate_forecast_analysis_directory,
    attach_walk_forward_risk,
    build_candidate_buffers,
    join_as_of_features,
    review_example_rows,
)


def _window(
    opportunity_id: str,
    decision_date: str,
    outcome_end: str,
    *,
    loss: str,
) -> dict[str, str]:
    return {
        "opportunity_id": opportunity_id,
        "nomenclature_code": opportunity_id.split(":", 1)[0],
        "decision_date": decision_date,
        "outcome_end": outcome_end,
        "forecast_demand_qty": "10",
        "has_model_loss_in_horizon": str(int(Decimal(loss) > 0)),
        "model_lost_observed_qty_in_horizon": loss,
        "demand_pattern_preperiod": "intermittent",
    }


def _decision(code: str, decision_date: str) -> dict[str, str]:
    return {
        "nomenclature_code": code,
        "decision_date": decision_date,
        "scheduled_review": "1",
        "status": "sale",
        "forecast_rate_sales": "1",
        "sales_30": "45",
        "sales_90": "90",
        "physical_stock_qty": "8",
        "effective_reserve_qty": "3",
        "free_incoming_qty": "4",
        "lead_time_p50_days": "30",
        "lead_time_p75_days": "45",
        "lead_time_confidence": "medium",
        "sales_trend": "growing",
        "kmp4_open_qty": "2",
        "site_order_open_qty": "1",
    }


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_forecast_analysis_input_checksums_are_enforced(tmp_path: Path) -> None:
    for name in ("decision-windows.csv", "matched-pairs.csv"):
        (tmp_path / name).write_text("column\nvalue\n", encoding="utf-8")
    manifest = {
        "files": {
            name: _digest(tmp_path / name) for name in ("decision-windows.csv", "matched-pairs.csv")
        }
    }
    (tmp_path / "analysis-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert _validate_forecast_analysis_directory(tmp_path) == manifest
    (tmp_path / "matched-pairs.csv").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch"):
        _validate_forecast_analysis_directory(tmp_path)


def test_join_as_of_features_uses_effective_reserve_in_free_stock_cover() -> None:
    windows = [_window("SKU-1:2026-03-01", "2026-03-01", "2026-04-07", loss="2")]
    rows, quality = join_as_of_features(windows, [_decision("SKU-1", "2026-03-01")])

    assert quality["joined_window_count"] == 1
    assert Decimal(rows[0]["feature_stock_cover"]) == Decimal("0.5")
    assert Decimal(rows[0]["feature_position_cover"]) == Decimal("0.9")
    assert rows[0]["feature_open_signal_qty"] == "3"


def test_join_as_of_features_fails_on_missing_decision_key() -> None:
    windows = [_window("SKU-1:2026-03-01", "2026-03-01", "2026-04-07", loss="2")]

    with pytest.raises(ValueError, match="as-of feature join failed"):
        join_as_of_features(windows, [])


def test_walk_forward_uses_only_strictly_completed_prior_outcomes() -> None:
    rows = [
        _window("A:2026-03-01", "2026-03-01", "2026-03-03", loss="4"),
        _window("B:2026-03-02", "2026-03-02", "2026-03-04", loss="0"),
        _window("C:2026-03-04", "2026-03-04", "2026-03-06", loss="0"),
    ]

    scored = attach_walk_forward_risk(rows, min_training_samples=1)
    by_id = {row["opportunity_id"]: row for row in scored}

    assert by_id["A:2026-03-01"]["risk_training_sample_count"] == 0
    assert by_id["B:2026-03-02"]["risk_training_sample_count"] == 0
    assert by_id["C:2026-03-04"]["risk_training_sample_count"] == 1
    assert by_id["C:2026-03-04"]["risk_training_max_outcome_end"] == "2026-03-03"
    assert Decimal(by_id["C:2026-03-04"]["shortage_expected_qty"]) > 0


def test_candidate_buffers_rank_only_current_date_scores() -> None:
    rows = []
    for index in range(10):
        rows.append(
            {
                "opportunity_id": f"SKU-{index}:2026-04-01",
                "decision_date": "2026-04-01",
                "risk_training_sufficient": 1,
                "shortage_expected_qty": str(10 - index),
                "shortage_risk_probability": "0.5",
                "buffer_p75_qty": str(index + 1),
            }
        )

    buffers = build_candidate_buffers(rows, shares=(Decimal("0.20"),))

    assert buffers[("SKU-0:2026-04-01", "risk_top_20_service1")] == Decimal("1")
    assert buffers[("SKU-1:2026-04-01", "risk_top_20_expected")] == Decimal("9")
    assert buffers[("SKU-1:2026-04-01", "risk_top_20_p75")] == Decimal("2")
    assert ("SKU-2:2026-04-01", "risk_top_20_service1") not in buffers


def test_review_examples_keep_final_month_misses_for_human_check() -> None:
    pair_rows = [
        {
            "pair_id": "1",
            "period": "final_month_exposed",
            "case_opportunity_id": "CASE:2026-05-01",
            "control_opportunity_id": "CONTROL:2026-05-01",
            "case_loss_proxy_qty": "10",
            "case_expected_shortage_qty": "1",
            "control_expected_shortage_qty": "2",
            "case_score_higher": 0,
        }
    ]
    opportunities = {
        "CASE:2026-05-01": {
            "nomenclature_code": "CASE",
            "decision_date": "2026-05-01",
            "shortage_expected_qty": "1",
        },
        "CONTROL:2026-05-01": {
            "nomenclature_code": "CONTROL",
            "decision_date": "2026-05-01",
            "shortage_expected_qty": "2",
        },
    }

    rows = review_example_rows(pair_rows, opportunities, examples_per_type=1)

    assert {row["example_type"] for row in rows} == {"miss", "false_alarm"}
    assert all(row["period"] == "final_month_exposed" for row in rows)
    assert all(row["human_check"] for row in rows)
