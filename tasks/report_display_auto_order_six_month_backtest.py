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
import inspect
import json
import math
import os
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import bindparam, func, select, text
from sqlalchemy import inspect as sa_inspect

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.assortment_lifecycle import (
    SALE_MIN_SALES_QTY,
    AssortmentLifecycleDecision,
    AssortmentLifecycleInput,
    AssortmentStatus,
    decide_legacy_assortment_status,
)
from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE,
)
from app.services.assortment_lifecycle_replay_store import (
    DEFAULT_REPLAY_STORE_PATH,
    AssortmentLifecycleReplayStore,
    stable_hash,
)
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
DEFAULT_HISTORY_START = date(2025, 1, 1)
DEFAULT_LEAD_TIME_DAYS = 52
DEFAULT_SENSITIVITY_DAYS = (45, 52, 59)
LAUNCH_PROFILE_OBSERVATION_DAYS = 30
LAUNCH_PROFILE_MIN_AVAILABILITY_DAYS = 7
DEFAULT_LAUNCH_PROFILE_MIN_SAMPLES = 8
STAGE_MODEL_SCENARIO_NAMES = ("legacy", "conservative", "typical", "service")
LEGACY_REPLAY_MODEL_VERSION = "legacy-v1-reconstructed"


@dataclass(frozen=True)
class PurchaseLine:
    created_at: date
    qty: Decimal
    price: Decimal
    supplier_name: str
    order_ref: str
    expected_receipt_at: date | None
    cargo_handoff_at: date | None = None


@dataclass(frozen=True)
class ReceiptLine:
    received_at: date
    qty: Decimal


@dataclass(frozen=True)
class StageModelScenario:
    name: str
    launch_quantile: Decimal | None
    use_sales_start_min_max: bool
    protected_working_safety_days: int = 0


@dataclass(frozen=True)
class LaunchObservation:
    nomenclature_code: str
    launch_at: date
    complete_at: date
    brand: str
    quality: str
    price_segment: str
    sales_qty: Decimal
    available_days: int
    normalized_demand_qty: Decimal
    window_metrics: Mapping[int, tuple[Decimal, int, Decimal]]

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "nomenclature_code": self.nomenclature_code,
            "launch_at": self.launch_at.isoformat(),
            "complete_at": self.complete_at.isoformat(),
            "brand": self.brand,
            "quality": self.quality,
            "price_segment": self.price_segment,
            "observation_days": LAUNCH_PROFILE_OBSERVATION_DAYS,
            "sales_qty": str(self.sales_qty),
            "available_days": self.available_days,
            "normalized_demand_qty": str(self.normalized_demand_qty),
        }
        for days, (sales_qty, available_days, normalized_qty) in sorted(
            self.window_metrics.items()
        ):
            payload[f"sales_qty_{days}d"] = str(sales_qty)
            payload[f"available_days_{days}d"] = available_days
            payload[f"normalized_demand_qty_{days}d"] = str(normalized_qty)
        return payload


