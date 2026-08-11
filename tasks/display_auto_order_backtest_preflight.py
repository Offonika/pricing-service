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
import shutil
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from openpyxl import Workbook
from sqlalchemy import bindparam, text

from app.services.assortment_lifecycle import AssortmentStatus
from app.services.procurement_order_formation import normalize_guid
from tasks.build_display_auto_order_dry_run import AutoOrderPolicy
from tasks.report_display_auto_order_six_month_backtest import (
    DEFAULT_LAUNCH_PROFILE_MIN_SAMPLES,
    DEFAULT_LEAD_TIME_DAYS,
    LaunchObservation,
    PipelineLot,
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
    "initial-pipeline.csv",
    "source-quality.csv",
    "reconciliations.csv",
    "site-events-raw.csv",
    "site-events-normalized.csv",
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
        return self.capital_annual_rate + self.storage_annual_rate + self.obsolescence_annual_rate


@dataclass(frozen=True)
class SiteSignalProfile:
    name: str
    order_weight: Decimal
    unordered_cart_weight: Decimal


@dataclass(frozen=True)
class BacktestScenarioConfig:
    kmp4_weights: tuple[Decimal, ...]
    kmp4_queue_days: int
    site_queue_days: int
    unordered_cart_daily_cap: Decimal
    site_signal_profiles: tuple[SiteSignalProfile, ...]
    holding_cost_scenarios: tuple[CarryingCostScenario, ...]
    lead_time_fallback_days: int
    lead_time_high_samples: int
    lead_time_medium_samples: int
    safety_max_units: int
    safety_lookback_days: int
    safety_step_days: int
    safety_min_samples: int
    quantity_tolerance: Decimal

    @property
    def site_signals_enabled(self) -> bool:
        return any(
            row.order_weight > ZERO or row.unordered_cart_weight > ZERO
            for row in self.site_signal_profiles
        )


@dataclass(frozen=True)
class Kmp4QueueDay:
    raw_qty: Decimal = ZERO
    matched_qty: Decimal = ZERO
    expired_qty: Decimal = ZERO
    open_qty: Decimal = ZERO
    reserve_increase_qty: Decimal = ZERO


@dataclass(frozen=True)
class DemandSignalQueueDay:
    kmp4_raw_qty: Decimal = ZERO
    kmp4_matched_qty: Decimal = ZERO
    kmp4_expired_qty: Decimal = ZERO
    kmp4_cancelled_qty: Decimal = ZERO
    kmp4_open_qty: Decimal = ZERO
    site_order_raw_qty: Decimal = ZERO
    site_order_matched_qty: Decimal = ZERO
    site_order_expired_qty: Decimal = ZERO
    site_order_cancelled_qty: Decimal = ZERO
    site_order_open_qty: Decimal = ZERO
    site_cart_raw_qty: Decimal = ZERO
    site_cart_matched_qty: Decimal = ZERO
    site_cart_expired_qty: Decimal = ZERO
    site_cart_cancelled_qty: Decimal = ZERO
    site_cart_open_qty: Decimal = ZERO
    site_cart_stock_blocked_qty: Decimal = ZERO
    site_soft_trigger_count: int = 0
    reserve_backlog_raw_qty: Decimal = ZERO
    reserve_backlog_matched_qty: Decimal = ZERO
    reserve_backlog_expired_qty: Decimal = ZERO
    reserve_backlog_cancelled_qty: Decimal = ZERO
    reserve_backlog_open_qty: Decimal = ZERO
    reserve_increase_qty: Decimal = ZERO
    raw_reserve_qty: Decimal = ZERO
    effective_reserve_qty: Decimal = ZERO
    reserve_backlog_qty: Decimal = ZERO

    @property
    def site_order_hidden_qty(self) -> Decimal:
        return self.site_order_expired_qty + self.site_order_cancelled_qty

    @property
    def site_cart_hidden_qty(self) -> Decimal:
        return self.site_cart_expired_qty

    @property
    def reserve_backlog_hidden_qty(self) -> Decimal:
        return self.reserve_backlog_expired_qty + self.reserve_backlog_cancelled_qty


@dataclass(frozen=True)
class SiteEventNormalization:
    rows: tuple[dict[str, Any], ...]
    mapping_stats: dict[str, Any]


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


@dataclass
class LeadTimeHistoryIndex:
    orders_by_code: dict[str, tuple[Mapping[str, Any], ...]]
    completed_by_sku_supplier: dict[
        tuple[str, str], tuple[tuple[Mapping[str, Any], int, date], ...]
    ]
    completed_by_group_supplier: dict[
        tuple[str, str], tuple[tuple[Mapping[str, Any], int, date], ...]
    ]
    completed_by_supplier: dict[str, tuple[tuple[Mapping[str, Any], int, date], ...]]
    completed_all: tuple[tuple[Mapping[str, Any], int, date], ...]
    candidate_cache: dict[tuple[str, str, date], tuple[tuple[Mapping[str, Any], int, date], ...]]


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
    initial_pipeline: list[dict[str, Any]]
    source_quality: list[dict[str, Any]]
    reconciliations: list[dict[str, Any]]
    site_events: list[dict[str, Any]]
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
    site_profiles = tuple(
        SiteSignalProfile(
            name=_clean(row.get("name")),
            order_weight=_decimal(row.get("order_weight")),
            unordered_cart_weight=_decimal(row.get("unordered_cart_weight")),
        )
        for row in payload.get("site_signal_profiles", ())
    )
    config = BacktestScenarioConfig(
        kmp4_weights=tuple(_decimal(value) for value in payload.get("kmp4_weights", ())),
        kmp4_queue_days=int(payload.get("kmp4_queue_days") or 0),
        site_queue_days=int(payload.get("site_queue_days") or 0),
        unordered_cart_daily_cap=_decimal(payload.get("unordered_cart_daily_cap")),
        site_signal_profiles=site_profiles,
        holding_cost_scenarios=holding,
        lead_time_fallback_days=int(lead_time.get("fallback_days") or DEFAULT_LEAD_TIME_DAYS),
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
    if config.site_queue_days <= 0 or config.site_queue_days != config.kmp4_queue_days:
        raise ValueError("site_queue_days must equal the positive common KMP4 queue window")
    if config.unordered_cart_daily_cap <= ZERO:
        raise ValueError("unordered_cart_daily_cap must be positive")
    if not config.site_signal_profiles:
        raise ValueError("site_signal_profiles must not be empty")
    if len({row.name for row in config.site_signal_profiles}) != len(
        config.site_signal_profiles
    ):
        raise ValueError("site signal profile names must be unique")
    if any(
        not row.name or row.order_weight < ZERO or row.unordered_cart_weight < ZERO
        for row in config.site_signal_profiles
    ):
        raise ValueError("site signal profiles must have names and non-negative weights")
    if not config.holding_cost_scenarios:
        raise ValueError("holding_cost_scenarios must not be empty")
    if any(row.total_annual_rate < ZERO for row in config.holding_cost_scenarios):
        raise ValueError("holding cost rates must be non-negative")
    return config


def fetch_kmp4_demand(
    engine: Any,
    *,
    codes: Sequence[str],
    product_refs: Mapping[str, str] | None = None,
    date_from: date,
    date_to: date,
) -> tuple[dict[str, dict[date, Decimal]], dict[str, int]]:
    """Read the daily KMP4 demand documents for configured demand counterparties."""

    result: dict[str, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    document_refs: set[str] = set()
    line_count = 0
    target_codes = set(codes)
    query = text("""
        WITH demand_counterparties AS (
            SELECT DISTINCT _Fld8857RRef AS counterparty_ref
            FROM dbo._Reference69 WITH (NOLOCK)
            WHERE _Fld8857RRef <> 0x00000000000000000000000000000000
        )
        SELECT
            CONVERT(varchar(34), doc._IDRRef, 1) AS document_ref,
            CAST(doc._Date_Time AS date) AS business_date,
            product.product_code,
            CAST(line._Fld2431 AS decimal(28, 3)) AS quantity
        FROM dbo._Document132 AS doc WITH (NOLOCK)
        JOIN demand_counterparties AS demand
          ON demand.counterparty_ref = doc._Fld2405RRef
        JOIN dbo._Document132_VT2427 AS line WITH (NOLOCK)
          ON line._Document132_IDRRef = doc._IDRRef
        JOIN #preflight_register_products AS product
          ON product.product_ref = line._Fld2434RRef
        WHERE doc._Marked = 0x00
          AND doc._Date_Time >= :date_from
          AND doc._Date_Time < :date_to
          AND line._Fld2431 > 0
        """)
    with engine.connect() as connection:
        _create_register_product_filter(connection, codes, product_refs=product_refs)
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


def fetch_daily_sales(
    engine: Any,
    *,
    codes: Sequence[str],
    product_refs: Mapping[str, str] | None = None,
    warehouse_codes: Sequence[str],
    date_from: date,
    date_to: date,
) -> dict[str, dict[date, Decimal]]:
    """Read display sales with product filtering applied by binary 1C reference."""

    query = text("""
        SELECT
            product.product_code,
            CAST(document._Date_Time AS date) AS business_date,
            CAST(SUM(line._Fld4971) AS decimal(28, 3)) AS sales_qty
        FROM dbo._Document203 AS document WITH (NOLOCK)
        JOIN dbo._Document203_VT4966 AS line WITH (NOLOCK)
          ON line._Document203_IDRRef = document._IDRRef
        JOIN #preflight_register_products AS product
          ON product.product_ref = line._Fld4974RRef
        JOIN #display_preflight_sales_warehouses AS warehouse
          ON warehouse.warehouse_ref = CASE
            WHEN line._Fld4983RRef <> 0x00000000000000000000000000000000
            THEN line._Fld4983RRef ELSE document._Fld4940RRef END
        WHERE document._Marked = 0x00
          AND document._Posted = 0x01
          AND document._Date_Time >= :date_from
          AND document._Date_Time < :date_to
          AND line._Fld4971 > 0
        GROUP BY product.product_code, CAST(document._Date_Time AS date)
        """)
    out: dict[str, dict[date, Decimal]] = defaultdict(dict)
    with engine.connect() as connection:
        _create_register_product_filter(connection, codes, product_refs=product_refs)
        connection.execute(
            text(
                "CREATE TABLE #display_preflight_sales_warehouses ("
                "warehouse_ref binary(16) NOT NULL PRIMARY KEY)"
            )
        )
        insert_warehouses = text(
            "INSERT INTO #display_preflight_sales_warehouses (warehouse_ref) "
            "SELECT DISTINCT warehouse._IDRRef "
            "FROM dbo._Reference80 AS warehouse WITH (NOLOCK) "
            "WHERE NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') IN :warehouses"
        ).bindparams(bindparam("warehouses", expanding=True))
        connection.execute(
            insert_warehouses,
            {"warehouses": tuple(sorted(set(warehouse_codes)))},
        )
        for row in connection.execute(
            query,
            {
                "date_from": datetime.combine(date_from, time.min),
                "date_to": datetime.combine(date_to + timedelta(days=1), time.min),
            },
        ).mappings():
            code = _clean(row.get("product_code"))
            business_date = _date(row.get("business_date"))
            if code and business_date is not None:
                out[code][business_date] = _decimal(row.get("sales_qty"))
    return dict(out)


def fetch_onec_product_refs(
    engine: Any,
    *,
    codes: Sequence[str],
) -> tuple[dict[str, str], dict[str, int]]:
    """Resolve current 1C binary references once from the stable SKU codes."""

    rows_by_code: dict[str, list[str]] = defaultdict(list)
    with engine.connect() as connection:
        connection.execute(
            text(
                "CREATE TABLE #display_preflight_codes ("
                "product_code nvarchar(100) NOT NULL PRIMARY KEY)"
            )
        )
        ordered_codes = sorted(set(codes))
        for offset in range(0, len(ordered_codes), 1000):
            code_batch = ordered_codes[offset : offset + 1000]
            values_sql = ", ".join(f"(:product_code_{index})" for index in range(len(code_batch)))
            connection.execute(
                text("INSERT INTO #display_preflight_codes (product_code) VALUES " + values_sql),
                {f"product_code_{index}": code for index, code in enumerate(code_batch)},
            )
        for row in connection.execute(
            text(
                "SELECT target.product_code, "
                "CONVERT(varchar(34), product._IDRRef, 1) AS product_ref "
                "FROM dbo._Reference62 AS product WITH (NOLOCK) "
                "JOIN #display_preflight_codes AS target "
                "ON target.product_code = LTRIM(RTRIM(product._Code))"
            )
        ).mappings():
            code = _clean(row.get("product_code"))
            ref = _clean(row.get("product_ref"))
            if code and ref:
                rows_by_code[code].append(ref)
    duplicate_codes = {code: refs for code, refs in rows_by_code.items() if len(set(refs)) != 1}
    resolved = {
        code: next(iter(set(refs))) for code, refs in rows_by_code.items() if len(set(refs)) == 1
    }
    return (
        resolved,
        {
            "requested_code_count": len(set(codes)),
            "resolved_code_count": len(resolved),
            "missing_code_count": len(set(codes) - set(rows_by_code)),
            "duplicate_code_count": len(duplicate_codes),
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

    query = text("""
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
        """).bindparams(bindparam("codes", expanding=True))
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
    product_refs: Mapping[str, str] | None,
    date_from: date,
    date_to: date,
) -> tuple[dict[date, dict[str, Decimal]], int]:
    query = text(f"""
        SELECT
            CAST(reg._Period AS date) AS period_month,
            product.product_code,
            CAST(SUM(reg.{quantity_field}) AS decimal(28, 3)) AS quantity
        FROM dbo.{table_name} AS reg WITH (NOLOCK)
        JOIN #preflight_register_products AS product
          ON product.product_ref = reg.{product_field}
        WHERE reg._Period >= :date_from
          AND reg._Period <= :date_to
        GROUP BY CAST(reg._Period AS date), product.product_code
        """)
    out: dict[date, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    row_count = 0
    with engine.connect() as connection:
        _create_register_product_filter(connection, codes, product_refs=product_refs)
        for row in connection.execute(
            query,
            {
                "date_from": datetime.combine(date_from, time.min),
                "date_to": datetime.combine(date_to, time.min),
            },
        ).mappings():
            period_month = _date(row.get("period_month"))
            code = _clean(row.get("product_code"))
            if period_month is not None and code:
                out[period_month][code] += _decimal(row.get("quantity"))
                row_count += 1
    return ({month: dict(rows) for month, rows in out.items()}, row_count)


def _create_register_product_filter(
    connection: Any,
    codes: Sequence[str],
    *,
    product_refs: Mapping[str, str] | None = None,
) -> None:
    del product_refs
    connection.execute(
        text(
            "CREATE TABLE #preflight_register_products ("
            "product_ref binary(16) NOT NULL PRIMARY KEY, "
            "product_code nvarchar(100) NOT NULL)"
        )
    )
    connection.execute(
        text(
            "CREATE TABLE #preflight_register_codes ("
            "product_code nvarchar(100) NOT NULL PRIMARY KEY)"
        )
    )
    ordered_codes = sorted(set(codes))
    for offset in range(0, len(ordered_codes), 1000):
        code_batch = ordered_codes[offset : offset + 1000]
        values_sql = ", ".join(f"(:product_code_{index})" for index in range(len(code_batch)))
        connection.execute(
            text("INSERT INTO #preflight_register_codes (product_code) VALUES " + values_sql),
            {f"product_code_{index}": code for index, code in enumerate(code_batch)},
        )
    connection.execute(
        text(
            "INSERT INTO #preflight_register_products (product_ref, product_code) "
            "SELECT product._IDRRef, target.product_code "
            "FROM dbo._Reference62 AS product WITH (NOLOCK) "
            "JOIN #preflight_register_codes AS target "
            "ON target.product_code = LTRIM(RTRIM(product._Code))"
        )
    )


def _fetch_register_movements(
    engine: Any,
    *,
    table_name: str,
    product_field: str,
    quantity_field: str,
    codes: Sequence[str],
    product_refs: Mapping[str, str] | None,
    date_from: date,
    date_to: date,
) -> tuple[dict[date, dict[str, Decimal]], int]:
    query = text(f"""
        SELECT
            CAST(reg._Period AS date) AS business_date,
            product.product_code,
            CAST(SUM(
                CASE WHEN reg._RecordKind = 0
                     THEN reg.{quantity_field} ELSE -reg.{quantity_field} END
            ) AS decimal(28, 3)) AS quantity_delta
        FROM dbo.{table_name} AS reg WITH (NOLOCK)
        JOIN #preflight_register_products AS product
          ON product.product_ref = reg.{product_field}
        WHERE reg._Active = 0x01
          AND reg._Period >= :date_from
          AND reg._Period < :date_to
        GROUP BY CAST(reg._Period AS date), product.product_code
        """)
    out: dict[date, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    row_count = 0
    with engine.connect() as connection:
        _create_register_product_filter(connection, codes, product_refs=product_refs)
        for row in connection.execute(
            query,
            {
                "date_from": datetime.combine(date_from, time.min),
                "date_to": datetime.combine(date_to + timedelta(days=1), time.min),
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
    product_refs: Mapping[str, str] | None = None,
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
        product_refs=product_refs,
        date_from=first_month,
        date_to=last_opening,
    )
    all_movements, movement_rows = _fetch_register_movements(
        engine,
        table_name=movements_table,
        product_field=product_field,
        quantity_field=quantity_field,
        codes=codes,
        product_refs=product_refs,
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
            code: balance.get(code, ZERO) - next_opening.get(code, ZERO) for code in all_codes
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
    product_refs: Mapping[str, str] | None = None,
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
        product_refs=product_refs,
        date_from=date_from,
        date_to=date_to,
        tolerance=tolerance,
    )


def reconstruct_historical_placements(
    engine: Any,
    *,
    codes: Sequence[str],
    product_refs: Mapping[str, str] | None = None,
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
        product_refs=product_refs,
        date_from=date_from,
        date_to=date_to,
        tolerance=tolerance,
    )


def reconstruct_historical_stock(
    engine: Any,
    *,
    codes: Sequence[str],
    product_refs: Mapping[str, str] | None = None,
    network_warehouse_codes: Sequence[str],
    physical_warehouse_codes: Sequence[str],
    date_from: date,
    date_to: date,
) -> tuple[dict[date, dict[str, Decimal]], dict[str, set[date]], dict[str, int]]:
    """Rebuild display stock with filtering performed inside SQL Server."""

    opening_query = text("""
        SELECT
            CAST(stock._Period AS date) AS period_month,
            product.product_code,
            warehouse.warehouse_code,
            CAST(SUM(stock._Fld7743) AS decimal(28, 3)) AS quantity
        FROM dbo._AccumRgT7745 AS stock WITH (NOLOCK)
        JOIN #preflight_register_products AS product
          ON product.product_ref = stock._Fld7738RRef
        JOIN #display_preflight_warehouses AS warehouse
          ON warehouse.warehouse_ref = stock._Fld7742RRef
        WHERE stock._Period >= :date_from
          AND stock._Period <= :date_to
        GROUP BY
            CAST(stock._Period AS date), product.product_code,
            warehouse.warehouse_code
        """)
    movement_query = text("""
        SELECT
            CAST(movement._Period AS date) AS business_date,
            product.product_code,
            warehouse.warehouse_code,
            CAST(SUM(
                CASE WHEN movement._RecordKind = 0
                     THEN movement._Fld7743 ELSE -movement._Fld7743 END
            ) AS decimal(28, 3)) AS quantity_delta
        FROM dbo._AccumRg7735 AS movement WITH (NOLOCK)
        JOIN #preflight_register_products AS product
          ON product.product_ref = movement._Fld7738RRef
        JOIN #display_preflight_warehouses AS warehouse
          ON warehouse.warehouse_ref = movement._Fld7742RRef
        WHERE movement._Active = 0x01
          AND movement._Period >= :date_from
          AND movement._Period < :date_to
        GROUP BY
            CAST(movement._Period AS date), product.product_code,
            warehouse.warehouse_code
        """)
    warehouses = tuple(sorted(set(network_warehouse_codes)))
    if not warehouses:
        raise ValueError("network warehouse list must not be empty")
    first_month = _month_start(date_from)
    final_month = _month_start(date_to)
    openings: dict[date, dict[tuple[str, str], Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    movements: dict[date, dict[tuple[str, str], Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    opening_rows = 0
    movement_rows = 0
    with engine.connect() as connection:
        _create_register_product_filter(connection, codes, product_refs=product_refs)
        connection.execute(
            text(
                "CREATE TABLE #display_preflight_warehouses ("
                "warehouse_ref binary(16) NOT NULL PRIMARY KEY, "
                "warehouse_code nvarchar(100) NOT NULL)"
            )
        )
        insert_warehouses = text(
            "INSERT INTO #display_preflight_warehouses "
            "(warehouse_ref, warehouse_code) "
            "SELECT DISTINCT warehouse._IDRRef, LTRIM(RTRIM(warehouse._Code)) "
            "FROM dbo._Reference80 AS warehouse WITH (NOLOCK) "
            "WHERE NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') IN :warehouses"
        ).bindparams(bindparam("warehouses", expanding=True))
        connection.execute(insert_warehouses, {"warehouses": warehouses})
        for row in connection.execute(
            opening_query,
            {
                "date_from": datetime.combine(first_month, time.min),
                "date_to": datetime.combine(final_month, time.min),
            },
        ).mappings():
            period_month = _date(row.get("period_month"))
            code = _clean(row.get("product_code"))
            warehouse = _clean(row.get("warehouse_code"))
            if period_month is not None and code and warehouse:
                openings[period_month][(code, warehouse)] += _decimal(row.get("quantity"))
                opening_rows += 1
        for row in connection.execute(
            movement_query,
            {
                "date_from": datetime.combine(first_month, time.min),
                "date_to": datetime.combine(date_to + timedelta(days=1), time.min),
            },
        ).mappings():
            business_date = _date(row.get("business_date"))
            code = _clean(row.get("product_code"))
            warehouse = _clean(row.get("warehouse_code"))
            if business_date is not None and code and warehouse:
                movements[business_date][(code, warehouse)] += _decimal(row.get("quantity_delta"))
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
                if any(key[1] in physical_codes and balances[key] > ZERO for key in keys):
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
            result[business_date] = {code: qty for code, qty in balances.items() if qty > ZERO}
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


def normalize_site_event_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    product_refs: Mapping[str, str],
    cohort_codes: Sequence[str],
    unordered_cart_daily_cap: Decimal,
) -> SiteEventNormalization:
    """Resolve site XML_ID values to 1C SKU codes and cap repeated cart intent."""

    cohort = set(cohort_codes)
    codes_by_guid: dict[str, list[str]] = defaultdict(list)
    for code, product_ref in product_refs.items():
        try:
            guid = str(UUID(normalize_guid(product_ref)))
        except (ValueError, AttributeError):
            continue
        codes_by_guid[guid].append(code)

    stats: dict[str, Any] = defaultdict(int)
    stats["raw_row_count"] = len(rows)
    normalized: list[dict[str, Any]] = []
    cart_groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    supported = {
        "site_order",
        "site_unordered_cart",
        "site_view",
        "site_search",
        "site_click",
    }
    for source_row in rows:
        event_type = _clean(source_row.get("event_type"))
        xml_id = _clean(source_row.get("product_xml_id"))
        event_date = _date(source_row.get("event_date"))
        quantity = max(ZERO, _decimal(source_row.get("quantity")))
        session_key = _clean(source_row.get("session_key"))
        delay_flag = "Y" if _clean(source_row.get("delay_flag")).upper() == "Y" else "N"
        mapping_status = "matched"
        guid = ""
        code = ""
        if not xml_id:
            mapping_status = "missing_xml_id"
            stats["missing_xml_id_count"] += 1
        else:
            try:
                guid = str(UUID(normalize_guid(xml_id)))
            except (ValueError, AttributeError):
                mapping_status = "invalid_xml_id"
                stats["invalid_xml_id_count"] += 1
            else:
                matched_codes = codes_by_guid.get(guid, ())
                if len(matched_codes) > 1:
                    mapping_status = "ambiguous_guid"
                    stats["ambiguous_guid_count"] += 1
                elif not matched_codes or matched_codes[0] not in cohort:
                    mapping_status = "out_of_cohort"
                    stats["out_of_cohort_count"] += 1
                else:
                    code = matched_codes[0]
                    stats["mapped_row_count"] += 1
        if event_type not in supported:
            stats["unsupported_event_type_count"] += 1
        if event_date is None:
            stats["invalid_event_date_count"] += 1
        cancellation_date = _date(source_row.get("cancelled_at"))
        if cancellation_date is not None and event_date is not None and cancellation_date < event_date:
            stats["invalid_cancellation_date_count"] += 1
        base = {
            "event_date": event_date.isoformat() if event_date else "",
            "event_type": event_type,
            "product_xml_id": xml_id,
            "normalized_product_guid": guid,
            "nomenclature_code": code,
            "raw_quantity": str(quantity),
            "quantity": str(quantity),
            "order_number": _clean(source_row.get("order_number")),
            "cancelled_at": cancellation_date.isoformat() if cancellation_date else "",
            "session_key": session_key,
            "event_key": _clean(source_row.get("event_key")),
            "delay_flag": delay_flag,
            "mapping_status": mapping_status,
            "manual_review_only": int(
                delay_flag == "Y" or event_type in {"site_view", "site_search", "site_click"}
            ),
        }
        if event_type == "site_unordered_cart" and mapping_status == "matched" and event_date:
            key = (event_date.isoformat(), code, session_key, delay_flag)
            current = cart_groups.get(key)
            if current is None:
                cart_groups[key] = base
            else:
                current["raw_quantity"] = str(
                    _decimal(current.get("raw_quantity")) + quantity
                )
                stats["cart_deduplicated_row_count"] += 1
            continue
        normalized.append(base)

    for row in cart_groups.values():
        raw = _decimal(row["raw_quantity"])
        row["quantity"] = str(
            ZERO
            if row["manual_review_only"]
            else min(unordered_cart_daily_cap, raw)
        )
        if raw > _decimal(row["quantity"]):
            stats["cart_capped_quantity"] += raw - _decimal(row["quantity"])
        normalized.append(row)

    normalized.sort(
        key=lambda row: (
            row["event_date"],
            row["event_type"],
            row["nomenclature_code"],
            row["event_key"],
        )
    )
    mapped_quantitative = [
        row
        for row in normalized
        if row["mapping_status"] == "matched" and not row["manual_review_only"]
    ]
    stats["normalized_row_count"] = len(normalized)
    stats["site_order_quantity"] = sum(
        (_decimal(row["quantity"]) for row in mapped_quantitative if row["event_type"] == "site_order"),
        ZERO,
    )
    stats["site_cart_quantity_before_stock_filter"] = sum(
        (
            _decimal(row["quantity"])
            for row in mapped_quantitative
            if row["event_type"] == "site_unordered_cart"
        ),
        ZERO,
    )
    stats["soft_trigger_row_count"] = sum(row["manual_review_only"] for row in normalized)
    daily_qty: dict[date, Decimal] = defaultdict(Decimal)
    for row in mapped_quantitative:
        business_date = _date(row["event_date"])
        if business_date:
            daily_qty[business_date] += _decimal(row["quantity"])
    stats["daily_volume_max"] = max(daily_qty.values(), default=ZERO)
    stats["july_quantity"] = sum(
        (qty for business_date, qty in daily_qty.items() if business_date.month == 7), ZERO
    )
    stats["pre_july_quantity"] = sum(
        (qty for business_date, qty in daily_qty.items() if business_date.month != 7), ZERO
    )
    return SiteEventNormalization(rows=tuple(normalized), mapping_stats=dict(stats))


def _consume_signal_queue(
    queue: list[dict[str, Any]],
    amount: Decimal,
    *,
    source: str | None = None,
    event_key: str | None = None,
) -> dict[str, Decimal]:
    consumed: dict[str, Decimal] = defaultdict(Decimal)
    remaining = max(ZERO, amount)
    for entry in queue:
        if remaining <= ZERO:
            break
        if source is not None and entry["source"] != source:
            continue
        if event_key is not None and entry["event_key"] != event_key:
            continue
        used = min(entry["quantity"], remaining)
        entry["quantity"] -= used
        remaining -= used
        consumed[entry["source"]] += used
    queue[:] = [entry for entry in queue if entry["quantity"] > ZERO]
    return dict(consumed)


def build_demand_signal_queue_history(
    *,
    codes: Sequence[str],
    kmp4_raw_by_code: Mapping[str, Mapping[date, Decimal]],
    site_event_rows: Sequence[Mapping[str, Any]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    reserves_by_day: Mapping[date, Mapping[str, Decimal]],
    stock_by_day: Mapping[date, Mapping[str, Decimal]],
    date_from: date,
    date_to: date,
    queue_days: int,
) -> dict[str, dict[date, DemandSignalQueueDay]]:
    """Build one FIFO queue so one sale/reserve cannot close two demand signals."""

    events_by_code_date: dict[tuple[str, date], list[Mapping[str, Any]]] = defaultdict(list)
    cancellations_by_code_date: dict[tuple[str, date], list[Mapping[str, Any]]] = defaultdict(list)
    for row in site_event_rows:
        code = _clean(row.get("nomenclature_code"))
        event_date = _date(row.get("event_date"))
        if code and event_date and _clean(row.get("mapping_status")) == "matched":
            events_by_code_date[(code, event_date)].append(row)
        cancellation_date = _date(row.get("cancelled_at"))
        if code and cancellation_date and _clean(row.get("event_type")) == "site_order":
            cancellations_by_code_date[(code, cancellation_date)].append(row)

    result: dict[str, dict[date, DemandSignalQueueDay]] = defaultdict(dict)
    for code in codes:
        queue: list[dict[str, Any]] = []
        previous_effective_reserve = ZERO
        previous_backlog = ZERO

        for business_date in _daterange(date_from, date_to):
            raw: dict[str, Decimal] = defaultdict(Decimal)
            matched: dict[str, Decimal] = defaultdict(Decimal)
            expired: dict[str, Decimal] = defaultdict(Decimal)
            cancelled: dict[str, Decimal] = defaultdict(Decimal)
            soft_trigger_count = 0
            cart_stock_blocked = ZERO

            active: list[dict[str, Any]] = []
            for entry in queue:
                if (business_date - entry["created_at"]).days >= queue_days:
                    expired[entry["source"]] += entry["quantity"]
                else:
                    active.append(entry)
            queue = active

            kmp4_raw = max(
                ZERO, _decimal(kmp4_raw_by_code.get(code, {}).get(business_date))
            )
            if kmp4_raw > ZERO:
                raw["kmp4"] += kmp4_raw
                queue.append(
                    {
                        "created_at": business_date,
                        "source": "kmp4",
                        "quantity": kmp4_raw,
                        "event_key": "",
                    }
                )

            raw_reserve = _decimal(reserves_by_day.get(business_date, {}).get(code))
            effective_reserve = max(ZERO, raw_reserve)
            physical_stock = max(ZERO, _decimal(stock_by_day.get(business_date, {}).get(code)))
            backlog = max(ZERO, effective_reserve - physical_stock)
            reserve_increase = max(ZERO, effective_reserve - previous_effective_reserve)
            backlog_increase = max(ZERO, backlog - previous_backlog)
            backlog_decrease = max(ZERO, previous_backlog - backlog)

            for event in events_by_code_date.get((code, business_date), ()):
                event_type = _clean(event.get("event_type"))
                if int(_decimal(event.get("manual_review_only"))):
                    soft_trigger_count += 1
                    continue
                quantity = max(ZERO, _decimal(event.get("quantity")))
                source = ""
                if event_type == "site_order":
                    source = "site_order"
                elif event_type == "site_unordered_cart":
                    if physical_stock - effective_reserve > ZERO:
                        cart_stock_blocked += quantity
                        continue
                    source = "site_cart"
                if source and quantity > ZERO:
                    raw[source] += quantity
                    queue.append(
                        {
                            "created_at": business_date,
                            "source": source,
                            "quantity": quantity,
                            "event_key": _clean(event.get("event_key")),
                        }
                    )

            sale_qty = max(ZERO, _decimal(sales_by_code.get(code, {}).get(business_date)))
            sale_remaining = sale_qty
            if backlog_decrease > ZERO and sale_remaining > ZERO:
                targeted = _consume_signal_queue(
                    queue,
                    min(backlog_decrease, sale_remaining), source="reserve_backlog"
                )
                targeted_qty = sum(targeted.values(), ZERO)
                for source, qty in targeted.items():
                    matched[source] += qty
                sale_remaining = max(ZERO, sale_remaining - targeted_qty)

            realization_capacity = max(sale_remaining, reserve_increase)
            general_matches = _consume_signal_queue(queue, realization_capacity)
            general_matched_qty = sum(general_matches.values(), ZERO)
            for source, qty in general_matches.items():
                matched[source] += qty

            confirmed_new_backlog = max(ZERO, backlog_increase - general_matched_qty)
            if confirmed_new_backlog > ZERO:
                raw["reserve_backlog"] += confirmed_new_backlog
                queue.append(
                    {
                        "created_at": business_date,
                        "source": "reserve_backlog",
                        "quantity": confirmed_new_backlog,
                        "event_key": "",
                    }
                )

            for event in cancellations_by_code_date.get((code, business_date), ()):
                cancelled_rows = _consume_signal_queue(
                    queue,
                    Decimal("1E30"),
                    source="site_order",
                    event_key=_clean(event.get("event_key")),
                )
                for source, qty in cancelled_rows.items():
                    cancelled[source] += qty

            backlog_cancel_capacity = max(
                ZERO,
                backlog_decrease - min(backlog_decrease, sale_qty),
            )
            if backlog_cancel_capacity > ZERO:
                cancelled_rows = _consume_signal_queue(
                    queue, backlog_cancel_capacity, source="reserve_backlog"
                )
                for source, qty in cancelled_rows.items():
                    cancelled[source] += qty

            open_by_source: dict[str, Decimal] = defaultdict(Decimal)
            for entry in queue:
                open_by_source[entry["source"]] += entry["quantity"]
            result[code][business_date] = DemandSignalQueueDay(
                kmp4_raw_qty=raw["kmp4"],
                kmp4_matched_qty=matched["kmp4"],
                kmp4_expired_qty=expired["kmp4"],
                kmp4_cancelled_qty=cancelled["kmp4"],
                kmp4_open_qty=open_by_source["kmp4"],
                site_order_raw_qty=raw["site_order"],
                site_order_matched_qty=matched["site_order"],
                site_order_expired_qty=expired["site_order"],
                site_order_cancelled_qty=cancelled["site_order"],
                site_order_open_qty=open_by_source["site_order"],
                site_cart_raw_qty=raw["site_cart"],
                site_cart_matched_qty=matched["site_cart"],
                site_cart_expired_qty=expired["site_cart"],
                site_cart_cancelled_qty=cancelled["site_cart"],
                site_cart_open_qty=open_by_source["site_cart"],
                site_cart_stock_blocked_qty=cart_stock_blocked,
                site_soft_trigger_count=soft_trigger_count,
                reserve_backlog_raw_qty=raw["reserve_backlog"],
                reserve_backlog_matched_qty=matched["reserve_backlog"],
                reserve_backlog_expired_qty=expired["reserve_backlog"],
                reserve_backlog_cancelled_qty=cancelled["reserve_backlog"],
                reserve_backlog_open_qty=open_by_source["reserve_backlog"],
                reserve_increase_qty=reserve_increase,
                raw_reserve_qty=raw_reserve,
                effective_reserve_qty=effective_reserve,
                reserve_backlog_qty=backlog,
            )
            previous_effective_reserve = effective_reserve
            previous_backlog = backlog
    return {code: dict(rows) for code, rows in result.items()}


def _nearest_rank(values: Sequence[int], quantile: Decimal) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, int(_ceil(quantile * Decimal(len(ordered)))))
    return int(ordered[rank - 1])


def latest_supplier_identity(
    detail_rows: Sequence[Mapping[str, Any]] | LeadTimeHistoryIndex,
    *,
    code: str,
    as_of: date,
) -> tuple[str, str]:
    if isinstance(detail_rows, LeadTimeHistoryIndex):
        rows = detail_rows.orders_by_code.get(code, ())
    else:
        rows = detail_rows
    candidates = []
    for row in rows:
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


def build_lead_time_history_index(
    detail_rows: Sequence[Mapping[str, Any]],
) -> LeadTimeHistoryIndex:
    orders_by_code: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_sku_supplier: dict[tuple[str, str], list[tuple[Mapping[str, Any], int, date]]] = defaultdict(
        list
    )
    by_group_supplier: dict[tuple[str, str], list[tuple[Mapping[str, Any], int, date]]] = (
        defaultdict(list)
    )
    by_supplier: dict[str, list[tuple[Mapping[str, Any], int, date]]] = defaultdict(list)
    completed: list[tuple[Mapping[str, Any], int, date]] = []
    for row in detail_rows:
        code = _clean(row.get("nomenclature_code"))
        supplier = _clean(row.get("supplier_ref"))
        if code:
            orders_by_code[code].append(row)
        receipt_at = _date(row.get("warehouse_receipt_at"))
        days = int(_decimal(row.get("total_arrival_days")))
        if receipt_at is None or days < 0:
            continue
        item = (row, days, receipt_at)
        completed.append(item)
        if code and supplier:
            by_sku_supplier[(code, supplier)].append(item)
        group_key = _clean(row.get("display_group_key"))
        if group_key and supplier:
            by_group_supplier[(group_key, supplier)].append(item)
        if supplier:
            by_supplier[supplier].append(item)
    return LeadTimeHistoryIndex(
        orders_by_code={
            code: tuple(
                sorted(
                    rows, key=lambda row: _date(row.get("supplier_order_created_at")) or date.min
                )
            )
            for code, rows in orders_by_code.items()
        },
        completed_by_sku_supplier={key: tuple(rows) for key, rows in by_sku_supplier.items()},
        completed_by_group_supplier={key: tuple(rows) for key, rows in by_group_supplier.items()},
        completed_by_supplier={key: tuple(rows) for key, rows in by_supplier.items()},
        completed_all=tuple(completed),
        candidate_cache={},
    )


def _lead_time_candidates_before(
    index: LeadTimeHistoryIndex,
    *,
    level: str,
    key: str,
    rows: Sequence[tuple[Mapping[str, Any], int, date]],
    as_of: date,
) -> tuple[tuple[Mapping[str, Any], int, date], ...]:
    cache_key = (level, key, as_of)
    cached = index.candidate_cache.get(cache_key)
    if cached is None:
        cached = tuple(item for item in rows if item[2] < as_of)
        index.candidate_cache[cache_key] = cached
    return cached


def select_lead_time_profile(
    detail_rows: Sequence[Mapping[str, Any]] | LeadTimeHistoryIndex,
    *,
    code: str,
    group_key: str,
    supplier_ref: str,
    supplier_name: str,
    as_of: date,
    config: BacktestScenarioConfig,
) -> LeadTimeProfile:
    index = (
        detail_rows
        if isinstance(detail_rows, LeadTimeHistoryIndex)
        else build_lead_time_history_index(detail_rows)
    )
    levels = (
        (
            "sku_supplier",
            f"{code}|{supplier_ref}",
            index.completed_by_sku_supplier.get((code, supplier_ref), ()) if supplier_ref else (),
        ),
        (
            "display_group_supplier",
            f"{group_key}|{supplier_ref}",
            (
                index.completed_by_group_supplier.get((group_key, supplier_ref), ())
                if supplier_ref
                else ()
            ),
        ),
        (
            "supplier",
            supplier_ref,
            index.completed_by_supplier.get(supplier_ref, ()) if supplier_ref else (),
        ),
        ("all_displays", "all", index.completed_all),
    )
    for level, key, candidate_rows in levels:
        rows = _lead_time_candidates_before(
            index,
            level=level,
            key=key,
            rows=candidate_rows,
            as_of=as_of,
        )
        if len(rows) < config.lead_time_medium_samples:
            continue
        values = [item[1] for item in rows]
        confidence = "high" if len(rows) >= config.lead_time_high_samples else "medium"
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
        probability = Decimal(sum(sample > threshold for sample in demand_samples)) / Decimal(
            len(demand_samples)
        )
        marginal_saved = gross_margin_per_unit_rub * probability
        if marginal_saved <= unit_cost:
            return EconomicSafetyStock(
                Decimal(units), saved_total, cost_total, marginal_saved, unit_cost
            )
        units += 1
        saved_total += marginal_saved
        cost_total += unit_cost
    return EconomicSafetyStock(Decimal(units), saved_total, cost_total, marginal_saved, unit_cost)


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
            if isinstance(event, HistoricalUnitEconomicsEvent) and event.business_date <= as_of
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
    initial_pipeline_by_code: Mapping[str, Sequence[PipelineLot]],
    kmp4_raw_by_code: Mapping[str, Mapping[date, Decimal]],
    site_event_rows: Sequence[Mapping[str, Any]],
    site_mapping_stats: Mapping[str, Any],
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
    signal_queue = build_demand_signal_queue_history(
        codes=codes,
        kmp4_raw_by_code=kmp4_raw_by_code,
        site_event_rows=site_event_rows,
        sales_by_code=sales_by_code,
        reserves_by_day=reserves.by_day,
        stock_by_day=stock_by_day,
        date_from=max(history_start, date_from - timedelta(days=config.kmp4_queue_days)),
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
    cadence = max(1, policy.order_cadence_days)
    scenario_decisions: list[dict[str, Any]] = [
        {
            "scenario_id": "legacy",
            "row_kind": "scenario_definition",
            "stage_profile": "legacy",
            "kmp4_weight": "0",
            "site_profile": "off",
            "site_order_weight": "0",
            "site_unordered_cart_weight": "0",
            "holding_cost_scenario": "legacy_excluded",
            "lead_time_rule": "fixed_legacy",
            "lead_time_days": DEFAULT_LEAD_TIME_DAYS,
        }
    ]
    for stage_name in ("conservative", "typical", "service"):
        for kmp4_weight in config.kmp4_weights:
            for site_profile in config.site_signal_profiles:
                for cost_scenario in config.holding_cost_scenarios:
                    scenario_decisions.append(
                        {
                            "scenario_id": (
                                f"{stage_name}_kmp{str(kmp4_weight).replace('.', '_')}"
                                f"_site{site_profile.name}_{cost_scenario.name}"
                            ),
                            "row_kind": "scenario_definition",
                            "stage_profile": stage_name,
                            "kmp4_weight": str(kmp4_weight),
                            "site_profile": site_profile.name,
                            "site_order_weight": str(site_profile.order_weight),
                            "site_unordered_cart_weight": str(
                                site_profile.unordered_cart_weight
                            ),
                            "holding_cost_scenario": cost_scenario.name,
                            "capital_annual_rate": str(cost_scenario.capital_annual_rate),
                            "storage_annual_rate": str(cost_scenario.storage_annual_rate),
                            "obsolescence_annual_rate": str(
                                cost_scenario.obsolescence_annual_rate
                            ),
                            "lead_time_rule": "p50_then_p75_economic_protection",
                            "review_cadence_days": cadence,
                        }
                    )
    lead_time_index = build_lead_time_history_index(lead_time_detail_rows)
    initial_pipeline = [
        {
            "nomenclature_code": code,
            "arrival_at": lot.arrival_at.isoformat(),
            "quantity": str(lot.qty),
            "source": lot.source,
        }
        for code, lots in sorted(initial_pipeline_by_code.items())
        for lot in lots
        if lot.qty > ZERO
    ]

    for business_date in _daterange(date_from, date_to):
        scheduled_review = (business_date - date_from).days % cadence == 0
        launch_snapshot = build_launch_profile_snapshot(launch_observations, as_of=business_date)
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
            queue_day = signal_queue.get(code, {}).get(
                business_date, DemandSignalQueueDay()
            )
            event_review = trend == "accelerating" or any(
                (
                    queue_day.kmp4_raw_qty > ZERO,
                    queue_day.site_order_raw_qty > ZERO,
                    queue_day.site_cart_raw_qty > ZERO,
                    queue_day.reserve_backlog_raw_qty > ZERO,
                    queue_day.site_soft_trigger_count > 0,
                )
            )
            physical_stock = _decimal(stock_by_day.get(business_date, {}).get(code))
            raw_reserve = queue_day.raw_reserve_qty
            effective_reserve = queue_day.effective_reserve_qty
            reserve_backlog = queue_day.reserve_backlog_qty
            gross_incoming = _decimal(incoming_by_day.get(business_date, {}).get(code))
            placed_incoming = _decimal(placements.by_day.get(business_date, {}).get(code))
            free_incoming = max(ZERO, gross_incoming - placed_incoming)
            free_stock = physical_stock - effective_reserve
            inventory_position = free_stock + free_incoming
            daily_facts.append(
                {
                    "business_date": business_date.isoformat(),
                    "nomenclature_code": code,
                    "observed_sales_qty": str(
                        _decimal(sales_by_code.get(code, {}).get(business_date))
                    ),
                    "physical_stock_qty": str(physical_stock),
                    "raw_reserve_qty": str(raw_reserve),
                    "effective_reserve_qty": str(effective_reserve),
                    "reserve_qty": str(effective_reserve),
                    "reserve_backlog_qty": str(reserve_backlog),
                    "gross_incoming_qty": str(gross_incoming),
                    "placed_incoming_qty": str(placed_incoming),
                    "free_incoming_qty": str(free_incoming),
                    "kmp4_raw_qty": str(queue_day.kmp4_raw_qty),
                    "kmp4_matched_qty": str(queue_day.kmp4_matched_qty),
                    "kmp4_expired_qty": str(queue_day.kmp4_expired_qty),
                    "kmp4_cancelled_qty": str(queue_day.kmp4_cancelled_qty),
                    "kmp4_open_qty": str(queue_day.kmp4_open_qty),
                    "site_order_raw_qty": str(queue_day.site_order_raw_qty),
                    "site_order_matched_qty": str(queue_day.site_order_matched_qty),
                    "site_order_expired_qty": str(queue_day.site_order_expired_qty),
                    "site_order_cancelled_qty": str(queue_day.site_order_cancelled_qty),
                    "site_order_hidden_qty": str(queue_day.site_order_hidden_qty),
                    "site_order_open_qty": str(queue_day.site_order_open_qty),
                    "site_cart_raw_qty": str(queue_day.site_cart_raw_qty),
                    "site_cart_matched_qty": str(queue_day.site_cart_matched_qty),
                    "site_cart_expired_qty": str(queue_day.site_cart_expired_qty),
                    "site_cart_cancelled_qty": str(queue_day.site_cart_cancelled_qty),
                    "site_cart_hidden_qty": str(queue_day.site_cart_hidden_qty),
                    "site_cart_open_qty": str(queue_day.site_cart_open_qty),
                    "site_cart_stock_blocked_qty": str(
                        queue_day.site_cart_stock_blocked_qty
                    ),
                    "site_soft_trigger_count": queue_day.site_soft_trigger_count,
                    "reserve_backlog_raw_qty": str(queue_day.reserve_backlog_raw_qty),
                    "reserve_backlog_matched_qty": str(
                        queue_day.reserve_backlog_matched_qty
                    ),
                    "reserve_backlog_expired_qty": str(
                        queue_day.reserve_backlog_expired_qty
                    ),
                    "reserve_backlog_cancelled_qty": str(
                        queue_day.reserve_backlog_cancelled_qty
                    ),
                    "reserve_backlog_hidden_qty": str(
                        queue_day.reserve_backlog_hidden_qty
                    ),
                    "reserve_backlog_open_qty": str(queue_day.reserve_backlog_open_qty),
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
            supplier_ref, supplier_name = latest_supplier_identity(
                lead_time_index, code=code, as_of=business_date
            )
            group_key = display_group_key(
                {"name": _clean(item.get("name")), "nomenclature_code": code}
            )
            lead_time = select_lead_time_profile(
                lead_time_index,
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
                "kmp4_raw_qty": str(queue_day.kmp4_raw_qty),
                "kmp4_matched_qty": str(queue_day.kmp4_matched_qty),
                "kmp4_expired_qty": str(queue_day.kmp4_expired_qty),
                "kmp4_open_qty": str(queue_day.kmp4_open_qty),
                "site_order_raw_qty": str(queue_day.site_order_raw_qty),
                "site_order_matched_qty": str(queue_day.site_order_matched_qty),
                "site_order_expired_qty": str(queue_day.site_order_expired_qty),
                "site_order_cancelled_qty": str(queue_day.site_order_cancelled_qty),
                "site_order_open_qty": str(queue_day.site_order_open_qty),
                "site_cart_raw_qty": str(queue_day.site_cart_raw_qty),
                "site_cart_matched_qty": str(queue_day.site_cart_matched_qty),
                "site_cart_expired_qty": str(queue_day.site_cart_expired_qty),
                "site_cart_open_qty": str(queue_day.site_cart_open_qty),
                "site_soft_trigger_count": queue_day.site_soft_trigger_count,
                "reserve_backlog_raw_qty": str(queue_day.reserve_backlog_raw_qty),
                "reserve_backlog_matched_qty": str(queue_day.reserve_backlog_matched_qty),
                "reserve_backlog_expired_qty": str(queue_day.reserve_backlog_expired_qty),
                "reserve_backlog_cancelled_qty": str(
                    queue_day.reserve_backlog_cancelled_qty
                ),
                "reserve_backlog_open_qty": str(queue_day.reserve_backlog_open_qty),
                "reserve_increase_qty": str(queue_day.reserve_increase_qty),
                "site_signals_included": int(config.site_signals_enabled),
                "physical_stock_qty": str(physical_stock),
                "raw_reserve_qty": str(raw_reserve),
                "effective_reserve_qty": str(effective_reserve),
                "reserve_qty": str(effective_reserve),
                "reserve_backlog_qty": str(reserve_backlog),
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
                        ("negative_raw_reserve_ignored", raw_reserve < ZERO),
                    )
                    if applies
                ),
            }
            for stage_name in ("conservative", "typical", "service"):
                launch_profile = select_launch_profile(
                    item=item,
                    snapshot=launch_snapshot,
                    scenario=stage_model_scenario(stage_name),
                    policy=policy,
                    min_samples=launch_profile_min_samples,
                )
                prefix = f"launch_{stage_name}"
                input_row.update(
                    {
                        f"{prefix}_group_level": (
                            launch_profile.group_level if launch_profile else ""
                        ),
                        f"{prefix}_group_key": (launch_profile.group_key if launch_profile else ""),
                        f"{prefix}_sample_count": (
                            launch_profile.sample_count if launch_profile else 0
                        ),
                        f"{prefix}_quantile": (
                            str(launch_profile.quantile) if launch_profile else ""
                        ),
                        f"{prefix}_demand_qty_30d": (
                            str(launch_profile.demand_qty_30d) if launch_profile else ""
                        ),
                        f"{prefix}_min_qty": (
                            str(launch_profile.min_qty) if launch_profile else ""
                        ),
                        f"{prefix}_max_qty": (
                            str(launch_profile.max_qty) if launch_profile else ""
                        ),
                        f"{prefix}_confidence": (
                            launch_profile.confidence if launch_profile else ""
                        ),
                    }
                )
            decision_inputs.append(input_row)

    reconciliations = reserves.reconciliations + placements.reconciliations
    input_keys = [(row["decision_date"], row["nomenclature_code"]) for row in decision_inputs]
    scenario_keys = [row["scenario_id"] for row in scenario_decisions]
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
        "scenario_definition_primary_key",
        passed=len(scenario_keys) == len(set(scenario_keys)),
        severity="critical",
        value=len(scenario_keys) - len(set(scenario_keys)),
        threshold=0,
        note="Одна строка на определение сценария; раскрытие выполняет backtest.",
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
    negative_balances = reserves.source_counts.get(
        "negative_balance_rows", 0
    ) + placements.source_counts.get("negative_balance_rows", 0)
    add_quality(
        "negative_register_balances",
        passed=negative_balances == 0,
        severity="high",
        value=negative_balances,
        threshold=0,
        note="Отрицательные резервы/размещения требуют отдельного аудита.",
    )
    for key, label in (
        ("missing_xml_id_count", "События сайта без PRODUCT_XML_ID."),
        ("invalid_xml_id_count", "События сайта с невалидным PRODUCT_XML_ID."),
        ("ambiguous_guid_count", "GUID сайта соответствует нескольким SKU 1С."),
        ("invalid_event_date_count", "События сайта с невалидной датой."),
    ):
        value = int(site_mapping_stats.get(key) or 0)
        add_quality(
            f"site_{key.removesuffix('_count')}",
            passed=value == 0,
            severity="critical",
            value=value,
            threshold=0,
            note=label,
        )
    out_of_cohort = int(site_mapping_stats.get("out_of_cohort_count") or 0)
    add_quality(
        "site_events_out_of_cohort",
        passed=out_of_cohort == 0,
        severity="high",
        value=out_of_cohort,
        threshold=0,
        note="События сохраняются для аудита, но не влияют на когорту дисплеев.",
    )
    invalid_cancellations = int(
        site_mapping_stats.get("invalid_cancellation_date_count") or 0
    )
    add_quality(
        "site_cancellation_date_order",
        passed=invalid_cancellations == 0,
        severity="high",
        value=invalid_cancellations,
        threshold=0,
        note="Дата отмены не должна предшествовать оформлению заказа.",
    )
    site_order_qty = _decimal(site_mapping_stats.get("site_order_quantity"))
    onec_sales_qty = sum(
        (
            max(ZERO, _decimal(qty))
            for code in codes
            for business_date, qty in sales_by_code.get(code, {}).items()
            if date_from <= business_date <= date_to
        ),
        ZERO,
    )
    add_quality(
        "site_order_vs_onec_sales_volume",
        passed=site_order_qty <= onec_sales_qty,
        severity="high",
        value=str(site_order_qty),
        threshold=str(onec_sales_qty),
        note=(
            "Заказы сайта сравниваются с верхней границей всех продаж 1С; "
            "это контроль объёма, а не атрибуция канала."
        ),
    )
    site_daily: dict[date, Decimal] = defaultdict(Decimal)
    for row in site_event_rows:
        business_date = _date(row.get("event_date"))
        if (
            business_date is not None
            and date_from <= business_date <= date_to
            and _clean(row.get("mapping_status")) == "matched"
            and not int(_decimal(row.get("manual_review_only")))
        ):
            site_daily[business_date] += _decimal(row.get("quantity"))
    july_days = [day for day in _daterange(date_from, date_to) if day.month == 7]
    pre_july_days = [day for day in _daterange(date_from, date_to) if day.month != 7]
    july_avg = (
        sum((site_daily.get(day, ZERO) for day in july_days), ZERO)
        / Decimal(len(july_days))
        if july_days
        else ZERO
    )
    pre_july_avg = (
        sum((site_daily.get(day, ZERO) for day in pre_july_days), ZERO)
        / Decimal(len(pre_july_days))
        if pre_july_days
        else ZERO
    )
    july_ratio = july_avg / pre_july_avg if pre_july_avg > ZERO else ZERO
    add_quality(
        "site_july_structural_jump",
        passed=july_ratio <= Decimal("3"),
        severity="high",
        value=str(july_ratio),
        threshold="3",
        note="Июль не исключается; отчёт обязан показывать его отдельным периодом.",
    )
    add_quality(
        "site_daily_event_volume",
        passed=True,
        severity="low",
        value=str(max(site_daily.values(), default=ZERO)),
        threshold="informational",
        note="Максимальный количественный объём SKU-событий сайта за день.",
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
        if not any(row["status"] == "fail" and row["severity"] == "critical" for row in quality)
        else "FAIL"
    )
    return PreflightTables(
        decision_inputs=decision_inputs,
        scenario_decisions=scenario_decisions,
        lifecycle_daily=lifecycle_daily,
        daily_facts=daily_facts,
        initial_pipeline=initial_pipeline,
        source_quality=quality,
        reconciliations=reconciliations,
        site_events=[dict(row) for row in site_event_rows],
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
    site_events_csv: Path,
    site_mapping_stats: Mapping[str, Any],
) -> dict[str, Any]:
    scenario_config = load_scenario_config(config_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    datasets = {
        "decision-inputs.csv": tables.decision_inputs,
        "scenario-decisions.csv": tables.scenario_decisions,
        "lifecycle-daily.csv": tables.lifecycle_daily,
        "daily-facts.csv": tables.daily_facts,
        "initial-pipeline.csv": tables.initial_pipeline,
        "source-quality.csv": tables.source_quality,
        "reconciliations.csv": tables.reconciliations,
        "site-events-normalized.csv": tables.site_events,
    }
    for filename, rows in datasets.items():
        write_csv(output_dir / filename, rows)
    raw_site_target = output_dir / "site-events-raw.csv"
    if site_events_csv.resolve() != raw_site_target.resolve():
        shutil.copyfile(site_events_csv, raw_site_target)
    write_workbook(
        output_dir / "backtest-preflight.xlsx",
        {
            "decision-inputs": tables.decision_inputs,
            "scenario-decisions": tables.scenario_decisions,
            "lifecycle-daily": tables.lifecycle_daily,
            "initial-pipeline": tables.initial_pipeline,
            "source-quality": tables.source_quality,
            "reconciliations": tables.reconciliations,
            "site-events": tables.site_events,
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
            "initial_pipeline": len(tables.initial_pipeline),
            "source_quality": len(tables.source_quality),
            "reconciliations": len(tables.reconciliations),
            "site_events": len(tables.site_events),
        },
        "site_export": {
            "raw_filename": "site-events-raw.csv",
            "raw_sha256": file_hashes["site-events-raw.csv"],
            "normalized_filename": "site-events-normalized.csv",
            "normalized_sha256": file_hashes["site-events-normalized.csv"],
            "queue_days": scenario_config.site_queue_days,
            "deduplication_key": "session_key+nomenclature_code+event_date",
            "unordered_cart_daily_cap": str(
                scenario_config.unordered_cart_daily_cap
            ),
            "mapping_stats": {
                key: str(value) if isinstance(value, Decimal) else value
                for key, value in sorted(site_mapping_stats.items())
            },
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
    "fetch_daily_sales",
    "fetch_kmp4_demand",
    "fetch_onec_product_refs",
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
