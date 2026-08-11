"""Build the partner-facing report payload for the frozen display backtest."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

BASE_SCENARIO_ID = (
    "grow_accel_balanced_r14_b42_x150_min2_sku50000_stage8000000_"
    "cap20_hold4_typical_kmp0_5_sitebalanced_base"
)
CONTROL_SCENARIO_ID = "grow_cap20_p90_hold4_typical_kmp0_5_sitebalanced_base"
SERVICE_FLOOR_SCENARIO_IDS = {
    "p75": ("grow_servicefloor_p75_floor75_econ90_cap20_hold4_" "typical_kmp0_5_sitebalanced_base"),
    "p90": ("grow_servicefloor_p90_floor90_econ90_cap20_hold4_" "typical_kmp0_5_sitebalanced_base"),
    "p90_budget": BASE_SCENARIO_ID,
}
ACCELERATION_SCENARIO_IDS = {
    "fast": (
        "grow_accel_fast_r7_b28_x150_min2_sku50000_stage8000000_"
        "cap20_hold4_typical_kmp0_5_sitebalanced_base"
    ),
    "balanced": BASE_SCENARIO_ID,
    "strict": (
        "grow_accel_strict_r14_b42_x200_min3_sku50000_stage8000000_"
        "cap20_hold4_typical_kmp0_5_sitebalanced_base"
    ),
}
ACTIVE_STAGE_LABELS = {
    "new_item": "Завезли / Новинка",
    "sales_start": "Пошли продажи",
    "sale": "Растим",
    "working": "Поддерживаем",
}
MONTH_LABELS = {
    "2026-02": "Февраль",
    "2026-03": "Март",
    "2026-04": "Апрель",
    "2026-05": "Май",
    "2026-06": "Июнь",
    "2026-07": "Июль",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(_clean(value) or "0")
    except (ArithmeticError, ValueError):
        return Decimal("0")


def _float(value: Any) -> float:
    return float(_decimal(value))


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _iter_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def _pct(value: Any, digits: int = 2) -> str:
    return f"{_decimal(value) * Decimal('100'):.{digits}f}%".replace(".", ",")


def _pp(value: Any, digits: int = 3) -> str:
    number = f"{_decimal(value) * Decimal('100'):+.{digits}f}".replace(".", ",")
    return f"{number} п.п."


def _number(value: Any, digits: int = 0, *, signed: bool = False) -> str:
    sign = "+" if signed else ""
    return f"{_decimal(value):{sign}.{digits}f}".replace(".", ",")


def _money_m(value: Any, digits: int = 2) -> str:
    return f"{_decimal(value) / Decimal('1000000'):.{digits}f} млн ₽".replace(".", ",")


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    return numerator / denominator if denominator else Decimal("0")


def _shorten(value: str, limit: int = 72) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def build_analysis(preflight_dir: Path, backtest_dir: Path) -> dict[str, Any]:
    frozen = json.loads((backtest_dir / "frozen-summary.json").read_text(encoding="utf-8"))
    preflight = json.loads((preflight_dir / "run-manifest.json").read_text(encoding="utf-8"))
    actual = frozen["base_actual"]
    model = frozen["base_model"]
    control_actual = frozen["control_actual"]
    control_model = frozen["control_model"]
    if frozen["base_scenario_id"] != BASE_SCENARIO_ID:
        raise ValueError(f"unexpected base scenario: {frozen['base_scenario_id']}")

    stage_source = _read_csv(backtest_dir / "frozen-baseline-stage.csv")
    stage_pairs: dict[str, dict[str, Mapping[str, str]]] = defaultdict(dict)
    for row in stage_source:
        if row.get("scenario_id") == BASE_SCENARIO_ID:
            stage_pairs[row["status"]][row["strategy"]] = row
    stages: list[dict[str, Any]] = []
    for status, label in ACTIVE_STAGE_LABELS.items():
        pair = stage_pairs.get(status, {})
        actual_stage = pair.get("actual")
        model_stage = pair.get("model")
        if not actual_stage or not model_stage:
            continue
        stages.append(
            {
                "status": status,
                "stage_label": label,
                "potential_demand_qty": _float(model_stage["potential_demand_qty"]),
                "model_observed_fill_rate": _float(model_stage["observed_fill_rate"]),
                "model_hidden_fill_rate": _float(model_stage["hidden_fill_rate"]),
                "additional_lost_qty": _float(model_stage["lost_qty"])
                - _float(actual_stage["lost_qty"]),
                "additional_lost_observed_qty": _float(model_stage["lost_observed_qty"])
                - _float(actual_stage["lost_observed_qty"]),
                "gross_profit_delta_rub": _float(model_stage["gross_profit_rub"])
                - _float(actual_stage["gross_profit_rub"]),
                "capital_delta_rub": _float(model_stage["average_inventory_value_rub"])
                - _float(actual_stage["average_inventory_value_rub"]),
                "manual_order_lines": int(model_stage["manual_order_lines"]),
                "manual_review_created": int(model_stage["manual_review_created"]),
                "manual_review_updated": int(model_stage["manual_review_updated"]),
            }
        )

    monthly_raw: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for row in _read_csv(backtest_dir / "frozen-baseline-daily.csv"):
        if row.get("scenario_id") != BASE_SCENARIO_ID:
            continue
        month = row["business_date"][:7]
        for field in (
            "actual_observed_demand_qty",
            "actual_served_observed_qty",
            "actual_hidden_demand_qty",
            "actual_served_hidden_qty",
            "model_served_observed_qty",
            "model_served_hidden_qty",
            "model_lost_observed_qty",
            "model_lost_hidden_qty",
        ):
            monthly_raw[month][field] += _decimal(row.get(field))
    monthly: list[dict[str, Any]] = []
    monthly_chart: list[dict[str, Any]] = []
    for month in sorted(monthly_raw):
        row = monthly_raw[month]
        actual_observed_fill = _ratio(
            row["actual_served_observed_qty"], row["actual_observed_demand_qty"]
        )
        model_observed_fill = _ratio(
            row["model_served_observed_qty"], row["actual_observed_demand_qty"]
        )
        actual_hidden_fill = _ratio(
            row["actual_served_hidden_qty"], row["actual_hidden_demand_qty"]
        )
        model_hidden_fill = _ratio(row["model_served_hidden_qty"], row["actual_hidden_demand_qty"])
        monthly.append(
            {
                "month": month,
                "month_label": MONTH_LABELS.get(month, month),
                "actual_observed_fill_rate": float(actual_observed_fill),
                "model_observed_fill_rate": float(model_observed_fill),
                "actual_hidden_fill_rate": float(actual_hidden_fill),
                "model_hidden_fill_rate": float(model_hidden_fill),
                "model_lost_observed_qty": float(row["model_lost_observed_qty"]),
                "model_lost_hidden_qty": float(row["model_lost_hidden_qty"]),
            }
        )
        for strategy, fill in (
            ("Фактическая стратегия", actual_observed_fill),
            ("Модель", model_observed_fill),
        ):
            monthly_chart.append(
                {
                    "month": month,
                    "month_label": MONTH_LABELS.get(month, month),
                    "strategy_label": strategy,
                    "observed_fill_rate": float(fill),
                }
            )

    scenarios: list[dict[str, Any]] = []
    scenario_chart: list[dict[str, Any]] = []
    for row in _read_csv(backtest_dir / "frozen-scenario-summary.csv"):
        if row.get("strategy") != "model":
            continue
        order_lines = _decimal(row.get("order_lines"))
        item = {
            "scenario_id": row["scenario_id"],
            "stage_profile": row["stage_profile"],
            "profile_label": {
                "conservative": "Осторожный",
                "typical": "Типичный",
                "service": "Сервисный",
                "legacy": "Legacy",
            }.get(row["stage_profile"], row["stage_profile"]),
            "kmp4_weight": _float(row["kmp4_weight"]),
            "kmp4_label": f"КМП4 × {row['kmp4_weight'].replace('.', ',')}",
            "site_profile": row.get("site_profile") or "off",
            "site_profile_label": {
                "off": "Сайт выключен",
                "order_only": "Только заказы",
                "cautious": "Осторожный",
                "balanced": "Сбалансированный",
                "service": "Сервисный",
            }.get(row.get("site_profile") or "off", row.get("site_profile") or "off"),
            "site_order_weight": _float(row.get("site_order_weight")),
            "site_unordered_cart_weight": _float(row.get("site_unordered_cart_weight")),
            "grow_weekly_reduction_cap": _float(row.get("grow_weekly_reduction_cap")),
            "forecast_error_percentile": _float(row.get("forecast_error_percentile")),
            "grow_entry_protection_weeks": int(_decimal(row.get("grow_entry_protection_weeks"))),
            "grow_service_floor_percentile": _float(row.get("grow_service_floor_percentile")),
            "grow_service_floor_sku_cap_rub": _float(row.get("grow_service_floor_sku_cap_rub")),
            "grow_service_floor_stage_budget_rub": _float(
                row.get("grow_service_floor_stage_budget_rub")
            ),
            "grow_acceleration_profile": row.get("grow_acceleration_profile") or "off",
            "grow_acceleration_recent_days": int(
                _decimal(row.get("grow_acceleration_recent_days"))
            ),
            "grow_acceleration_baseline_days": int(
                _decimal(row.get("grow_acceleration_baseline_days"))
            ),
            "grow_acceleration_min_recent_sales": _float(
                row.get("grow_acceleration_min_recent_sales")
            ),
            "grow_acceleration_rate_multiplier": _float(
                row.get("grow_acceleration_rate_multiplier")
            ),
            "grow_acceleration_sku_cap_rub": _float(row.get("grow_acceleration_sku_cap_rub")),
            "grow_acceleration_stage_budget_rub": _float(
                row.get("grow_acceleration_stage_budget_rub")
            ),
            "grow_acceleration_medium_pipeline_fraction": _float(
                row.get("grow_acceleration_medium_pipeline_fraction")
            ),
            "grow_acceleration_low_pipeline_fraction": _float(
                row.get("grow_acceleration_low_pipeline_fraction")
            ),
            "holding_cost_scenario": row["holding_cost_scenario"],
            "total_fill_rate": _float(row["fill_rate"]),
            "observed_fill_rate": _float(row["observed_fill_rate"]),
            "hidden_fill_rate": _float(row["hidden_fill_rate"]),
            "served_qty": _float(row["served_qty"]),
            "served_observed_qty": _float(row["served_observed_qty"]),
            "served_hidden_qty": _float(row["served_hidden_qty"]),
            "gross_profit_rub": _float(row["gross_profit_rub"]),
            "average_inventory_value_rub": _float(row["average_inventory_value_rub"]),
            "gross_profit_delta_rub": _float(row["gross_profit_delta_rub"]),
            "capital_delta_rub": _float(row["capital_delta_rub"]),
            "economic_contribution_rub": _float(row["economic_contribution_rub"]),
            "economic_contribution_delta_rub": _float(row["economic_contribution_delta_rub"]),
            "gmroi_annualized": _float(row["gmroi_annualized"]),
            "manual_order_share": (
                _float(row["manual_order_lines"]) / float(order_lines) if order_lines else 0.0
            ),
            "safety_stock_units_ordered": _float(row["safety_stock_units_ordered"]),
            "order_qty": _float(row["order_qty"]),
            "ending_inventory_qty": _float(row["ending_inventory_qty"]),
            "manual_order_lines": int(_decimal(row.get("manual_order_lines"))),
            "manual_review_created": int(_decimal(row.get("manual_review_created"))),
            "manual_review_updated": int(_decimal(row.get("manual_review_updated"))),
            "acceptance_passed": _clean(row.get("acceptance_passed")) == "1",
        }
        scenarios.append(item)
        comparison_labels = {
            SERVICE_FLOOR_SCENARIO_IDS["p75"]: "P75 без лимитов",
            SERVICE_FLOOR_SCENARIO_IDS["p90"]: "P90 без лимитов",
            SERVICE_FLOOR_SCENARIO_IDS["p90_budget"]: "P90 + лимиты",
            ACCELERATION_SCENARIO_IDS["fast"]: "Ускорение: быстрое",
            ACCELERATION_SCENARIO_IDS["balanced"]: "Ускорение: баланс",
            ACCELERATION_SCENARIO_IDS["strict"]: "Ускорение: строгое",
        }
        if item["scenario_id"] in comparison_labels:
            item["scenario_label"] = comparison_labels[item["scenario_id"]]
            scenario_chart.append(item)
    control_scenario = next(row for row in scenarios if row["scenario_id"] == CONTROL_SCENARIO_ID)
    base_scenario = next(row for row in scenarios if row["scenario_id"] == BASE_SCENARIO_ID)
    for row in scenario_chart:
        row["observed_fill_delta_vs_control"] = (
            row["observed_fill_rate"] - control_scenario["observed_fill_rate"]
        )
        row["served_observed_delta_vs_control_qty"] = (
            row["served_observed_qty"] - control_scenario["served_observed_qty"]
        )
        row["served_delta_vs_control_qty"] = row["served_qty"] - control_scenario["served_qty"]
        row["gross_profit_delta_vs_control_rub"] = (
            row["gross_profit_delta_rub"] - control_scenario["gross_profit_delta_rub"]
        )
        row["capital_delta_vs_control_rub"] = (
            row["capital_delta_rub"] - control_scenario["capital_delta_rub"]
        )
        row["capital_delta_vs_control_million_rub"] = (
            row["capital_delta_vs_control_rub"] / 1_000_000
        )
        row["observed_fill_delta_vs_control_pp"] = row["observed_fill_delta_vs_control"] * 100
    p75_scenario = next(
        row for row in scenario_chart if row["scenario_id"] == SERVICE_FLOOR_SCENARIO_IDS["p75"]
    )
    p90_scenario = next(
        row for row in scenario_chart if row["scenario_id"] == SERVICE_FLOOR_SCENARIO_IDS["p90"]
    )
    p90_budget_scenario = next(
        row
        for row in scenario_chart
        if row["scenario_id"] == SERVICE_FLOOR_SCENARIO_IDS["p90_budget"]
    )
    acceleration_scenarios = {
        profile: next(row for row in scenario_chart if row["scenario_id"] == scenario_id)
        for profile, scenario_id in ACCELERATION_SCENARIO_IDS.items()
    }
    acceleration_acceptance = {
        "evaluated_count": len(acceleration_scenarios),
        "passed_count": sum(
            row["acceptance_passed"] for row in acceleration_scenarios.values()
        ),
        "passing_profiles": sorted(
            profile
            for profile, row in acceleration_scenarios.items()
            if row["acceptance_passed"]
        ),
    }
    best_service = max(scenario_chart, key=lambda row: row["observed_fill_rate"])
    best_economic = max(scenario_chart, key=lambda row: row["economic_contribution_rub"])
    comparison_specs = [
        ("Фактический запас", actual),
        ("Прежняя модель", control_model),
        ("Сервисный P75", p75_scenario),
        ("Сервисный P90", p90_scenario),
        ("P90 + лимиты", p90_budget_scenario),
        ("Ускорение: быстрое", acceleration_scenarios["fast"]),
        ("Ускорение: баланс", acceleration_scenarios["balanced"]),
        ("Ускорение: строгое", acceleration_scenarios["strict"]),
    ]
    scenario_comparison: list[dict[str, Any]] = []
    seen_comparisons: set[str] = set()
    for label, row in comparison_specs:
        scenario_id = _clean(row.get("scenario_id")) or "actual"
        comparison_key = f"{label}:{scenario_id}"
        if comparison_key in seen_comparisons:
            continue
        seen_comparisons.add(comparison_key)
        scenario_comparison.append(
            {
                "scenario_label": label,
                "scenario_id": scenario_id,
                "observed_fill_rate": _float(row["observed_fill_rate"]),
                "gross_profit_rub": _float(row["gross_profit_rub"]),
                "average_inventory_value_rub": _float(row["average_inventory_value_rub"]),
                "economic_contribution_rub": _float(row["economic_contribution_rub"]),
                "served_observed_qty": _float(row["served_observed_qty"]),
                "served_observed_delta_vs_control_qty": _float(row["served_observed_qty"])
                - _float(control_model["served_observed_qty"]),
            }
        )

    period_rows = _read_csv(backtest_dir / "frozen-baseline-period.csv")

    names: dict[str, str] = {}
    for row in _read_csv(preflight_dir / "decision-inputs.csv"):
        names.setdefault(row["nomenclature_code"], row.get("name", ""))
    sku_rows = _read_csv(backtest_dir / "frozen-baseline-sku.csv")
    sku_rows.sort(key=lambda row: _decimal(row["gross_profit_delta_rub"]))
    negative_gp_total = -sum(
        (min(Decimal("0"), _decimal(row["gross_profit_delta_rub"])) for row in sku_rows),
        Decimal("0"),
    )
    top_skus: list[dict[str, Any]] = []
    for rank, row in enumerate(sku_rows[:10], start=1):
        top_skus.append(
            {
                "rank": rank,
                "code": row["nomenclature_code"],
                "name": _shorten(names.get(row["nomenclature_code"], "")),
                "model_lost_observed_qty": _float(row["model_lost_observed_qty"]),
                "model_lost_hidden_qty": _float(row["model_lost_hidden_qty"]),
                "gross_profit_delta_rub": _float(row["gross_profit_delta_rub"]),
                "capital_delta_rub": _float(row["capital_delta_rub"]),
            }
        )
    top10_loss = -sum((min(0.0, row["gross_profit_delta_rub"]) for row in top_skus), 0.0)

    trigger_counts: dict[str, int] = defaultdict(int)
    protection_reason_counts: dict[str, int] = defaultdict(int)
    protection_skus: dict[str, set[str]] = defaultdict(set)
    percentile_binding_rows = 0
    economic_binding_rows = 0
    service_floor_recalculation_count = 0
    service_floor_limited_count = 0
    service_floor_requested_qty = Decimal("0")
    service_floor_sku_capped_qty = Decimal("0")
    service_floor_allocated_qty = Decimal("0")
    service_floor_unfunded_qty = Decimal("0")
    service_floor_effective_unfunded_qty = Decimal("0")
    service_floor_skus: set[str] = set()
    service_floor_limited_skus: set[str] = set()
    acceleration_triggered_count = 0
    acceleration_recalculation_count = 0
    acceleration_limited_count = 0
    acceleration_requested_qty = Decimal("0")
    acceleration_allocated_qty = Decimal("0")
    acceleration_unfunded_qty = Decimal("0")
    acceleration_skus: set[str] = set()
    acceleration_limited_skus: set[str] = set()
    for row in _iter_csv(backtest_dir / "frozen-baseline-decisions.csv"):
        if row.get("scenario_id") != BASE_SCENARIO_ID:
            continue
        if _decimal(row.get("recommended_order_qty")) > 0:
            trigger_counts[row.get("decision_trigger") or "unknown"] += 1
        if row.get("status") != "sale":
            continue
        requested_qty = _decimal(row.get("service_floor_requested_qty"))
        if requested_qty > 0:
            service_floor_recalculation_count += 1
            service_floor_skus.add(row["nomenclature_code"])
            service_floor_requested_qty += requested_qty
            service_floor_sku_capped_qty += _decimal(row.get("service_floor_sku_capped_qty"))
            service_floor_allocated_qty += _decimal(row.get("service_floor_allocated_qty"))
            service_floor_unfunded_qty += _decimal(row.get("service_floor_unfunded_qty"))
            service_floor_effective_unfunded_qty += _decimal(
                row.get("service_floor_effective_unfunded_qty")
            )
            if _decimal(row.get("service_floor_budget_limited")) > 0:
                service_floor_limited_count += 1
                service_floor_limited_skus.add(row["nomenclature_code"])
        if _decimal(row.get("acceleration_triggered")) > 0:
            acceleration_triggered_count += 1
            acceleration_skus.add(row["nomenclature_code"])
        acceleration_requested = _decimal(row.get("acceleration_requested_qty"))
        if acceleration_requested > 0:
            acceleration_recalculation_count += 1
            acceleration_requested_qty += acceleration_requested
            acceleration_allocated_qty += _decimal(row.get("acceleration_allocated_qty"))
            acceleration_unfunded_qty += _decimal(row.get("acceleration_unfunded_qty"))
            if _decimal(row.get("acceleration_budget_limited")) > 0:
                acceleration_limited_count += 1
                acceleration_limited_skus.add(row["nomenclature_code"])
        reason = row.get("grow_protection_reason") or "none"
        protection_reason_counts[reason] += 1
        protection_skus[reason].add(row["nomenclature_code"])
        percentile_qty = _decimal(row.get("forecast_error_percentile_qty"))
        economic_cap = _decimal(row.get("economic_safety_cap_qty"))
        if percentile_qty < economic_cap:
            percentile_binding_rows += 1
        elif economic_cap > 0 and economic_cap <= percentile_qty:
            economic_binding_rows += 1

    entered_sale_codes: set[str] = set()
    sale_codes: set[str] = set()
    sale_demand_by_code: dict[str, Decimal] = defaultdict(Decimal)
    for row in _iter_csv(preflight_dir / "daily-facts.csv"):
        if row.get("status") != "sale":
            continue
        code = row["nomenclature_code"]
        sale_codes.add(code)
        sale_demand_by_code[code] += _decimal(row.get("observed_sales_qty"))
        if row.get("previous_status") and row.get("previous_status") != "sale":
            entered_sale_codes.add(code)
    entered_sale_demand = sum(
        (sale_demand_by_code[code] for code in entered_sale_codes), Decimal("0")
    )
    total_sale_demand = sum(sale_demand_by_code.values(), Decimal("0"))

    quality = {
        row["check"]: {
            "status": row["status"],
            "severity": row["severity"],
            "value": row["value"],
            "note": row["note"],
        }
        for row in _read_csv(preflight_dir / "source-quality.csv")
    }

    extra_lost_total = _decimal(model["lost_observed_qty"]) - _decimal(actual["lost_observed_qty"])
    sale_extra_lost = next(
        (
            _decimal(str(row["additional_lost_observed_qty"]))
            for row in stages
            if row["status"] == "sale"
        ),
        Decimal("0"),
    )
    order_lines = _decimal(model["order_lines"])
    manual_share = _ratio(_decimal(model["manual_order_lines"]), order_lines)
    return {
        "schema": "display_auto_order_frozen_report_analysis.v4",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "period": {"date_from": frozen["date_from"], "date_to": frozen["date_to"]},
        "cohort": {
            "sku_count": int(quality["source_count_cohort_sku_count"]["value"]),
            "classification_run_id": preflight["classification_run_id"],
            "decision_input_rows": preflight["row_counts"]["decision_inputs"],
        },
        "preflight_directory_name": preflight_dir.name,
        "acceptance": frozen["acceptance"],
        "protective_scenario_acceptance": frozen["protective_scenario_acceptance"],
        "actual": actual,
        "model": model,
        "control_actual": control_actual,
        "control_model": control_model,
        "control_scenario": control_scenario,
        "base_scenario": base_scenario,
        "best_service_scenario": best_service,
        "best_economic_scenario": best_economic,
        "p75_scenario": p75_scenario,
        "p90_scenario": p90_scenario,
        "p90_budget_scenario": p90_budget_scenario,
        "acceleration_scenarios": acceleration_scenarios,
        "acceleration_acceptance": acceleration_acceptance,
        "headline": {
            "observed_fill_rate": _float(model["observed_fill_rate"]),
            "observed_fill_delta": _float(model["observed_fill_rate"])
            - _float(actual["observed_fill_rate"]),
            "hidden_fill_rate": _float(model["hidden_fill_rate"]),
            "hidden_fill_delta": _float(model["hidden_fill_rate"])
            - _float(actual["hidden_fill_rate"]),
            "gross_profit_delta_rub": _float(model["gross_profit_delta_rub"]),
            "capital_delta_rub": _float(model["capital_delta_rub"]),
            "economic_contribution_delta_rub": _float(model["economic_contribution_delta_rub"]),
            "observed_fill_improvement_vs_control": _float(model["observed_fill_rate"])
            - _float(control_model["observed_fill_rate"]),
            "gross_profit_improvement_vs_control_rub": _float(model["gross_profit_rub"])
            - _float(control_model["gross_profit_rub"]),
            "capital_increase_vs_control_rub": _float(model["average_inventory_value_rub"])
            - _float(control_model["average_inventory_value_rub"]),
            "best_service_fill_improvement_vs_control": best_service["observed_fill_rate"]
            - control_scenario["observed_fill_rate"],
            "best_service_profit_improvement_vs_control_rub": best_service[
                "gross_profit_delta_vs_control_rub"
            ],
            "best_service_capital_increase_vs_control_rub": best_service[
                "capital_delta_vs_control_rub"
            ],
            "manual_order_share": float(manual_share),
            "manual_review_created": int(model["manual_review_created"]),
            "manual_review_updated": int(model["manual_review_updated"]),
            "manual_review_creation_reduction": 1
            - int(model["manual_review_created"]) / max(1, int(model["manual_order_lines"])),
            "extra_lost_total_qty": float(extra_lost_total),
            "sale_extra_lost_qty": float(sale_extra_lost),
            "sale_extra_lost_share": float(_ratio(sale_extra_lost, extra_lost_total)),
            "top10_negative_gp_share": float(
                Decimal(str(top10_loss)) / negative_gp_total if negative_gp_total else Decimal("0")
            ),
            "hidden_kmp4_qty": _float(model.get("hidden_kmp4_qty")),
            "hidden_site_order_qty": _float(model.get("hidden_site_order_qty")),
            "hidden_site_cart_qty": _float(model.get("hidden_site_cart_qty")),
            "hidden_reserve_backlog_qty": _float(model.get("hidden_reserve_backlog_qty")),
            "entered_sale_demand_share": float(_ratio(entered_sale_demand, total_sale_demand)),
            "sale_sku_count": len(sale_codes),
            "entered_sale_sku_count": len(entered_sale_codes),
            "economic_safety_binding_share": economic_binding_rows
            / max(1, economic_binding_rows + percentile_binding_rows),
            "service_floor_recalculation_count": service_floor_recalculation_count,
            "service_floor_limited_count": service_floor_limited_count,
            "service_floor_limited_share": service_floor_limited_count
            / max(1, service_floor_recalculation_count),
            "service_floor_sku_count": len(service_floor_skus),
            "service_floor_limited_sku_count": len(service_floor_limited_skus),
            "service_floor_requested_qty": float(service_floor_requested_qty),
            "service_floor_sku_capped_qty": float(service_floor_sku_capped_qty),
            "service_floor_allocated_qty": float(service_floor_allocated_qty),
            "service_floor_unfunded_qty": float(service_floor_unfunded_qty),
            "service_floor_effective_unfunded_qty": float(service_floor_effective_unfunded_qty),
            "service_floor_allocated_share": float(
                _ratio(service_floor_allocated_qty, service_floor_requested_qty)
            ),
            "p90_incremental_served_observed_qty": p90_scenario[
                "served_observed_delta_vs_control_qty"
            ],
            "p90_incremental_served_total_qty": p90_scenario["served_delta_vs_control_qty"],
            "p90_incremental_order_qty": p90_scenario["order_qty"] - control_scenario["order_qty"],
            "p90_incremental_ending_inventory_qty": p90_scenario["ending_inventory_qty"]
            - control_scenario["ending_inventory_qty"],
            "p90_budget_service_recovery_share": (
                p90_budget_scenario["served_observed_delta_vs_control_qty"]
                / p90_scenario["served_observed_delta_vs_control_qty"]
                if p90_scenario["served_observed_delta_vs_control_qty"]
                else 0.0
            ),
            "acceleration_triggered_decision_count": acceleration_triggered_count,
            "acceleration_recalculation_count": acceleration_recalculation_count,
            "acceleration_limited_count": acceleration_limited_count,
            "acceleration_limited_share": acceleration_limited_count
            / max(1, acceleration_recalculation_count),
            "acceleration_sku_count": len(acceleration_skus),
            "acceleration_limited_sku_count": len(acceleration_limited_skus),
            "acceleration_requested_qty": float(acceleration_requested_qty),
            "acceleration_allocated_qty": float(acceleration_allocated_qty),
            "acceleration_unfunded_qty": float(acceleration_unfunded_qty),
            "acceleration_allocated_share": float(
                _ratio(acceleration_allocated_qty, acceleration_requested_qty)
            ),
        },
        "stages": stages,
        "monthly": monthly,
        "monthly_chart": monthly_chart,
        "scenarios": scenarios,
        "scenario_chart": scenario_chart,
        "scenario_comparison": scenario_comparison,
        "factor_effects": [],
        "period_sensitivity": period_rows,
        "top_skus": top_skus,
        "trigger_counts": dict(trigger_counts),
        "protection_reason_counts": dict(protection_reason_counts),
        "protection_sku_counts": {key: len(values) for key, values in protection_skus.items()},
        "source_quality": quality,
        "site_export": preflight.get("site_export", {}),
        "method": frozen["method"],
    }


def build_markdown(analysis: Mapping[str, Any]) -> str:
    headline = analysis["headline"]
    actual = analysis["actual"]
    control = analysis["control_model"]
    p75 = analysis["p75_scenario"]
    p90 = analysis["p90_scenario"]
    budget = analysis["p90_budget_scenario"]
    quality = analysis["source_quality"]
    passed_count = analysis["protective_scenario_acceptance"]["passed_count"]
    periods: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in analysis["period_sensitivity"]:
        periods[row["period"]][row["strategy"]] = row
    pre_july_model = periods.get("pre_july", {}).get("model", {})
    july_model = periods.get("july", {}).get("model", {})
    site_mapping = analysis.get("site_export", {}).get("mapping_stats", {})
    return f"""# Автозаказ дисплеев: сервисный P90 помогает мало и не проходит проверку

