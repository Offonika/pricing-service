"""Diagnose a compact display auto-order quick backtest at SKU level.

The task replays only the persisted control and hypothesis scenarios from a
frozen preflight.  It does not query production sources and cannot create
purchase orders.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

import nbformat
from nbclient import NotebookClient

from app.services.display_auto_order_demand_pattern import (
    DEMAND_HISTORY_WEEKS,
    WEEK_DAYS,
    classify_demand_pattern,
    completed_weekly_demand,
)
from app.services.display_auto_order_demand_pattern import (
    sum_dates as _sum_dates,
)
from tasks import report_display_auto_order_frozen_backtest as frozen
from tasks.build_display_auto_order_dry_run import load_auto_order_policy
from tasks.display_auto_order_backtest_preflight import (
    load_scenario_config,
    validate_preflight_directory,
)

ZERO = Decimal("0")
ONE = Decimal("1")
YEAR_DAYS = Decimal("365")


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


def croston_sba_forecast(
    weekly_demand: Sequence[Decimal],
    *,
    alpha: Decimal = Decimal("0.1"),
) -> Decimal:
    values = [max(ZERO, _decimal(value)) for value in weekly_demand]
    first_index = next((index for index, value in enumerate(values) if value > ZERO), None)
    if first_index is None:
        return ZERO
    size = values[first_index]
    interval = Decimal(first_index + 1)
    elapsed = 1
    for value in values[first_index + 1 :]:
        if value > ZERO:
            size += alpha * (value - size)
            interval += alpha * (Decimal(elapsed) - interval)
            elapsed = 1
        else:
            elapsed += 1
    return max(ZERO, (ONE - alpha / Decimal("2")) * size / interval)


def tsb_forecast(
    weekly_demand: Sequence[Decimal],
    *,
    alpha: Decimal = Decimal("0.1"),
    beta: Decimal = Decimal("0.1"),
) -> Decimal:
    values = [max(ZERO, _decimal(value)) for value in weekly_demand]
    first_index = next((index for index, value in enumerate(values) if value > ZERO), None)
    if first_index is None:
        return ZERO
    size = values[first_index]
    probability = ONE / Decimal(first_index + 1)
    for value in values[first_index + 1 :]:
        occurrence = ONE if value > ZERO else ZERO
        probability += beta * (occurrence - probability)
        if value > ZERO:
            size += alpha * (value - size)
    return max(ZERO, probability * size)


def empirical_quantile(values: Sequence[Decimal], percentile: Decimal) -> Decimal:
    ordered = sorted(max(ZERO, _decimal(value)) for value in values)
    if not ordered:
        return ZERO
    rank = int((Decimal(len(ordered) - 1) * percentile).to_integral_value())
    return ordered[max(0, min(rank, len(ordered) - 1))]


def _cost_band(value: Decimal) -> str:
    if value <= ZERO:
        return "unknown"
    if value < Decimal("500"):
        return "<500"
    if value < Decimal("1500"):
        return "500-1499"
    if value < Decimal("3000"):
        return "1500-2999"
    return ">=3000"


def _margin_band(value: Decimal) -> str:
    if value <= ZERO:
        return "unknown_or_nonpositive"
    if value < Decimal("300"):
        return "<300"
    if value < Decimal("700"):
        return "300-699"
    if value < Decimal("1500"):
        return "700-1499"
    return ">=1500"


def _lead_time_band(days: int) -> str:
    if days <= 0:
        return "unknown"
    if days <= 30:
        return "<=30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return ">90"


def _velocity_band(mean_weekly: Decimal) -> str:
    if mean_weekly <= ZERO:
        return "0"
    if mean_weekly <= Decimal("0.25"):
        return "(0;0.25]"
    if mean_weekly <= ONE:
        return "(0.25;1]"
    return ">1"


def _economic_contribution(
    metric: frozen.Metric,
    *,
    annual_rate: Decimal,
    period_days: int,
) -> Decimal:
    average_inventory = metric.inventory_value_days_rub / Decimal(period_days)
    carrying_cost = average_inventory * annual_rate * Decimal(period_days) / YEAR_DAYS
    return metric.gross_profit_rub - carrying_cost


def _prepare_inputs(preflight_dir: Path) -> dict[str, Any]:
    manifest = validate_preflight_directory(preflight_dir)
    date_from = date.fromisoformat(manifest["date_from"])
    date_to = date.fromisoformat(manifest["date_to"])
    fact_rows_by_date: dict[date, list[dict[str, str]]] = defaultdict(list)
    decision_rows_by_date: dict[date, list[dict[str, str]]] = defaultdict(list)
    sales_by_code: dict[str, dict[date, Decimal]] = defaultdict(dict)
    first_decision_by_code: dict[str, dict[str, str]] = {}

    for row in _read_csv(preflight_dir / "historical-sales.csv"):
        business_date = frozen._date(row.get("business_date"))
        code = _clean(row.get("nomenclature_code"))
        if business_date is not None and code:
            sales_by_code[code][business_date] = max(
                ZERO,
                _decimal(row.get("observed_sales_qty")),
            )
    for row in _read_csv(preflight_dir / "daily-facts.csv"):
        business_date = frozen._date(row.get("business_date"))
        code = _clean(row.get("nomenclature_code"))
        if business_date is None or not code:
            continue
        fact_rows_by_date[business_date].append(row)
        sales_by_code[code][business_date] = max(
            ZERO,
            _decimal(row.get("observed_sales_qty")),
        )
    for row in _read_csv(preflight_dir / "decision-inputs.csv"):
        business_date = frozen._date(row.get("decision_date"))
        code = _clean(row.get("nomenclature_code"))
        if business_date is None or not code:
            continue
        decision_rows_by_date[business_date].append(row)
        first_decision_by_code.setdefault(code, row)

    return {
        "manifest": manifest,
        "date_from": date_from,
        "date_to": date_to,
        "fact_rows_by_date": fact_rows_by_date,
        "decision_rows_by_date": decision_rows_by_date,
        "sales_by_code": sales_by_code,
        "first_decision_by_code": first_decision_by_code,
        "initial_pipeline": _read_csv(preflight_dir / "initial-pipeline.csv"),
        "frozen_scenarios": frozen._load_scenarios(preflight_dir / "scenario-decisions.csv"),
    }


def _selected_scenarios(
    *,
    quick_summary: Mapping[str, Any],
    scenarios: Sequence[frozen.FrozenScenario],
) -> tuple[frozen.FrozenScenario, frozen.FrozenScenario]:
    roles = quick_summary["source_scenario_roles"]
    selection = frozen.select_scenarios(
        scenarios,
        run_mode=frozen.RUN_MODE_QUICK,
        control_scenario_id=roles["control"],
        hypothesis_scenario_id=roles["hypothesis"],
        cautious_scenario_id=roles["cautious"],
    )
    guard = quick_summary["quick_acceleration_guard"]
    selection = frozen.apply_quick_acceleration_guard(
        selection,
        hypothesis_min_shortage_qty=_decimal(guard["hypothesis_min_shortage_qty"]),
        cautious_min_shortage_qty=_decimal(guard["cautious_min_shortage_qty"]),
        cap_to_projected_shortage=bool(guard["cap_to_projected_shortage"]),
        single_open_lot=bool(guard.get("single_open_lot")),
    )
    hypothesis_segment_profile = (
        _clean(guard.get("hypothesis_segment_profile")) or frozen.ACCELERATION_SEGMENT_PROFILE_OFF
    )
    cautious_segment_profile = (
        _clean(guard.get("cautious_segment_profile")) or frozen.ACCELERATION_SEGMENT_PROFILE_OFF
    )
    if any(
        profile != frozen.ACCELERATION_SEGMENT_PROFILE_OFF
        for profile in (hypothesis_segment_profile, cautious_segment_profile)
    ):
        selection = frozen.apply_quick_acceleration_segment_gates(
            selection,
            hypothesis_profile=hypothesis_segment_profile,
            cautious_profile=cautious_segment_profile,
        )
    by_id = {scenario.scenario_id: scenario for scenario in selection.scenarios}
    return by_id[selection.scenario_roles["control"]], by_id[selection.scenario_roles["hypothesis"]]


def _acceleration_by_sku(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Decimal | int | str]]:
    output: dict[str, dict[str, Decimal | int | str]] = defaultdict(
        lambda: {
            "acceleration_positive_recalculations": 0,
            "acceleration_order_lines": 0,
            "first_acceleration_order_date": "",
            "first_acceleration_order_qty": ZERO,
            "repeat_acceleration_order_lines": 0,
            "repeat_acceleration_order_qty": ZERO,
            "acceleration_uncapped_requested_qty": ZERO,
            "acceleration_shortage_cap_reduction_qty": ZERO,
            "acceleration_requested_qty": ZERO,
            "acceleration_allocated_qty": ZERO,
            "pipeline_haircut_exposure_qty": ZERO,
        }
    )
    first_seen: set[str] = set()
    for row in sorted(
        rows,
        key=lambda item: (_clean(item.get("decision_date")), _clean(item.get("nomenclature_code"))),
    ):
        allocated = _decimal(row.get("acceleration_allocated_qty"))
        if allocated <= ZERO:
            continue
        code = _clean(row.get("nomenclature_code"))
        values = output[code]
        values["acceleration_positive_recalculations"] = (
            int(values["acceleration_positive_recalculations"]) + 1
        )
        for field in (
            "acceleration_uncapped_requested_qty",
            "acceleration_shortage_cap_reduction_qty",
            "acceleration_requested_qty",
            "acceleration_allocated_qty",
        ):
            values[field] = _decimal(values[field]) + _decimal(row.get(field))
        fraction = _decimal(row.get("acceleration_pipeline_fraction"))
        effective_pipeline = _decimal(row.get("effective_model_pipeline_qty"))
        if ZERO < fraction < ONE:
            values["pipeline_haircut_exposure_qty"] = _decimal(
                values["pipeline_haircut_exposure_qty"]
            ) + max(ZERO, effective_pipeline / fraction - effective_pipeline)

        order_qty = (
            _decimal(row.get("acceleration_order_component_qty"))
            if "acceleration_order_component_qty" in row
            else _decimal(row.get("recommended_order_qty"))
        )
        if order_qty <= ZERO:
            continue
        values["acceleration_order_lines"] = int(values["acceleration_order_lines"]) + 1
        if code not in first_seen:
            first_seen.add(code)
            values["first_acceleration_order_date"] = _clean(row.get("decision_date"))
            values["first_acceleration_order_qty"] = order_qty
        else:
            values["repeat_acceleration_order_lines"] = (
                int(values["repeat_acceleration_order_lines"]) + 1
            )
            values["repeat_acceleration_order_qty"] = (
                _decimal(values["repeat_acceleration_order_qty"]) + order_qty
            )
    return output


def _sku_rows(
    *,
    control: frozen.SimulationResult,
    hypothesis: frozen.SimulationResult,
    first_decision_by_code: Mapping[str, Mapping[str, Any]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    date_from: date,
    date_to: date,
) -> list[dict[str, Any]]:
    acceleration = _acceleration_by_sku(hypothesis.decision_rows)
    period_days = (date_to - date_from).days + 1
    annual_rate = hypothesis.scenario.cost.total_annual_rate
    rows: list[dict[str, Any]] = []
    for code in sorted(hypothesis.model):
        start = first_decision_by_code.get(code, {})
        pattern = classify_demand_pattern(
            completed_weekly_demand(sales_by_code.get(code, {}), as_of=date_from)
        )
        actual_metric = hypothesis.actual[code]
        control_metric = control.model[code]
        hypothesis_metric = hypothesis.model[code]
        control_economic = _economic_contribution(
            control_metric,
            annual_rate=annual_rate,
            period_days=period_days,
        )
        hypothesis_economic = _economic_contribution(
            hypothesis_metric,
            annual_rate=annual_rate,
            period_days=period_days,
        )
        capital_delta = (
            hypothesis_metric.inventory_value_days_rub - control_metric.inventory_value_days_rub
        ) / Decimal(period_days)
        acceleration_values = acceleration.get(code, {})
        cost = _decimal(start.get("inventory_cost_per_unit_rub"))
        margin = _decimal(start.get("gross_margin_per_unit_rub"))
        lead_days = int(_decimal(start.get("lead_time_p75_days")))
        row = {
            "nomenclature_code": code,
            "name": _clean(start.get("name")),
            "stage_at_period_start": _clean(start.get("status")),
            "demand_pattern_preperiod": pattern.name,
            "adi_preperiod": "" if pattern.adi is None else str(pattern.adi),
            "cv2_preperiod": "" if pattern.cv2 is None else str(pattern.cv2),
            "positive_sales_weeks_preperiod": pattern.positive_weeks,
            "mean_weekly_sales_preperiod": str(pattern.mean_weekly_demand),
            "inventory_cost_per_unit_rub_start": str(cost),
            "gross_margin_per_unit_rub_start": str(margin),
            "lead_time_p75_days_start": lead_days,
            "lead_time_confidence_start": _clean(start.get("lead_time_confidence")) or "unknown",
            "cost_band": _cost_band(cost),
            "margin_band": _margin_band(margin),
            "lead_time_band": _lead_time_band(lead_days),
            "velocity_band_preperiod": _velocity_band(pattern.mean_weekly_demand),
            "observed_demand_qty": str(actual_metric.observed_demand_qty),
            "control_served_observed_qty": str(control_metric.served_observed_qty),
            "hypothesis_served_observed_qty": str(hypothesis_metric.served_observed_qty),
            "served_observed_delta_to_control_qty": str(
                hypothesis_metric.served_observed_qty - control_metric.served_observed_qty
            ),
            "gross_profit_delta_to_control_rub": str(
                hypothesis_metric.gross_profit_rub - control_metric.gross_profit_rub
            ),
            "capital_delta_to_control_rub": str(capital_delta),
            "ending_inventory_delta_to_control_qty": str(
                hypothesis_metric.ending_inventory_qty - control_metric.ending_inventory_qty
            ),
            "economic_contribution_control_rub": str(control_economic),
            "economic_contribution_hypothesis_rub": str(hypothesis_economic),
            "economic_contribution_delta_to_control_rub": str(
                hypothesis_economic - control_economic
            ),
            "control_order_qty": str(control_metric.order_qty),
            "hypothesis_order_qty": str(hypothesis_metric.order_qty),
            "order_delta_to_control_qty": str(
                hypothesis_metric.order_qty - control_metric.order_qty
            ),
        }
        row.update(acceleration_values)
        rows.append(row)
    rows.sort(key=lambda row: _decimal(row["economic_contribution_delta_to_control_rub"]))
    return rows


def _segment_rows(sku_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    dimensions = (
        "demand_pattern_preperiod",
        "cost_band",
        "margin_band",
        "lead_time_band",
        "lead_time_confidence_start",
        "velocity_band_preperiod",
        "stage_at_period_start",
    )
    output: list[dict[str, Any]] = []
    for dimension in dimensions:
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in sku_rows:
            grouped[_clean(row.get(dimension)) or "unknown"].append(row)
        for value, rows in sorted(grouped.items()):
            output.append(
                {
                    "segment_dimension": dimension,
                    "segment_value": value,
                    "sku_count": len(rows),
                    "observed_demand_qty": str(
                        sum((_decimal(row.get("observed_demand_qty")) for row in rows), ZERO)
                    ),
                    "served_observed_delta_to_control_qty": str(
                        sum(
                            (
                                _decimal(row.get("served_observed_delta_to_control_qty"))
                                for row in rows
                            ),
                            ZERO,
                        )
                    ),
                    "gross_profit_delta_to_control_rub": str(
                        sum(
                            (
                                _decimal(row.get("gross_profit_delta_to_control_rub"))
                                for row in rows
                            ),
                            ZERO,
                        )
                    ),
                    "capital_delta_to_control_rub": str(
                        sum(
                            (_decimal(row.get("capital_delta_to_control_rub")) for row in rows),
                            ZERO,
                        )
                    ),
                    "ending_inventory_delta_to_control_qty": str(
                        sum(
                            (
                                _decimal(row.get("ending_inventory_delta_to_control_qty"))
                                for row in rows
                            ),
                            ZERO,
                        )
                    ),
                    "economic_contribution_delta_to_control_rub": str(
                        sum(
                            (
                                _decimal(row.get("economic_contribution_delta_to_control_rub"))
                                for row in rows
                            ),
                            ZERO,
                        )
                    ),
                    "negative_economic_sku_count": sum(
                        _decimal(row.get("economic_contribution_delta_to_control_rub")) < ZERO
                        for row in rows
                    ),
                    "acceleration_positive_recalculations": sum(
                        int(row.get("acceleration_positive_recalculations") or 0) for row in rows
                    ),
                    "acceleration_order_lines": sum(
                        int(row.get("acceleration_order_lines") or 0) for row in rows
                    ),
                    "repeat_acceleration_order_lines": sum(
                        int(row.get("repeat_acceleration_order_lines") or 0) for row in rows
                    ),
                    "repeat_acceleration_order_qty": str(
                        sum(
                            (_decimal(row.get("repeat_acceleration_order_qty")) for row in rows),
                            ZERO,
                        )
                    ),
                    "acceleration_allocated_qty": str(
                        sum(
                            (_decimal(row.get("acceleration_allocated_qty")) for row in rows),
                            ZERO,
                        )
                    ),
                    "pipeline_haircut_exposure_qty": str(
                        sum(
                            (_decimal(row.get("pipeline_haircut_exposure_qty")) for row in rows),
                            ZERO,
                        )
                    ),
                }
            )
    return output


def _mechanism_rows(sku_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in sku_rows:
        order_lines = int(row.get("acceleration_order_lines") or 0)
        repeat_lines = int(row.get("repeat_acceleration_order_lines") or 0)
        if order_lines <= 0:
            group = "no_acceleration_order"
        elif repeat_lines <= 0:
            group = "first_order_only"
        else:
            group = "has_repeat_orders"
        grouped[group].append(row)

    output = []
    for group in ("no_acceleration_order", "first_order_only", "has_repeat_orders"):
        rows = grouped.get(group, [])
        output.append(
            {
                "mechanism_group": group,
                "sku_count": len(rows),
                "served_observed_delta_to_control_qty": str(
                    sum(
                        (_decimal(row.get("served_observed_delta_to_control_qty")) for row in rows),
                        ZERO,
                    )
                ),
                "gross_profit_delta_to_control_rub": str(
                    sum(
                        (_decimal(row.get("gross_profit_delta_to_control_rub")) for row in rows),
                        ZERO,
                    )
                ),
                "capital_delta_to_control_rub": str(
                    sum(
                        (_decimal(row.get("capital_delta_to_control_rub")) for row in rows),
                        ZERO,
                    )
                ),
                "ending_inventory_delta_to_control_qty": str(
                    sum(
                        (
                            _decimal(row.get("ending_inventory_delta_to_control_qty"))
                            for row in rows
                        ),
                        ZERO,
                    )
                ),
                "economic_contribution_delta_to_control_rub": str(
                    sum(
                        (
                            _decimal(row.get("economic_contribution_delta_to_control_rub"))
                            for row in rows
                        ),
                        ZERO,
                    )
                ),
                "order_delta_to_control_qty": str(
                    sum((_decimal(row.get("order_delta_to_control_qty")) for row in rows), ZERO)
                ),
                "acceleration_order_lines": sum(
                    int(row.get("acceleration_order_lines") or 0) for row in rows
                ),
                "repeat_acceleration_order_lines": sum(
                    int(row.get("repeat_acceleration_order_lines") or 0) for row in rows
                ),
            }
        )
    return output


def _concentration_rows(sku_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    negative = sorted(
        (
            -_decimal(row.get("economic_contribution_delta_to_control_rub"))
            for row in sku_rows
            if _decimal(row.get("economic_contribution_delta_to_control_rub")) < ZERO
        ),
        reverse=True,
    )
    gross_loss = sum(negative, ZERO)
    net = sum(
        (_decimal(row.get("economic_contribution_delta_to_control_rub")) for row in sku_rows),
        ZERO,
    )
    output = []
    for count in (10, 50, 100, 250):
        amount = sum(negative[:count], ZERO)
        output.append(
            {
                "top_n": count,
                "gross_negative_contribution_rub": str(amount),
                "share_of_gross_negative_contribution": str(
                    amount / gross_loss if gross_loss > ZERO else ZERO
                ),
                "all_negative_sku_count": len(negative),
                "all_gross_negative_contribution_rub": str(gross_loss),
                "net_economic_delta_rub": str(net),
            }
        )
    return output


def _forecast_benchmark_rows(
    *,
    decision_rows_by_date: Mapping[date, Sequence[Mapping[str, Any]]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    patterns: Mapping[str, str],
    date_to: date,
) -> list[dict[str, Any]]:
    detail: list[tuple[str, str, Decimal, Decimal]] = []
    for decision_date, rows in decision_rows_by_date.items():
        if decision_date + timedelta(days=7) > date_to:
            continue
        for row in rows:
            if _clean(row.get("scheduled_review")) != "1" or _clean(row.get("status")) != "sale":
                continue
            code = _clean(row.get("nomenclature_code"))
            history = completed_weekly_demand(sales_by_code.get(code, {}), as_of=decision_date)
            actual = _sum_dates(
                sales_by_code.get(code, {}),
                decision_date + timedelta(days=1),
                decision_date + timedelta(days=8),
            )
            forecasts = {
                "current_rate": max(ZERO, _decimal(row.get("forecast_rate_sales"))) * Decimal("7"),
                "rolling_52w_mean": sum(history, ZERO) / Decimal(len(history)),
                "croston_sba": croston_sba_forecast(history),
                "tsb": tsb_forecast(history),
            }
            pattern = patterns.get(code, "unknown")
            for model_name, prediction in forecasts.items():
                detail.append((pattern, model_name, actual, prediction))

    output: list[dict[str, Any]] = []
    for pattern in sorted({"all", *(row[0] for row in detail)}):
        for model_name in sorted({row[1] for row in detail}):
            selected = [
                row
                for row in detail
                if row[1] == model_name and (pattern == "all" or row[0] == pattern)
            ]
            if not selected:
                continue
            actual_total = sum((row[2] for row in selected), ZERO)
            prediction_total = sum((row[3] for row in selected), ZERO)
            absolute_error = sum((abs(row[3] - row[2]) for row in selected), ZERO)
            output.append(
                {
                    "demand_pattern_preperiod": pattern,
                    "forecast_model": model_name,
                    "observations": len(selected),
                    "actual_next_7d_qty": str(actual_total),
                    "predicted_next_7d_qty": str(prediction_total),
                    "wape": str(absolute_error / actual_total if actual_total > ZERO else ZERO),
                    "bias": str(
                        (prediction_total - actual_total) / actual_total
                        if actual_total > ZERO
                        else ZERO
                    ),
                    "mae_qty": str(absolute_error / Decimal(len(selected))),
                }
            )
    return output


def _coverage_benchmark_rows(
    *,
    decision_rows_by_date: Mapping[date, Sequence[Mapping[str, Any]]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    patterns: Mapping[str, str],
    history_start: date,
    date_to: date,
) -> list[dict[str, Any]]:
    detail: list[tuple[str, str, Decimal, Decimal]] = []
    for decision_date, rows in decision_rows_by_date.items():
        for row in rows:
            if _clean(row.get("scheduled_review")) != "1" or _clean(row.get("status")) != "sale":
                continue
            code = _clean(row.get("nomenclature_code"))
            coverage_days = int(_decimal(row.get("lead_time_p75_days"))) + WEEK_DAYS
            if coverage_days <= 0 or decision_date + timedelta(days=coverage_days) > date_to:
                continue
            samples = []
            for step in range(DEMAND_HISTORY_WEEKS):
                end = decision_date - timedelta(days=step * WEEK_DAYS)
                start = end - timedelta(days=coverage_days)
                if start < history_start:
                    continue
                samples.append(_sum_dates(sales_by_code.get(code, {}), start, end))
            if len(samples) < 8:
                continue
            actual = _sum_dates(
                sales_by_code.get(code, {}),
                decision_date + timedelta(days=1),
                decision_date + timedelta(days=coverage_days + 1),
            )
            targets = {
                "current_point_projection": (
                    max(ZERO, _decimal(row.get("forecast_rate_sales"))) * Decimal(coverage_days)
                ),
                "empirical_p75": empirical_quantile(samples, Decimal("0.75")),
                "empirical_p90": empirical_quantile(samples, Decimal("0.90")),
            }
            pattern = patterns.get(code, "unknown")
            for target_name, target in targets.items():
                detail.append((pattern, target_name, actual, target))

    output: list[dict[str, Any]] = []
    for pattern in sorted({"all", *(row[0] for row in detail)}):
        for target_name in sorted({row[1] for row in detail}):
            selected = [
                row
                for row in detail
                if row[1] == target_name and (pattern == "all" or row[0] == pattern)
            ]
            if not selected:
                continue
            actual_total = sum((row[2] for row in selected), ZERO)
            target_total = sum((row[3] for row in selected), ZERO)
            shortage = sum((max(ZERO, row[2] - row[3]) for row in selected), ZERO)
            excess = sum((max(ZERO, row[3] - row[2]) for row in selected), ZERO)
            output.append(
                {
                    "demand_pattern_preperiod": pattern,
                    "coverage_target": target_name,
                    "observations": len(selected),
                    "actual_coverage_demand_qty": str(actual_total),
                    "target_qty": str(target_total),
                    "empirical_service_frequency": str(
                        Decimal(sum(row[3] >= row[2] for row in selected)) / Decimal(len(selected))
                    ),
                    "shortage_qty": str(shortage),
                    "excess_qty": str(excess),
                }
            )
    return output


def _period_rows(
    *,
    control: frozen.SimulationResult,
    hypothesis: frozen.SimulationResult,
    date_from: date,
    date_to: date,
) -> list[dict[str, Any]]:
    periods = (
        ("pre_july", date_from, min(date_to, date(2026, 6, 30))),
        ("july", max(date_from, date(2026, 7, 1)), date_to),
    )
    output: list[dict[str, Any]] = []
    for period_name, period_from, period_to in periods:
        if period_from > period_to:
            continue
        days = (period_to - period_from).days + 1
        by_role: dict[str, dict[str, Decimal]] = {}
        for role, result in (("control", control), ("hypothesis", hypothesis)):
            selected = [
                row
                for row in result.daily_rows
                if period_from <= date.fromisoformat(_clean(row["business_date"])) <= period_to
            ]
            observed = sum(
                (_decimal(row.get("actual_observed_demand_qty")) for row in selected), ZERO
            )
            served_observed = sum(
                (_decimal(row.get("model_served_observed_qty")) for row in selected), ZERO
            )
            gross_profit = sum(
                (_decimal(row.get("model_gross_profit_rub")) for row in selected), ZERO
            )
            average_inventory = sum(
                (_decimal(row.get("model_inventory_value_rub")) for row in selected), ZERO
            ) / Decimal(days)
            economic = gross_profit - (
                average_inventory
                * result.scenario.cost.total_annual_rate
                * Decimal(days)
                / YEAR_DAYS
            )
            by_role[role] = {
                "observed": observed,
                "served": served_observed,
                "gross_profit": gross_profit,
                "average_inventory": average_inventory,
                "economic": economic,
            }
            output.append(
                {
                    "period": period_name,
                    "date_from": period_from.isoformat(),
                    "date_to": period_to.isoformat(),
                    "scenario_role": role,
                    "observed_demand_qty": str(observed),
                    "served_observed_qty": str(served_observed),
                    "observed_fill_rate": str(
                        served_observed / observed if observed > ZERO else ONE
                    ),
                    "gross_profit_rub": str(gross_profit),
                    "average_inventory_value_rub": str(average_inventory),
                    "economic_contribution_rub": str(economic),
                }
            )
        output.append(
            {
                "period": period_name,
                "date_from": period_from.isoformat(),
                "date_to": period_to.isoformat(),
                "scenario_role": "hypothesis_minus_control",
                "observed_demand_qty": "0",
                "served_observed_qty": str(
                    by_role["hypothesis"]["served"] - by_role["control"]["served"]
                ),
                "observed_fill_rate": "",
                "gross_profit_rub": str(
                    by_role["hypothesis"]["gross_profit"] - by_role["control"]["gross_profit"]
                ),
                "average_inventory_value_rub": str(
                    by_role["hypothesis"]["average_inventory"]
                    - by_role["control"]["average_inventory"]
                ),
                "economic_contribution_rub": str(
                    by_role["hypothesis"]["economic"] - by_role["control"]["economic"]
                ),
            }
        )
    return output


def _fmt_number(value: Any, digits: int = 0) -> str:
    rendered = f"{_decimal(value):,.{digits}f}"
    return rendered.replace(",", " ").replace(".", ",")


def _fmt_signed(value: Any, digits: int = 0) -> str:
    rendered = f"{_decimal(value):+,.{digits}f}"
    return rendered.replace(",", " ").replace(".", ",")


def _fmt_percent(value: Any, digits: int = 1) -> str:
    return f"{_decimal(value) * Decimal('100'):.{digits}f}%".replace(".", ",")


def _build_segmented_markdown(summary: Mapping[str, Any]) -> str:
    headline = summary["headline"]
    data_quality = summary["data_quality"]
    scenario_id = _clean(summary["scenario_ids"]["hypothesis"])
    economic_delta = _decimal(headline["economic_delta_rub"])
    economic_word = "положительный" if economic_delta > ZERO else "отрицательный"
    return f"""# Сегментная диагностика ускорения автозаказа дисплеев

