"""Compare P75 loss windows with matched no-loss controls.

The task reads only frozen preflight and diagnostic artifacts.  It builds
scheduled-review decision windows, performs deterministic one-to-one matching,
and evaluates rolling cross-SKU P60/P75/P90 underforecast buffers without
look-ahead.  It never creates purchase orders or queries production systems.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Mapping, Sequence

from app.services.display_auto_order_demand_pattern import (
    classify_demand_pattern,
    completed_weekly_demand,
)
from tasks.analyze_display_auto_order_quick_backtest import _prepare_inputs

ZERO = Decimal("0")
ONE = Decimal("1")
PERCENTILES = (Decimal("0.60"), Decimal("0.75"), Decimal("0.90"))
STAGE_GROW = "sale"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(_clean(value) or "0")
    except (ArithmeticError, ValueError):
        return ZERO


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sum_dates(
    values: Mapping[date, Decimal],
    start_exclusive: date,
    end_inclusive: date,
) -> Decimal:
    return sum(
        (
            max(ZERO, _decimal(quantity))
            for business_date, quantity in values.items()
            if start_exclusive < business_date <= end_inclusive
        ),
        ZERO,
    )


def _lead_band(days: int) -> str:
    if days <= 30:
        return "<=30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return ">90"


def _rate_band(rate: Decimal) -> str:
    if rate <= ZERO:
        return "0"
    if rate <= Decimal("0.10"):
        return "(0;0.10]"
    if rate <= Decimal("0.50"):
        return "(0.10;0.50]"
    if rate <= Decimal("2"):
        return "(0.50;2]"
    return ">2"


def _cost_band(cost: Decimal) -> str:
    if cost <= ZERO:
        return "unknown"
    if cost < Decimal("500"):
        return "<500"
    if cost < Decimal("1500"):
        return "500-1499"
    if cost < Decimal("3000"):
        return "1500-2999"
    return ">=3000"


def _outcome_period(outcome_end: date, *, date_to: date) -> str:
    final_month_start = date(date_to.year, date_to.month, 1)
    return "final_month_exposed" if outcome_end >= final_month_start else "pre_final_month"


def nearest_rank(values: Sequence[Decimal], percentile: Decimal) -> Decimal:
    cleaned = sorted(max(ZERO, _decimal(value)) for value in values)
    if not cleaned:
        return ZERO
    rank = int((percentile * Decimal(len(cleaned))).to_integral_value(rounding=ROUND_CEILING))
    return cleaned[max(0, min(rank - 1, len(cleaned) - 1))]


def _median_decimal(values: Iterable[Decimal]) -> Decimal:
    materialized = [value for value in values]
    if not materialized:
        return ZERO
    return Decimal(str(median(materialized)))


def _pattern_by_code(
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    *,
    as_of: date,
) -> dict[str, str]:
    return {
        code: classify_demand_pattern(completed_weekly_demand(sales, as_of=as_of)).name
        for code, sales in sales_by_code.items()
    }


def build_decision_windows(
    *,
    decision_rows_by_date: Mapping[date, Sequence[Mapping[str, Any]]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    loss_by_code: Mapping[str, Mapping[date, Decimal]],
    pattern_by_code: Mapping[str, str],
    date_to: date,
    cadence_days: int = 7,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for decision_date in sorted(decision_rows_by_date):
        for source in decision_rows_by_date[decision_date]:
            if _clean(source.get("scheduled_review")) != "1":
                continue
            if _clean(source.get("status")) != STAGE_GROW:
                continue
            code = _clean(source.get("nomenclature_code"))
            lead_days = max(1, int(_decimal(source.get("lead_time_p50_days") or 52)))
            horizon_days = lead_days + max(1, cadence_days)
            outcome_end = decision_date + timedelta(days=horizon_days)
            if outcome_end > date_to:
                continue
            rate = max(ZERO, _decimal(source.get("forecast_rate_sales")))
            actual = _sum_dates(
                sales_by_code.get(code, {}),
                decision_date,
                outcome_end,
            )
            predicted = rate * Decimal(horizon_days)
            underforecast = max(ZERO, actual - predicted)
            lost = _sum_dates(
                loss_by_code.get(code, {}),
                decision_date,
                outcome_end,
            )
            cost = max(ZERO, _decimal(source.get("inventory_cost_per_unit_rub")))
            output.append(
                {
                    "opportunity_id": f"{code}:{decision_date.isoformat()}",
                    "nomenclature_code": code,
                    "name": _clean(source.get("name")),
                    "decision_date": decision_date.isoformat(),
                    "outcome_end": outcome_end.isoformat(),
                    "horizon_days": horizon_days,
                    "status": STAGE_GROW,
                    "demand_pattern_preperiod": pattern_by_code.get(code, "unknown"),
                    "lead_time_p50_days": lead_days,
                    "lead_time_band": _lead_band(lead_days),
                    "lead_time_confidence": (
                        _clean(source.get("lead_time_confidence")) or "unknown"
                    ),
                    "decision_period": (
                        "final_month"
                        if decision_date.year == date_to.year
                        and decision_date.month == date_to.month
                        else "pre_final_month"
                    ),
                    "period": _outcome_period(outcome_end, date_to=date_to),
                    "forecast_rate_sales": str(rate),
                    "forecast_rate_band": _rate_band(rate),
                    "forecast_demand_qty": str(predicted),
                    "actual_demand_qty": str(actual),
                    "underforecast_error_qty": str(underforecast),
                    "underforecasted": int(underforecast > ZERO),
                    "model_lost_observed_qty_in_horizon": str(lost),
                    "has_model_loss_in_horizon": int(lost > ZERO),
                    "inventory_cost_per_unit_rub": str(cost),
                    "gross_margin_per_unit_rub": str(
                        _decimal(source.get("gross_margin_per_unit_rub"))
                    ),
                    "cost_band": _cost_band(cost),
                }
            )
    output.sort(
        key=lambda row: (
            _clean(row.get("decision_date")),
            _clean(row.get("nomenclature_code")),
        )
    )
    return output


def select_case_anchors(
    opportunities: Sequence[Mapping[str, Any]],
    episodes: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_code: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in opportunities:
        by_code[_clean(row.get("nomenclature_code"))].append(row)
    anchors: dict[str, dict[str, Any]] = {}
    for episode in episodes:
        if _clean(episode.get("status")) != STAGE_GROW:
            continue
        code = _clean(episode.get("nomenclature_code"))
        start = date.fromisoformat(_clean(episode.get("episode_start")))
        candidates = [
            row
            for row in by_code.get(code, ())
            if date.fromisoformat(_clean(row.get("decision_date")))
            < start
            <= date.fromisoformat(_clean(row.get("outcome_end")))
        ]
        if not candidates:
            continue
        anchor = max(candidates, key=lambda row: _clean(row.get("decision_date")))
        anchor_id = _clean(anchor.get("opportunity_id"))
        current = anchors.setdefault(
            anchor_id,
            {
                **dict(anchor),
                "episode_count": 0,
                "episode_lost_observed_qty": ZERO,
                "first_episode_start": _clean(episode.get("episode_start")),
                "last_episode_end": _clean(episode.get("episode_end")),
            },
        )
        current["episode_count"] = int(current["episode_count"]) + 1
        current["episode_lost_observed_qty"] = _decimal(
            current["episode_lost_observed_qty"]
        ) + _decimal(episode.get("lost_observed_qty"))
        current["first_episode_start"] = min(
            _clean(current["first_episode_start"]),
            _clean(episode.get("episode_start")),
        )
        current["last_episode_end"] = max(
            _clean(current["last_episode_end"]),
            _clean(episode.get("episode_end")),
        )
    output: list[dict[str, Any]] = []
    for row in anchors.values():
        rendered = dict(row)
        rendered["episode_lost_observed_qty"] = str(_decimal(rendered["episode_lost_observed_qty"]))
        output.append(rendered)
    output.sort(
        key=lambda row: (
            -_decimal(row.get("episode_lost_observed_qty")),
            _clean(row.get("opportunity_id")),
        )
    )
    return output


def select_control_pool(
    opportunities: Sequence[Mapping[str, Any]],
    *,
    case_ids: set[str],
) -> list[dict[str, Any]]:
    by_sku_month: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in opportunities:
        if _clean(row.get("opportunity_id")) in case_ids:
            continue
        if int(row.get("has_model_loss_in_horizon") or 0):
            continue
        decision_date = _clean(row.get("decision_date"))
        by_sku_month[(_clean(row.get("nomenclature_code")), decision_date[:7])].append(row)
    output = [
        dict(
            min(
                rows,
                key=lambda row: (
                    abs(date.fromisoformat(_clean(row.get("decision_date"))).day - 15),
                    _clean(row.get("decision_date")),
                ),
            )
        )
        for rows in by_sku_month.values()
    ]
    output.sort(key=lambda row: _clean(row.get("opportunity_id")))
    return output


MATCH_LEVELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "exact",
        (
            "demand_pattern_preperiod",
            "lead_time_band",
            "lead_time_confidence",
            "period",
            "forecast_rate_band",
        ),
    ),
    (
        "without_period",
        (
            "demand_pattern_preperiod",
            "lead_time_band",
            "lead_time_confidence",
            "forecast_rate_band",
        ),
    ),
    (
        "without_confidence",
        ("demand_pattern_preperiod", "lead_time_band", "forecast_rate_band"),
    ),
    ("pattern_and_lead", ("demand_pattern_preperiod", "lead_time_band")),
)


def _match_key(row: Mapping[str, Any], fields: Sequence[str]) -> tuple[str, ...]:
    return tuple(_clean(row.get(field)) for field in fields)


def _log_distance(left: Decimal, right: Decimal) -> float:
    return abs(math.log1p(float(max(ZERO, left))) - math.log1p(float(max(ZERO, right))))


def _match_score(case: Mapping[str, Any], control: Mapping[str, Any]) -> float:
    forecast_distance = _log_distance(
        _decimal(case.get("forecast_rate_sales")),
        _decimal(control.get("forecast_rate_sales")),
    )
    cost_distance = _log_distance(
        _decimal(case.get("inventory_cost_per_unit_rub")),
        _decimal(control.get("inventory_cost_per_unit_rub")),
    )
    lead_distance = abs(
        int(case.get("lead_time_p50_days") or 0) - int(control.get("lead_time_p50_days") or 0)
    )
    date_distance = abs(
        (
            date.fromisoformat(_clean(case.get("decision_date")))
            - date.fromisoformat(_clean(control.get("decision_date")))
        ).days
    )
    return (
        forecast_distance * 4
        + cost_distance * 0.25
        + lead_distance / 30
        + date_distance / 365
        + (0 if case.get("cost_band") == control.get("cost_band") else 0.25)
    )


def match_cases_to_controls(
    cases: Sequence[Mapping[str, Any]],
    controls: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    indexes: list[dict[tuple[str, ...], list[Mapping[str, Any]]]] = []
    for _, fields in MATCH_LEVELS:
        index: dict[tuple[str, ...], list[Mapping[str, Any]]] = defaultdict(list)
        for row in controls:
            index[_match_key(row, fields)].append(row)
        indexes.append(index)

    strict_fields = MATCH_LEVELS[0][1]
    ordered_cases = sorted(
        cases,
        key=lambda row: (
            len(indexes[0].get(_match_key(row, strict_fields), ())),
            -_decimal(row.get("episode_lost_observed_qty")),
            _clean(row.get("opportunity_id")),
        ),
    )
    used_controls: set[str] = set()
    pairs: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for case in ordered_cases:
        selected: Mapping[str, Any] | None = None
        selected_level = ""
        selected_score = 0.0
        for index, (level_name, fields) in enumerate(MATCH_LEVELS):
            candidates = [
                row
                for row in indexes[index].get(_match_key(case, fields), ())
                if _clean(row.get("opportunity_id")) not in used_controls
                and _clean(row.get("nomenclature_code")) != _clean(case.get("nomenclature_code"))
            ]
            if not candidates:
                continue
            scored = sorted(
                ((_match_score(case, row), row) for row in candidates),
                key=lambda item: (item[0], _clean(item[1].get("opportunity_id"))),
            )
            if scored[0][0] > 6:
                continue
            selected_score, selected = scored[0]
            selected_level = level_name
            break
        if selected is None:
            unmatched.append(dict(case))
            continue
        control_id = _clean(selected.get("opportunity_id"))
        used_controls.add(control_id)
        pairs.append(
            {
                "pair_id": len(pairs) + 1,
                "match_level": selected_level,
                "match_score": f"{selected_score:.12f}",
                "case_opportunity_id": _clean(case.get("opportunity_id")),
                "case_nomenclature_code": _clean(case.get("nomenclature_code")),
                "case_name": _clean(case.get("name")),
                "case_decision_date": _clean(case.get("decision_date")),
                "case_episode_lost_qty": _clean(case.get("episode_lost_observed_qty")),
                "case_pattern": _clean(case.get("demand_pattern_preperiod")),
                "case_lead_band": _clean(case.get("lead_time_band")),
                "case_confidence": _clean(case.get("lead_time_confidence")),
                "case_period": _clean(case.get("period")),
                "case_forecast_rate": _clean(case.get("forecast_rate_sales")),
                "case_forecast_qty": _clean(case.get("forecast_demand_qty")),
                "case_actual_qty": _clean(case.get("actual_demand_qty")),
                "case_underforecast_error_qty": _clean(case.get("underforecast_error_qty")),
                "case_cost_rub": _clean(case.get("inventory_cost_per_unit_rub")),
                "control_opportunity_id": control_id,
                "control_nomenclature_code": _clean(selected.get("nomenclature_code")),
                "control_name": _clean(selected.get("name")),
                "control_decision_date": _clean(selected.get("decision_date")),
                "control_pattern": _clean(selected.get("demand_pattern_preperiod")),
                "control_lead_band": _clean(selected.get("lead_time_band")),
                "control_confidence": _clean(selected.get("lead_time_confidence")),
                "control_period": _clean(selected.get("period")),
                "control_forecast_rate": _clean(selected.get("forecast_rate_sales")),
                "control_forecast_qty": _clean(selected.get("forecast_demand_qty")),
                "control_actual_qty": _clean(selected.get("actual_demand_qty")),
                "control_underforecast_error_qty": _clean(selected.get("underforecast_error_qty")),
                "control_cost_rub": _clean(selected.get("inventory_cost_per_unit_rub")),
                "forecast_rate_log_distance": str(
                    _log_distance(
                        _decimal(case.get("forecast_rate_sales")),
                        _decimal(selected.get("forecast_rate_sales")),
                    )
                ),
            }
        )
    pairs.sort(key=lambda row: int(row["pair_id"]))
    return pairs, unmatched


CALIBRATION_LEVELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "pattern_lead_confidence_rate",
        (
            "demand_pattern_preperiod",
            "lead_time_band",
            "lead_time_confidence",
            "forecast_rate_band",
        ),
    ),
    (
        "pattern_lead_confidence",
        ("demand_pattern_preperiod", "lead_time_band", "lead_time_confidence"),
    ),
    ("pattern_lead", ("demand_pattern_preperiod", "lead_time_band")),
    ("pattern", ("demand_pattern_preperiod",)),
    ("all", ()),
)


def attach_rolling_buffers(
    opportunities: Sequence[Mapping[str, Any]],
    *,
    min_samples: int,
) -> list[dict[str, Any]]:
    by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    completed: list[tuple[date, dict[str, Any]]] = []
    for source in opportunities:
        row = dict(source)
        decision_date = date.fromisoformat(_clean(row.get("decision_date")))
        outcome_end = date.fromisoformat(_clean(row.get("outcome_end")))
        by_date[decision_date].append(row)
        completed.append((outcome_end, row))
    completed.sort(key=lambda item: (item[0], _clean(item[1].get("opportunity_id"))))

    histories: list[dict[tuple[str, ...], list[Decimal]]] = [
        defaultdict(list) for _ in CALIBRATION_LEVELS
    ]
    completion_index = 0
    output: list[dict[str, Any]] = []
    for decision_date in sorted(by_date):
        while completion_index < len(completed) and completed[completion_index][0] < decision_date:
            _, completed_row = completed[completion_index]
            error = _decimal(completed_row.get("underforecast_error_qty"))
            for level_index, (_, fields) in enumerate(CALIBRATION_LEVELS):
                histories[level_index][_match_key(completed_row, fields)].append(error)
            completion_index += 1
        cache: dict[tuple[int, tuple[str, ...]], tuple[int, dict[str, Decimal]]] = {}
        for source in by_date[decision_date]:
            row = dict(source)
            selected_level = "insufficient"
            selected_count = 0
            selected_quantiles = {f"p{int(p * 100)}": ZERO for p in PERCENTILES}
            for level_index, (level_name, fields) in enumerate(CALIBRATION_LEVELS):
                key = _match_key(row, fields)
                cache_key = (level_index, key)
                cached = cache.get(cache_key)
                if cached is None:
                    samples = histories[level_index].get(key, [])
                    cached = (
                        len(samples),
                        {
                            f"p{int(percentile * 100)}": nearest_rank(samples, percentile)
                            for percentile in PERCENTILES
                        },
                    )
                    cache[cache_key] = cached
                count, quantiles = cached
                if count >= min_samples:
                    selected_level = level_name
                    selected_count = count
                    selected_quantiles = quantiles
                    break
            row["calibration_level"] = selected_level
            row["calibration_sample_count"] = selected_count
            for label, value in selected_quantiles.items():
                row[f"buffer_{label}_qty"] = str(value)
            output.append(row)
    output.sort(key=lambda row: _clean(row.get("opportunity_id")))
    return output


def _standardized_mean_difference(cases: Sequence[Decimal], controls: Sequence[Decimal]) -> Decimal:
    if not cases or not controls:
        return ZERO
    case_floats = [float(value) for value in cases]
    control_floats = [float(value) for value in controls]
    case_mean = mean(case_floats)
    control_mean = mean(control_floats)
    case_variance = sum((value - case_mean) ** 2 for value in case_floats) / max(1, len(cases) - 1)
    control_variance = sum((value - control_mean) ** 2 for value in control_floats) / max(
        1, len(controls) - 1
    )
    pooled = math.sqrt((case_variance + control_variance) / 2)
    if pooled == 0:
        return ZERO
    return Decimal(str((case_mean - control_mean) / pooled))


def matching_summary(pairs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    case_errors = [_decimal(row.get("case_underforecast_error_qty")) for row in pairs]
    control_errors = [_decimal(row.get("control_underforecast_error_qty")) for row in pairs]
    case_rates = [_decimal(row.get("case_forecast_rate")) for row in pairs]
    control_rates = [_decimal(row.get("control_forecast_rate")) for row in pairs]
    case_costs = [_decimal(row.get("case_cost_rub")) for row in pairs]
    control_costs = [_decimal(row.get("control_cost_rub")) for row in pairs]
    levels: dict[str, int] = defaultdict(int)
    for row in pairs:
        levels[_clean(row.get("match_level"))] += 1
    return {
        "matched_pair_count": len(pairs),
        "match_levels": dict(sorted(levels.items())),
        "case_underforecast_frequency": str(
            Decimal(sum(value > ZERO for value in case_errors)) / Decimal(len(case_errors))
            if case_errors
            else ZERO
        ),
        "control_underforecast_frequency": str(
            Decimal(sum(value > ZERO for value in control_errors)) / Decimal(len(control_errors))
            if control_errors
            else ZERO
        ),
        "case_mean_underforecast_qty": str(
            sum(case_errors, ZERO) / Decimal(len(case_errors)) if case_errors else ZERO
        ),
        "control_mean_underforecast_qty": str(
            sum(control_errors, ZERO) / Decimal(len(control_errors)) if control_errors else ZERO
        ),
        "case_median_underforecast_qty": str(_median_decimal(case_errors)),
        "control_median_underforecast_qty": str(_median_decimal(control_errors)),
        "case_p75_underforecast_qty": str(nearest_rank(case_errors, Decimal("0.75"))),
        "control_p75_underforecast_qty": str(nearest_rank(control_errors, Decimal("0.75"))),
        "case_p90_underforecast_qty": str(nearest_rank(case_errors, Decimal("0.90"))),
        "control_p90_underforecast_qty": str(nearest_rank(control_errors, Decimal("0.90"))),
        "forecast_rate_smd": str(_standardized_mean_difference(case_rates, control_rates)),
        "cost_smd": str(_standardized_mean_difference(case_costs, control_costs)),
        "exact_pattern_share": str(
            Decimal(sum(row["case_pattern"] == row["control_pattern"] for row in pairs))
            / Decimal(len(pairs))
            if pairs
            else ZERO
        ),
        "exact_lead_band_share": str(
            Decimal(sum(row["case_lead_band"] == row["control_lead_band"] for row in pairs))
            / Decimal(len(pairs))
            if pairs
            else ZERO
        ),
        "exact_confidence_share": str(
            Decimal(sum(row["case_confidence"] == row["control_confidence"] for row in pairs))
            / Decimal(len(pairs))
            if pairs
            else ZERO
        ),
        "exact_period_share": str(
            Decimal(sum(row["case_period"] == row["control_period"] for row in pairs))
            / Decimal(len(pairs))
            if pairs
            else ZERO
        ),
    }


def evaluate_buffers(
    pairs: Sequence[Mapping[str, Any]],
    opportunities: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for percentile in PERCENTILES:
        label = f"p{int(percentile * 100)}"
        calibrated_pairs = [
            row
            for row in pairs
            if _clean(
                opportunities.get(_clean(row.get("case_opportunity_id")), {}).get(
                    "calibration_level"
                )
            )
            != "insufficient"
            and _clean(
                opportunities.get(_clean(row.get("control_opportunity_id")), {}).get(
                    "calibration_level"
                )
            )
            != "insufficient"
        ]
        for role in ("case", "control"):
            errors: list[Decimal] = []
            buffers: list[Decimal] = []
            costs: list[Decimal] = []
            losses: list[Decimal] = []
            for pair in calibrated_pairs:
                opportunity = opportunities[_clean(pair.get(f"{role}_opportunity_id"))]
                errors.append(_decimal(opportunity.get("underforecast_error_qty")))
                buffers.append(_decimal(opportunity.get(f"buffer_{label}_qty")))
                costs.append(_decimal(opportunity.get("inventory_cost_per_unit_rub")))
                losses.append(
                    _decimal(pair.get("case_episode_lost_qty")) if role == "case" else ZERO
                )
            error_total = sum(errors, ZERO)
            buffer_total = sum(buffers, ZERO)
            protected_error = sum(
                (min(error, buffer) for error, buffer in zip(errors, buffers, strict=True)),
                ZERO,
            )
            excess = sum(
                (max(ZERO, buffer - error) for error, buffer in zip(errors, buffers, strict=True)),
                ZERO,
            )
            buffer_value = sum(
                (buffer * cost for buffer, cost in zip(buffers, costs, strict=True)),
                ZERO,
            )
            loss_proxy = sum(
                (min(buffer, loss) for buffer, loss in zip(buffers, losses, strict=True)),
                ZERO,
            )
            output.append(
                {
                    "profile": label.upper(),
                    "percentile": str(percentile),
                    "cohort": role,
                    "matched_pair_count": len(calibrated_pairs),
                    "calibration_coverage_share": str(
                        Decimal(len(calibrated_pairs)) / Decimal(len(pairs)) if pairs else ZERO
                    ),
                    "mean_buffer_qty": str(
                        buffer_total / Decimal(len(buffers)) if buffers else ZERO
                    ),
                    "median_buffer_qty": str(_median_decimal(buffers)),
                    "error_covered_frequency": str(
                        Decimal(
                            sum(
                                buffer >= error
                                for buffer, error in zip(buffers, errors, strict=True)
                            )
                        )
                        / Decimal(len(errors))
                        if errors
                        else ZERO
                    ),
                    "underforecast_unit_coverage_share": str(
                        protected_error / error_total if error_total > ZERO else ONE
                    ),
                    "underforecast_error_qty": str(error_total),
                    "protected_underforecast_qty": str(protected_error),
                    "excess_buffer_proxy_qty": str(excess),
                    "mean_buffer_value_rub_per_opportunity": str(
                        buffer_value / Decimal(len(buffers)) if buffers else ZERO
                    ),
                    "case_loss_proxy_covered_qty": str(loss_proxy),
                    "case_loss_proxy_coverage_share": str(
                        loss_proxy / sum(losses, ZERO) if sum(losses, ZERO) > ZERO else ZERO
                    ),
                }
            )
    return output


def segment_comparison_rows(pairs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in pairs:
        groups[
            (
                _clean(row.get("case_pattern")),
                _clean(row.get("case_lead_band")),
                _clean(row.get("case_confidence")),
            )
        ].append(row)
    output: list[dict[str, Any]] = []
    for (pattern, lead_band, confidence), rows in groups.items():
        case_errors = [_decimal(row.get("case_underforecast_error_qty")) for row in rows]
        control_errors = [_decimal(row.get("control_underforecast_error_qty")) for row in rows]
        loss = sum((_decimal(row.get("case_episode_lost_qty")) for row in rows), ZERO)
        output.append(
            {
                "segment": f"{pattern} | {lead_band} | {confidence}",
                "demand_pattern_preperiod": pattern,
                "lead_time_band": lead_band,
                "lead_time_confidence": confidence,
                "matched_pair_count": len(rows),
                "case_episode_lost_qty": str(loss),
                "case_mean_underforecast_qty": str(
                    sum(case_errors, ZERO) / Decimal(len(case_errors))
                ),
                "control_mean_underforecast_qty": str(
                    sum(control_errors, ZERO) / Decimal(len(control_errors))
                ),
                "mean_underforecast_gap_qty": str(
                    sum(case_errors, ZERO) / Decimal(len(case_errors))
                    - sum(control_errors, ZERO) / Decimal(len(control_errors))
                ),
                "case_underforecast_frequency": str(
                    Decimal(sum(value > ZERO for value in case_errors)) / Decimal(len(case_errors))
                ),
                "control_underforecast_frequency": str(
                    Decimal(sum(value > ZERO for value in control_errors))
                    / Decimal(len(control_errors))
                ),
            }
        )
    output.sort(
        key=lambda row: (
            -_decimal(row.get("case_episode_lost_qty")),
            _clean(row.get("segment")),
        )
    )
    return output


def _pair_segment(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        _clean(row.get("case_pattern")),
        _clean(row.get("case_lead_band")),
        _clean(row.get("case_confidence")),
    )


def _segment_label(segment: tuple[str, str, str]) -> str:
    return " | ".join(segment)


def _segment_statistics(
    pairs: Sequence[Mapping[str, Any]],
    *,
    period: str | None = None,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in pairs:
        if _clean(row.get("case_period")) != _clean(row.get("control_period")):
            continue
        if period is not None and _clean(row.get("case_period")) != period:
            continue
        grouped[_pair_segment(row)].append(row)

    output: dict[tuple[str, str, str], dict[str, Any]] = {}
    for segment, rows in grouped.items():
        case_errors = [_decimal(row.get("case_underforecast_error_qty")) for row in rows]
        control_errors = [_decimal(row.get("control_underforecast_error_qty")) for row in rows]
        case_mean = sum(case_errors, ZERO) / Decimal(len(rows))
        control_mean = sum(control_errors, ZERO) / Decimal(len(rows))
        case_frequency = Decimal(sum(value > ZERO for value in case_errors)) / Decimal(len(rows))
        control_frequency = Decimal(sum(value > ZERO for value in control_errors)) / Decimal(
            len(rows)
        )
        output[segment] = {
            "matched_pair_count": len(rows),
            "case_episode_lost_qty": sum(
                (_decimal(row.get("case_episode_lost_qty")) for row in rows), ZERO
            ),
            "case_mean_underforecast_qty": case_mean,
            "control_mean_underforecast_qty": control_mean,
            "mean_underforecast_gap_qty": case_mean - control_mean,
            "case_underforecast_frequency": case_frequency,
            "control_underforecast_frequency": control_frequency,
            "underforecast_frequency_gap": case_frequency - control_frequency,
        }
    return output


def segment_stability_rows(
    pairs: Sequence[Mapping[str, Any]],
    *,
    candidate_min_pairs: int = 30,
    candidate_min_gap: Decimal = ONE,
) -> list[dict[str, Any]]:
    pre = _segment_statistics(pairs, period="pre_final_month")
    final = _segment_statistics(pairs, period="final_month_exposed")
    overall = _segment_statistics(pairs)
    output: list[dict[str, Any]] = []
    for segment in sorted(set(pre) | set(final) | set(overall)):
        pre_row = pre.get(segment, {})
        final_row = final.get(segment, {})
        overall_row = overall.get(segment, {})
        pre_count = int(pre_row.get("matched_pair_count") or 0)
        pre_gap = _decimal(pre_row.get("mean_underforecast_gap_qty"))
        pre_frequency_gap = _decimal(pre_row.get("underforecast_frequency_gap"))
        selected = (
            pre_count >= candidate_min_pairs
            and pre_gap >= candidate_min_gap
            and pre_frequency_gap > ZERO
        )
        output.append(
            {
                "segment": _segment_label(segment),
                "demand_pattern_preperiod": segment[0],
                "lead_time_band": segment[1],
                "lead_time_confidence": segment[2],
                "candidate_p90": int(selected),
                "candidate_rule": (
                    f"pre_final_pairs>={candidate_min_pairs};"
                    f"pre_final_mean_gap>={candidate_min_gap};"
                    "pre_final_frequency_gap>0"
                ),
                "overall_matched_pair_count": int(overall_row.get("matched_pair_count") or 0),
                "overall_case_episode_lost_qty": str(
                    _decimal(overall_row.get("case_episode_lost_qty"))
                ),
                "overall_mean_underforecast_gap_qty": str(
                    _decimal(overall_row.get("mean_underforecast_gap_qty"))
                ),
                "pre_final_matched_pair_count": pre_count,
                "pre_final_case_episode_lost_qty": str(
                    _decimal(pre_row.get("case_episode_lost_qty"))
                ),
                "pre_final_mean_underforecast_gap_qty": str(pre_gap),
                "pre_final_underforecast_frequency_gap": str(pre_frequency_gap),
                "final_month_matched_pair_count": int(final_row.get("matched_pair_count") or 0),
                "final_month_case_episode_lost_qty": str(
                    _decimal(final_row.get("case_episode_lost_qty"))
                ),
                "final_month_mean_underforecast_gap_qty": str(
                    _decimal(final_row.get("mean_underforecast_gap_qty"))
                ),
                "final_month_underforecast_frequency_gap": str(
                    _decimal(final_row.get("underforecast_frequency_gap"))
                ),
            }
        )
    output.sort(
        key=lambda row: (
            -int(row["candidate_p90"]),
            -_decimal(row.get("pre_final_case_episode_lost_qty")),
            _clean(row.get("segment")),
        )
    )
    return output


def _calibrated_pairs(
    pairs: Sequence[Mapping[str, Any]],
    opportunities: Mapping[str, Mapping[str, Any]],
    *,
    period: str | None,
) -> list[Mapping[str, Any]]:
    return [
        row
        for row in pairs
        if _clean(row.get("case_period")) == _clean(row.get("control_period"))
        and (period is None or _clean(row.get("case_period")) == period)
        and _clean(
            opportunities.get(_clean(row.get("case_opportunity_id")), {}).get("calibration_level")
        )
        != "insufficient"
        and _clean(
            opportunities.get(_clean(row.get("control_opportunity_id")), {}).get(
                "calibration_level"
            )
        )
        != "insufficient"
    ]


def evaluate_buffer_policy(
    pairs: Sequence[Mapping[str, Any]],
    opportunities: Mapping[str, Mapping[str, Any]],
    *,
    policy: str,
    period: str | None,
    candidate_segments: set[tuple[str, str, str]],
) -> dict[str, Any]:
    calibrated = _calibrated_pairs(pairs, opportunities, period=period)
    case_loss_total = ZERO
    case_loss_covered = ZERO
    case_error_total = ZERO
    case_error_protected = ZERO
    control_excess = ZERO
    control_buffer_value = ZERO
    selected_pair_count = 0
    for pair in calibrated:
        segment = _pair_segment(pair)
        if policy == "p90_all":
            buffer_label = "p90"
        elif policy == "targeted_p90_else_p75" and segment in candidate_segments:
            buffer_label = "p90"
            selected_pair_count += 1
        else:
            buffer_label = "p75"
        case = opportunities[_clean(pair.get("case_opportunity_id"))]
        control = opportunities[_clean(pair.get("control_opportunity_id"))]
        case_buffer = _decimal(case.get(f"buffer_{buffer_label}_qty"))
        control_buffer = _decimal(control.get(f"buffer_{buffer_label}_qty"))
        case_error = _decimal(case.get("underforecast_error_qty"))
        control_error = _decimal(control.get("underforecast_error_qty"))
        loss = _decimal(pair.get("case_episode_lost_qty"))
        case_loss_total += loss
        case_loss_covered += min(case_buffer, loss)
        case_error_total += case_error
        case_error_protected += min(case_buffer, case_error)
        control_excess += max(ZERO, control_buffer - control_error)
        control_buffer_value += control_buffer * _decimal(
            control.get("inventory_cost_per_unit_rub")
        )
    return {
        "policy": policy,
        "period": period or "all",
        "matched_pair_count": len(calibrated),
        "candidate_segment_count": len(candidate_segments),
        "candidate_pair_count": selected_pair_count,
        "case_loss_proxy_qty": str(case_loss_total),
        "case_loss_proxy_covered_qty": str(case_loss_covered),
        "case_loss_proxy_coverage_share": str(
            case_loss_covered / case_loss_total if case_loss_total > ZERO else ZERO
        ),
        "case_underforecast_error_qty": str(case_error_total),
        "case_underforecast_protected_qty": str(case_error_protected),
        "case_underforecast_unit_coverage_share": str(
            case_error_protected / case_error_total if case_error_total > ZERO else ONE
        ),
        "control_excess_buffer_proxy_qty": str(control_excess),
        "control_buffer_value_proxy_rub": str(control_buffer_value),
        "loss_covered_per_control_excess_qty": str(
            case_loss_covered / control_excess if control_excess > ZERO else ZERO
        ),
    }


def buffer_policy_evaluation_rows(
    pairs: Sequence[Mapping[str, Any]],
    opportunities: Mapping[str, Mapping[str, Any]],
    stability_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    candidate_segments = {
        (
            _clean(row.get("demand_pattern_preperiod")),
            _clean(row.get("lead_time_band")),
            _clean(row.get("lead_time_confidence")),
        )
        for row in stability_rows
        if int(row.get("candidate_p90") or 0)
    }
    output: list[dict[str, Any]] = []
    for period in (None, "pre_final_month", "final_month_exposed"):
        period_rows = [
            evaluate_buffer_policy(
                pairs,
                opportunities,
                policy=policy,
                period=period,
                candidate_segments=candidate_segments,
            )
            for policy in ("p75_all", "targeted_p90_else_p75", "p90_all")
        ]
        baseline = period_rows[0]
        for row in period_rows:
            incremental_covered = _decimal(row.get("case_loss_proxy_covered_qty")) - _decimal(
                baseline.get("case_loss_proxy_covered_qty")
            )
            incremental_excess = _decimal(row.get("control_excess_buffer_proxy_qty")) - _decimal(
                baseline.get("control_excess_buffer_proxy_qty")
            )
            row["incremental_loss_proxy_covered_qty_vs_p75"] = str(incremental_covered)
            row["incremental_control_excess_proxy_qty_vs_p75"] = str(incremental_excess)
            row["incremental_covered_per_incremental_excess_qty"] = str(
                incremental_covered / incremental_excess if incremental_excess > ZERO else ZERO
            )
            row["candidate_segments"] = "; ".join(
                _segment_label(segment) for segment in sorted(candidate_segments)
            )
            output.append(row)
    return output


def candidate_sensitivity_rows(
    pairs: Sequence[Mapping[str, Any]],
    opportunities: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    pre = _segment_statistics(pairs, period="pre_final_month")
    baseline = evaluate_buffer_policy(
        pairs,
        opportunities,
        policy="p75_all",
        period="final_month_exposed",
        candidate_segments=set(),
    )
    output: list[dict[str, Any]] = []
    for min_pairs in (20, 30, 50):
        for min_gap in (ZERO, ONE, Decimal("2")):
            selected = {
                segment
                for segment, stats in pre.items()
                if int(stats["matched_pair_count"]) >= min_pairs
                and _decimal(stats["mean_underforecast_gap_qty"]) >= min_gap
                and _decimal(stats["underforecast_frequency_gap"]) > ZERO
            }
            evaluation = evaluate_buffer_policy(
                pairs,
                opportunities,
                policy="targeted_p90_else_p75",
                period="final_month_exposed",
                candidate_segments=selected,
            )
            incremental_covered = _decimal(
                evaluation.get("case_loss_proxy_covered_qty")
            ) - _decimal(baseline.get("case_loss_proxy_covered_qty"))
            incremental_excess = _decimal(
                evaluation.get("control_excess_buffer_proxy_qty")
            ) - _decimal(baseline.get("control_excess_buffer_proxy_qty"))
            output.append(
                {
                    "training_period": "pre_final_month",
                    "evaluation_period": "final_month_exposed",
                    "min_training_pairs": min_pairs,
                    "min_training_mean_gap_qty": str(min_gap),
                    "require_positive_frequency_gap": 1,
                    "candidate_segment_count": len(selected),
                    "candidate_segments": "; ".join(
                        _segment_label(segment) for segment in sorted(selected)
                    ),
                    "evaluation_matched_pair_count": evaluation["matched_pair_count"],
                    "case_loss_proxy_coverage_share": evaluation["case_loss_proxy_coverage_share"],
                    "control_excess_buffer_proxy_qty": evaluation[
                        "control_excess_buffer_proxy_qty"
                    ],
                    "incremental_loss_proxy_covered_qty_vs_p75": str(incremental_covered),
                    "incremental_control_excess_proxy_qty_vs_p75": str(incremental_excess),
                    "incremental_covered_per_incremental_excess_qty": str(
                        incremental_covered / incremental_excess
                        if incremental_excess > ZERO
                        else ZERO
                    ),
                }
            )
    return output


def build_analysis(
    *,
    preflight_dir: Path,
    loss_analysis_dir: Path,
    output_dir: Path,
    min_calibration_samples: int = 30,
) -> dict[str, Any]:
    inputs = _prepare_inputs(preflight_dir)
    loss_summary = json.loads(
        (loss_analysis_dir / "analysis-summary.json").read_text(encoding="utf-8")
    )
    if any(_decimal(value) != ZERO for value in loss_summary["reconciliation"].values()):
        raise ValueError("source P75 loss analysis does not reconcile")
    loss_events = _read_csv(loss_analysis_dir / "lost-sales-events.csv")
    episodes = _read_csv(loss_analysis_dir / "loss-episodes.csv")
    loss_by_code: dict[str, dict[date, Decimal]] = defaultdict(dict)
    for row in loss_events:
        code = _clean(row.get("nomenclature_code"))
        business_date = date.fromisoformat(_clean(row.get("business_date")))
        loss_by_code[code][business_date] = _decimal(row.get("lost_observed_qty"))

    patterns = _pattern_by_code(
        inputs["sales_by_code"],
        as_of=inputs["date_from"],
    )
    opportunities = build_decision_windows(
        decision_rows_by_date=inputs["decision_rows_by_date"],
        sales_by_code=inputs["sales_by_code"],
        loss_by_code=loss_by_code,
        pattern_by_code=patterns,
        date_to=inputs["date_to"],
    )
    opportunities_with_buffers = attach_rolling_buffers(
        opportunities,
        min_samples=min_calibration_samples,
    )
    opportunity_index = {
        _clean(row.get("opportunity_id")): row for row in opportunities_with_buffers
    }
    cases = select_case_anchors(opportunities_with_buffers, episodes)
    controls = select_control_pool(
        opportunities_with_buffers,
        case_ids={_clean(row.get("opportunity_id")) for row in cases},
    )
    pairs, unmatched = match_cases_to_controls(cases, controls)
    match = matching_summary(pairs)
    buffers = evaluate_buffers(pairs, opportunity_index)
    segments = segment_comparison_rows(pairs)
    stability = segment_stability_rows(pairs)
    policies = buffer_policy_evaluation_rows(pairs, opportunity_index, stability)
    sensitivity = candidate_sensitivity_rows(pairs, opportunity_index)

    sale_episode_loss = sum(
        (
            _decimal(row.get("lost_observed_qty"))
            for row in episodes
            if _clean(row.get("status")) == STAGE_GROW
        ),
        ZERO,
    )
    anchored_loss = sum((_decimal(row.get("episode_lost_observed_qty")) for row in cases), ZERO)
    matched_loss = sum((_decimal(row.get("case_episode_lost_qty")) for row in pairs), ZERO)
    missing_cost = sum(
        _decimal(row.get("inventory_cost_per_unit_rub")) <= ZERO for row in opportunities
    )
    insufficient_calibration = sum(
        _clean(row.get("calibration_level")) == "insufficient" for row in opportunities_with_buffers
    )
    summary: dict[str, Any] = {
        "schema": "display_auto_order_forecast_control_analysis.v2",
        "source_preflight_manifest_sha256": _sha256(preflight_dir / "run-manifest.json"),
        "source_loss_analysis_sha256": _sha256(loss_analysis_dir / "analysis-summary.json"),
        "date_from": inputs["date_from"].isoformat(),
        "date_to": inputs["date_to"].isoformat(),
        "diagnostic_only": True,
        "production_authorized": False,
        "headline": {
            "decision_window_count": len(opportunities),
            "case_anchor_count": len(cases),
            "control_pool_count": len(controls),
            "matched_pair_count": len(pairs),
            "unmatched_case_count": len(unmatched),
            "sale_episode_loss_qty": str(sale_episode_loss),
            "anchored_sale_episode_loss_qty": str(anchored_loss),
            "matched_sale_episode_loss_qty": str(matched_loss),
            "matched_sale_episode_loss_share": str(
                matched_loss / sale_episode_loss if sale_episode_loss > ZERO else ZERO
            ),
        },
        "matching": match,
        "buffer_profiles": buffers,
        "buffer_policies": policies,
        "candidate_sensitivity": sensitivity,
        "top_segments": segments[:20],
        "candidate_segments": [row for row in stability if int(row.get("candidate_p90") or 0)],
        "quality": {
            "missing_cost_opportunity_count": missing_cost,
            "insufficient_calibration_opportunity_count": insufficient_calibration,
            "insufficient_calibration_share": str(
                Decimal(insufficient_calibration) / Decimal(len(opportunities))
                if opportunities
                else ZERO
            ),
            "min_calibration_samples": min_calibration_samples,
        },
        "method": {
            "population": "scheduled weekly reviews in historical sale/Растим stage with a fully observed P50 lead-time plus 7-day cadence outcome window",
            "case": "latest eligible scheduled review covering a P75 loss episode; duplicate episodes sharing one anchor are combined",
            "control": "one deterministic no-loss scheduled review per different SKU-month",
            "matching": "one-to-one without replacement; pre-outcome demand pattern, P50 lead band, confidence, outcome period and forecast-rate band with deterministic relaxations",
            "buffer": "nearest-rank P60/P75/P90 from other fully completed prior decision windows; hierarchical cross-SKU segment fallback; no future window enters calibration",
            "candidate_rule": "P90 candidate segments are selected only from windows ending before the final month: at least 30 pairs, mean case-control underforecast gap at least 1 unit and positive frequency gap; all other segments keep P75",
            "sensitivity": "candidate minimum pair counts 20/30/50 and mean gaps 0/1/2 are trained before the final month and evaluated on windows ending in the final month; stability and policy proxies use only pairs with the same outcome period",
            "interpretation": "buffer results are screening proxies, not simulated saved sales or warehouse capital; a frozen backtest is required before any policy change",
        },
        "reconciliation": {
            "matched_plus_unmatched_minus_case_anchors": len(pairs) + len(unmatched) - len(cases),
            "pair_unique_case_ids": len({_clean(row.get("case_opportunity_id")) for row in pairs}),
            "pair_unique_control_ids": len(
                {_clean(row.get("control_opportunity_id")) for row in pairs}
            ),
        },
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "decision-windows.csv", opportunities_with_buffers)
    _write_csv(output_dir / "case-anchors.csv", cases)
    _write_csv(output_dir / "control-pool.csv", controls)
    _write_csv(output_dir / "matched-pairs.csv", pairs)
    _write_csv(output_dir / "unmatched-cases.csv", unmatched)
    _write_csv(output_dir / "buffer-profile-evaluation.csv", buffers)
    _write_csv(output_dir / "segment-comparison.csv", segments)
    _write_csv(output_dir / "segment-stability.csv", stability)
    _write_csv(output_dir / "buffer-policy-evaluation.csv", policies)
    _write_csv(output_dir / "candidate-sensitivity.csv", sensitivity)
    (output_dir / "analysis-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    artifact_names = (
        "analysis-summary.json",
        "decision-windows.csv",
        "case-anchors.csv",
        "control-pool.csv",
        "matched-pairs.csv",
        "unmatched-cases.csv",
        "buffer-profile-evaluation.csv",
        "segment-comparison.csv",
        "segment-stability.csv",
        "buffer-policy-evaluation.csv",
        "candidate-sensitivity.csv",
    )
    manifest = {
        "schema": "display_auto_order_forecast_control_analysis_manifest.v2",
        "diagnostic_only": True,
        "production_authorized": False,
        "files": {name: _sha256(output_dir / name) for name in artifact_names},
    }
    (output_dir / "analysis-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, required=True)
    parser.add_argument("--loss-analysis-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--min-calibration-samples", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.min_calibration_samples <= 0:
        raise SystemExit("--min-calibration-samples must be positive")
    summary = build_analysis(
        preflight_dir=args.preflight_dir,
        loss_analysis_dir=args.loss_analysis_dir,
        output_dir=args.output_dir,
        min_calibration_samples=args.min_calibration_samples,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