## Executive Summary

- **Правило пока нельзя включать в production.** Ни P75, ни P90, ни P90 с денежными лимитами не прошли строгую проверку (`{passed_count}/3`). Лучший вариант обслужил **{_pct(p90['observed_fill_rate'])}** записанных продаж против **{_pct(actual['observed_fill_rate'])}** у фактического запаса.
- **Полный P90 действительно вернул часть продаж, но слишком мало.** Относительно прежней модели он обслужил на {p90['served_observed_delta_vs_control_qty']:.0f} продаж больше, добавил {_money_m(p90['gross_profit_delta_vs_control_rub'])} валовой прибыли и потребовал {_money_m(p90['capital_delta_vs_control_rub'])} среднего складского капитала.
- **Лимиты 50 тыс. ₽ на SKU и 8 млн ₽ на стадию урезали примерно половину сервисного эффекта.** Бюджетный P90 вернул {_number(budget['served_observed_delta_vs_control_qty'], 1)} продажи и {_money_m(budget['gross_profit_delta_vs_control_rub'])} прибыли при дополнительных {_money_m(budget['capital_delta_vs_control_rub'])} капитала.
- **P75 не оправдал дополнительный запас.** Он не улучшил сервис ({p75['served_observed_delta_vs_control_qty']:+.0f} продажи к прежней модели), хотя средний капитал вырос на {_money_m(p75['capital_delta_vs_control_rub'])}.

