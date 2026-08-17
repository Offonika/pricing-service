from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.display_family_order_recommendation import (
    FAMILY_RECOMMENDATION_COLUMNS,
    apply_display_family_order_recommendations,
    display_family_order_recommendation_summary,
    reset_display_family_order_recommendations,
)
from app.services.display_family_registry import (
    ActiveDisplayFamilyMemberContext,
    load_active_display_family_member_contexts,
)
from tasks.report_display_supplier_lead_time_history import display_group_key

DEFAULT_REPORT_ROOT = Path("reports/assortment_lifecycle")
DEFAULT_POLICY_JSON = Path("config/assortment/display-auto-order-policy.json")
DEFAULT_OUTPUT_CSV = (
    DEFAULT_REPORT_ROOT
    / date.today().isoformat()
    / "display-auto-order-adaptive-lead-time-comparison.csv"
)
DEFAULT_OUTPUT_JSON = (
    DEFAULT_REPORT_ROOT
    / date.today().isoformat()
    / "display-auto-order-adaptive-lead-time-comparison-summary.json"
)
DEFAULT_SYNC_READY_CSV = (
    DEFAULT_REPORT_ROOT / date.today().isoformat() / "display-auto-order-adaptive-sync-ready.csv"
)

CSV_COLUMNS = [
    "nomenclature_code",
    "name",
    "analog_group_id",
    "analog_role",
    "speed_tier",
    "current_decision",
    "adaptive_decision",
    "current_recommended_order_qty",
    "adaptive_recommended_order_qty",
    "adaptive_recommended_order_qty_raw",
    "qty_delta",
    "current_target_stock_qty",
    "adaptive_target_stock_qty",
    "target_stock_delta",
    "current_effective_target_days",
    "adaptive_effective_target_days",
    "effective_target_days_delta",
    "current_lead_time_days",
    "adaptive_lead_time_days",
    "lead_time_days_delta",
    "adaptive_supplier_prepare_days",
    "adaptive_logistics_days",
    "adaptive_safety_stock_days",
    "adaptive_forecast_qty",
    "adaptive_safety_stock_qty",
    "free_stock_qty",
    "incoming_qty",
    "lead_time_source_level",
    "lead_time_confidence",
    "lead_time_applied",
    "supplier_name",
    "responsible_name",
    "supplier_order_line_count",
    "missing_cargo_count",
    "missing_receipt_after_cargo_count",
    "seasonality_adjustment_days",
    "seasonality_week_start",
    "seasonality_route_risk_level",
    "seasonality_signal",
    "estimated_purchase_value_delta",
    "action_ru",
    "reason_ru",
    "warnings",
]


CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1, "": 0}
APPLICABLE_CONFIDENCE = {"high", "medium"}
BLOCKING_WARNING_CODES = {
    "not_auto_order_allowed",
    "analog_winner_not_auto_order_allowed",
}