## Вывод

Сценарий `{scenario_id}` дал {economic_word} экономический вклад относительно
контроля: {_fmt_signed(economic_delta / Decimal('1000'), 2)} тыс. ₽. Он вернул
{_fmt_signed(headline['served_observed_delta_qty'], 1)} записанных продаж при
изменении среднего капитала на
{_fmt_signed(_decimal(headline['capital_delta_rub']) / Decimal('1000'), 2)} тыс. ₽
и конечного остатка на {_fmt_signed(headline['ending_inventory_delta_qty'], 1)}
единицы.

Это результат ускорительной надбавки относительно frozen-контроля, а не
production-разрешение и не автоматическое прохождение полного строгого
acceptance относительно факта.

## Методика

- тип спроса рассчитан по 52 завершённым неделям до начала теста;
- будущий результат отдельного SKU не использован как фильтр;
- causal single-open компонент заказа отделён от обычного min/max;
- исходная когорта: {data_quality['source_cohort_sku_count']} SKU, в симуляции:
  {data_quality['simulated_sku_count']} SKU;
- production-записей и PDF нет.

Подробные разрезы находятся в `segment-diagnostics.csv` и
`sku-diagnostics.csv`.
"""


def _build_markdown(summary: Mapping[str, Any]) -> str:
    if "_segment_" in _clean(summary["scenario_ids"]["hypothesis"]):
        return _build_segmented_markdown(summary)
    headline = summary["headline"]
    concentration = summary["concentration"]
    demand = summary["demand_patterns"]
    forecast = summary["forecast_recommendation"]
    coverage = summary["coverage_recommendation"]
    segments = summary["segment_highlights"]
    mechanism = summary["mechanism_highlights"]
    periods = summary["periods"]
    data_quality = summary["data_quality"]
    repeated_share = _decimal(headline["repeat_acceleration_order_qty_share"])
    top_100_share = _decimal(concentration["top_100_share"])
    return f"""# Почему ускорение автозаказа дисплеев не окупилось