## Что именно проверяли

Период — с 1 февраля по 31 июля 2026 года, когорта — {analysis['cohort']['sku_count']} SKU предмета «Дисплеи». На каждую дату стадия восстановлена только по фактам продаж, без знания будущего. Сайт, КМП4 и резерв не переводят товар между стадиями, а только уточняют скрытый спрос.

Сравнивались прежняя модель и три новых правила для стадии «Растим»: обязательный P75, обязательный P90 и P90 с лимитом 50 тыс. ₽ на SKU и общим бюджетом 8 млн ₽. Во всех вариантах экономически оправданный запас мог добавляться сверху сервисного минимума. Остальные настройки были одинаковыми: типичный старт новинок, КМП4 `0,5`, сайт `balanced`, недельный пересмотр и базовая стоимость запаса.

## Полный P90 — лучший по сервису, но разрыв с фактом остаётся большим

Прежняя модель обслужила **{_pct(control['observed_fill_rate'])}** записанных продаж. Полный P90 поднял сервис до **{_pct(p90['observed_fill_rate'])}**, то есть всего на {_pp(p90['observed_fill_delta_vs_control'])} Валовая прибыль выросла с {_money_m(control['gross_profit_rub'])} до {_money_m(p90['gross_profit_rub'])}, а средний капитал — с {_money_m(control['average_inventory_value_rub'])} до {_money_m(p90['average_inventory_value_rub'])}.