def main() -> int:
    args = _parse_args()
    dry_rows = read_csv(args.dry_run_csv)
    lead_time_rows = read_csv(args.lead_time_csv)
    seasonality_rows = read_csv(args.seasonality_csv) if args.seasonality_csv else []
    policy = load_policy(args.auto_order_policy_json)
    comparison_rows = build_comparison_rows(
        dry_rows,
        lead_time_rows,
        seasonality_rows=seasonality_rows,
        policy=policy,
        as_of=args.as_of,
        recent_seasonality_weeks=args.recent_seasonality_weeks,
    )
    sync_ready_rows = (
        build_sync_ready_rows(dry_rows, comparison_rows) if args.sync_ready_csv else []
    )
    sync_ready_family_summary: dict[str, Any] | None = None
    if sync_ready_rows and args.use_active_display_family_registry:
        registry_error = ""
        membership_by_code = {}
        settings = get_settings()
        engine = build_engine(settings.database_url, pool_pre_ping=True)
        try:
            with Session(engine) as session:
                membership_by_code = load_active_display_family_member_contexts(
                    session,
                    nomenclature_codes=[
                        _clean(row.get("nomenclature_code")) for row in sync_ready_rows
                    ],
                )
        except Exception as exc:  # noqa: BLE001 - final shadow must fail closed.
            registry_error = f"{type(exc).__name__}: {exc}"
        finally:
            engine.dispose()
        sync_ready_family_summary = refresh_sync_ready_family_recommendations(
            sync_ready_rows,
            membership_by_code=membership_by_code,
            registry_error=registry_error,
        )
    summary = build_summary(
        comparison_rows,
        dry_run_csv=args.dry_run_csv,
        lead_time_csv=args.lead_time_csv,
        seasonality_csv=args.seasonality_csv,
        as_of=args.as_of,
    )
    if args.sync_ready_csv:
        summary = {
            **summary,
            "sync_ready_csv": str(args.sync_ready_csv),
            "sync_ready_order_rows": sum(
                1
                for row in sync_ready_rows
                if _clean(row.get("dry_run_decision")) == "order"
                and (_decimal(row.get("recommended_order_qty")) or Decimal("0")) > 0
            ),
            "sync_ready_total_recommended_order_qty": _out_decimal(
                sum(
                    (_decimal(row.get("recommended_order_qty")) or Decimal("0"))
                    for row in sync_ready_rows
                    if _clean(row.get("dry_run_decision")) == "order"
                )
            ),
        }
        if sync_ready_family_summary is not None:
            summary["sync_ready_display_family_order_recommendation"] = sync_ready_family_summary
    write_csv(args.output_csv, comparison_rows, CSV_COLUMNS)
    if args.sync_ready_csv:
        sync_columns = list(dry_rows[0].keys()) if dry_rows else []
        if args.use_active_display_family_registry:
            sync_columns = list(dict.fromkeys([*sync_columns, *FAMILY_RECOMMENDATION_COLUMNS]))
        write_csv(args.sync_ready_csv, sync_ready_rows, sync_columns)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    payload = {
        "status": "ready",
        "output_csv": str(args.output_csv),
        "output_json": str(args.output_json) if args.output_json else None,
        **summary,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_comparison_rows(
    dry_rows: Sequence[Mapping[str, Any]],
    lead_time_rows: Sequence[Mapping[str, Any]],
    *,
    seasonality_rows: Sequence[Mapping[str, Any]] = (),
    policy: Mapping[str, Any] | None = None,
    as_of: date | None = None,
    recent_seasonality_weeks: int = 8,
) -> list[dict[str, Any]]:
    policy = policy or {}
    as_of = as_of or date.today()
    order_rounding_rules = tuple(policy.get("order_rounding_rules") or ())
    code_index, group_index = build_lead_time_indexes(lead_time_rows)
    recent_signals = tuple(
        _recent_seasonality_signals(
            seasonality_rows,
            as_of=as_of,
            recent_weeks=recent_seasonality_weeks,
        )
    )
    result: list[dict[str, Any]] = []
    for dry_row in dry_rows:
        row = dict(dry_row)
        code = _clean(row.get("nomenclature_code"))
        group_key = display_group_key(row)
        lead_candidate, source_level = choose_lead_time_candidate(
            code,
            group_key,
            code_index=code_index,
            group_index=group_index,
        )
        lead_applied = bool(
            lead_candidate
            and _clean(lead_candidate.get("lead_time_confidence")) in APPLICABLE_CONFIDENCE
            and _int_or_none(lead_candidate.get("recommended_supplier_prepare_days")) is not None
            and _int_or_none(lead_candidate.get("recommended_logistics_days")) is not None
        )
        current_prepare = _int_or_none(row.get("supplier_prepare_days")) or 0
        current_logistics = _int_or_none(row.get("logistics_days")) or 0
        adaptive_prepare = current_prepare
        adaptive_logistics = current_logistics
        warnings: list[str] = []
        if lead_applied and lead_candidate:
            adaptive_prepare = (
                _int_or_none(lead_candidate.get("recommended_supplier_prepare_days"))
                or current_prepare
            )
            adaptive_logistics = _int_or_none(lead_candidate.get("recommended_logistics_days")) or (
                current_logistics
            )
            warnings.append("adaptive_lead_time_applied")
        elif lead_candidate:
            warnings.append("lead_time_low_confidence_fallback")
        else:
            warnings.append("lead_time_missing_fallback")

        seasonality_adjustment = seasonality_adjustment_for_candidate(
            lead_candidate,
            recent_signals,
        )
        if seasonality_adjustment["prepare_delta_days"]:
            adaptive_prepare += int(seasonality_adjustment["prepare_delta_days"])
        if seasonality_adjustment["logistics_delta_days"]:
            adaptive_logistics += int(seasonality_adjustment["logistics_delta_days"])
        if seasonality_adjustment["total_adjustment_days"]:
            warnings.append("recent_seasonality_adjustment_applied")

        comparison = build_row_comparison(
            row,
            lead_candidate=lead_candidate,
            source_level=source_level,
            lead_applied=lead_applied,
            adaptive_prepare=adaptive_prepare,
            adaptive_logistics=adaptive_logistics,
            seasonality_adjustment=seasonality_adjustment,
            order_rounding_rules=order_rounding_rules,
            warnings=warnings,
        )
        result.append(comparison)
    return result


def build_row_comparison(
    row: Mapping[str, Any],
    *,
    lead_candidate: Mapping[str, Any] | None,
    source_level: str,
    lead_applied: bool,
    adaptive_prepare: int,
    adaptive_logistics: int,
    seasonality_adjustment: Mapping[str, Any],
    order_rounding_rules: Sequence[Mapping[str, Any]],
    warnings: Sequence[str],
) -> dict[str, Any]:
    current_qty = _decimal(row.get("recommended_order_qty")) or Decimal("0")
    current_target_stock = _decimal(row.get("target_stock_qty")) or Decimal("0")
    current_effective_days = _int_or_none(row.get("effective_target_days")) or 0
    current_lead_days = _int_or_none(row.get("lead_time_days")) or 0
    role = _clean(row.get("analog_role"))
    speed_action = _clean(row.get("speed_rule_action"))
    source_warnings = set(_split_codes(row.get("warnings")))

    adaptive_safety_days = _int_or_none(row.get("speed_rule_safety_stock_days"))
    if adaptive_safety_days is None:
        adaptive_safety_days = _int_or_none(row.get("safety_stock_days")) or 0
    max_effective_days = _int_or_none(row.get("speed_max_effective_target_days"))
    target_days = _int_or_none(row.get("target_days")) or 0
    cadence_days = _int_or_none(row.get("order_cadence_days")) or 0
    delay_buffer_days = _int_or_none(row.get("supplier_delay_buffer_days")) or 0
    receiving_days = _int_or_none(row.get("receiving_buffer_days")) or 0
    adaptive_lead_days = adaptive_prepare + adaptive_logistics
    adaptive_uncapped_effective_days = (
        target_days
        + cadence_days
        + adaptive_lead_days
        + delay_buffer_days
        + receiving_days
        + adaptive_safety_days
    )
    adaptive_effective_days = adaptive_uncapped_effective_days
    if max_effective_days is not None:
        adaptive_effective_days = min(adaptive_effective_days, max_effective_days)
    forecast_days = max(0, adaptive_effective_days - adaptive_safety_days)

    blocked_by_status = bool(source_warnings & BLOCKING_WARNING_CODES)
    transition_to_better = role == "transition_to_better_analog"
    slow_review = speed_action == "manual_review"
    group_role = role in {"primary_analog", "transition_to_better_analog"}
    if group_role:
        avg_daily = _decimal(row.get("speed_group_avg_daily_sales_qty")) or Decimal("0")
        free_stock = _decimal(row.get("analog_group_free_stock_qty")) or Decimal("0")
        incoming = _decimal(row.get("analog_group_incoming_qty")) or Decimal("0")
    else:
        avg_daily = _decimal(row.get("avg_daily_sales_qty")) or Decimal("0")
        free_stock = _decimal(row.get("free_stock_qty")) or Decimal("0")
        incoming = _decimal(row.get("incoming_qty")) or Decimal("0")

    adaptive_forecast_qty = _ceil(avg_daily * Decimal(str(forecast_days)))
    adaptive_safety_qty = _ceil(avg_daily * Decimal(str(adaptive_safety_days)))
    adaptive_target_stock = adaptive_forecast_qty + adaptive_safety_qty
    adaptive_qty_raw = _ceil(max(Decimal("0"), adaptive_target_stock - free_stock - incoming))
    adaptive_qty = rounded_order_qty(adaptive_qty_raw, order_rounding_rules)

    adaptive_decision = "order" if adaptive_qty > 0 else "do_not_order"
    action_ru = "рассчитать по живому сроку"
    comparison_warnings = list(warnings)
    if transition_to_better:
        adaptive_qty = Decimal("0")
        adaptive_qty_raw = Decimal("0")
        adaptive_decision = "do_not_order"
        action_ru = "не заказывать старый аналог"
        comparison_warnings.append("analog_transition_preserved")
    elif slow_review:
        adaptive_qty = Decimal("0")
        adaptive_qty_raw = Decimal("0")
        adaptive_decision = "manual_review"
        action_ru = "slow оставить на ручной review"
        comparison_warnings.append("slow_manual_review_preserved")
    elif blocked_by_status:
        adaptive_qty = Decimal("0")
        adaptive_qty_raw = Decimal("0")
        adaptive_decision = "manual_review"
        action_ru = "не включать без ручного разрешения"
        comparison_warnings.append("manual_blocker_preserved")

    qty_delta = adaptive_qty - current_qty
    target_delta = adaptive_target_stock - current_target_stock
    value_delta = qty_delta * (_decimal(row.get("latest_purchase_price")) or Decimal("0"))
    reason = adaptive_reason(
        current_qty=current_qty,
        adaptive_qty=adaptive_qty,
        current_effective_days=current_effective_days,
        adaptive_effective_days=adaptive_effective_days,
        lead_applied=lead_applied,
        lead_candidate=lead_candidate,
        seasonality_adjustment=seasonality_adjustment,
    )
    return {
        "nomenclature_code": _clean(row.get("nomenclature_code")),
        "name": _clean(row.get("name")),
        "analog_group_id": _clean(row.get("analog_group_id")),
        "analog_role": role,
        "speed_tier": _clean(row.get("speed_tier")),
        "current_decision": _clean(row.get("dry_run_decision")),
        "adaptive_decision": adaptive_decision,
        "current_recommended_order_qty": _out_decimal(current_qty),
        "adaptive_recommended_order_qty": _out_decimal(adaptive_qty),
        "adaptive_recommended_order_qty_raw": _out_decimal(adaptive_qty_raw),
        "qty_delta": _out_decimal(qty_delta),
        "current_target_stock_qty": _out_decimal(current_target_stock),
        "adaptive_target_stock_qty": _out_decimal(adaptive_target_stock),
        "target_stock_delta": _out_decimal(target_delta),
        "current_effective_target_days": current_effective_days,
        "adaptive_effective_target_days": adaptive_effective_days,
        "effective_target_days_delta": adaptive_effective_days - current_effective_days,
        "current_lead_time_days": current_lead_days,
        "adaptive_lead_time_days": adaptive_lead_days,
        "lead_time_days_delta": adaptive_lead_days - current_lead_days,
        "adaptive_supplier_prepare_days": adaptive_prepare,
        "adaptive_logistics_days": adaptive_logistics,
        "adaptive_safety_stock_days": adaptive_safety_days,
        "adaptive_forecast_qty": _out_decimal(adaptive_forecast_qty),
        "adaptive_safety_stock_qty": _out_decimal(adaptive_safety_qty),
        "free_stock_qty": _out_decimal(free_stock),
        "incoming_qty": _out_decimal(incoming),
        "lead_time_source_level": source_level,
        "lead_time_confidence": (
            _clean(lead_candidate.get("lead_time_confidence")) if lead_candidate else ""
        ),
        "lead_time_applied": int(lead_applied),
        "supplier_name": _clean(lead_candidate.get("supplier_name")) if lead_candidate else "",
        "responsible_name": (
            _clean(lead_candidate.get("responsible_name")) if lead_candidate else ""
        ),
        "supplier_order_line_count": (
            _clean(lead_candidate.get("order_line_count")) if lead_candidate else ""
        ),
        "missing_cargo_count": (
            _clean(lead_candidate.get("missing_cargo_count")) if lead_candidate else ""
        ),
        "missing_receipt_after_cargo_count": (
            _clean(lead_candidate.get("missing_receipt_after_cargo_count"))
            if lead_candidate
            else ""
        ),
        "seasonality_adjustment_days": seasonality_adjustment["total_adjustment_days"],
        "seasonality_week_start": seasonality_adjustment["week_start"],
        "seasonality_route_risk_level": seasonality_adjustment["route_risk_level"],
        "seasonality_signal": seasonality_adjustment["signal"],
        "estimated_purchase_value_delta": _out_decimal(value_delta),
        "action_ru": action_ru,
        "reason_ru": reason,
        "warnings": "; ".join(sorted(set(comparison_warnings))),
    }


def build_sync_ready_rows(
    dry_rows: Sequence[Mapping[str, Any]],
    comparison_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if len(dry_rows) != len(comparison_rows):
        raise ValueError(
            "dry-run rows and adaptive comparison rows must have the same length "
            f"({len(dry_rows)} != {len(comparison_rows)})"
        )
    rows: list[dict[str, Any]] = []
    for dry_row, comparison_row in zip(dry_rows, comparison_rows, strict=True):
        row = dict(dry_row)
        adaptive_qty = _clean(comparison_row.get("adaptive_recommended_order_qty"))
        adaptive_qty_raw = (
            _clean(comparison_row.get("adaptive_recommended_order_qty_raw")) or adaptive_qty
        )
        adaptive_target = _clean(comparison_row.get("adaptive_target_stock_qty"))
        adaptive_lead_days = _clean(comparison_row.get("adaptive_lead_time_days"))
        adaptive_prepare = _clean(comparison_row.get("adaptive_supplier_prepare_days"))
        adaptive_logistics = _clean(comparison_row.get("adaptive_logistics_days"))
        adaptive_safety_days = _clean(comparison_row.get("adaptive_safety_stock_days"))

        row["dry_run_decision"] = _clean(comparison_row.get("adaptive_decision"))
        row["recommended_order_qty"] = adaptive_qty
        row["recommended_order_qty_raw"] = adaptive_qty_raw
        row["target_stock_qty"] = adaptive_target
        row["lead_time_days"] = adaptive_lead_days
        row["supplier_prepare_days"] = adaptive_prepare
        row["supplier_assembly_days"] = adaptive_prepare
        row["logistics_days"] = adaptive_logistics
        row["delivery_days"] = adaptive_logistics
        row["safety_stock_days"] = adaptive_safety_days
        row["effective_target_days"] = _clean(comparison_row.get("adaptive_effective_target_days"))
        row["forecast_qty"] = _clean(comparison_row.get("adaptive_forecast_qty"))
        row["safety_stock_qty"] = _clean(comparison_row.get("adaptive_safety_stock_qty"))
        row["free_stock_qty"] = _clean(comparison_row.get("free_stock_qty"))
        row["incoming_qty"] = _clean(comparison_row.get("incoming_qty"))
        row["reason_ru"] = _clean(comparison_row.get("reason_ru"))

        if _clean(row.get("analog_role")) in {"primary_analog", "transition_to_better_analog"}:
            row["analog_group_target_stock_qty"] = adaptive_target
            row["analog_group_recommended_order_qty"] = adaptive_qty
            row["analog_group_recommended_order_qty_raw"] = adaptive_qty_raw

        warnings = set(_split_codes(row.get("warnings")))
        warnings.update(_split_codes(comparison_row.get("warnings")))
        if _clean(comparison_row.get("lead_time_applied")) in {"1", "true", "True"}:
            warnings.add("adaptive_lead_time_sync_ready")
        row["warnings"] = "; ".join(sorted(warnings))

        data_sources = set(_split_codes(row.get("data_sources")))
        data_sources.add("local:adaptive_lead_time")
        row["data_sources"] = "; ".join(sorted(data_sources))
        rows.append(row)
    return rows


def refresh_sync_ready_family_recommendations(
    rows: Sequence[dict[str, Any]],
    *,
    membership_by_code: Mapping[str, ActiveDisplayFamilyMemberContext],
    registry_error: str = "",
) -> dict[str, Any]:
    """Rebuild the family overlay only after adaptive quantities are final."""

    reset_display_family_order_recommendations(rows)
    apply_display_family_order_recommendations(
        rows,
        membership_by_code=membership_by_code,
        registry_error=registry_error,
    )
    return display_family_order_recommendation_summary(rows)


def build_lead_time_indexes(
    lead_time_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, list[Mapping[str, Any]]], dict[str, list[Mapping[str, Any]]]]:
    by_code: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_group: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in lead_time_rows:
        if not _clean(row.get("recommended_supplier_prepare_days")):
            continue
        if not _clean(row.get("recommended_logistics_days")):
            continue
        code = _clean(row.get("nomenclature_code"))
        group_key = _clean(row.get("display_group_key"))
        if code:
            by_code[code].append(row)
        if group_key:
            by_group[group_key].append(row)
    return by_code, by_group


def choose_lead_time_candidate(
    code: str,
    group_key: str,
    *,
    code_index: Mapping[str, Sequence[Mapping[str, Any]]],
    group_index: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[Mapping[str, Any] | None, str]:
    exact = tuple(code_index.get(code, ()))
    group = tuple(group_index.get(group_key, ()))
    for rows, level in (
        (eligible_lead_time_rows(exact), "sku"),
        (eligible_lead_time_rows(group), "display_group"),
        (exact, "sku_low_confidence"),
        (group, "display_group_low_confidence"),
    ):
        candidate = best_lead_time_row(rows)
        if candidate:
            return candidate, level
    return None, "fallback_default"


def eligible_lead_time_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        row for row in rows if _clean(row.get("lead_time_confidence")) in APPLICABLE_CONFIDENCE
    )


def best_lead_time_row(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    if not rows:
        return None
    return max(
        rows,
        key=lambda row: (
            CONFIDENCE_RANK.get(_clean(row.get("lead_time_confidence")), 0),
            _int_or_none(row.get("order_line_count")) or 0,
            _clean(row.get("latest_supplier_order_at")),
            -(
                (_int_or_none(row.get("recommended_supplier_prepare_days")) or 0)
                + (_int_or_none(row.get("recommended_logistics_days")) or 0)
            ),
        ),
    )


def seasonality_adjustment_for_candidate(
    lead_candidate: Mapping[str, Any] | None,
    recent_signals: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not lead_candidate:
        return _empty_seasonality_adjustment()
    supplier_name = _clean(lead_candidate.get("supplier_name"))
    matching = [
        row
        for row in recent_signals
        if supplier_name and supplier_name == _clean(row.get("top_supplier_name"))
    ]
    if not matching:
        return _empty_seasonality_adjustment()
    best = max(
        matching,
        key=lambda row: (
            max(0, _int_or_none(row.get("logistics_delta_days")) or 0)
            + max(0, _int_or_none(row.get("supplier_prepare_delta_days")) or 0),
            _int_or_none(row.get("logistics_delta_days")) or 0,
            _clean(row.get("week_start")),
        ),
    )
    prepare_delta = max(0, _int_or_none(best.get("supplier_prepare_delta_days")) or 0)
    logistics_delta = max(0, _int_or_none(best.get("logistics_delta_days")) or 0)
    signal = []
    if _int_or_none(best.get("prepare_delay_signal")):
        signal.append("prepare_delay")
    if _int_or_none(best.get("road_seasonality_signal")):
        signal.append("road_seasonality")
    return {
        "prepare_delta_days": prepare_delta,
        "logistics_delta_days": logistics_delta,
        "total_adjustment_days": prepare_delta + logistics_delta,
        "week_start": _clean(best.get("week_start")),
        "route_risk_level": _clean(best.get("route_risk_level")),
        "signal": "+".join(signal),
    }


def _recent_seasonality_signals(
    seasonality_rows: Sequence[Mapping[str, Any]],
    *,
    as_of: date,
    recent_weeks: int,
) -> list[Mapping[str, Any]]:
    cutoff = as_of - timedelta(weeks=max(1, recent_weeks))
    rows = []
    for row in seasonality_rows:
        week_start = _date_or_none(row.get("week_start"))
        if week_start is None or week_start < cutoff or week_start > as_of:
            continue
        if _int_or_none(row.get("prepare_delay_signal")) or _int_or_none(
            row.get("road_seasonality_signal")
        ):
            rows.append(row)
    return rows


def build_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    dry_run_csv: Path,
    lead_time_csv: Path,
    seasonality_csv: Path | None,
    as_of: date,
) -> dict[str, Any]:
    current_total = sum(
        (_decimal(row.get("current_recommended_order_qty")) or Decimal("0")) for row in rows
    )
    adaptive_total = sum(
        (_decimal(row.get("adaptive_recommended_order_qty")) or Decimal("0")) for row in rows
    )
    value_delta = sum(
        (_decimal(row.get("estimated_purchase_value_delta")) or Decimal("0")) for row in rows
    )
    qty_increase_rows = [
        row for row in rows if (_decimal(row.get("qty_delta")) or Decimal("0")) > 0
    ]
    qty_decrease_rows = [
        row for row in rows if (_decimal(row.get("qty_delta")) or Decimal("0")) < 0
    ]
    return {
        "schema": "display_auto_order_adaptive_lead_time_comparison.v1",
        "as_of": as_of.isoformat(),
        "input_dry_run_csv": str(dry_run_csv),
        "input_lead_time_csv": str(lead_time_csv),
        "input_seasonality_csv": str(seasonality_csv) if seasonality_csv else None,
        "items": len(rows),
        "current_total_recommended_order_qty": _out_decimal(current_total),
        "adaptive_total_recommended_order_qty": _out_decimal(adaptive_total),
        "qty_delta": _out_decimal(adaptive_total - current_total),
        "estimated_purchase_value_delta": _out_decimal(value_delta),
        "current_order_rows": sum(
            1
            for row in rows
            if (_decimal(row.get("current_recommended_order_qty")) or Decimal("0")) > 0
        ),
        "adaptive_order_rows": sum(
            1
            for row in rows
            if (_decimal(row.get("adaptive_recommended_order_qty")) or Decimal("0")) > 0
        ),
        "qty_increase_rows": len(qty_increase_rows),
        "qty_decrease_rows": len(qty_decrease_rows),
        "adaptive_decision_counts": dict(
            sorted(Counter(_clean(row.get("adaptive_decision")) for row in rows).items())
        ),
        "lead_time_source_counts": dict(
            sorted(Counter(_clean(row.get("lead_time_source_level")) for row in rows).items())
        ),
        "lead_time_confidence_counts": dict(
            sorted(
                Counter(
                    _clean(row.get("lead_time_confidence")) or "missing" for row in rows
                ).items()
            )
        ),
        "lead_time_applied_rows": sum(
            _int_or_none(row.get("lead_time_applied")) or 0 for row in rows
        ),
        "seasonality_adjusted_rows": sum(
            1 for row in rows if (_int_or_none(row.get("seasonality_adjustment_days")) or 0) > 0
        ),
        "top_qty_increase": [
            _summary_change_row(row) for row in top_qty_changes(qty_increase_rows, reverse=True)
        ],
        "top_qty_decrease": [
            _summary_change_row(row) for row in top_qty_changes(qty_decrease_rows, reverse=False)
        ],
        "notes": [
            "high/medium lead-time confidence applies live supplier history",
            "low/missing lead-time confidence keeps current dry-run lead time as fallback",
            "recent seasonality is applied only for signals in the lookback window, not historical spikes",
        ],
    }


def top_qty_changes(rows: Sequence[Mapping[str, Any]], *, reverse: bool) -> list[Mapping[str, Any]]:
    return sorted(
        rows,
        key=lambda row: _decimal(row.get("qty_delta")) or Decimal("0"),
        reverse=reverse,
    )[:10]


def _summary_change_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "nomenclature_code": row.get("nomenclature_code"),
        "name": row.get("name"),
        "speed_tier": row.get("speed_tier"),
        "qty_delta": row.get("qty_delta"),
        "current_recommended_order_qty": row.get("current_recommended_order_qty"),
        "adaptive_recommended_order_qty": row.get("adaptive_recommended_order_qty"),
        "current_effective_target_days": row.get("current_effective_target_days"),
        "adaptive_effective_target_days": row.get("adaptive_effective_target_days"),
        "supplier_name": row.get("supplier_name"),
        "responsible_name": row.get("responsible_name"),
        "reason_ru": row.get("reason_ru"),
    }


def adaptive_reason(
    *,
    current_qty: Decimal,
    adaptive_qty: Decimal,
    current_effective_days: int,
    adaptive_effective_days: int,
    lead_applied: bool,
    lead_candidate: Mapping[str, Any] | None,
    seasonality_adjustment: Mapping[str, Any],
) -> str:
    if adaptive_qty > current_qty:
        direction = "адаптивный расчет увеличил заказ"
    elif adaptive_qty < current_qty:
        direction = "адаптивный расчет уменьшил заказ"
    else:
        direction = "количество не изменилось"
    source = "живой срок применен" if lead_applied else "оставлен fallback по текущему dry-run"
    supplier = _clean(lead_candidate.get("supplier_name")) if lead_candidate else ""
    responsible = _clean(lead_candidate.get("responsible_name")) if lead_candidate else ""
    parts = [
        f"{direction}: горизонт {current_effective_days} -> {adaptive_effective_days} дней",
        source,
    ]
    if supplier:
        parts.append(f"поставщик {supplier}")
    if responsible:
        parts.append(f"ответственный {responsible}")
    if seasonality_adjustment.get("total_adjustment_days"):
        parts.append(
            "учтена текущая сезонность дороги/подготовки "
            f"+{seasonality_adjustment['total_adjustment_days']} дней"
        )
    return "; ".join(parts)


def rounded_order_qty(
    qty: Decimal,
    order_rounding_rules: Sequence[Mapping[str, Any]],
) -> Decimal:
    if qty <= 0:
        return Decimal("0")
    rounded = qty
    for rule in order_rounding_rules:
        threshold = _decimal(rule.get("threshold_gt")) or Decimal("0")
        multiple = _decimal(rule.get("round_to")) or Decimal("0")
        if multiple > 0 and qty > threshold:
            rounded = _round_up_to_multiple(rounded, multiple)
    return rounded


def load_policy(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    raw_policy = payload.get("auto_order_policy", payload)
    if not isinstance(raw_policy, Mapping):
        raise SystemExit(f"auto order policy must be an object: {path}")
    return dict(raw_policy)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as csv_file:
        return list(csv.DictReader(csv_file))


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})
    return path


