"""Pure helpers for shadow/backtest allocation of display-family demand."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any, Mapping, Sequence

from app.services.display_identity import display_identity_from_mapping
from app.services.display_scope_policy import filter_display_scope_records

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class DisplayFamilyMember:
    nomenclature_code: str
    name: str
    family_id: str
    segment_id: str
    quality_segment: str
    construction_segment: str
    model_tokens: tuple[str, ...]


@dataclass(frozen=True)
class DisplayFamilyAllocation:
    nomenclature_code: str
    family_id: str
    segment_id: str
    baseline_rate: Decimal
    family_baseline_rate: Decimal
    recent_sales_qty: Decimal
    family_recent_sales_qty: Decimal
    pure_family_rate: Decimal
    allocated_rate: Decimal
    sku_share: Decimal
    allocation_source: str


@dataclass(frozen=True)
class DisplayFamilyOrderAllocation:
    """Auditable redistribution of an already calculated net order need."""

    decision_date: date
    nomenclature_code: str
    family_id: str
    segment_id: str
    baseline_order_qty: Decimal
    allocated_order_qty: Decimal
    baseline_share: Decimal
    target_share: Decimal
    short_sales_qty: Decimal
    long_sales_qty: Decimal
    short_available_days: int
    long_available_days: int
    inventory_cost_per_unit_rub: Decimal
    segment_baseline_order_qty: Decimal
    segment_baseline_order_value_rub: Decimal
    segment_allocated_order_value_rub: Decimal
    allocation_source: str
    blocker: str


@dataclass(frozen=True)
class DisplayFamilyProfitProtection:
    decision_date: date
    nomenclature_code: str
    mode: str
    base_order_qty: Decimal
    added_order_qty: Decimal
    final_order_qty: Decimal
    projected_shortage_qty: Decimal
    inventory_cost_per_unit_rub: Decimal
    gross_margin_per_unit_rub: Decimal
    expected_holding_cost_per_unit_rub: Decimal
    expected_unit_gmroi: Decimal
    expected_arrival_date: date | None
    blocker: str


@dataclass(frozen=True)
class DisplayFamilyRegularTopUp:
    """Auditable early top-up that keeps the ordinary supplier lead time."""

    decision_date: date
    nomenclature_code: str
    coverage_fraction: Decimal
    base_order_qty: Decimal
    added_order_qty: Decimal
    final_order_qty: Decimal
    projected_shortage_qty: Decimal
    shortage_after_base_order_qty: Decimal
    target_protection_qty: Decimal
    open_protection_qty: Decimal
    inventory_cost_per_unit_rub: Decimal
    gross_margin_per_unit_rub: Decimal
    expected_holding_cost_per_unit_rub: Decimal
    expected_unit_gmroi: Decimal
    expected_arrival_date: date
    blocker: str


def freeze_display_family_order_trajectory(
    order_rows: Sequence[Mapping[str, Any]],
) -> dict[tuple[date, str], Decimal]:
    """Freeze the ordinary baseline actually used by a completed simulation."""

    overrides: dict[tuple[date, str], Decimal] = {}
    for row in order_rows:
        raw_date = row.get("decision_date")
        business_date = (
            raw_date if isinstance(raw_date, date) else date.fromisoformat(_clean(raw_date))
        )
        code = _clean(row.get("nomenclature_code"))
        if not code:
            raise ValueError("frozen family order trajectory contains an empty SKU code")
        raw_qty = row.get("ordinary_family_allocated_order_qty")
        if raw_qty is None or _clean(raw_qty) == "":
            raw_qty = row.get("ordinary_recommended_order_qty")
        qty = max(ZERO, Decimal(str(raw_qty or 0)))
        key = (business_date, code)
        existing = overrides.get(key)
        if existing is not None and existing != qty:
            raise ValueError("frozen family order trajectory contains conflicting quantities")
        overrides[key] = qty
    return overrides


def build_regular_topup_delivery_overrides(
    audit: Sequence[DisplayFamilyRegularTopUp],
) -> tuple[
    dict[tuple[date, str], Decimal],
    dict[tuple[date, str], date],
]:
    """Return explicit P75 quantities and receipt dates for simulation."""

    quantities: dict[tuple[date, str], Decimal] = {}
    arrivals: dict[tuple[date, str], date] = {}
    for row in audit:
        if row.added_order_qty <= ZERO:
            continue
        key = (row.decision_date, row.nomenclature_code)
        if key in quantities:
            raise ValueError("regular top-up audit contains duplicate accepted decisions")
        quantities[key] = row.added_order_qty
        arrivals[key] = row.expected_arrival_date
    return quantities, arrivals


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _family_id(tokens: Sequence[str], *, fallback_code: str) -> str:
    normalized = sorted({_clean(token).casefold() for token in tokens if _clean(token)})
    if not normalized:
        return f"display-singleton-{fallback_code.casefold()}"
    digest = hashlib.sha256("\0".join(normalized).encode()).hexdigest()[:16]
    return f"display-family-{digest}"


def _compatibility_signature(tokens: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(sorted({_clean(token).casefold() for token in tokens if _clean(token)}))
    model_tokens = tuple(token for token in normalized if ":model:" in token)
    return model_tokens or normalized


def display_quality_segment(name: str) -> str:
    """Compatibility adapter; new family code uses ``DisplayIdentity`` directly."""

    identity = display_identity_from_mapping({"name": name})
    if identity.construction_segment != "unknown":
        return identity.construction_segment
    return identity.quality_segment


def display_construction_segment(name: str) -> str:
    """Legacy compact construction label backed by canonical display identity."""

    identity = display_identity_from_mapping({"name": name})
    tags: list[str] = []
    if identity.has_frame is True:
        tags.append("with_frame")
    elif identity.has_frame is False:
        tags.append("without_frame")
    else:
        tags.append("frame_unknown")
    if identity.has_ic_pad is True:
        tags.append("ic_pad")
    tags.extend(identity.modifiers)
    return "+".join(tags)


def build_display_family_members(
    items: Sequence[Mapping[str, Any]],
) -> dict[str, DisplayFamilyMember]:
    """Build conservative deterministic families from compatibility signatures."""

    normalized: list[dict[str, Any]] = []
    scoped_items = filter_display_scope_records(items).included
    for item in scoped_items:
        code = _clean(item.get("nomenclature_code"))
        if not code:
            continue
        tokens = _compatibility_signature(item.get("model_tokens", ()))
        normalized.append(
            {
                "nomenclature_code": code,
                "name": _clean(item.get("name")),
                "model_tokens": tokens,
                "identity_source": dict(item),
            }
        )

    grouped_indexes: dict[tuple[str, ...] | tuple[str, str], list[int]] = defaultdict(list)
    for index, item in enumerate(normalized):
        tokens = item["model_tokens"]
        group_key: tuple[str, ...] | tuple[str, str]
        if tokens:
            group_key = tokens
        else:
            group_key = ("singleton", item["nomenclature_code"].casefold())
        grouped_indexes[group_key].append(index)

    result: dict[str, DisplayFamilyMember] = {}
    for indexes in grouped_indexes.values():
        group_tokens = normalized[indexes[0]]["model_tokens"]
        for index in indexes:
            item = normalized[index]
            code = item["nomenclature_code"]
            name = item["name"]
            family_id = _family_id(group_tokens, fallback_code=code)
            identity = display_identity_from_mapping(
                {
                    **item["identity_source"],
                    "nomenclature_code": code,
                    "name": name,
                    "model_tokens": item["model_tokens"],
                }
            )
            quality = identity.quality_segment
            construction = "|".join(identity.segment_id.split("|")[1:])
            result[code] = DisplayFamilyMember(
                nomenclature_code=code,
                name=name,
                family_id=family_id,
                segment_id=identity.segment_id,
                quality_segment=quality,
                construction_segment=construction,
                model_tokens=item["model_tokens"],
            )
    return result


def _weights(
    codes: Sequence[str],
    *,
    primary: Mapping[str, Decimal],
    fallback: Mapping[str, Decimal],
) -> tuple[dict[str, Decimal], str]:
    primary_total = sum((max(ZERO, primary.get(code, ZERO)) for code in codes), ZERO)
    if primary_total > ZERO:
        return (
            {code: max(ZERO, primary.get(code, ZERO)) / primary_total for code in codes},
            "recent_sales",
        )
    fallback_total = sum((max(ZERO, fallback.get(code, ZERO)) for code in codes), ZERO)
    if fallback_total > ZERO:
        return (
            {code: max(ZERO, fallback.get(code, ZERO)) / fallback_total for code in codes},
            "baseline_rate",
        )
    equal = ONE / Decimal(len(codes))
    return ({code: equal for code in codes}, "equal")


def allocate_display_family_rates(
    members: Mapping[str, DisplayFamilyMember],
    *,
    baseline_rates: Mapping[str, Decimal],
    recent_sales: Mapping[str, Decimal],
    blend: Decimal,
) -> dict[str, DisplayFamilyAllocation]:
    """Reallocate, but never add, forecast rate within each compatible family."""

    normalized_blend = Decimal(blend)
    if not ZERO <= normalized_blend <= ONE:
        raise ValueError("family allocation blend must be between zero and one")

    by_family: dict[str, list[str]] = defaultdict(list)
    for code, member in members.items():
        if code in baseline_rates:
            by_family[member.family_id].append(code)

    allocations: dict[str, DisplayFamilyAllocation] = {}
    for family_id, family_codes in by_family.items():
        family_codes = sorted(family_codes)
        family_rate = sum(
            (max(ZERO, Decimal(baseline_rates.get(code, ZERO))) for code in family_codes),
            ZERO,
        )
        family_sales = sum(
            (max(ZERO, Decimal(recent_sales.get(code, ZERO))) for code in family_codes),
            ZERO,
        )
        segment_codes: dict[str, list[str]] = defaultdict(list)
        for code in family_codes:
            segment_codes[members[code].segment_id].append(code)

        segment_sales = {
            segment_id: sum(
                (max(ZERO, Decimal(recent_sales.get(code, ZERO))) for code in codes),
                ZERO,
            )
            for segment_id, codes in segment_codes.items()
        }
        segment_baseline = {
            segment_id: sum(
                (max(ZERO, Decimal(baseline_rates.get(code, ZERO))) for code in codes),
                ZERO,
            )
            for segment_id, codes in segment_codes.items()
        }
        segment_weights, segment_source = _weights(
            sorted(segment_codes),
            primary=segment_sales,
            fallback=segment_baseline,
        )

        for segment_id, codes in segment_codes.items():
            sku_weights, sku_source = _weights(
                sorted(codes),
                primary=recent_sales,
                fallback=baseline_rates,
            )
            segment_rate = family_rate * segment_weights[segment_id]
            for code in codes:
                baseline = max(ZERO, Decimal(baseline_rates.get(code, ZERO)))
                pure_rate = baseline if family_sales == ZERO else segment_rate * sku_weights[code]
                allocated = baseline * (ONE - normalized_blend) + pure_rate * normalized_blend
                allocations[code] = DisplayFamilyAllocation(
                    nomenclature_code=code,
                    family_id=family_id,
                    segment_id=segment_id,
                    baseline_rate=baseline,
                    family_baseline_rate=family_rate,
                    recent_sales_qty=max(ZERO, Decimal(recent_sales.get(code, ZERO))),
                    family_recent_sales_qty=family_sales,
                    pure_family_rate=pure_rate,
                    allocated_rate=allocated,
                    sku_share=(allocated / family_rate if family_rate > ZERO else ZERO),
                    allocation_source=f"segment:{segment_source};sku:{sku_source}",
                )
    return allocations


def _bounded_shares(
    codes: Sequence[str],
    *,
    baseline: Mapping[str, Decimal],
    desired: Mapping[str, Decimal],
    max_share_step: Decimal,
) -> dict[str, Decimal]:
    lower = {code: max(ZERO, baseline[code] - max_share_step) for code in codes}
    upper = {code: min(ONE, baseline[code] + max_share_step) for code in codes}
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


def _completed_sales(sales: Mapping[date, Decimal], *, as_of: date, lookback_days: int) -> Decimal:
    start = as_of - timedelta(days=lookback_days)
    return sum(
        (
            max(ZERO, Decimal(quantity))
            for sold_at, quantity in sales.items()
            if start <= sold_at < as_of
        ),
        ZERO,
    )


def allocate_display_family_order_pool(
    order_rows: Sequence[Mapping[str, Any]],
    *,
    members: Mapping[str, DisplayFamilyMember],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    available_dates_by_code: Mapping[str, set[date]] | None = None,
    availability_corrected: bool = False,
    short_lookback_days: int = 30,
    long_lookback_days: int = 90,
    max_share_step: Decimal = Decimal("0.20"),
    capital_cap_fraction: Decimal = ZERO,
    one_open_family_lot: bool = True,
) -> tuple[dict[tuple[date, str], Decimal], list[DisplayFamilyOrderAllocation]]:
    """Redistribute an ordinary net-order pool inside one confirmed segment.

    Input quantities are expected to be calculated after free stock, reserve and
    reliable pipeline.  Segment quantity never grows and allocated purchase
    value cannot exceed the ordinary segment budget plus the explicit cap.
    """

    if short_lookback_days <= 0 or long_lookback_days < short_lookback_days:
        raise ValueError("family order lookbacks must be positive and ordered")
    normalized_step = Decimal(max_share_step)
    normalized_cap = Decimal(capital_cap_fraction)
    if not ZERO <= normalized_step <= ONE:
        raise ValueError("family order max share step must be between zero and one")
    if normalized_cap < ZERO:
        raise ValueError("family order capital cap fraction must be non-negative")

    by_date: dict[date, list[Mapping[str, Any]]] = defaultdict(list)
    for row in order_rows:
        business_date = row.get("decision_date")
        if isinstance(business_date, str):
            business_date = date.fromisoformat(business_date)
        code = _clean(row.get("nomenclature_code"))
        if isinstance(business_date, date) and code in members:
            by_date[business_date].append(row)

    overrides: dict[tuple[date, str], Decimal] = {}
    audit: list[DisplayFamilyOrderAllocation] = []
    open_until: dict[tuple[str, str], date] = {}
    for business_date in sorted(by_date):
        grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for row in by_date[business_date]:
            member = members[_clean(row.get("nomenclature_code"))]
            grouped[(member.family_id, member.segment_id)].append(row)

        for (family_id, segment_id), rows in sorted(grouped.items()):
            rows = sorted(rows, key=lambda row: _clean(row.get("nomenclature_code")))
            codes = [_clean(row.get("nomenclature_code")) for row in rows]
            baseline_qty = {
                code: max(ZERO, Decimal(str(row.get("ordinary_recommended_order_qty") or 0)))
                for code, row in zip(codes, rows, strict=True)
            }
            costs = {
                code: max(ZERO, Decimal(str(row.get("inventory_cost_per_unit_rub") or 0)))
                for code, row in zip(codes, rows, strict=True)
            }
            positions = {
                code: max(ZERO, Decimal(str(row.get("inventory_position_qty") or 0)))
                for code, row in zip(codes, rows, strict=True)
            }
            pool = sum(baseline_qty.values(), ZERO)
            baseline_value = sum((baseline_qty[code] * costs[code] for code in codes), ZERO)
            budget = baseline_value * (ONE + normalized_cap)
            allocated = dict(baseline_qty)
            baseline_shares = {
                code: baseline_qty[code] / pool if pool > ZERO else ZERO for code in codes
            }
            target_shares = dict(baseline_shares)
            blocker = ""
            source = (
                "availability_corrected_sales_rate_30_90"
                if availability_corrected
                else "sales_rate_30_90"
            )

            if len(codes) < 2:
                blocker = "segment_has_fewer_than_two_skus"
            elif "unknown" in segment_id:
                blocker = "segment_not_confirmed"
            elif pool <= ZERO:
                blocker = "no_new_order_need"
            elif any(costs[code] <= ZERO for code in codes):
                blocker = "inventory_cost_missing"
            elif (
                one_open_family_lot
                and open_until.get((family_id, segment_id), date.min) >= business_date
            ):
                blocker = "family_lot_still_open"
            else:
                short_sales = {
                    code: _completed_sales(
                        sales_by_code.get(code, {}),
                        as_of=business_date,
                        lookback_days=short_lookback_days,
                    )
                    for code in codes
                }
                long_sales = {
                    code: _completed_sales(
                        sales_by_code.get(code, {}),
                        as_of=business_date,
                        lookback_days=long_lookback_days,
                    )
                    for code in codes
                }
                scores = {
                    code: short_sales[code]
                    / Decimal(
                        max(
                            1,
                            (
                                sum(
                                    business_date - timedelta(days=short_lookback_days)
                                    <= available_at
                                    < business_date
                                    for available_at in (available_dates_by_code or {}).get(
                                        code, set()
                                    )
                                )
                                if availability_corrected
                                else short_lookback_days
                            ),
                        )
                    )
                    + long_sales[code]
                    / Decimal(
                        max(
                            1,
                            (
                                sum(
                                    business_date - timedelta(days=long_lookback_days)
                                    <= available_at
                                    < business_date
                                    for available_at in (available_dates_by_code or {}).get(
                                        code, set()
                                    )
                                )
                                if availability_corrected
                                else long_lookback_days
                            ),
                        )
                    )
                    for code in codes
                }
                score_total = sum(scores.values(), ZERO)
                if score_total <= ZERO:
                    source = "baseline_order_share"
                    desired_order_shares = baseline_shares
                else:
                    demand_shares = {code: scores[code] / score_total for code in codes}
                    segment_position_after_order = sum(positions.values(), ZERO) + pool
                    desired_new_order = {
                        code: max(
                            ZERO,
                            demand_shares[code] * segment_position_after_order - positions[code],
                        )
                        for code in codes
                    }
                    desired_new_order_total = sum(desired_new_order.values(), ZERO)
                    desired_order_shares = (
                        {code: desired_new_order[code] / desired_new_order_total for code in codes}
                        if desired_new_order_total > ZERO
                        else baseline_shares
                    )
                target_shares = _bounded_shares(
                    codes,
                    baseline=baseline_shares,
                    desired=desired_order_shares,
                    max_share_step=normalized_step,
                )
                target_qty = {code: target_shares[code] * pool for code in codes}
                # Begin with the feasible ordinary order and move whole units.
                for _iteration in range(int(pool) * max(1, len(codes))):
                    current_value = sum((allocated[code] * costs[code] for code in codes), ZERO)
                    best: tuple[Decimal, str, str] | None = None
                    for donor in codes:
                        if allocated[donor] <= ZERO:
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
                                candidate = (improvement, donor, recipient)
                                if best is None or candidate > best:
                                    best = candidate
                    if best is None:
                        break
                    _improvement, donor, recipient = best
                    allocated[donor] -= ONE
                    allocated[recipient] += ONE

                if allocated != baseline_qty and one_open_family_lot:
                    arrival_dates = [
                        date.fromisoformat(raw_arrival)
                        for row in rows
                        if (raw_arrival := _clean(row.get("expected_arrival_date")))
                    ]
                    open_until[(family_id, segment_id)] = max(arrival_dates or [business_date])

            allocated_value = sum((allocated[code] * costs[code] for code in codes), ZERO)
            for code in codes:
                overrides[(business_date, code)] = allocated[code]
                audit.append(
                    DisplayFamilyOrderAllocation(
                        decision_date=business_date,
                        nomenclature_code=code,
                        family_id=family_id,
                        segment_id=segment_id,
                        baseline_order_qty=baseline_qty[code],
                        allocated_order_qty=allocated[code],
                        baseline_share=baseline_shares[code],
                        target_share=target_shares[code],
                        short_sales_qty=_completed_sales(
                            sales_by_code.get(code, {}),
                            as_of=business_date,
                            lookback_days=short_lookback_days,
                        ),
                        long_sales_qty=_completed_sales(
                            sales_by_code.get(code, {}),
                            as_of=business_date,
                            lookback_days=long_lookback_days,
                        ),
                        short_available_days=sum(
                            business_date - timedelta(days=short_lookback_days)
                            <= available_at
                            < business_date
                            for available_at in (available_dates_by_code or {}).get(code, set())
                        ),
                        long_available_days=sum(
                            business_date - timedelta(days=long_lookback_days)
                            <= available_at
                            < business_date
                            for available_at in (available_dates_by_code or {}).get(code, set())
                        ),
                        inventory_cost_per_unit_rub=costs[code],
                        segment_baseline_order_qty=pool,
                        segment_baseline_order_value_rub=baseline_value,
                        segment_allocated_order_value_rub=allocated_value,
                        allocation_source=source,
                        blocker=blocker,
                    )
                )
    return overrides, audit


def build_display_family_profit_protection(
    order_rows: Sequence[Mapping[str, Any]],
    *,
    base_overrides: Mapping[tuple[date, str], Decimal],
    mode: str,
    annual_carrying_rate: Decimal,
    max_units_per_decision: int,
    profit_hurdle_multiplier: Decimal = Decimal("1.5"),
    gmroi_hurdle: Decimal = ZERO,
    open_lot_keys: set[tuple[date, str]] | None = None,
) -> tuple[dict[tuple[date, str], Decimal], list[DisplayFamilyProfitProtection]]:
    """Add a bounded, economically justified buffer to a family order plan."""

    normalized_mode = _clean(mode).lower()
    if normalized_mode not in {"safety", "pipeline_topup"}:
        raise ValueError("profit protection mode must be safety or pipeline_topup")
    unit_cap = int(max_units_per_decision)
    if unit_cap < 0:
        raise ValueError("profit protection unit cap must be non-negative")
    carrying_rate = Decimal(annual_carrying_rate)
    hurdle = Decimal(profit_hurdle_multiplier)
    normalized_gmroi_hurdle = Decimal(gmroi_hurdle)
    if carrying_rate < ZERO or hurdle < ONE or normalized_gmroi_hurdle < ZERO:
        raise ValueError("profit protection economics are invalid")

    overrides = dict(base_overrides)
    audit: list[DisplayFamilyProfitProtection] = []
    protection_open_until: dict[str, date] = {}
    for row in sorted(
        order_rows,
        key=lambda item: (
            _clean(item.get("decision_date")),
            _clean(item.get("nomenclature_code")),
        ),
    ):
        raw_date = row.get("decision_date")
        business_date = (
            raw_date if isinstance(raw_date, date) else date.fromisoformat(_clean(raw_date))
        )
        code = _clean(row.get("nomenclature_code"))
        key = (business_date, code)
        base_qty = max(
            ZERO,
            Decimal(
                base_overrides.get(
                    key,
                    Decimal(str(row.get("ordinary_recommended_order_qty") or 0)),
                )
            ),
        )
        cost = max(ZERO, Decimal(str(row.get("inventory_cost_per_unit_rub") or 0)))
        margin = max(ZERO, Decimal(str(row.get("gross_margin_per_unit_rub") or 0)))
        p75_days = max(1, int(row.get("lead_time_p75_days") or 52))
        position = max(ZERO, Decimal(str(row.get("inventory_position_qty") or 0)))
        rate = max(ZERO, Decimal(str(row.get("forecast_rate_sales") or 0)))
        shortage = max(
            ZERO,
            Decimal(str(row.get("acceleration_gross_projected_shortage_to_p75_qty") or 0)),
            rate * Decimal(p75_days) - position,
        )
        expected_holding = cost * carrying_rate * Decimal(p75_days) / Decimal("365") * hurdle
        expected_unit_gmroi = (
            margin * Decimal("365") / Decimal(p75_days) / cost if cost > ZERO else ZERO
        )
        raw_arrival = _clean(row.get("expected_arrival_date"))
        expected_arrival = date.fromisoformat(raw_arrival) if raw_arrival else None
        blocker = ""
        added = ZERO
        if unit_cap == 0:
            blocker = "unit_cap_zero"
        elif normalized_mode == "pipeline_topup" and key not in (open_lot_keys or set()):
            blocker = "no_open_family_lot_block"
        elif shortage <= ZERO:
            blocker = "projected_shortage_not_proven"
        elif cost <= ZERO or margin <= ZERO:
            blocker = "unit_economics_missing"
        elif margin <= expected_holding:
            blocker = "profit_hurdle_not_passed"
        elif expected_unit_gmroi < normalized_gmroi_hurdle:
            blocker = "gmroi_hurdle_not_passed"
        elif protection_open_until.get(code, date.min) >= business_date:
            blocker = "profit_protection_lot_still_open"
        else:
            added = min(
                Decimal(unit_cap),
                Decimal(int(shortage.to_integral_value(rounding=ROUND_CEILING))),
            )
            if added > ZERO:
                protection_open_until[code] = expected_arrival or (
                    business_date + timedelta(days=p75_days)
                )
        overrides[key] = base_qty + added
        audit.append(
            DisplayFamilyProfitProtection(
                decision_date=business_date,
                nomenclature_code=code,
                mode=normalized_mode,
                base_order_qty=base_qty,
                added_order_qty=added,
                final_order_qty=base_qty + added,
                projected_shortage_qty=shortage,
                inventory_cost_per_unit_rub=cost,
                gross_margin_per_unit_rub=margin,
                expected_holding_cost_per_unit_rub=expected_holding,
                expected_unit_gmroi=expected_unit_gmroi,
                expected_arrival_date=expected_arrival,
                blocker=blocker,
            )
        )
    return overrides, audit


def build_display_family_regular_topup(
    order_rows: Sequence[Mapping[str, Any]],
    *,
    base_overrides: Mapping[tuple[date, str], Decimal],
    focus_codes: set[str],
    annual_carrying_rate: Decimal,
    shortage_coverage_fraction: Decimal,
    profit_hurdle_multiplier: Decimal = Decimal("1.5"),
    gmroi_hurdle: Decimal = ZERO,
    latest_evaluable_arrival_date: date | None = None,
    minimum_days_between_topups: int = 1,
) -> tuple[dict[tuple[date, str], Decimal], list[DisplayFamilyRegularTopUp]]:
    """Add only the still-uncovered shortage through the ordinary supply channel.

    The helper is intended for a frozen targeted backtest.  It does not accelerate
    delivery: every added lot receives the SKU P75 lead time.  A later calculation
    may open another ordinary lot only for the part of the protection target that
    is not already covered by still-open protection lots.
    """

    coverage = Decimal(shortage_coverage_fraction)
    carrying_rate = Decimal(annual_carrying_rate)
    hurdle = Decimal(profit_hurdle_multiplier)
    normalized_gmroi_hurdle = Decimal(gmroi_hurdle)
    minimum_cadence_days = int(minimum_days_between_topups)
    normalized_focus_codes = {_clean(code) for code in focus_codes if _clean(code)}
    if coverage <= ZERO or coverage > ONE:
        raise ValueError("regular top-up coverage must be in the interval (0, 1]")
    if carrying_rate < ZERO or hurdle < ONE or normalized_gmroi_hurdle < ZERO:
        raise ValueError("regular top-up economics are invalid")
    if not normalized_focus_codes:
        raise ValueError("regular top-up requires at least one focus code")
    if minimum_cadence_days < 1:
        raise ValueError("regular top-up cadence must be at least one day")

    overrides = dict(base_overrides)
    audit: list[DisplayFamilyRegularTopUp] = []
    open_lots: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    last_topup_date: dict[str, date] = {}
    for row in sorted(
        order_rows,
        key=lambda item: (
            _clean(item.get("decision_date")),
            _clean(item.get("nomenclature_code")),
        ),
    ):
        raw_date = row.get("decision_date")
        business_date = (
            raw_date if isinstance(raw_date, date) else date.fromisoformat(_clean(raw_date))
        )
        code = _clean(row.get("nomenclature_code"))
        if code not in normalized_focus_codes:
            continue
        key = (business_date, code)
        base_qty = max(
            ZERO,
            Decimal(
                base_overrides.get(
                    key,
                    Decimal(str(row.get("ordinary_recommended_order_qty") or 0)),
                )
            ),
        )
        cost = max(ZERO, Decimal(str(row.get("inventory_cost_per_unit_rub") or 0)))
        margin = max(ZERO, Decimal(str(row.get("gross_margin_per_unit_rub") or 0)))
        p75_days = max(1, int(row.get("lead_time_p75_days") or 52))
        position = max(ZERO, Decimal(str(row.get("inventory_position_qty") or 0)))
        rate = max(ZERO, Decimal(str(row.get("forecast_rate_sales") or 0)))
        projected_shortage = max(
            ZERO,
            Decimal(str(row.get("acceleration_gross_projected_shortage_to_p75_qty") or 0)),
            rate * Decimal(p75_days) - position,
        )
        shortage_after_base = max(ZERO, projected_shortage - base_qty)
        target_protection = Decimal(
            int((shortage_after_base * coverage).to_integral_value(rounding=ROUND_CEILING))
        )
        active_lots = [lot for lot in open_lots[code] if lot[0] > business_date]
        open_lots[code] = active_lots
        open_protection = sum((lot_qty for _arrival, lot_qty in active_lots), ZERO)
        expected_arrival = business_date + timedelta(days=p75_days)
        expected_holding = cost * carrying_rate * Decimal(p75_days) / Decimal("365") * hurdle
        expected_unit_gmroi = (
            margin * Decimal("365") / Decimal(p75_days) / cost if cost > ZERO else ZERO
        )
        blocker = ""
        added = ZERO
        if shortage_after_base <= ZERO:
            blocker = "shortage_after_base_not_proven"
        elif target_protection <= open_protection:
            blocker = "regular_topup_target_already_covered"
        elif (
            latest_evaluable_arrival_date is not None
            and expected_arrival > latest_evaluable_arrival_date
        ):
            blocker = "arrival_outside_evaluation_window"
        elif (
            code in last_topup_date
            and (business_date - last_topup_date[code]).days < minimum_cadence_days
        ):
            blocker = "regular_topup_cadence_block"
        elif cost <= ZERO or margin <= ZERO:
            blocker = "unit_economics_missing"
        elif margin <= expected_holding:
            blocker = "profit_hurdle_not_passed"
        elif expected_unit_gmroi < normalized_gmroi_hurdle:
            blocker = "gmroi_hurdle_not_passed"
        else:
            added = target_protection - open_protection
            open_lots[code].append((expected_arrival, added))
            last_topup_date[code] = business_date
        overrides[key] = base_qty + added
        audit.append(
            DisplayFamilyRegularTopUp(
                decision_date=business_date,
                nomenclature_code=code,
                coverage_fraction=coverage,
                base_order_qty=base_qty,
                added_order_qty=added,
                final_order_qty=base_qty + added,
                projected_shortage_qty=projected_shortage,
                shortage_after_base_order_qty=shortage_after_base,
                target_protection_qty=target_protection,
                open_protection_qty=open_protection,
                inventory_cost_per_unit_rub=cost,
                gross_margin_per_unit_rub=margin,
                expected_holding_cost_per_unit_rub=expected_holding,
                expected_unit_gmroi=expected_unit_gmroi,
                expected_arrival_date=expected_arrival,
                blocker=blocker,
            )
        )
    return overrides, audit
