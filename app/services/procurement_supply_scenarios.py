"""Evidence-preserving incoming supply scenarios; never applies an order quantity."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence


def decimal(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def supply_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    # SQL dates from UT can contain the 2000-year offset.
    if parsed.year > 3000:
        parsed = parsed.replace(year=parsed.year - 2000)
    return parsed if parsed.year > 1900 else None


def supply_schedule(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        value = json.loads(value) if value else []
    return [dict(item) for item in value or []]


def partition_supply(
    schedule: Sequence[Mapping[str, Any]], *, as_of: date, horizon_days: int
) -> dict[str, Any]:
    buckets = {key: Decimal(0) for key in ("dated", "overdue", "undated", "later")}
    normalized = []
    nearest = None
    for item in schedule:
        qty = max(Decimal(0), decimal(item.get("quantity")))
        eta = supply_date(item.get("expected_at"))
        category = (
            "undated"
            if eta is None
            else (
                "overdue"
                if eta < as_of
                else "later" if eta > as_of + timedelta(days=max(0, horizon_days)) else "dated"
            )
        )
        buckets[category] += qty
        if category == "dated" and qty > 0:
            nearest = min(nearest, eta) if nearest else eta
        normalized.append(
            {
                "order_ref": str(item.get("order_ref") or ""),
                "quantity": str(qty),
                "expected_at": eta.isoformat() if eta else None,
                "category": category,
            }
        )
    normalized.sort(
        key=lambda item: (item["order_ref"], item["expected_at"] or "", item["quantity"])
    )
    return {
        **{key + "_quantity": str(value) for key, value in buckets.items()},
        "nearest_expected_at": nearest.isoformat() if nearest else None,
        "schedule": normalized,
    }


def facts_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")
        ).encode()
    ).hexdigest()


def annotate_scenario(row: dict[str, Any], cautious_quantity: Any, *, as_of: date) -> None:
    schedule = supply_schedule(row.get("incoming_schedule"))
    # Legacy input without detail is explicitly unconfirmed, never silently dated.
    if not schedule and decimal(row.get("incoming_qty")) > 0:
        schedule = [{"quantity": str(row["incoming_qty"]), "expected_at": None}]
    horizon = int(decimal(row.get("effective_target_days")))
    supply = partition_supply(schedule, as_of=as_of, horizon_days=horizon)
    quantities = {
        "all_open_quantity": str(decimal(row.get("recommended_order_qty"))),
        "dated_only_quantity": str(decimal(cautious_quantity)),
    }
    facts = {
        **supply,
        **quantities,
        "target_stock_qty": str(row.get("target_stock_qty") or 0),
        "order_available_stock_qty": str(row.get("order_available_stock_qty") or 0),
    }
    review = decimal(quantities["dated_only_quantity"]) != decimal(quantities["all_open_quantity"])
    row["supply_scenario"] = json.dumps(
        {
            **facts,
            "facts_hash": facts_hash(facts),
            "review_required": review,
            "as_of": as_of.isoformat(),
        },
        ensure_ascii=False,
    )
    row["incoming_schedule"] = json.dumps(schedule, ensure_ascii=False, default=str)
    row["recommended_order_qty_dated_only"] = quantities["dated_only_quantity"]
    row["supply_review_required"] = "true" if review else "false"
    row["calculation_as_of"] = as_of.isoformat()
    apply_stockout_guard(row, as_of=as_of, nearest_expected_at=supply["nearest_expected_at"])


def apply_stockout_guard(
    row: dict[str, Any], *, as_of: date, nearest_expected_at: str | None = None
) -> None:
    warnings = {v.strip() for v in str(row.get("warnings") or "").split(";") if v.strip()}
    warnings.discard("stockout_guard_triggered")
    reason = str(row.get("reason_ru") or "").split(" ТРЕВОГА (stockout_guard):", 1)[0]
    row["reason_ru"] = reason
    for key in (
        "stockout_guard_triggered",
        "stockout_guard_days_remaining",
        "stockout_guard_required_days",
    ):
        row[key] = ""
    status = str(
        row.get("_assortment_status") or row.get("status") or row.get("assortment_status") or ""
    )
    speed = decimal(row.get("avg_daily_sales_qty"))
    if (
        status in {"sale", "working"}
        and not str(row.get("blockers") or "").strip()
        and row.get("dry_run_decision") in {"order", "do_not_order"}
        and speed > 0
    ):
        available = max(
            Decimal(0),
            decimal(row.get("sellable_stock_qty")) - decimal(row.get("active_customer_order_qty")),
        )
        eta = supply_date(nearest_expected_at)
        travel = (
            Decimal((eta - as_of).days)
            if eta and eta >= as_of
            else decimal(row.get("lead_time_days"))
        )
        required = travel + decimal(row.get("distribution_to_shelf_days")) + 10
        if available / speed < required:
            row.update(
                stockout_guard_triggered="true",
                stockout_guard_days_remaining=str((available / speed).quantize(Decimal("0.1"))),
                stockout_guard_required_days=str(required),
            )
            warnings.add("stockout_guard_triggered")
            row["reason_ru"] = reason + (
                " ТРЕВОГА (stockout_guard): запаса недостаточно до поступления "
                "с буфером 10 дней; требуется действие закупщика."
            )
    row["warnings"] = "; ".join(sorted(warnings))


def active_manual_removal(payload: Mapping[str, Any]) -> bool:
    removal = payload.get("manual_removal") or {}
    return bool(removal.get("removed_at") and not removal.get("restored_at"))


def price_confirmed(line: Any) -> bool:
    return (
        decimal(line.purchase_price) > 0
        and decimal(line.purchase_price) != 1
        and (line.source_kind == "onec_import" or bool((line.payload or {}).get("price_confirmed")))
    )


def supply_review_valid(line: Any) -> bool:
    payload = line.payload or {}
    scenario = payload.get("supply_scenario") or {}
    review = payload.get("supply_review") or {}
    return bool(
        not review.get("stale")
        and review.get("facts_hash")
        and review.get("facts_hash") == scenario.get("facts_hash")
        and review.get("final_quantity") is not None
        and decimal(review["final_quantity"]) == line.final_quantity
    )


def annotate_family_scenarios(
    rows: Sequence[dict[str, Any]],
    *,
    membership_by_code: Mapping[str, Any],
    registry_error: str = "",
) -> None:
    from app.services.display_family_order_recommendation import (
        apply_display_family_order_recommendations,
        reset_display_family_order_recommendations,
    )

    # Both recommendations must pass the same final family allocation rules.
    cautious_rows = [
        dict(
            row,
            recommended_order_qty=row.get(
                "recommended_order_qty_dated_only", row.get("recommended_order_qty")
            ),
        )
        for row in rows
    ]
    reset_display_family_order_recommendations(cautious_rows)
    apply_display_family_order_recommendations(
        cautious_rows,
        membership_by_code=membership_by_code,
        registry_error=registry_error,
    )
    for row, cautious in zip(rows, cautious_rows, strict=True):
        as_of = supply_date(row.get("calculation_as_of"))
        if as_of is None:
            continue
        baseline = row.get("recommended_order_qty")
        row["recommended_order_qty"] = row.get("display_family_allocated_order_qty", baseline)
        annotate_scenario(
            row,
            cautious.get(
                "display_family_allocated_order_qty", cautious.get("recommended_order_qty")
            ),
            as_of=as_of,
        )
        row["recommended_order_qty"] = baseline