До фактической стратегии всё ещё не хватает {_money_m(-p90['gross_profit_delta_rub'])} валовой прибыли и {_pp(p90['observed_fill_rate'] - _float(actual['observed_fill_rate']))} сервиса. Средний капитал при этом ниже факта на {_money_m(-p90['capital_delta_rub'])}, но GMROI тоже ниже: {_number(p90['gmroi_annualized'], 2)} против {_number(actual['gmroi_annualized'], 2)}. Поэтому строгий критерий не выполнен.

## Сравнение вариантов

| Вариант | Сервис записанных продаж | Продажи к прежней модели | Валовая прибыль к прежней модели | Средний капитал к прежней модели |
| --- | ---: | ---: | ---: | ---: |
| Прежняя модель | {_pct(control['observed_fill_rate'])} | — | — | — |
| Сервисный P75 | {_pct(p75['observed_fill_rate'])} | {_number(p75['served_observed_delta_vs_control_qty'], signed=True)} | {_money_m(p75['gross_profit_delta_vs_control_rub'])} | {_money_m(p75['capital_delta_vs_control_rub'])} |
| Сервисный P90 | {_pct(p90['observed_fill_rate'])} | {_number(p90['served_observed_delta_vs_control_qty'], signed=True)} | {_money_m(p90['gross_profit_delta_vs_control_rub'])} | {_money_m(p90['capital_delta_vs_control_rub'])} |
| P90 + лимиты | {_pct(budget['observed_fill_rate'])} | {_number(budget['served_observed_delta_vs_control_qty'], 1, signed=True)} | {_money_m(budget['gross_profit_delta_vs_control_rub'])} | {_money_m(budget['capital_delta_vs_control_rub'])} |

