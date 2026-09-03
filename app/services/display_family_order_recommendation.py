"""Deterministic, read-only family order-pool recommendation overlay."""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, MutableMapping, Sequence

from app.services.display_family_registry import ActiveDisplayFamilyMemberContext

FAMILY_ORDER_RECOMMENDATION_SCHEMA = "display_family_order_recommendation.v1"
FAMILY_ORDER_RECOMMENDATION_MODE = "active_registry_order_pool_shadow_v1"
FAMILY_MAX_SHARE_STEP = Decimal("0.10")
ZERO = Decimal("0")
ONE = Decimal("1")

FAMILY_RECOMMENDATION_COLUMNS = (
    "display_family_registry_version",
    "display_family_registry_checksum",
    "display_family_record_id",
    "display_family_id",
    "display_family_label",
    "display_family_registry_member_count",
    "display_family_calculation_member_count",
    "display_family_segment_id",
    "display_family_quality_segment",
    "display_family_construction_segment",
    "display_family_baseline_order_qty",
    "display_family_allocated_order_qty",
    "display_family_pool_order_qty",
    "display_family_segment_pool_order_qty",
    "display_family_baseline_share_pct",
    "display_family_target_share_pct",
    "display_family_allocation_source",
    "display_family_recommendation_status",
    "display_family_confidence",
    "display_family_manual_approval_required",
    "display_family_registry_warning_codes",
    "display_family_conflict_codes",
    "display_family_reason_ru",
)


def reset_display_family_order_recommendations(
    rows: Sequence[MutableMapping[str, Any]],
) -> None:
    """Remove a stale overlay before recalculating against changed quantities."""

    for row in rows:
        for column in FAMILY_RECOMMENDATION_COLUMNS:
            row[column] = ""


def _decimal(value: object) -> Decimal:
    if value in (None, ""):
        return ZERO
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return ZERO


def _text(value: object) -> str:
    return str(value or "").strip()


