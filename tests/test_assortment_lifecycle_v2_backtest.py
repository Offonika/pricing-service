from __future__ import annotations

from copy import deepcopy

import pytest

from app.services.assortment_lifecycle_v2_backtest import (
    HOLDOUT_RESULTS_SCHEMA,
    TRAINING_RESULTS_SCHEMA,
    evaluate_selected_holdout,
    select_training_candidate,
)
from app.services.assortment_lifecycle_v2_policy import (
    load_assortment_lifecycle_v2_policy,
)


def _parameters(*, multiplier: str = "1.5", days: int = 14) -> dict[str, object]:
    return {
        "growth_multiplier": multiplier,
        "confirmation_days": days,
        "max_single_day_share": "0.7",
        "min_independent_sales": 2,
        "spike_quantity_policy": "ordinary_demand_only",
        "comparable_group_min_size": 8,
        "comparable_group_level": "quality",
    }


def _metrics(
    *,
    sales: str = "1",
    profit: str = "100",
    effect: str = "50",
    gmroi: str = "0.1",
    excess: str = "0",
) -> dict[str, str]:
    return {
        "served_sales_delta_qty": sales,
        "gross_profit_delta_rub": profit,
        "economic_effect_delta_rub": effect,
        "gmroi_delta": gmroi,
        "ending_excess_stock_delta_qty": excess,
    }


def _training_payload() -> dict[str, object]:
    return {
        "schema": TRAINING_RESULTS_SCHEMA,
        "period_from": "2026-02-01",
        "period_to": "2026-06-30",
        "candidates": [
            {
                "candidate_id": "training-winner",
                "parameters": _parameters(),
                "metrics": _metrics(effect="10"),
            },
            {
                "candidate_id": "holdout-looking-winner",
                "parameters": _parameters(multiplier="2.0"),
                "metrics": _metrics(effect="9"),
            },
        ],
    }


def _holdout_payload(candidate_id: str, *, effect: str = "5") -> dict[str, object]:
    multiplier = "1.5" if candidate_id == "training-winner" else "2.0"
    return {
        "schema": HOLDOUT_RESULTS_SCHEMA,
        "period_from": "2026-07-01",
        "period_to": "2026-07-31",
        "candidate": {
            "candidate_id": candidate_id,
            "parameters": _parameters(multiplier=multiplier),
            "metrics": _metrics(effect=effect),
        },
    }


def test_training_selection_uses_only_strict_training_metrics() -> None:
    policy = load_assortment_lifecycle_v2_policy()

    selection = select_training_candidate(
        _training_payload(), policy=policy, policy_sha256="policy-hash"
    )

    assert selection["selected_candidate_id"] == "training-winner"
    assert selection["holdout_consumed"] is False
    assert selection["production_authorized"] is False


@pytest.mark.parametrize(
    ("metric", "value", "criterion"),
    [
        ("served_sales_delta_qty", "-0.1", "served_sales_not_worse"),
        ("gross_profit_delta_rub", "-1", "gross_profit_not_worse"),
        ("economic_effect_delta_rub", "-1", "economic_effect_non_negative"),
        ("gmroi_delta", "-0.01", "gmroi_not_worse"),
        ("ending_excess_stock_delta_qty", "0.1", "ending_excess_stock_not_worse"),
    ],
)
def test_each_acceptance_criterion_can_reject_training_candidate(
    metric: str, value: str, criterion: str
) -> None:
    policy = load_assortment_lifecycle_v2_policy()
    payload = _training_payload()
    payload["candidates"] = [payload["candidates"][0]]
    payload["candidates"][0]["metrics"][metric] = value

    selection = select_training_candidate(payload, policy=policy, policy_sha256="hash")

    assert selection["selected_candidate_id"] is None
    assert criterion in selection["evaluations"][0]["evaluation"]["failed_criteria"]


def test_holdout_refuses_candidate_not_selected_on_training() -> None:
    policy = load_assortment_lifecycle_v2_policy()
    selection = select_training_candidate(
        _training_payload(), policy=policy, policy_sha256="policy-hash"
    )

    with pytest.raises(ValueError, match="holdout_candidate_must_match_training_selection"):
        evaluate_selected_holdout(
            selection,
            _holdout_payload("holdout-looking-winner"),
            policy=policy,
            policy_sha256="policy-hash",
        )


def test_holdout_refuses_parameter_change_after_training_selection() -> None:
    policy = load_assortment_lifecycle_v2_policy()
    selection = select_training_candidate(
        _training_payload(), policy=policy, policy_sha256="policy-hash"
    )
    holdout = _holdout_payload("training-winner")
    holdout["candidate"]["parameters"]["confirmation_days"] = 21

    with pytest.raises(ValueError, match="parameters_changed_after_selection"):
        evaluate_selected_holdout(
            selection,
            holdout,
            policy=policy,
            policy_sha256="policy-hash",
        )


def test_successful_holdout_only_unlocks_diff_review_not_live() -> None:
    policy = load_assortment_lifecycle_v2_policy()
    selection = select_training_candidate(
        _training_payload(), policy=policy, policy_sha256="policy-hash"
    )

    decision = evaluate_selected_holdout(
        selection,
        _holdout_payload("training-winner"),
        policy=policy,
        policy_sha256="policy-hash",
    )

    assert decision["decision"] == "eligible_for_diff_review"
    assert decision["requires_separate_diff_approval"] is True
    assert decision["live_enabled_unchanged"] is True
    assert decision["production_authorized"] is False


def test_failed_holdout_rejects_candidate() -> None:
    policy = load_assortment_lifecycle_v2_policy()
    selection = select_training_candidate(
        _training_payload(), policy=policy, policy_sha256="policy-hash"
    )
    holdout = _holdout_payload("training-winner")
    holdout["candidate"]["metrics"] = _metrics(excess="1")

    decision = evaluate_selected_holdout(
        selection, holdout, policy=policy, policy_sha256="policy-hash"
    )

    assert decision["decision"] == "rejected_on_holdout"
    assert decision["requires_separate_diff_approval"] is False


def test_training_and_holdout_periods_must_match_frozen_policy() -> None:
    policy = load_assortment_lifecycle_v2_policy()
    payload = deepcopy(_training_payload())
    payload["period_to"] = "2026-07-31"

    with pytest.raises(ValueError, match="training_period_must_match_policy"):
        select_training_candidate(payload, policy=policy, policy_sha256="hash")
