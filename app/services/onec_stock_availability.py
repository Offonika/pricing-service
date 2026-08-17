from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    and_,
    bindparam,
    delete,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection, Engine

DEFAULT_HISTORY_DAYS = 180
DEFAULT_RETENTION_DAYS = 210
INSERT_CHUNK_SIZE = 5000
ZERO = Decimal("0.000")
ID_TYPE = BigInteger().with_variant(Integer, "sqlite")

metadata = MetaData()

SYNC_RUN_TABLE = Table(
    "onec_stock_availability_sync_run",
    metadata,
    Column("id", ID_TYPE, primary_key=True, autoincrement=True),
    Column("run_key", String(160), nullable=False, unique=True),
    Column("range_start", Date, nullable=False),
    Column("range_end", Date, nullable=False),
    Column("status", String(24), nullable=False),
    Column("opening_rows", Integer, nullable=False, default=0),
    Column("movement_rows", Integer, nullable=False, default=0),
    Column("day_delta_rows", Integer, nullable=False, default=0),
    Column("interval_rows", Integer, nullable=False, default=0),
    Column("summary", sa.JSON, nullable=False, default=dict),
    Column("error_text", Text, nullable=True),
    Column("started_at", DateTime, nullable=False),
    Column("finished_at", DateTime, nullable=True),
    Column("created_at", DateTime, nullable=False),
)

COVERAGE_TABLE = Table(
    "onec_stock_availability_coverage",
    metadata,
    Column("period_month", Date, primary_key=True),
    Column("covered_from", Date, nullable=False),
    Column("covered_to", Date, nullable=False),
    Column("status", String(24), nullable=False),
    Column("last_run_id", ID_TYPE, nullable=True),
    Column("updated_at", DateTime, nullable=False),
)

DAY_DELTA_TABLE = Table(
    "onec_stock_day_delta",
    metadata,
    Column("id", ID_TYPE, primary_key=True, autoincrement=True),
    Column("business_date", Date, nullable=False),
    Column("period_month", Date, nullable=False),
    Column("source_register", String(32), nullable=False),
    Column("product_ref", String(34), nullable=False),
    Column("product_code", String(64), nullable=False),
    Column("warehouse_key", String(80), nullable=False),
    Column("warehouse_code", String(64), nullable=False),
    Column("opening_qty", Numeric(28, 3), nullable=False),
    Column("receipt_qty", Numeric(28, 3), nullable=False),
    Column("expense_qty", Numeric(28, 3), nullable=False),
    Column("closing_qty", Numeric(28, 3), nullable=False),
    Column("available_day", Boolean, nullable=False),
    Column("last_run_id", ID_TYPE, nullable=True),
    Column("updated_at", DateTime, nullable=False),
)

INTERVAL_TABLE = Table(
    "onec_stock_availability_interval",
    metadata,
    Column("id", ID_TYPE, primary_key=True, autoincrement=True),
    Column("period_month", Date, nullable=False),
    Column("source_register", String(32), nullable=False),
    Column("product_ref", String(34), nullable=False),
    Column("product_code", String(64), nullable=False),
    Column("warehouse_key", String(80), nullable=False),
    Column("warehouse_code", String(64), nullable=False),
    Column("available_from", Date, nullable=False),
    Column("available_to", Date, nullable=False),
    Column("last_run_id", ID_TYPE, nullable=True),
    Column("updated_at", DateTime, nullable=False),
)

