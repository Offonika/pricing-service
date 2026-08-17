"""Diagnose recorded sales missed by the frozen display min/max control.

The task replays one persisted control scenario with daily loss instrumentation.
It reads only frozen artifacts, writes Markdown/CSV/JSON, and has no production
or purchasing side effects.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Any, Mapping, Sequence

from tasks import report_display_auto_order_frozen_backtest as frozen
from tasks.analyze_display_auto_order_quick_backtest import _prepare_inputs
from tasks.build_display_auto_order_dry_run import load_auto_order_policy
from tasks.display_auto_order_backtest_preflight import load_scenario_config

ZERO = Decimal("0")

STAGE_LABELS = {
    "fruit": "Рассматриваем",
    "newborn": "Заказали",
    "new_item": "Новинка",
    "sales_start": "Пошли продажи",
    "sale": "Растим",
    "working": "Поддерживаем",
}

REASON_LABELS = {
    "new_sku_without_start_stock": "Новинка без стартового остатка",
    "no_prior_control_state": "Нет предыдущего решения модели",
    "quantity_rounding_or_cap": "Количество обрезано округлением или лимитом",
    "pipeline_counted_before_arrival": "Товар в пути завысил доступный запас",
    "replenishment_in_transit": "Пополнение ещё было в пути",
    "zero_or_missing_forecast": "Нулевой или отсутствующий прогноз",
    "demand_jump_over_min_trigger": "Спрос вырос раньше порога min",
    "trigger_without_order": "Порог сработал, но заказ не сформирован",
    "base_target_underforecast": "Целевой запас оказался ниже спроса",
}

RECOVERABILITY_LABELS = {
    "stock_above_min_at_last_chance": "Запас ещё был выше min в последний срок заказа",
    "pipeline_blocked_at_last_chance": "Товар в пути удержал позицию выше min",
    "ordered_but_target_too_low": "Время было, но заказ оказался мал",
    "triggered_without_quantity": "Порог сработал, но количество осталось нулевым",
    "no_advance_signal": "Заранее не было достаточного сигнала",
    "supply_or_signal_too_late": "Сигнал или поставка появились слишком поздно",
    "insufficient_control_history": "Недостаточно истории для честного вывода",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(_clean(value) or "0")
    except (ArithmeticError, ValueError):
        return ZERO


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


def _lead_time_band(days: int) -> str:
    if days <= 30:
        return "<=30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return ">90"


def _pipeline_band(quantity: Decimal, days_to_arrival: int | None) -> str:
    if quantity <= ZERO:
        return "none"
    if days_to_arrival is None:
        return "open_without_date"
    if days_to_arrival <= 7:
        return "arrives_1_7d"
    if days_to_arrival <= 30:
        return "arrives_8_30d"
    return "arrives_over_30d"


def loss_reason_flags(row: Mapping[str, Any]) -> dict[str, int]:
    prior_min = _decimal(row.get("prior_min_stock_qty"))
    prior_position = _decimal(row.get("prior_inventory_position_qty"))
    prior_stock = _decimal(row.get("prior_model_stock_qty"))
    prior_reserve = _decimal(row.get("prior_reserve_qty"))
    prior_pipeline = _decimal(row.get("prior_effective_model_pipeline_qty"))
    current_pipeline = _decimal(row.get("model_pipeline_qty"))
    raw_order = _decimal(row.get("prior_recommended_order_qty_raw"))
    rounded_order = _decimal(row.get("prior_recommended_order_qty"))
    p75 = int(_decimal(row.get("current_lead_time_p75_days")))
    selected_lead = int(_decimal(row.get("prior_selected_lead_time_days")))
    confidence = _clean(row.get("current_lead_time_confidence")) or "unknown"
    return {
        "pipeline_present": int(current_pipeline > ZERO),
        "pipeline_blocked_reorder": int(
            prior_pipeline > ZERO
            and prior_position > prior_min
            and prior_stock - prior_reserve <= prior_min
        ),
        "quantity_limited": int(raw_order > rounded_order),
        "zero_or_missing_forecast": int(_decimal(row.get("prior_forecast_rate_sales")) <= ZERO),
        "demand_jump_over_trigger": int(
            bool(_clean(row.get("prior_evaluation_date")))
            and prior_position > prior_min
            and rounded_order <= ZERO
        ),
        "lead_time_risk": int(
            (selected_lead > 0 and p75 > selected_lead) or confidence in {"low", "unknown"}
        ),
        "zero_safety_stock": int(_decimal(row.get("prior_safety_stock_qty")) <= ZERO),
    }


def classify_loss_reason(row: Mapping[str, Any]) -> str:
    flags = loss_reason_flags(row)
    status = _clean(row.get("status"))
    if status in {"fruit", "newborn", "new_item", "sales_start"} and int(
        _decimal(row.get("launch_seed_pending"))
    ):
        return "new_sku_without_start_stock"
    if not _clean(row.get("prior_evaluation_date")):
        return "no_prior_control_state"
    if flags["quantity_limited"]:
        return "quantity_rounding_or_cap"
    if flags["pipeline_blocked_reorder"]:
        return "pipeline_counted_before_arrival"
    if flags["pipeline_present"]:
        return "replenishment_in_transit"
    if flags["zero_or_missing_forecast"]:
        return "zero_or_missing_forecast"
    if flags["demand_jump_over_trigger"]:
        return "demand_jump_over_min_trigger"
    if (
        int(_decimal(row.get("prior_triggered")))
        and _decimal(row.get("prior_recommended_order_qty")) <= ZERO
    ):
        return "trigger_without_order"
    return "base_target_underforecast"


def _enrich_loss_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    names_by_code: Mapping[str, str],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        business_date = date.fromisoformat(_clean(row["business_date"]))
        cost = _decimal(row.get("inventory_cost_per_unit_rub"))
        p75 = int(_decimal(row.get("current_lead_time_p75_days")))
        days_text = _clean(row.get("days_to_next_pipeline_arrival"))
        days_to_arrival = int(days_text) if days_text else None
        flags = loss_reason_flags(row)
        lost = _decimal(row.get("lost_observed_qty"))
        margin = _decimal(row.get("gross_margin_per_unit_rub"))
        row.update(flags)
        row.update(
            {
                "name": names_by_code.get(_clean(row.get("nomenclature_code")), ""),
                "period": "july" if business_date.month == 7 else "pre_july",
                "cost_band": _cost_band(cost),
                "lead_time_p75_band": _lead_time_band(p75),
                "lead_time_confidence": (
                    _clean(row.get("current_lead_time_confidence")) or "unknown"
                ),
                "pipeline_band": _pipeline_band(
                    _decimal(row.get("model_pipeline_qty")), days_to_arrival
                ),
                "reserve_state": (
                    "positive" if _decimal(row.get("effective_reserve_qty")) > ZERO else "zero"
                ),
                "loss_reason": classify_loss_reason(row),
                "lost_gross_margin_rub": str(lost * margin),
            }
        )
        output.append(row)
    output.sort(
        key=lambda row: (
            _clean(row.get("business_date")),
            _clean(row.get("nomenclature_code")),
        )
    )
    return output


def _aggregate_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    dimensions = (
        "loss_reason",
        "status",
        "demand_pattern_preperiod",
        "cost_band",
        "lead_time_confidence",
        "lead_time_p75_band",
        "period",
        "pipeline_band",
        "reserve_state",
    )
    total_lost = sum((_decimal(row.get("lost_observed_qty")) for row in rows), ZERO)
    output: list[dict[str, Any]] = []
    for dimension in dimensions:
        groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[_clean(row.get(dimension)) or "unknown"].append(row)
        for value, group in groups.items():
            lost = sum((_decimal(row.get("lost_observed_qty")) for row in group), ZERO)
            output.append(
                {
                    "segment_dimension": dimension,
                    "segment_value": value,
                    "lost_observed_qty": str(lost),
                    "lost_share": str(lost / total_lost if total_lost > ZERO else ZERO),
                    "lost_gross_margin_rub": str(
                        sum((_decimal(row.get("lost_gross_margin_rub")) for row in group), ZERO)
                    ),
                    "loss_event_rows": len(group),
                    "sku_count": len({_clean(row.get("nomenclature_code")) for row in group}),
                }
            )
    output.sort(
        key=lambda row: (
            _clean(row["segment_dimension"]),
            -_decimal(row["lost_observed_qty"]),
            _clean(row["segment_value"]),
        )
    )
    return output


def _top_sku_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[_clean(row.get("nomenclature_code"))].append(row)
    output: list[dict[str, Any]] = []
    for code, group in groups.items():
        lost_by_reason: dict[str, Decimal] = defaultdict(Decimal)
        lost_by_stage: dict[str, Decimal] = defaultdict(Decimal)
        for row in group:
            lost = _decimal(row.get("lost_observed_qty"))
            lost_by_reason[_clean(row.get("loss_reason"))] += lost
            lost_by_stage[_clean(row.get("status"))] += lost
        first = group[0]
        output.append(
            {
                "nomenclature_code": code,
                "name": _clean(first.get("name")),
                "lost_observed_qty": str(
                    sum((_decimal(row.get("lost_observed_qty")) for row in group), ZERO)
                ),
                "lost_gross_margin_rub": str(
                    sum((_decimal(row.get("lost_gross_margin_rub")) for row in group), ZERO)
                ),
                "loss_days": len(group),
                "first_loss_date": min(_clean(row.get("business_date")) for row in group),
                "last_loss_date": max(_clean(row.get("business_date")) for row in group),
                "main_loss_reason": max(lost_by_reason, key=lost_by_reason.get),
                "main_stage": max(lost_by_stage, key=lost_by_stage.get),
                "demand_pattern_preperiod": _clean(first.get("demand_pattern_preperiod")),
                "cost_band": _clean(first.get("cost_band")),
                "lead_time_confidence": _clean(first.get("lead_time_confidence")),
                "lead_time_p75_band": _clean(first.get("lead_time_p75_band")),
            }
        )
    output.sort(key=lambda row: -_decimal(row["lost_observed_qty"]))
    return output


def _prior_state_as_decision(row: Mapping[str, Any]) -> dict[str, Any] | None:
    evaluation_date = _clean(row.get("prior_evaluation_date"))
    if not evaluation_date:
        return None
    return {
        "decision_date": evaluation_date,
        "nomenclature_code": _clean(row.get("nomenclature_code")),
        "status": _clean(row.get("status")),
        "decision_trigger": "latest_causal_evaluation",
        "forecast_rate_sales": _clean(row.get("prior_forecast_rate_sales")),
        "selected_lead_time_days": _clean(row.get("prior_selected_lead_time_days")),
        "simulated_arrival_lead_time_days": _clean(
            row.get("prior_simulated_arrival_lead_time_days")
        ),
        "min_stock_qty": _clean(row.get("prior_min_stock_qty")),
        "max_stock_qty": _clean(row.get("prior_max_stock_qty")),
        "economic_safety_stock_qty": _clean(row.get("prior_safety_stock_qty")),
        "model_stock_qty": _clean(row.get("prior_model_stock_qty")),
        "reserve_qty": _clean(row.get("prior_reserve_qty")),
        "model_pipeline_qty": _clean(row.get("prior_model_pipeline_qty")),
        "effective_model_pipeline_qty": _clean(row.get("prior_effective_model_pipeline_qty")),
        "inventory_position_qty": _clean(row.get("prior_inventory_position_qty")),
        "recommended_order_qty_raw": _clean(row.get("prior_recommended_order_qty_raw")),
        "recommended_order_qty": _clean(row.get("prior_recommended_order_qty")),
        "base_pipeline_profile": _clean(row.get("prior_base_pipeline_profile")),
        "base_pipeline_fraction": _clean(row.get("prior_base_pipeline_fraction")),
        "base_pipeline_margin_to_cost_ratio": _clean(
            row.get("prior_base_pipeline_margin_to_cost_ratio")
        ),
        "base_pipeline_lot_risk_boundary": _clean(row.get("prior_base_pipeline_lot_risk_boundary")),
        "base_pipeline_lot_risk_boundary_days": _clean(
            row.get("prior_base_pipeline_lot_risk_boundary_days")
        ),
        "base_pipeline_lot_risky_qty": _clean(row.get("prior_base_pipeline_lot_risky_qty")),
    }


def _decision_arrival_date(row: Mapping[str, Any]) -> date | None:
    decision_text = _clean(row.get("decision_date"))
    if not decision_text:
        return None
    lead_days = int(
        _decimal(row.get("simulated_arrival_lead_time_days") or row.get("selected_lead_time_days"))
    )
    if lead_days <= 0:
        return None
    return date.fromisoformat(decision_text) + timedelta(days=max(1, lead_days))


def _weighted_signal_qty(row: Mapping[str, Any]) -> Decimal:
    return sum(
        (
            max(ZERO, _decimal(row.get(column)))
            for column in (
                "kmp4_open_weighted_qty",
                "site_order_open_weighted_qty",
                "site_cart_open_weighted_qty",
                "reserve_backlog_qty",
            )
        ),
        ZERO,
    )


def _recoverability(
    *,
    latest_prior_decision: Mapping[str, Any] | None,
    advance_decision: Mapping[str, Any] | None,
) -> tuple[str, str]:
    if latest_prior_decision is None:
        return "insufficient_control_history", "insufficient"
    if advance_decision is None:
        return "supply_or_signal_too_late", "low"
    if _decimal(advance_decision.get("recommended_order_qty")) > ZERO:
        return "ordered_but_target_too_low", "medium"
    if (
        _decimal(advance_decision.get("forecast_rate_sales")) > ZERO
        or _weighted_signal_qty(advance_decision) > ZERO
    ):
        min_qty = _decimal(advance_decision.get("min_stock_qty"))
        position = _decimal(advance_decision.get("inventory_position_qty"))
        free_stock = _decimal(advance_decision.get("model_stock_qty")) - _decimal(
            advance_decision.get("reserve_qty")
        )
        effective_pipeline = _decimal(advance_decision.get("effective_model_pipeline_qty"))
        if effective_pipeline > ZERO and free_stock <= min_qty and position > min_qty:
            return "pipeline_blocked_at_last_chance", "medium"
        if position > min_qty:
            return "stock_above_min_at_last_chance", "medium"
        return "triggered_without_quantity", "medium"
    return "no_advance_signal", "low"


def _episode_explanation(
    *,
    recoverability: str,
    mechanism: str,
    advance_decision: Mapping[str, Any] | None,
) -> str:
    if recoverability == "ordered_but_target_too_low":
        return (
            "До дефицита оставался полный срок поставки, и модель уже делала заказ, "
            "но рассчитанного количества не хватило до следующего спроса."
        )
    if recoverability == "stock_above_min_at_last_chance":
        return (
            "На последней дате, когда новый заказ ещё успевал, собственный запас был "
            "выше min. Модель считала этого достаточным, но затем фактический спрос "
            "израсходовал защиту быстрее прогноза."
        )
    if recoverability == "pipeline_blocked_at_last_chance":
        return (
            "На последней дате, когда новый заказ ещё успевал, свободный запас уже был "
            "не выше min, но учтённый товар в пути поднял inventory position и заблокировал заказ."
        )
    if recoverability == "triggered_without_quantity":
        return (
            "На последней своевременной проверке inventory position уже достиг min, "
            "но рассчитанная цель не требовала положительного заказа."
        )
    if recoverability == "no_advance_signal":
        return (
            "На последней дате, когда поставка ещё могла успеть, frozen-данные не "
            "показывали ни прогноза, ни подтверждённого сигнала спроса."
        )
    if recoverability == "supply_or_signal_too_late":
        suffix = (
            " На первой дате дефицита товар уже числился в пути."
            if mechanism in {"pipeline_counted_before_arrival", "replenishment_in_transit"}
            else ""
        )
        return (
            "Последнее доступное решение возникло позже, чем требовал исторический "
            "срок поставки; новый заказ с этой даты не успевал бы." + suffix
        )
    if advance_decision is None:
        return "До первой потери нет полного причинного состояния модели."
    return "Frozen-истории недостаточно, чтобы отделить ошибку модели от поставки."


def _human_check(recoverability: str, mechanism: str) -> str:
    checks = {
        "ordered_but_target_too_low": (
            "Проверить размер и дату реального заказа, разовую крупную продажу, "
            "снятые резервы и не был ли min/max занижен из-за отсутствия товара."
        ),
        "stock_above_min_at_last_chance": (
            "Сверить остаток и продажи после указанной даты: был ли всплеск спроса или "
            "min не оставлял времени на следующий недельный пересмотр и поставку."
        ),
        "pipeline_blocked_at_last_chance": (
            "Проверить каждую партию в пути: ordered_at, первоначальную обещанную дату, "
            "переносы и частичные приходы. Затем решать, какую долю pipeline считать надёжной."
        ),
        "triggered_without_quantity": (
            "Проверить, почему max совпал с inventory position: округление, скрытый спрос "
            "и минимальный размер партии."
        ),
        "no_advance_signal": (
            "Проверить поиск/клики/заказы сайта и обращения менеджеров до дефицита; "
            "если их не было, автоматический запас потребует отдельного бизнес-правила."
        ),
        "supply_or_signal_too_late": (
            "Проверить ordered_at и первоначальную обещанную дату поставщика, переносы, "
            "частичные приходы и отмены; текущий frozen pipeline этого не доказывает."
        ),
        "insufficient_control_history": (
            "Поднять историю до начала теста: заказ, обещанную дату, остаток и резерв."
        ),
    }
    result = checks[recoverability]
    if mechanism == "quantity_rounding_or_cap":
        result += " Отдельно проверить округление партии и максимальный лимит заказа."
    return result


def _build_episode(
    rows: Sequence[Mapping[str, Any]],
    *,
    decisions_by_code: Mapping[str, Sequence[Mapping[str, Any]]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    sequence: int,
) -> dict[str, Any]:
    first = rows[0]
    code = _clean(first.get("nomenclature_code"))
    start = date.fromisoformat(_clean(first.get("business_date")))
    end = date.fromisoformat(_clean(rows[-1].get("business_date")))
    candidate_decisions = [
        dict(row)
        for row in decisions_by_code.get(code, ())
        if _clean(row.get("decision_date"))
        and date.fromisoformat(_clean(row.get("decision_date"))) < start
    ]
    prior_state = _prior_state_as_decision(first)
    if prior_state is not None and all(
        _clean(row.get("decision_date")) != _clean(prior_state.get("decision_date"))
        for row in candidate_decisions
    ):
        candidate_decisions.append(prior_state)
    candidate_decisions.sort(key=lambda row: _clean(row.get("decision_date")))
    latest_prior = candidate_decisions[-1] if candidate_decisions else None
    advance_candidates = [
        row
        for row in candidate_decisions
        if (arrival := _decision_arrival_date(row)) is not None and arrival <= start
    ]
    advance_decision = advance_candidates[-1] if advance_candidates else None
    mechanism = classify_loss_reason(first)
    recoverability, confidence = _recoverability(
        latest_prior_decision=latest_prior,
        advance_decision=advance_decision,
    )
    lost_qty = sum((_decimal(row.get("lost_observed_qty")) for row in rows), ZERO)
    lost_margin = sum((_decimal(row.get("lost_gross_margin_rub")) for row in rows), ZERO)
    decision = advance_decision or latest_prior or {}
    decision_arrival = _decision_arrival_date(decision)
    decision_date_text = _clean(decision.get("decision_date"))
    decision_date = date.fromisoformat(decision_date_text) if decision_date_text else None
    observed_since_decision = (
        sum(
            (
                max(ZERO, _decimal(quantity))
                for business_date, quantity in sales_by_code.get(code, {}).items()
                if decision_date < business_date <= start
            ),
            ZERO,
        )
        if decision_date is not None
        else ZERO
    )
    forecast_since_decision = (
        _decimal(decision.get("forecast_rate_sales")) * Decimal((start - decision_date).days)
        if decision_date is not None
        else ZERO
    )
    stage = _clean(first.get("status"))
    return {
        "episode_id": f"{code}:{start.isoformat()}:{sequence}",
        "nomenclature_code": code,
        "name": _clean(first.get("name")),
        "episode_start": start.isoformat(),
        "episode_end": end.isoformat(),
        "episode_calendar_days": (end - start).days + 1,
        "loss_days": len(rows),
        "lost_observed_qty": str(lost_qty),
        "lost_gross_margin_rub": str(lost_margin),
        "status": stage,
        "stage_label": STAGE_LABELS.get(stage, stage or "Неизвестно"),
        "demand_pattern_preperiod": _clean(first.get("demand_pattern_preperiod")),
        "period": "july" if start.month == 7 else "pre_july",
        "mechanism": mechanism,
        "mechanism_label": REASON_LABELS.get(mechanism, mechanism),
        "recoverability": recoverability,
        "recoverability_label": RECOVERABILITY_LABELS[recoverability],
        "recoverability_confidence": confidence,
        "explanation": _episode_explanation(
            recoverability=recoverability,
            mechanism=mechanism,
            advance_decision=advance_decision,
        ),
        "human_check": _human_check(recoverability, mechanism),
        "first_loss_model_stock_before_qty": _clean(first.get("model_stock_before_demand_qty")),
        "first_loss_actual_stock_qty": _clean(first.get("actual_physical_stock_qty")),
        "first_loss_effective_reserve_qty": _clean(first.get("effective_reserve_qty")),
        "first_loss_pipeline_qty": _clean(first.get("model_pipeline_qty")),
        "first_loss_next_arrival_date": _clean(first.get("next_pipeline_arrival_date")),
        "decision_date": decision_date_text,
        "decision_trigger": _clean(decision.get("decision_trigger")),
        "decision_arrival_date": decision_arrival.isoformat() if decision_arrival else "",
        "decision_forecast_rate_sales": _clean(decision.get("forecast_rate_sales")),
        "decision_min_stock_qty": _clean(decision.get("min_stock_qty")),
        "decision_max_stock_qty": _clean(decision.get("max_stock_qty")),
        "decision_model_stock_qty": _clean(decision.get("model_stock_qty")),
        "decision_reserve_qty": _clean(decision.get("reserve_qty")),
        "decision_pipeline_qty": _clean(decision.get("model_pipeline_qty")),
        "decision_effective_pipeline_qty": _clean(decision.get("effective_model_pipeline_qty")),
        "decision_inventory_position_qty": _clean(decision.get("inventory_position_qty")),
        "decision_recommended_order_qty": _clean(decision.get("recommended_order_qty")),
        "decision_weighted_signal_qty": str(_weighted_signal_qty(decision)),
        "observed_demand_after_decision_to_first_loss_qty": str(observed_since_decision),
        "forecast_demand_after_decision_to_first_loss_qty": str(forecast_since_decision),
        "forecast_shortfall_to_first_loss_qty": str(
            observed_since_decision - forecast_since_decision
        ),
        "observed_above_forecast_to_first_loss": int(
            observed_since_decision > forecast_since_decision
        ),
        "decision_base_pipeline_fraction": _clean(decision.get("base_pipeline_fraction")),
        "decision_base_pipeline_lot_risky_qty": _clean(decision.get("base_pipeline_lot_risky_qty")),
    }


def group_loss_episodes(
    rows: Sequence[Mapping[str, Any]],
    *,
    decision_rows: Sequence[Mapping[str, Any]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]] | None = None,
) -> list[dict[str, Any]]:
    sales = sales_by_code or {}
    decisions_by_code: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in decision_rows:
        decisions_by_code[_clean(row.get("nomenclature_code"))].append(row)
    for group in decisions_by_code.values():
        group.sort(key=lambda row: _clean(row.get("decision_date")))

    loss_by_code: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        loss_by_code[_clean(row.get("nomenclature_code"))].append(row)
    episodes: list[dict[str, Any]] = []
    for code in sorted(loss_by_code):
        code_rows = sorted(loss_by_code[code], key=lambda row: _clean(row.get("business_date")))
        current: list[Mapping[str, Any]] = []
        previous_date: date | None = None
        sequence = 0
        for row in code_rows:
            row_date = date.fromisoformat(_clean(row.get("business_date")))
            if previous_date is not None and row_date > previous_date + timedelta(days=1):
                sequence += 1
                episodes.append(
                    _build_episode(
                        current,
                        decisions_by_code=decisions_by_code,
                        sales_by_code=sales,
                        sequence=sequence,
                    )
                )
                current = []
            current.append(row)
            previous_date = row_date
        if current:
            sequence += 1
            episodes.append(
                _build_episode(
                    current,
                    decisions_by_code=decisions_by_code,
                    sales_by_code=sales,
                    sequence=sequence,
                )
            )
    episodes.sort(
        key=lambda row: (
            _clean(row.get("episode_start")),
            _clean(row.get("nomenclature_code")),
        )
    )
    return episodes


def _aggregate_episodes(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    dimensions = ("recoverability", "mechanism", "status", "period")
    total = sum((_decimal(row.get("lost_observed_qty")) for row in rows), ZERO)
    output: list[dict[str, Any]] = []
    for dimension in dimensions:
        groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[_clean(row.get(dimension)) or "unknown"].append(row)
        for value, group in groups.items():
            lost = sum((_decimal(row.get("lost_observed_qty")) for row in group), ZERO)
            label = (
                RECOVERABILITY_LABELS.get(value, value)
                if dimension == "recoverability"
                else (
                    REASON_LABELS.get(value, value)
                    if dimension == "mechanism"
                    else (
                        STAGE_LABELS.get(value, value)
                        if dimension == "status"
                        else "Июль" if value == "july" else "Февраль–июнь"
                    )
                )
            )
            output.append(
                {
                    "segment_dimension": dimension,
                    "segment_value": value,
                    "segment_label": label,
                    "lost_observed_qty": str(lost),
                    "lost_share": str(lost / total if total > ZERO else ZERO),
                    "episode_count": len(group),
                    "sku_count": len({_clean(row.get("nomenclature_code")) for row in group}),
                }
            )
    output.sort(
        key=lambda row: (
            _clean(row.get("segment_dimension")),
            -_decimal(row.get("lost_observed_qty")),
            _clean(row.get("segment_value")),
        )
    )
    return output


def _forecast_diagnostics(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if _clean(row.get("decision_date")):
            groups[_clean(row.get("recoverability"))].append(row)
    output: list[dict[str, Any]] = []
    for category, group in groups.items():
        above = [row for row in group if int(row.get("observed_above_forecast_to_first_loss") or 0)]
        output.append(
            {
                "recoverability": category,
                "recoverability_label": RECOVERABILITY_LABELS.get(category, category),
                "episode_count": len(group),
                "actual_above_forecast_episode_count": len(above),
                "actual_above_forecast_episode_share": str(
                    Decimal(len(above)) / Decimal(len(group)) if group else ZERO
                ),
                "lost_observed_qty": str(
                    sum((_decimal(row.get("lost_observed_qty")) for row in group), ZERO)
                ),
                "lost_qty_where_actual_above_forecast": str(
                    sum((_decimal(row.get("lost_observed_qty")) for row in above), ZERO)
                ),
            }
        )
    output.sort(key=lambda row: -_decimal(row.get("lost_observed_qty")))
    return output


def _select_review_examples(
    episodes: Sequence[Mapping[str, Any]],
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    ranked = sorted(
        episodes,
        key=lambda row: (
            -_decimal(row.get("lost_observed_qty")),
            _clean(row.get("episode_start")),
            _clean(row.get("nomenclature_code")),
        ),
    )
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    selected_skus: set[str] = set()

    def add(row: Mapping[str, Any]) -> None:
        episode_id = _clean(row.get("episode_id"))
        code = _clean(row.get("nomenclature_code"))
        if episode_id in selected_ids or code in selected_skus or len(selected) >= limit:
            return
        selected_ids.add(episode_id)
        selected_skus.add(code)
        selected.append(dict(row))

    for category in RECOVERABILITY_LABELS:
        candidate = next(
            (row for row in ranked if _clean(row.get("recoverability")) == category),
            None,
        )
        if candidate is not None:
            add(candidate)
    for stage in ("sale", "sales_start", "working", "new_item"):
        candidate = next((row for row in ranked if _clean(row.get("status")) == stage), None)
        if candidate is not None:
            add(candidate)
    july = next((row for row in ranked if _clean(row.get("period")) == "july"), None)
    if july is not None:
        add(july)
    for row in ranked:
        add(row)
        if len(selected) >= limit:
            break
    for index, row in enumerate(selected, start=1):
        row["review_priority"] = index
    return selected


def _ranked(aggregate_rows: Sequence[Mapping[str, Any]], dimension: str) -> list[dict[str, Any]]:
    return [
        dict(row) for row in aggregate_rows if _clean(row.get("segment_dimension")) == dimension
    ]


def _markdown_table(rows: Sequence[Mapping[str, Any]]) -> str:
    body = ["| Причина | Потеряно, ед. | Доля | SKU |", "| --- | ---: | ---: | ---: |"]
    for row in rows:
        body.append(
            "| {value} | {lost:,.2f} | {share:.1%} | {sku} |".format(
                value=_clean(row.get("segment_value")),
                lost=float(_decimal(row.get("lost_observed_qty"))),
                share=float(_decimal(row.get("lost_share"))),
                sku=int(row.get("sku_count") or 0),
            ).replace(",", " ")
        )
    return "\n".join(body)


def _build_markdown(summary: Mapping[str, Any]) -> str:
    headline = summary["headline"]
    causes = summary["rankings"]["loss_reason"]
    stages = summary["rankings"]["status"][:4]
    patterns = summary["rankings"]["demand_pattern_preperiod"][:5]
    return (
        "# Почему базовый min/max недообслужил записанные продажи\n\n"
        "## Короткий вывод\n\n"
        f"Контроль потерял **{float(_decimal(headline['lost_observed_qty'])):,.2f}** "
        f"единицы записанных продаж на **{headline['loss_sku_count']} SKU**. "
        "Это replay одной и той же frozen-модели: стадии и ускорительная надбавка "
        "не менялись, production-заказы не создавались.\n\n"
        "Главный смысл диагностики: одна строка потери назначена одной основной "
        "причине, а параллельные риски — товар в пути, нулевой safety stock и "
        "неопределённый срок — сохранены отдельными флагами. Поэтому таблица не "
        "выдаёт совпадение за доказанную причинность.\n\n"
        "## Основные причины\n\n"
        + _markdown_table(causes)
        + "\n\n## Где сосредоточен разрыв\n\n"
        + "По стадиям: "
        + ", ".join(
            f"`{row['segment_value']}` — {float(_decimal(row['lost_observed_qty'])):,.2f}"
            for row in stages
        ).replace(",", " ")
        + ".\n\nПо типу спроса: "
        + ", ".join(
            f"`{row['segment_value']}` — {float(_decimal(row['lost_observed_qty'])):,.2f}"
            for row in patterns
        ).replace(",", " ")
        + ".\n\n"
        f"До июля потеряно `{headline['pre_july_lost_qty']}`, в июле — "
        f"`{headline['july_lost_qty']}`. Топ-20 SKU дают "
        f"`{float(_decimal(headline['top_20_loss_share'])):.1%}` разрыва.\n\n"
        "## Ограничения\n\n"
        "Диагностика объясняет механизм внутри симулятора, но не доказывает, что "
        "каждый фактический приход поставщика повторился бы в production. Решения "
        "после начала эпизода не используются; выбор SKU по будущей потере запрещён. "
        "PDF не создавался.\n"
    )


def _number_ru(value: Any, digits: int = 2) -> str:
    return f"{float(_decimal(value)):,.{digits}f}".replace(",", " ").replace(".", ",")


def _percent_ru(value: Any, digits: int = 1) -> str:
    return f"{float(_decimal(value)) * 100:.{digits}f}%".replace(".", ",")


def _episode_ranking(summary: Mapping[str, Any], dimension: str) -> list[Mapping[str, Any]]:
    return [
        row
        for row in summary["episode_rankings"]
        if _clean(row.get("segment_dimension")) == dimension
    ]


def _build_html(
    summary: Mapping[str, Any],
    examples: Sequence[Mapping[str, Any]],
) -> str:
    headline = summary["headline"]
    recoverability = _episode_ranking(summary, "recoverability")
    mechanisms = _episode_ranking(summary, "mechanism")
    stages = _episode_ranking(summary, "status")
    forecast_by_category = {
        _clean(row.get("recoverability")): row
        for row in summary.get("advance_forecast_diagnostics", ())
    }
    stock_forecast_share = _percent_ru(
        forecast_by_category.get("stock_above_min_at_last_chance", {}).get(
            "actual_above_forecast_episode_share"
        )
    )
    pipeline_forecast_share = _percent_ru(
        forecast_by_category.get("pipeline_blocked_at_last_chance", {}).get(
            "actual_above_forecast_episode_share"
        )
    )
    max_bar = max((_decimal(row.get("lost_observed_qty")) for row in recoverability), default=ZERO)

    bar_rows = []
    for row in recoverability:
        width = (
            float(_decimal(row.get("lost_observed_qty")) / max_bar) * 100 if max_bar > ZERO else 0
        )
        bar_rows.append(
            "<div class='bar-row'>"
            f"<div class='bar-title'>{escape(_clean(row.get('segment_label')))}</div>"
            f"<div class='bar-track'><div class='bar-fill' style='width:{width:.2f}%'></div></div>"
            f"<div class='bar-value'>{_number_ru(row.get('lost_observed_qty'))} ед. · "
            f"{_percent_ru(row.get('lost_share'))}</div></div>"
        )

    mechanism_rows = "".join(
        "<tr>"
        f"<td>{escape(_clean(row.get('segment_label')))}</td>"
        f"<td class='num'>{_number_ru(row.get('lost_observed_qty'))}</td>"
        f"<td class='num'>{_percent_ru(row.get('lost_share'))}</td>"
        f"<td class='num'>{int(row.get('episode_count') or 0)}</td>"
        f"<td class='num'>{int(row.get('sku_count') or 0)}</td>"
        "</tr>"
        for row in mechanisms
    )
    stage_rows = "".join(
        "<tr>"
        f"<td>{escape(_clean(row.get('segment_label')))}</td>"
        f"<td class='num'>{_number_ru(row.get('lost_observed_qty'))}</td>"
        f"<td class='num'>{_percent_ru(row.get('lost_share'))}</td>"
        f"<td class='num'>{int(row.get('episode_count') or 0)}</td>"
        "</tr>"
        for row in stages
    )
    example_rows = "".join(
        "<tr>"
        f"<td>{int(row.get('review_priority') or 0)}</td>"
        f"<td><strong>{escape(_clean(row.get('nomenclature_code')))}</strong><br>"
        f"<span class='muted'>{escape(_clean(row.get('name')))}</span></td>"
        f"<td>{escape(_clean(row.get('episode_start')))} — "
        f"{escape(_clean(row.get('episode_end')))}<br>"
        f"<strong>{_number_ru(row.get('lost_observed_qty'))} ед.</strong></td>"
        f"<td>{escape(_clean(row.get('stage_label')))}<br>"
        f"<span class='pill'>{escape(_clean(row.get('demand_pattern_preperiod')))}</span></td>"
        f"<td>{escape(_clean(row.get('recoverability_label')))}<br>"
        f"<span class='muted'>{escape(_clean(row.get('mechanism_label')))}</span></td>"
        f"<td>{escape(_clean(row.get('decision_date')) or 'нет')}<br>"
        f"прогноз {_number_ru(row.get('decision_forecast_rate_sales'), 3)} в день<br>"
        f"min/max {_number_ru(row.get('decision_min_stock_qty'))} / "
        f"{_number_ru(row.get('decision_max_stock_qty'))}<br>"
        f"позиция {_number_ru(row.get('decision_inventory_position_qty'))}<br>"
        f"заказ {_number_ru(row.get('decision_recommended_order_qty'))}<br>"
        f"спрос до потери: факт {_number_ru(row.get('observed_demand_after_decision_to_first_loss_qty'))} / "
        f"прогноз {_number_ru(row.get('forecast_demand_after_decision_to_first_loss_qty'))}</td>"
        f"<td>{escape(_clean(row.get('explanation')))}</td>"
        f"<td>{escape(_clean(row.get('human_check')))}</td>"
        "</tr>"
        for row in examples
    )
    scenario_label = escape(_clean(summary.get("scenario_role")))
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Почему P75 всё ещё теряет продажи</title>
  <style>
    :root {{ --ink:#15202b; --muted:#64748b; --line:#dce3ea; --blue:#1967d2;
      --blue-soft:#eaf2ff; --orange:#d97706; --bg:#f5f7fa; --card:#fff; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 Arial,sans-serif; }}
    main {{ max-width:1240px; margin:0 auto; padding:28px 20px 60px; }}
    h1 {{ font-size:34px; line-height:1.12; margin:0 0 8px; }}
    h2 {{ font-size:24px; margin:36px 0 12px; }}
    h3 {{ font-size:18px; margin:22px 0 8px; }}
    p {{ max-width:900px; }}
    .lead {{ color:var(--muted); font-size:17px; margin:0 0 24px; }}
    .cards {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; }}
    .card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px; }}
    .metric {{ font-size:28px; font-weight:700; line-height:1.1; }}
    .label {{ color:var(--muted); margin-top:7px; }}
    .callout {{ border-left:5px solid var(--orange); background:#fff7ed; padding:14px 16px;
      border-radius:8px; max-width:1000px; }}
    .bar-chart {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:18px; }}
    .bar-row {{ display:grid; grid-template-columns:280px 1fr 150px; gap:12px; align-items:center; margin:12px 0; }}
    .bar-title {{ font-weight:600; }} .bar-track {{ height:18px; background:#e8edf3; border-radius:9px; overflow:hidden; }}
    .bar-fill {{ height:100%; background:linear-gradient(90deg,var(--blue),#60a5fa); border-radius:9px; }}
    .bar-value {{ text-align:right; white-space:nowrap; }}
    .table-wrap {{ overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:14px; }}
    table {{ border-collapse:collapse; width:100%; min-width:720px; }}
    th,td {{ border-bottom:1px solid var(--line); padding:10px 12px; text-align:left; vertical-align:top; }}
    th {{ background:#eef3f8; position:sticky; top:0; z-index:1; }}
    tr:last-child td {{ border-bottom:0; }} .num {{ text-align:right; white-space:nowrap; }}
    .examples {{ min-width:1500px; font-size:13px; }} .examples td:nth-child(2) {{ min-width:260px; }}
    .examples td:nth-child(7),.examples td:nth-child(8) {{ min-width:300px; }}
    .muted {{ color:var(--muted); }} .pill {{ display:inline-block; padding:2px 7px; border-radius:999px;
      background:var(--blue-soft); color:#174ea6; margin-top:4px; }}
    ul {{ max-width:920px; }} code {{ background:#eef2f7; padding:2px 5px; border-radius:4px; }}
    @media (max-width:800px) {{ main {{ padding:18px 12px 40px; }} h1 {{ font-size:27px; }}
      .cards {{ grid-template-columns:1fr 1fr; }} .bar-row {{ grid-template-columns:1fr; gap:4px; }}
      .bar-value {{ text-align:left; }} }}
  </style>
</head>
<body><main>
  <h1>Почему вариант P75 всё ещё теряет продажи</h1>
  <p class="lead">Узкий причинный replay сценария <code>{scenario_label}</code> за
    {escape(_clean(summary.get('date_from')))} — {escape(_clean(summary.get('date_to')))}.
    Только frozen-данные, без production-заказов. PDF не создавался.</p>
  <div class="cards">
    <div class="card"><div class="metric">{_number_ru(headline['lost_observed_qty'])}</div><div class="label">пропущено записанных продаж, ед.</div></div>
    <div class="card"><div class="metric">{int(headline['loss_episode_count'])}</div><div class="label">непрерывных эпизодов дефицита</div></div>
    <div class="card"><div class="metric">{int(headline['loss_sku_count'])}</div><div class="label">SKU с потерей</div></div>
    <div class="card"><div class="metric">{_percent_ru(headline['top_20_loss_share'])}</div><div class="label">потери дают топ‑20 SKU</div></div>
  </div>
  <h2>Главный вывод</h2>
  <div class="callout"><strong>«Товар был в пути» — это механизм, а не готовое решение.</strong>
    В прошлой диагностике он объяснял почти весь разрыв, но P75 вернул только 97 продаж.
    Здесь потери разделены по более строгому вопросу: было ли решение модели достаточно
    заранее, чтобы обычная поставка вообще могла успеть. Даже категория «время было» —
    кандидат на разбор, а не обещание, что вся указанная продажа будет спасена.</div>
  <p><strong>Почему min/max не угадал:</strong> среди эпизодов, где в последний
    своевременный срок собственный запас был выше min, фактический спрос до дефицита
    превысил прогноз в {stock_forecast_share} случаев. Среди эпизодов, заблокированных
    pipeline, — в {pipeline_forecast_share}. Это условная статистика только по уже
    случившимся дефицитам; её нельзя превращать в общий процент надбавки без проверки
    на SKU без потерь.</p>
  <h2>Можно ли было среагировать заранее</h2>
  <div class="bar-chart">{''.join(bar_rows)}</div>
  <h2>Что происходило в момент дефицита</h2>
  <div class="table-wrap"><table><thead><tr><th>Механизм</th><th class="num">Потеря, ед.</th>
    <th class="num">Доля</th><th class="num">Эпизоды</th><th class="num">SKU</th></tr></thead>
    <tbody>{mechanism_rows}</tbody></table></div>
  <h2>По стадиям</h2>
  <div class="table-wrap"><table><thead><tr><th>Стадия</th><th class="num">Потеря, ед.</th>
    <th class="num">Доля</th><th class="num">Эпизоды</th></tr></thead><tbody>{stage_rows}</tbody></table></div>
  <h2>12 эпизодов для ручной проверки</h2>
  <p>Это не случайные строки: сначала взяты разные типы исправимости и стадии, затем
    крупнейшие оставшиеся потери. Таблица специально показывает, какое решение было
    доступно до дефицита и что нужно поднять глазами в 1С или у поставщика.</p>
  <div class="table-wrap"><table class="examples"><thead><tr><th>№</th><th>SKU</th><th>Эпизод</th>
    <th>Стадия</th><th>Вывод</th><th>Решение заранее</th><th>Почему так считаем</th>
    <th>Что проверить человеку</th></tr></thead><tbody>{example_rows}</tbody></table></div>
  <h2>Как провести разговор по этим примерам</h2>
  <ol>
    <li>Сначала открыть первые 3–5 SKU и сверить реальный <code>ordered_at</code>, первоначальную обещанную дату и частичные приходы.</li>
    <li>Затем отметить разовые оптовые продажи, замены одного дисплея другим и ошибки соответствия карточек.</li>
    <li>Для случаев «модель не заказала» проверить min/max и inventory position именно на указанную дату — это лучшие кандидаты на следующую формулу.</li>
    <li>Для случаев без сигнала решить бизнесом: нужен ли небольшой сервисный запас или только ручное наблюдение.</li>
  </ol>
  <h2>Ограничение вывода</h2>
  <p>Frozen-набор знает фактические приходы и состояние pipeline, но не хранит полную
    историю первоначальных обещаний поставщика. Поэтому отчёт не называет позднюю
    поставку «ошибкой модели» без ручной сверки. Все категории взаимоисключающие и
    сверены с общей потерей; будущие решения не использовались.</p>
</main></body></html>"""