## Executive Summary

- **Проблема не в том, что модель не нашла рост, а в том, что она повторяла закупочную реакцию.** После первого ускорительного заказа SKU мог снова получить добавку, пока предыдущая защита ещё ехала. Повторные ускорительные строки дали {_fmt_number(headline['repeat_acceleration_order_lines'])} решений и {_fmt_number(headline['repeat_acceleration_order_qty'])} единиц заказов; это {_fmt_percent(repeated_share)} количества всех строк заказа, где участвовало ускорение. SKU с повторами создали {_fmt_percent(mechanism['repeat_group_share_of_net_loss'])} чистого экономического провала и {_fmt_percent(mechanism['repeat_group_share_of_capital_delta'])} прироста капитала.
- **Сервис вырос, но слишком дорогой ценой.** Относительно контроля модель вернула {_fmt_signed(headline['served_observed_delta_qty'], 1)} записанных продаж, добавила {_fmt_signed(_decimal(headline['capital_delta_rub']) / Decimal('1000000'), 3)} млн ₽ среднего капитала и ухудшила экономический вклад на {_fmt_number(abs(_decimal(headline['economic_delta_rub'])) / Decimal('1000000'), 3)} млн ₽.
- **Спрос дисплеев в основном не похож на ровный поток.** В предтестовом окне {demand['non_smooth_sku_count']} из {demand['classified_sku_count']} классифицированных SKU ({_fmt_percent(demand['non_smooth_share'])}) имеют прерывистый, неровный или комковатый спрос. Ещё {demand['history_gap_sku_count']} SKU вообще не имеют достаточной предыстории. Поэтому правило «последние 14 дней быстрее предыдущих 42» часто принимает случайную пачку продаж за устойчивое ускорение.
- **Нужен другой контур количества, но не одна волшебная формула.** TSB улучшил недельный WAPE лишь на {_fmt_number((_decimal(forecast['current_wape']) - _decimal(forecast['best_wape'])) * Decimal('100'), 1)} п.п., а на SKU без истории стал хуже. Жизненную стадию оставляем как бизнес-режим, метод количества выбираем по типу и полноте истории, а экономику применяем как портфельный приоритет и бюджет.