OPENING_BALANCE_SQL = text("""
    SELECT
        N'warehouse' AS source_register,
        CONVERT(varchar(34), t._Fld7738RRef, 1) AS product_ref,
        COALESCE(NULLIF(LTRIM(RTRIM(product._Code)), N''), N'') AS product_code,
        CONVERT(varchar(34), t._Fld7742RRef, 1) AS warehouse_key,
        COALESCE(NULLIF(LTRIM(RTRIM(warehouse._Code)), N''), N'') AS warehouse_code,
        CAST(SUM(t._Fld7743) AS decimal(28, 3)) AS quantity
    FROM dbo._AccumRgT7745 AS t WITH (NOLOCK)
    LEFT JOIN dbo._Reference62 AS product WITH (NOLOCK)
        ON product._IDRRef = t._Fld7738RRef
    LEFT JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
        ON warehouse._IDRRef = t._Fld7742RRef
    WHERE t._Period = :month_start
    GROUP BY
        t._Fld7738RRef,
        product._Code,
        t._Fld7742RRef,
        warehouse._Code

    UNION ALL

    SELECT
        N'retail' AS source_register,
        CONVERT(varchar(34), t._Fld7751RRef, 1) AS product_ref,
        COALESCE(NULLIF(LTRIM(RTRIM(product._Code)), N''), N'') AS product_code,
        CONVERT(varchar(10), t._Fld7749_RTRef, 1) + N':' +
            CONVERT(varchar(34), t._Fld7749_RRRef, 1) AS warehouse_key,
        N'' AS warehouse_code,
        CAST(SUM(t._Fld7756) AS decimal(28, 3)) AS quantity
    FROM dbo._AccumRgT7759 AS t WITH (NOLOCK)
    LEFT JOIN dbo._Reference62 AS product WITH (NOLOCK)
        ON product._IDRRef = t._Fld7751RRef
    WHERE t._Period = :month_start
    GROUP BY
        t._Fld7751RRef,
        product._Code,
        t._Fld7749_RTRef,
        t._Fld7749_RRRef
    """)

MOVEMENT_SQL = text("""
    SELECT
        N'warehouse' AS source_register,
        CONVERT(varchar(34), r._Fld7738RRef, 1) AS product_ref,
        COALESCE(NULLIF(LTRIM(RTRIM(product._Code)), N''), N'') AS product_code,
        CONVERT(varchar(34), r._Fld7742RRef, 1) AS warehouse_key,
        COALESCE(NULLIF(LTRIM(RTRIM(warehouse._Code)), N''), N'') AS warehouse_code,
        CAST(r._Period AS date) AS business_date,
        CAST(SUM(CASE WHEN r._RecordKind = 0 THEN r._Fld7743 ELSE 0 END)
            AS decimal(28, 3)) AS receipt_qty,
        CAST(SUM(CASE WHEN r._RecordKind = 1 THEN r._Fld7743 ELSE 0 END)
            AS decimal(28, 3)) AS expense_qty
    FROM dbo._AccumRg7735 AS r WITH (NOLOCK)
    LEFT JOIN dbo._Reference62 AS product WITH (NOLOCK)
        ON product._IDRRef = r._Fld7738RRef
    LEFT JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
        ON warehouse._IDRRef = r._Fld7742RRef
    WHERE r._Active = 0x01
      AND r._Period >= :month_start
      AND r._Period < :date_to
    GROUP BY
        r._Fld7738RRef,
        product._Code,
        r._Fld7742RRef,
        warehouse._Code,
        CAST(r._Period AS date)

    UNION ALL

    SELECT
        N'retail' AS source_register,
        CONVERT(varchar(34), r._Fld7751RRef, 1) AS product_ref,
        COALESCE(NULLIF(LTRIM(RTRIM(product._Code)), N''), N'') AS product_code,
        CONVERT(varchar(10), r._Fld7749_RTRef, 1) + N':' +
            CONVERT(varchar(34), r._Fld7749_RRRef, 1) AS warehouse_key,
        N'' AS warehouse_code,
        CAST(r._Period AS date) AS business_date,
        CAST(SUM(CASE WHEN r._RecordKind = 0 THEN r._Fld7756 ELSE 0 END)
            AS decimal(28, 3)) AS receipt_qty,
        CAST(SUM(CASE WHEN r._RecordKind = 1 THEN r._Fld7756 ELSE 0 END)
            AS decimal(28, 3)) AS expense_qty
    FROM dbo._AccumRg7747 AS r WITH (NOLOCK)
    LEFT JOIN dbo._Reference62 AS product WITH (NOLOCK)
        ON product._IDRRef = r._Fld7751RRef
    WHERE r._Active = 0x01
      AND r._Period >= :month_start
      AND r._Period < :date_to
    GROUP BY
        r._Fld7751RRef,
        product._Code,
        r._Fld7749_RTRef,
        r._Fld7749_RRRef,
        CAST(r._Period AS date)
    """)