@dataclass(frozen=True)
class LaunchProfile:
    scenario: str
    group_level: str
    group_key: str
    sample_count: int
    quantile: Decimal
    demand_qty_30d: Decimal
    min_qty: Decimal
    max_qty: Decimal
    confidence: str


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
    lifecycle_rows: list[dict[str, Any]] = field(default_factory=list)


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
    """Load every display row from one classification run, without status filtering.

    The former backtest called ``load_auto_order_items`` and therefore kept only
    the current ``sale``/``working`` cohort.  That projected today's eligibility
    into every historical date.  The historical simulation needs the complete
    display subject and decides eligibility from events available as of each day.
    """

    table = ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE
    with app_engine.connect() as connection:
        existing_columns = {
            column["name"] for column in sa_inspect(connection).get_columns(table.name)
        }
        required_columns = {"nomenclature_code", "folder", "last_run_id"}
        missing_required = required_columns - existing_columns
        if missing_required:
            raise ValueError(
                "assortment_lifecycle_classification_missing_columns:"
                + ",".join(sorted(missing_required))
            )
        readable_columns = [column for column in table.c if column.name in existing_columns]
        run_id = connection.execute(
            select(func.max(table.c.last_run_id)).where(table.c.folder.ilike(f"%{folder}%"))
        ).scalar()
        if run_id is None:
            return [], 0
        rows = (
            connection.execute(
                select(*readable_columns)
                .where(
                    table.c.folder.ilike(f"%{folder}%"),
                    table.c.last_run_id == run_id,
                )
                .order_by(table.c.nomenclature_code.asc())
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows], int(run_id)


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
                cargo_handoff_at=_date(
                    row.get("cargo_handoff_at") or row.get("cargo_handoff_date")
                ),
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


def _source_record(item: Mapping[str, Any]) -> Mapping[str, Any]:
    value = item.get("source_record")
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _item_created_at(item: Mapping[str, Any]) -> date | None:
    source = _source_record(item)
    return _date(
        source.get("created_at")
        or source.get("card_created_at")
        or source.get("onec_novelty_date")
        or item.get("created_at")
    )


def item_active_as_of(item: Mapping[str, Any], *, as_of: date) -> bool:
    created_at = _item_created_at(item)
    return created_at is None or created_at <= as_of


def stage_model_scenario(name: str) -> StageModelScenario:
    scenarios = {
        "legacy": StageModelScenario(
            name="legacy",
            launch_quantile=None,
            use_sales_start_min_max=False,
        ),
        "conservative": StageModelScenario(
            name="conservative",
            launch_quantile=Decimal("0.25"),
            use_sales_start_min_max=True,
        ),
        "typical": StageModelScenario(
            name="typical",
            launch_quantile=Decimal("0.50"),
            use_sales_start_min_max=True,
        ),
        "service": StageModelScenario(
            name="service",
            launch_quantile=Decimal("0.75"),
            use_sales_start_min_max=True,
            protected_working_safety_days=7,
        ),
    }
    try:
        return scenarios[name]
    except KeyError as exc:
        raise ValueError(f"unknown stage model scenario: {name}") from exc


def _profile_text(item: Mapping[str, Any], field: str) -> str:
    return _clean(item.get(field) or _source_record(item).get(field)).casefold()


def _launch_group_keys(item: Mapping[str, Any]) -> tuple[tuple[str, str, str, str], ...]:
    brand = _profile_text(item, "brand_compatibility")
    quality = _profile_text(item, "quality_normalized")
    price_segment = _profile_text(item, "price_segment")
    candidates = (
        ("brand_quality_price", brand, quality, price_segment),
        ("brand_price", brand, "", price_segment),
        ("price", "", "", price_segment),
        ("all_displays", "", "", ""),
    )
    result: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for level, group_brand, group_quality, group_price in candidates:
        value_key = (group_brand, group_quality, group_price)
        if level != "all_displays" and not any(value_key):
            continue
        if value_key in seen:
            continue
        seen.add(value_key)
        result.append((level, group_brand, group_quality, group_price))
    return tuple(result)


def build_launch_observations(
    *,
    items: Sequence[Mapping[str, Any]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    availability_by_code: Mapping[str, set[date]],
    receipt_history: Mapping[str, Sequence[ReceiptLine]],
    history_start: date,
) -> list[LaunchObservation]:
    """Build first-30-day launch observations without using phone sales.

    A launch starts at the first receipt or first proven positive physical
    availability.  Left-censored rows at the history boundary and rows with
    fewer than seven proven availability days are excluded from the profile.
    """

    observations: list[LaunchObservation] = []
    for item in items:
        code = _clean(item.get("nomenclature_code"))
        if not code:
            continue
        receipt_dates = [row.received_at for row in receipt_history.get(code, ())]
        available_dates = sorted(availability_by_code.get(code, set()))
        launch_dates = receipt_dates + available_dates[:1]
        if not launch_dates:
            continue
        launch_at = min(launch_dates)
        if launch_at <= history_start:
            continue
        window_metrics: dict[int, tuple[Decimal, int, Decimal]] = {}
        for days in (7, 14, 30, 60, 90):
            window_end = launch_at + timedelta(days=days - 1)
            window_dates = set(_daterange(launch_at, window_end))
            window_available = len(window_dates.intersection(available_dates))
            window_sales = sum(
                (
                    qty
                    for business_date, qty in sales_by_code.get(code, {}).items()
                    if launch_at <= business_date <= window_end
                ),
                ZERO,
            )
            window_normalized = (
                window_sales * Decimal(days) / Decimal(window_available)
                if window_available > 0
                else ZERO
            )
            window_metrics[days] = (
                window_sales,
                window_available,
                window_normalized,
            )
        complete_at = launch_at + timedelta(days=LAUNCH_PROFILE_OBSERVATION_DAYS - 1)
        sales_qty, proven_available, normalized = window_metrics[LAUNCH_PROFILE_OBSERVATION_DAYS]
        if proven_available < LAUNCH_PROFILE_MIN_AVAILABILITY_DAYS:
            continue
        observations.append(
            LaunchObservation(
                nomenclature_code=code,
                launch_at=launch_at,
                complete_at=complete_at,
                brand=_profile_text(item, "brand_compatibility"),
                quality=_profile_text(item, "quality_normalized"),
                price_segment=_profile_text(item, "price_segment"),
                sales_qty=sales_qty,
                available_days=proven_available,
                normalized_demand_qty=normalized,
                window_metrics=window_metrics,
            )
        )
    observations.sort(key=lambda row: (row.complete_at, row.nomenclature_code))
    return observations


def build_launch_profile_snapshot(
    observations: Sequence[LaunchObservation],
    *,
    as_of: date,
) -> dict[tuple[str, str, str], tuple[Decimal, ...]]:
    """Return launch samples fully observable before one decision date."""

    grouped: dict[tuple[str, str, str], list[Decimal]] = defaultdict(list)
    for row in observations:
        if row.complete_at >= as_of:
            continue
        keys = (
            (row.brand, row.quality, row.price_segment),
            (row.brand, "", row.price_segment),
            ("", "", row.price_segment),
            ("", "", ""),
        )
        for key in dict.fromkeys(keys):
            grouped[key].append(row.normalized_demand_qty)
    return {key: tuple(sorted(values)) for key, values in grouped.items()}


def _nearest_rank_quantile(values: Sequence[Decimal], quantile: Decimal) -> Decimal:
    if not values:
        return ZERO
    ordered = sorted(values)
    rank = max(1, math.ceil(quantile * Decimal(len(ordered))))
    return ordered[rank - 1]


def select_launch_profile(
    *,
    item: Mapping[str, Any],
    snapshot: Mapping[tuple[str, str, str], Sequence[Decimal]],
    scenario: StageModelScenario,
    policy: AutoOrderPolicy,
    min_samples: int = DEFAULT_LAUNCH_PROFILE_MIN_SAMPLES,
) -> LaunchProfile | None:
    if scenario.launch_quantile is None:
        return None
    selected_level = ""
    selected_key: tuple[str, str, str] | None = None
    selected_values: Sequence[Decimal] = ()
    group_keys = _launch_group_keys(item)
    for level, brand, quality, price_segment in group_keys:
        values = snapshot.get((brand, quality, price_segment), ())
        if len(values) >= min_samples:
            selected_level = level
            selected_key = (brand, quality, price_segment)
            selected_values = values
            break
    if not selected_values:
        fallback = snapshot.get(("", "", ""), ())
        if not fallback:
            return None
        selected_level = "all_displays_low_sample"
        selected_key = ("", "", "")
        selected_values = fallback
    demand_qty = _nearest_rank_quantile(selected_values, scenario.launch_quantile)
    planning_days = (
        policy.order_cadence_days
        + policy.supplier_prepare_days
        + policy.logistics_days
        + policy.supplier_delay_buffer_days
        + policy.receiving_buffer_days
        + policy.distribution_to_shelf_days
    )
    daily_rate = demand_qty / Decimal(LAUNCH_PROFILE_OBSERVATION_DAYS)
    min_qty = _ceil(daily_rate * Decimal(LAUNCH_PROFILE_OBSERVATION_DAYS))
    max_qty = _ceil(daily_rate * Decimal(planning_days))
    sample_count = len(selected_values)
    confidence = (
        "high"
        if selected_level == "brand_quality_price" and sample_count >= 20
        else "medium" if sample_count >= min_samples else "low"
    )
    assert selected_key is not None
    return LaunchProfile(
        scenario=scenario.name,
        group_level=selected_level,
        group_key="|".join(selected_key),
        sample_count=sample_count,
        quantile=scenario.launch_quantile,
        demand_qty_30d=demand_qty,
        min_qty=min_qty,
        max_qty=max_qty,
        confidence=confidence,
    )


def _dated_values(values: Any, *, as_of: date) -> tuple[date, ...]:
    if not isinstance(values, (list, tuple, set)):
        values = (values,)
    return tuple(
        sorted(
            {parsed for value in values if (parsed := _date(value)) is not None and parsed <= as_of}
        )
    )


def historical_lifecycle_decision(
    *,
    item: Mapping[str, Any],
    sales: Mapping[date, Decimal],
    availability_dates: set[date],
    purchases: Sequence[PurchaseLine],
    receipts: Sequence[ReceiptLine],
    as_of: date,
    previous_status: str | None,
) -> tuple[AssortmentLifecycleDecision, dict[str, Any]]:
    """Classify one SKU using only evidence dated on or before ``as_of``."""

    source = _source_record(item)
    source_order_at = _date(source.get("first_supplier_order_at"))
    order_dates = [line.created_at for line in purchases if line.created_at <= as_of]
    if source_order_at is not None and source_order_at <= as_of:
        order_dates.append(source_order_at)

    cargo_dates = {
        line.cargo_handoff_at
        for line in purchases
        if line.cargo_handoff_at is not None and line.cargo_handoff_at <= as_of
    }
    cargo_dates.update(_dated_values(source.get("supplier_order_cargo_handoff_dates"), as_of=as_of))
    receipt_dates = {line.received_at for line in receipts if line.received_at <= as_of}
    receipt_dates.update(_dated_values(source.get("receipt_dates"), as_of=as_of))

    observed_sale_dates = sorted(day for day, qty in sales.items() if day <= as_of and qty > ZERO)
    source_first_sale = _date(source.get("first_sale_at"))
    if source_first_sale is not None and source_first_sale <= as_of:
        observed_sale_dates.append(source_first_sale)
    source_last_sale = _date(source.get("last_sale_at"))
    if source_last_sale is not None and source_last_sale <= as_of:
        observed_sale_dates.append(source_last_sale)
    first_sale_at = min(observed_sale_dates) if observed_sale_dates else None
    last_sale_at = max(observed_sale_dates) if observed_sale_dates else None

    sales_short = _window_sum(sales, as_of=as_of, days=30)
    sales_medium = _window_sum(sales, as_of=as_of, days=90)
    sales_long = _window_sum(sales, as_of=as_of, days=180)
    available_short = _available_days(availability_dates, as_of=as_of, days=30)
    available_medium = _available_days(availability_dates, as_of=as_of, days=90)
    available_long = _available_days(availability_dates, as_of=as_of, days=180)

    manual_changed_at = _date(source.get("manual_changed_at"))
    manual_status = (
        source.get("manual_status") if manual_changed_at and manual_changed_at <= as_of else None
    )
    decision = decide_legacy_assortment_status(
        AssortmentLifecycleInput(
            nomenclature_code=_clean(item.get("nomenclature_code")),
            created_at=_item_created_at(item),
            first_supplier_order_at=min(order_dates) if order_dates else None,
            supplier_order_cargo_handoff_dates=tuple(sorted(cargo_dates)),
            receipt_dates=tuple(sorted(receipt_dates)),
            first_sale_at=first_sale_at,
            last_sale_at=last_sale_at,
            as_of=as_of,
            sales_qty_short=sales_short,
            sales_qty_medium=sales_medium,
            sales_qty_long=sales_long,
            days_in_sale_short=available_short,
            days_in_sale_medium=available_medium,
            days_in_sale_long=available_long,
            previous_status=previous_status,
            manual_status=manual_status,
            manual_reason=_clean(source.get("manual_reason")) if manual_status else "",
            manual_approved_by=(_clean(source.get("manual_approved_by")) if manual_status else ""),
            manual_changed_at=manual_changed_at if manual_status else None,
        )
    )
    evidence = {
        "sales_30": sales_short,
        "sales_90": sales_medium,
        "sales_180": sales_long,
        "available_30": available_short,
        "available_90": available_medium,
        "available_180": available_long,
        "first_sale_at": first_sale_at,
        "last_sale_at": last_sale_at,
        "first_supplier_order_at": min(order_dates) if order_dates else None,
        "first_cargo_at": min(cargo_dates) if cargo_dates else None,
        "historical_manual_status_replayed": bool(manual_status),
    }
    return decision, evidence


def warmup_lifecycle_statuses(
    *,
    items: Sequence[Mapping[str, Any]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    availability_by_code: Mapping[str, set[date]],
    purchase_history: Mapping[str, Sequence[PurchaseLine]],
    receipt_history: Mapping[str, Sequence[ReceiptLine]],
    date_from: date,
    date_to: date,
) -> dict[str, str]:
    previous: dict[str, str] = {}
    if date_from > date_to:
        return previous
    for business_date in _daterange(date_from, date_to):
        for item in items:
            code = _clean(item.get("nomenclature_code"))
            if not code or not item_active_as_of(item, as_of=business_date):
                continue
            decision, _evidence = historical_lifecycle_decision(
                item=item,
                sales=sales_by_code.get(code, {}),
                availability_dates=availability_by_code.get(code, set()),
                purchases=purchase_history.get(code, ()),
                receipts=receipt_history.get(code, ()),
                as_of=business_date,
                previous_status=previous.get(code),
            )
            previous[code] = decision.status.value
    return previous


def build_historical_lifecycle_trajectory(
    *,
    items: Sequence[Mapping[str, Any]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    availability_by_code: Mapping[str, set[date]],
    purchase_history: Mapping[str, Sequence[PurchaseLine]],
    receipt_history: Mapping[str, Sequence[ReceiptLine]],
    history_start: date,
    date_from: date,
    date_to: date,
) -> list[dict[str, Any]]:
    """Build one look-ahead-free daily legacy trajectory for all scenarios."""

    item_by_code = {
        _clean(item.get("nomenclature_code")): item
        for item in items
        if _clean(item.get("nomenclature_code"))
    }
    previous_statuses = warmup_lifecycle_statuses(
        items=items,
        sales_by_code=sales_by_code,
        availability_by_code=availability_by_code,
        purchase_history=purchase_history,
        receipt_history=receipt_history,
        date_from=history_start,
        date_to=date_from - timedelta(days=1),
    )
    rows: list[dict[str, Any]] = []
    for business_date in _daterange(date_from, date_to):
        for code in sorted(item_by_code):
            item = item_by_code[code]
            if not item_active_as_of(item, as_of=business_date):
                continue
            previous_status = previous_statuses.get(code)
            lifecycle, evidence = historical_lifecycle_decision(
                item=item,
                sales=sales_by_code.get(code, {}),
                availability_dates=availability_by_code.get(code, set()),
                purchases=purchase_history.get(code, ()),
                receipts=receipt_history.get(code, ()),
                as_of=business_date,
                previous_status=previous_status,
            )
            previous_statuses[code] = lifecycle.status.value
            rows.append(
                {
                    "business_date": business_date.isoformat(),
                    "nomenclature_code": code,
                    "name": _clean(item.get("name")),
                    "previous_status": previous_status or "",
                    "status": lifecycle.status.value,
                    "status_label": lifecycle.status_label,
                    "reason_codes": list(lifecycle.reason_codes),
                    "reason_text": lifecycle.reason_text,
                    "auto_order_allowed": lifecycle.auto_order_allowed,
                    "manual_review_required": lifecycle.manual_review_required,
                    "blockers": list(lifecycle.blockers),
                    "recommended_status": (
                        lifecycle.recommended_status.value
                        if lifecycle.recommended_status is not None
                        else None
                    ),
                    "sales_30": str(evidence["sales_30"]),
                    "sales_90": str(evidence["sales_90"]),
                    "sales_180": str(evidence["sales_180"]),
                    "available_days_30": evidence["available_30"],
                    "available_days_90": evidence["available_90"],
                    "available_days_180": evidence["available_180"],
                    "first_sale_at": evidence["first_sale_at"],
                    "last_sale_at": evidence["last_sale_at"],
                    "first_supplier_order_at": evidence["first_supplier_order_at"],
                    "first_cargo_at": evidence["first_cargo_at"],
                    "historical_manual_status_replayed": evidence[
                        "historical_manual_status_replayed"
                    ],
                }
            )
    return rows


def historical_replay_facts(
    *,
    items: Sequence[Mapping[str, Any]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    availability_by_code: Mapping[str, set[date]],
    purchase_history: Mapping[str, Sequence[PurchaseLine]],
    receipt_history: Mapping[str, Sequence[ReceiptLine]],
    item_default_date: date,
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for item in items:
        code = _clean(item.get("nomenclature_code"))
        if code:
            facts.append(
                {
                    "business_date": _item_created_at(item) or item_default_date,
                    "nomenclature_code": code,
                    "fact_type": "item",
                    "payload": _source_record(item),
                }
            )
    for code, daily in sales_by_code.items():
        for business_date, quantity in daily.items():
            facts.append(
                {
                    "business_date": business_date,
                    "nomenclature_code": code,
                    "fact_type": "sale",
                    "payload": {"quantity": quantity},
                }
            )
    for code, dates in availability_by_code.items():
        for business_date in dates:
            facts.append(
                {
                    "business_date": business_date,
                    "nomenclature_code": code,
                    "fact_type": "available",
                    "payload": {"available": True},
                }
            )
    for code, purchases in purchase_history.items():
        for purchase in purchases:
            facts.append(
                {
                    "business_date": purchase.created_at,
                    "nomenclature_code": code,
                    "fact_type": "supplier_order",
                    "payload": purchase.__dict__,
                }
            )
    for code, receipts in receipt_history.items():
        for receipt in receipts:
            facts.append(
                {
                    "business_date": receipt.received_at,
                    "nomenclature_code": code,
                    "fact_type": "receipt",
                    "payload": receipt.__dict__,
                }
            )
    return facts


def load_or_build_historical_lifecycle_trajectory(
    *,
    store: AssortmentLifecycleReplayStore,
    items: Sequence[Mapping[str, Any]],
    sales_by_code: Mapping[str, Mapping[date, Decimal]],
    availability_by_code: Mapping[str, set[date]],
    purchase_history: Mapping[str, Sequence[PurchaseLine]],
    receipt_history: Mapping[str, Sequence[ReceiptLine]],
    history_start: date,
    date_from: date,
    date_to: date,
    scope: str,
    source_manifest: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    facts = historical_replay_facts(
        items=items,
        sales_by_code=sales_by_code,
        availability_by_code=availability_by_code,
        purchase_history=purchase_history,
        receipt_history=receipt_history,
        item_default_date=history_start,
    )
    dataset = store.put_dataset(
        scope=scope,
        observation_from=min([history_start, *(_date(fact["business_date"]) for fact in facts)]),
        observation_to=max([date_to, *(_date(fact["business_date"]) for fact in facts)]),
        facts=facts,
        source_manifest=source_manifest,
    )
    lifecycle_module_path = Path(inspect.getsourcefile(decide_legacy_assortment_status) or "")
    policy_hash = stable_hash(
        {
            "model_version": LEGACY_REPLAY_MODEL_VERSION,
            "lifecycle_module_source": lifecycle_module_path.read_text(encoding="utf-8"),
            "implementation": inspect.getsource(historical_lifecycle_decision),
            "warmup_implementation": inspect.getsource(warmup_lifecycle_statuses),
        }
    )
    cached = store.find_trajectory(
        dataset_hash=dataset.key,
        model_version=LEGACY_REPLAY_MODEL_VERSION,
        policy_hash=policy_hash,
        period_from=date_from,
        period_to=date_to,
    )
    if cached is not None:
        rows = store.load_trajectory_rows(cached.trajectory_hash)
        return rows, {
            "dataset_hash": dataset.key,
            "dataset_reused": dataset.reused,
            "trajectory_hash": cached.trajectory_hash,
            "trajectory_reused": True,
            "policy_hash": policy_hash,
            "row_count": len(rows),
        }
    rows = build_historical_lifecycle_trajectory(
        items=items,
        sales_by_code=sales_by_code,
        availability_by_code=availability_by_code,
        purchase_history=purchase_history,
        receipt_history=receipt_history,
        history_start=history_start,
        date_from=date_from,
        date_to=date_to,
    )
    trajectory = store.put_trajectory(
        dataset_hash=dataset.key,
        model_version=LEGACY_REPLAY_MODEL_VERSION,
        policy_hash=policy_hash,
        period_from=date_from,
        period_to=date_to,
        rows=rows,
        metadata={
            "source": "reconstructed_legacy",
            "look_ahead_free": True,
            "production_action": "none_read_only",
        },
    )
    rows = store.load_trajectory_rows(trajectory.key)
    return rows, {
        "dataset_hash": dataset.key,
        "dataset_reused": dataset.reused,
        "trajectory_hash": trajectory.key,
        "trajectory_reused": trajectory.reused,
        "policy_hash": policy_hash,
        "row_count": len(rows),
    }


def _lifecycle_from_replay_row(
    row: Mapping[str, Any],
) -> tuple[AssortmentLifecycleDecision, dict[str, Any]]:
    recommended = _clean(row.get("recommended_status"))
    lifecycle = AssortmentLifecycleDecision(
        nomenclature_code=_clean(row.get("nomenclature_code")),
        status=AssortmentStatus(_clean(row.get("status"))),
        status_label=_clean(row.get("status_label")),
        reason_codes=tuple(_text_values(row.get("reason_codes"))),
        reason_text=_clean(row.get("reason_text")),
        recommended_status=AssortmentStatus(recommended) if recommended else None,
        manual_review_required=bool(row.get("manual_review_required")),
        auto_order_allowed=bool(row.get("auto_order_allowed")),
        blockers=tuple(_text_values(row.get("blockers"))),
    )
    evidence = {
        "sales_30": _decimal(row.get("sales_30")),
        "sales_90": _decimal(row.get("sales_90")),
        "sales_180": _decimal(row.get("sales_180")),
        "available_30": _int_or_none(row.get("available_days_30")),
        "available_90": _int_or_none(row.get("available_days_90")),
        "available_180": _int_or_none(row.get("available_days_180")),
        "first_sale_at": _date(row.get("first_sale_at")),
        "last_sale_at": _date(row.get("last_sale_at")),
        "first_supplier_order_at": _date(row.get("first_supplier_order_at")),
        "first_cargo_at": _date(row.get("first_cargo_at")),
        "historical_manual_status_replayed": bool(row.get("historical_manual_status_replayed")),
    }
    return lifecycle, evidence


def _lifecycle_csv_row(
    *,
    business_date: date,
    code: str,
    name: str,
    previous_status: str | None,
    lifecycle: AssortmentLifecycleDecision,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "business_date": business_date.isoformat(),
        "nomenclature_code": code,
        "name": name,
        "previous_status": previous_status or "",
        "status": lifecycle.status.value,
        "status_label": lifecycle.status_label,
        "reason_codes": "|".join(lifecycle.reason_codes),
        "auto_order_allowed": int(lifecycle.auto_order_allowed),
        "manual_review_required": int(lifecycle.manual_review_required),
        "sales_30": str(evidence["sales_30"]),
        "sales_90": str(evidence["sales_90"]),
        "sales_180": str(evidence["sales_180"]),
        "available_days_30": evidence["available_30"],
        "available_days_90": evidence["available_90"],
        "available_days_180": evidence["available_180"],
    }


def _text_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item for item in value.split("|") if item)
    if isinstance(value, Sequence):
        return tuple(_clean(item) for item in value if _clean(item))
    return ()


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(Decimal(str(value)))


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
    accelerating = rates[30] > ZERO and rates[30] >= rates[90] * ACCELERATING_MIN_GROWTH_MULTIPLIER
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


def stage_recommendation(
    *,
    lifecycle: AssortmentLifecycleDecision,
    rate: Decimal,
    trend: str,
    evidence: Mapping[str, Any],
    free_stock: Decimal,
    incoming_qty: Decimal,
    policy: AutoOrderPolicy,
    item: Mapping[str, Any] | None = None,
    stage_scenario: StageModelScenario | None = None,
    launch_profile: LaunchProfile | None = None,
) -> tuple[Decimal, Decimal, str, str, Decimal]:
    """Apply the approved quantity mode for the historical lifecycle stage."""

    status = lifecycle.status
    scenario = stage_scenario or stage_model_scenario("legacy")
    if status in {
        AssortmentStatus.FRUIT,
        AssortmentStatus.NEWBORN,
        AssortmentStatus.NEWBORN_NEED,
        AssortmentStatus.PENSION,
        AssortmentStatus.NONLIQUID,
        AssortmentStatus.DO_NOT_ORDER,
    }:
        return ZERO, ZERO, "do_not_order", f"stage_{status.value}", ZERO

    if status is AssortmentStatus.NEW_ITEM:
        speed_tier = f"stage_{status.value}_{scenario.name}"
        if launch_profile is None or launch_profile.max_qty <= ZERO:
            return ZERO, ZERO, "do_not_order", speed_tier, ZERO
        inventory_position = free_stock + incoming_qty
        if inventory_position > launch_profile.min_qty:
            return ZERO, launch_profile.max_qty, "do_not_order", speed_tier, ZERO
        raw = _ceil(max(ZERO, launch_profile.max_qty - inventory_position))
        rounded = rounded_order_qty(
            raw,
            min_order_qty=policy.min_order_qty,
            max_order_qty=policy.max_order_qty,
            order_rounding_rules=policy.order_rounding_rules,
        )
        if rounded <= ZERO:
            return ZERO, launch_profile.max_qty, "do_not_order", speed_tier, raw
        return rounded, launch_profile.max_qty, "manual_review", speed_tier, raw

    if status is AssortmentStatus.SALE:
        return _recommendation(
            item={"auto_order_allowed": lifecycle.auto_order_allowed},
            rate=rate,
            trend=trend,
            availability_90=int(evidence.get("available_90") or 0),
            free_stock=free_stock,
            incoming_qty=incoming_qty,
            policy=policy,
        )

    planning_days = (
        policy.order_cadence_days
        + policy.supplier_prepare_days
        + policy.logistics_days
        + policy.supplier_delay_buffer_days
        + policy.receiving_buffer_days
        + policy.distribution_to_shelf_days
    )
    target = _ceil(rate * Decimal(planning_days)) if rate > ZERO else ZERO
    speed_tier = f"stage_{status.value}_{scenario.name}"
    if status is AssortmentStatus.SALES_START:
        if scenario.use_sales_start_min_max:
            reorder_days = max(1, planning_days - policy.order_cadence_days)
            min_target = _ceil(rate * Decimal(reorder_days)) if rate > ZERO else ZERO
            if free_stock + incoming_qty > min_target:
                return ZERO, target, "do_not_order", speed_tier, ZERO
        else:
            remaining_test_qty = max(
                ZERO,
                SALE_MIN_SALES_QTY - _decimal(evidence.get("sales_180")),
            )
            target = min(target, remaining_test_qty)
    if (
        status is AssortmentStatus.WORKING
        and scenario.protected_working_safety_days > 0
        and (trend == "accelerating" or bool(_clean((item or {}).get("expensive_profile"))))
    ):
        target += _ceil(rate * Decimal(scenario.protected_working_safety_days))
    raw = _ceil(max(ZERO, target - free_stock - incoming_qty))
    rounded = rounded_order_qty(
        raw,
        min_order_qty=policy.min_order_qty,
        max_order_qty=policy.max_order_qty,
        order_rounding_rules=policy.order_rounding_rules,
    )
    if rounded <= ZERO:
        return ZERO, target, "do_not_order", speed_tier, raw
    if status is AssortmentStatus.SALES_START or not lifecycle.auto_order_allowed:
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
    receipt_history: Mapping[str, Sequence[ReceiptLine]] | None = None,
    history_start: date | None = None,
    use_historical_lifecycle: bool = False,
    stage_scenario: StageModelScenario | None = None,
    launch_observations: Sequence[LaunchObservation] = (),
    launch_profile_min_samples: int = DEFAULT_LAUNCH_PROFILE_MIN_SAMPLES,
    historical_lifecycle_trajectory: Sequence[Mapping[str, Any]] | None = None,
) -> SimulationResult:
    codes = [_clean(item.get("nomenclature_code")) for item in items]
    item_by_code = {_clean(item.get("nomenclature_code")): item for item in items}
    # reconstruct_historical_stock records the end-of-day balance after applying
    # that day's movements.  Starting the simulation from date_from would then
    # subtract date_from sales twice.  Prefer the prior day's closing balance;
    # retain the date_from fallback for isolated tests and legacy callers that
    # provide an explicit opening snapshot instead of daily closing balances.
    prior_date = date_from - timedelta(days=1)
    starting_stock = actual_stock_by_day.get(
        prior_date,
        actual_stock_by_day.get(date_from, {}),
    )
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
    active_stage_scenario = stage_scenario or stage_model_scenario("legacy")
    summary = SimulationSummary(scenario=scenario, lead_time_days=lead_time_days)
    receipts_by_code = receipt_history or {}
    previous_statuses: dict[str, str] = {}
    lifecycle_by_code: dict[str, AssortmentLifecycleDecision] = {}
    lifecycle_evidence_by_code: dict[str, dict[str, Any]] = {}
    lifecycle_rows: list[dict[str, Any]] = []
    trajectory_by_key = {
        (_date(row.get("business_date")), _clean(row.get("nomenclature_code"))): row
        for row in (historical_lifecycle_trajectory or ())
    }
    if use_historical_lifecycle and not trajectory_by_key:
        effective_history_start = history_start or min(
            (
                business_date
                for rows in sales_by_code.values()
                for business_date in rows
                if business_date < date_from
            ),
            default=date_from,
        )
        previous_statuses = warmup_lifecycle_statuses(
            items=items,
            sales_by_code=sales_by_code,
            availability_by_code=availability_by_code,
            purchase_history=purchase_history,
            receipt_history=receipts_by_code,
            date_from=effective_history_start,
            date_to=date_from - timedelta(days=1),
        )
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

        if use_historical_lifecycle:
            for code in codes:
                item = item_by_code[code]
                if not item_active_as_of(item, as_of=business_date):
                    continue
                cached_row = trajectory_by_key.get((business_date, code))
                if cached_row is not None:
                    lifecycle, lifecycle_evidence = _lifecycle_from_replay_row(cached_row)
                    previous_status = _clean(cached_row.get("previous_status")) or None
                else:
                    if trajectory_by_key:
                        raise ValueError(f"replay_trajectory_row_missing:{business_date}:{code}")
                    previous_status = previous_statuses.get(code)
                    lifecycle, lifecycle_evidence = historical_lifecycle_decision(
                        item=item,
                        sales=sales_by_code.get(code, {}),
                        availability_dates=availability_by_code.get(code, set()),
                        purchases=purchase_history.get(code, ()),
                        receipts=receipts_by_code.get(code, ()),
                        as_of=business_date,
                        previous_status=previous_status,
                    )
                previous_statuses[code] = lifecycle.status.value
                lifecycle_by_code[code] = lifecycle
                lifecycle_evidence_by_code[code] = lifecycle_evidence
                lifecycle_rows.append(
                    _lifecycle_csv_row(
                        business_date=business_date,
                        code=code,
                        name=_clean(item.get("name")),
                        previous_status=previous_status,
                        lifecycle=lifecycle,
                        evidence=lifecycle_evidence,
                    )
                )

        if business_date not in decisions:
            continue
        summary.decision_points += 1
        launch_snapshot = build_launch_profile_snapshot(
            launch_observations,
            as_of=business_date,
        )
        for code in codes:
            item = item_by_code[code]
            if not item_active_as_of(item, as_of=business_date):
                continue
            rate, trend, evidence = forecast_rate(
                sales_by_code.get(code, {}),
                availability_by_code.get(code, set()),
                as_of=business_date,
                demand_multiplier=_demand_multiplier(item, policy),
            )
            incoming = sum((lot.qty for lot in pipeline.get(code, ())), ZERO)
            launch_profile = select_launch_profile(
                item=item,
                snapshot=launch_snapshot,
                scenario=active_stage_scenario,
                policy=policy,
                min_samples=launch_profile_min_samples,
            )
            if use_historical_lifecycle:
                lifecycle = lifecycle_by_code[code]
                recommended, target, decision, speed_tier, raw = stage_recommendation(
                    lifecycle=lifecycle,
                    rate=rate,
                    trend=trend,
                    evidence=lifecycle_evidence_by_code[code],
                    free_stock=stock[code],
                    incoming_qty=incoming,
                    policy=policy,
                    item=item,
                    stage_scenario=active_stage_scenario,
                    launch_profile=launch_profile,
                )
            else:
                lifecycle = None
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
                        "status": (
                            lifecycle.status.value if lifecycle else _clean(item.get("status"))
                        ),
                        "status_label": lifecycle.status_label if lifecycle else "",
                        "status_reason_codes": (
                            "|".join(lifecycle.reason_codes) if lifecycle else ""
                        ),
                        "auto_order_allowed_current": int(bool(item.get("auto_order_allowed"))),
                        "speed_tier": speed_tier,
                        "stage_model_scenario": active_stage_scenario.name,
                        "launch_profile_group_level": (
                            launch_profile.group_level if launch_profile else ""
                        ),
                        "launch_profile_group_key": (
                            launch_profile.group_key if launch_profile else ""
                        ),
                        "launch_profile_sample_count": (
                            launch_profile.sample_count if launch_profile else 0
                        ),
                        "launch_profile_confidence": (
                            launch_profile.confidence if launch_profile else ""
                        ),
                        "launch_profile_demand_qty_30d": (
                            str(launch_profile.demand_qty_30d) if launch_profile else ""
                        ),
                        "launch_profile_min_qty": (
                            str(launch_profile.min_qty) if launch_profile else ""
                        ),
                        "launch_profile_max_qty": (
                            str(launch_profile.max_qty) if launch_profile else ""
                        ),
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

    ending_launch_snapshot = build_launch_profile_snapshot(
        launch_observations,
        as_of=date_to,
    )
    for code in codes:
        rate, trend, evidence = forecast_rate(
            sales_by_code.get(code, {}),
            availability_by_code.get(code, set()),
            as_of=date_to,
            demand_multiplier=_demand_multiplier(item_by_code[code], policy),
        )
        if use_historical_lifecycle and code in lifecycle_by_code:
            ending_launch_profile = select_launch_profile(
                item=item_by_code[code],
                snapshot=ending_launch_snapshot,
                scenario=active_stage_scenario,
                policy=policy,
                min_samples=launch_profile_min_samples,
            )
            _recommended, target, _decision, _tier, _raw = stage_recommendation(
                lifecycle=lifecycle_by_code[code],
                rate=rate,
                trend=trend,
                evidence=lifecycle_evidence_by_code[code],
                free_stock=ZERO,
                incoming_qty=ZERO,
                policy=policy,
                item=item_by_code[code],
                stage_scenario=active_stage_scenario,
                launch_profile=ending_launch_profile,
            )
        else:
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
        lifecycle_rows=lifecycle_rows,
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


def write_replay_only_artifacts(
    *,
    output_dir: Path,
    lifecycle_rows: Sequence[Mapping[str, Any]],
    replay_store: Mapping[str, Any],
    replay_store_path: Path,
    date_from: date,
    date_to: date,
    history_start: date,
    preflight_dir: Path,
    preflight_manifest: Mapping[str, Any],
    classification_run_id: Any,
    scope: str,
    sku_count: int,
    daily_sales_sku_count: int,
    supplier_order_row_count: int,
    receipt_row_count: int,
    stock_counts: Mapping[str, Any],
) -> dict[str, Any]:
    """Write the reusable replay without running any order simulation."""

    _write_csv(output_dir / "lifecycle-history.csv", lifecycle_rows)
    payload = {
        "schema": "display_assortment_lifecycle_historical_replay.v1",
        "status": "complete",
        "mode": "replay_only",
        "production_action": "none_read_only",
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "history_start": history_start.isoformat(),
        "preflight": {
            "directory": str(preflight_dir),
            "schema": preflight_manifest.get("schema"),
            "status": preflight_manifest.get("preflight_status"),
            "files": preflight_manifest.get("files"),
        },
        "historical_replay_store": {
            **replay_store,
            "path": str(replay_store_path),
            "model_version": LEGACY_REPLAY_MODEL_VERSION,
            "production_action": "none_read_only",
        },
        "cohort": {
            "classification_run_id": classification_run_id,
            "scope": scope,
            "sku_count": sku_count,
        },
        "source_counts": {
            "daily_sales_sku_count": daily_sales_sku_count,
            "supplier_order_rows": supplier_order_row_count,
            "receipt_rows": receipt_row_count,
            **stock_counts,
        },
        "outputs": {"lifecycle_history_csv": "lifecycle-history.csv"},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


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
        "--preflight-dir",
        type=Path,
        required=True,
        help="Каталог проверенной предварительной таблицы со статусом PASS",
    )
    parser.add_argument(
        "--lead-time-days",
        type=int,
        nargs="+",
        default=list(DEFAULT_SENSITIVITY_DAYS),
    )
    parser.add_argument(
        "--stage-model-scenarios",
        choices=STAGE_MODEL_SCENARIO_NAMES,
        nargs="+",
        default=list(STAGE_MODEL_SCENARIO_NAMES),
    )
    parser.add_argument(
        "--launch-profile-min-samples",
        type=int,
        default=DEFAULT_LAUNCH_PROFILE_MIN_SAMPLES,
    )
    parser.add_argument(
        "--replay-store-path",
        type=Path,
        default=DEFAULT_REPLAY_STORE_PATH,
        help="Append-only SQLite store for reusable historical lifecycle trajectories",
    )
    parser.add_argument(
        "--replay-only",
        action="store_true",
        help=(
            "Build or reuse the historical lifecycle trajectory, write its local "
            "artifacts, and stop before order simulation"
        ),
    )
    args = parser.parse_args()
    if args.date_from > args.date_to:
        raise SystemExit("date-from must not exceed date-to")
    if (args.date_from - args.history_start).days < 365:
        raise SystemExit("history-start must provide at least 365 days of warm-up")
    if any(days <= 0 for days in args.lead_time_days):
        raise SystemExit("lead-time-days must be positive")
    if args.launch_profile_min_samples <= 0:
        raise SystemExit("launch-profile-min-samples must be positive")
    return args


def main() -> int:
    args = _parse_args()
    from tasks.display_auto_order_backtest_preflight import validate_preflight_directory

    try:
        preflight_manifest = validate_preflight_directory(args.preflight_dir)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if (
        preflight_manifest.get("date_from") != args.date_from.isoformat()
        or preflight_manifest.get("date_to") != args.date_to.isoformat()
    ):
        raise SystemExit("preflight period does not match backtest period")
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
        starting_pipeline = (
            {}
            if args.replay_only
            else fetch_historical_open_supplier_pipeline(
                onec_engine,
                codes=codes,
                as_of=args.date_from - timedelta(days=1),
                fallback_lead_time_days=DEFAULT_LEAD_TIME_DAYS,
            )
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
            limit=50000,
        )
    finally:
        onec_engine.dispose()

    purchases, receipts = normalize_purchase_history(
        source_rows["supplier_order_rows"],
        source_rows["receipt_rows"],
    )
    lifecycle_rows, replay_store = load_or_build_historical_lifecycle_trajectory(
        store=AssortmentLifecycleReplayStore(args.replay_store_path),
        items=items,
        sales_by_code=sales,
        availability_by_code=availability,
        purchase_history=purchases,
        receipt_history=receipts,
        history_start=args.history_start,
        date_from=args.date_from,
        date_to=args.date_to,
        scope=args.folder,
        source_manifest={
            "classification_run_id": run_id,
            "preflight_schema": preflight_manifest.get("schema"),
            "preflight_files": preflight_manifest.get("files"),
            "production_action": "none_read_only",
        },
    )
    if args.replay_only:
        payload = write_replay_only_artifacts(
            output_dir=args.output_dir,
            lifecycle_rows=lifecycle_rows,
            replay_store=replay_store,
            replay_store_path=args.replay_store_path,
            date_from=args.date_from,
            date_to=args.date_to,
            history_start=args.history_start,
            preflight_dir=args.preflight_dir,
            preflight_manifest=preflight_manifest,
            classification_run_id=run_id,
            scope=args.folder,
            sku_count=len(codes),
            daily_sales_sku_count=len(sales),
            supplier_order_row_count=len(source_rows["supplier_order_rows"]),
            receipt_row_count=len(source_rows["receipt_rows"]),
            stock_counts=stock_counts,
        )
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    auto_policy = load_auto_order_policy(args.auto_order_policy_json)
    launch_observations = build_launch_observations(
        items=items,
        sales_by_code=sales,
        availability_by_code=availability,
        receipt_history=receipts,
        history_start=args.history_start,
    )
    all_detail: list[dict[str, Any]] = []
    all_monthly: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    base_stage_scenario = (
        "typical" if "typical" in args.stage_model_scenarios else args.stage_model_scenarios[0]
    )
    for stage_scenario_name in dict.fromkeys(args.stage_model_scenarios):
        active_stage_scenario = stage_model_scenario(stage_scenario_name)
        for lead_time in sorted(set(args.lead_time_days)):
            scenario = f"{stage_scenario_name}_lead_time_{lead_time}d"
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
                receipt_history=receipts,
                history_start=args.history_start,
                use_historical_lifecycle=True,
                stage_scenario=active_stage_scenario,
                launch_observations=launch_observations,
                launch_profile_min_samples=args.launch_profile_min_samples,
                historical_lifecycle_trajectory=lifecycle_rows,
            )
            summary_row = result.summary.as_dict()
            summary_row["stage_model_scenario"] = stage_scenario_name
            summaries.append(summary_row)
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
    _write_csv(output / "lifecycle-history.csv", lifecycle_rows)
    _write_csv(
        output / "launch-observation-history.csv",
        [row.as_dict() for row in launch_observations],
    )
    payload = {
        "schema": "display_auto_order_six_month_backtest.v2",
        "status": "share_with_caveats",
        "date_from": args.date_from.isoformat(),
        "date_to": args.date_to.isoformat(),
        "history_start": args.history_start.isoformat(),
        "preflight": {
            "directory": str(args.preflight_dir),
            "schema": preflight_manifest.get("schema"),
            "status": preflight_manifest.get("preflight_status"),
            "files": preflight_manifest.get("files"),
        },
        "decision_cadence_days": auto_policy.order_cadence_days,
        "stage_model": {
            "scenarios": list(dict.fromkeys(args.stage_model_scenarios)),
            "scenario_parameters": [
                {
                    "name": scenario_name,
                    "launch_quantile": (
                        str(stage_model_scenario(scenario_name).launch_quantile)
                        if stage_model_scenario(scenario_name).launch_quantile is not None
                        else None
                    ),
                    "use_sales_start_min_max": stage_model_scenario(
                        scenario_name
                    ).use_sales_start_min_max,
                    "protected_working_safety_days": stage_model_scenario(
                        scenario_name
                    ).protected_working_safety_days,
                }
                for scenario_name in dict.fromkeys(args.stage_model_scenarios)
            ],
            "base_scenario": base_stage_scenario,
            "launch_profile_observation_days": LAUNCH_PROFILE_OBSERVATION_DAYS,
            "launch_profile_min_availability_days": LAUNCH_PROFILE_MIN_AVAILABILITY_DAYS,
            "launch_profile_min_samples": args.launch_profile_min_samples,
            "launch_observation_count": len(launch_observations),
            "phone_sales_used": False,
        },
        "historical_replay_store": {
            **replay_store,
            "path": str(args.replay_store_path),
            "model_version": LEGACY_REPLAY_MODEL_VERSION,
            "production_action": "none_read_only",
        },
        "cohort": {
            "classification_run_id": run_id,
            "sku_count": len(codes),
            "definition": "all display rows from one classification run; daily eligibility is reconstructed from historical events",
            "current_status_filter": False,
            "historical_folder_membership_warning": True,
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
            "Daily lifecycle status is reconstructed walk-forward; historical folder membership changes are unavailable, so the current display subject defines identity.",
            "Historical reserves are unavailable and therefore treated as zero; simulated free stock may be overstated and order quantity understated.",
            "Historical manual statuses are replayed only when source evidence includes an effective date; undated quality blockers are not projected backward.",
            "Opening stock and movements are reconstructed by warehouse but without an explicit quality dimension; defect and non-systematic warehouses are excluded.",
            "Historical incoming quantity is reconstructed from the 1C open-supplier-order register; expected arrival dates still depend on the supplier-order document and may be missing or stale.",
            "Supplier selection is approximated by the latest supplier and purchase price known on each decision date.",
            "Observed sales are used as demand; model stockouts expose unmet observed sales but cannot fully recover latent demand.",
            "Launch profiles exclude left-censored launches and rows with fewer than seven proven availability days; brand, quality and price-segment grouping comes from the classification snapshot.",
            "Phone sales and installed-base estimates are not used because the company does not sell phones.",
        ],
        "outputs": {
            "decision_detail_csv": "decision-detail.csv",
            "monthly_summary_csv": "monthly-summary.csv",
            "scenario_summary_csv": "scenario-summary.csv",
            "lifecycle_history_csv": "lifecycle-history.csv",
            "launch_observation_history_csv": "launch-observation-history.csv",
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