def _scenario_for_role(
    *,
    quick_summary: Mapping[str, Any],
    scenarios: Sequence[frozen.FrozenScenario],
    scenario_role: str,
) -> frozen.FrozenScenario:
    if scenario_role not in {"control", "hypothesis", "cautious"}:
        raise ValueError("scenario_role must be control, hypothesis or cautious")
    source_roles = quick_summary["source_scenario_roles"]
    selection = frozen.select_scenarios(
        scenarios,
        run_mode=frozen.RUN_MODE_QUICK,
        control_scenario_id=source_roles["control"],
        hypothesis_scenario_id=source_roles["hypothesis"],
        cautious_scenario_id=source_roles["cautious"],
    )
    pipeline = quick_summary.get("quick_base_pipeline", {})
    if pipeline.get("enabled"):
        selection = frozen.apply_quick_base_pipeline_profiles(
            selection,
            hypothesis_profile=_clean(pipeline.get("hypothesis_profile")),
            cautious_profile=_clean(pipeline.get("cautious_profile")),
        )
    by_id = {scenario.scenario_id: scenario for scenario in selection.scenarios}
    selected = by_id[selection.scenario_roles[scenario_role]]
    expected = _clean(quick_summary.get("scenario_roles", {}).get(scenario_role))
    if expected and selected.scenario_id != expected:
        raise ValueError(
            f"reconstructed scenario does not match quick artifact: {selected.scenario_id} != {expected}"
        )
    return selected


