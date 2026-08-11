"""Explain shortage-risk misses and false alarms without replaying orders.

The task joins the frozen v17 loss episodes, v18 case windows, and v19
walk-forward risk scores.  It decomposes missed loss into pipeline exposure,
demand shocks, both, or other model logic and explains matched false alarms
from the realized control outcome.  It has no production or purchasing side
effects.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from tasks.analyze_display_auto_order_shortage_risk import (
    ZERO,
    _clean,
    _decimal,
    _read_csv,
    _sha256,
    _write_csv,
)

PIPELINE_MECHANISMS = {
    "pipeline_counted_before_arrival",
    "replenishment_in_transit",
}
PIPELINE_RECOVERABILITY = {"pipeline_blocked_at_last_chance"}

DRIVER_LABELS = {
    "pipeline_and_demand_shock": "Pipeline и скачок спроса",
    "pipeline_only": "Только pipeline",
    "demand_shock_only": "Только скачок спроса",
    "other_model_logic": "Прочая логика модели",
}

DETECTION_LABELS = {
    "detected": "Риск распознан",
    "missed": "Риск пропущен",
    "unscored": "Не хватило истории",
}

FALSE_ALARM_LABELS = {
    "demand_not_above_forecast": "Спрос не превысил прогноз",
    "starting_position_absorbed_demand": "Позиция запаса покрыла спрос",
    "future_replenishment_or_review": "Помогло будущее пополнение или пересмотр",
    "unresolved_no_loss": "Причина не доказана frozen-данными",
}


def _validate_analysis_directory(path: Path, required: Sequence[str]) -> dict[str, Any]:
    manifest_path = path / "analysis-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_files = manifest.get("files") or {}
    for name in required:
        expected = _clean(expected_files.get(name))
        if not expected or _sha256(path / name) != expected:
            raise ValueError(f"analysis checksum mismatch: {name}")
    return manifest


def detection_status(pair: Mapping[str, Any]) -> str:
    case_score = _decimal(pair.get("case_expected_shortage_qty"))
    control_score = _decimal(pair.get("control_expected_shortage_qty"))
    if case_score <= ZERO and control_score <= ZERO:
        return "unscored"
    if case_score > control_score:
        return "detected"
    return "missed"


def episode_driver_flags(episode: Mapping[str, Any]) -> dict[str, Any]:
    pipeline = int(
        _clean(episode.get("mechanism")) in PIPELINE_MECHANISMS
        or _clean(episode.get("recoverability")) in PIPELINE_RECOVERABILITY
    )
    demand_shock = int(
        int(_decimal(episode.get("observed_above_forecast_to_first_loss"))) == 1
        and _decimal(episode.get("forecast_shortfall_to_first_loss_qty")) > ZERO
    )
    if pipeline and demand_shock:
        driver = "pipeline_and_demand_shock"
    elif pipeline:
        driver = "pipeline_only"
    elif demand_shock:
        driver = "demand_shock_only"
    else:
        driver = "other_model_logic"
    return {
        "pipeline_exposure": pipeline,
        "demand_shock": demand_shock,
        "driver": driver,
        "driver_label": DRIVER_LABELS[driver],
    }


def map_episodes_to_case_windows(
    episodes: Sequence[Mapping[str, Any]],
    case_windows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    windows_by_code: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in case_windows:
        windows_by_code[_clean(row.get("nomenclature_code"))].append(row)
    for rows in windows_by_code.values():
        rows.sort(key=lambda row: _clean(row.get("decision_date")))

    mapped: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    for source in episodes:
        if _clean(source.get("status")) != "sale":
            continue
        episode_start = date.fromisoformat(_clean(source.get("episode_start")))
        candidates = [
            row
            for row in windows_by_code.get(_clean(source.get("nomenclature_code")), ())
            if date.fromisoformat(_clean(row.get("decision_date")))
            <= episode_start
            <= date.fromisoformat(_clean(row.get("outcome_end")))
        ]
        if not candidates:
            unmapped.append(dict(source))
            continue
        selected = max(candidates, key=lambda row: _clean(row.get("decision_date")))
        row = dict(source)
        row["case_opportunity_id"] = _clean(selected.get("opportunity_id"))
        row.update(episode_driver_flags(row))
        mapped.append(row)
    mapped.sort(
        key=lambda row: (
            _clean(row.get("case_opportunity_id")),
            _clean(row.get("episode_start")),
        )
    )
    return mapped, unmapped


def build_episode_diagnostics(
    mapped_episodes: Sequence[Mapping[str, Any]],
    pair_by_case: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for source in mapped_episodes:
        pair = pair_by_case.get(_clean(source.get("case_opportunity_id")))
        if pair is None:
            excluded.append(dict(source))
            continue
        row = dict(source)
        row["pair_id"] = _clean(pair.get("pair_id"))
        row["outcome_period"] = _clean(pair.get("period"))
        row["detection_status"] = detection_status(pair)
        row["detection_label"] = DETECTION_LABELS[row["detection_status"]]
        row["case_expected_shortage_qty"] = _clean(pair.get("case_expected_shortage_qty"))
        row["control_expected_shortage_qty"] = _clean(pair.get("control_expected_shortage_qty"))
        diagnostics.append(row)
    diagnostics.sort(
        key=lambda row: (
            _clean(row.get("outcome_period")),
            _clean(row.get("detection_status")),
            -_decimal(row.get("lost_observed_qty")),
        )
    )
    return diagnostics, excluded


def aggregate_driver_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    status_totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    period_totals: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        period = _clean(row.get("outcome_period"))
        status = _clean(row.get("detection_status"))
        driver = _clean(row.get("driver"))
        lost = _decimal(row.get("lost_observed_qty"))
        groups[(period, status, driver)].append(row)
        status_totals[(period, status)] += lost
        period_totals[period] += lost
    output: list[dict[str, Any]] = []
    for (period, status, driver), group in sorted(groups.items()):
        lost = sum((_decimal(row.get("lost_observed_qty")) for row in group), ZERO)
        output.append(
            {
                "period": period,
                "detection_status": status,
                "detection_label": DETECTION_LABELS[status],
                "driver": driver,
                "driver_label": DRIVER_LABELS[driver],
                "episode_count": len(group),
                "pair_count": len({_clean(row.get("pair_id")) for row in group}),
                "lost_observed_qty": str(lost),
                "share_within_detection_status": str(
                    lost / status_totals[(period, status)]
                    if status_totals[(period, status)] > ZERO
                    else ZERO
                ),
                "share_of_period_loss": str(
                    lost / period_totals[period] if period_totals[period] > ZERO else ZERO
                ),
            }
        )
    return output


def detection_performance_rows(
    diagnostics: Sequence[Mapping[str, Any]],
    pairs: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    episode_loss_by_pair: dict[str, Decimal] = defaultdict(Decimal)
    for row in diagnostics:
        episode_loss_by_pair[_clean(row.get("pair_id"))] += _decimal(row.get("lost_observed_qty"))
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for pair in pairs:
        groups[(_clean(pair.get("period")), detection_status(pair))].append(pair)
    output: list[dict[str, Any]] = []
    for period in ("pre_final_month", "final_month_exposed"):
        total_pairs = sum(len(groups[(period, status)]) for status in DETECTION_LABELS)
        total_loss = sum(
            (
                episode_loss_by_pair[_clean(pair.get("pair_id"))]
                for status in DETECTION_LABELS
                for pair in groups[(period, status)]
            ),
            ZERO,
        )
        scored_loss = sum(
            (
                episode_loss_by_pair[_clean(pair.get("pair_id"))]
                for status in ("detected", "missed")
                for pair in groups[(period, status)]
            ),
            ZERO,
        )
        detected_loss = sum(
            (
                episode_loss_by_pair[_clean(pair.get("pair_id"))]
                for pair in groups[(period, "detected")]
            ),
            ZERO,
        )
        for status in DETECTION_LABELS:
            group = groups[(period, status)]
            lost = sum(
                (episode_loss_by_pair[_clean(pair.get("pair_id"))] for pair in group),
                ZERO,
            )
            output.append(
                {
                    "period": period,
                    "detection_status": status,
                    "detection_label": DETECTION_LABELS[status],
                    "pair_count": len(group),
                    "pair_share": str(
                        Decimal(len(group)) / Decimal(total_pairs) if total_pairs else ZERO
                    ),
                    "lost_observed_qty": str(lost),
                    "loss_share": str(lost / total_loss if total_loss > ZERO else ZERO),
                    "loss_weighted_detection_share_scored": str(
                        detected_loss / scored_loss if scored_loss > ZERO else ZERO
                    ),
                }
            )
    return output


def false_alarm_reason(control: Mapping[str, Any]) -> str:
    actual = max(ZERO, _decimal(control.get("actual_demand_qty")))
    forecast = max(ZERO, _decimal(control.get("forecast_demand_qty")))
    position = max(ZERO, _decimal(control.get("as_of_inventory_position_qty")))
    incoming = max(ZERO, _decimal(control.get("as_of_free_incoming_qty")))
    if actual <= forecast:
        return "demand_not_above_forecast"
    if position >= actual:
        return "starting_position_absorbed_demand"
    if incoming > ZERO:
        return "future_replenishment_or_review"
    return "unresolved_no_loss"


def false_alarm_rows(
    pairs: Sequence[Mapping[str, Any]],
    opportunities: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    details: list[dict[str, Any]] = []
    for pair in pairs:
        case_score = _decimal(pair.get("case_expected_shortage_qty"))
        control_score = _decimal(pair.get("control_expected_shortage_qty"))
        if control_score <= case_score:
            continue
        control = opportunities[_clean(pair.get("control_opportunity_id"))]
        reason = false_alarm_reason(control)
        details.append(
            {
                "pair_id": _clean(pair.get("pair_id")),
                "period": _clean(pair.get("period")),
                "reason": reason,
                "reason_label": FALSE_ALARM_LABELS[reason],
                "control_opportunity_id": _clean(control.get("opportunity_id")),
                "nomenclature_code": _clean(control.get("nomenclature_code")),
                "name": _clean(control.get("name")),
                "decision_date": _clean(control.get("decision_date")),
                "risk_score_gap": str(control_score - case_score),
                "control_expected_shortage_qty": str(control_score),
                "actual_demand_qty": _clean(control.get("actual_demand_qty")),
                "forecast_demand_qty": _clean(control.get("forecast_demand_qty")),
                "inventory_position_qty": _clean(control.get("as_of_inventory_position_qty")),
                "free_incoming_qty": _clean(control.get("as_of_free_incoming_qty")),
                "stock_cover": _clean(control.get("feature_stock_cover")),
                "position_cover": _clean(control.get("feature_position_cover")),
                "acceleration_30_forecast": _clean(control.get("feature_acceleration_30_forecast")),
                "open_signal_qty": _clean(control.get("feature_open_signal_qty")),
            }
        )
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in details:
        groups[(_clean(row.get("period")), _clean(row.get("reason")))].append(row)
    totals: dict[str, int] = defaultdict(int)
    for (period, _), rows in groups.items():
        totals[period] += len(rows)
    summary: list[dict[str, Any]] = []
    for (period, reason), rows in sorted(groups.items()):
        summary.append(
            {
                "period": period,
                "reason": reason,
                "reason_label": FALSE_ALARM_LABELS[reason],
                "pair_count": len(rows),
                "pair_share": str(
                    Decimal(len(rows)) / Decimal(totals[period]) if totals[period] else ZERO
                ),
                "mean_control_expected_shortage_qty": str(
                    sum(
                        (_decimal(row.get("control_expected_shortage_qty")) for row in rows),
                        ZERO,
                    )
                    / Decimal(len(rows))
                ),
            }
        )
    return details, summary


def feature_gap_rows(
    pairs: Sequence[Mapping[str, Any]],
    opportunities: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    features = {
        "stock_cover": "feature_stock_cover",
        "position_cover": "feature_position_cover",
        "pipeline_cover": "feature_pipeline_cover",
        "acceleration_30_forecast": "feature_acceleration_30_forecast",
        "recent_30_90": "feature_recent_30_90",
        "open_signal_qty": "feature_open_signal_qty",
    }
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for pair in pairs:
        groups[(_clean(pair.get("period")), detection_status(pair))].append(pair)
    output: list[dict[str, Any]] = []
    for (period, status), group in sorted(groups.items()):
        for feature, column in features.items():
            case_values = [
                _decimal(opportunities[_clean(pair.get("case_opportunity_id"))].get(column))
                for pair in group
            ]
            control_values = [
                _decimal(opportunities[_clean(pair.get("control_opportunity_id"))].get(column))
                for pair in group
            ]
            case_mean = sum(case_values, ZERO) / Decimal(len(case_values))
            control_mean = sum(control_values, ZERO) / Decimal(len(control_values))
            output.append(
                {
                    "period": period,
                    "detection_status": status,
                    "feature": feature,
                    "pair_count": len(group),
                    "case_mean": str(case_mean),
                    "control_mean": str(control_mean),
                    "case_minus_control": str(case_mean - control_mean),
                }
            )
    return output


def manual_review_rows(
    diagnostics: Sequence[Mapping[str, Any]],
    false_alarms: Sequence[Mapping[str, Any]],
    *,
    per_group: int = 3,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    miss_groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in diagnostics:
        if _clean(row.get("detection_status")) in {"missed", "unscored"}:
            miss_groups[(_clean(row.get("outcome_period")), _clean(row.get("driver")))].append(row)
    for (period, driver), rows in sorted(miss_groups.items()):
        ranked = sorted(rows, key=lambda row: -_decimal(row.get("lost_observed_qty")))
        for row in ranked[:per_group]:
            output.append(
                {
                    "review_type": "missed_loss",
                    "period": period,
                    "category": driver,
                    "category_label": DRIVER_LABELS[driver],
                    "pair_id": _clean(row.get("pair_id")),
                    "nomenclature_code": _clean(row.get("nomenclature_code")),
                    "name": _clean(row.get("name")),
                    "decision_date": _clean(row.get("decision_date")),
                    "episode_start": _clean(row.get("episode_start")),
                    "episode_end": _clean(row.get("episode_end")),
                    "quantity": _clean(row.get("lost_observed_qty")),
                    "mechanism": _clean(row.get("mechanism")),
                    "recoverability": _clean(row.get("recoverability")),
                    "human_check": _clean(row.get("human_check")),
                }
            )
    false_groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in false_alarms:
        false_groups[(_clean(row.get("period")), _clean(row.get("reason")))].append(row)
    for (period, reason), rows in sorted(false_groups.items()):
        ranked = sorted(rows, key=lambda row: -_decimal(row.get("risk_score_gap")))
        for row in ranked[:per_group]:
            output.append(
                {
                    "review_type": "false_alarm",
                    "period": period,
                    "category": reason,
                    "category_label": FALSE_ALARM_LABELS[reason],
                    "pair_id": _clean(row.get("pair_id")),
                    "nomenclature_code": _clean(row.get("nomenclature_code")),
                    "name": _clean(row.get("name")),
                    "decision_date": _clean(row.get("decision_date")),
                    "episode_start": "",
                    "episode_end": "",
                    "quantity": _clean(row.get("control_expected_shortage_qty")),
                    "mechanism": "",
                    "recoverability": "",
                    "human_check": (
                        "Сверить фактический приход и продажи внутри окна; определить, "
                        "риск погас запас, будущий заказ или спрос просто не реализовался."
                    ),
                }
            )
    return output


def _period_detection_index(
    rows: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], Mapping[str, Any]]:
    return {(_clean(row.get("period")), _clean(row.get("detection_status"))): row for row in rows}


def _driver_index(
    rows: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str, str], Mapping[str, Any]]:
    return {
        (
            _clean(row.get("period")),
            _clean(row.get("detection_status")),
            _clean(row.get("driver")),
        ): row
        for row in rows
    }


def _pct(value: Any) -> str:
    return f"{float(_decimal(value)):.1%}"


def _num(value: Any) -> str:
    return f"{float(_decimal(value)):,.1f}".replace(",", " ")


def _markdown(summary: Mapping[str, Any]) -> str:
    detection = _period_detection_index(summary["detection_performance"])
    drivers = _driver_index(summary["driver_breakdown"])
    pre_detected = detection[("pre_final_month", "detected")]
    final_detected = detection[("final_month_exposed", "detected")]
    final_missed = detection[("final_month_exposed", "missed")]
    final_pipeline = sum(
        (
            _decimal(
                drivers.get(("final_month_exposed", "missed", driver), {}).get("lost_observed_qty")
            )
            for driver in ("pipeline_only", "pipeline_and_demand_shock")
        ),
        ZERO,
    )
    final_missed_qty = _decimal(final_missed.get("lost_observed_qty"))
    return "\n".join(
        [
            "# Почему риск дефицита хуже сработал на июле",
            "",
            "## Короткий вывод",
            "",
            f"До июля риск распознал {_pct(pre_detected['loss_share'])} matched-потери, "
            f"а на окнах с июлем — {_pct(final_detected['loss_share'])}. Главный разрыв "
            "остаётся связан с pipeline; скачок спроса часто усиливает его, но редко "
            "является единственной причиной.",
            "",
            "## Что находится внутри июльских пропусков",
            "",
            f"В scored-пропусках с июльской экспозицией потеряно "
            f"{_num(final_missed_qty)} единицы. Pipeline присутствует в "
            f"{_pct(final_pipeline / final_missed_qty if final_missed_qty else ZERO)} этой "
            "потери. Это диагностическая экспозиция: frozen-набор не хранит полную "
            "историю первоначальных обещаний поставщика и переносов каждой партии.",
            "",
            "## Что делать дальше",
            "",
            "Следующий эксперимент должен быть не новым общим буфером. Сначала нужна "
            "таблица надёжности конкретной партии: исходная обещанная дата, переносы, "
            "частичные приходы и доля фактически пришедшего к P50/P75. Ускорение спроса "
            "следует оставить вторым независимым сигналом, а не заменой проверки pipeline.",
            "",
            "## Ограничение",
            "",
            "Разложение объясняет механизм внутри frozen-контроля и не доказывает, что "
            "каждую потерю можно было вернуть заказом. Production и forward shadow не "
            "разрешены.",
            "",
        ]
    )


def build_analysis(
    *,
    risk_analysis_dir: Path,
    forecast_analysis_dir: Path,
    loss_analysis_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    _validate_analysis_directory(
        risk_analysis_dir,
        ("risk-scores.csv", "matched-risk-pairs.csv"),
    )
    _validate_analysis_directory(forecast_analysis_dir, ("case-anchors.csv",))
    _validate_analysis_directory(loss_analysis_dir, ("loss-episodes.csv",))
    opportunities = {
        _clean(row.get("opportunity_id")): row
        for row in _read_csv(risk_analysis_dir / "risk-scores.csv")
    }
    all_pairs = _read_csv(risk_analysis_dir / "matched-risk-pairs.csv")
    pairs = [
        row
        for row in all_pairs
        if _clean(row.get("period")) in {"pre_final_month", "final_month_exposed"}
    ]
    case_windows = _read_csv(forecast_analysis_dir / "case-anchors.csv")
    episodes = _read_csv(loss_analysis_dir / "loss-episodes.csv")
    mapped, unmapped = map_episodes_to_case_windows(episodes, case_windows)
    pair_by_case = {_clean(row.get("case_opportunity_id")): row for row in pairs}
    diagnostics, excluded = build_episode_diagnostics(mapped, pair_by_case)
    driver_breakdown = aggregate_driver_rows(diagnostics)
    detection_performance = detection_performance_rows(diagnostics, pairs)
    false_details, false_summary = false_alarm_rows(pairs, opportunities)
    feature_gaps = feature_gap_rows(pairs, opportunities)
    manual_review = manual_review_rows(diagnostics, false_details)

    pair_loss = sum((_decimal(row.get("case_loss_proxy_qty")) for row in pairs), ZERO)
    diagnostic_loss = sum((_decimal(row.get("lost_observed_qty")) for row in diagnostics), ZERO)
    if pair_loss != diagnostic_loss:
        raise ValueError(
            f"episode reconciliation failed: pairs={pair_loss}, diagnostics={diagnostic_loss}"
        )
    summary: dict[str, Any] = {
        "schema": "display_auto_order_shortage_risk_driver_analysis.v1",
        "diagnostic_only": True,
        "production_authorized": False,
        "date_from": "2026-02-01",
        "date_to": "2026-07-31",
        "headline": {
            "same_period_pair_count": len(pairs),
            "mapped_episode_count": len(diagnostics),
            "matched_loss_qty": str(diagnostic_loss),
            "unmapped_sale_episode_count": len(unmapped),
            "excluded_mixed_or_unmatched_episode_count": len(excluded),
            "false_alarm_pair_count": len(false_details),
        },
        "detection_performance": detection_performance,
        "driver_breakdown": driver_breakdown,
        "false_alarm_breakdown": false_summary,
        "feature_gaps": feature_gaps,
        "method": {
            "comparison": "same-outcome-period v19 matched pairs; case expected-shortage score compared with its control score",
            "detected": "case expected-shortage score strictly greater than control; both zero is unscored history",
            "pipeline_exposure": "v17 loss mechanism pipeline_counted_before_arrival or replenishment_in_transit, or recoverability pipeline_blocked_at_last_chance",
            "demand_shock": "observed demand from the causal decision to first loss strictly exceeded its forecast",
            "driver_decomposition": "mutually exclusive 2x2 of pipeline exposure and demand shock; episode losses reconcile exactly to same-period matched case loss",
            "false_alarm": "control score greater than case score while the v18 control window has no model loss; reason is an outcome proxy, not causal proof",
        },
        "reconciliation": {
            "pair_loss_qty": str(pair_loss),
            "diagnostic_episode_loss_qty": str(diagnostic_loss),
            "difference_qty": str(pair_loss - diagnostic_loss),
        },
        "source_checksums": {
            "risk_analysis_manifest": _sha256(risk_analysis_dir / "analysis-manifest.json"),
            "forecast_analysis_manifest": _sha256(forecast_analysis_dir / "analysis-manifest.json"),
            "loss_analysis_manifest": _sha256(loss_analysis_dir / "analysis-manifest.json"),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "episode-diagnostics.csv", diagnostics)
    _write_csv(output_dir / "driver-breakdown.csv", driver_breakdown)
    _write_csv(output_dir / "detection-performance.csv", detection_performance)
    _write_csv(output_dir / "false-alarm-details.csv", false_details)
    _write_csv(output_dir / "false-alarm-breakdown.csv", false_summary)
    _write_csv(output_dir / "feature-gaps.csv", feature_gaps)
    _write_csv(output_dir / "manual-review.csv", manual_review)
    (output_dir / "analysis-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "SHORTAGE-RISK-DRIVERS.md").write_text(_markdown(summary), encoding="utf-8")
    artifact_names = (
        "analysis-summary.json",
        "episode-diagnostics.csv",
        "driver-breakdown.csv",
        "detection-performance.csv",
        "false-alarm-details.csv",
        "false-alarm-breakdown.csv",
        "feature-gaps.csv",
        "manual-review.csv",
        "SHORTAGE-RISK-DRIVERS.md",
    )
    manifest = {
        "schema": "display_auto_order_shortage_risk_driver_analysis_manifest.v1",
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
    parser.add_argument("--risk-analysis-dir", type=Path, required=True)
    parser.add_argument("--forecast-analysis-dir", type=Path, required=True)
    parser.add_argument("--loss-analysis-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    summary = build_analysis(
        risk_analysis_dir=args.risk_analysis_dir,
        forecast_analysis_dir=args.forecast_analysis_dir,
        loss_analysis_dir=args.loss_analysis_dir,
        output_dir=args.output_dir,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