@dataclass(frozen=True)
class AvailabilityBuildResult:
    day_deltas: tuple[dict[str, Any], ...]
    intervals: tuple[dict[str, Any], ...]
    products: int
    warehouse_pairs: int


@dataclass(frozen=True)
class AvailabilitySyncResult:
    run_ids: tuple[int, ...]
    range_start: date
    range_end: date
    opening_rows: int
    movement_rows: int
    day_delta_rows: int
    interval_rows: int
    removed_rows: int


def month_start(value: date) -> date:
    return value.replace(day=1)


def next_month(value: date) -> date:
    return (value.replace(day=28) + timedelta(days=4)).replace(day=1)


def iter_month_ranges(date_from: date, date_to: date) -> Iterable[tuple[date, date]]:
    if date_from > date_to:
        raise ValueError("date_from_must_not_exceed_date_to")
    cursor = month_start(date_from)
    while cursor <= date_to:
        yield max(cursor, date_from), min(next_month(cursor) - timedelta(days=1), date_to)
        cursor = next_month(cursor)


def build_availability_rows(
    *,
    month: date,
    range_start: date,
    range_end: date,
    opening_rows: Sequence[Mapping[str, Any]],
    movement_rows: Sequence[Mapping[str, Any]],
) -> AvailabilityBuildResult:
    if month != month_start(month):
        raise ValueError("month_must_be_first_day")
    if not (month <= range_start <= range_end < next_month(month)):
        raise ValueError("range_must_be_inside_month")

    openings: dict[tuple[str, str, str], Decimal] = defaultdict(lambda: ZERO)
    labels: dict[tuple[str, str, str], tuple[str, str]] = {}
    movements: dict[tuple[str, str, str], dict[date, tuple[Decimal, Decimal]]] = defaultdict(dict)

    for row in opening_rows:
        key = _row_key(row)
        openings[key] += _decimal(row.get("quantity"))
        labels[key] = (_clean(row.get("product_code")), _clean(row.get("warehouse_code")))

    for row in movement_rows:
        key = _row_key(row)
        business_date = _date(row.get("business_date"))
        if business_date is None or business_date < month or business_date > range_end:
            continue
        receipt = _decimal(row.get("receipt_qty"))
        expense = _decimal(row.get("expense_qty"))
        old_receipt, old_expense = movements[key].get(business_date, (ZERO, ZERO))
        movements[key][business_date] = (old_receipt + receipt, old_expense + expense)
        labels[key] = (_clean(row.get("product_code")), _clean(row.get("warehouse_code")))

    day_deltas: list[dict[str, Any]] = []
    intervals: list[dict[str, Any]] = []
    keys = set(openings) | set(movements)
    for key in sorted(keys):
        source_register, product_ref, warehouse_key = key
        product_code, warehouse_code = labels.get(key, ("", ""))
        quantity = openings.get(key, ZERO)
        for movement_date in sorted(day for day in movements.get(key, {}) if day < range_start):
            receipt, expense = movements[key][movement_date]
            quantity = quantity + receipt - expense

        interval_from: date | None = range_start if quantity > ZERO else None
        for business_date in sorted(
            day for day in movements.get(key, {}) if range_start <= day <= range_end
        ):
            receipt, expense = movements[key][business_date]
            opening_qty = quantity
            closing_qty = opening_qty + receipt - expense
            available_day = closing_qty > ZERO or (
                opening_qty > ZERO and expense != ZERO and closing_qty <= expense
            )

            if interval_from is None and available_day:
                interval_from = business_date
            elif interval_from is not None and not available_day:
                _append_interval(
                    intervals,
                    month=month,
                    key=key,
                    product_code=product_code,
                    warehouse_code=warehouse_code,
                    available_from=interval_from,
                    available_to=business_date - timedelta(days=1),
                )
                interval_from = None

            day_deltas.append(
                {
                    "business_date": business_date,
                    "period_month": month,
                    "source_register": source_register,
                    "product_ref": product_ref,
                    "product_code": product_code,
                    "warehouse_key": warehouse_key,
                    "warehouse_code": warehouse_code,
                    "opening_qty": opening_qty,
                    "receipt_qty": receipt,
                    "expense_qty": expense,
                    "closing_qty": closing_qty,
                    "available_day": available_day,
                }
            )
            quantity = closing_qty

            if quantity <= ZERO and interval_from is not None:
                _append_interval(
                    intervals,
                    month=month,
                    key=key,
                    product_code=product_code,
                    warehouse_code=warehouse_code,
                    available_from=interval_from,
                    available_to=business_date,
                )
                interval_from = None
            elif quantity > ZERO and interval_from is None:
                interval_from = business_date

        if interval_from is not None:
            _append_interval(
                intervals,
                month=month,
                key=key,
                product_code=product_code,
                warehouse_code=warehouse_code,
                available_from=interval_from,
                available_to=range_end,
            )

    return AvailabilityBuildResult(
        day_deltas=tuple(day_deltas),
        intervals=tuple(intervals),
        products=len({key[1] for key in keys}),
        warehouse_pairs=len(keys),
    )