Ни один вариант не улучшил экономический вклад после стоимости хранения: P75 дал {_money_m(p75['economic_contribution_rub'] - analysis['control_scenario']['economic_contribution_rub'])}, полный P90 — {_money_m(p90['economic_contribution_rub'] - analysis['control_scenario']['economic_contribution_rub'])}, бюджетный P90 — {_money_m(budget['economic_contribution_rub'] - analysis['control_scenario']['economic_contribution_rub'])} к прежней модели.

## Почему обязательный запас почти не помог

1. **P90 добавил много закупки, но мало обслуженных продаж.** За период модель заказала на {_number(headline['p90_incremental_order_qty'])} единиц больше прежней. К концу теста на складе осталось на {_number(headline['p90_incremental_ending_inventory_qty'])} единиц больше, а реально обслужено лишь на {_number(headline['p90_incremental_served_total_qty'])} единиц спроса больше. Значит, правило добавляет запас слишком широко или не в тот момент.
2. **Дефицит остаётся сосредоточен в «Растим».** В бюджетном P90 эта стадия всё ещё потеряла {_number(headline['sale_extra_lost_qty'])} записанных продаж сверх факта — {_pct(headline['sale_extra_lost_share'], 1)} всего дополнительного дефицита модели.
3. **Бюджет срабатывает слишком часто.** На лимит наткнулись {_pct(headline['service_floor_limited_share'], 1)} пересчётов с положительным сервисным минимумом, или {headline['service_floor_limited_sku_count']} SKU из {headline['service_floor_sku_count']}. В сумме по пересчётам было профинансировано лишь {_pct(headline['service_floor_allocated_share'], 1)} запрошенных сервисных единиц. Это не потерянный спрос, а диагностический объём целей min/max, повторяющихся при пересмотре.
4. **Распределение по ожидаемой марже на рубль не дало ожидаемой экономической отдачи.** Бюджетный вариант сохранил {_pct(headline['p90_budget_service_recovery_share'], 1)} прироста продаж полного P90, но лишь {_pct(budget['gross_profit_delta_vs_control_rub'] / p90['gross_profit_delta_vs_control_rub'], 1)} прироста валовой прибыли. Текущий приоритет недостаточно учитывает момент поставки и вероятность продажи до конца горизонта.

