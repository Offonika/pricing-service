"""Screen a walk-forward SKU-date shortage-risk challenger.

The task reads only frozen preflight and v18 forecast-control artifacts.  It
learns from outcome windows that were fully completed before each decision,
scores current SKU-date rows, and evaluates risk-ranked service buffers on the
existing matched case/control pairs.  It does not simulate or create orders.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from datetime import date
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence

from tasks.display_auto_order_backtest_preflight import validate_preflight_directory

ZERO = Decimal("0")
ONE = Decimal("1")
SMOOTHING = Decimal("10")
RELIABLE_BIN_COUNT = Decimal("50")
TOP_SHARES = (
    Decimal("0.05"),
    Decimal("0.10"),
    Decimal("0.20"),
    Decimal("0.30"),
    Decimal("0.50"),
)

FEATURE_COLUMNS = (
    "stock_cover",
    "position_cover",
    "pipeline_cover",
    "acceleration_30_forecast",
    "recent_30_90",
    "lead_spread",
    "lead_confidence",
    "demand_pattern",
    "sales_trend",
    "open_signal",
)


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


def _validate_forecast_analysis_directory(path: Path) -> dict[str, Any]:
    manifest_path = path / "analysis-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_files = manifest.get("files") or {}
    for name in ("decision-windows.csv", "matched-pairs.csv"):
        expected = _clean(expected_files.get(name))
        if not expected or _sha256(path / name) != expected:
            raise ValueError(f"forecast analysis checksum mismatch: {name}")
    return manifest


def _ceil(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_CEILING)


def _bucket(value: Decimal, boundaries: Sequence[Decimal]) -> str:
    for boundary in boundaries:
        if value <= boundary:
            return f"<={boundary}"
    return f">{boundaries[-1]}"


def _safe_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    return numerator / max(Decimal("0.01"), denominator)


def _feature_values(
    window: Mapping[str, Any],
    source: Mapping[str, Any],
) -> dict[str, Any]:
    forecast_qty = max(Decimal("0.01"), _decimal(window.get("forecast_demand_qty")))
    forecast_rate = max(Decimal("0.0001"), _decimal(source.get("forecast_rate_sales")))
    physical = max(ZERO, _decimal(source.get("physical_stock_qty")))
    reserve = max(ZERO, _decimal(source.get("effective_reserve_qty")))
    free_stock = max(ZERO, physical - reserve)
    free_incoming = max(ZERO, _decimal(source.get("free_incoming_qty")))
    position = max(ZERO, free_stock + free_incoming)
    sales_30_rate = max(ZERO, _decimal(source.get("sales_30"))) / Decimal("30")
    sales_90_rate = max(ZERO, _decimal(source.get("sales_90"))) / Decimal("90")
    lead_spread = max(
        ZERO,
        _decimal(source.get("lead_time_p75_days")) - _decimal(source.get("lead_time_p50_days")),
    )
    open_signal = sum(
        (
            max(ZERO, _decimal(source.get(column)))
            for column in (
                "kmp4_open_qty",
                "site_order_open_qty",
                "site_cart_open_qty",
                "reserve_backlog_open_qty",
            )
        ),
        ZERO,
    )
    stock_cover = _safe_ratio(free_stock, forecast_qty)
    position_cover = _safe_ratio(position, forecast_qty)
    pipeline_cover = _safe_ratio(free_incoming, forecast_qty)
    acceleration = _safe_ratio(sales_30_rate, forecast_rate)
    recent_ratio = _safe_ratio(sales_30_rate, max(Decimal("0.0001"), sales_90_rate))
    values: dict[str, Any] = {
        "feature_stock_cover": str(stock_cover),
        "feature_position_cover": str(position_cover),
        "feature_pipeline_cover": str(pipeline_cover),
        "feature_acceleration_30_forecast": str(acceleration),
        "feature_recent_30_90": str(recent_ratio),
        "feature_lead_spread_days": str(lead_spread),
        "feature_open_signal_qty": str(open_signal),
        "feature_stock_cover_bin": _bucket(
            stock_cover,
            tuple(Decimal(value) for value in ("0", "0.25", "0.5", "1", "2", "5")),
        ),
        "feature_position_cover_bin": _bucket(
            position_cover,
            tuple(Decimal(value) for value in ("0", "0.5", "1", "2", "5", "10")),
        ),
        "feature_pipeline_cover_bin": _bucket(
            pipeline_cover,
            tuple(Decimal(value) for value in ("0", "0.5", "1", "2", "5", "10")),
        ),
        "feature_acceleration_30_forecast_bin": _bucket(
            acceleration,
            tuple(Decimal(value) for value in ("0.5", "0.8", "1", "1.25", "1.5", "2", "4")),
        ),
        "feature_recent_30_90_bin": _bucket(
            recent_ratio,
            tuple(Decimal(value) for value in ("0.5", "0.8", "1", "1.25", "1.5", "2", "4")),
        ),
        "feature_lead_spread_bin": _bucket(
            lead_spread,
            tuple(Decimal(value) for value in ("7", "14", "30", "60")),
        ),
        "feature_lead_confidence_bin": _clean(source.get("lead_time_confidence")) or "unknown",
        "feature_demand_pattern_bin": _clean(window.get("demand_pattern_preperiod")) or "unknown",
        "feature_sales_trend_bin": _clean(source.get("sales_trend")) or "unknown",
        "feature_open_signal_bin": _bucket(
            open_signal,
            tuple(Decimal(value) for value in ("0", "1", "3", "10")),
        ),
    }
    return values


def join_as_of_features(
    windows: Sequence[Mapping[str, Any]],
    decision_inputs: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    index: dict[tuple[str, str], Mapping[str, Any]] = {}
    duplicate_count = 0
    for row in decision_inputs:
        if _clean(row.get("scheduled_review")) != "1" or _clean(row.get("status")) != "sale":
            continue
        key = (_clean(row.get("nomenclature_code")), _clean(row.get("decision_date")))
        if key in index:
            duplicate_count += 1
            continue
        index[key] = row

    output: list[dict[str, Any]] = []
    missing_count = 0
    for source_window in windows:
        row = dict(source_window)
        key = (_clean(row.get("nomenclature_code")), _clean(row.get("decision_date")))
        source = index.get(key)
        if source is None:
            missing_count += 1
            continue
        row.update(_feature_values(row, source))
        for column in (
            "sales_30",
            "sales_90",
            "sales_180",
            "physical_stock_qty",
            "effective_reserve_qty",
            "reserve_backlog_qty",
            "free_stock_qty",
            "free_incoming_qty",
            "inventory_position_qty",
            "lead_time_p75_days",
            "sales_trend",
        ):
            row[f"as_of_{column}"] = _clean(source.get(column))
        output.append(row)
    output.sort(
        key=lambda row: (_clean(row.get("decision_date")), _clean(row.get("opportunity_id")))
    )
    quality = {
        "window_count": len(windows),
        "joined_window_count": len(output),
        "missing_as_of_feature_count": missing_count,
        "duplicate_as_of_key_count": duplicate_count,
    }
    if missing_count or duplicate_count:
        raise ValueError(f"as-of feature join failed: {quality}")
    return output, quality


def _new_history() -> dict[str, Any]:
    return {
        "count": 0,
        "positive": 0,
        "positive_loss_qty": ZERO,
        "bins": defaultdict(lambda: defaultdict(lambda: [0, 0, ZERO])),
        "max_outcome_end": "",
    }


def _update_history(history: MutableMapping[str, Any], row: Mapping[str, Any]) -> None:
    positive = int(_decimal(row.get("has_model_loss_in_horizon")) > ZERO)
    loss = max(ZERO, _decimal(row.get("model_lost_observed_qty_in_horizon")))
    history["count"] += 1
    history["positive"] += positive
    history["positive_loss_qty"] += loss
    history["max_outcome_end"] = max(
        _clean(history.get("max_outcome_end")), _clean(row.get("outcome_end"))
    )
    for feature in FEATURE_COLUMNS:
        feature_bin = _clean(row.get(f"feature_{feature}_bin")) or "unknown"
        stats = history["bins"][feature][feature_bin]
        stats[0] += 1
        stats[1] += positive
        stats[2] += loss


def _predict(history: Mapping[str, Any], row: Mapping[str, Any]) -> tuple[Decimal, Decimal]:
    count = int(history["count"])
    positive = int(history["positive"])
    global_probability = (Decimal(positive) + ONE) / (Decimal(count) + Decimal("2"))
    global_severity = (
        _decimal(history["positive_loss_qty"]) / Decimal(positive) if positive else ZERO
    )
    probability_total = global_probability
    severity_total = global_severity
    probability_weight = ONE
    severity_weight = ONE
    for feature in FEATURE_COLUMNS:
        feature_bin = _clean(row.get(f"feature_{feature}_bin")) or "unknown"
        stats = history["bins"][feature].get(feature_bin)
        if not stats:
            continue
        bin_count, bin_positive, bin_loss = stats
        reliability = min(ONE, Decimal(bin_count) / RELIABLE_BIN_COUNT)
        bin_probability = (Decimal(bin_positive) + SMOOTHING * global_probability) / (
            Decimal(bin_count) + SMOOTHING
        )
        probability_total += bin_probability * reliability
        probability_weight += reliability
        if bin_positive:
            bin_severity = (_decimal(bin_loss) + SMOOTHING * global_severity) / (
                Decimal(bin_positive) + SMOOTHING
            )
            severity_total += bin_severity * reliability
            severity_weight += reliability
    probability = probability_total / probability_weight
    conditional_severity = severity_total / severity_weight
    return probability, max(ZERO, probability * conditional_severity)


def attach_walk_forward_risk(
    rows: Sequence[Mapping[str, Any]],
    *,
    min_training_samples: int,
) -> list[dict[str, Any]]:
    by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    completed: list[tuple[date, dict[str, Any]]] = []
    for source in rows:
        row = dict(source)
        decision_date = date.fromisoformat(_clean(row.get("decision_date")))
        outcome_end = date.fromisoformat(_clean(row.get("outcome_end")))
        by_date[decision_date].append(row)
        completed.append((outcome_end, row))
    completed.sort(key=lambda item: (item[0], _clean(item[1].get("opportunity_id"))))
    history = _new_history()
    completion_index = 0
    output: list[dict[str, Any]] = []
    for decision_date in sorted(by_date):
        while completion_index < len(completed) and completed[completion_index][0] < decision_date:
            _, completed_row = completed[completion_index]
            _update_history(history, completed_row)
            completion_index += 1
        for source in sorted(
            by_date[decision_date], key=lambda item: _clean(item.get("opportunity_id"))
        ):
            row = dict(source)
            row["risk_training_sample_count"] = history["count"]
            row["risk_training_positive_count"] = history["positive"]
            row["risk_training_max_outcome_end"] = history["max_outcome_end"]
            sufficient = int(history["count"] >= min_training_samples and history["positive"] > 0)
            row["risk_training_sufficient"] = sufficient
            if sufficient:
                probability, expected_qty = _predict(history, row)
            else:
                probability, expected_qty = ZERO, ZERO
            row["shortage_risk_probability"] = str(probability)
            row["shortage_expected_qty"] = str(expected_qty)
            output.append(row)
    output.sort(key=lambda row: _clean(row.get("opportunity_id")))
    return output


def build_candidate_buffers(
    rows: Sequence[Mapping[str, Any]],
    *,
    shares: Sequence[Decimal] = TOP_SHARES,
) -> dict[tuple[str, str], Decimal]:
    by_date: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if int(row.get("risk_training_sufficient") or 0):
            by_date[_clean(row.get("decision_date"))].append(row)
    buffers: dict[tuple[str, str], Decimal] = {}
    for decision_date in sorted(by_date):
        ranked = sorted(
            by_date[decision_date],
            key=lambda row: (
                -_decimal(row.get("shortage_expected_qty")),
                -_decimal(row.get("shortage_risk_probability")),
                _clean(row.get("opportunity_id")),
            ),
        )
        for share in shares:
            selected_count = min(len(ranked), max(1, int(_ceil(Decimal(len(ranked)) * share))))
            share_label = f"{int(share * 100):02d}"
            for row in ranked[:selected_count]:
                opportunity_id = _clean(row.get("opportunity_id"))
                buffers[(opportunity_id, f"risk_top_{share_label}_service1")] = ONE
                buffers[(opportunity_id, f"risk_top_{share_label}_expected")] = max(
                    ONE, _ceil(_decimal(row.get("shortage_expected_qty")))
                )
                buffers[(opportunity_id, f"risk_top_{share_label}_p75")] = max(
                    ZERO, _decimal(row.get("buffer_p75_qty"))
                )
    return buffers


def _candidate_names(shares: Sequence[Decimal] = TOP_SHARES) -> list[str]:
    names = ["baseline_p75"]
    for share in shares:
        label = f"{int(share * 100):02d}"
        names.extend(
            (
                f"risk_top_{label}_service1",
                f"risk_top_{label}_expected",
                f"risk_top_{label}_p75",
            )
        )
    return names


def _candidate_buffer(
    opportunity: Mapping[str, Any],
    candidate: str,
    buffers: Mapping[tuple[str, str], Decimal],
) -> Decimal:
    if candidate == "baseline_p75":
        return max(ZERO, _decimal(opportunity.get("buffer_p75_qty")))
    return max(
        ZERO,
        buffers.get((_clean(opportunity.get("opportunity_id")), candidate), ZERO),
    )


def evaluate_candidates(
    pairs: Sequence[Mapping[str, Any]],
    opportunities: Mapping[str, Mapping[str, Any]],
    buffers: Mapping[tuple[str, str], Decimal],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Decimal]]] = defaultdict(list)
    pair_rows: list[dict[str, Any]] = []
    for pair in pairs:
        if _clean(pair.get("case_period")) != _clean(pair.get("control_period")):
            continue
        period = _clean(pair.get("case_period"))
        case = opportunities[_clean(pair.get("case_opportunity_id"))]
        control = opportunities[_clean(pair.get("control_opportunity_id"))]
        case_loss = max(ZERO, _decimal(pair.get("case_episode_lost_qty")))
        case_score = _decimal(case.get("shortage_expected_qty"))
        control_score = _decimal(control.get("shortage_expected_qty"))
        pair_row: dict[str, Any] = {
            "pair_id": _clean(pair.get("pair_id")),
            "period": period,
            "case_opportunity_id": _clean(case.get("opportunity_id")),
            "control_opportunity_id": _clean(control.get("opportunity_id")),
            "case_loss_proxy_qty": str(case_loss),
            "case_risk_probability": _clean(case.get("shortage_risk_probability")),
            "control_risk_probability": _clean(control.get("shortage_risk_probability")),
            "case_expected_shortage_qty": str(case_score),
            "control_expected_shortage_qty": str(control_score),
            "case_score_higher": int(case_score > control_score),
            "score_tied": int(case_score == control_score),
        }
        for candidate in _candidate_names():
            case_buffer = _candidate_buffer(case, candidate, buffers)
            control_buffer = _candidate_buffer(control, candidate, buffers)
            covered = min(case_loss, case_buffer)
            avoidable_excess = control_buffer + max(ZERO, case_buffer - case_loss)
            excess_value = control_buffer * _decimal(control.get("inventory_cost_per_unit_rub"))
            excess_value += max(ZERO, case_buffer - case_loss) * _decimal(
                case.get("inventory_cost_per_unit_rub")
            )
            groups[(period, candidate)].append(
                {
                    "loss": case_loss,
                    "covered": covered,
                    "case_buffer": case_buffer,
                    "control_buffer": control_buffer,
                    "avoidable_excess": avoidable_excess,
                    "excess_value": excess_value,
                }
            )
            pair_row[f"{candidate}_case_buffer_qty"] = str(case_buffer)
            pair_row[f"{candidate}_control_buffer_qty"] = str(control_buffer)
        pair_rows.append(pair_row)

    evaluation: list[dict[str, Any]] = []
    for (period, candidate), values in sorted(groups.items()):
        loss = sum((row["loss"] for row in values), ZERO)
        covered = sum((row["covered"] for row in values), ZERO)
        total_buffer = sum((row["case_buffer"] + row["control_buffer"] for row in values), ZERO)
        excess = sum((row["avoidable_excess"] for row in values), ZERO)
        evaluation.append(
            {
                "period": period,
                "candidate": candidate,
                "matched_pair_count": len(values),
                "case_loss_proxy_qty": str(loss),
                "case_loss_proxy_covered_qty": str(covered),
                "case_loss_proxy_coverage_share": str(covered / loss if loss else ZERO),
                "total_buffer_proxy_qty": str(total_buffer),
                "avoidable_excess_proxy_qty": str(excess),
                "avoidable_excess_value_proxy_rub": str(
                    sum((row["excess_value"] for row in values), ZERO)
                ),
                "covered_per_total_buffer_qty": str(
                    covered / total_buffer if total_buffer else ZERO
                ),
                "covered_per_avoidable_excess_qty": str(covered / excess if excess else ZERO),
            }
        )
    return evaluation, pair_rows


def discrimination_rows(pair_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in pair_rows:
        groups[_clean(row.get("period"))].append(row)
        groups["all_same_period"].append(row)
    output: list[dict[str, Any]] = []
    for period, rows in sorted(groups.items()):
        sufficient = [
            row
            for row in rows
            if _decimal(row.get("case_expected_shortage_qty")) > ZERO
            or _decimal(row.get("control_expected_shortage_qty")) > ZERO
        ]
        higher = sum(int(row.get("case_score_higher") or 0) for row in sufficient)
        ties = sum(int(row.get("score_tied") or 0) for row in sufficient)
        output.append(
            {
                "period": period,
                "matched_pair_count": len(rows),
                "scored_pair_count": len(sufficient),
                "case_score_higher_count": higher,
                "score_tie_count": ties,
                "pairwise_discrimination": str(
                    (Decimal(higher) + Decimal(ties) / Decimal("2")) / Decimal(len(sufficient))
                    if sufficient
                    else ZERO
                ),
            }
        )
    return output


def review_example_rows(
    pair_rows: Sequence[Mapping[str, Any]],
    opportunities: Mapping[str, Mapping[str, Any]],
    *,
    examples_per_type: int = 3,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    checks = {
        "hit": "Сверить исходную обещанную дату pipeline и исключить разовую оптовую продажу.",
        "miss": "Проверить заменяемость SKU, запуск модели телефона и сигналы спроса, которых нет во frozen-наборе.",
        "false_alarm": "Проверить, почему высокий риск не стал дефицитом: приход, отмена спроса или избыточный сигнал.",
    }
    for period in ("pre_final_month", "final_month_exposed"):
        period_rows = [row for row in pair_rows if _clean(row.get("period")) == period]
        ranked = {
            "hit": sorted(
                (row for row in period_rows if int(row.get("case_score_higher") or 0)),
                key=lambda row: (
                    -_decimal(row.get("case_loss_proxy_qty")),
                    -_decimal(row.get("case_expected_shortage_qty")),
                    _clean(row.get("pair_id")),
                ),
            ),
            "miss": sorted(
                (row for row in period_rows if not int(row.get("case_score_higher") or 0)),
                key=lambda row: (
                    -_decimal(row.get("case_loss_proxy_qty")),
                    _clean(row.get("pair_id")),
                ),
            ),
            "false_alarm": sorted(
                (
                    row
                    for row in period_rows
                    if _decimal(row.get("control_expected_shortage_qty"))
                    > _decimal(row.get("case_expected_shortage_qty"))
                ),
                key=lambda row: (
                    -_decimal(row.get("control_expected_shortage_qty")),
                    -_decimal(row.get("case_loss_proxy_qty")),
                    _clean(row.get("pair_id")),
                ),
            ),
        }
        for example_type, candidates in ranked.items():
            for source in candidates[:examples_per_type]:
                case = opportunities[_clean(source.get("case_opportunity_id"))]
                control = opportunities[_clean(source.get("control_opportunity_id"))]
                output.append(
                    {
                        "example_type": example_type,
                        "period": period,
                        "pair_id": _clean(source.get("pair_id")),
                        "case_nomenclature_code": _clean(case.get("nomenclature_code")),
                        "case_name": _clean(case.get("name")),
                        "case_decision_date": _clean(case.get("decision_date")),
                        "case_loss_proxy_qty": _clean(source.get("case_loss_proxy_qty")),
                        "case_expected_shortage_qty": _clean(case.get("shortage_expected_qty")),
                        "case_stock_cover": _clean(case.get("feature_stock_cover")),
                        "case_position_cover": _clean(case.get("feature_position_cover")),
                        "case_acceleration_30_forecast": _clean(
                            case.get("feature_acceleration_30_forecast")
                        ),
                        "case_open_signal_qty": _clean(case.get("feature_open_signal_qty")),
                        "control_nomenclature_code": _clean(control.get("nomenclature_code")),
                        "control_name": _clean(control.get("name")),
                        "control_decision_date": _clean(control.get("decision_date")),
                        "control_expected_shortage_qty": _clean(
                            control.get("shortage_expected_qty")
                        ),
                        "control_stock_cover": _clean(control.get("feature_stock_cover")),
                        "control_position_cover": _clean(control.get("feature_position_cover")),
                        "control_acceleration_30_forecast": _clean(
                            control.get("feature_acceleration_30_forecast")
                        ),
                        "control_open_signal_qty": _clean(control.get("feature_open_signal_qty")),
                        "human_check": checks[example_type],
                    }
                )
    return output


def mark_pre_final_frontier(evaluation: list[dict[str, Any]]) -> None:
    pre_rows = [
        row
        for row in evaluation
        if row["period"] == "pre_final_month" and row["candidate"] != "baseline_p75"
    ]
    baseline = next(
        row
        for row in evaluation
        if row["period"] == "pre_final_month" and row["candidate"] == "baseline_p75"
    )
    baseline_coverage = _decimal(baseline["case_loss_proxy_covered_qty"])
    baseline_excess = _decimal(baseline["avoidable_excess_proxy_qty"])
    for row in evaluation:
        row["pre_final_pareto"] = 0
        row["pre_final_dominates_p75"] = 0
    for row in pre_rows:
        covered = _decimal(row["case_loss_proxy_covered_qty"])
        excess = _decimal(row["avoidable_excess_proxy_qty"])
        dominated = any(
            _decimal(other["case_loss_proxy_covered_qty"]) >= covered
            and _decimal(other["avoidable_excess_proxy_qty"]) <= excess
            and (
                _decimal(other["case_loss_proxy_covered_qty"]) > covered
                or _decimal(other["avoidable_excess_proxy_qty"]) < excess
            )
            for other in pre_rows
            if other is not row
        )
        row["pre_final_pareto"] = int(not dominated)
        row["pre_final_dominates_p75"] = int(
            covered >= baseline_coverage
            and excess <= baseline_excess
            and (covered > baseline_coverage or excess < baseline_excess)
        )


def _markdown(summary: Mapping[str, Any], evaluation: Sequence[Mapping[str, Any]]) -> str:
    discrimination = {row["period"]: row for row in summary["discrimination"]}
    baseline = {row["period"]: row for row in evaluation if row["candidate"] == "baseline_p75"}
    evaluation_index = {(row["period"], row["candidate"]): row for row in evaluation}
    frontier = [
        row
        for row in evaluation
        if row["period"] == "pre_final_month" and int(row["pre_final_pareto"])
    ]
    lines = [
        "# Screening риска дефицита на уровне SKU + дата",
        "",
        "## Короткий вывод",
        "",
        "Это диагностический walk-forward challenger: каждая дата использует только",
        "окна, которые полностью закончились раньше неё. Новые заказы не симулировались",
        "и не создавались.",
        "",
        "## Различимость дефицита",
        "",
        "| Период результата | Сопоставимых пар | Оценённых пар | Case выше control |",
        "| --- | ---: | ---: | ---: |",
    ]
    for period in ("pre_final_month", "final_month_exposed"):
        row = discrimination[period]
        lines.append(
            f"| {period} | {row['matched_pair_count']} | {row['scored_pair_count']} | "
            f"{float(_decimal(row['pairwise_discrimination'])):.1%} |"
        )
    lines.extend(
        [
            "",
            "## База P75",
            "",
            "| Период | Покрытие proxy-потери | Лишний buffer, ед. |",
            "| --- | ---: | ---: |",
        ]
    )
    for period in ("pre_final_month", "final_month_exposed"):
        row = baseline[period]
        lines.append(
            f"| {period} | {float(_decimal(row['case_loss_proxy_coverage_share'])):.1%} | "
            f"{float(_decimal(row['avoidable_excess_proxy_qty'])):,.1f} |"
        )
    lines.extend(
        [
            "",
            "## Pareto-кандидаты, выбранные только до июля",
            "",
            "| Кандидат | Покрытие | Лишний buffer, ед. |",
            "| --- | ---: | ---: |",
        ]
    )
    for row in frontier:
        lines.append(
            f"| {row['candidate']} | "
            f"{float(_decimal(row['case_loss_proxy_coverage_share'])):.1%} | "
            f"{float(_decimal(row['avoidable_excess_proxy_qty'])):,.1f} |"
        )
    lines.extend(
        [
            "",
            "## Перенос осторожного top-50 expected на июль",
            "",
            "| Период | Покрытие | Лишний buffer, ед. |",
            "| --- | ---: | ---: |",
        ]
    )
    for period in ("pre_final_month", "final_month_exposed"):
        row = evaluation_index[(period, "risk_top_50_expected")]
        lines.append(
            f"| {period} | {float(_decimal(row['case_loss_proxy_coverage_share'])):.1%} | "
            f"{float(_decimal(row['avoidable_excess_proxy_qty'])):,.1f} |"
        )
    lines.extend(
        [
            "",
            "## Решение по screening",
            "",
            "Ни один профиль не сохранил покрытие P75 при меньшем excess, а различимость",
            "на окнах с июлем заметно снизилась. Полный frozen-backtest на этой итерации",
            "не запускается: сначала нужно улучшить количественную модель и проверить",
            "структурный сдвиг на примерах пропусков и ложных тревог.",
            "",
            "## Ограничение",
            "",
            "Покрытие и excess здесь являются screening-proxy на matched-парах, а не",
            "спасёнными продажами и не средним складским капиталом. Полный frozen-backtest",
            "нужен только если challenger устойчиво улучшает этот обмен, включая окна с июлем.",
            "Примеры удачных сигналов, пропусков и ложных тревог сохранены в `review-examples.csv`.",
            "",
        ]
    )
    return "\n".join(lines)


def build_analysis(
    *,
    preflight_dir: Path,
    forecast_analysis_dir: Path,
    output_dir: Path,
    min_training_samples: int = 200,
) -> dict[str, Any]:
    validate_preflight_directory(preflight_dir)
    _validate_forecast_analysis_directory(forecast_analysis_dir)
    windows = _read_csv(forecast_analysis_dir / "decision-windows.csv")
    pairs = _read_csv(forecast_analysis_dir / "matched-pairs.csv")
    decision_inputs = _read_csv(preflight_dir / "decision-inputs.csv")
    joined, quality = join_as_of_features(windows, decision_inputs)
    scored = attach_walk_forward_risk(joined, min_training_samples=min_training_samples)
    opportunity_index = {_clean(row.get("opportunity_id")): row for row in scored}
    buffers = build_candidate_buffers(scored)
    evaluation, pair_rows = evaluate_candidates(pairs, opportunity_index, buffers)
    mark_pre_final_frontier(evaluation)
    discrimination = discrimination_rows(pair_rows)
    review_examples = review_example_rows(pair_rows, opportunity_index)
    insufficient = sum(not int(row.get("risk_training_sufficient") or 0) for row in scored)
    summary: dict[str, Any] = {
        "schema": "display_auto_order_shortage_risk_screening.v1",
        "diagnostic_only": True,
        "production_authorized": False,
        "date_from": min(_clean(row.get("decision_date")) for row in scored),
        "date_to": max(_clean(row.get("outcome_end")) for row in scored),
        "headline": {
            "decision_window_count": len(scored),
            "matched_pair_count": len(pairs),
            "scored_window_count": len(scored) - insufficient,
            "insufficient_training_window_count": insufficient,
            "pre_final_pareto_candidates": [
                row["candidate"]
                for row in evaluation
                if row["period"] == "pre_final_month" and int(row["pre_final_pareto"])
            ],
            "pre_final_p75_dominating_candidates": [
                row["candidate"]
                for row in evaluation
                if row["period"] == "pre_final_month" and int(row["pre_final_dominates_p75"])
            ],
        },
        "discrimination": discrimination,
        "candidate_evaluation": evaluation,
        "quality": {**quality, "min_training_samples": min_training_samples},
        "method": {
            "population": "v18 scheduled sale/Растим SKU-date windows with fully observed P50 lead-time plus seven-day outcome horizons",
            "features": "as-of free stock, position and pipeline cover; recent acceleration; P75-P50 lead spread; lead confidence; preperiod demand pattern; sales trend; open KMP4/site/backlog signal",
            "learner": "smoothed empirical multi-signal probability and conditional severity; cumulative history is updated only after outcome_end is strictly earlier than decision_date",
            "candidates": "top 5/10/20/30/50 percent of scored SKU per decision date; one-unit service, rounded expected-shortage quantity, or the existing causal P75 quantity",
            "selection": "Pareto frontier is derived only on pre-final-month matched pairs; the same named candidates are reported separately on final-month-exposed pairs",
            "interpretation": "screening proxy only; no order simulation, production writes, or policy threshold approval",
        },
        "source_checksums": {
            "preflight_manifest": _sha256(preflight_dir / "run-manifest.json"),
            "forecast_analysis_manifest": _sha256(forecast_analysis_dir / "analysis-manifest.json"),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "risk-scores.csv", scored)
    _write_csv(output_dir / "matched-risk-pairs.csv", pair_rows)
    _write_csv(output_dir / "candidate-evaluation.csv", evaluation)
    _write_csv(output_dir / "discrimination.csv", discrimination)
    _write_csv(output_dir / "review-examples.csv", review_examples)
    (output_dir / "analysis-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "SHORTAGE-RISK-SCREENING.md").write_text(
        _markdown(summary, evaluation), encoding="utf-8"
    )
    artifact_names = (
        "analysis-summary.json",
        "risk-scores.csv",
        "matched-risk-pairs.csv",
        "candidate-evaluation.csv",
        "discrimination.csv",
        "review-examples.csv",
        "SHORTAGE-RISK-SCREENING.md",
    )
    manifest = {
        "schema": "display_auto_order_shortage_risk_screening_manifest.v1",
        "diagnostic_only": True,
        "production_authorized": False,
        "files": {name: _sha256(output_dir / name) for name in artifact_names},
    }
    (output_dir / "analysis-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, required=True)
    parser.add_argument("--forecast-analysis-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--min-training-samples", type=int, default=200)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.min_training_samples <= 0:
        raise SystemExit("--min-training-samples must be positive")
    summary = build_analysis(
        preflight_dir=args.preflight_dir,
        forecast_analysis_dir=args.forecast_analysis_dir,
        output_dir=args.output_dir,
        min_training_samples=args.min_training_samples,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
