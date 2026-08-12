"""Screen reconstructed pipeline-lot reliability without replaying orders.

The task reads only frozen v19 risk scores and the frozen preflight daily facts.
It reconstructs FIFO pseudo-lots from changes in the historical supplier-order
balance, keeps the opening balance left-censored, and tests whether lot age plus
a separate demand-acceleration add-on improves matched-pair screening.  It has
no production, purchasing, database, or external side effects.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from tasks.analyze_display_auto_order_shortage_risk import (
    ONE,
    ZERO,
    _ceil,
    _clean,
    _decimal,
    _read_csv,
    _sha256,
    _write_csv,
)
from tasks.display_auto_order_backtest_preflight import validate_preflight_directory

TOLERANCE = Decimal("0.001")
TOP_SHARE = Decimal("0.50")
ECONOMIC_INCREMENTAL_SHARES = (
    Decimal("0.25"),
    Decimal("0.50"),
    Decimal("0.75"),
    ONE,
)
SELECTED_LOT_PROFILE = "lot_quantile_mass_acceleration_p50_w75"


@dataclass(frozen=True)
class ReliabilityProfile:
    name: str
    p50_to_p75_fraction: Decimal
    over_p75_fraction: Decimal
    acceleration_weight: Decimal = ZERO
    acceleration_gate: str = "off"


PROFILES = (
    ReliabilityProfile("baseline_v19", ONE, ONE),
    ReliabilityProfile("lot_p75_guard", ONE, ZERO),
    ReliabilityProfile("lot_quantile_mass", Decimal("0.50"), Decimal("0.25")),
    ReliabilityProfile(
        "lot_quantile_mass_acceleration_all",
        Decimal("0.50"),
        Decimal("0.25"),
        ONE,
        "all",
    ),
    ReliabilityProfile(
        "lot_quantile_mass_acceleration_p50_w25",
        Decimal("0.50"),
        Decimal("0.25"),
        Decimal("0.25"),
        "over_p50",
    ),
    ReliabilityProfile(
        "lot_quantile_mass_acceleration_p50_w50",
        Decimal("0.50"),
        Decimal("0.25"),
        Decimal("0.50"),
        "over_p50",
    ),
    ReliabilityProfile(
        "lot_quantile_mass_acceleration_p50_w75",
        Decimal("0.50"),
        Decimal("0.25"),
        Decimal("0.75"),
        "over_p50",
    ),
    ReliabilityProfile(
        "lot_quantile_mass_acceleration_p50",
        Decimal("0.50"),
        Decimal("0.25"),
        ONE,
        "over_p50",
    ),
    ReliabilityProfile(
        "lot_quantile_mass_acceleration_p75_w25",
        Decimal("0.50"),
        Decimal("0.25"),
        Decimal("0.25"),
        "over_p75",
    ),
    ReliabilityProfile(
        "lot_quantile_mass_acceleration_p75_w50",
        Decimal("0.50"),
        Decimal("0.25"),
        Decimal("0.50"),
        "over_p75",
    ),
    ReliabilityProfile(
        "lot_quantile_mass_acceleration_p75_w75",
        Decimal("0.50"),
        Decimal("0.25"),
        Decimal("0.75"),
        "over_p75",
    ),
    ReliabilityProfile(
        "lot_quantile_mass_acceleration_p75",
        Decimal("0.50"),
        Decimal("0.25"),
        ONE,
        "over_p75",
    ),
)


def _validate_risk_directory(path: Path) -> dict[str, Any]:
    manifest_path = path / "analysis-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_files = manifest.get("files") or {}
    for name in ("risk-scores.csv", "matched-risk-pairs.csv"):
        expected = _clean(expected_files.get(name))
        if not expected or _sha256(path / name) != expected:
            raise ValueError(f"risk analysis checksum mismatch: {name}")
    return manifest


def _daily_rows(path: Path) -> Iterable[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def _consume_fifo(
    lots: deque[list[Any]],
    quantity: Decimal,
) -> tuple[Decimal, int]:
    remaining = max(ZERO, quantity)
    closed = ZERO
    partial_closures = 0
    while lots and remaining > TOLERANCE:
        available = _decimal(lots[0][1])
        used = min(available, remaining)
        lots[0][1] = available - used
        remaining -= used
        closed += used
        if lots[0][1] <= TOLERANCE:
            lots.popleft()
        elif used > ZERO:
            partial_closures += 1
    return remaining, partial_closures


def _lot_state(
    lots: Sequence[Sequence[Any]],
    *,
    as_of: date,
    p50_days: int,
    p75_days: int,
) -> dict[str, Decimal | int]:
    unknown = ZERO
    known = ZERO
    within_p50 = ZERO
    p50_to_p75 = ZERO
    over_p75 = ZERO
    weighted_age = ZERO
    oldest_age = 0
    for opened_at, raw_qty in lots:
        qty = max(ZERO, _decimal(raw_qty))
        if qty <= ZERO:
            continue
        if opened_at is None:
            unknown += qty
            continue
        age_days = max(0, (as_of - opened_at).days)
        known += qty
        weighted_age += qty * Decimal(age_days)
        oldest_age = max(oldest_age, age_days)
        if age_days <= p50_days:
            within_p50 += qty
        elif age_days <= p75_days:
            p50_to_p75 += qty
        else:
            over_p75 += qty
    total = unknown + known
    return {
        "lot_total_pipeline_qty": total,
        "lot_unknown_age_pipeline_qty": unknown,
        "lot_known_age_pipeline_qty": known,
        "lot_within_p50_pipeline_qty": within_p50,
        "lot_p50_to_p75_pipeline_qty": p50_to_p75,
        "lot_over_p75_pipeline_qty": over_p75,
        "lot_known_age_share": known / total if total > ZERO else ZERO,
        "lot_over_p50_share": ((p50_to_p75 + over_p75) / known if known > ZERO else ZERO),
        "lot_over_p75_share": over_p75 / known if known > ZERO else ZERO,
        "lot_mean_known_age_days": weighted_age / known if known > ZERO else ZERO,
        "lot_oldest_known_age_days": oldest_age,
    }


def reconstruct_lot_age_features(
    opportunities: Sequence[Mapping[str, Any]],
    daily_rows: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    targets: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in opportunities:
        key = (_clean(row.get("nomenclature_code")), _clean(row.get("decision_date")))
        if key in targets:
            raise ValueError(f"duplicate risk opportunity key: {key}")
        targets[key] = row

    queues: dict[str, deque[list[Any]]] = defaultdict(deque)
    prior_balance: dict[str, Decimal] = {}
    last_date_by_code: dict[str, date] = {}
    features: dict[tuple[str, str], dict[str, Any]] = {}
    positive_increments = 0
    negative_increments = 0
    partial_closures = 0
    unmatched_close_qty = ZERO
    row_count = 0

    for source in daily_rows:
        row_count += 1
        code = _clean(source.get("nomenclature_code"))
        rendered_date = _clean(source.get("business_date"))
        if not code or not rendered_date:
            continue
        business_date = date.fromisoformat(rendered_date)
        if business_date <= last_date_by_code.get(code, date.min):
            raise ValueError(f"daily facts are not strictly increasing for {code}")
        last_date_by_code[code] = business_date
        gross = max(ZERO, _decimal(source.get("gross_incoming_qty")))
        previous = prior_balance.get(code)
        if previous is None:
            if gross > TOLERANCE:
                queues[code].append([None, gross])
        else:
            delta = gross - previous
            if delta > TOLERANCE:
                queues[code].append([business_date, delta])
                positive_increments += 1
            elif delta < -TOLERANCE:
                unmatched, partial = _consume_fifo(queues[code], -delta)
                unmatched_close_qty += unmatched
                partial_closures += partial
                negative_increments += 1
        prior_balance[code] = gross

        key = (code, rendered_date)
        opportunity = targets.get(key)
        if opportunity is None:
            continue
        p50_days = max(1, int(_decimal(opportunity.get("lead_time_p50_days")) or 1))
        p75_days = max(
            p50_days,
            int(
                _decimal(
                    opportunity.get("as_of_lead_time_p75_days")
                    or opportunity.get("lead_time_p50_days")
                    or 1
                )
            ),
        )
        state = _lot_state(
            tuple(queues[code]),
            as_of=business_date,
            p50_days=p50_days,
            p75_days=p75_days,
        )
        reconstructed = _decimal(state["lot_total_pipeline_qty"])
        state["lot_balance_difference_qty"] = reconstructed - gross
        state["lot_p50_days"] = p50_days
        state["lot_p75_days"] = p75_days
        features[key] = dict(state)

    missing = sorted(set(targets) - set(features))
    if missing:
        raise ValueError(f"missing daily pipeline facts for {len(missing)} opportunities")
    if abs(unmatched_close_qty) > TOLERANCE:
        raise ValueError(f"pipeline FIFO close exceeds reconstructed lots: {unmatched_close_qty}")

    output: list[dict[str, Any]] = []
    max_difference = ZERO
    for source in opportunities:
        row = dict(source)
        key = (_clean(row.get("nomenclature_code")), _clean(row.get("decision_date")))
        row.update(features[key])
        max_difference = max(max_difference, abs(_decimal(row["lot_balance_difference_qty"])))
        output.append(row)
    quality = {
        "daily_fact_row_count": row_count,
        "opportunity_count": len(opportunities),
        "joined_opportunity_count": len(output),
        "positive_pipeline_increment_count": positive_increments,
        "negative_pipeline_increment_count": negative_increments,
        "partial_fifo_closure_count": partial_closures,
        "unmatched_fifo_close_qty": str(unmatched_close_qty),
        "max_reconstructed_balance_difference_qty": str(max_difference),
    }
    return output, quality


def score_profile(
    row: Mapping[str, Any],
    profile: ReliabilityProfile,
) -> dict[str, Decimal]:
    baseline = max(ZERO, _decimal(row.get("shortage_expected_qty")))
    if profile.name == "baseline_v19":
        return {
            "score": baseline,
            "reliable_free_incoming_qty": max(ZERO, _decimal(row.get("as_of_free_incoming_qty"))),
            "lot_shortage_qty": ZERO,
            "acceleration_addon_qty": ZERO,
        }
    gross = max(ZERO, _decimal(row.get("lot_total_pipeline_qty")))
    free_incoming = max(ZERO, _decimal(row.get("as_of_free_incoming_qty")))
    free_scale = min(ONE, free_incoming / gross) if gross > ZERO else ZERO
    reliable_gross = sum(
        (
            _decimal(row.get("lot_unknown_age_pipeline_qty")),
            _decimal(row.get("lot_within_p50_pipeline_qty")),
            _decimal(row.get("lot_p50_to_p75_pipeline_qty")) * profile.p50_to_p75_fraction,
            _decimal(row.get("lot_over_p75_pipeline_qty")) * profile.over_p75_fraction,
        ),
        ZERO,
    )
    reliable_free = min(free_incoming, max(ZERO, reliable_gross * free_scale))
    demand = max(ZERO, _decimal(row.get("forecast_demand_qty")))
    free_stock = max(ZERO, _decimal(row.get("as_of_free_stock_qty")))
    lot_shortage = max(ZERO, demand - free_stock - reliable_free)
    sales_30_rate = max(ZERO, _decimal(row.get("as_of_sales_30"))) / Decimal("30")
    forecast_rate = max(ZERO, _decimal(row.get("forecast_rate_sales")))
    horizon_days = max(1, int(_decimal(row.get("horizon_days")) or 1))
    acceleration_gap = max(ZERO, sales_30_rate - forecast_rate) * Decimal(horizon_days)
    acceleration_allowed = (
        profile.acceleration_gate == "all"
        or (
            profile.acceleration_gate == "over_p50"
            and (
                _decimal(row.get("lot_p50_to_p75_pipeline_qty"))
                + _decimal(row.get("lot_over_p75_pipeline_qty"))
            )
            > ZERO
        )
        or (
            profile.acceleration_gate == "over_p75"
            and _decimal(row.get("lot_over_p75_pipeline_qty")) > ZERO
        )
    )
    acceleration_addon = (
        min(demand, acceleration_gap) * profile.acceleration_weight
        if acceleration_allowed
        else ZERO
    )
    return {
        "score": max(baseline, lot_shortage + acceleration_addon),
        "reliable_free_incoming_qty": reliable_free,
        "lot_shortage_qty": lot_shortage,
        "acceleration_addon_qty": acceleration_addon,
    }


def attach_profile_scores(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        for profile in PROFILES:
            score = score_profile(row, profile)
            prefix = profile.name
            row[f"{prefix}_score"] = str(score["score"])
            row[f"{prefix}_reliable_free_incoming_qty"] = str(score["reliable_free_incoming_qty"])
            row[f"{prefix}_lot_shortage_qty"] = str(score["lot_shortage_qty"])
            row[f"{prefix}_acceleration_addon_qty"] = str(score["acceleration_addon_qty"])
        output.append(row)
    return output


def top_share_buffers(
    rows: Sequence[Mapping[str, Any]],
    profile: ReliabilityProfile,
    *,
    share: Decimal = TOP_SHARE,
) -> dict[str, Decimal]:
    by_date: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if int(_decimal(row.get("risk_training_sufficient"))) == 1:
            by_date[_clean(row.get("decision_date"))].append(row)
    buffers: dict[str, Decimal] = {}
    score_column = f"{profile.name}_score"
    for decision_date in sorted(by_date):
        ranked = sorted(
            by_date[decision_date],
            key=lambda row: (
                -_decimal(row.get(score_column)),
                -_decimal(row.get("shortage_risk_probability")),
                _clean(row.get("opportunity_id")),
            ),
        )
        selected_count = min(
            len(ranked),
            max(1, int(_ceil(Decimal(len(ranked)) * share))),
        )
        for row in ranked[:selected_count]:
            buffers[_clean(row.get("opportunity_id"))] = max(
                ONE, _ceil(_decimal(row.get(score_column)))
            )
    return buffers


def economic_incremental_buffers(
    rows: Sequence[Mapping[str, Any]],
    *,
    challenger: ReliabilityProfile,
    incremental_share: Decimal,
) -> tuple[dict[str, Decimal], list[dict[str, Any]]]:
    if incremental_share < ZERO or incremental_share > ONE:
        raise ValueError("incremental_share must be between 0 and 1")
    baseline_profile = next(profile for profile in PROFILES if profile.name == "baseline_v19")
    baseline = top_share_buffers(rows, baseline_profile)
    desired = top_share_buffers(rows, challenger)
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        opportunity_id = _clean(row.get("opportunity_id"))
        extra_qty = max(
            ZERO, desired.get(opportunity_id, ZERO) - baseline.get(opportunity_id, ZERO)
        )
        if extra_qty <= ZERO:
            continue
        cost = max(ZERO, _decimal(row.get("inventory_cost_per_unit_rub")))
        margin = max(ZERO, _decimal(row.get("gross_margin_per_unit_rub")))
        probability = max(ZERO, _decimal(row.get("shortage_risk_probability")))
        economic_priority = probability * margin / cost if cost > ZERO and margin > ZERO else ZERO
        by_date[_clean(row.get("decision_date"))].append(
            {
                "opportunity_id": opportunity_id,
                "decision_date": _clean(row.get("decision_date")),
                "period": _clean(row.get("period")),
                "nomenclature_code": _clean(row.get("nomenclature_code")),
                "extra_qty": extra_qty,
                "inventory_cost_per_unit_rub": cost,
                "gross_margin_per_unit_rub": margin,
                "shortage_risk_probability": probability,
                "economic_priority": economic_priority,
                "has_unit_economics": int(cost > ZERO and margin > ZERO),
                "baseline_buffer_qty": baseline.get(opportunity_id, ZERO),
                "desired_buffer_qty": desired.get(opportunity_id, ZERO),
            }
        )
    buffers = dict(baseline)
    allocation: list[dict[str, Any]] = []
    for decision_date in sorted(by_date):
        candidates = sorted(
            by_date[decision_date],
            key=lambda row: (
                -int(row.get("has_unit_economics") or 0),
                -_decimal(row.get("economic_priority")),
                -_decimal(row.get("shortage_risk_probability")),
                _clean(row.get("opportunity_id")),
            ),
        )
        total_extra = sum((_decimal(row.get("extra_qty")) for row in candidates), ZERO)
        remaining = _ceil(total_extra * incremental_share)
        for source in candidates:
            row = dict(source)
            allocated = min(_decimal(row.get("extra_qty")), remaining)
            remaining -= allocated
            opportunity_id = _clean(row.get("opportunity_id"))
            if allocated > ZERO:
                buffers[opportunity_id] = baseline.get(opportunity_id, ZERO) + allocated
            row["incremental_share"] = str(incremental_share)
            row["allocated_extra_qty"] = str(allocated)
            row["selected"] = int(allocated > ZERO)
            allocation.append(row)
    return buffers, allocation


def compare_economic_strategies(
    evaluation: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    baseline_by_period = {
        _clean(row.get("period")): row
        for row in evaluation
        if _clean(row.get("strategy")) == "baseline_service"
    }
    output: list[dict[str, Any]] = []
    for source in evaluation:
        strategy = _clean(source.get("strategy"))
        if strategy == "baseline_service":
            continue
        period = _clean(source.get("period"))
        baseline = baseline_by_period[period]
        covered_delta = _decimal(source.get("covered_loss_qty")) - _decimal(
            baseline.get("covered_loss_qty")
        )
        excess_delta = _decimal(source.get("avoidable_excess_qty")) - _decimal(
            baseline.get("avoidable_excess_qty")
        )
        margin_delta = _decimal(source.get("covered_margin_proxy_rub")) - _decimal(
            baseline.get("covered_margin_proxy_rub")
        )
        excess_value_delta = _decimal(source.get("avoidable_excess_value_proxy_rub")) - _decimal(
            baseline.get("avoidable_excess_value_proxy_rub")
        )
        output.append(
            {
                "period": period,
                "strategy": strategy,
                "incremental_share": strategy.removeprefix("economic_extra_"),
                "covered_loss_delta_qty": str(covered_delta),
                "avoidable_excess_delta_qty": str(excess_delta),
                "covered_margin_delta_proxy_rub": str(margin_delta),
                "avoidable_excess_value_delta_proxy_rub": str(excess_value_delta),
                "incremental_covered_per_excess": str(
                    covered_delta / excess_delta
                    if excess_delta > ZERO
                    else Decimal("999999") if covered_delta > ZERO else ZERO
                ),
                "incremental_margin_per_excess_value": str(
                    margin_delta / excess_value_delta
                    if excess_value_delta > ZERO
                    else Decimal("999999") if margin_delta > ZERO else ZERO
                ),
            }
        )
    return output


def select_economic_strategy(
    comparison: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """Select on the pre-July period only; July remains an untouched holdout."""

    candidates = [
        row
        for row in comparison
        if _clean(row.get("period")) == "pre_final_month"
        and _decimal(row.get("covered_loss_delta_qty")) > ZERO
        and _decimal(row.get("covered_margin_delta_proxy_rub")) > ZERO
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (
            _decimal(row.get("incremental_margin_per_excess_value")),
            _decimal(row.get("covered_margin_delta_proxy_rub")),
            -_decimal(row.get("incremental_share")),
        ),
    )


def evaluate_buffer_strategy(
    pairs: Sequence[Mapping[str, Any]],
    opportunities: Mapping[str, Mapping[str, Any]],
    buffers: Mapping[str, Decimal],
    *,
    strategy: str,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Decimal]]] = defaultdict(list)
    for pair in pairs:
        period = _clean(pair.get("period"))
        case = opportunities[_clean(pair.get("case_opportunity_id"))]
        control = opportunities[_clean(pair.get("control_opportunity_id"))]
        loss = max(ZERO, _decimal(pair.get("case_loss_proxy_qty")))
        case_buffer = buffers.get(_clean(case.get("opportunity_id")), ZERO)
        control_buffer = buffers.get(_clean(control.get("opportunity_id")), ZERO)
        covered = min(loss, case_buffer)
        avoidable_case = max(ZERO, case_buffer - loss)
        excess = control_buffer + avoidable_case
        covered_margin = covered * max(ZERO, _decimal(case.get("gross_margin_per_unit_rub")))
        excess_value = control_buffer * max(
            ZERO, _decimal(control.get("inventory_cost_per_unit_rub"))
        ) + avoidable_case * max(ZERO, _decimal(case.get("inventory_cost_per_unit_rub")))
        groups[period].append(
            {
                "loss": loss,
                "covered": covered,
                "case_buffer": case_buffer,
                "control_buffer": control_buffer,
                "excess": excess,
                "covered_margin": covered_margin,
                "excess_value": excess_value,
            }
        )
    output: list[dict[str, Any]] = []
    for period, values in sorted(groups.items()):
        loss = sum((row["loss"] for row in values), ZERO)
        covered = sum((row["covered"] for row in values), ZERO)
        excess = sum((row["excess"] for row in values), ZERO)
        covered_margin = sum((row["covered_margin"] for row in values), ZERO)
        excess_value = sum((row["excess_value"] for row in values), ZERO)
        output.append(
            {
                "period": period,
                "strategy": strategy,
                "matched_pair_count": len(values),
                "case_loss_proxy_qty": str(loss),
                "covered_loss_qty": str(covered),
                "coverage_share": str(covered / loss if loss > ZERO else ZERO),
                "total_buffer_qty": str(
                    sum(
                        (row["case_buffer"] + row["control_buffer"] for row in values),
                        ZERO,
                    )
                ),
                "avoidable_excess_qty": str(excess),
                "covered_per_excess": str(covered / excess if excess > ZERO else ZERO),
                "covered_margin_proxy_rub": str(covered_margin),
                "avoidable_excess_value_proxy_rub": str(excess_value),
                "covered_margin_per_excess_value": str(
                    covered_margin / excess_value if excess_value > ZERO else ZERO
                ),
            }
        )
    return output


def evaluate_profiles(
    rows: Sequence[Mapping[str, Any]],
    pairs: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    opportunities = {_clean(row.get("opportunity_id")): row for row in rows}
    buffers_by_profile = {profile.name: top_share_buffers(rows, profile) for profile in PROFILES}
    groups: dict[tuple[str, str], list[dict[str, Decimal]]] = defaultdict(list)
    pair_rows: list[dict[str, Any]] = []
    for pair in pairs:
        period = _clean(pair.get("period"))
        case = opportunities[_clean(pair.get("case_opportunity_id"))]
        control = opportunities[_clean(pair.get("control_opportunity_id"))]
        loss = max(ZERO, _decimal(pair.get("case_loss_proxy_qty")))
        pair_row: dict[str, Any] = {
            "pair_id": _clean(pair.get("pair_id")),
            "period": period,
            "case_opportunity_id": _clean(case.get("opportunity_id")),
            "control_opportunity_id": _clean(control.get("opportunity_id")),
            "case_loss_proxy_qty": str(loss),
            "case_known_age_share": _clean(case.get("lot_known_age_share")),
            "control_known_age_share": _clean(control.get("lot_known_age_share")),
            "case_over_p75_share": _clean(case.get("lot_over_p75_share")),
            "control_over_p75_share": _clean(control.get("lot_over_p75_share")),
        }
        for profile in PROFILES:
            name = profile.name
            case_score = _decimal(case.get(f"{name}_score"))
            control_score = _decimal(control.get(f"{name}_score"))
            case_buffer = buffers_by_profile[name].get(_clean(case.get("opportunity_id")), ZERO)
            control_buffer = buffers_by_profile[name].get(
                _clean(control.get("opportunity_id")), ZERO
            )
            covered = min(loss, case_buffer)
            excess = control_buffer + max(ZERO, case_buffer - loss)
            groups[(period, name)].append(
                {
                    "loss": loss,
                    "detected_loss": loss if case_score > control_score else ZERO,
                    "scored_loss": loss if case_score > ZERO or control_score > ZERO else ZERO,
                    "case_higher": ONE if case_score > control_score else ZERO,
                    "control_higher": ONE if control_score > case_score else ZERO,
                    "tie": ONE if case_score == control_score else ZERO,
                    "covered": covered,
                    "case_buffer": case_buffer,
                    "control_buffer": control_buffer,
                    "excess": excess,
                }
            )
            pair_row[f"{name}_case_score"] = str(case_score)
            pair_row[f"{name}_control_score"] = str(control_score)
            pair_row[f"{name}_case_higher"] = int(case_score > control_score)
            pair_row[f"{name}_control_higher"] = int(control_score > case_score)
            pair_row[f"{name}_case_buffer_qty"] = str(case_buffer)
            pair_row[f"{name}_control_buffer_qty"] = str(control_buffer)
        pair_rows.append(pair_row)

    evaluation: list[dict[str, Any]] = []
    for (period, profile), values in sorted(groups.items()):
        loss = sum((row["loss"] for row in values), ZERO)
        detected = sum((row["detected_loss"] for row in values), ZERO)
        scored_loss = sum((row["scored_loss"] for row in values), ZERO)
        covered = sum((row["covered"] for row in values), ZERO)
        excess = sum((row["excess"] for row in values), ZERO)
        total_buffer = sum((row["case_buffer"] + row["control_buffer"] for row in values), ZERO)
        evaluation.append(
            {
                "period": period,
                "profile": profile,
                "matched_pair_count": len(values),
                "case_higher_count": int(sum(row["case_higher"] for row in values)),
                "control_higher_count": int(sum(row["control_higher"] for row in values)),
                "tie_count": int(sum(row["tie"] for row in values)),
                "case_loss_proxy_qty": str(loss),
                "detected_loss_qty": str(detected),
                "detected_loss_share": str(detected / loss if loss > ZERO else ZERO),
                "detected_loss_share_scored": str(
                    detected / scored_loss if scored_loss > ZERO else ZERO
                ),
                "top50_covered_loss_qty": str(covered),
                "top50_coverage_share": str(covered / loss if loss > ZERO else ZERO),
                "top50_total_buffer_qty": str(total_buffer),
                "top50_avoidable_excess_qty": str(excess),
                "top50_covered_per_excess": str(covered / excess if excess > ZERO else ZERO),
            }
        )
    return evaluation, pair_rows


def coverage_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[_clean(row.get("period"))].append(row)
        groups["all"].append(row)
    output: list[dict[str, Any]] = []
    for period, group in sorted(groups.items()):
        pipeline = [row for row in group if _decimal(row.get("as_of_free_incoming_qty")) > ZERO]
        known = [row for row in pipeline if _decimal(row.get("lot_known_age_pipeline_qty")) > ZERO]
        over_p75 = [row for row in known if _decimal(row.get("lot_over_p75_pipeline_qty")) > ZERO]
        pipeline_qty = sum((_decimal(row.get("as_of_free_incoming_qty")) for row in pipeline), ZERO)
        known_qty = sum(
            (
                min(
                    _decimal(row.get("as_of_free_incoming_qty")),
                    _decimal(row.get("lot_known_age_pipeline_qty")),
                )
                for row in pipeline
            ),
            ZERO,
        )
        output.append(
            {
                "period": period,
                "opportunity_count": len(group),
                "pipeline_opportunity_count": len(pipeline),
                "known_age_opportunity_count": len(known),
                "over_p75_opportunity_count": len(over_p75),
                "free_incoming_qty": str(pipeline_qty),
                "known_age_free_incoming_proxy_qty": str(known_qty),
                "known_age_quantity_coverage": str(
                    known_qty / pipeline_qty if pipeline_qty > ZERO else ZERO
                ),
            }
        )
    return output


def manual_review_rows(
    pair_rows: Sequence[Mapping[str, Any]],
    opportunities: Mapping[str, Mapping[str, Any]],
    *,
    challenger: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in pair_rows:
        baseline_case = int(row.get("baseline_v19_case_higher") or 0)
        challenger_case = int(row.get(f"{challenger}_case_higher") or 0)
        challenger_control = int(row.get(f"{challenger}_control_higher") or 0)
        if not baseline_case and challenger_case:
            groups["new_hit"].append(row)
        if baseline_case and not challenger_case:
            groups["lost_hit"].append(row)
        if challenger_control:
            groups["false_alarm"].append(row)
        case = opportunities[_clean(row.get("case_opportunity_id"))]
        if (
            _decimal(case.get("as_of_free_incoming_qty")) > ZERO
            and _decimal(case.get("lot_known_age_pipeline_qty")) <= ZERO
        ):
            groups["left_censored_pipeline"].append(row)
    output: list[dict[str, Any]] = []
    checks = {
        "new_hit": "Проверить дату создания и частичные приходы партии; исключить разовую оптовую продажу.",
        "lost_hit": "Проверить, не замаскировал ли контрольный SKU большой возраст партии.",
        "false_alarm": "Проверить фактический приход и почему риск не превратился в потерю.",
        "left_censored_pipeline": "Нужна исходная дата заказа: opening-партии нельзя надёжно состарить.",
    }
    for review_type, rows in sorted(groups.items()):
        ranked = sorted(rows, key=lambda row: -_decimal(row.get("case_loss_proxy_qty")))
        for row in ranked[:limit]:
            case = opportunities[_clean(row.get("case_opportunity_id"))]
            output.append(
                {
                    "review_type": review_type,
                    "period": _clean(row.get("period")),
                    "pair_id": _clean(row.get("pair_id")),
                    "nomenclature_code": _clean(case.get("nomenclature_code")),
                    "name": _clean(case.get("name")),
                    "decision_date": _clean(case.get("decision_date")),
                    "case_loss_proxy_qty": _clean(row.get("case_loss_proxy_qty")),
                    "free_incoming_qty": _clean(case.get("as_of_free_incoming_qty")),
                    "known_age_share": _clean(case.get("lot_known_age_share")),
                    "over_p75_share": _clean(case.get("lot_over_p75_share")),
                    "baseline_score": _clean(row.get("baseline_v19_case_score")),
                    "challenger_score": _clean(row.get(f"{challenger}_case_score")),
                    "human_check": checks[review_type],
                }
            )
    return output


def _index_evaluation(
    rows: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], Mapping[str, Any]]:
    return {(_clean(row.get("period")), _clean(row.get("profile"))): row for row in rows}


def _pct(value: Any) -> str:
    return f"{float(_decimal(value)):.2%}".replace(".", ",")


def _num(value: Any) -> str:
    return f"{float(_decimal(value)):,.1f}".replace(",", " ").replace(".", ",")


def _markdown(summary: Mapping[str, Any]) -> str:
    final = summary["final_month_comparison"]
    coverage = summary["lot_age_coverage"]
    best = summary["screening"]
    economic = summary["economic_screening"]
    selected_economic = economic.get("selected_on_pre_july")
    holdout = economic.get("july_holdout")
    economic_lines = (
        [
            f"На данных до июля выбран вариант `{selected_economic['strategy']}`: "
            f"добавочное покрытие — {_num(selected_economic['covered_loss_delta_qty'])} ед., "
            f"добавочный excess — {_num(selected_economic['avoidable_excess_delta_qty'])} ед.",
            f"На не использовавшемся для выбора июле он дал ещё "
            f"{_num(holdout['covered_loss_delta_qty'])} ед. proxy-покрытия при "
            f"{_num(holdout['avoidable_excess_delta_qty'])} ед. добавочного excess.",
        ]
        if selected_economic and holdout
        else ["Экономический отбор не нашёл положительной добавки на периоде до июля."]
    )
    return "\n".join(
        [
            "# Быстрый экономический screening pipeline v22",
            "",
            "## Что удалось восстановить",
            "",
            f"Возраст известен для {_pct(coverage['known_age_quantity_coverage'])} свободного "
            "pipeline в frozen-окнах. Остальные opening-партии оставлены без возраста и "
            "не дисконтируются догадкой.",
            "",
            "## Результат на окнах с июлем",
            "",
            f"Базовый v19 распознаёт {_pct(final['baseline_detected_loss_share'])} matched-потери. "
            f"Сбалансированный lot-age профиль `{best['selected_profile']}` распознаёт "
            f"{_pct(best['selected_detected_loss_share'])}; изменение — "
            f"{_pct(best['detected_loss_share_delta'])}.",
            "",
            "## Решение screening",
            "",
            best["recommendation"],
            "",
            "## Экономический отбор только добавочных единиц",
            "",
            *economic_lines,
            "Базовые буферы v19 при этом не уменьшаются. Ранжирование использует только "
            "доступные на дату решения вероятность дефицита, маржу и стоимость; строки без "
            "себестоимости идут последними, но не удаляются.",
            "",
            "## Ограничение",
            "",
            "Это восстановленные FIFO-псевдопартии из изменения агрегатного остатка заказов. "
            "Они показывают дату появления и частичные закрытия, но не первоначальную обещанную "
            "дату, историю переносов и причину отмены. Screening также не доказывает, что общий "
            "складской капитал не вырастет: это обязан проверить строгий полный backtest по "
            "капиталу и GMROI. Production и полный backtest не запускались.",
            "",
        ]
    )


def build_analysis(
    *,
    risk_analysis_dir: Path,
    preflight_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    risk_manifest = _validate_risk_directory(risk_analysis_dir)
    preflight_manifest = validate_preflight_directory(preflight_dir)
    risk_rows = _read_csv(risk_analysis_dir / "risk-scores.csv")
    pairs = _read_csv(risk_analysis_dir / "matched-risk-pairs.csv")
    reconstructed, quality = reconstruct_lot_age_features(
        risk_rows,
        _daily_rows(preflight_dir / "daily-facts.csv"),
    )
    scored = attach_profile_scores(reconstructed)
    evaluation, pair_rows = evaluate_profiles(scored, pairs)
    coverage = coverage_rows(scored)
    opportunity_index = {_clean(row.get("opportunity_id")): row for row in scored}
    evaluation_index = _index_evaluation(evaluation)
    final_baseline = evaluation_index[("final_month_exposed", "baseline_v19")]
    pre_baseline = evaluation_index[("pre_final_month", "baseline_v19")]
    final_challengers = [
        row
        for row in evaluation
        if _clean(row.get("period")) == "final_month_exposed"
        and _clean(row.get("profile")) != "baseline_v19"
    ]
    maximum_detection = max(
        final_challengers,
        key=lambda row: (
            _decimal(row.get("detected_loss_share")),
            _decimal(row.get("top50_covered_per_excess")),
            -_decimal(row.get("top50_avoidable_excess_qty")),
        ),
    )
    screening_comparison: list[dict[str, Any]] = []
    for final_row in final_challengers:
        profile = _clean(final_row.get("profile"))
        pre_row = evaluation_index[("pre_final_month", profile)]
        final_covered_delta = _decimal(final_row.get("top50_covered_loss_qty")) - _decimal(
            final_baseline.get("top50_covered_loss_qty")
        )
        final_excess_delta = _decimal(final_row.get("top50_avoidable_excess_qty")) - _decimal(
            final_baseline.get("top50_avoidable_excess_qty")
        )
        incremental_efficiency = (
            final_covered_delta / final_excess_delta
            if final_excess_delta > ZERO
            else Decimal("999999") if final_covered_delta > ZERO else ZERO
        )
        final_detection_delta = _decimal(final_row.get("detected_loss_share")) - _decimal(
            final_baseline.get("detected_loss_share")
        )
        pre_detection_delta = _decimal(pre_row.get("detected_loss_share")) - _decimal(
            pre_baseline.get("detected_loss_share")
        )
        pre_covered_delta = _decimal(pre_row.get("top50_covered_loss_qty")) - _decimal(
            pre_baseline.get("top50_covered_loss_qty")
        )
        pre_excess_delta = _decimal(pre_row.get("top50_avoidable_excess_qty")) - _decimal(
            pre_baseline.get("top50_avoidable_excess_qty")
        )
        comparison = {
            "profile": profile,
            "final_detected_loss_share_delta": str(final_detection_delta),
            "final_top50_covered_loss_delta_qty": str(final_covered_delta),
            "final_top50_avoidable_excess_delta_qty": str(final_excess_delta),
            "final_incremental_covered_per_excess": str(incremental_efficiency),
            "pre_detected_loss_share_delta": str(pre_detection_delta),
            "pre_top50_covered_loss_delta_qty": str(pre_covered_delta),
            "pre_top50_avoidable_excess_delta_qty": str(pre_excess_delta),
            "passes_stability_gate": int(
                final_detection_delta >= ZERO
                and pre_detection_delta >= ZERO
                and final_covered_delta > ZERO
            ),
        }
        screening_comparison.append(comparison)
    selected = evaluation_index[("final_month_exposed", SELECTED_LOT_PROFILE)]
    selected_comparison = next(
        row for row in screening_comparison if _clean(row.get("profile")) == SELECTED_LOT_PROFILE
    )
    selected_efficiency = _decimal(selected_comparison.get("final_incremental_covered_per_excess"))
    manual_review = manual_review_rows(
        pair_rows,
        opportunity_index,
        challenger=_clean(selected.get("profile")),
    )
    baseline_detection = _decimal(final_baseline.get("detected_loss_share"))
    selected_detection = _decimal(selected.get("detected_loss_share"))
    dominates = (
        _decimal(selected.get("top50_coverage_share"))
        >= _decimal(final_baseline.get("top50_coverage_share"))
        and _decimal(selected.get("top50_avoidable_excess_qty"))
        <= _decimal(final_baseline.get("top50_avoidable_excess_qty"))
        and selected_detection >= baseline_detection
        and (
            _decimal(selected.get("top50_coverage_share"))
            > _decimal(final_baseline.get("top50_coverage_share"))
            or _decimal(selected.get("top50_avoidable_excess_qty"))
            < _decimal(final_baseline.get("top50_avoidable_excess_qty"))
            or selected_detection > baseline_detection
        )
    )
    final_coverage = next(row for row in coverage if row["period"] == "final_month_exposed")
    selected_profile = next(profile for profile in PROFILES if profile.name == SELECTED_LOT_PROFILE)
    baseline_profile = next(profile for profile in PROFILES if profile.name == "baseline_v19")
    economic_evaluation = evaluate_buffer_strategy(
        pairs,
        opportunity_index,
        top_share_buffers(scored, baseline_profile),
        strategy="baseline_service",
    )
    economic_allocation: list[dict[str, Any]] = []
    for share in ECONOMIC_INCREMENTAL_SHARES:
        buffers, allocation = economic_incremental_buffers(
            scored,
            challenger=selected_profile,
            incremental_share=share,
        )
        strategy = f"economic_extra_{share}"
        for row in allocation:
            row["strategy"] = strategy
        economic_allocation.extend(allocation)
        economic_evaluation.extend(
            evaluate_buffer_strategy(
                pairs,
                opportunity_index,
                buffers,
                strategy=strategy,
            )
        )
    economic_comparison = compare_economic_strategies(economic_evaluation)
    selected_economic = select_economic_strategy(economic_comparison)
    selected_economic_strategy = (
        _clean(selected_economic.get("strategy")) if selected_economic else ""
    )
    july_holdout = next(
        (
            row
            for row in economic_comparison
            if _clean(row.get("period")) == "final_month_exposed"
            and _clean(row.get("strategy")) == selected_economic_strategy
        ),
        None,
    )
    economic_screening_passed = bool(
        selected_economic
        and july_holdout
        and _decimal(july_holdout.get("covered_loss_delta_qty")) > ZERO
        and _decimal(july_holdout.get("incremental_covered_per_excess")) > ONE
    )
    if economic_screening_passed:
        recommendation = (
            f"Экономический screening добавочных единиц пройден профилем "
            f"`{selected_economic_strategy}`; его можно передать в полный frozen-backtest. "
            "Это ещё не разрешение production."
        )
    elif dominates:
        recommendation = (
            "Lot-age challenger доминирует базовый screening; его можно передать в полный "
            "frozen-backtest."
        )
    else:
        recommendation = (
            "Экономический screening не подтвердил устойчивую добавочную защиту; полный "
            "backtest пока не запускается."
        )
    allocation_groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in economic_allocation:
        allocation_groups[(_clean(row.get("strategy")), _clean(row.get("period")))].append(row)
    allocation_summary = []
    for (strategy, period), rows in sorted(allocation_groups.items()):
        allocation_summary.append(
            {
                "strategy": strategy,
                "period": period,
                "candidate_count": len(rows),
                "selected_count": sum(int(row.get("selected") or 0) for row in rows),
                "candidate_extra_qty": str(
                    sum((_decimal(row.get("extra_qty")) for row in rows), ZERO)
                ),
                "allocated_extra_qty": str(
                    sum((_decimal(row.get("allocated_extra_qty")) for row in rows), ZERO)
                ),
                "unknown_economics_candidate_qty": str(
                    sum(
                        (
                            _decimal(row.get("extra_qty"))
                            for row in rows
                            if not int(row.get("has_unit_economics") or 0)
                        ),
                        ZERO,
                    )
                ),
                "unknown_economics_allocated_qty": str(
                    sum(
                        (
                            _decimal(row.get("allocated_extra_qty"))
                            for row in rows
                            if not int(row.get("has_unit_economics") or 0)
                        ),
                        ZERO,
                    )
                ),
            }
        )
    summary: dict[str, Any] = {
        "schema": "display_auto_order_pipeline_lot_reliability_analysis.v2",
        "diagnostic_only": True,
        "production_authorized": False,
        "date_from": preflight_manifest.get("date_from"),
        "date_to": preflight_manifest.get("date_to"),
        "method": {
            "lot_reconstruction": "FIFO pseudo-lots from daily gross incoming balance increases and decreases; opening balance remains left-censored",
            "p75_guard": "known-age quantity older than current P75 is excluded; younger and unknown-age quantity is counted fully",
            "quantile_mass": "known-age quantity through P50 counts fully, P50-P75 at 0.50, over P75 at 0.25; opening quantity counts fully",
            "acceleration": "positive 30-day daily sales rate minus frozen forecast rate, multiplied by the outcome horizon and capped by frozen forecast demand; evaluated for all SKU and only with known lots older than P50/P75",
            "top50": "same 50% decision-date ranking and rounded expected quantity as v19",
            "promotion": "challenger must weakly improve July detection and top50 coverage while not increasing avoidable excess, with at least one strict improvement",
            "economic_increment": "baseline v19 service buffers are immutable; only positive challenger-minus-baseline units are ranked by shortage probability times gross margin divided by inventory cost",
            "economic_selection": "25/50/75/100 percent shares are evaluated; the share is selected only on pre-final-month matched outcomes and July is kept as holdout",
        },
        "quality": quality,
        "lot_age_coverage": final_coverage,
        "final_month_comparison": {
            "baseline_detected_loss_share": final_baseline["detected_loss_share"],
            "baseline_top50_coverage_share": final_baseline["top50_coverage_share"],
            "baseline_top50_avoidable_excess_qty": final_baseline["top50_avoidable_excess_qty"],
        },
        "screening": {
            "selected_profile": selected["profile"],
            "selected_detected_loss_share": selected["detected_loss_share"],
            "detected_loss_share_delta": str(selected_detection - baseline_detection),
            "selected_top50_coverage_share": selected["top50_coverage_share"],
            "selected_top50_avoidable_excess_qty": selected["top50_avoidable_excess_qty"],
            "selected_incremental_covered_per_excess": str(selected_efficiency),
            "maximum_detection_profile": maximum_detection["profile"],
            "maximum_detected_loss_share": maximum_detection["detected_loss_share"],
            "dominates_baseline": dominates,
            "recommendation": recommendation,
        },
        "economic_screening": {
            "challenger_profile": SELECTED_LOT_PROFILE,
            "baseline_service_preserved": True,
            "incremental_shares": [str(value) for value in ECONOMIC_INCREMENTAL_SHARES],
            "selection_period": "pre_final_month",
            "holdout_period": "final_month_exposed",
            "passed_for_full_backtest": economic_screening_passed,
            "selected_on_pre_july": dict(selected_economic) if selected_economic else None,
            "july_holdout": dict(july_holdout) if july_holdout else None,
            "comparison": economic_comparison,
            "allocation_summary": allocation_summary,
        },
        "screening_comparison": screening_comparison,
        "profile_evaluation": evaluation,
        "source_checksums": {
            "risk_analysis_manifest": _sha256(risk_analysis_dir / "analysis-manifest.json"),
            "preflight_manifest": _sha256(preflight_dir / "run-manifest.json"),
            "risk_manifest_schema": risk_manifest.get("schema"),
        },
    }
    compact_rows = [
        {
            key: value
            for key, value in row.items()
            if key
            in {
                "opportunity_id",
                "nomenclature_code",
                "name",
                "decision_date",
                "period",
                "forecast_demand_qty",
                "as_of_free_stock_qty",
                "as_of_free_incoming_qty",
                "as_of_sales_30",
                "forecast_rate_sales",
                "lot_total_pipeline_qty",
                "lot_unknown_age_pipeline_qty",
                "lot_known_age_pipeline_qty",
                "lot_within_p50_pipeline_qty",
                "lot_p50_to_p75_pipeline_qty",
                "lot_over_p75_pipeline_qty",
                "lot_known_age_share",
                "lot_over_p50_share",
                "lot_over_p75_share",
                "lot_mean_known_age_days",
                "lot_oldest_known_age_days",
                "lot_p50_days",
                "lot_p75_days",
            }
            or key.endswith("_score")
            or key.endswith("_lot_shortage_qty")
            or key.endswith("_acceleration_addon_qty")
        }
        for row in scored
    ]
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "lot-state-features.csv", compact_rows)
    _write_csv(output_dir / "profile-evaluation.csv", evaluation)
    _write_csv(output_dir / "screening-comparison.csv", screening_comparison)
    _write_csv(output_dir / "pair-comparison.csv", pair_rows)
    _write_csv(output_dir / "coverage-summary.csv", coverage)
    _write_csv(output_dir / "manual-review.csv", manual_review)
    _write_csv(output_dir / "economic-evaluation.csv", economic_evaluation)
    _write_csv(output_dir / "economic-comparison.csv", economic_comparison)
    _write_csv(output_dir / "economic-extra-allocation.csv", economic_allocation)
    (output_dir / "analysis-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "PIPELINE-LOT-RELIABILITY.md").write_text(
        _markdown(summary),
        encoding="utf-8",
    )
    artifacts = (
        "analysis-summary.json",
        "lot-state-features.csv",
        "profile-evaluation.csv",
        "screening-comparison.csv",
        "pair-comparison.csv",
        "coverage-summary.csv",
        "manual-review.csv",
        "economic-evaluation.csv",
        "economic-comparison.csv",
        "economic-extra-allocation.csv",
        "PIPELINE-LOT-RELIABILITY.md",
    )
    manifest = {
        "schema": "display_auto_order_pipeline_lot_reliability_analysis_manifest.v2",
        "diagnostic_only": True,
        "production_authorized": False,
        "files": {name: _sha256(output_dir / name) for name in artifacts},
    }
    (output_dir / "analysis-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk-analysis-dir", type=Path, required=True)
    parser.add_argument("--preflight-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    summary = build_analysis(
        risk_analysis_dir=args.risk_analysis_dir,
        preflight_dir=args.preflight_dir,
        output_dir=args.output_dir,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