## До июля и июль отдельно

- До июля общий сервис бюджетного P90: **{_pct(pre_july_model.get('fill_rate', 0))}**, валовая прибыль: **{_money_m(pre_july_model.get('gross_profit_rub', 0))}**.
- За июль общий сервис бюджетного P90: **{_pct(july_model.get('fill_rate', 0))}**, валовая прибыль: **{_money_m(july_model.get('gross_profit_rub', 0))}**.

Июльский структурный скачок событий сайта не удалён из расчёта. Он показан отдельно, чтобы не скрывать чувствительность результата к изменению структуры интернет-магазина.

## Что делать дальше

1. **Не включать production-заказы.** Оставить расчёт рекомендаций и ручную проверку.
2. **Не принимать P75.** Он увеличил капитал без роста сервиса.
3. **Не принимать широкий P90 как готовое правило.** Оставить его контрольным сценарием следующего теста, а не production-настройкой.
4. **Следующим изменением проверять не более высокий percentile, а адресность и момент заказа.** Сервисный минимум давать SKU с ускорением продаж и высокой ценой дефицита, учитывать вероятность поздней поставки и заказывать раньше, когда продажи ускоряются.
5. **Пересчитать бюджетный приоритет.** Оценивать не только ожидаемую маржу на рубль, но и вероятность, что дополнительная единица будет продана в пределах срока поставки и тестового горизонта.
6. **Пороги 50 тыс. ₽ и 8 млн ₽ пока считать экспериментальными.** Они не стали утверждёнными бизнес-лимитами.

## Открытые вопросы

- Какую полную цену дефицита считать высокой: только потерянную маржу или также риск ухода клиента и срочной закупки?
- Какие факты надёжности поставщика использовать для уменьшения доверия к товару в пути?
- Как учитывать замену одного качества или версии дисплея другим, чтобы не считать весь дефицит потерянной продажей?
- Какой денежный лимит стадии допустим бизнесу после положительного backtest?

## Ограничения

