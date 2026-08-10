"""Closed-loop historical simulation of the display auto-order policy.

The task is deliberately read-only for 1C and the application database.  It
writes only local CSV/JSON analytical artifacts.  Unlike the legacy one-point
backtest, this task reconstructs historical network stock directly from 1C
monthly openings and movements, keeps simulated orders in a pipeline, and
never uses sales or prices dated after a decision point.

The result is still a scenario, not an accounting restatement.  Historical
reserves, historical assortment statuses and historical quality blockers are
not fully reconstructable and are reported as explicit limitations.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import bindparam, text

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.onec_stock_availability import (
    MOVEMENT_SQL,
    OPENING_BALANCE_SQL,
    month_start,
    next_month,
)
from tasks.build_display_auto_order_dry_run import (
    ACCELERATING_MIN_GROWTH_MULTIPLIER,
    MIN_RELIABLE_AVAILABILITY_DAYS,
    PENSION_CANDIDATE_MIN_DAYS_IN_SALE,
    STRUCTURAL_FLOOR_QTY,
    AutoOrderPolicy,
    WarehousePolicy,
    load_auto_order_items,
    load_auto_order_policy,
    load_warehouse_policy,
    rounded_order_qty,
)
from tasks.report_display_supplier_lead_time_history import (
    DEFAULT_RECEIPT_MAPPING_JSON,
    DEFAULT_SUPPLIER_ORDER_MAPPING_JSON,
    RECEIPT_MAPPING_UNRESOLVED,
    SUPPLIER_ORDER_MAPPING_UNRESOLVED,
    _load_document_line_mapping,
    fetch_display_supplier_lead_time_source_rows,
)

ZERO = Decimal("0")
ONE = Decimal("1")
DEFAULT_DATE_FROM = date(2026, 2, 1)
DEFAULT_DATE_TO = date(2026, 7, 31)
DEFAULT_HISTORY_START = date(2025, 7, 24)
DEFAULT_LEAD_TIME_DAYS = 52
DEFAULT_SENSITIVITY_DAYS = (45, 52, 59)


@dataclass(frozen=True)
class PurchaseLine:
    created_at: date
    qty: Decimal
    price: Decimal
    supplier_name: str
    order_ref: str
    expected_receipt_at: date | None


@dataclass(frozen=True)
class ReceiptLine:
    received_at: date
    qty: Decimal


@dataclass
class PipelineLot:
    arrival_at: date
    qty: Decimal
    source: str


@dataclass
class SimulationSummary:
    scenario: str
    lead_time_days: int
    decision_points: int = 0
    project_count: int = 0
    order_lines: int = 0
    ordered_sku_count: int = 0
    ordered_qty: Decimal = ZERO
    ordered_value_rub: Decimal = ZERO
    priced_order_lines: int = 0
    unpriced_order_lines: int = 0
    priced_order_qty: Decimal = ZERO
    manual_review_lines: int = 0
    forecast_actual_qty_7d: Decimal = ZERO
    forecast_predicted_qty_7d: Decimal = ZERO
    forecast_abs_error_qty_7d: Decimal = ZERO
    model_unmet_observed_sales_qty: Decimal = ZERO
    model_stockout_sku_days: int = 0
    ending_stock_qty: Decimal = ZERO
    ending_excess_qty: Decimal = ZERO

    def as_dict(self) -> dict[str, Any]:
        return {
            key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in self.__dict__.items()
        }


@dataclass
class SimulationResult:
    summary: SimulationSummary
    decision_rows: list[dict[str, Any]] = field(default_factory=list)
    monthly_rows: list[dict[str, Any]] = field(default_factory=list)
    ending_stock_by_code: dict[str, Decimal] = field(default_factory=dict)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal:
    raw = _clean(value)
    if not raw:
        return ZERO
    try:
        return Decimal(raw)
    except (ArithmeticError, ValueError):
        return ZERO


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = _clean(value)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _ceil(value: Decimal) -> Decimal:
    return Decimal(math.ceil(value))


def _daterange(date_from: date, date_to: date) -> Iterable[date]:
    cursor = date_from
    while cursor <= date_to:
        yield cursor
        cursor += timedelta(days=1)


def _decision_dates(date_from: date, date_to: date, cadence_days: int) -> set[date]:
    cadence = max(1, cadence_days)
    return {
        date_from + timedelta(days=offset)
        for offset in range(0, (date_to - date_from).days + 1, cadence)
    }


def _expanding_text(sql: str, **values: Sequence[str]):
    statement = text(sql)
    for name, items in values.items():
        statement = statement.bindparams(bindparam(name, value=tuple(items), expanding=True))
    return statement


def _chunks(values: Sequence[str], size: int = 700) -> Iterable[tuple[str, ...]]:
    for offset in range(0, len(values), size):
        yield tuple(values[offset : offset + size])


def load_backtest_items(app_engine: Any, *, folder: str) -> tuple[list[dict[str, Any]], int]:
    items, run_id = load_auto_order_items(
        app_engine,
        folder=folder,
        include_sale_review_candidates=True,
    )
    if run_id is None:
        return [], 0
    # load_auto_order_items already applies the canonical current cohort.  A
    # current snapshot is an explicit survivorship limitation, not historical
    # status evidence.
    return items, int(run_id)


def fetch_daily_sales(
    engine: Any,
    *,
    codes: Sequence[str],
    warehouse_codes: Sequence[str],
    date_from: date,
    date_to: date,
) -> dict[str, dict[date, Decimal]]:
    out: dict[str, dict[date, Decimal]] = defaultdict(dict)
    for code_chunk in _chunks(sorted(set(codes))):
        statement = _expanding_text(
            """
            SELECT
                NULLIF(LTRIM(RTRIM(product._Code)), N'') AS code,
                CAST(rtu._Date_Time AS date) AS business_date,
                SUM(CAST(line._Fld4971 AS decimal(18, 3))) AS sales_qty
            FROM dbo._Document203 AS rtu WITH (NOLOCK)
            JOIN dbo._Document203_VT4966 AS line WITH (NOLOCK)
              ON line._Document203_IDRRef = rtu._IDRRef
            JOIN dbo._Reference62 AS product WITH (NOLOCK)
              ON product._IDRRef = line._Fld4974RRef
            JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
              ON warehouse._IDRRef = CASE
                WHEN line._Fld4983RRef <> 0x00000000000000000000000000000000
                THEN line._Fld4983RRef ELSE rtu._Fld4940RRef END
            WHERE rtu._Marked = 0x00
              AND rtu._Posted = 0x01
              AND rtu._Date_Time >= :date_from
              AND rtu._Date_Time < :date_to
              AND line._Fld4971 > 0
              AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
              AND NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') IN :warehouses
            GROUP BY NULLIF(LTRIM(RTRIM(product._Code)), N''), CAST(rtu._Date_Time AS date)
            """,
            codes=code_chunk,
            warehouses=sorted(set(warehouse_codes)),
        ).bindparams(
            bindparam("date_from", value=datetime.combine(date_from, time.min)),
            bindparam("date_to", value=datetime.combine(date_to + timedelta(days=1), time.min)),
        )
        with engine.connect() as connection:
            for row in connection.execute(statement).mappings():
                code = _clean(row["code"])
                business_date = _date(row["business_date"])
                if code and business_date:
                    out[code][business_date] = _decimal(row["sales_qty"])
    return dict(out)


def reconstruct_historical_stock(
    engine: Any,
    *,
    codes: Sequence[str],
    network_warehouse_codes: Sequence[str],
    physical_warehouse_codes: Sequence[str],
    date_from: date,
    date_to: date,
) -> tuple[dict[date, dict[str, Decimal]], dict[str, set[date]], dict[str, Any]]:
    """Rebuild daily network quantity and physical-point availability.

    The same monthly opening and movement registers as the canonical stock
    availability service are used, but no rows are written to the application
    database.
    """

    wanted_codes = set(codes)
    network_codes = set(network_warehouse_codes)
    physical_codes = set(physical_warehouse_codes)
    stock_by_day: dict[date, dict[str, Decimal]] = {}
    available_days: dict[str, set[date]] = defaultdict(set)
    source_counts = {"opening_rows": 0, "movement_rows": 0, "months": 0}

    cursor = month_start(date_from)
    while cursor <= date_to:
        month_end = min(next_month(cursor) - timedelta(days=1), date_to)
        query_end = datetime.combine(month_end + timedelta(days=1), time.min)
        with engine.connect() as connection:
            openings = [
                dict(row)
                for row in connection.execute(
                    OPENING_BALANCE_SQL,
                    {"month_start": datetime.combine(cursor, time.min)},
                ).mappings()
                if _clean(row.get("source_register")) == "warehouse"
                and _clean(row.get("product_code")) in wanted_codes
                and _clean(row.get("warehouse_code")) in network_codes
            ]
            movements = [
                dict(row)
                for row in connection.execute(
                    MOVEMENT_SQL,
                    {"month_start": datetime.combine(cursor, time.min), "date_to": query_end},
                ).mappings()
                if _clean(row.get("source_register")) == "warehouse"
                and _clean(row.get("product_code")) in wanted_codes
                and _clean(row.get("warehouse_code")) in network_codes
            ]
        source_counts["opening_rows"] += len(openings)
        source_counts["movement_rows"] += len(movements)
        source_counts["months"] += 1

        qty_by_key: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        movements_by_day: dict[date, list[Mapping[str, Any]]] = defaultdict(list)
        for row in openings:
            key = (_clean(row.get("product_code")), _clean(row.get("warehouse_code")))
            qty_by_key[key] += _decimal(row.get("quantity"))
        for row in movements:
            business_date = _date(row.get("business_date"))
            if business_date:
                movements_by_day[business_date].append(row)

        keys_by_code: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for key in set(qty_by_key) | {
            (_clean(row.get("product_code")), _clean(row.get("warehouse_code")))
            for row in movements
        }:
            keys_by_code[key[0]].append(key)

        for business_date in _daterange(cursor, month_end):
            for row in movements_by_day.get(business_date, ()):
                key = (_clean(row.get("product_code")), _clean(row.get("warehouse_code")))
                qty_by_key[key] += _decimal(row.get("receipt_qty")) - _decimal(
                    row.get("expense_qty")
                )
            if business_date < date_from:
                continue
            daily: dict[str, Decimal] = {}
            for code in wanted_codes:
                keys = keys_by_code.get(code, ())
                total = sum((max(ZERO, qty_by_key[key]) for key in keys), ZERO)
                daily[code] = total
                if any(key[1] in physical_codes and qty_by_key[key] > ZERO for key in keys):
                    available_days[code].add(business_date)
            stock_by_day[business_date] = daily
        cursor = next_month(cursor)
    return stock_by_day, dict(available_days), source_counts


def fetch_historical_open_supplier_pipeline(
    engine: Any,
    *,
    codes: Sequence[str],
    as_of: date,
    fallback_lead_time_days: int,
) -> dict[str, list[PipelineLot]]:
    """Reconstruct the exact register balance at a historical date.

    ``_AccumRgT7160`` provides the opening balance at the first day of the
    month; ``_AccumRg7147`` provides active movements through ``as_of``.
    Grouping by supplier-order and SKU preserves the arrival date carried by
    the order document while avoiding the unreliable receipt-to-order FIFO
    approximation.
    """

    opening_at = month_start(as_of)
    balances: dict[tuple[str, str], dict[str, Any]] = {}
    for code_chunk in _chunks(sorted(set(codes))):
        opening_statement = _expanding_text(
            """
            SELECT
                NULLIF(LTRIM(RTRIM(product._Code)), N'') AS code,
                CONVERT(varchar(34), balance._Fld7149RRef, 1) AS order_ref,
                supplier_order._Date_Time AS order_created_at,
                supplier_order._Fld2493 AS expected_receipt_at,
                SUM(CAST(balance._Fld7156 AS decimal(28, 3))) AS qty
            FROM dbo._AccumRgT7160 AS balance WITH (NOLOCK)
            JOIN dbo._Reference62 AS product WITH (NOLOCK)
              ON product._IDRRef = balance._Fld7151RRef
            LEFT JOIN dbo._Document133 AS supplier_order WITH (NOLOCK)
              ON supplier_order._IDRRef = balance._Fld7149RRef
            WHERE balance._Period = :opening_at
              AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
            GROUP BY
                NULLIF(LTRIM(RTRIM(product._Code)), N''),
                balance._Fld7149RRef,
                supplier_order._Date_Time,
                supplier_order._Fld2493
            """,
            codes=code_chunk,
        ).bindparams(bindparam("opening_at", value=datetime.combine(opening_at, time.min)))
        movement_statement = _expanding_text(
            """
            SELECT
                NULLIF(LTRIM(RTRIM(product._Code)), N'') AS code,
                CONVERT(varchar(34), movement._Fld7149RRef, 1) AS order_ref,
                supplier_order._Date_Time AS order_created_at,
                supplier_order._Fld2493 AS expected_receipt_at,
                SUM(CASE WHEN movement._RecordKind = 0
                    THEN CAST(movement._Fld7156 AS decimal(28, 3))
                    ELSE -CAST(movement._Fld7156 AS decimal(28, 3)) END) AS qty
            FROM dbo._AccumRg7147 AS movement WITH (NOLOCK)
            JOIN dbo._Reference62 AS product WITH (NOLOCK)
              ON product._IDRRef = movement._Fld7151RRef
            LEFT JOIN dbo._Document133 AS supplier_order WITH (NOLOCK)
              ON supplier_order._IDRRef = movement._Fld7149RRef
            WHERE movement._Active = 0x01
              AND movement._Period >= :opening_at
              AND movement._Period < :date_to
              AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
            GROUP BY
                NULLIF(LTRIM(RTRIM(product._Code)), N''),
                movement._Fld7149RRef,
                supplier_order._Date_Time,
                supplier_order._Fld2493
            """,
            codes=code_chunk,
        ).bindparams(
            bindparam("opening_at", value=datetime.combine(opening_at, time.min)),
            bindparam(
                "date_to",
                value=datetime.combine(as_of + timedelta(days=1), time.min),
            ),
        )
        with engine.connect() as connection:
            rows = list(connection.execute(opening_statement).mappings()) + list(
                connection.execute(movement_statement).mappings()
            )
        for row in rows:
            key = (_clean(row.get("code")), _clean(row.get("order_ref")))
            if not key[0]:
                continue
            target = balances.setdefault(
                key,
                {
                    "qty": ZERO,
                    "order_created_at": _date(row.get("order_created_at")),
                    "expected_receipt_at": _date(row.get("expected_receipt_at")),
                },
            )
            target["qty"] += _decimal(row.get("qty"))
            target["order_created_at"] = target["order_created_at"] or _date(
                row.get("order_created_at")
            )
            target["expected_receipt_at"] = target["expected_receipt_at"] or _date(
                row.get("expected_receipt_at")
            )

    result: dict[str, list[PipelineLot]] = defaultdict(list)
    for (code, _order_ref), row in balances.items():
        qty = _decimal(row.get("qty"))
        if qty <= ZERO:
            continue
        order_created = _date(row.get("order_created_at")) or as_of
        arrival = _date(row.get("expected_receipt_at")) or (
            order_created + timedelta(days=fallback_lead_time_days)
        )
        result[code].append(
            PipelineLot(
                arrival_at=max(as_of + timedelta(days=1), arrival),
                qty=qty,
                source="historical_open_order_register",
            )
        )
    return dict(result)


def normalize_purchase_history(
    supplier_rows: Sequence[Mapping[str, Any]],
    receipt_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, list[PurchaseLine]], dict[str, list[ReceiptLine]]]:
    purchases: dict[str, list[PurchaseLine]] = defaultdict(list)
    receipts: dict[str, list[ReceiptLine]] = defaultdict(list)
    for row in supplier_rows:
        code = _clean(row.get("nomenclature_code"))
        created = _date(row.get("supplier_order_created_at"))
        if not code or created is None:
            continue
        purchases[code].append(
            PurchaseLine(
                created_at=created,
                qty=_decimal(row.get("qty")),
                price=_decimal(row.get("price")),
                supplier_name=_clean(row.get("supplier_name")) or "Поставщик не определён",
                order_ref=_clean(row.get("supplier_order_ref")),
                expected_receipt_at=_date(row.get("expected_receipt_at")),
            )
        )
    for row in receipt_rows:
        code = _clean(row.get("nomenclature_code"))
        received = _date(row.get("receipt_at"))
        if not code or received is None:
            continue
        receipts[code].append(
            ReceiptLine(received_at=received, qty=_decimal(row.get("receipt_qty")))
        )
    for rows in purchases.values():
        rows.sort(key=lambda item: (item.created_at, item.order_ref))
    for rows in receipts.values():
        rows.sort(key=lambda item: item.received_at)
    return dict(purchases), dict(receipts)


def initial_pipeline(
    purchases: Mapping[str, Sequence[PurchaseLine]],
    receipts: Mapping[str, Sequence[ReceiptLine]],
    *,
    as_of: date,
    fallback_lead_time_days: int,
) -> dict[str, list[PipelineLot]]:
    """Approximate open supplier quantities at ``as_of`` by FIFO matching."""

    result: dict[str, list[PipelineLot]] = defaultdict(list)
    for code, order_lines in purchases.items():
        queue: deque[list[Any]] = deque()
        events: list[tuple[date, int, Any]] = []
        for line in order_lines:
            if line.created_at <= as_of:
                events.append((line.created_at, 1, line))
        for receipt in receipts.get(code, ()):
            if receipt.received_at <= as_of:
                events.append((receipt.received_at, 2, receipt))
        for _event_date, event_type, payload in sorted(events, key=lambda row: (row[0], row[1])):
            if event_type == 1:
                line: PurchaseLine = payload
                queue.append([line.qty, line])
                continue
            remaining = payload.qty
            while remaining > ZERO and queue:
                available, line = queue[0]
                used = min(available, remaining)
                available -= used
                remaining -= used
                if available <= ZERO:
                    queue.popleft()
                else:
                    queue[0][0] = available
        for qty, line in queue:
            if qty <= ZERO:
                continue
            arrival = line.expected_receipt_at or (
                line.created_at + timedelta(days=fallback_lead_time_days)
            )
            result[code].append(
                PipelineLot(
                    arrival_at=max(as_of + timedelta(days=1), arrival),
                    qty=qty,
                    source="actual_open_at_start",
                )
            )
    return dict(result)


def _window_sum(series: Mapping[date, Decimal], *, as_of: date, days: int) -> Decimal:
    start = as_of - timedelta(days=days - 1)
    return sum((qty for day, qty in series.items() if start <= day <= as_of), ZERO)


def _available_days(
    dates: set[date],
    *,
    as_of: date,
    days: int,
) -> int:
    start = as_of - timedelta(days=days - 1)
    return sum(1 for day in dates if start <= day <= as_of)


def _availability_rate(qty: Decimal, *, days: int, available_days: int) -> Decimal:
    if days <= 0:
        return ZERO
    base = qty / Decimal(days)
    if available_days <= 0 or Decimal(available_days) < MIN_RELIABLE_AVAILABILITY_DAYS:
        return base
    days_without = Decimal(days - min(days, available_days))
    return (qty + days_without * base) / Decimal(days)


def forecast_rate(
    sales: Mapping[date, Decimal],
    availability_dates: set[date],
    *,
    as_of: date,
    demand_multiplier: Decimal = ONE,
) -> tuple[Decimal, str, dict[str, Any]]:
    quantities = {days: _window_sum(sales, as_of=as_of, days=days) for days in (180, 90, 30)}
    available = {
        days: _available_days(availability_dates, as_of=as_of, days=days) for days in (180, 90, 30)
    }
    rates = {
        days: _availability_rate(quantities[days], days=days, available_days=available[days])
        for days in (180, 90, 30)
    }
    accelerating = (
        rates[30] >= rates[90] * ACCELERATING_MIN_GROWTH_MULTIPLIER
        and rates[90] >= rates[180] * ACCELERATING_MIN_GROWTH_MULTIPLIER
    )
    base = max(rates.values()) if accelerating else sum(rates.values(), ZERO) / Decimal(3)
    return (
        base * demand_multiplier,
        "accelerating" if accelerating else "flat_or_slowing",
        {
            "sales_180": quantities[180],
            "sales_90": quantities[90],
            "sales_30": quantities[30],
            "available_180": available[180],
            "available_90": available[90],
            "available_30": available[30],
        },
    )


def _demand_multiplier(item: Mapping[str, Any], policy: AutoOrderPolicy) -> Decimal:
    name = _clean(item.get("name")).casefold()
    for rule in policy.demand_uplift_rules:
        if "iphone 11" in name and "iphone 11 pro" not in name:
            return rule.demand_multiplier
    return ONE


def _latest_purchase(
    lines: Sequence[PurchaseLine],
    *,
    as_of: date,
) -> PurchaseLine | None:
    candidates = [line for line in lines if line.created_at <= as_of and line.price > ZERO]
    return (
        max(candidates, key=lambda line: (line.created_at, line.order_ref)) if candidates else None
    )


def _recommendation(
    *,
    item: Mapping[str, Any],
    rate: Decimal,
    trend: str,
    availability_90: int,
    free_stock: Decimal,
    incoming_qty: Decimal,
    policy: AutoOrderPolicy,
) -> tuple[Decimal, Decimal, str, str, Decimal]:
    speed_rule = max(
        (rule for rule in policy.speed_horizon_rules if rate >= rule.min_group_avg_daily_sales_qty),
        key=lambda rule: rule.min_group_avg_daily_sales_qty,
        default=None,
    )
    planning_days = (
        policy.target_days
        + policy.order_cadence_days
        + policy.supplier_prepare_days
        + policy.logistics_days
        + policy.supplier_delay_buffer_days
        + policy.receiving_buffer_days
        + policy.distribution_to_shelf_days
    )
    safety_days = policy.safety_stock_days
    speed_tier = "unclassified"
    manual_reason = ""
    if speed_rule is not None:
        speed_tier = speed_rule.tier
        if speed_rule.review_only:
            pension_candidate = (
                free_stock < STRUCTURAL_FLOOR_QTY
                and trend != "accelerating"
                and Decimal(availability_90) >= PENSION_CANDIDATE_MIN_DAYS_IN_SALE
            )
            if pension_candidate or (
                free_stock >= STRUCTURAL_FLOOR_QTY and trend != "accelerating"
            ):
                manual_reason = "slow_tier_manual_review"
            else:
                manual_reason = "slow_tier_starter_order"
        else:
            planning_days = max(
                0,
                speed_rule.max_effective_target_days
                - speed_rule.safety_stock_days
                + policy.distribution_to_shelf_days,
            )
            safety_days = speed_rule.safety_stock_days
    forecast_qty = _ceil(rate * Decimal(planning_days)) if rate > ZERO else ZERO
    safety_qty = _ceil(rate * Decimal(safety_days)) if rate > ZERO else ZERO
    target = forecast_qty + safety_qty
    raw = _ceil(max(ZERO, target - free_stock - incoming_qty))
    rounded = rounded_order_qty(
        raw,
        min_order_qty=policy.min_order_qty,
        max_order_qty=policy.max_order_qty,
        order_rounding_rules=policy.order_rounding_rules,
    )
    auto_allowed = bool(item.get("auto_order_allowed", True))
    if rounded <= ZERO:
        return ZERO, target, "do_not_order", speed_tier, raw
    if manual_reason == "slow_tier_manual_review" or not auto_allowed:
        return rounded, target, "manual_review", speed_tier, raw
    return rounded, target, "order", speed_tier, raw


def run_simulation(
    *,
    items: Sequence[Mapping[str, Any]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    availability_by_code: Mapping[str, set[date]],
    actual_stock_by_day: Mapping[date, Mapping[str, Decimal]],
    purchase_history: Mapping[str, Sequence[PurchaseLine]],
    initial_pipeline_by_code: Mapping[str, Sequence[PipelineLot]],
    policy: AutoOrderPolicy,
    date_from: date,
    date_to: date,
    lead_time_days: int,
    scenario: str,
) -> SimulationResult:
    codes = [_clean(item.get("nomenclature_code")) for item in items]
    item_by_code = {_clean(item.get("nomenclature_code")): item for item in items}
    starting_stock = actual_stock_by_day.get(date_from, {})
    stock = {code: _decimal(starting_stock.get(code)) for code in codes}
    pipeline: dict[str, list[PipelineLot]] = {
        code: [PipelineLot(lot.arrival_at, lot.qty, lot.source) for lot in lots]
        for code, lots in initial_pipeline_by_code.items()
    }
    decisions = _decision_dates(date_from, date_to, policy.order_cadence_days or 7)
    detail: list[dict[str, Any]] = []
    ordered_codes: set[str] = set()
    project_keys: set[tuple[date, str]] = set()
    monthly: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "order_lines": 0,
            "manual_review_lines": 0,
            "ordered_qty": ZERO,
            "ordered_value_rub": ZERO,
            "actual_sales_qty": ZERO,
            "model_unmet_observed_sales_qty": ZERO,
            "model_stockout_sku_days": 0,
            "forecast_predicted_qty_7d": ZERO,
            "forecast_actual_qty_7d": ZERO,
            "forecast_abs_error_qty_7d": ZERO,
        }
    )
    summary = SimulationSummary(scenario=scenario, lead_time_days=lead_time_days)
    demand_active_dates: dict[str, set[date]] = defaultdict(set)
    for code in codes:
        for sale_date, qty in sales_by_code.get(code, {}).items():
            if qty <= ZERO:
                continue
            active_from = max(date_from, sale_date)
            active_to = min(date_to, sale_date + timedelta(days=179))
            if active_from <= active_to:
                demand_active_dates[code].update(_daterange(active_from, active_to))

    for business_date in _daterange(date_from, date_to):
        month_key = business_date.strftime("%Y-%m")
        for code in codes:
            lots = pipeline.get(code, [])
            arrived = sum((lot.qty for lot in lots if lot.arrival_at <= business_date), ZERO)
            if arrived:
                stock[code] += arrived
                pipeline[code] = [lot for lot in lots if lot.arrival_at > business_date]
            sale_qty = _decimal(sales_by_code.get(code, {}).get(business_date))
            monthly[month_key]["actual_sales_qty"] += sale_qty
            if sale_qty > stock[code]:
                unmet = sale_qty - stock[code]
                monthly[month_key]["model_unmet_observed_sales_qty"] += unmet
                stock[code] = ZERO
            else:
                stock[code] -= sale_qty
            if stock[code] <= ZERO and business_date in demand_active_dates.get(code, set()):
                monthly[month_key]["model_stockout_sku_days"] += 1

        if business_date not in decisions:
            continue
        summary.decision_points += 1
        for code in codes:
            item = item_by_code[code]
            rate, trend, evidence = forecast_rate(
                sales_by_code.get(code, {}),
                availability_by_code.get(code, set()),
                as_of=business_date,
                demand_multiplier=_demand_multiplier(item, policy),
            )
            incoming = sum((lot.qty for lot in pipeline.get(code, ())), ZERO)
            recommended, target, decision, speed_tier, raw = _recommendation(
                item=item,
                rate=rate,
                trend=trend,
                availability_90=int(evidence["available_90"]),
                free_stock=stock[code],
                incoming_qty=incoming,
                policy=policy,
            )
            future_end = min(date_to, business_date + timedelta(days=7))
            future_actual = sum(
                (
                    qty
                    for day, qty in sales_by_code.get(code, {}).items()
                    if business_date < day <= future_end
                ),
                ZERO,
            )
            predicted_7d = rate * Decimal((future_end - business_date).days)
            abs_error = abs(predicted_7d - future_actual)
            monthly[month_key]["forecast_predicted_qty_7d"] += predicted_7d
            monthly[month_key]["forecast_actual_qty_7d"] += future_actual
            monthly[month_key]["forecast_abs_error_qty_7d"] += abs_error

            purchase = _latest_purchase(purchase_history.get(code, ()), as_of=business_date)
            price = purchase.price if purchase else ZERO
            supplier = purchase.supplier_name if purchase else "Поставщик не определён"
            scheduled = ZERO
            if decision == "order" and recommended > ZERO:
                arrival = business_date + timedelta(days=lead_time_days)
                pipeline.setdefault(code, []).append(
                    PipelineLot(arrival_at=arrival, qty=recommended, source="simulated_order")
                )
                scheduled = recommended
                ordered_codes.add(code)
                project_keys.add((business_date, supplier))
                summary.order_lines += 1
                summary.ordered_qty += recommended
                summary.ordered_value_rub += recommended * price
                if price > ZERO:
                    summary.priced_order_lines += 1
                    summary.priced_order_qty += recommended
                else:
                    summary.unpriced_order_lines += 1
                monthly[month_key]["order_lines"] += 1
                monthly[month_key]["ordered_qty"] += recommended
                monthly[month_key]["ordered_value_rub"] += recommended * price
            elif decision == "manual_review" and recommended > ZERO:
                summary.manual_review_lines += 1
                monthly[month_key]["manual_review_lines"] += 1

            if recommended > ZERO or future_actual > ZERO or rate > ZERO:
                detail.append(
                    {
                        "scenario": scenario,
                        "decision_date": business_date.isoformat(),
                        "month": month_key,
                        "nomenclature_code": code,
                        "name": _clean(item.get("name")),
                        "status": _clean(item.get("status")),
                        "auto_order_allowed_current": int(bool(item.get("auto_order_allowed"))),
                        "speed_tier": speed_tier,
                        "trend": trend,
                        "sales_180": str(evidence["sales_180"]),
                        "sales_90": str(evidence["sales_90"]),
                        "sales_30": str(evidence["sales_30"]),
                        "available_days_180": evidence["available_180"],
                        "forecast_rate": f"{rate:.6f}",
                        "predicted_sales_next_7d": f"{predicted_7d:.3f}",
                        "actual_sales_next_7d": str(future_actual),
                        "forecast_abs_error_7d": f"{abs_error:.3f}",
                        "model_free_stock": str(stock[code]),
                        "model_incoming_qty": str(incoming),
                        "target_stock_qty": str(target),
                        "recommended_order_qty_raw": str(raw),
                        "recommended_order_qty": str(recommended),
                        "scheduled_order_qty": str(scheduled),
                        "decision": decision,
                        "supplier_as_of": supplier,
                        "purchase_price_as_of": str(price),
                        "recommended_value_rub": str(recommended * price),
                        "arrival_date": (
                            (business_date + timedelta(days=lead_time_days)).isoformat()
                            if scheduled > ZERO
                            else ""
                        ),
                    }
                )

    summary.project_count = len(project_keys)
    summary.ordered_sku_count = len(ordered_codes)
    summary.forecast_actual_qty_7d = sum(
        (row["forecast_actual_qty_7d"] for row in monthly.values()), ZERO
    )
    summary.forecast_predicted_qty_7d = sum(
        (row["forecast_predicted_qty_7d"] for row in monthly.values()), ZERO
    )
    summary.forecast_abs_error_qty_7d = sum(
        (row["forecast_abs_error_qty_7d"] for row in monthly.values()), ZERO
    )
    summary.model_unmet_observed_sales_qty = sum(
        (row["model_unmet_observed_sales_qty"] for row in monthly.values()), ZERO
    )
    summary.model_stockout_sku_days = sum(
        int(row["model_stockout_sku_days"]) for row in monthly.values()
    )
    summary.ending_stock_qty = sum(stock.values(), ZERO)

    for code in codes:
        rate, trend, evidence = forecast_rate(
            sales_by_code.get(code, {}),
            availability_by_code.get(code, set()),
            as_of=date_to,
            demand_multiplier=_demand_multiplier(item_by_code[code], policy),
        )
        _recommended, target, _decision, _tier, _raw = _recommendation(
            item=item_by_code[code],
            rate=rate,
            trend=trend,
            availability_90=int(evidence["available_90"]),
            free_stock=ZERO,
            incoming_qty=ZERO,
            policy=policy,
        )
        summary.ending_excess_qty += max(ZERO, stock[code] - target)

    monthly_rows = []
    for month_key, row in sorted(monthly.items()):
        monthly_rows.append(
            {
                "scenario": scenario,
                "month": month_key,
                **{
                    key: (str(value) if isinstance(value, Decimal) else value)
                    for key, value in row.items()
                },
            }
        )
    return SimulationResult(
        summary=summary,
        decision_rows=detail,
        monthly_rows=monthly_rows,
        ending_stock_by_code=stock,
    )


def actual_purchase_summary(
    purchases: Mapping[str, Sequence[PurchaseLine]],
    *,
    date_from: date,
    date_to: date,
    allowed_codes: set[str] | None = None,
) -> dict[str, Any]:
    lines = [
        line
        for code, rows in purchases.items()
        if allowed_codes is None or code in allowed_codes
        for line in rows
        if date_from <= line.created_at <= date_to
    ]
    return {
        "order_count": len({line.order_ref for line in lines if line.order_ref}),
        "line_count": len(lines),
        "sku_count": len(
            {
                code
                for code, rows in purchases.items()
                if allowed_codes is None or code in allowed_codes
                if any(date_from <= line.created_at <= date_to for line in rows)
            }
        ),
        "qty": str(sum((line.qty for line in lines), ZERO)),
        "value_rub": str(sum((line.qty * line.price for line in lines), ZERO)),
    }


def actual_stock_summary(
    stock_by_day: Mapping[date, Mapping[str, Decimal]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    *,
    codes: Sequence[str],
    date_from: date,
    date_to: date,
) -> dict[str, Any]:
    demand_active_dates: dict[str, set[date]] = defaultdict(set)
    for code in codes:
        for sale_date, qty in sales_by_code.get(code, {}).items():
            if qty <= ZERO:
                continue
            active_from = max(date_from, sale_date)
            active_to = min(date_to, sale_date + timedelta(days=179))
            if active_from <= active_to:
                demand_active_dates[code].update(_daterange(active_from, active_to))
    stockout_sku_days = 0
    demand_active_sku_days = 0
    for business_date in _daterange(date_from, date_to):
        daily = stock_by_day.get(business_date, {})
        for code in codes:
            if business_date not in demand_active_dates.get(code, set()):
                continue
            demand_active_sku_days += 1
            if _decimal(daily.get(code)) <= ZERO:
                stockout_sku_days += 1
    return {
        "starting_stock_qty": str(
            sum((_decimal(stock_by_day.get(date_from, {}).get(code)) for code in codes), ZERO)
        ),
        "ending_stock_qty": str(
            sum((_decimal(stock_by_day.get(date_to, {}).get(code)) for code in codes), ZERO)
        ),
        "stockout_sku_days": stockout_sku_days,
        "demand_active_sku_days": demand_active_sku_days,
        "stockout_share": (
            str(Decimal(stockout_sku_days) / Decimal(demand_active_sku_days))
            if demand_active_sku_days
            else "0"
        ),
    }


def pipeline_summary(pipeline_by_code: Mapping[str, Sequence[PipelineLot]]) -> dict[str, Any]:
    lots = [lot for rows in pipeline_by_code.values() for lot in rows]
    return {
        "sku_count": sum(1 for rows in pipeline_by_code.values() if rows),
        "lot_count": len(lots),
        "qty": str(sum((lot.qty for lot in lots), ZERO)),
        "latest_arrival_at": (max(lot.arrival_at for lot in lots).isoformat() if lots else None),
        "method": "historical _AccumRgT7160 opening plus _AccumRg7147 movements",
    }


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Six-month closed-loop display auto-order backtest"
    )
    parser.add_argument("--date-from", type=date.fromisoformat, default=DEFAULT_DATE_FROM)
    parser.add_argument("--date-to", type=date.fromisoformat, default=DEFAULT_DATE_TO)
    parser.add_argument("--history-start", type=date.fromisoformat, default=DEFAULT_HISTORY_START)
    parser.add_argument("--folder", default="дисплеи")
    parser.add_argument("--database-url", default="")
    parser.add_argument("--onec-database-url", default="")
    parser.add_argument(
        "--auto-order-policy-json",
        type=Path,
        default=Path("config/assortment/display-auto-order-policy.json"),
    )
    parser.add_argument(
        "--warehouse-policy-json",
        type=Path,
        default=Path("config/assortment/display-warehouse-policy.json"),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--lead-time-days",
        type=int,
        nargs="+",
        default=list(DEFAULT_SENSITIVITY_DAYS),
    )
    args = parser.parse_args()
    if args.date_from > args.date_to:
        raise SystemExit("date-from must not exceed date-to")
    if (args.date_from - args.history_start).days < 180:
        raise SystemExit("history-start must provide at least 180 days of warm-up")
    if any(days <= 0 for days in args.lead_time_days):
        raise SystemExit("lead-time-days must be positive")
    return args


def main() -> int:
    args = _parse_args()
    settings = get_settings()
    app_url = args.database_url or os.environ.get("DATABASE_URL") or settings.database_url
    onec_url = (
        args.onec_database_url
        or os.environ.get("ONEC_DATABASE_URL", "")
        or settings.onec_database_url
        or ""
    )
    if not onec_url:
        raise SystemExit("ONEC_DATABASE_URL is not configured")
    auto_policy = load_auto_order_policy(args.auto_order_policy_json)
    warehouse_policy: WarehousePolicy = load_warehouse_policy(args.warehouse_policy_json)

    app_engine = build_engine(app_url, pool_pre_ping=True)
    try:
        items, run_id = load_backtest_items(app_engine, folder=args.folder)
        coverage_rows = []
        with app_engine.connect() as connection:
            if connection.dialect.has_table(connection, "onec_stock_availability_coverage"):
                coverage_rows = [
                    dict(row)
                    for row in connection.execute(
                        text(
                            "SELECT period_month, covered_from, covered_to, status "
                            "FROM onec_stock_availability_coverage "
                            "WHERE period_month BETWEEN :date_from AND :date_to "
                            "ORDER BY period_month"
                        ),
                        {
                            "date_from": month_start(args.date_from),
                            "date_to": month_start(args.date_to),
                        },
                    ).mappings()
                ]
    finally:
        app_engine.dispose()
    if not items:
        raise SystemExit("display auto-order cohort is empty")

    codes = sorted(
        {
            _clean(item.get("nomenclature_code"))
            for item in items
            if _clean(item.get("nomenclature_code"))
        }
    )
    network_codes = sorted(
        {
            _clean(row.get("warehouse_code") or row.get("code"))
            for row in json.loads(args.warehouse_policy_json.read_text(encoding="utf-8-sig"))[
                "warehouses"
            ]
            if _clean(row.get("warehouse_code") or row.get("code"))
            and not row.get("is_defect_warehouse")
            and not row.get("is_non_systematic_sale")
        }
    )
    onec_engine = build_engine(onec_url, pool_pre_ping=True)
    try:
        sales = fetch_daily_sales(
            onec_engine,
            codes=codes,
            warehouse_codes=warehouse_policy.sellable_codes,
            date_from=args.history_start,
            date_to=args.date_to,
        )
        stock_by_day, availability, stock_counts = reconstruct_historical_stock(
            onec_engine,
            codes=codes,
            network_warehouse_codes=network_codes,
            physical_warehouse_codes=warehouse_policy.sellable_codes,
            date_from=args.history_start,
            date_to=args.date_to,
        )
        starting_pipeline = fetch_historical_open_supplier_pipeline(
            onec_engine,
            codes=codes,
            as_of=args.date_from - timedelta(days=1),
            fallback_lead_time_days=DEFAULT_LEAD_TIME_DAYS,
        )
        supplier_mapping = _load_document_line_mapping(
            DEFAULT_SUPPLIER_ORDER_MAPPING_JSON,
            error_code=SUPPLIER_ORDER_MAPPING_UNRESOLVED,
        )
        receipt_mapping = _load_document_line_mapping(
            DEFAULT_RECEIPT_MAPPING_JSON,
            error_code=RECEIPT_MAPPING_UNRESOLVED,
        )
        source_rows = fetch_display_supplier_lead_time_source_rows(
            onec_engine,
            folder=args.folder,
            history_start=args.history_start,
            as_of=args.date_to,
            supplier_mapping=supplier_mapping,
            receipt_mapping=receipt_mapping,
            limit=10000,
        )
    finally:
        onec_engine.dispose()

    purchases, receipts = normalize_purchase_history(
        source_rows["supplier_order_rows"],
        source_rows["receipt_rows"],
    )
    all_detail: list[dict[str, Any]] = []
    all_monthly: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for lead_time in sorted(set(args.lead_time_days)):
        scenario = f"lead_time_{lead_time}d"
        result = run_simulation(
            items=items,
            sales_by_code=sales,
            availability_by_code=availability,
            actual_stock_by_day=stock_by_day,
            purchase_history=purchases,
            initial_pipeline_by_code=starting_pipeline,
            policy=auto_policy,
            date_from=args.date_from,
            date_to=args.date_to,
            lead_time_days=lead_time,
            scenario=scenario,
        )
        summaries.append(result.summary.as_dict())
        all_detail.extend(result.decision_rows)
        all_monthly.extend(result.monthly_rows)

    actual = actual_purchase_summary(
        purchases,
        date_from=args.date_from,
        date_to=args.date_to,
        allowed_codes=set(codes),
    )
    actual_stock = actual_stock_summary(
        stock_by_day,
        sales,
        codes=codes,
        date_from=args.date_from,
        date_to=args.date_to,
    )
    output = args.output_dir
    _write_csv(output / "decision-detail.csv", all_detail)
    _write_csv(output / "monthly-summary.csv", all_monthly)
    _write_csv(output / "scenario-summary.csv", summaries)
    payload = {
        "schema": "display_auto_order_six_month_backtest.v1",
        "status": "share_with_caveats",
        "date_from": args.date_from.isoformat(),
        "date_to": args.date_to.isoformat(),
        "history_start": args.history_start.isoformat(),
        "decision_cadence_days": auto_policy.order_cadence_days,
        "cohort": {
            "classification_run_id": run_id,
            "sku_count": len(codes),
            "definition": "current working/sale display cohort eligible for calculation",
            "survivorship_warning": True,
        },
        "source_counts": {
            "daily_sales_sku_count": len(sales),
            "supplier_order_rows": len(source_rows["supplier_order_rows"]),
            "receipt_rows": len(source_rows["receipt_rows"]),
            **stock_counts,
        },
        "application_stock_coverage": [
            {
                key: (value.isoformat() if isinstance(value, date) else value)
                for key, value in row.items()
            }
            for row in coverage_rows
        ],
        "actual_supplier_orders": actual,
        "actual_stock": actual_stock,
        "initial_supplier_pipeline": pipeline_summary(starting_pipeline),
        "scenarios": summaries,
        "limitations": [
            "Historical assortment status is unavailable; the current eligible cohort is used (survivorship bias).",
            "Historical reserves are unavailable and therefore treated as zero; simulated free stock may be overstated and order quantity understated.",
            "Historical quality blockers and return-based stop rules are not replayed.",
            "Opening stock and movements are reconstructed by warehouse but without an explicit quality dimension; defect and non-systematic warehouses are excluded.",
            "Historical incoming quantity is reconstructed from the 1C open-supplier-order register; expected arrival dates still depend on the supplier-order document and may be missing or stale.",
            "Supplier selection is approximated by the latest supplier and purchase price known on each decision date.",
            "Observed sales are used as demand; model stockouts expose unmet observed sales but cannot fully recover latent demand.",
        ],
        "outputs": {
            "decision_detail_csv": "decision-detail.csv",
            "monthly_summary_csv": "monthly-summary.csv",
            "scenario_summary_csv": "scenario-summary.csv",
        },
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