## Лишний капитал создаёт повторное действие, а не первый сигнал

Ограничение добавки величиной дефицита действительно помогло: по каноническому итогу v10 оно сняло {_fmt_number(headline['shortage_cap_reduction_qty'])} единиц из первоначального расчёта. Но после маленького первого заказа модель продолжала видеть дефицит и создавала следующий лот. В расчёте нет отдельного состояния «ускорительная защита уже заказана и находится в пути».

Практический смысл: новый ускорительный заказ должен появляться только на **прирост** подтверждённого дефицита сверх уже открытого ускорительного pipeline. Пока этот лот не приехал, не отменён или не оказался недостаточным, повторно заказывать ту же защиту нельзя.

## Убыток нужно лечить адресно, но без выбора по будущему результату

Топ-100 убыточных SKU формируют {_fmt_percent(top_100_share)} валовой отрицательной части результата. Это полезно для диагностики, но нельзя просто внести эти коды в production-исключения: такой список использовал бы знание будущего. Следующее правило должно опираться только на признаки, известные в дату решения — тип спроса, уже открытый ускорительный pipeline, цена, маржа, срок и доверие к поставке.

До июля экономический эффект ускорения составил {_fmt_signed(_decimal(periods['pre_july_economic_delta_rub']) / Decimal('1000000'), 3)} млн ₽, в июле — {_fmt_signed(_decimal(periods['july_economic_delta_rub']) / Decimal('1000000'), 3)} млн ₽. Июльский скачок показан отдельно, поэтому вывод не держится на одном агрегате за полугодие.