- Фактический fill rate обычных продаж равен 100% по определению: видны только состоявшиеся продажи. Незарегистрированный спрос оценивается через несколько косвенных источников.
- В frozen-файл вошло {site_mapping.get('mapped_row_count', '—')} сопоставленных строк сайта; события вне когорты не распределялись догадкой.
- Найдены {quality['negative_register_balances']['value']} отрицательных дневных балансов регистра. Они не считаются спросом и не увеличивают доступный остаток, но причина в 1С остаётся вопросом качества данных.
- В сценарии все положительные ручные рекомендации считаются принятыми. Реальный результат без этой дисциплины будет хуже.
- Production-заказы и внешние записи не создавались.
"""


def _source(source_id: str, label: str, path: str) -> dict[str, Any]:
    reader = "read_json_auto" if path.endswith(".json") else "read_csv_auto"
    sql = f"SELECT *\nFROM {reader}('{path}');"
    return {
        "id": source_id,
        "label": label,
        "path": path,
        "query": {
            "engine": "DuckDB SQL over frozen CSV/JSON artifacts",
            "language": "sql",
            "sql": sql,
            "description": "Читает сохранённый PASS preflight или frozen-backtest; отчёт агрегирует эти строки локальным детерминированным Python-кодом без обращения к БД.",
            "filters": [
                "Период 2026-02-01 — 2026-07-31",
                "Все SKU предмета Дисплеи",
                "Стадии восстановлены только по историческим продажам",
                "Сравнение контроля, P75/P90 и трёх профилей ускорения",
            ],
            "metric_definitions": [
                "Fill rate = обслуженный потенциальный спрос / потенциальный спрос.",
                "Средний капитал = средняя дневная стоимость модельного остатка.",
                "Валовая прибыль = обслуженное количество × историческая валовая маржа единицы.",
            ],
            "tables_used": [path],
        },
    }


def build_artifact(analysis: Mapping[str, Any]) -> dict[str, Any]:
    title = "Автозаказ дисплеев: сервисный P90 помогает мало"
    headline = analysis["headline"]
    p90 = analysis["p90_scenario"]
    budget = analysis["p90_budget_scenario"]
    preflight_name = analysis["preflight_directory_name"]
    sources = [
        _source("frozen_summary", "Итог frozen-backtest", "frozen-summary.json"),
        _source(
            "scenario_summary",
            "Сценарная сетка frozen-backtest",
            "frozen-scenario-summary.csv",
        ),
        _source(
            "preflight_manifest",
            "PASS preflight и контрольные суммы источников",
            f"{preflight_name}/run-manifest.json",
        ),
        _source(
            "decision_detail",
            "Детализация сервисного минимума и бюджетных ограничений",
            "frozen-baseline-decisions.csv",
        ),
        _source(
            "period_summary",
            "Результат до июля и за июль",
            "frozen-baseline-period.csv",
        ),
    ]
    headline_dataset = [
        {
            "passed_scenario_count": 0,
            "evaluated_scenario_count": 3,
            "p90_observed_fill_rate": p90["observed_fill_rate"],
            "p90_observed_fill_delta_vs_control": p90["observed_fill_delta_vs_control"],
            "p90_incremental_sales_qty": p90["served_observed_delta_vs_control_qty"],
            "p90_incremental_profit_million_rub": p90["gross_profit_delta_vs_control_rub"]
            / 1_000_000,
            "p90_incremental_capital_million_rub": p90["capital_delta_vs_control_rub"] / 1_000_000,
            "p90_economic_contribution_delta_million_rub": (
                p90["economic_contribution_rub"]
                - analysis["control_scenario"]["economic_contribution_rub"]
            )
            / 1_000_000,
        }
    ]
    cards = [
        {
            "id": "acceptance_card",
            "description": "Сколько новых сервисных вариантов выполнили все строгие условия.",
            "dataset": "headline",
            "sourceId": "frozen_summary",
            "metrics": [
                {
                    "label": "Прошли acceptance",
                    "field": "passed_scenario_count",
                    "format": "number",
                },
                {
                    "label": "проверено",
                    "field": "evaluated_scenario_count",
                    "format": "number",
                },
            ],
        },
        {
            "id": "p90_service_card",
            "description": "Доля записанных продаж полного P90 и изменение к прежней модели.",
            "dataset": "headline",
            "sourceId": "scenario_summary",
            "metrics": [
                {
                    "label": "Сервис полного P90",
                    "field": "p90_observed_fill_rate",
                    "format": "percent",
                },
                {
                    "label": "к прежней модели",
                    "field": "p90_observed_fill_delta_vs_control",
                    "format": "percent",
                    "signed": True,
                },
            ],
        },
        {
            "id": "p90_sales_card",
            "description": "Сколько записанных продаж и валовой прибыли вернул полный P90.",
            "dataset": "headline",
            "sourceId": "scenario_summary",
            "metrics": [
                {
                    "label": "Дополнительные продажи",
                    "field": "p90_incremental_sales_qty",
                    "format": "number",
                    "signed": True,
                },
                {
                    "label": "Δ валовая прибыль, млн ₽",
                    "field": "p90_incremental_profit_million_rub",
                    "format": "number",
                    "signed": True,
                },
            ],
        },
        {
            "id": "p90_capital_card",
            "description": "Цена улучшения полного P90 в среднем складском капитале.",
            "dataset": "headline",
            "sourceId": "scenario_summary",
            "metrics": [
                {
                    "label": "Дополнительный капитал, млн ₽",
                    "field": "p90_incremental_capital_million_rub",
                    "format": "number",
                    "signed": True,
                },
                {
                    "label": "Δ экономический вклад, млн ₽",
                    "field": "p90_economic_contribution_delta_million_rub",
                    "format": "number",
                    "signed": True,
                },
            ],
        },
    ]
    charts = [
        {
            "id": "scenario_comparison_chart",
            "title": "Сервис записанных продаж по вариантам",
            "subtitle": "Февраль–июль 2026 года, доля состоявшихся продаж в одинаковой когорте.",
            "showDescription": True,
            "intent": "comparison",
            "question": "Насколько сервисный минимум приблизил модель к фактическому запасу?",
            "rationale": "Пять столбцов показывают абсолютный сервис факта и сопоставимых политик.",
            "comparisonContext": {
                "baseline": "Прежняя модель и фактическая стратегия",
                "grain": "Сценарий frozen-backtest",
                "unit": "fraction",
            },
            "type": "bar",
            "dataset": "scenario_comparison",
            "sourceId": "scenario_summary",
            "encodings": {
                "x": {
                    "field": "scenario_label",
                    "type": "nominal",
                    "label": "Вариант",
                },
                "y": {
                    "field": "observed_fill_rate",
                    "type": "quantitative",
                    "label": "Обслужено записанных продаж",
                },
                "tooltip": [
                    {
                        "field": "served_observed_qty",
                        "type": "quantitative",
                        "label": "Обслужено продаж, шт.",
                    },
                    {
                        "field": "gross_profit_rub",
                        "type": "quantitative",
                        "label": "Валовая прибыль, ₽",
                    },
                ],
            },
            "valueFormat": "percent",
            "layout": "full",
            "palette": {"kind": "categorical"},
        },
    ]
    tables: list[dict[str, Any]] = []
    report_markdown = build_markdown(analysis)
    executive_summary = (
        report_markdown.split("## Executive Summary", 1)[1]
        .split("## Что именно проверяли", 1)[0]
        .strip()
    )
    blocks = [
        {"id": "title", "type": "markdown", "body": f"# {title}"},
        {
            "id": "executive_summary",
            "type": "markdown",
            "body": f"## Executive Summary\n\n{executive_summary}",
            "sourceId": "frozen_summary",
        },
        {
            "id": "headline_metrics",
            "type": "metric-strip",
            "cardIds": [row["id"] for row in cards],
        },
        {
            "id": "scope",
            "type": "markdown",
            "body": (
                "## Что именно проверяли\n\n"
                f"Период — с 1 февраля по 31 июля 2026 года, когорта — {analysis['cohort']['sku_count']} SKU предмета «Дисплеи». Стадия на каждую дату восстановлена только по продажам. Сравнивались прежняя модель, обязательный P75, обязательный P90 и P90 с лимитами 50 тыс. ₽ на SKU и 8 млн ₽ на стадию."
            ),
            "sourceId": "preflight_manifest",
        },
        {
            "id": "result_finding",
            "type": "markdown",
            "body": (
                "## Полный P90 — лучший по сервису, но не закрывает разрыв\n\n"
                f"**Он вернул {_number(p90['served_observed_delta_vs_control_qty'])} записанных продаж и {_money_m(p90['gross_profit_delta_vs_control_rub'])} валовой прибыли относительно прежней модели.** Для этого средний капитал вырос на {_money_m(p90['capital_delta_vs_control_rub'])}. До факта всё ещё не хватает {_money_m(-p90['gross_profit_delta_rub'])} прибыли."
            ),
            "sourceId": "scenario_summary",
        },
        {
            "id": "scenario_comparison_chart_block",
            "type": "chart",
            "chartId": "scenario_comparison_chart",
        },
        {
            "id": "budget_finding",
            "type": "markdown",
            "body": (
                "## Денежные лимиты сохранили лишь половину эффекта P90\n\n"
                f"**Бюджетный P90 вернул {_number(budget['served_observed_delta_vs_control_qty'], 1)} продажи против {_number(p90['served_observed_delta_vs_control_qty'])} у полного P90.** На ограничения наткнулись {_pct(headline['service_floor_limited_share'], 1)} пересчётов положительного сервисного минимума. Текущий порядок распределения бюджета не обеспечил положительного экономического вклада."
            ),
            "sourceId": "decision_detail",
        },
        {
            "id": "mechanism_finding",
            "type": "markdown",
            "body": (
                "## Дополнительный запас размещён недостаточно адресно\n\n"
                f"**Полный P90 заказал на {_number(headline['p90_incremental_order_qty'])} единиц больше, но обслужил всего на {_number(headline['p90_incremental_served_total_qty'])} единиц спроса больше.** К концу периода на складе осталось на {_number(headline['p90_incremental_ending_inventory_qty'])} единиц больше. Это означает, что один общий percentile для всей стадии добавляет запас слишком широко или слишком поздно."
            ),
            "sourceId": "scenario_summary",
        },
        {
            "id": "recommendations",
            "type": "markdown",
            "body": (
                "## Что делать дальше\n\n"
                "1. **Не включать production-заказы:** оставить рекомендации под ручным контролем.\n"
                "2. **Не принимать P75 и широкий P90 как готовое правило.**\n"
                "3. **Следующий тест направить на адресность и момент заказа:** ускорение продаж, высокая цена дефицита и риск опоздания поставщика.\n"
                "4. **Пересчитать бюджетный приоритет:** учитывать вероятность продажи дополнительной единицы до её устаревания, а не только маржу на рубль закупки.\n"
                "5. **Не утверждать 50 тыс. ₽ и 8 млн ₽ как бизнес-лимиты:** это пока параметры чувствительности."
            ),
        },
        {
            "id": "further_questions",
            "type": "markdown",
            "body": (
                "## Открытые вопросы\n\n"
                "- Как оценивать полную цену дефицита: маржа, срочная закупка и риск ухода клиента?\n"
                "- Какие факты надёжности поставщика должны уменьшать доверие к товару в пути?\n"
                "- Как учитывать замену одного качества или версии дисплея другим?\n"
                "- Какой бюджет стадии допустим после положительного backtest?"
            ),
        },
        {
            "id": "caveats",
            "type": "markdown",
            "body": (
                "## Ограничения\n\n"
                "- Фактический сервис обычных продаж равен 100% по определению: видны только состоявшиеся продажи; скрытый спрос измеряется отдельно.\n"
                "- Сайт связан с SKU только по валидному PRODUCT_XML_ID; строки вне когорты не распределялись догадкой.\n"
                f"- Предварительная проверка данных пройдена, но найдены {analysis['source_quality']['negative_register_balances']['value']} отрицательных дневных резервов; они не считаются спросом и не увеличивают остаток.\n"
                "- Все положительные ручные рекомендации считаются принятыми; без этого реальный сервис будет ниже.\n"
                "- Production-заказы и внешние записи не создавались."
            ),
            "sourceId": "preflight_manifest",
        },
    ]
    return {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": title,
            "description": "Партнёрский отчёт о следующей стадийной модели автозаказа дисплеев.",
            "generatedAt": analysis["generated_at"],
            "filters": [],
            "cards": cards,
            "charts": charts,
            "tables": tables,
            "sources": sources,
            "blocks": blocks,
        },
        "snapshot": {
            "version": 1,
            "status": "ready",
            "generatedAt": analysis["generated_at"],
            "datasets": {
                "headline": headline_dataset,
                "scenario_comparison": analysis["scenario_comparison"],
            },
            "accessIssues": [],
        },
        "sources": sources,
        "package_info": {
            "originUrl": "artifact://display-auto-order-service-floor-frozen-backtest-2026-h1"
        },
    }


def build_source_notes(analysis: Mapping[str, Any]) -> str:
    return f"""# Примечания к источникам frozen-отчёта

