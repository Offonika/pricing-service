"""Reusable preparation layer for the display auto-order walk-forward backtest.

The module contains only read-only extraction helpers, deterministic historical
reconstruction and artifact writers.  It deliberately keeps the source facts
separate from scenario decisions so the inputs can be reviewed before the
economic simulation is run.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from sqlalchemy import bindparam, text

from app.services.assortment_lifecycle import AssortmentStatus
from tasks.build_display_auto_order_dry_run import AutoOrderPolicy, rounded_order_qty
from tasks.report_display_auto_order_six_month_backtest import (
    DEFAULT_LAUNCH_PROFILE_MIN_SAMPLES,
    DEFAULT_LEAD_TIME_DAYS,
    LaunchObservation,
    PurchaseLine,
    ReceiptLine,
    _demand_multiplier,
    _latest_purchase,
    build_launch_profile_snapshot,
    forecast_rate,
    historical_lifecycle_decision,
    item_active_as_of,
    select_launch_profile,
    stage_model_scenario,
    warmup_lifecycle_statuses,
)
from tasks.report_display_supplier_lead_time_history import display_group_key

ZERO = Decimal("0")
ONE = Decimal("1")
EMPTY_REF_SQL = "0x00000000000000000000000000000000"
PREFLIGHT_SCHEMA = "display_auto_order_backtest_preflight.v1"
REQUIRED_PREFLIGHT_FILES = (
    "decision-inputs.csv",
    "scenario-decisions.csv",
    "lifecycle-daily.csv",
    "daily-facts.csv",
    "source-quality.csv",
    "reconciliations.csv",
    "backtest-preflight.xlsx",
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0").strip() or "0")
    except (ArithmeticError, ValueError):
        return ZERO


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    rendered = _clean(value)
    if not rendered:
        return None
    try:
        return date.fromisoformat(rendered[:10])
    except ValueError:
        return None


def _ceil(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_CEILING)


def _daterange(date_from: date, date_to: date) -> Iterable[date]:
    cursor = date_from
    while cursor <= date_to:
        yield cursor
        cursor += timedelta(days=1)


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _next_month(value: date) -> date:
    return (value.replace(day=28) + timedelta(days=4)).replace(day=1)


def _chunks(values: Sequence[str], size: int = 700) -> Iterable[tuple[str, ...]]:
    for offset in range(0, len(values), size):
        yield tuple(values[offset : offset + size])


@dataclass(frozen=True)
class CarryingCostScenario:
    name: str
    capital_annual_rate: Decimal
    storage_annual_rate: Decimal
    obsolescence_annual_rate: Decimal

    @property
    def total_annual_rate(self) -> Decimal:
        return (
            self.capital_annual_rate
            + self.storage_annual_rate
            + self.obsolescence_annual_rate
        )


@dataclass(frozen=True)
class BacktestScenarioConfig:
    kmp4_weights: tuple[Decimal, ...]
    kmp4_queue_days: int
    site_signals_enabled: bool
    holding_cost_scenarios: tuple[CarryingCostScenario, ...]
    lead_time_fallback_days: int
    lead_time_high_samples: int
    lead_time_medium_samples: int
    safety_max_units: int
    safety_lookback_days: int
    safety_step_days: int
    safety_min_samples: int
    quantity_tolerance: Decimal


@dataclass(frozen=True)
class Kmp4QueueDay:
    raw_qty: Decimal = ZERO
    matched_qty: Decimal = ZERO
    expired_qty: Decimal = ZERO
    open_qty: Decimal = ZERO
    reserve_increase_qty: Decimal = ZERO


@dataclass(frozen=True)
class LeadTimeProfile:
    p50_days: int
    p75_days: int
    sample_count: int
    source_level: str
    confidence: str
    supplier_ref: str = ""
    supplier_name: str = ""
    last_observation_at: date | None = None


@dataclass(frozen=True)
class EconomicSafetyStock:
    units: Decimal
    expected_saved_margin_rub: Decimal
    carrying_cost_rub: Decimal
    marginal_saved_margin_rub: Decimal
    marginal_carrying_cost_rub: Decimal


@dataclass(frozen=True)
class HistoricalUnitEconomicsEvent:
    business_date: date
    gross_sale_qty: Decimal = ZERO
    net_revenue_rub: Decimal = ZERO
    net_cost_rub: Decimal = ZERO
    gross_sale_cost_rub: Decimal = ZERO


@dataclass
class RegisterHistory:
    by_day: dict[date, dict[str, Decimal]]
    source_counts: dict[str, int]
    reconciliations: list[dict[str, Any]]


@dataclass
class PreflightTables:
    decision_inputs: list[dict[str, Any]]
    scenario_decisions: list[dict[str, Any]]
    lifecycle_daily: list[dict[str, Any]]
    daily_facts: list[dict[str, Any]]
    source_quality: list[dict[str, Any]]
    reconciliations: list[dict[str, Any]]
    status: str


def load_scenario_config(path: Path) -> BacktestScenarioConfig:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    holding = tuple(
        CarryingCostScenario(
            name=_clean(row.get("name")),
            capital_annual_rate=_decimal(row.get("capital_annual_rate")),
            storage_annual_rate=_decimal(row.get("storage_annual_rate")),
            obsolescence_annual_rate=_decimal(row.get("obsolescence_annual_rate")),
        )
        for row in payload.get("holding_cost_scenarios", ())
    )
    lead_time = payload.get("lead_time") or {}
    safety = payload.get("economic_safety_stock") or {}
    acceptance = payload.get("acceptance") or {}
    config = BacktestScenarioConfig(
        kmp4_weights=tuple(_decimal(value) for value in payload.get("kmp4_weights", ())),
        kmp4_queue_days=int(payload.get("kmp4_queue_days") or 0),
        site_signals_enabled=bool(payload.get("site_signals_enabled")),
        holding_cost_scenarios=holding,
        lead_time_fallback_days=int(
            lead_time.get("fallback_days") or DEFAULT_LEAD_TIME_DAYS
        ),
        lead_time_high_samples=int(lead_time.get("high_confidence_min_samples") or 5),
        lead_time_medium_samples=int(lead_time.get("medium_confidence_min_samples") or 2),
        safety_max_units=int(safety.get("max_units") or 1000),
        safety_lookback_days=int(safety.get("demand_sample_lookback_days") or 365),
        safety_step_days=int(safety.get("demand_sample_step_days") or 7),
        safety_min_samples=int(safety.get("min_demand_samples") or 8),
        quantity_tolerance=_decimal(acceptance.get("quantity_tolerance") or "0.001"),
    )
    if not config.kmp4_weights or any(value < ZERO for value in config.kmp4_weights):
        raise ValueError("kmp4_weights must contain non-negative values")
    if config.kmp4_queue_days <= 0:
        raise ValueError("kmp4_queue_days must be positive")
    if not config.holding_cost_scenarios:
        raise ValueError("holding_cost_scenarios must not be empty")
    if any(row.total_annual_rate < ZERO for row in config.holding_cost_scenarios):
        raise ValueError("holding cost rates must be non-negative")
    return config


def fetch_kmp4_demand(
    engine: Any,
    *,
    codes: Sequence[str],
    date_from: date,
    date_to: date,
) -> tuple[dict[str, dict[date, Decimal]], dict[str, int]]:
    """Read the daily KMP4 demand documents for configured demand counterparties."""

    result: dict[str, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    document_refs: set[str] = set()
    line_count = 0
    target_codes = set(codes)
    query = text(
        """
        WITH demand_counterparties AS (
            SELECT DISTINCT _Fld8857RRef AS counterparty_ref
            FROM dbo._Reference69 WITH (NOLOCK)
            WHERE _Fld8857RRef <> 0x00000000000000000000000000000000
        )
        SELECT
            CONVERT(varchar(34), doc._IDRRef, 1) AS document_ref,
            CAST(doc._Date_Time AS date) AS business_date,
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS product_code,
            CAST(line._Fld2431 AS decimal(28, 3)) AS quantity
        FROM dbo._Document132 AS doc WITH (NOLOCK)
        JOIN demand_counterparties AS demand
          ON demand.counterparty_ref = doc._Fld2405RRef
        JOIN dbo._Document132_VT2427 AS line WITH (NOLOCK)
          ON line._Document132_IDRRef = doc._IDRRef
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
          ON product._IDRRef = line._Fld2434RRef
        WHERE doc._Marked = 0x00
          AND doc._Date_Time >= :date_from
          AND doc._Date_Time < :date_to
          AND line._Fld2431 > 0
        """
    )
    with engine.connect() as connection:
        rows = connection.execute(
            query,
            {
                "date_from": datetime.combine(date_from, time.min),
                "date_to": datetime.combine(date_to + timedelta(days=1), time.min),
            },
        ).mappings()
        for row in rows:
            code = _clean(row.get("product_code"))
            business_date = _date(row.get("business_date"))
            qty = _decimal(row.get("quantity"))
            if code not in target_codes or business_date is None or qty <= ZERO:
                continue
            result[code][business_date] += qty
            document_refs.add(_clean(row.get("document_ref")))
            line_count += 1
    return (
        {code: dict(rows) for code, rows in result.items()},
        {
            "document_count": len(document_refs),
            "line_count": line_count,
            "sku_count": len(result),
            "active_date_count": len(
                {business_date for rows in result.values() for business_date in rows}
            ),
        },
    )


def fetch_daily_unit_economics(
    engine: Any,
    *,
    codes: Sequence[str],
    date_from: date,
    date_to: date,
) -> dict[str, list[HistoricalUnitEconomicsEvent]]:
    """Read dated revenue and cost events without projecting future margins backward."""

    query = text(
        """
        WITH target_organization AS (
            SELECT _IDRRef
            FROM dbo._Reference66 WITH (NOLOCK)
            WHERE _Description = N'MASTER MOBILE'
        ),
        revenue_rows AS (
            SELECT
                CAST(reg._Period AS date) AS business_date,
                product._Code AS code,
                reg._RecorderTRef AS recorder_tref,
                reg._RecorderRRef AS recorder_rref,
                reg._Fld7551RRef AS product_ref,
                SUM(CAST(reg._Fld7560 AS decimal(28, 3))) AS qty,
                SUM(CAST(reg._Fld7561 AS decimal(28, 2))) AS revenue
            FROM dbo._AccumRg7550 AS reg WITH (NOLOCK)
            JOIN dbo._Reference62 AS product WITH (NOLOCK)
              ON product._IDRRef = reg._Fld7551RRef
            WHERE reg._Active = 0x01
              AND reg._RecorderTRef IN (0x000000CB, 0x0000006D)
              AND reg._Fld7558RRef IN (SELECT _IDRRef FROM target_organization)
              AND reg._Period >= :date_from
              AND reg._Period < :date_to
              AND product._Code IN :codes
            GROUP BY
                CAST(reg._Period AS date), product._Code, reg._RecorderTRef,
                reg._RecorderRRef, reg._Fld7551RRef
        ),
        cost_rows AS (
            SELECT
                revenue.business_date,
                revenue.code,
                revenue.recorder_tref,
                revenue.recorder_rref,
                revenue.product_ref,
                SUM(CAST(cost._Fld7588 AS decimal(28, 2))) AS cost
            FROM revenue_rows AS revenue
            LEFT JOIN dbo._AccumRg7580 AS cost WITH (NOLOCK)
              ON cost._Active = 0x01
             AND cost._RecorderTRef = revenue.recorder_tref
             AND cost._RecorderRRef = revenue.recorder_rref
             AND cost._Fld7581RRef = revenue.product_ref
            GROUP BY
                revenue.business_date, revenue.code, revenue.recorder_tref,
                revenue.recorder_rref, revenue.product_ref
        )
        SELECT
            revenue.business_date,
            revenue.code,
            SUM(CASE WHEN revenue.recorder_tref = 0x000000CB
                     THEN revenue.qty ELSE 0 END) AS gross_sale_qty,
            SUM(revenue.revenue) AS net_revenue,
            SUM(COALESCE(cost.cost, 0)) AS net_cost,
            SUM(CASE WHEN cost.recorder_tref = 0x000000CB
                     THEN COALESCE(cost.cost, 0) ELSE 0 END) AS gross_sale_cost
        FROM revenue_rows AS revenue
        LEFT JOIN cost_rows AS cost
         ON cost.business_date = revenue.business_date
         AND cost.code = revenue.code
         AND cost.recorder_tref = revenue.recorder_tref
         AND cost.recorder_rref = revenue.recorder_rref
         AND cost.product_ref = revenue.product_ref
        GROUP BY revenue.business_date, revenue.code
        ORDER BY revenue.code, revenue.business_date
        """
    ).bindparams(bindparam("codes", expanding=True))
    result: dict[str, list[HistoricalUnitEconomicsEvent]] = defaultdict(list)
    with engine.connect() as connection:
        for code_chunk in _chunks(sorted(set(codes))):
            rows = connection.execute(
                query,
                {
                    "codes": code_chunk,
                    "date_from": datetime.combine(date_from, time.min),
                    "date_to": datetime.combine(date_to + timedelta(days=1), time.min),
                },
            ).mappings()
            for row in rows:
                code = _clean(row.get("code"))
                business_date = _date(row.get("business_date"))
                if not code or business_date is None:
                    continue
                result[code].append(
                    HistoricalUnitEconomicsEvent(
                        business_date=business_date,
                        gross_sale_qty=_decimal(row.get("gross_sale_qty")),
                        net_revenue_rub=_decimal(row.get("net_revenue")),
                        net_cost_rub=_decimal(row.get("net_cost")),
                        gross_sale_cost_rub=_decimal(row.get("gross_sale_cost")),
                    )
                )
    return dict(result)


def _fetch_register_openings(
    engine: Any,
    *,
    table_name: str,
    product_field: str,
    quantity_field: str,
    codes: Sequence[str],
    date_from: date,
    date_to: date,
) -> tuple[dict[date, dict[str, Decimal]], int]:
    query = text(
        f"""
        SELECT
            CAST(reg._Period AS date) AS period_month,
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS product_code,
            CAST(SUM(reg.{quantity_field}) AS decimal(28, 3)) AS quantity
        FROM dbo.{table_name} AS reg WITH (NOLOCK)
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
          ON product._IDRRef = reg.{product_field}
        WHERE reg._Period >= :date_from
          AND reg._Period <= :date_to
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
        GROUP BY CAST(reg._Period AS date), product._Code
        """
    ).bindparams(bindparam("codes", expanding=True))
    out: dict[date, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    row_count = 0
    with engine.connect() as connection:
        for code_chunk in _chunks(sorted(set(codes))):
            for row in connection.execute(
                query,
                {
                    "date_from": datetime.combine(date_from, time.min),
                    "date_to": datetime.combine(date_to, time.min),
                    "codes": code_chunk,
                },
            ).mappings():
                period_month = _date(row.get("period_month"))
                code = _clean(row.get("product_code"))
                if period_month is not None and code:
                    out[period_month][code] += _decimal(row.get("quantity"))
                    row_count += 1
    return ({month: dict(rows) for month, rows in out.items()}, row_count)


def _fetch_register_movements(
    engine: Any,
    *,
    table_name: str,
    product_field: str,
    quantity_field: str,
    codes: Sequence[str],
    date_from: date,
    date_to: date,
) -> tuple[dict[date, dict[str, Decimal]], int]:
    query = text(
        f"""
        SELECT
            CAST(reg._Period AS date) AS business_date,
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS product_code,
            CAST(SUM(
                CASE WHEN reg._RecordKind = 0
                     THEN reg.{quantity_field} ELSE -reg.{quantity_field} END
            ) AS decimal(28, 3)) AS quantity_delta
        FROM dbo.{table_name} AS reg WITH (NOLOCK)
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
          ON product._IDRRef = reg.{product_field}
        WHERE reg._Active = 0x01
          AND reg._Period >= :date_from
          AND reg._Period < :date_to
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
        GROUP BY CAST(reg._Period AS date), product._Code
        """
    ).bindparams(bindparam("codes", expanding=True))
    out: dict[date, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    row_count = 0
    with engine.connect() as connection:
        for code_chunk in _chunks(sorted(set(codes))):
            for row in connection.execute(
                query,
                {
                    "date_from": datetime.combine(date_from, time.min),
                    "date_to": datetime.combine(date_to + timedelta(days=1), time.min),
                    "codes": code_chunk,
                },
            ).mappings():
                business_date = _date(row.get("business_date"))
                code = _clean(row.get("product_code"))
                if business_date is not None and code:
                    out[business_date][code] += _decimal(row.get("quantity_delta"))
                    row_count += 1
    return ({day: dict(rows) for day, rows in out.items()}, row_count)


def reconstruct_quantity_register(
    engine: Any,
    *,
    register_name: str,
    totals_table: str,
    movements_table: str,
    product_field: str,
    quantity_field: str,
    codes: Sequence[str],
    date_from: date,
    date_to: date,
    tolerance: Decimal = Decimal("0.001"),
) -> RegisterHistory:
    """Reconstruct a monthly accumulation register at daily SKU grain."""

    by_day: dict[date, dict[str, Decimal]] = {}
    first_month = _month_start(date_from)
    final_month = _month_start(date_to)
    last_opening = _next_month(final_month)
    openings_by_month, opening_rows = _fetch_register_openings(
        engine,
        table_name=totals_table,
        product_field=product_field,
        quantity_field=quantity_field,
        codes=codes,
        date_from=first_month,
        date_to=last_opening,
    )
    all_movements, movement_rows = _fetch_register_movements(
        engine,
        table_name=movements_table,
        product_field=product_field,
        quantity_field=quantity_field,
        codes=codes,
        date_from=first_month,
        date_to=_next_month(final_month) - timedelta(days=1),
    )

    cursor = first_month
    reconciliations: list[dict[str, Any]] = []
    negative_balance_rows = 0
    while cursor <= final_month:
        month_end = _next_month(cursor) - timedelta(days=1)
        balance = defaultdict(Decimal, openings_by_month.get(cursor, {}))
        for business_date in _daterange(cursor, month_end):
            for code, delta in all_movements.get(business_date, {}).items():
                balance[code] += delta
            if business_date >= date_from and business_date <= date_to:
                row = {code: qty for code, qty in balance.items() if qty != ZERO}
                negative_balance_rows += sum(qty < -tolerance for qty in row.values())
                by_day[business_date] = row
        next_opening = openings_by_month.get(_next_month(cursor), {})
        all_codes = set(balance) | set(next_opening)
        differences = {
            code: balance.get(code, ZERO) - next_opening.get(code, ZERO)
            for code in all_codes
        }
        max_abs = max((abs(value) for value in differences.values()), default=ZERO)
        mismatch_count = sum(abs(value) > tolerance for value in differences.values())
        reconciliations.append(
            {
                "source": register_name,
                "period_month": cursor.isoformat(),
                "compared_sku_count": len(all_codes),
                "mismatch_sku_count": mismatch_count,
                "max_abs_difference_qty": str(max_abs),
                "status": "pass" if mismatch_count == 0 else "fail",
            }
        )
        cursor = _next_month(cursor)
    return RegisterHistory(
        by_day=by_day,
        source_counts={
            "opening_rows": opening_rows,
            "movement_rows": movement_rows,
            "negative_balance_rows": negative_balance_rows,
        },
        reconciliations=reconciliations,
    )


def reconstruct_historical_reserves(
    engine: Any,
    *,
    codes: Sequence[str],
    date_from: date,
    date_to: date,
    tolerance: Decimal = Decimal("0.001"),
) -> RegisterHistory:
    return reconstruct_quantity_register(
        engine,
        register_name="customer_reserve",
        totals_table="_AccumRgT7662",
        movements_table="_AccumRg7653",
        product_field="_Fld7655RRef",
        quantity_field="_Fld7659",
        codes=codes,
        date_from=date_from,
        date_to=date_to,
        tolerance=tolerance,
    )


def reconstruct_historical_placements(
    engine: Any,
    *,
    codes: Sequence[str],
    date_from: date,
    date_to: date,
    tolerance: Decimal = Decimal("0.001"),
) -> RegisterHistory:
    return reconstruct_quantity_register(
        engine,
        register_name="customer_order_placement",
        totals_table="_AccumRgT7606",
        movements_table="_AccumRg7596",
        product_field="_Fld7598RRef",
        quantity_field="_Fld7602",
        codes=codes,
        date_from=date_from,
        date_to=date_to,
        tolerance=tolerance,
    )


def reconstruct_historical_stock(
    engine: Any,
    *,
    codes: Sequence[str],
    network_warehouse_codes: Sequence[str],
    physical_warehouse_codes: Sequence[str],
    date_from: date,
    date_to: date,
) -> tuple[dict[date, dict[str, Decimal]], dict[str, set[date]], dict[str, int]]:
    """Rebuild display stock with filtering performed inside SQL Server."""

    opening_query = text(
        """
        SELECT
            CAST(stock._Period AS date) AS period_month,
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS product_code,
            NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') AS warehouse_code,
            CAST(SUM(stock._Fld7743) AS decimal(28, 3)) AS quantity
        FROM dbo._AccumRgT7745 AS stock WITH (NOLOCK)
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
          ON product._IDRRef = stock._Fld7738RRef
        JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
          ON warehouse._IDRRef = stock._Fld7742RRef
        WHERE stock._Period >= :date_from
          AND stock._Period <= :date_to
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
          AND NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') IN :warehouses
        GROUP BY CAST(stock._Period AS date), product._Code, warehouse._Code
        """
    ).bindparams(
        bindparam("codes", expanding=True), bindparam("warehouses", expanding=True)
    )
    movement_query = text(
        """
        SELECT
            CAST(movement._Period AS date) AS business_date,
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS product_code,
            NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') AS warehouse_code,
            CAST(SUM(
                CASE WHEN movement._RecordKind = 0
                     THEN movement._Fld7743 ELSE -movement._Fld7743 END
            ) AS decimal(28, 3)) AS quantity_delta
        FROM dbo._AccumRg7735 AS movement WITH (NOLOCK)
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
          ON product._IDRRef = movement._Fld7738RRef
        JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
          ON warehouse._IDRRef = movement._Fld7742RRef
        WHERE movement._Active = 0x01
          AND movement._Period >= :date_from
          AND movement._Period < :date_to
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
          AND NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') IN :warehouses
        GROUP BY CAST(movement._Period AS date), product._Code, warehouse._Code
        """
    ).bindparams(
        bindparam("codes", expanding=True), bindparam("warehouses", expanding=True)
    )
    warehouses = tuple(sorted(set(network_warehouse_codes)))
    if not warehouses:
        raise ValueError("network warehouse list must not be empty")
    first_month = _month_start(date_from)
    final_month = _month_start(date_to)
    openings: dict[date, dict[tuple[str, str], Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    movements: dict[date, dict[tuple[str, str], Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    opening_rows = 0
    movement_rows = 0
    with engine.connect() as connection:
        for code_chunk in _chunks(sorted(set(codes))):
            for row in connection.execute(
                opening_query,
                {
                    "date_from": datetime.combine(first_month, time.min),
                    "date_to": datetime.combine(final_month, time.min),
                    "codes": code_chunk,
                    "warehouses": warehouses,
                },
            ).mappings():
                period_month = _date(row.get("period_month"))
                code = _clean(row.get("product_code"))
                warehouse = _clean(row.get("warehouse_code"))
                if period_month is not None and code and warehouse:
                    openings[period_month][(code, warehouse)] += _decimal(
                        row.get("quantity")
                    )
                    opening_rows += 1
            for row in connection.execute(
                movement_query,
                {
                    "date_from": datetime.combine(first_month, time.min),
                    "date_to": datetime.combine(date_to + timedelta(days=1), time.min),
                    "codes": code_chunk,
                    "warehouses": warehouses,
                },
            ).mappings():
                business_date = _date(row.get("business_date"))
                code = _clean(row.get("product_code"))
                warehouse = _clean(row.get("warehouse_code"))
                if business_date is not None and code and warehouse:
                    movements[business_date][(code, warehouse)] += _decimal(
                        row.get("quantity_delta")
                    )
                    movement_rows += 1

    wanted_codes = tuple(sorted(set(codes)))
    physical_codes = set(physical_warehouse_codes)
    stock_by_day: dict[date, dict[str, Decimal]] = {}
    available_days: dict[str, set[date]] = defaultdict(set)
    cursor = first_month
    while cursor <= final_month:
        balances = defaultdict(Decimal, openings.get(cursor, {}))
        month_end = min(_next_month(cursor) - timedelta(days=1), date_to)
        keys_by_code: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for key in set(balances) | {
            key
            for business_date in _daterange(cursor, month_end)
            for key in movements.get(business_date, {})
        }:
            keys_by_code[key[0]].append(key)
        for business_date in _daterange(cursor, month_end):
            for key, delta in movements.get(business_date, {}).items():
                balances[key] += delta
            if business_date < date_from:
                continue
            daily: dict[str, Decimal] = {}
            for code in wanted_codes:
                keys = keys_by_code.get(code, ())
                daily[code] = sum((max(ZERO, balances[key]) for key in keys), ZERO)
                if any(
                    key[1] in physical_codes and balances[key] > ZERO for key in keys
                ):
                    available_days[code].add(business_date)
            stock_by_day[business_date] = daily
        cursor = _next_month(cursor)
    return (
        stock_by_day,
        dict(available_days),
        {
            "opening_rows": opening_rows,
            "movement_rows": movement_rows,
            "months": (final_month.year - first_month.year) * 12
            + final_month.month
            - first_month.month
            + 1,
        },
    )


def build_historical_incoming_by_day(
    *,
    codes: Sequence[str],
    purchases: Mapping[str, Sequence[PurchaseLine]],
    receipts: Mapping[str, Sequence[ReceiptLine]],
    date_from: date,
    date_to: date,
) -> dict[date, dict[str, Decimal]]:
    events: dict[date, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for code in codes:
        for row in purchases.get(code, ()):
            if row.created_at <= date_to:
                events[row.created_at][code] += max(ZERO, row.qty)
        for row in receipts.get(code, ()):
            if row.received_at <= date_to:
                events[row.received_at][code] -= max(ZERO, row.qty)
    balances = defaultdict(Decimal)
    result: dict[date, dict[str, Decimal]] = {}
    history_start = min(events, default=date_from)
    for business_date in _daterange(min(history_start, date_from), date_to):
        for code, delta in events.get(business_date, {}).items():
            balances[code] = max(ZERO, balances[code] + delta)
        if business_date >= date_from:
            result[business_date] = {
                code: qty for code, qty in balances.items() if qty > ZERO
            }
    return result


def build_kmp4_queue_history(
    *,
    codes: Sequence[str],
    raw_demand_by_code: Mapping[str, Mapping[date, Decimal]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    reserves_by_day: Mapping[date, Mapping[str, Decimal]],
    date_from: date,
    date_to: date,
    queue_days: int,
) -> dict[str, dict[date, Kmp4QueueDay]]:
    result: dict[str, dict[date, Kmp4QueueDay]] = defaultdict(dict)
    for code in codes:
        queue: deque[list[Any]] = deque()
        previous_reserve = ZERO
        for business_date in _daterange(date_from, date_to):
            expired = ZERO
            while queue and (business_date - queue[0][0]).days >= queue_days:
                expired += _decimal(queue.popleft()[1])
            raw = max(ZERO, _decimal(raw_demand_by_code.get(code, {}).get(business_date)))
            if raw > ZERO:
                queue.append([business_date, raw])
            reserve = max(ZERO, _decimal(reserves_by_day.get(business_date, {}).get(code)))
            reserve_increase = max(ZERO, reserve - previous_reserve)
            previous_reserve = reserve
            realized = max(
                max(ZERO, _decimal(sales_by_code.get(code, {}).get(business_date))),
                reserve_increase,
            )
            matched = ZERO
            remaining = realized
            while queue and remaining > ZERO:
                used = min(_decimal(queue[0][1]), remaining)
                queue[0][1] = _decimal(queue[0][1]) - used
                remaining -= used
                matched += used
                if _decimal(queue[0][1]) <= ZERO:
                    queue.popleft()
            open_qty = sum((_decimal(row[1]) for row in queue), ZERO)
            result[code][business_date] = Kmp4QueueDay(
                raw_qty=raw,
                matched_qty=matched,
                expired_qty=expired,
                open_qty=open_qty,
                reserve_increase_qty=reserve_increase,
            )
    return {code: dict(rows) for code, rows in result.items()}


def _nearest_rank(values: Sequence[int], quantile: Decimal) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, int(_ceil(quantile * Decimal(len(ordered)))))
    return int(ordered[rank - 1])


def latest_supplier_identity(
    detail_rows: Sequence[Mapping[str, Any]],
    *,
    code: str,
    as_of: date,
) -> tuple[str, str]:
    candidates = []
    for row in detail_rows:
        if _clean(row.get("nomenclature_code")) != code:
            continue
        order_date = _date(row.get("supplier_order_created_at"))
        if order_date is None or order_date >= as_of:
            continue
        candidates.append((order_date, row))
    if not candidates:
        return "", ""
    row = max(candidates, key=lambda item: item[0])[1]
    return _clean(row.get("supplier_ref")), _clean(row.get("supplier_name"))


def select_lead_time_profile(
    detail_rows: Sequence[Mapping[str, Any]],
    *,
    code: str,
    group_key: str,
    supplier_ref: str,
    supplier_name: str,
    as_of: date,
    config: BacktestScenarioConfig,
) -> LeadTimeProfile:
    completed: list[tuple[Mapping[str, Any], int, date]] = []
    for row in detail_rows:
        receipt_at = _date(row.get("warehouse_receipt_at"))
        days = int(_decimal(row.get("total_arrival_days")))
        if receipt_at is None or receipt_at >= as_of or days < 0:
            continue
        completed.append((row, days, receipt_at))
    levels = (
        (
            "sku_supplier",
            [
                item
                for item in completed
                if _clean(item[0].get("nomenclature_code")) == code
                and supplier_ref
                and _clean(item[0].get("supplier_ref")) == supplier_ref
            ],
        ),
        (
            "display_group_supplier",
            [
                item
                for item in completed
                if _clean(item[0].get("display_group_key")) == group_key
                and supplier_ref
                and _clean(item[0].get("supplier_ref")) == supplier_ref
            ],
        ),
        (
            "supplier",
            [
                item
                for item in completed
                if supplier_ref and _clean(item[0].get("supplier_ref")) == supplier_ref
            ],
        ),
        ("all_displays", completed),
    )
    for level, rows in levels:
        if len(rows) < config.lead_time_medium_samples:
            continue
        values = [item[1] for item in rows]
        confidence = (
            "high" if len(rows) >= config.lead_time_high_samples else "medium"
        )
        return LeadTimeProfile(
            p50_days=max(1, _nearest_rank(values, Decimal("0.50"))),
            p75_days=max(1, _nearest_rank(values, Decimal("0.75"))),
            sample_count=len(rows),
            source_level=level,
            confidence=confidence,
            supplier_ref=supplier_ref,
            supplier_name=supplier_name,
            last_observation_at=max(item[2] for item in rows),
        )
    return LeadTimeProfile(
        p50_days=config.lead_time_fallback_days,
        p75_days=config.lead_time_fallback_days,
        sample_count=0,
        source_level="fixed_fallback",
        confidence="low",
        supplier_ref=supplier_ref,
        supplier_name=supplier_name,
    )


def historical_demand_samples(
    sales: Mapping[date, Decimal],
    *,
    as_of: date,
    horizon_days: int,
    lookback_days: int,
    step_days: int,
) -> list[Decimal]:
    if horizon_days <= 0:
        return []
    earliest = as_of - timedelta(days=lookback_days)
    latest_start = as_of - timedelta(days=horizon_days)
    samples: list[Decimal] = []
    cursor = earliest
    while cursor <= latest_start:
        end = cursor + timedelta(days=horizon_days - 1)
        samples.append(
            sum(
                (
                    max(ZERO, _decimal(qty))
                    for business_date, qty in sales.items()
                    if cursor <= business_date <= end
                ),
                ZERO,
            )
        )
        cursor += timedelta(days=max(1, step_days))
    return samples


def calculate_economic_safety_stock(
    *,
    base_max_qty: Decimal,
    demand_samples: Sequence[Decimal],
    gross_margin_per_unit_rub: Decimal,
    inventory_cost_per_unit_rub: Decimal,
    holding_days: int,
    cost_scenario: CarryingCostScenario,
    max_units: int,
    min_samples: int,
) -> EconomicSafetyStock:
    if (
        len(demand_samples) < min_samples
        or gross_margin_per_unit_rub <= ZERO
        or inventory_cost_per_unit_rub <= ZERO
        or holding_days <= 0
    ):
        return EconomicSafetyStock(ZERO, ZERO, ZERO, ZERO, ZERO)
    unit_cost = (
        inventory_cost_per_unit_rub
        * cost_scenario.total_annual_rate
        * Decimal(holding_days)
        / Decimal("365")
    )
    units = 0
    saved_total = ZERO
    cost_total = ZERO
    marginal_saved = ZERO
    for unit_number in range(1, max_units + 1):
        threshold = base_max_qty + Decimal(unit_number - 1)
        probability = Decimal(
            sum(sample > threshold for sample in demand_samples)
        ) / Decimal(len(demand_samples))
        marginal_saved = gross_margin_per_unit_rub * probability
        if marginal_saved <= unit_cost:
            return EconomicSafetyStock(
                Decimal(units), saved_total, cost_total, marginal_saved, unit_cost
            )
        units += 1
        saved_total += marginal_saved
        cost_total += unit_cost
    return EconomicSafetyStock(
        Decimal(units), saved_total, cost_total, marginal_saved, unit_cost
    )


def _inventory_cost_and_margin(
    *,
    code: str,
    economics: Mapping[str, Any],
    purchases: Mapping[str, Sequence[PurchaseLine]],
    as_of: date,
) -> tuple[Decimal, Decimal, str]:
    row = economics.get(code)
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
        events = [
            event
            for event in row
            if isinstance(event, HistoricalUnitEconomicsEvent)
            and event.business_date <= as_of
        ]
        gross_qty = sum((event.gross_sale_qty for event in events), ZERO)
        net_revenue = sum((event.net_revenue_rub for event in events), ZERO)
        net_cost = sum((event.net_cost_rub for event in events), ZERO)
        gross_cost = sum((event.gross_sale_cost_rub for event in events), ZERO)
        inventory_cost = gross_cost / gross_qty if gross_qty > ZERO else ZERO
        margin = (net_revenue - net_cost) / gross_qty if gross_qty > ZERO else ZERO
    else:
        inventory_cost = _decimal(
            getattr(row, "inventory_cost_per_unit", ZERO) if row is not None else ZERO
        )
        margin = _decimal(
            getattr(row, "gross_profit_per_gross_unit", ZERO) if row is not None else ZERO
        )
    if inventory_cost > ZERO:
        return inventory_cost, margin, "historical_unit_economics"
    purchase = _latest_purchase(purchases.get(code, ()), as_of=as_of)
    if purchase is not None and purchase.price > ZERO:
        return purchase.price, margin, "latest_purchase_price"
    return ZERO, margin, "unpriced"


def _action_for_stage(
    *,
    status: str,
    recommended_qty: Decimal,
    scheduled_review: bool,
    lead_time_confidence: str,
    kmp4_open_weighted: Decimal,
) -> str:
    if recommended_qty <= ZERO:
        return "do_not_order"
    if status == AssortmentStatus.FRUIT.value:
        return "do_not_order"
    if status == AssortmentStatus.NEWBORN.value:
        return "manual_review" if kmp4_open_weighted > ZERO else "do_not_order"
    if status in {
        AssortmentStatus.NEW_ITEM.value,
        AssortmentStatus.SALES_START.value,
    }:
        return "manual_review"
    if not scheduled_review or lead_time_confidence == "low":
        return "manual_review"
    return "order"


def build_preflight_tables(
    *,
    items: Sequence[Mapping[str, Any]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    availability_by_code: Mapping[str, set[date]],
    stock_by_day: Mapping[date, Mapping[str, Decimal]],
    reserves: RegisterHistory,
    placements: RegisterHistory,
    incoming_by_day: Mapping[date, Mapping[str, Decimal]],
    kmp4_raw_by_code: Mapping[str, Mapping[date, Decimal]],
    purchases: Mapping[str, Sequence[PurchaseLine]],
    receipts: Mapping[str, Sequence[ReceiptLine]],
    launch_observations: Sequence[LaunchObservation],
    lead_time_detail_rows: Sequence[Mapping[str, Any]],
    economics: Mapping[str, Any],
    policy: AutoOrderPolicy,
    config: BacktestScenarioConfig,
    history_start: date,
    date_from: date,
    date_to: date,
    source_metadata: Mapping[str, Any] | None = None,
    launch_profile_min_samples: int = DEFAULT_LAUNCH_PROFILE_MIN_SAMPLES,
) -> PreflightTables:
    codes = sorted(
        {
            _clean(item.get("nomenclature_code"))
            for item in items
            if _clean(item.get("nomenclature_code"))
        }
    )
    item_by_code = {
        _clean(item.get("nomenclature_code")): item
        for item in items
        if _clean(item.get("nomenclature_code"))
    }
    kmp4_queue = build_kmp4_queue_history(
        codes=codes,
        raw_demand_by_code=kmp4_raw_by_code,
        sales_by_code=sales_by_code,
        reserves_by_day=reserves.by_day,
        date_from=history_start,
        date_to=date_to,
        queue_days=config.kmp4_queue_days,
    )
    warmup_sales = {
        code: {day: qty for day, qty in sales_by_code.get(code, {}).items() if day < date_from}
        for code in codes
    }
    warmup_availability = {
        code: {day for day in availability_by_code.get(code, set()) if day < date_from}
        for code in codes
    }
    previous_statuses = warmup_lifecycle_statuses(
        items=items,
        sales_by_code=warmup_sales,
        availability_by_code=warmup_availability,
        purchase_history=purchases,
        receipt_history=receipts,
        date_from=history_start,
        date_to=date_from - timedelta(days=1),
    )
    lifecycle_daily: list[dict[str, Any]] = []
    daily_facts: list[dict[str, Any]] = []
    decision_inputs: list[dict[str, Any]] = []
    scenario_decisions: list[dict[str, Any]] = []
    cadence = max(1, policy.order_cadence_days)

    for business_date in _daterange(date_from, date_to):
        scheduled_review = (business_date - date_from).days % cadence == 0
        launch_snapshot = build_launch_profile_snapshot(
            launch_observations, as_of=business_date
        )
        for code in codes:
            item = item_by_code[code]
            if not item_active_as_of(item, as_of=business_date):
                continue
            previous_status = previous_statuses.get(code)
            lifecycle, lifecycle_evidence = historical_lifecycle_decision(
                item=item,
                sales=sales_by_code.get(code, {}),
                availability_dates=availability_by_code.get(code, set()),
                purchases=purchases.get(code, ()),
                receipts=receipts.get(code, ()),
                as_of=business_date,
                previous_status=previous_status,
            )
            previous_statuses[code] = lifecycle.status.value
            rate, trend, rate_evidence = forecast_rate(
                sales_by_code.get(code, {}),
                availability_by_code.get(code, set()),
                as_of=business_date,
                demand_multiplier=_demand_multiplier(item, policy),
            )
            queue_day = kmp4_queue.get(code, {}).get(business_date, Kmp4QueueDay())
            event_review = trend == "accelerating" or queue_day.raw_qty > ZERO
            physical_stock = _decimal(stock_by_day.get(business_date, {}).get(code))
            reserve_qty = _decimal(reserves.by_day.get(business_date, {}).get(code))
            gross_incoming = _decimal(incoming_by_day.get(business_date, {}).get(code))
            placed_incoming = _decimal(placements.by_day.get(business_date, {}).get(code))
            free_incoming = max(ZERO, gross_incoming - placed_incoming)
            free_stock = physical_stock - reserve_qty
            inventory_position = free_stock + free_incoming
            supplier_ref, supplier_name = latest_supplier_identity(
                lead_time_detail_rows, code=code, as_of=business_date
            )
            group_key = display_group_key(
                {"name": _clean(item.get("name")), "nomenclature_code": code}
            )
            lead_time = select_lead_time_profile(
                lead_time_detail_rows,
                code=code,
                group_key=group_key,
                supplier_ref=supplier_ref,
                supplier_name=supplier_name,
                as_of=business_date,
                config=config,
            )
            inventory_cost, unit_margin, cost_source = _inventory_cost_and_margin(
                code=code,
                economics=economics,
                purchases=purchases,
                as_of=business_date,
            )
            daily_facts.append(
                {
                    "business_date": business_date.isoformat(),
                    "nomenclature_code": code,
                    "observed_sales_qty": str(
                        _decimal(sales_by_code.get(code, {}).get(business_date))
                    ),
                    "physical_stock_qty": str(physical_stock),
                    "reserve_qty": str(reserve_qty),
                    "gross_incoming_qty": str(gross_incoming),
                    "placed_incoming_qty": str(placed_incoming),
                    "free_incoming_qty": str(free_incoming),
                    "kmp4_raw_qty": str(queue_day.raw_qty),
                    "kmp4_matched_qty": str(queue_day.matched_qty),
                    "kmp4_expired_qty": str(queue_day.expired_qty),
                    "kmp4_open_qty": str(queue_day.open_qty),
                    "status": lifecycle.status.value,
                }
            )
            lifecycle_daily.append(
                {
                    "business_date": business_date.isoformat(),
                    "nomenclature_code": code,
                    "name": _clean(item.get("name")),
                    "previous_status": previous_status or "",
                    "status": lifecycle.status.value,
                    "status_label": lifecycle.status_label,
                    "reason_codes": "|".join(lifecycle.reason_codes),
                    "auto_order_allowed": int(lifecycle.auto_order_allowed),
                    "manual_review_required": int(lifecycle.manual_review_required),
                    "sales_30": str(lifecycle_evidence.get("sales_30") or ZERO),
                    "sales_90": str(lifecycle_evidence.get("sales_90") or ZERO),
                    "sales_180": str(lifecycle_evidence.get("sales_180") or ZERO),
                    "available_days_30": lifecycle_evidence.get("available_30") or 0,
                    "available_days_90": lifecycle_evidence.get("available_90") or 0,
                    "available_days_180": lifecycle_evidence.get("available_180") or 0,
                }
            )
            if not scheduled_review and not event_review:
                continue
            input_row = {
                "decision_date": business_date.isoformat(),
                "nomenclature_code": code,
                "name": _clean(item.get("name")),
                "row_kind": (
                    "scheduled_and_event"
                    if scheduled_review and event_review
                    else "scheduled_review" if scheduled_review else "event_review"
                ),
                "scheduled_review": int(scheduled_review),
                "event_review": int(event_review),
                "previous_status": previous_status or "",
                "status": lifecycle.status.value,
                "status_label": lifecycle.status_label,
                "status_reason_codes": "|".join(lifecycle.reason_codes),
                "sales_30": str(rate_evidence["sales_30"]),
                "sales_90": str(rate_evidence["sales_90"]),
                "sales_180": str(rate_evidence["sales_180"]),
                "available_days_30": rate_evidence["available_30"],
                "available_days_90": rate_evidence["available_90"],
                "available_days_180": rate_evidence["available_180"],
                "forecast_rate_sales": str(rate),
                "sales_trend": trend,
                "kmp4_raw_qty": str(queue_day.raw_qty),
                "kmp4_matched_qty": str(queue_day.matched_qty),
                "kmp4_expired_qty": str(queue_day.expired_qty),
                "kmp4_open_qty": str(queue_day.open_qty),
                "reserve_increase_qty": str(queue_day.reserve_increase_qty),
                "site_signals_included": int(config.site_signals_enabled),
                "physical_stock_qty": str(physical_stock),
                "reserve_qty": str(reserve_qty),
                "free_stock_qty": str(free_stock),
                "gross_incoming_qty": str(gross_incoming),
                "placed_incoming_qty": str(placed_incoming),
                "free_incoming_qty": str(free_incoming),
                "inventory_position_qty": str(inventory_position),
                "supplier_ref": supplier_ref,
                "supplier_name": supplier_name,
                "lead_time_p50_days": lead_time.p50_days,
                "lead_time_p75_days": lead_time.p75_days,
                "lead_time_sample_count": lead_time.sample_count,
                "lead_time_source_level": lead_time.source_level,
                "lead_time_confidence": lead_time.confidence,
                "lead_time_last_observation_at": (
                    lead_time.last_observation_at.isoformat()
                    if lead_time.last_observation_at
                    else ""
                ),
                "inventory_cost_per_unit_rub": str(inventory_cost),
                "gross_margin_per_unit_rub": str(unit_margin),
                "unit_economics_source": cost_source,
                "source_warnings": "|".join(
                    warning
                    for warning, applies in (
                        ("lead_time_fixed_fallback", lead_time.confidence == "low"),
                        ("unit_economics_missing", inventory_cost <= ZERO),
                        ("negative_free_stock", free_stock < ZERO),
                        ("site_history_excluded", not config.site_signals_enabled),
                    )
                    if applies
                ),
            }
            decision_inputs.append(input_row)

            legacy_target = _ceil(rate * Decimal(DEFAULT_LEAD_TIME_DAYS + cadence))
            legacy_raw = _ceil(max(ZERO, legacy_target - physical_stock - gross_incoming))
            legacy_qty = rounded_order_qty(
                legacy_raw,
                min_order_qty=policy.min_order_qty,
                max_order_qty=policy.max_order_qty,
                order_rounding_rules=policy.order_rounding_rules,
            )
            scenario_decisions.append(
                {
                    "scenario_id": "legacy",
                    "decision_date": business_date.isoformat(),
                    "nomenclature_code": code,
                    "stage_profile": "legacy",
                    "kmp4_weight": "0",
                    "holding_cost_scenario": "legacy_excluded",
                    "status": lifecycle.status.value,
                    "forecast_rate": str(rate),
                    "selected_lead_time_days": DEFAULT_LEAD_TIME_DAYS,
                    "lead_time_basis": "fixed_legacy",
                    "min_stock_qty": str(legacy_target),
                    "max_stock_qty": str(legacy_target),
                    "economic_safety_stock_qty": "0",
                    "recommended_order_qty_raw": str(legacy_raw),
                    "recommended_order_qty": str(legacy_qty),
                    "decision": _action_for_stage(
                        status=lifecycle.status.value,
                        recommended_qty=legacy_qty,
                        scheduled_review=scheduled_review,
                        lead_time_confidence="high",
                        kmp4_open_weighted=ZERO,
                    ),
                    "inventory_position_qty": str(physical_stock + gross_incoming),
                    "expected_saved_margin_rub": "0",
                    "carrying_cost_rub": "0",
                    "manual_review_reason": "legacy_control",
                }
            )

            for stage_name in ("conservative", "typical", "service"):
                stage_scenario = stage_model_scenario(stage_name)
                launch_profile = select_launch_profile(
                    item=item,
                    snapshot=launch_snapshot,
                    scenario=stage_scenario,
                    policy=policy,
                    min_samples=launch_profile_min_samples,
                )
                for kmp4_weight in config.kmp4_weights:
                    weighted_kmp = queue_day.open_qty * kmp4_weight
                    scenario_rate = rate + weighted_kmp / Decimal(config.kmp4_queue_days)
                    if (
                        lifecycle.status is AssortmentStatus.NEW_ITEM
                        and launch_profile is not None
                    ):
                        scenario_rate = max(
                            scenario_rate,
                            launch_profile.demand_qty_30d / Decimal("30"),
                        )
                    for cost_scenario in config.holding_cost_scenarios:
                        selected_days = lead_time.p50_days
                        min_qty = _ceil(scenario_rate * Decimal(selected_days))
                        max_qty = _ceil(scenario_rate * Decimal(selected_days + cadence))
                        samples = historical_demand_samples(
                            sales_by_code.get(code, {}),
                            as_of=business_date,
                            horizon_days=selected_days + cadence,
                            lookback_days=config.safety_lookback_days,
                            step_days=config.safety_step_days,
                        )
                        safety = calculate_economic_safety_stock(
                            base_max_qty=max_qty,
                            demand_samples=samples,
                            gross_margin_per_unit_rub=unit_margin,
                            inventory_cost_per_unit_rub=inventory_cost,
                            holding_days=selected_days + cadence,
                            cost_scenario=cost_scenario,
                            max_units=config.safety_max_units,
                            min_samples=config.safety_min_samples,
                        )
                        if safety.units > ZERO and lead_time.p75_days > selected_days:
                            selected_days = lead_time.p75_days
                            min_qty = _ceil(scenario_rate * Decimal(selected_days))
                            max_qty = _ceil(
                                scenario_rate * Decimal(selected_days + cadence)
                            )
                            samples = historical_demand_samples(
                                sales_by_code.get(code, {}),
                                as_of=business_date,
                                horizon_days=selected_days + cadence,
                                lookback_days=config.safety_lookback_days,
                                step_days=config.safety_step_days,
                            )
                            safety = calculate_economic_safety_stock(
                                base_max_qty=max_qty,
                                demand_samples=samples,
                                gross_margin_per_unit_rub=unit_margin,
                                inventory_cost_per_unit_rub=inventory_cost,
                                holding_days=selected_days + cadence,
                                cost_scenario=cost_scenario,
                                max_units=config.safety_max_units,
                                min_samples=config.safety_min_samples,
                            )
                        target_qty = max_qty + safety.units
                        if lifecycle.status is AssortmentStatus.FRUIT:
                            target_qty = ZERO
                        elif lifecycle.status is AssortmentStatus.NEWBORN:
                            target_qty = weighted_kmp
                        reorder_triggered = inventory_position <= min_qty
                        raw = (
                            _ceil(max(ZERO, target_qty - inventory_position))
                            if reorder_triggered
                            or lifecycle.status
                            in {AssortmentStatus.NEWBORN, AssortmentStatus.NEW_ITEM}
                            else ZERO
                        )
                        recommended = rounded_order_qty(
                            raw,
                            min_order_qty=policy.min_order_qty,
                            max_order_qty=policy.max_order_qty,
                            order_rounding_rules=policy.order_rounding_rules,
                        )
                        decision = _action_for_stage(
                            status=lifecycle.status.value,
                            recommended_qty=recommended,
                            scheduled_review=scheduled_review,
                            lead_time_confidence=lead_time.confidence,
                            kmp4_open_weighted=weighted_kmp,
                        )
                        reasons = []
                        if not scheduled_review:
                            reasons.append("out_of_cycle_manual_review")
                        if lead_time.confidence == "low":
                            reasons.append("lead_time_low_confidence")
                        if inventory_cost <= ZERO or unit_margin <= ZERO:
                            reasons.append("unit_economics_low_confidence")
                        if lifecycle.status.value in {
                            AssortmentStatus.NEWBORN.value,
                            AssortmentStatus.NEW_ITEM.value,
                            AssortmentStatus.SALES_START.value,
                        }:
                            reasons.append(f"stage_{lifecycle.status.value}_manual_review")
                        scenario_decisions.append(
                            {
                                "scenario_id": (
                                    f"{stage_name}_kmp{str(kmp4_weight).replace('.', '_')}"
                                    f"_{cost_scenario.name}"
                                ),
                                "decision_date": business_date.isoformat(),
                                "nomenclature_code": code,
                                "stage_profile": stage_name,
                                "kmp4_weight": str(kmp4_weight),
                                "holding_cost_scenario": cost_scenario.name,
                                "capital_annual_rate": str(
                                    cost_scenario.capital_annual_rate
                                ),
                                "storage_annual_rate": str(
                                    cost_scenario.storage_annual_rate
                                ),
                                "obsolescence_annual_rate": str(
                                    cost_scenario.obsolescence_annual_rate
                                ),
                                "status": lifecycle.status.value,
                                "forecast_rate": str(scenario_rate),
                                "kmp4_open_weighted_qty": str(weighted_kmp),
                                "selected_lead_time_days": selected_days,
                                "lead_time_basis": (
                                    "p75_economic_protection"
                                    if selected_days == lead_time.p75_days
                                    and safety.units > ZERO
                                    else "p50"
                                ),
                                "lead_time_confidence": lead_time.confidence,
                                "min_stock_qty": str(min_qty),
                                "max_stock_qty": str(max_qty),
                                "economic_safety_stock_qty": str(safety.units),
                                "recommended_order_qty_raw": str(raw),
                                "recommended_order_qty": str(recommended),
                                "decision": decision,
                                "inventory_position_qty": str(inventory_position),
                                "expected_saved_margin_rub": str(
                                    safety.expected_saved_margin_rub
                                ),
                                "carrying_cost_rub": str(safety.carrying_cost_rub),
                                "marginal_saved_margin_rub": str(
                                    safety.marginal_saved_margin_rub
                                ),
                                "marginal_carrying_cost_rub": str(
                                    safety.marginal_carrying_cost_rub
                                ),
                                "launch_profile_group_level": (
                                    launch_profile.group_level if launch_profile else ""
                                ),
                                "launch_profile_sample_count": (
                                    launch_profile.sample_count if launch_profile else 0
                                ),
                                "launch_profile_confidence": (
                                    launch_profile.confidence if launch_profile else ""
                                ),
                                "manual_review_reason": "|".join(reasons),
                            }
                        )

    reconciliations = reserves.reconciliations + placements.reconciliations
    input_keys = [
        (row["decision_date"], row["nomenclature_code"]) for row in decision_inputs
    ]
    scenario_keys = [
        (row["scenario_id"], row["decision_date"], row["nomenclature_code"])
        for row in scenario_decisions
    ]
    quality: list[dict[str, Any]] = []

    def add_quality(
        check: str,
        *,
        passed: bool,
        severity: str,
        value: Any,
        threshold: Any,
        note: str,
    ) -> None:
        quality.append(
            {
                "check": check,
                "status": "pass" if passed else "fail",
                "severity": severity,
                "value": value,
                "threshold": threshold,
                "note": note,
            }
        )

    add_quality(
        "decision_input_primary_key",
        passed=len(input_keys) == len(set(input_keys)),
        severity="critical",
        value=len(input_keys) - len(set(input_keys)),
        threshold=0,
        note="Одна строка на дату решения и SKU.",
    )
    add_quality(
        "scenario_decision_primary_key",
        passed=len(scenario_keys) == len(set(scenario_keys)),
        severity="critical",
        value=len(scenario_keys) - len(set(scenario_keys)),
        threshold=0,
        note="Одна строка на сценарий, дату решения и SKU.",
    )
    failed_reconciliations = sum(row["status"] != "pass" for row in reconciliations)
    add_quality(
        "register_month_boundary_reconciliation",
        passed=failed_reconciliations == 0,
        severity="critical",
        value=failed_reconciliations,
        threshold=0,
        note="Дневное восстановление должно сходиться с totals следующего месяца.",
    )
    negative_balances = (
        reserves.source_counts.get("negative_balance_rows", 0)
        + placements.source_counts.get("negative_balance_rows", 0)
    )
    add_quality(
        "negative_register_balances",
        passed=negative_balances == 0,
        severity="high",
        value=negative_balances,
        threshold=0,
        note="Отрицательные резервы/размещения требуют отдельного аудита.",
    )
    missing_economics = sum(
        _decimal(row["inventory_cost_per_unit_rub"]) <= ZERO for row in decision_inputs
    )
    add_quality(
        "unit_economics_coverage",
        passed=missing_economics == 0,
        severity="medium",
        value=missing_economics,
        threshold=0,
        note="Строки без себестоимости остаются manual_review без safety stock.",
    )
    fallback_lead = sum(
        row["lead_time_source_level"] == "fixed_fallback" for row in decision_inputs
    )
    add_quality(
        "adaptive_lead_time_coverage",
        passed=fallback_lead == 0,
        severity="medium",
        value=fallback_lead,
        threshold=0,
        note="Fallback разрешён, но явно понижает доверие и запрещает авто-заказ.",
    )
    if source_metadata:
        for key, value in sorted(source_metadata.items()):
            quality.append(
                {
                    "check": f"source_count_{key}",
                    "status": "info",
                    "severity": "low",
                    "value": value,
                    "threshold": "",
                    "note": "Контрольный объём источника.",
                }
            )
    status = (
        "PASS"
        if not any(
            row["status"] == "fail" and row["severity"] == "critical"
            for row in quality
        )
        else "FAIL"
    )
    return PreflightTables(
        decision_inputs=decision_inputs,
        scenario_decisions=scenario_decisions,
        lifecycle_daily=lifecycle_daily,
        daily_facts=daily_facts,
        source_quality=quality,
        reconciliations=reconciliations,
        status=status,
    )


def _columns(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                columns.append(key)
                seen.add(key)
    return columns


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = _columns(rows)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        if not columns:
            return
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_workbook(path: Path, sheets: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook(write_only=True)
    for title, rows in sheets.items():
        sheet = workbook.create_sheet(title=title[:31])
        columns = _columns(rows)
        if not columns:
            sheet.append(["Нет строк"])
            continue
        sheet.append(columns)
        for row in rows:
            sheet.append([row.get(column, "") for column in columns])
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:{_excel_column(len(columns))}1"
    workbook.save(path)


def _excel_column(number: int) -> str:
    value = max(1, number)
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_preflight_artifacts(
    output_dir: Path,
    *,
    tables: PreflightTables,
    date_from: date,
    date_to: date,
    history_start: date,
    config_path: Path,
    cohort_run_id: int,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    datasets = {
        "decision-inputs.csv": tables.decision_inputs,
        "scenario-decisions.csv": tables.scenario_decisions,
        "lifecycle-daily.csv": tables.lifecycle_daily,
        "daily-facts.csv": tables.daily_facts,
        "source-quality.csv": tables.source_quality,
        "reconciliations.csv": tables.reconciliations,
    }
    for filename, rows in datasets.items():
        write_csv(output_dir / filename, rows)
    write_workbook(
        output_dir / "backtest-preflight.xlsx",
        {
            "decision-inputs": tables.decision_inputs,
            "scenario-decisions": tables.scenario_decisions,
            "lifecycle-daily": tables.lifecycle_daily,
            "source-quality": tables.source_quality,
            "reconciliations": tables.reconciliations,
        },
    )
    file_hashes = {
        filename: _sha256(output_dir / filename) for filename in REQUIRED_PREFLIGHT_FILES
    }
    manifest = {
        "schema": PREFLIGHT_SCHEMA,
        "preflight_status": tables.status,
        "created_at": datetime.now().astimezone().isoformat(),
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "history_start": history_start.isoformat(),
        "classification_run_id": cohort_run_id,
        "scenario_config": str(config_path),
        "row_counts": {
            "decision_inputs": len(tables.decision_inputs),
            "scenario_decisions": len(tables.scenario_decisions),
            "lifecycle_daily": len(tables.lifecycle_daily),
            "daily_facts": len(tables.daily_facts),
            "source_quality": len(tables.source_quality),
            "reconciliations": len(tables.reconciliations),
        },
        "files": file_hashes,
    }
    (output_dir / "run-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def validate_preflight_directory(path: Path) -> dict[str, Any]:
    manifest_path = path / "run-manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"preflight manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("schema") != PREFLIGHT_SCHEMA:
        raise ValueError("unsupported preflight schema")
    if manifest.get("preflight_status") != "PASS":
        raise ValueError("preflight_status must be PASS")
    expected = manifest.get("files") or {}
    for filename in REQUIRED_PREFLIGHT_FILES:
        file_path = path / filename
        if not file_path.is_file():
            raise ValueError(f"preflight file is missing: {filename}")
        if expected.get(filename) != _sha256(file_path):
            raise ValueError(f"preflight checksum mismatch: {filename}")
    return manifest


__all__ = [
    "BacktestScenarioConfig",
    "CarryingCostScenario",
    "EconomicSafetyStock",
    "HistoricalUnitEconomicsEvent",
    "Kmp4QueueDay",
    "LeadTimeProfile",
    "PreflightTables",
    "RegisterHistory",
    "build_historical_incoming_by_day",
    "build_kmp4_queue_history",
    "build_preflight_tables",
    "calculate_economic_safety_stock",
    "fetch_kmp4_demand",
    "fetch_daily_unit_economics",
    "historical_demand_samples",
    "load_scenario_config",
    "reconstruct_historical_placements",
    "reconstruct_historical_reserves",
    "reconstruct_historical_stock",
    "select_lead_time_profile",
    "validate_preflight_directory",
    "write_preflight_artifacts",
]