Убыток особенно заметен там, где риск одной лишней единицы высок: SKU с себестоимостью от 3 000 ₽ дали {_fmt_signed(_decimal(segments['expensive_economic_delta_rub']) / Decimal('1000000'), 3)} млн ₽, а товары со средним доверием к сроку поставки — {_fmt_signed(_decimal(segments['medium_lead_confidence_economic_delta_rub']) / Decimal('1000000'), 3)} млн ₽. Это не готовые запреты, а признаки для будущего правила.

## Почему среднее ускорение плохо подходит части ассортимента

ADI — среднее число недель между неделями с продажей. CV² — насколько скачет размер ненулевой недельной продажи. В этой диагностике использована только история до 1 февраля 2026 года, поэтому классификация не подсматривает результат теста.

- `smooth`: продажи частые и сравнительно ровные;
- `intermittent`: продажи редкие, но размер события относительно стабилен;
- `erratic`: продажи частые, но размер сильно скачет;
- `lumpy`: продажи и редкие, и скачкообразные;
- `insufficient_history` / `no_history`: истории недостаточно для устойчивой оценки.

На семидневном прогнозе лучший из проверенных простых методов — **{forecast['best_model']}** с WAPE {_fmt_percent(forecast['best_wape'])}; текущая скорость дала {_fmt_percent(forecast['current_wape'])}. Улучшение есть, но оно небольшое. На SKU без предыстории TSB дал WAPE {_fmt_percent(forecast['tsb_no_history_wape'])} против {_fmt_percent(forecast['current_no_history_wape'])} у текущего расчёта, поэтому заменять весь прогноз на TSB нельзя.