## Аудитория и структура

- Аудитория: партнёр и владельцы бизнес-процесса.
- Структура: заголовок; краткий вывод; сравнение сценариев; причины; рекомендации;
  открытые вопросы; ограничения.
- Формат: portable HTML как статический источник для PDF.
- Выбран delivery mode `html`, потому что пользователь запросил PDF; PDF печатается
  из того же проверенного HTML, а не строится отдельным макетом.

## Карта графиков

| Раздел | Вопрос | Тип | Данные | Подтверждаемый вывод |
| --- | --- | --- | --- | --- |
| Сравнение правил | Улучшает ли адресная защита ускоряющихся SKU сервис и экономику? | Столбцы по восьми категориям | `frozen-scenario-summary.csv` | сопоставление контроля, P90 и трёх профилей ускорения |

График использует абсолютный fill rate с нулевой шкалой, чтобы не преувеличивать
небольшую разницу между вариантами. Точные изменения продаж, прибыли и капитала
приведены рядом в тексте и метриках.

## Ограниченные разрезы

- При ускорении товар в пути учитывается на `100% / 75% / 50%` для высокой,
  средней и низкой уверенности в сроке поставки; это сценарное допущение, а не
  доказанная вероятность прихода.
- `50 000 ₽` на SKU и `8 000 000 ₽` на стадию — параметры чувствительности,
  а не утверждённые бизнес-лимиты.
- Суммы requested/allocated acceleration считаются по событиям пересчёта. Они
  диагностируют частоту адресной надбавки min/max и не являются уникальным
  количеством потерянного спроса.
- `historical-sales.csv` содержит разреженную историю продаж с 1 января 2025 года,
  поэтому окна ускорения в начале теста не принимают декабрь–январь за нулевой спрос.
- События сайта входят только при однозначной связи валидного `PRODUCT_XML_ID` с
  SKU когорты; события вне когорты не распределяются догадкой.
- `reserve_backlog` реализован и проверен, но в историческом периоде фактических
  случаев резерва сверх физического остатка не найдено.
- Отчёт использует classification run `{analysis['cohort']['classification_run_id']}`
  и SHA-256 frozen preflight manifest из `frozen-summary.json`.
"""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, required=True)
    parser.add_argument("--backtest-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    analysis = build_analysis(args.preflight_dir, args.backtest_dir)
    (args.backtest_dir / "frozen-report-analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.backtest_dir / "FROZEN-BACKTEST-DIAGNOSTIC.md").write_text(
        build_markdown(analysis), encoding="utf-8"
    )
    (args.backtest_dir / "FROZEN-REPORT-SOURCE-NOTES.md").write_text(
        build_source_notes(analysis), encoding="utf-8"
    )
    artifact = build_artifact(analysis)
    (args.backtest_dir / "artifact.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "ready",
                "analysis": str(args.backtest_dir / "frozen-report-analysis.json"),
                "markdown": str(args.backtest_dir / "FROZEN-BACKTEST-DIAGNOSTIC.md"),
                "source_notes": str(args.backtest_dir / "FROZEN-REPORT-SOURCE-NOTES.md"),
                "artifact": str(args.backtest_dir / "artifact.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