def _codes(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return sorted({_text(item) for item in value if _text(item)})
    return sorted(
        {item.strip() for item in _text(value).replace(",", ";").split(";") if item.strip()}
    )


def _out(value: Decimal, *, places: int | None = None) -> str:
    if places is not None:
        value = value.quantize(Decimal("1").scaleb(-places))
    normalized = format(value, "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def _matching_conflict_codes(context: ActiveDisplayFamilyMemberContext) -> list[str]:
    evidence = context.matching_evidence
    codes = set(_codes(evidence.get("warnings")))
    if evidence.get("requires_review"):
        codes.add("accepted_matching_review")
    return sorted(codes)


def _bounded_shares(
    codes: Sequence[str],
    *,
    baseline: Mapping[str, Decimal],
    desired: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    lower = {code: max(ZERO, baseline[code] - FAMILY_MAX_SHARE_STEP) for code in codes}
    upper = {code: min(ONE, baseline[code] + FAMILY_MAX_SHARE_STEP) for code in codes}
    shares = {code: min(upper[code], max(lower[code], desired.get(code, ZERO))) for code in codes}
    for _iteration in range(len(codes) * 4):
        difference = ONE - sum(shares.values(), ZERO)
        if abs(difference) <= Decimal("0.0000001"):
            break
        if difference > ZERO:
            adjustable = [code for code in codes if shares[code] < upper[code]]
            capacity = sum((upper[code] - shares[code] for code in adjustable), ZERO)
        else:
            adjustable = [code for code in codes if shares[code] > lower[code]]
            capacity = sum((shares[code] - lower[code] for code in adjustable), ZERO)
        if not adjustable or capacity <= ZERO:
            break
        for code in adjustable:
            room = upper[code] - shares[code] if difference > ZERO else shares[code] - lower[code]
            adjustment = min(abs(difference) * room / capacity, room)
            shares[code] += adjustment if difference > ZERO else -adjustment
    return shares


def demand_speed_scores(
    rows_by_code: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Decimal], str]:
    """Return the canonical family ranking scores used by order allocation and review UI."""

    scores = {
        code: max(ZERO, _decimal(row.get("sales_qty_window_short"))) / Decimal("30")
        + max(ZERO, _decimal(row.get("sales_qty_window_medium"))) / Decimal("90")
        for code, row in rows_by_code.items()
    }
    if sum(scores.values(), ZERO) > ZERO:
        return scores, "completed_sales_rate_30_90"
    scores = {
        code: max(ZERO, _decimal(row.get("sales_qty_window"))) / Decimal("180")
        for code, row in rows_by_code.items()
    }
    return scores, "completed_sales_rate_180_fallback"


def _allocate_whole_units(
    codes: Sequence[str],
    *,
    baseline_qty: Mapping[str, Decimal],
    target_shares: Mapping[str, Decimal],
    costs: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    allocated = dict(baseline_qty)
    pool = sum(baseline_qty.values(), ZERO)
    target_qty = {code: target_shares[code] * pool for code in codes}
    budget = sum((baseline_qty[code] * costs[code] for code in codes), ZERO)
    max_iterations = int(pool.to_integral_value(rounding="ROUND_CEILING")) * max(1, len(codes))
    for _iteration in range(max_iterations):
        current_value = sum((allocated[code] * costs[code] for code in codes), ZERO)
        candidates: list[tuple[Decimal, str, str]] = []
        for donor in codes:
            if allocated[donor] < ONE:
                continue
            for recipient in codes:
                if donor == recipient:
                    continue
                before = abs(allocated[donor] - target_qty[donor]) + abs(
                    allocated[recipient] - target_qty[recipient]
                )
                after = abs(allocated[donor] - ONE - target_qty[donor]) + abs(
                    allocated[recipient] + ONE - target_qty[recipient]
                )
                improvement = before - after
                next_value = current_value - costs[donor] + costs[recipient]
                if improvement > ZERO and next_value <= budget:
                    candidates.append((improvement, donor, recipient))
        if not candidates:
            break
        _improvement, donor, recipient = sorted(
            candidates,
            key=lambda value: (-value[0], value[1], value[2]),
        )[0]
        allocated[donor] -= ONE
        allocated[recipient] += ONE
    return allocated


def _set_common_fields(
    row: MutableMapping[str, Any],
    context: ActiveDisplayFamilyMemberContext,
    *,
    family_calculation_member_count: int,
    family_pool: Decimal,
    segment_pool: Decimal,
) -> None:
    matching_conflicts = _matching_conflict_codes(context)
    registry_warnings = sorted(
        set(context.family_warning_codes) | set(context.member_warning_codes)
    )
    row.update(
        {
            "display_family_registry_version": context.registry_version_number,
            "display_family_registry_checksum": context.registry_inventory_checksum,
            "display_family_record_id": context.family_record_id,
            "display_family_id": context.family_key,
            "display_family_label": context.family_label,
            "display_family_registry_member_count": context.family_member_count,
            "display_family_calculation_member_count": family_calculation_member_count,
            "display_family_segment_id": context.segment_id,
            "display_family_quality_segment": context.quality_segment,
            "display_family_construction_segment": context.construction_segment,
            "display_family_pool_order_qty": _out(family_pool),
            "display_family_segment_pool_order_qty": _out(segment_pool),
            "display_family_manual_approval_required": "yes",
            "display_family_registry_warning_codes": "; ".join(registry_warnings),
            "display_family_conflict_codes": "; ".join(matching_conflicts),
        }
    )


def apply_display_family_order_recommendations(
    rows: Sequence[MutableMapping[str, Any]],
    *,
    membership_by_code: Mapping[str, ActiveDisplayFamilyMemberContext],
    registry_error: str = "",
) -> None:
    """Attach a conservative family allocation without changing base SKU quantities."""

    rows_by_code = {_text(row.get("nomenclature_code")): row for row in rows}
    if registry_error:
        for row in rows:
            baseline = _decimal(row.get("recommended_order_qty"))
            row.update(
                {
                    "display_family_baseline_order_qty": _out(baseline),
                    "display_family_allocated_order_qty": _out(baseline),
                    "display_family_allocation_source": "base_sku_fallback",
                    "display_family_recommendation_status": "blocked_registry_unavailable",
                    "display_family_confidence": "none",
                    "display_family_manual_approval_required": "yes",
                    "display_family_conflict_codes": "display_family_registry_unavailable",
                    "display_family_reason_ru": (
                        "Активный семейный реестр недоступен; семейная рекомендация "
                        "заблокирована, показано базовое SKU-количество."
                    ),
                }
            )
        return

    by_family: dict[str, list[str]] = defaultdict(list)
    by_segment: dict[tuple[str, str], list[str]] = defaultdict(list)
    for code, row in rows_by_code.items():
        baseline = _decimal(row.get("recommended_order_qty"))
        row["display_family_baseline_order_qty"] = _out(baseline)
        row["display_family_allocated_order_qty"] = _out(baseline)
        context = membership_by_code.get(code)
        if context is None:
            row.update(
                {
                    "display_family_allocation_source": "base_sku_fallback",
                    "display_family_recommendation_status": "blocked_membership_missing",
                    "display_family_confidence": "none",
                    "display_family_manual_approval_required": "yes",
                    "display_family_conflict_codes": "display_family_membership_missing",
                    "display_family_reason_ru": (
                        "Код отсутствует в активной версии семейного реестра; "
                        "семейное распределение запрещено."
                    ),
                }
            )
            continue
        by_family[context.family_key].append(code)
        by_segment[(context.family_key, context.segment_id)].append(code)

    family_pools = {
        family_key: sum(
            (_decimal(rows_by_code[code].get("recommended_order_qty")) for code in codes),
            ZERO,
        )
        for family_key, codes in by_family.items()
    }
    for (family_key, segment_id), raw_codes in sorted(by_segment.items()):
        codes = sorted(raw_codes)
        contexts = {code: membership_by_code[code] for code in codes}
        baseline_qty = {
            code: max(ZERO, _decimal(rows_by_code[code].get("recommended_order_qty")))
            for code in codes
        }
        segment_pool = sum(baseline_qty.values(), ZERO)
        family_pool = family_pools[family_key]
        for code in codes:
            _set_common_fields(
                rows_by_code[code],
                contexts[code],
                family_calculation_member_count=len(by_family[family_key]),
                family_pool=family_pool,
                segment_pool=segment_pool,
            )

        eligible_codes = [
            code
            for code in codes
            if not _text(rows_by_code[code].get("blockers"))
            and _text(rows_by_code[code].get("dry_run_decision")) != "manual_review"
            and bool(rows_by_code[code].get("_auto_order_allowed", True))
        ]
        allocated = dict(baseline_qty)
        baseline_shares = {
            code: baseline_qty[code] / segment_pool if segment_pool > ZERO else ZERO
            for code in codes
        }
        target_shares = dict(baseline_shares)
        status = "identity"
        source = "base_sku_order_pool"
        reason = "Семейный пул сохранён без перераспределения."

        segment_is_unconfirmed = any(
            value in {"", "unknown"}
            for context in contexts.values()
            for value in (context.quality_segment, context.construction_segment)
        )
        if len(eligible_codes) < 2:
            status = "identity_insufficient_eligible_skus"
            reason = (
                "В подтверждённом сегменте меньше двух доступных SKU; оставлено базовое количество."
            )
        elif segment_is_unconfirmed:
            status = "review_unconfirmed_segment"
            reason = (
                "Качество или конструкция сегмента не подтверждены; перенос между SKU запрещён."
            )
        elif segment_pool <= ZERO:
            status = "identity_no_new_order_need"
            reason = "После остатка и надёжного pipeline новый семейный пул заказа равен нулю."
        else:
            eligible_rows = {code: rows_by_code[code] for code in eligible_codes}
            eligible_baseline = {code: baseline_qty[code] for code in eligible_codes}
            eligible_pool = sum(eligible_baseline.values(), ZERO)
            costs = {
                code: max(ZERO, _decimal(rows_by_code[code].get("latest_purchase_price")))
                for code in eligible_codes
            }
            if eligible_pool <= ZERO:
                status = "identity_no_eligible_order_need"
                reason = "У доступных SKU нет положительного чистого пула заказа."
            elif any(costs[code] <= ZERO for code in eligible_codes):
                status = "review_purchase_price_missing"
                reason = (
                    "Не для всех SKU подтверждена закупочная цена; перераспределение заблокировано."
                )
            else:
                scores, source = demand_speed_scores(eligible_rows)
                score_total = sum(scores.values(), ZERO)
                if score_total <= ZERO:
                    status = "identity_demand_history_missing"
                    source = "base_sku_order_pool"
                    reason = "Нет завершённой истории спроса для распределения; оставлено базовое количество."
                else:
                    positions = {
                        code: max(ZERO, _decimal(rows_by_code[code].get("free_stock_qty")))
                        + max(ZERO, _decimal(rows_by_code[code].get("incoming_qty")))
                        for code in eligible_codes
                    }
                    demand_shares = {code: scores[code] / score_total for code in eligible_codes}
                    future_position = sum(positions.values(), ZERO) + eligible_pool
                    desired_new = {
                        code: max(
                            ZERO,
                            demand_shares[code] * future_position - positions[code],
                        )
                        for code in eligible_codes
                    }
                    desired_total = sum(desired_new.values(), ZERO)
                    desired_shares = (
                        {code: desired_new[code] / desired_total for code in eligible_codes}
                        if desired_total > ZERO
                        else {
                            code: eligible_baseline[code] / eligible_pool for code in eligible_codes
                        }
                    )
                    eligible_baseline_shares = {
                        code: eligible_baseline[code] / eligible_pool for code in eligible_codes
                    }
                    bounded = _bounded_shares(
                        eligible_codes,
                        baseline=eligible_baseline_shares,
                        desired=desired_shares,
                    )
                    eligible_allocated = _allocate_whole_units(
                        eligible_codes,
                        baseline_qty=eligible_baseline,
                        target_shares=bounded,
                        costs=costs,
                    )
                    allocated.update(eligible_allocated)
                    for code in eligible_codes:
                        target_shares[code] = (
                            eligible_allocated[code] / eligible_pool
                            if eligible_pool > ZERO
                            else ZERO
                        )
                    status = (
                        "allocated_shadow"
                        if any(allocated[code] != baseline_qty[code] for code in eligible_codes)
                        else "identity_with_family_evidence"
                    )
                    reason = (
                        "Чистый пул заказа после остатка и pipeline распределён только внутри "
                        "подтверждённого сегмента; общий объём и капитальный бюджет не увеличены."
                    )

        if sum(allocated.values(), ZERO) != segment_pool:
            raise ValueError(f"family segment quantity drift for {family_key}/{segment_id}")
        for code in codes:
            context = contexts[code]
            conflicts = _matching_conflict_codes(context)
            confidence = (
                "low"
                if context.requires_manual_review or conflicts or status.startswith("review_")
                else "medium"
            )
            rows_by_code[code].update(
                {
                    "display_family_allocated_order_qty": _out(allocated[code]),
                    "display_family_baseline_share_pct": _out(
                        baseline_shares[code] * Decimal("100"), places=2
                    ),
                    "display_family_target_share_pct": _out(
                        target_shares[code] * Decimal("100"), places=2
                    ),
                    "display_family_allocation_source": source,
                    "display_family_recommendation_status": status,
                    "display_family_confidence": confidence,
                    "display_family_reason_ru": reason,
                }
            )


def display_family_order_recommendation_summary(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    family_rows = [row for row in rows if _text(row.get("display_family_recommendation_status"))]
    status_counts = Counter(
        _text(row.get("display_family_recommendation_status")) for row in family_rows
    )
    return {
        "schema": FAMILY_ORDER_RECOMMENDATION_SCHEMA,
        "mode": FAMILY_ORDER_RECOMMENDATION_MODE,
        "enabled": bool(family_rows),
        "row_count": len(family_rows),
        "mapped_row_count": sum(bool(_text(row.get("display_family_id"))) for row in family_rows),
        "family_count": len(
            {
                _text(row.get("display_family_id"))
                for row in family_rows
                if _text(row.get("display_family_id"))
            }
        ),
        "status_counts": dict(sorted(status_counts.items())),
        "baseline_order_qty": _out(
            sum(
                (_decimal(row.get("display_family_baseline_order_qty")) for row in family_rows),
                ZERO,
            )
        ),
        "allocated_order_qty": _out(
            sum(
                (_decimal(row.get("display_family_allocated_order_qty")) for row in family_rows),
                ZERO,
            )
        ),
        "reallocated_row_count": sum(
            _decimal(row.get("display_family_baseline_order_qty"))
            != _decimal(row.get("display_family_allocated_order_qty"))
            for row in family_rows
        ),
        "manual_approval_required": True,
        "max_share_step": _out(FAMILY_MAX_SHARE_STEP),
        "capital_cap_fraction": "0",
    }