def sync_onec_stock_availability(
    onec_engine: Engine,
    application_engine: Engine,
    *,
    date_from: date,
    date_to: date,
    run_key: str,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> AvailabilitySyncResult:
    if date_from > date_to:
        raise ValueError("date_from_must_not_exceed_date_to")
    if retention_days < DEFAULT_HISTORY_DAYS:
        raise ValueError("retention_days_must_cover_history")
    _require_tables(application_engine)

    totals = defaultdict(int)
    run_ids: list[int] = []
    for range_start, range_end in iter_month_ranges(date_from, date_to):
        monthly_key = f"{run_key}:{month_start(range_start).isoformat()}"
        existing = _ready_run(application_engine, monthly_key)
        if existing is not None:
            run_ids.append(int(existing["id"]))
            for field in (
                "opening_rows",
                "movement_rows",
                "day_delta_rows",
                "interval_rows",
            ):
                totals[field] += int(existing.get(field) or 0)
            continue
        run_id = _start_run(
            application_engine,
            run_key=monthly_key,
            range_start=range_start,
            range_end=range_end,
        )
        try:
            month = month_start(range_start)
            query_end = datetime.combine(range_end + timedelta(days=1), datetime.min.time())
            with onec_engine.connect() as connection:
                raw_openings = [
                    dict(row)
                    for row in connection.execute(
                        OPENING_BALANCE_SQL,
                        {"month_start": datetime.combine(month, datetime.min.time())},
                    ).mappings()
                ]
                raw_movements = [
                    dict(row)
                    for row in connection.execute(
                        MOVEMENT_SQL,
                        {
                            "month_start": datetime.combine(month, datetime.min.time()),
                            "date_to": query_end,
                        },
                    ).mappings()
                ]
            built = build_availability_rows(
                month=month,
                range_start=range_start,
                range_end=range_end,
                opening_rows=raw_openings,
                movement_rows=raw_movements,
            )
            _replace_month(
                application_engine,
                run_id=run_id,
                month=month,
                range_start=range_start,
                range_end=range_end,
                built=built,
            )
            summary = {
                "products": built.products,
                "warehouse_pairs": built.warehouse_pairs,
                "retention_days": retention_days,
            }
            _finish_run(
                application_engine,
                run_id=run_id,
                status="ready",
                opening_rows=len(raw_openings),
                movement_rows=len(raw_movements),
                day_delta_rows=len(built.day_deltas),
                interval_rows=len(built.intervals),
                summary=summary,
            )
        except Exception as exc:
            _finish_run(
                application_engine,
                run_id=run_id,
                status="failed",
                error_text=str(exc)[:4000],
            )
            raise
        run_ids.append(run_id)
        totals["opening_rows"] += len(raw_openings)
        totals["movement_rows"] += len(raw_movements)
        totals["day_delta_rows"] += len(built.day_deltas)
        totals["interval_rows"] += len(built.intervals)

    removed_rows = enforce_retention(
        application_engine,
        cutoff=date_to - timedelta(days=retention_days - 1),
    )
    return AvailabilitySyncResult(
        run_ids=tuple(run_ids),
        range_start=date_from,
        range_end=date_to,
        opening_rows=totals["opening_rows"],
        movement_rows=totals["movement_rows"],
        day_delta_rows=totals["day_delta_rows"],
        interval_rows=totals["interval_rows"],
        removed_rows=removed_rows,
    )


def enforce_retention(engine: Engine, *, cutoff: date) -> int:
    with engine.begin() as connection:
        deleted_deltas = connection.execute(
            delete(DAY_DELTA_TABLE).where(DAY_DELTA_TABLE.c.business_date < cutoff)
        ).rowcount
        deleted_intervals = connection.execute(
            delete(INTERVAL_TABLE).where(INTERVAL_TABLE.c.available_to < cutoff)
        ).rowcount
        connection.execute(
            update(INTERVAL_TABLE)
            .where(INTERVAL_TABLE.c.available_from < cutoff)
            .where(INTERVAL_TABLE.c.available_to >= cutoff)
            .values(available_from=cutoff, updated_at=_utcnow())
        )
    return int(deleted_deltas or 0) + int(deleted_intervals or 0)


def fetch_effective_availability_shadow(
    engine: Engine,
    *,
    product_refs: Sequence[str],
    date_from: date,
    date_to: date,
    warehouse_codes: Sequence[str],
) -> dict[str, dict[str, Any]]:
    if not product_refs:
        return {}
    coverage = _coverage_status(engine, date_from=date_from, date_to=date_to)
    rows: list[Mapping[str, Any]] = []
    with engine.connect() as connection:
        for refs in _chunks(sorted(set(product_refs)), 1000):
            statement = (
                select(
                    INTERVAL_TABLE.c.product_ref,
                    INTERVAL_TABLE.c.warehouse_key,
                    INTERVAL_TABLE.c.warehouse_code,
                    INTERVAL_TABLE.c.available_from,
                    INTERVAL_TABLE.c.available_to,
                )
                .where(INTERVAL_TABLE.c.product_ref.in_(refs))
                .where(INTERVAL_TABLE.c.available_from <= date_to)
                .where(INTERVAL_TABLE.c.available_to >= date_from)
            )
            if warehouse_codes:
                statement = statement.where(
                    INTERVAL_TABLE.c.warehouse_code.in_(sorted(set(warehouse_codes)))
                )
            rows.extend(connection.execute(statement).mappings())

    intervals_by_product_point: dict[str, dict[str, list[tuple[date, date]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        start = max(date_from, _date(row["available_from"]) or date_from)
        end = min(date_to, _date(row["available_to"]) or date_to)
        intervals_by_product_point[str(row["product_ref"])][
            str(row["warehouse_code"] or row["warehouse_key"] or "")
        ].append((start, end))

    observed_days = (date_to - date_from).days + 1
    result: dict[str, dict[str, Any]] = {}
    for product_ref in product_refs:
        points = intervals_by_product_point.get(product_ref, {})
        point_rows = [
            {
                "warehouse_code": warehouse_code,
                "available_days": merged_interval_days(point_intervals),
                "out_of_stock_days": max(0, observed_days - merged_interval_days(point_intervals)),
            }
            for warehouse_code, point_intervals in sorted(points.items())
        ]
        result[product_ref] = {
            "schema": "effective_availability_shadow.v1",
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "observed_days": observed_days,
            "coverage_status": coverage,
            "missing_effective_availability_data": coverage != "ready",
            "points": point_rows,
        }
    return result


def fetch_days_in_sale_by_code(
    engine: Engine,
    *,
    codes: Sequence[str],
    physical_sales_point_codes: Sequence[str],
    date_to: date,
    windows_days: Sequence[int],
) -> dict[str, dict[int, Decimal]]:
    """Средние дни наличия по сети за каждое окно, ключ — код номенклатуры.

    Сумма точко-дней наличия по физическим точкам продаж, делённая на число
    точек: «сколько дней товар в среднем был на полке». Ищем по
    ``product_code``, а не по ссылке 1С, потому что ссылки в витрине и в
    классификации совпадают не у всех карточек (у 629 из 2735 на 2026-08-01
    формат отличается) — поиск по ссылке молча отдавал бы 0 дней при живых
    данных.

    Уровень расчёта здесь сетевой, а не по каждой точке отдельно, как требует
    спека: это осознанное упрощение до починки сопоставления ссылок, оно уже
    работает в автозаказе и даёт корректные числа.
    """
    if not codes or not physical_sales_point_codes:
        return {}
    windows = tuple(sorted({int(value) for value in windows_days if int(value) > 0}))
    if not windows:
        return {}
    store_count = Decimal(str(len(set(physical_sales_point_codes))))
    result: dict[str, dict[int, Decimal]] = {code: {} for code in codes}
    statement = text("""
        SELECT product_code,
            SUM(
                GREATEST(
                    0,
                    (
                        LEAST(available_to, :window_to)
                        - GREATEST(available_from, :window_from)
                    ) + 1
                )
            ) AS available_point_days
        FROM onec_stock_availability_interval
        WHERE product_code IN :codes
          AND warehouse_code IN :warehouse_codes
          AND available_from <= :window_to
          AND available_to >= :window_from
        GROUP BY product_code
        """).bindparams(
        bindparam("codes", expanding=True),
        bindparam("warehouse_codes", expanding=True),
    )
    with engine.connect() as connection:
        for window_days in windows:
            window_from = date_to - timedelta(days=window_days - 1)
            for chunk in _chunks(sorted(set(codes)), 1000):
                rows = connection.execute(
                    statement,
                    {
                        "codes": list(chunk),
                        "warehouse_codes": sorted(set(physical_sales_point_codes)),
                        "window_from": window_from,
                        "window_to": date_to,
                    },
                ).mappings()
                for row in rows:
                    code = _clean(row.get("product_code"))
                    if code not in result:
                        continue
                    point_days = Decimal(str(row.get("available_point_days") or 0))
                    result[code][window_days] = point_days / store_count
    return result


def attach_effective_availability_shadow_to_facts(
    engine: Engine,
    facts: Sequence[Mapping[str, Any]],
    *,
    date_to: date,
    history_days: int = DEFAULT_HISTORY_DAYS,
) -> list[dict[str, Any]]:
    if history_days < 1:
        raise ValueError("history_days_must_be_positive")
    if not facts:
        return []
    existing = set(sa.inspect(engine).get_table_names())
    if not {COVERAGE_TABLE.name, INTERVAL_TABLE.name}.issubset(existing):
        return [
            {
                **dict(fact),
                "effective_availability_shadow": {
                    "schema": "effective_availability_shadow.v1",
                    "coverage_status": "missing",
                    "missing_effective_availability_data": True,
                    "points": [],
                },
            }
            for fact in facts
        ]

    date_from = date_to - timedelta(days=history_days - 1)
    product_refs = sorted(
        {
            _clean(fact.get("product_ref") or fact.get("nomenclature_ref"))
            for fact in facts
            if _clean(fact.get("product_ref") or fact.get("nomenclature_ref"))
        }
    )
    warehouse_codes = _physical_sales_point_codes(facts)
    if not warehouse_codes:
        return [
            {
                **dict(fact),
                "effective_availability_shadow": {
                    "schema": "effective_availability_shadow.v1",
                    "date_from": date_from.isoformat(),
                    "date_to": date_to.isoformat(),
                    "observed_days": history_days,
                    "coverage_status": "warehouse_policy_missing",
                    "missing_effective_availability_data": True,
                    "physical_sales_point_count": 0,
                    "available_point_days": 0,
                    "out_of_stock_point_days": 0,
                    "points": [],
                },
            }
            for fact in facts
        ]
    metrics = fetch_effective_availability_shadow(
        engine,
        product_refs=product_refs,
        date_from=date_from,
        date_to=date_to,
        warehouse_codes=warehouse_codes,
    )
    result: list[dict[str, Any]] = []
    for fact in facts:
        product_ref = _clean(fact.get("product_ref") or fact.get("nomenclature_ref"))
        metric = metrics.get(
            product_ref,
            {
                "schema": "effective_availability_shadow.v1",
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "observed_days": history_days,
                "coverage_status": "partial",
                "missing_effective_availability_data": True,
                "points": [],
            },
        )
        points = metric.get("points") or []
        shadow = {
            **metric,
            "physical_sales_point_count": len(warehouse_codes),
            "available_point_days": sum(int(point.get("available_days") or 0) for point in points),
            "out_of_stock_point_days": sum(
                int(point.get("out_of_stock_days") or 0) for point in points
            ),
        }
        result.append({**dict(fact), "effective_availability_shadow": shadow})
    return result


def _append_interval(
    target: list[dict[str, Any]],
    *,
    month: date,
    key: tuple[str, str, str],
    product_code: str,
    warehouse_code: str,
    available_from: date,
    available_to: date,
) -> None:
    if available_from > available_to:
        return
    source_register, product_ref, warehouse_key = key
    target.append(
        {
            "period_month": month,
            "source_register": source_register,
            "product_ref": product_ref,
            "product_code": product_code,
            "warehouse_key": warehouse_key,
            "warehouse_code": warehouse_code,
            "available_from": available_from,
            "available_to": available_to,
        }
    )


def merged_interval_days(intervals: Sequence[tuple[date, date]]) -> int:
    merged: list[tuple[date, date]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1] + timedelta(days=1):
            merged.append((start, end))
            continue
        old_start, old_end = merged[-1]
        merged[-1] = old_start, max(old_end, end)
    return sum((end - start).days + 1 for start, end in merged)


def _replace_month(
    engine: Engine,
    *,
    run_id: int,
    month: date,
    range_start: date,
    range_end: date,
    built: AvailabilityBuildResult,
) -> None:
    now = _utcnow()
    with engine.begin() as connection:
        connection.execute(delete(DAY_DELTA_TABLE).where(DAY_DELTA_TABLE.c.period_month == month))
        connection.execute(delete(INTERVAL_TABLE).where(INTERVAL_TABLE.c.period_month == month))
        _bulk_insert(
            connection,
            DAY_DELTA_TABLE,
            [{**row, "last_run_id": run_id, "updated_at": now} for row in built.day_deltas],
        )
        _bulk_insert(
            connection,
            INTERVAL_TABLE,
            [{**row, "last_run_id": run_id, "updated_at": now} for row in built.intervals],
        )
        connection.execute(delete(COVERAGE_TABLE).where(COVERAGE_TABLE.c.period_month == month))
        connection.execute(
            insert(COVERAGE_TABLE).values(
                period_month=month,
                covered_from=range_start,
                covered_to=range_end,
                status="pending",
                last_run_id=run_id,
                updated_at=now,
            )
        )


def _bulk_insert(
    connection: Connection,
    table: Table,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    for chunk in _chunks(rows, INSERT_CHUNK_SIZE):
        connection.execute(insert(table), list(chunk))


def _ready_run(engine: Engine, run_key: str) -> Mapping[str, Any] | None:
    with engine.connect() as connection:
        return (
            connection.execute(
                select(SYNC_RUN_TABLE).where(
                    and_(
                        SYNC_RUN_TABLE.c.run_key == run_key,
                        SYNC_RUN_TABLE.c.status == "ready",
                    )
                )
            )
            .mappings()
            .first()
        )


def _start_run(
    engine: Engine,
    *,
    run_key: str,
    range_start: date,
    range_end: date,
) -> int:
    now = _utcnow()
    with engine.begin() as connection:
        existing = connection.execute(
            select(SYNC_RUN_TABLE.c.id).where(SYNC_RUN_TABLE.c.run_key == run_key)
        ).scalar_one_or_none()
        if existing is not None:
            connection.execute(
                update(SYNC_RUN_TABLE)
                .where(SYNC_RUN_TABLE.c.id == existing)
                .values(
                    range_start=range_start,
                    range_end=range_end,
                    status="running",
                    error_text=None,
                    started_at=now,
                    finished_at=None,
                )
            )
            return int(existing)
        result = connection.execute(
            insert(SYNC_RUN_TABLE).values(
                run_key=run_key,
                range_start=range_start,
                range_end=range_end,
                status="running",
                opening_rows=0,
                movement_rows=0,
                day_delta_rows=0,
                interval_rows=0,
                summary={},
                error_text=None,
                started_at=now,
                finished_at=None,
                created_at=now,
            )
        )
        return int(result.inserted_primary_key[0])


def _finish_run(
    engine: Engine,
    *,
    run_id: int,
    status: str,
    opening_rows: int = 0,
    movement_rows: int = 0,
    day_delta_rows: int = 0,
    interval_rows: int = 0,
    summary: Mapping[str, Any] | None = None,
    error_text: str | None = None,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(SYNC_RUN_TABLE)
            .where(SYNC_RUN_TABLE.c.id == run_id)
            .values(
                status=status,
                opening_rows=opening_rows,
                movement_rows=movement_rows,
                day_delta_rows=day_delta_rows,
                interval_rows=interval_rows,
                summary=dict(summary or {}),
                error_text=error_text,
                finished_at=_utcnow(),
            )
        )
        if status == "ready":
            connection.execute(
                update(COVERAGE_TABLE)
                .where(COVERAGE_TABLE.c.last_run_id == run_id)
                .values(status="ready", updated_at=_utcnow())
            )


def _coverage_status(engine: Engine, *, date_from: date, date_to: date) -> str:
    expected = {month_start(start) for start, _ in iter_month_ranges(date_from, date_to)}
    with engine.connect() as connection:
        rows = connection.execute(
            select(COVERAGE_TABLE).where(
                and_(
                    COVERAGE_TABLE.c.period_month >= month_start(date_from),
                    COVERAGE_TABLE.c.period_month <= month_start(date_to),
                    COVERAGE_TABLE.c.status == "ready",
                )
            )
        ).mappings()
        covered = {
            row["period_month"]
            for row in rows
            if row["covered_from"] <= max(date_from, row["period_month"])
            and row["covered_to"]
            >= min(date_to, next_month(row["period_month"]) - timedelta(days=1))
        }
    return "ready" if expected == covered else "partial"


def _require_tables(engine: Engine) -> None:
    existing = set(sa.inspect(engine).get_table_names())
    required = {
        SYNC_RUN_TABLE.name,
        COVERAGE_TABLE.name,
        DAY_DELTA_TABLE.name,
        INTERVAL_TABLE.name,
    }
    missing = sorted(required - existing)
    if missing:
        raise RuntimeError(f"onec_stock_availability_tables_missing:{','.join(missing)}")


def physical_sales_point_codes(warehouses: Sequence[Mapping[str, Any]]) -> list[str]:
    """Коды реальных точек продаж из политики складов.

    Отдельная публичная функция нужна тем, у кого на руках сама политика
    складов, а не собранные факты (например, ночной пересчёт классификации).
    """
    codes: set[str] = set()
    for warehouse in warehouses:
        if not isinstance(warehouse, Mapping):
            continue
        role = _clean(warehouse.get("role"))
        if role:
            include = role == "physical_sales_point"
        else:
            name = _clean(warehouse.get("name")).casefold()
            include = bool(warehouse.get("sells_systematically")) and not any(
                marker in name for marker in ("сайт", "онлайн", "оптов")
            )
        code = _clean(warehouse.get("warehouse_code") or warehouse.get("code"))
        if include and code:
            codes.add(code)
    return sorted(codes)


def _physical_sales_point_codes(facts: Sequence[Mapping[str, Any]]) -> list[str]:
    codes: set[str] = set()
    for fact in facts:
        warehouses = fact.get("warehouses")
        if not isinstance(warehouses, Sequence) or isinstance(warehouses, (str, bytes)):
            continue
        codes.update(physical_sales_point_codes(warehouses))
    return sorted(codes)


def _row_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    source_register = _clean(row.get("source_register"))
    product_ref = _clean(row.get("product_ref"))
    warehouse_key = _clean(row.get("warehouse_key"))
    if not source_register or not product_ref or not warehouse_key:
        raise ValueError("stock_row_key_is_incomplete")
    return source_register, product_ref, warehouse_key


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return ZERO
    return Decimal(str(value)).quantize(Decimal("0.001"))


def _date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _chunks(values: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
