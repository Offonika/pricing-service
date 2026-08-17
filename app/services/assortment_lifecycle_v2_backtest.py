from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from app.services.assortment_lifecycle_v2_policy import AssortmentLifecycleV2Policy

TRAINING_RESULTS_SCHEMA = "display_assortment_lifecycle_v2_training_results.v1"
SELECTION_SCHEMA = "display_assortment_lifecycle_v2_candidate_selection.v1"
HOLDOUT_RESULTS_SCHEMA = "display_assortment_lifecycle_v2_holdout_result.v1"
HOLDOUT_DECISION_SCHEMA = "display_assortment_lifecycle_v2_holdout_decision.v1"


@dataclass(frozen=True)
class BacktestMetrics:
    served_sales_delta_qty: Decimal
    gross_profit_delta_rub: Decimal
    economic_effect_delta_rub: Decimal
    gmroi_delta: Decimal
    ending_excess_stock_delta_qty: Decimal


def evaluate_backtest_metrics(metrics: BacktestMetrics) -> dict[str, Any]:
    criteria = {
        "served_sales_not_worse": metrics.served_sales_delta_qty >= 0,
        "gross_profit_not_worse": metrics.gross_profit_delta_rub >= 0,
        "economic_effect_non_negative": metrics.economic_effect_delta_rub >= 0,
        "gmroi_not_worse": metrics.gmroi_delta >= 0,
        "ending_excess_stock_not_worse": metrics.ending_excess_stock_delta_qty <= 0,
    }
    return {
        "metrics": {name: str(value) for name, value in asdict(metrics).items()},
        "criteria": criteria,
        "passed": all(criteria.values()),
        "failed_criteria": [name for name, passed in criteria.items() if not passed],
    }


def select_training_candidate(
    payload: Mapping[str, Any],
    *,
    policy: AssortmentLifecycleV2Policy,
    policy_sha256: str,
) -> dict[str, Any]:
    """Select only from the frozen training period; holdout is not an input."""

    _require_schema(payload, TRAINING_RESULTS_SCHEMA)
    _validate_period(
        payload,
        expected_from=policy.periods.training_from,
        expected_to=policy.periods.training_to,
        prefix="training",
    )
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValueError("training_candidates_required")

    evaluated: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_candidate in raw_candidates:
        candidate = _mapping(raw_candidate, "training_candidate_must_be_object")
        candidate_id = _required_text(candidate, "candidate_id")
        if candidate_id in seen_ids:
            raise ValueError(f"duplicate_training_candidate:{candidate_id}")
        seen_ids.add(candidate_id)
        parameters = dict(_mapping(candidate.get("parameters"), "candidate_parameters_required"))
        _validate_candidate_parameters(parameters, policy=policy)
        evaluation = evaluate_backtest_metrics(
            _metrics(_mapping(candidate.get("metrics"), "candidate_metrics_required"))
        )
        evaluated.append(
            {
                "candidate_id": candidate_id,
                "parameters": parameters,
                "candidate_fingerprint": _fingerprint(parameters),
                "evaluation": evaluation,
            }
        )

    eligible = [row for row in evaluated if row["evaluation"]["passed"]]
    if not eligible:
        selected = None
        decision = "no_training_candidate_passed"
    else:
        selected = sorted(eligible, key=_training_rank)[0]
        decision = "selected_for_single_holdout"
    return {
        "schema": SELECTION_SCHEMA,
        "policy_schema": policy.schema,
        "policy_id": policy.policy_id,
        "policy_sha256": policy_sha256,
        "training_period": {
            "from": policy.periods.training_from.isoformat(),
            "to": policy.periods.training_to.isoformat(),
        },
        "selection_basis": (
            "strict_acceptance_then_economic_effect_served_sales_gross_profit_"
            "gmroi_ending_excess"
        ),
        "decision": decision,
        "selected_candidate_id": selected["candidate_id"] if selected else None,
        "selected_candidate_fingerprint": (selected["candidate_fingerprint"] if selected else None),
        "selected_parameters": selected["parameters"] if selected else None,
        "selected_training_evaluation": selected["evaluation"] if selected else None,
        "candidate_count": len(evaluated),
        "training_pass_count": len(eligible),
        "evaluations": evaluated,
        "holdout_consumed": False,
        "production_authorized": False,
        "production_action": "none_read_only",
    }