Простой исторический `P90` спроса на срок поставки тоже оказался неготовым решением: фактическое покрытие составило лишь {_fmt_percent(coverage['p90_all_service'])} в целом, {_fmt_percent(coverage['p90_intermittent_service'])} для прерывистого и {_fmt_percent(coverage['p90_lumpy_service'])} для комковатого спроса. Значит, `(s, S)` надо калибровать по типу спроса, учитывать изменение уровня и цензурирование продаж дефицитом, а не просто брать сырой квантиль.

## Какой подход предлагается вместо единого правила

1. **Разделить жизненную стадию и тип спроса.** `Новинка → Пошли продажи → Растим → Поддерживаем` отвечает за режим сервиса и ручного контроля. `smooth/intermittent/erratic/lumpy` выбирает метод количества.
2. **Для `intermittent/lumpy` проверить калиброванный `(s, S)`.** Точка нового заказа `s` — прогнозное распределение спроса за срок поставки, цель `S` — за срок поставки плюс неделю пересмотра. Сырой исторический `P90` использовать нельзя: диагностика показала недокрытие.
3. **Для `smooth/erratic` оставить прогноз скорости; TSB использовать как challenger**, а не как немедленную замену. Для SKU без истории работают стартовый сервисный запас и сигналы спроса, а не исторический прогноз.
4. **Ввести реестр открытой защиты.** На SKU хранить источник, заказанное ускорением количество, дату ожидаемого прихода и остаток защиты. Новый лот — только на положительную разницу между новым дефицитом и открытой защитой.
5. **Экономику перенести на уровень портфеля.** Не обнулять нужное количество одной SKU. Сначала посчитать кандидатов, затем распределить недельный бюджет по ожидаемой сохранённой марже на рубль капитала, с лимитом общего риска неликвида.

## Что проверить следующим быстрым тестом

Сначала нужен один изолированный quick-тест: v10 плюс правило «один открытый ускорительный лот / только прирост дефицита». Он проверяет главную найденную ошибку, не меняя прогноз и стадии одновременно.

Вторым независимым тестом — shadow-расчёт `(s, S)` по эмпирическому спросу на срок поставки для `intermittent/lumpy`, без создания заказов. Сравнивать нужно сервис, валовую прибыль, средний капитал, конечный остаток и экономический вклад по прежнему строгому правилу.

## Открытые вопросы

- Какой недельный лимит капитала допустим для стадии «Растим» и всего портфеля дисплеев?
- Как долго открытый ускорительный лот считается защитой: до фактического прихода, до просрочки P75 или до ручной отмены?
- Нужны ли отдельные уровни сервиса для оригинальных дорогих дисплеев и дешёвых совместимых версий?

## Ограничения