def build_analysis(
    *,
    preflight_dir: Path,
    quick_result_dir: Path,
    output_dir: Path,
    policy_json: Path,
    scenario_config_json: Path,
    scenario_role: str = "control",
) -> dict[str, Any]:
    inputs = _prepare_inputs(preflight_dir)
    quick_summary = json.loads(
        (quick_result_dir / "frozen-summary.json").read_text(encoding="utf-8")
    )
    replay_scenario = _scenario_for_role(
        quick_summary=quick_summary,
        scenarios=inputs["frozen_scenarios"],
        scenario_role=scenario_role,
    )
    replay = frozen.simulate_scenario(
        scenario=replay_scenario,
        fact_rows_by_date=inputs["fact_rows_by_date"],
        decision_rows_by_date=inputs["decision_rows_by_date"],
        initial_pipeline_rows=inputs["initial_pipeline"],
        sales_by_code=inputs["sales_by_code"],
        policy=load_auto_order_policy(policy_json),
        config=load_scenario_config(scenario_config_json),
        date_from=inputs["date_from"],
        date_to=inputs["date_to"],
        keep_detail=True,
        demand_sample_cache={},
    )
    names_by_code = {
        code: _clean(row.get("name")) for code, row in inputs["first_decision_by_code"].items()
    }
    loss_rows = _enrich_loss_rows(replay.loss_rows, names_by_code=names_by_code)
    aggregate_rows = _aggregate_rows(loss_rows)
    top_skus = _top_sku_rows(loss_rows)
    episodes = group_loss_episodes(
        loss_rows,
        decision_rows=replay.decision_rows,
        sales_by_code=inputs["sales_by_code"],
    )
    episode_segments = _aggregate_episodes(episodes)
    forecast_diagnostics = _forecast_diagnostics(episodes)
    review_examples = _select_review_examples(episodes)
    total_lost = sum((_decimal(row.get("lost_observed_qty")) for row in loss_rows), ZERO)
    episode_lost = sum((_decimal(row.get("lost_observed_qty")) for row in episodes), ZERO)
    metric_lost = sum((metric.lost_observed_qty for metric in replay.model.values()), ZERO)
    persisted_role = next(
        row
        for row in quick_summary["quick_comparison"]
        if _clean(row.get("scenario_role")) == scenario_role
    )
    persisted_lost = _decimal(quick_summary["base_actual"]["observed_demand_qty"]) - _decimal(
        persisted_role["served_observed_qty"]
    )
    period_index = {row["segment_value"]: row for row in _ranked(aggregate_rows, "period")}
    flag_totals = {
        flag: str(
            sum(
                (
                    _decimal(row.get("lost_observed_qty"))
                    for row in loss_rows
                    if int(row.get(flag) or 0)
                ),
                ZERO,
            )
        )
        for flag in (
            "pipeline_present",
            "pipeline_blocked_reorder",
            "quantity_limited",
            "zero_or_missing_forecast",
            "demand_jump_over_trigger",
            "lead_time_risk",
            "zero_safety_stock",
        )
    }
    top_20_lost = sum((_decimal(row.get("lost_observed_qty")) for row in top_skus[:20]), ZERO)
    summary: dict[str, Any] = {
        "schema": "display_auto_order_control_gap_diagnostics.v2",
        "source_preflight_manifest_sha256": _sha256(preflight_dir / "run-manifest.json"),
        "source_quick_summary_sha256": _sha256(quick_result_dir / "frozen-summary.json"),
        "scenario_role": scenario_role,
        "scenario_id": replay_scenario.scenario_id,
        "date_from": inputs["date_from"].isoformat(),
        "date_to": inputs["date_to"].isoformat(),
        "diagnostic_only": True,
        "production_authorized": False,
        "pdf_created": False,
        "headline": {
            "lost_observed_qty": str(total_lost),
            "loss_event_rows": len(loss_rows),
            "loss_episode_count": len(episodes),
            "loss_sku_count": len(top_skus),
            "lost_gross_margin_rub": str(
                sum((_decimal(row.get("lost_gross_margin_rub")) for row in loss_rows), ZERO)
            ),
            "pre_july_lost_qty": _clean(period_index.get("pre_july", {}).get("lost_observed_qty")),
            "july_lost_qty": _clean(period_index.get("july", {}).get("lost_observed_qty")),
            "top_20_loss_share": str(top_20_lost / total_lost if total_lost > ZERO else ZERO),
        },
        "rankings": {
            dimension: _ranked(aggregate_rows, dimension)
            for dimension in (
                "loss_reason",
                "status",
                "demand_pattern_preperiod",
                "cost_band",
                "lead_time_confidence",
                "lead_time_p75_band",
                "period",
                "pipeline_band",
                "reserve_state",
            )
        },
        "overlapping_risk_exposure_lost_qty": flag_totals,
        "episode_rankings": episode_segments,
        "advance_forecast_diagnostics": forecast_diagnostics,
        "reconciliation": {
            "loss_detail_minus_replay_metric": str(total_lost - metric_lost),
            "episode_detail_minus_loss_detail": str(episode_lost - total_lost),
            "replay_metric_minus_persisted_scenario": str(metric_lost - persisted_lost),
        },
        "method": {
            "primary_reason": "one mutually exclusive reason per lost SKU-day, based on the prior causal control evaluation",
            "episode": "consecutive calendar loss days for one SKU are one episode",
            "recoverability": "latest replay decision whose simulated arrival was no later than the first loss date; category is diagnostic exposure, not promised saved sales",
            "risk_flags": "non-exclusive exposures retained separately; they must not be summed as causes",
            "demand_pattern": "52 completed weekly buckets before the frozen period",
            "no_look_ahead": "only decisions strictly before episode start are eligible; future decisions and cancellations are excluded",
            "supplier_limitation": "frozen data does not contain full ordered_at and original promised-date history",
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "lost-sales-events.csv", loss_rows)
    _write_csv(output_dir / "lost-sales-segments.csv", aggregate_rows)
    _write_csv(output_dir / "lost-sales-top-skus.csv", top_skus)
    _write_csv(output_dir / "loss-episodes.csv", episodes)
    _write_csv(output_dir / "loss-episode-segments.csv", episode_segments)
    _write_csv(output_dir / "review-examples.csv", review_examples)
    (output_dir / "analysis-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "CONTROL-GAP-DIAGNOSTIC.md").write_text(
        _build_markdown(summary), encoding="utf-8"
    )
    (output_dir / "P75-LOSS-REVIEW.html").write_text(
        _build_html(summary, review_examples), encoding="utf-8"
    )
    artifact_names = (
        "analysis-summary.json",
        "CONTROL-GAP-DIAGNOSTIC.md",
        "lost-sales-events.csv",
        "lost-sales-segments.csv",
        "lost-sales-top-skus.csv",
        "loss-episodes.csv",
        "loss-episode-segments.csv",
        "review-examples.csv",
        "P75-LOSS-REVIEW.html",
    )
    manifest = {
        "schema": "display_auto_order_control_gap_diagnostics_manifest.v2",
        "diagnostic_only": True,
        "production_authorized": False,
        "pdf_created": False,
        "files": {name: _sha256(output_dir / name) for name in artifact_names},
    }
    (output_dir / "analysis-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-dir", type=Path, required=True)
    parser.add_argument("--quick-result-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--scenario-role",
        choices=("control", "hypothesis", "cautious"),
        default="control",
        help="Quick scenario role to replay; use cautious for the P75 v16 variant.",
    )
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
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    summary = build_analysis(
        preflight_dir=args.preflight_dir,
        quick_result_dir=args.quick_result_dir,
        output_dir=args.output_dir,
        policy_json=args.auto_order_policy_json,
        scenario_config_json=args.scenario_config_json,
        scenario_role=args.scenario_role,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