def evaluate_selected_holdout(
    selection: Mapping[str, Any],
    holdout_payload: Mapping[str, Any],
    *,
    policy: AssortmentLifecycleV2Policy,
    policy_sha256: str,
) -> dict[str, Any]:
    """Evaluate exactly the training-selected candidate on the configured holdout."""

    _require_schema(selection, SELECTION_SCHEMA)
    if selection.get("policy_sha256") != policy_sha256:
        raise ValueError("holdout_policy_does_not_match_selection")
    if selection.get("decision") != "selected_for_single_holdout":
        raise ValueError("holdout_requires_selected_training_candidate")
    selected_id = _required_text(selection, "selected_candidate_id")
    selected_fingerprint = _required_text(selection, "selected_candidate_fingerprint")

    _require_schema(holdout_payload, HOLDOUT_RESULTS_SCHEMA)
    _validate_period(
        holdout_payload,
        expected_from=policy.periods.holdout_from,
        expected_to=policy.periods.holdout_to,
        prefix="holdout",
    )
    candidate = _mapping(holdout_payload.get("candidate"), "holdout_candidate_required")
    candidate_id = _required_text(candidate, "candidate_id")
    if candidate_id != selected_id:
        raise ValueError(
            f"holdout_candidate_must_match_training_selection:{selected_id}:{candidate_id}"
        )
    parameters = dict(_mapping(candidate.get("parameters"), "candidate_parameters_required"))
    _validate_candidate_parameters(parameters, policy=policy)
    if _fingerprint(parameters) != selected_fingerprint:
        raise ValueError("holdout_candidate_parameters_changed_after_selection")

    evaluation = evaluate_backtest_metrics(
        _metrics(_mapping(candidate.get("metrics"), "candidate_metrics_required"))
    )
    passed = bool(evaluation["passed"])
    return {
        "schema": HOLDOUT_DECISION_SCHEMA,
        "policy_schema": policy.schema,
        "policy_id": policy.policy_id,
        "policy_sha256": policy_sha256,
        "selection_sha256": _fingerprint(selection),
        "candidate_id": candidate_id,
        "candidate_fingerprint": selected_fingerprint,
        "parameters": parameters,
        "holdout_period": {
            "from": policy.periods.holdout_from.isoformat(),
            "to": policy.periods.holdout_to.isoformat(),
        },
        "evaluation": evaluation,
        "decision": "eligible_for_diff_review" if passed else "rejected_on_holdout",
        "requires_separate_diff_approval": passed,
        "live_enabled_unchanged": True,
        "production_authorized": False,
        "production_action": "none_read_only",
    }


def _training_rank(row: Mapping[str, Any]) -> tuple[Any, ...]:
    metrics = row["evaluation"]["metrics"]
    return (
        -Decimal(metrics["economic_effect_delta_rub"]),
        -Decimal(metrics["served_sales_delta_qty"]),
        -Decimal(metrics["gross_profit_delta_rub"]),
        -Decimal(metrics["gmroi_delta"]),
        Decimal(metrics["ending_excess_stock_delta_qty"]),
        str(row["candidate_id"]),
    )


def _validate_candidate_parameters(
    parameters: Mapping[str, Any], *, policy: AssortmentLifecycleV2Policy
) -> None:
    grid = policy.backtest_grid
    allowed: tuple[tuple[str, Sequence[Any]], ...] = (
        ("growth_multiplier", grid.growth_multipliers),
        ("confirmation_days", grid.confirmation_days),
        ("max_single_day_share", grid.max_single_day_shares),
        ("min_independent_sales", grid.min_independent_sales),
        ("spike_quantity_policy", grid.spike_quantity_policies),
        ("comparable_group_min_size", grid.comparable_group_min_sizes),
        ("comparable_group_level", grid.comparable_group_levels),
    )
    for name, values in allowed:
        if name not in parameters:
            raise ValueError(f"candidate_parameter_required:{name}")
        raw_value = parameters[name]
        if values and isinstance(values[0], Decimal):
            value: Any = _decimal(raw_value, f"candidate_parameter_invalid:{name}")
        elif values and isinstance(values[0], int):
            try:
                value = int(raw_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"candidate_parameter_invalid:{name}") from exc
        else:
            value = str(raw_value).strip()
        if value not in values:
            raise ValueError(f"candidate_parameter_outside_grid:{name}:{raw_value}")


def _metrics(payload: Mapping[str, Any]) -> BacktestMetrics:
    return BacktestMetrics(
        served_sales_delta_qty=_decimal_field(payload, "served_sales_delta_qty"),
        gross_profit_delta_rub=_decimal_field(payload, "gross_profit_delta_rub"),
        economic_effect_delta_rub=_decimal_field(payload, "economic_effect_delta_rub"),
        gmroi_delta=_decimal_field(payload, "gmroi_delta"),
        ending_excess_stock_delta_qty=_decimal_field(payload, "ending_excess_stock_delta_qty"),
    )


def _validate_period(
    payload: Mapping[str, Any], *, expected_from: date, expected_to: date, prefix: str
) -> None:
    actual_from = _date_field(payload, "period_from")
    actual_to = _date_field(payload, "period_to")
    if actual_from != expected_from or actual_to != expected_to:
        raise ValueError(
            f"{prefix}_period_must_match_policy:"
            f"{expected_from.isoformat()}:{expected_to.isoformat()}"
        )


def _require_schema(payload: Mapping[str, Any], expected: str) -> None:
    if payload.get("schema") != expected:
        raise ValueError(f"unsupported_backtest_schema:{payload.get('schema')}:{expected}")


def _mapping(value: Any, error: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(error)
    return value


def _required_text(payload: Mapping[str, Any], name: str) -> str:
    value = str(payload.get(name) or "").strip()
    if not value:
        raise ValueError(f"backtest_value_required:{name}")
    return value


def _decimal_field(payload: Mapping[str, Any], name: str) -> Decimal:
    if name not in payload:
        raise ValueError(f"backtest_metric_required:{name}")
    return _decimal(payload[name], f"backtest_metric_invalid:{name}")


def _decimal(value: Any, error: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(error) from exc
    if not parsed.is_finite():
        raise ValueError(error)
    return parsed


def _date_field(payload: Mapping[str, Any], name: str) -> date:
    value = _required_text(payload, name)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"backtest_date_invalid:{name}") from exc


def _fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