- Это диагностический replay двух frozen-сценариев, а не production-разрешение.
- Источник заявил {data_quality['source_cohort_sku_count']} SKU, но frozen daily/decision tables содержат {data_quality['simulated_sku_count']}; {data_quality['missing_from_simulation_count']} SKU не имели ни одного дня, прошедшего фильтр активности `item_active_as_of`, и не попали в симуляционные строки. Итог v10 воспроизведён точно, но отчёт должен явно различать исходную и фактически активную когорту.
- SKU-концентрация используется только для поиска признаков; будущая прибыльность не используется как production-фильтр.
- ADI/CV² зависит от недельной гранулярности. Перед production пороги и метод прогноза должны пройти отдельный rolling backtest.
- PDF не создавался: для этой диагностики канонические артефакты — Markdown, CSV, JSON и исполняемый notebook.
"""


def _markdown_table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    if not rows:
        return "Нет данных."
    header = "| " + " | ".join(columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join(_clean(row.get(column)) for column in columns) + " |" for row in rows]
    return "\n".join([header, divider, *body])


def _build_notebook(output_dir: Path, summary: Mapping[str, Any]) -> Path:
    notebook = nbformat.v4.new_notebook()
    notebook.metadata["kernelspec"] = {
        "display_name": "Python 3",
        "language": "python",
        "name": "python3",
    }
    notebook.cells = [
        nbformat.v4.new_markdown_cell(
            "# Диагностика quick-backtest автозаказа дисплеев\n\n"
            "## tl;dr\n\n"
            "Исполняемый companion к Markdown-отчёту. Все таблицы построены только из "
            "frozen preflight и двух replay-сценариев; production-записей нет."
        ),
        nbformat.v4.new_markdown_cell(
            "## Context & Methods\n\n"
            "- период: 1 февраля — 31 июля 2026;\n"
            f"- источник когорты: {summary['data_quality']['source_cohort_sku_count']} SKU; "
            f"в frozen-симуляции: {summary['data_quality']['simulated_sku_count']} SKU;\n"
            "- сравнение: v10 против frozen-контроля;\n"
            "- ADI/CV²: 52 завершённые недели до начала теста;\n"
            "- экономический вклад: валовая прибыль минус стоимость капитала, хранения и устаревания.\n\n"
            "### Key Assumptions\n\n"
            "Ручные решения приняты, как в исходной симуляции. Концентрация убытка — диагностика, "
            "а не разрешённый production-фильтр."
        ),
        nbformat.v4.new_markdown_cell("## Data"),
        nbformat.v4.new_code_cell(
            "from pathlib import Path\n"
            "import csv, json\n\n"
            "output_dir = Path('.')\n"
            "summary = json.loads((output_dir / 'analysis-summary.json').read_text(encoding='utf-8'))\n"
            "def read_csv(name):\n"
            "    with (output_dir / name).open(encoding='utf-8-sig') as handle:\n"
            "        return list(csv.DictReader(handle))\n"
            "segments = read_csv('segment-diagnostics.csv')\n"
            "forecast = read_csv('forecast-benchmark.csv')\n"
            "concentration = read_csv('loss-concentration.csv')\n"
            "summary['headline']"
        ),
        nbformat.v4.new_markdown_cell("## Results"),
        nbformat.v4.new_code_cell(
            "all_forecasts = [row for row in forecast if row['demand_pattern_preperiod'] == 'all']\n"
            "sorted(all_forecasts, key=lambda row: float(row['wape']))"
        ),
        nbformat.v4.new_code_cell(
            "pattern_rows = [row for row in segments if row['segment_dimension'] == 'demand_pattern_preperiod']\n"
            "sorted(pattern_rows, key=lambda row: float(row['economic_contribution_delta_to_control_rub']))"
        ),
        nbformat.v4.new_code_cell("concentration"),
        nbformat.v4.new_markdown_cell(
            "## Takeaways\n\n"
            + "\n".join(
                [
                    "- Основной следующий тест: один открытый ускорительный лот на SKU.",
                    "- Стадию и метод прогноза нужно разделить.",
                    "- Для прерывистого спроса проверить `(s, S)` по распределению спроса на срок поставки.",
                    "- PDF не формируется без отдельной команды.",
                ]
            )
        ),
    ]
    path = output_dir / "diagnostic-analysis.ipynb"
    nbformat.write(notebook, path)
    client = NotebookClient(notebook, timeout=180, kernel_name="python3")
    client.execute(cwd=str(output_dir))
    nbformat.write(notebook, path)
    return path


def build_analysis(
    *,
    preflight_dir: Path,
    quick_result_dir: Path,
    output_dir: Path,
    policy_json: Path,
    scenario_config_json: Path,
    build_notebook: bool = True,
) -> dict[str, Any]:
    quick_summary = json.loads(
        (quick_result_dir / "frozen-summary.json").read_text(encoding="utf-8")
    )
    inputs = _prepare_inputs(preflight_dir)
    control_scenario, hypothesis_scenario = _selected_scenarios(
        quick_summary=quick_summary,
        scenarios=inputs["frozen_scenarios"],
    )
    policy = load_auto_order_policy(policy_json)
    config = load_scenario_config(scenario_config_json)
    shared_cache: dict[tuple[str, date, int], list[Decimal]] = {}
    simulation_args = {
        "fact_rows_by_date": inputs["fact_rows_by_date"],
        "decision_rows_by_date": inputs["decision_rows_by_date"],
        "initial_pipeline_rows": inputs["initial_pipeline"],
        "sales_by_code": inputs["sales_by_code"],
        "policy": policy,
        "config": config,
        "date_from": inputs["date_from"],
        "date_to": inputs["date_to"],
        "keep_detail": True,
        "demand_sample_cache": shared_cache,
    }
    control = frozen.simulate_scenario(scenario=control_scenario, **simulation_args)
    hypothesis = frozen.simulate_scenario(scenario=hypothesis_scenario, **simulation_args)

    output_dir.mkdir(parents=True, exist_ok=True)
    sku_rows = _sku_rows(
        control=control,
        hypothesis=hypothesis,
        first_decision_by_code=inputs["first_decision_by_code"],
        sales_by_code=inputs["sales_by_code"],
        date_from=inputs["date_from"],
        date_to=inputs["date_to"],
    )
    segment_rows = _segment_rows(sku_rows)
    mechanism_rows = _mechanism_rows(sku_rows)
    concentration_rows = _concentration_rows(sku_rows)
    patterns = {
        _clean(row["nomenclature_code"]): _clean(row["demand_pattern_preperiod"])
        for row in sku_rows
    }
    forecast_rows = _forecast_benchmark_rows(
        decision_rows_by_date=inputs["decision_rows_by_date"],
        sales_by_code=inputs["sales_by_code"],
        patterns=patterns,
        date_to=inputs["date_to"],
    )
    coverage_rows = _coverage_benchmark_rows(
        decision_rows_by_date=inputs["decision_rows_by_date"],
        sales_by_code=inputs["sales_by_code"],
        patterns=patterns,
        history_start=date.fromisoformat(inputs["manifest"]["history_start"]),
        date_to=inputs["date_to"],
    )
    period_rows = _period_rows(
        control=control,
        hypothesis=hypothesis,
        date_from=inputs["date_from"],
        date_to=inputs["date_to"],
    )

    _write_csv(output_dir / "sku-diagnostics.csv", sku_rows)
    _write_csv(output_dir / "segment-diagnostics.csv", segment_rows)
    _write_csv(output_dir / "mechanism-diagnostics.csv", mechanism_rows)
    _write_csv(output_dir / "loss-concentration.csv", concentration_rows)
    _write_csv(output_dir / "top-loss-skus.csv", sku_rows[:100])
    _write_csv(output_dir / "forecast-benchmark.csv", forecast_rows)
    _write_csv(output_dir / "coverage-benchmark.csv", coverage_rows)
    _write_csv(output_dir / "period-diagnostics.csv", period_rows)

    period_days = (inputs["date_to"] - inputs["date_from"]).days + 1
    control_summary = frozen._summary(
        scenario=control.scenario,
        strategy="model",
        metrics=control.model,
        period_days=period_days,
    )
    hypothesis_summary = frozen._summary(
        scenario=hypothesis.scenario,
        strategy="model",
        metrics=hypothesis.model,
        period_days=period_days,
    )
    persisted_hypothesis = next(
        row for row in quick_summary["quick_comparison"] if row["scenario_role"] == "hypothesis"
    )
    replay_checks = {
        field: str(_decimal(hypothesis_summary[field]) - _decimal(persisted_hypothesis[field]))
        for field in (
            "served_observed_qty",
            "gross_profit_rub",
            "average_inventory_value_rub",
            "ending_inventory_qty",
            "economic_contribution_rub",
        )
    }

    acceleration_rows = [
        row for row in sku_rows if int(row.get("acceleration_order_lines") or 0) > 0
    ]
    first_qty = sum(
        (_decimal(row.get("first_acceleration_order_qty")) for row in acceleration_rows), ZERO
    )
    repeat_qty = sum(
        (_decimal(row.get("repeat_acceleration_order_qty")) for row in acceleration_rows), ZERO
    )
    repeated_lines = sum(
        int(row.get("repeat_acceleration_order_lines") or 0) for row in acceleration_rows
    )
    cap_reduction = sum(
        (_decimal(row.get("acceleration_shortage_cap_reduction_qty")) for row in sku_rows), ZERO
    )
    all_forecasts = [row for row in forecast_rows if row["demand_pattern_preperiod"] == "all"]
    best_forecast = min(all_forecasts, key=lambda row: _decimal(row["wape"]))
    current_forecast = next(row for row in all_forecasts if row["forecast_model"] == "current_rate")
    tsb_no_history = next(
        row
        for row in forecast_rows
        if row["demand_pattern_preperiod"] == "no_history" and row["forecast_model"] == "tsb"
    )
    current_no_history = next(
        row
        for row in forecast_rows
        if row["demand_pattern_preperiod"] == "no_history"
        and row["forecast_model"] == "current_rate"
    )
    pattern_counts: dict[str, int] = defaultdict(int)
    for row in sku_rows:
        pattern_counts[_clean(row["demand_pattern_preperiod"])] += 1
    classified = sum(
        pattern_counts[name] for name in ("smooth", "intermittent", "erratic", "lumpy")
    )
    non_smooth = sum(pattern_counts[name] for name in ("intermittent", "erratic", "lumpy"))
    period_delta = {
        row["period"]: row
        for row in period_rows
        if row["scenario_role"] == "hypothesis_minus_control"
    }
    concentration_100 = next(row for row in concentration_rows if row["top_n"] == 100)
    p90_coverage = {
        row["demand_pattern_preperiod"]: row
        for row in coverage_rows
        if row["coverage_target"] == "empirical_p90"
    }
    segment_index = {(row["segment_dimension"], row["segment_value"]): row for row in segment_rows}
    mechanism_index = {row["mechanism_group"]: row for row in mechanism_rows}
    source_quality_rows = _read_csv(preflight_dir / "source-quality.csv")
    source_cohort_sku_count = int(
        _decimal(
            next(
                row["value"]
                for row in source_quality_rows
                if row["check"] == "source_count_cohort_sku_count"
            )
        )
    )
    persisted_cap_reduction = _decimal(
        persisted_hypothesis["acceleration_shortage_cap_reduction_qty"]
    )
    summary = {
        "schema": "display_auto_order_quick_backtest_diagnostics.v1",
        "source_preflight_manifest_sha256": _sha256(preflight_dir / "run-manifest.json"),
        "source_quick_summary_sha256": _sha256(quick_result_dir / "frozen-summary.json"),
        "date_from": inputs["date_from"].isoformat(),
        "date_to": inputs["date_to"].isoformat(),
        "source_cohort_sku_count": source_cohort_sku_count,
        "simulated_sku_count": len(sku_rows),
        "diagnostic_only": True,
        "production_authorized": False,
        "pdf_created": False,
        "scenario_ids": {
            "control": control.scenario.scenario_id,
            "hypothesis": hypothesis.scenario.scenario_id,
        },
        "headline": {
            "served_observed_delta_qty": str(
                _decimal(hypothesis_summary["served_observed_qty"])
                - _decimal(control_summary["served_observed_qty"])
            ),
            "gross_profit_delta_rub": str(
                _decimal(hypothesis_summary["gross_profit_rub"])
                - _decimal(control_summary["gross_profit_rub"])
            ),
            "capital_delta_rub": str(
                _decimal(hypothesis_summary["average_inventory_value_rub"])
                - _decimal(control_summary["average_inventory_value_rub"])
            ),
            "ending_inventory_delta_qty": str(
                _decimal(hypothesis_summary["ending_inventory_qty"])
                - _decimal(control_summary["ending_inventory_qty"])
            ),
            "economic_delta_rub": str(
                _decimal(hypothesis_summary["economic_contribution_rub"])
                - _decimal(control_summary["economic_contribution_rub"])
            ),
            "first_acceleration_order_qty": str(first_qty),
            "repeat_acceleration_order_lines": repeated_lines,
            "repeat_acceleration_order_qty": str(repeat_qty),
            "repeat_acceleration_order_qty_share": str(
                repeat_qty / (first_qty + repeat_qty) if first_qty + repeat_qty > ZERO else ZERO
            ),
            "shortage_cap_reduction_qty": str(persisted_cap_reduction),
            "recorded_detail_shortage_cap_reduction_qty": str(cap_reduction),
        },
        "demand_patterns": {
            "counts": dict(sorted(pattern_counts.items())),
            "classified_sku_count": classified,
            "non_smooth_sku_count": non_smooth,
            "history_gap_sku_count": (
                pattern_counts["no_history"] + pattern_counts["insufficient_history"]
            ),
            "non_smooth_share": str(
                Decimal(non_smooth) / Decimal(classified) if classified else ZERO
            ),
        },
        "forecast_recommendation": {
            "best_model": best_forecast["forecast_model"],
            "best_wape": best_forecast["wape"],
            "current_wape": current_forecast["wape"],
            "tsb_no_history_wape": tsb_no_history["wape"],
            "current_no_history_wape": current_no_history["wape"],
        },
        "coverage_recommendation": {
            "p90_all_service": p90_coverage["all"]["empirical_service_frequency"],
            "p90_intermittent_service": p90_coverage["intermittent"]["empirical_service_frequency"],
            "p90_lumpy_service": p90_coverage["lumpy"]["empirical_service_frequency"],
        },
        "segment_highlights": {
            "expensive_economic_delta_rub": segment_index[("cost_band", ">=3000")][
                "economic_contribution_delta_to_control_rub"
            ],
            "medium_lead_confidence_economic_delta_rub": segment_index[
                ("lead_time_confidence_start", "medium")
            ]["economic_contribution_delta_to_control_rub"],
        },
        "mechanism_highlights": {
            "repeat_group_sku_count": mechanism_index["has_repeat_orders"]["sku_count"],
            "repeat_group_economic_delta_rub": mechanism_index["has_repeat_orders"][
                "economic_contribution_delta_to_control_rub"
            ],
            "repeat_group_capital_delta_rub": mechanism_index["has_repeat_orders"][
                "capital_delta_to_control_rub"
            ],
            "repeat_group_served_observed_delta_qty": mechanism_index["has_repeat_orders"][
                "served_observed_delta_to_control_qty"
            ],
            "repeat_group_share_of_net_loss": str(
                abs(
                    _decimal(
                        mechanism_index["has_repeat_orders"][
                            "economic_contribution_delta_to_control_rub"
                        ]
                    )
                )
                / abs(
                    _decimal(hypothesis_summary["economic_contribution_rub"])
                    - _decimal(control_summary["economic_contribution_rub"])
                )
            ),
            "repeat_group_share_of_capital_delta": str(
                _decimal(mechanism_index["has_repeat_orders"]["capital_delta_to_control_rub"])
                / (
                    _decimal(hypothesis_summary["average_inventory_value_rub"])
                    - _decimal(control_summary["average_inventory_value_rub"])
                )
            ),
        },
        "concentration": {
            "top_100_share": concentration_100["share_of_gross_negative_contribution"],
            "gross_negative_contribution_rub": concentration_100[
                "all_gross_negative_contribution_rub"
            ],
        },
        "periods": {
            "pre_july_economic_delta_rub": period_delta["pre_july"]["economic_contribution_rub"],
            "july_economic_delta_rub": period_delta["july"]["economic_contribution_rub"],
        },
        "replay_checks_delta_to_persisted_hypothesis": replay_checks,
        "data_quality": {
            "source_cohort_sku_count": source_cohort_sku_count,
            "simulated_sku_count": len(sku_rows),
            "missing_from_simulation_count": source_cohort_sku_count - len(sku_rows),
            "simulation_coverage": str(
                Decimal(len(sku_rows)) / Decimal(source_cohort_sku_count)
                if source_cohort_sku_count
                else ZERO
            ),
        },
        "method": {
            "demand_pattern": "52 completed pre-period weekly buckets; ADI 1.32 and CV2 0.49",
            "forecast_benchmark": "scheduled sale-stage decisions; completed history only; next seven days fully observed",
            "economic_contribution": "gross profit minus average inventory value times annual capital+storage+obsolescence rate prorated to period",
            "acceleration_orders": "when present, acceleration_order_component_qty identifies the causal single-open component; older runs fall back to the full recommended row",
            "recorded_acceleration_detail": "per-SKU detail contains persisted decision rows only; canonical cap totals come from quick-scenario-comparison.csv diagnostics",
        },
    }
    summary_path = output_dir / "analysis-summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report_path = output_dir / "diagnostic-analysis.md"
    report_path.write_text(_build_markdown(summary), encoding="utf-8")

    artifact_names = [
        "analysis-summary.json",
        "diagnostic-analysis.md",
        "sku-diagnostics.csv",
        "segment-diagnostics.csv",
        "mechanism-diagnostics.csv",
        "loss-concentration.csv",
        "top-loss-skus.csv",
        "forecast-benchmark.csv",
        "coverage-benchmark.csv",
        "period-diagnostics.csv",
    ]
    if build_notebook:
        notebook_path = _build_notebook(output_dir, summary)
        artifact_names.insert(2, notebook_path.name)
    manifest = {
        "schema": "display_auto_order_quick_backtest_diagnostics_manifest.v1",
        "diagnostic_only": True,
        "production_authorized": False,
        "pdf_created": False,
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
    parser.add_argument("--quick-result-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--auto-order-policy-json",
        type=Path,
        default=Path("config/assortment/display-auto-order-policy.json"),
    )
    parser.add_argument(
        "--scenario-config-json",
        type=Path,
        default=Path("config/assortment/display-auto-order-backtest-scenarios.json"),
    )
    parser.add_argument("--skip-notebook", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    summary = build_analysis(
        preflight_dir=args.preflight_dir,
        quick_result_dir=args.quick_result_dir,
        output_dir=args.output_dir,
        policy_json=args.auto_order_policy_json,
        scenario_config_json=args.scenario_config_json,
        build_notebook=not args.skip_notebook,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