def _empty_seasonality_adjustment() -> dict[str, Any]:
    return {
        "prepare_delta_days": 0,
        "logistics_delta_days": 0,
        "total_adjustment_days": 0,
        "week_start": "",
        "route_risk_level": "",
        "signal": "",
    }


def _ceil(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_CEILING)


def _round_up_to_multiple(value: Decimal, multiple: Decimal) -> Decimal:
    if multiple <= 0:
        return value
    return ((value / multiple).to_integral_value(rounding=ROUND_CEILING)) * multiple


def _split_codes(value: Any) -> tuple[str, ...]:
    return tuple(part.strip() for part in str(value or "").split(";") if part.strip())


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(Decimal(str(value).replace(" ", "").replace(",", ".")))
    except (InvalidOperation, ValueError):
        return None


def _date_or_none(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _out_decimal(value: Decimal) -> str:
    formatted = format(value.normalize(), "f")
    if "." not in formatted:
        return formatted
    return formatted.rstrip("0").rstrip(".") or "0"


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"date must be YYYY-MM-DD, got: {value}") from exc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare the display auto-order dry-run with adaptive supplier lead-time "
            "and recent road seasonality."
        )
    )
    parser.add_argument("--dry-run-csv", type=Path, required=True)
    parser.add_argument("--lead-time-csv", type=Path, required=True)
    parser.add_argument("--seasonality-csv", type=Path)
    parser.add_argument("--auto-order-policy-json", type=Path, default=DEFAULT_POLICY_JSON)
    parser.add_argument("--as-of", type=_parse_date, default=date.today())
    parser.add_argument("--recent-seasonality-weeks", type=int, default=8)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_JSON)
    parser.add_argument(
        "--sync-ready-csv",
        type=Path,
        help=(
            "Optional dry-run-shaped CSV with adaptive quantities applied, suitable "
            "for sync_display_auto_order_candidates_to_bitrix.py."
        ),
    )
    parser.add_argument(
        "--use-active-display-family-registry",
        action="store_true",
        help=(
            "Rebuild the family recommendation on final adaptive quantities using "
            "only the verified active registry."
        ),
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.recent_seasonality_weeks <= 0:
        raise SystemExit("--recent-seasonality-weeks must be positive")
    return args


if __name__ == "__main__":
    raise SystemExit(main())
