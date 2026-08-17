from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import LargeBinary, bindparam, text
from sqlalchemy.engine import Engine

from app.services.onec_inventory_cost import (
    CURRENT_TOTALS_PERIOD,
    UNBILLED_PARTY_STATUS_HEX,
)
from app.services.onec_stock_availability import merged_interval_days

ZERO = Decimal("0")
DEFAULT_WINDOWS = (30, 90, 180)
MIN_RELIABLE_AVAILABILITY_DAYS = Decimal("15")
ACCELERATING_MIN_GROWTH_MULTIPLIER = Decimal("1.2")
MAX_QUERY_CODES = 1700


@dataclass(frozen=True)
class MarginFlowPolicy:
    enabled: bool = False
    status_code: str = "sale"
    speed_min_inclusive: Decimal = Decimal("0.1")
    speed_max_inclusive: Decimal = Decimal("0.25")
    profitability_min_exclusive: Decimal = Decimal("31")
    safety_stock_days: int = 25
    minimum_representation_qty: int = 13
    physical_store_count: int = 11
    central_reserve_qty: int = 2
    central_warehouse_code: str = "РБ0000010"

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.status_code:
            raise SystemExit("margin flow status_code is required")
        if self.speed_min_inclusive < ZERO:
            raise SystemExit("margin flow speed_min_inclusive must be non-negative")
        if self.speed_max_inclusive < self.speed_min_inclusive:
            raise SystemExit("margin flow speed range is invalid")
        if self.safety_stock_days < 0:
            raise SystemExit("margin flow safety_stock_days must be non-negative")
        expected_minimum = self.physical_store_count + self.central_reserve_qty
        if self.minimum_representation_qty != expected_minimum:
            raise SystemExit(
                "margin flow minimum_representation_qty must equal physical stores plus "
                "central reserve"
            )
        if not self.central_warehouse_code:
            raise SystemExit("margin flow central_warehouse_code is required")


def fetch_point_gross_sales(
    engine: Engine,
    *,
    codes: Sequence[str],
    warehouse_codes: Sequence[str],
    as_of: date,
    windows_days: Sequence[int] = DEFAULT_WINDOWS,
) -> dict[str, dict[str, dict[int, Decimal]]]:
    normalized_codes = sorted({_clean(value) for value in codes if _clean(value)})
    normalized_warehouses = sorted({_clean(value) for value in warehouse_codes if _clean(value)})
    windows = tuple(sorted({int(value) for value in windows_days if int(value) > 0}))
    if not normalized_codes or not normalized_warehouses or not windows:
        return {}
    window_columns = ",\n".join(
        f"SUM(CASE WHEN sale._Date_Time >= :window_from_{days} "
        f"THEN CAST(line._Fld4971 AS decimal(28, 3)) ELSE 0 END) AS qty_{days}"
        for days in windows
    )
    product_ref_query = text("""
        SELECT _IDRRef AS product_ref,
               NULLIF(LTRIM(RTRIM(_Code)), N'') AS product_code
        FROM dbo._Reference62 WITH (NOLOCK)
        WHERE NULLIF(LTRIM(RTRIM(_Code)), N'') IN :codes
        """).bindparams(bindparam("codes", expanding=True))
    warehouse_ref_query = text("""
        SELECT _IDRRef AS warehouse_ref,
               NULLIF(LTRIM(RTRIM(_Code)), N'') AS warehouse_code
        FROM dbo._Reference80 WITH (NOLOCK)
        WHERE NULLIF(LTRIM(RTRIM(_Code)), N'') IN :warehouse_codes
        """).bindparams(bindparam("warehouse_codes", expanding=True))
    query = text(f"""
        SELECT line._Fld4974RRef AS product_ref,
               CASE
                 WHEN line._Fld4983RRef <> 0x00000000000000000000000000000000
                 THEN line._Fld4983RRef
                 ELSE sale._Fld4940RRef
               END AS warehouse_ref,
               {window_columns}
        FROM dbo._Document203 AS sale WITH (NOLOCK)
        JOIN dbo._Document203_VT4966 AS line WITH (NOLOCK)
          ON line._Document203_IDRRef = sale._IDRRef
        WHERE sale._Marked = 0x00
          AND sale._Posted = 0x01
          AND sale._Date_Time >= :date_from
          AND sale._Date_Time < :date_to
          AND line._Fld4971 > 0
          AND line._Fld4974RRef IN :product_refs
          AND CASE
                WHEN line._Fld4983RRef <> 0x00000000000000000000000000000000
                THEN line._Fld4983RRef
                ELSE sale._Fld4940RRef
              END IN :warehouse_refs
        GROUP BY line._Fld4974RRef,
                 CASE
                   WHEN line._Fld4983RRef <> 0x00000000000000000000000000000000
                   THEN line._Fld4983RRef
                   ELSE sale._Fld4940RRef
                 END
        """).bindparams(
        bindparam("product_refs", expanding=True, type_=LargeBinary(16)),
        bindparam("warehouse_refs", expanding=True, type_=LargeBinary(16)),
    )
    params = {
        "date_from": datetime.combine(as_of - timedelta(days=max(windows) - 1), time.min),
        "date_to": datetime.combine(as_of + timedelta(days=1), time.min),
        **{
            f"window_from_{days}": datetime.combine(as_of - timedelta(days=days - 1), time.min)
            for days in windows
        },
    }
    result: dict[str, dict[str, dict[int, Decimal]]] = defaultdict(dict)
    with engine.connect() as connection:
        product_ref_to_code: dict[bytes, str] = {}
        for code_chunk in _chunks(normalized_codes):
            for row in connection.execute(product_ref_query, {"codes": code_chunk}).mappings():
                reference = bytes(row["product_ref"])
                code = _clean(row.get("product_code"))
                if len(reference) == 16 and code:
                    product_ref_to_code[reference] = code
        warehouse_ref_to_code = {
            bytes(row["warehouse_ref"]): _clean(row.get("warehouse_code"))
            for row in connection.execute(
                warehouse_ref_query,
                {"warehouse_codes": normalized_warehouses},
            ).mappings()
            if len(bytes(row["warehouse_ref"])) == 16 and _clean(row.get("warehouse_code"))
        }
        warehouse_refs = tuple(warehouse_ref_to_code)
        for product_ref_chunk in _chunks_binary(sorted(product_ref_to_code)):
            for row in connection.execute(
                query,
                {
                    **params,
                    "product_refs": product_ref_chunk,
                    "warehouse_refs": warehouse_refs,
                },
            ).mappings():
                code = product_ref_to_code.get(bytes(row["product_ref"]), "")
                warehouse = warehouse_ref_to_code.get(bytes(row["warehouse_ref"]), "")
                if code and warehouse:
                    result[code][warehouse] = {
                        days: _decimal(row.get(f"qty_{days}")) for days in windows
                    }
    return {code: dict(points) for code, points in result.items()}


def fetch_point_availability_days(
    engine: Engine,
    *,
    codes: Sequence[str],
    warehouse_codes: Sequence[str],
    as_of: date,
    windows_days: Sequence[int] = DEFAULT_WINDOWS,
) -> dict[str, dict[str, dict[int, Decimal]]]:
    normalized_codes = sorted({_clean(value) for value in codes if _clean(value)})
    normalized_warehouses = sorted({_clean(value) for value in warehouse_codes if _clean(value)})
    windows = tuple(sorted({int(value) for value in windows_days if int(value) > 0}))
    if not normalized_codes or not normalized_warehouses or not windows:
        return {}
    query = text("""
        SELECT product_code, warehouse_code, available_from, available_to
        FROM onec_stock_availability_interval
        WHERE product_code IN :codes
          AND warehouse_code IN :warehouse_codes
          AND available_from <= :window_to
          AND available_to >= :window_from
        """).bindparams(
        bindparam("codes", expanding=True),
        bindparam("warehouse_codes", expanding=True),
    )
    intervals: dict[tuple[str, str], list[tuple[date, date]]] = defaultdict(list)
    with engine.connect() as connection:
        for code_chunk in _chunks(normalized_codes):
            for row in connection.execute(
                query,
                {
                    "codes": code_chunk,
                    "warehouse_codes": normalized_warehouses,
                    "window_from": as_of - timedelta(days=max(windows) - 1),
                    "window_to": as_of,
                },
            ).mappings():
                code = _clean(row.get("product_code"))
                warehouse = _clean(row.get("warehouse_code"))
                if code and warehouse:
                    intervals[(code, warehouse)].append(
                        (row["available_from"], row["available_to"])
                    )
    result: dict[str, dict[str, dict[int, Decimal]]] = defaultdict(dict)
    for (code, warehouse), values in intervals.items():
        point: dict[int, Decimal] = {}
        for days in windows:
            window_from = as_of - timedelta(days=days - 1)
            clipped = [
                (max(start, window_from), min(end, as_of))
                for start, end in values
                if end >= window_from and start <= as_of
            ]
            if clipped:
                point[days] = Decimal(merged_interval_days(clipped))
        result[code][warehouse] = point
    return {code: dict(points) for code, points in result.items()}


def fetch_current_party_costs(
    engine: Engine,
    *,
    codes: Sequence[str],
) -> dict[str, Decimal]:
    normalized_codes = sorted({_clean(value) for value in codes if _clean(value)})
    if not normalized_codes:
        return {}
    query = text(f"""
        SELECT NULLIF(LTRIM(RTRIM(product._Code)), N'') AS product_code,
               SUM(CAST(t._Fld7462 AS decimal(28, 3))) AS party_quantity,
               SUM(CAST(t._Fld7463 AS decimal(28, 2))) AS party_amount
        FROM dbo._AccumRgT7473 AS t WITH (NOLOCK)
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
          ON product._IDRRef = t._Fld7454RRef
        WHERE t._Period = :current_period
          AND t._Fld7459RRef <> 0x{UNBILLED_PARTY_STATUS_HEX}
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
        GROUP BY NULLIF(LTRIM(RTRIM(product._Code)), N'')
        HAVING SUM(CAST(t._Fld7462 AS decimal(28, 3))) > 0
           AND SUM(CAST(t._Fld7463 AS decimal(28, 2))) >= 0
        """).bindparams(bindparam("codes", expanding=True))
    result: dict[str, Decimal] = {}
    with engine.connect() as connection:
        for code_chunk in _chunks(normalized_codes):
            for row in connection.execute(
                query,
                {"codes": code_chunk, "current_period": CURRENT_TOTALS_PERIOD},
            ).mappings():
                code = _clean(row.get("product_code"))
                quantity = _decimal(row.get("party_quantity"))
                amount = _decimal(row.get("party_amount"))
                if code and quantity > ZERO and amount >= ZERO:
                    result[code] = amount / quantity
    return result


def fetch_point_safe_free_stock(
    engine: Engine,
    *,
    codes: Sequence[str],
    warehouse_codes: Sequence[str],
    quality_names: Sequence[str],
) -> dict[str, dict[str, Any]]:
    normalized_codes = sorted({_clean(value) for value in codes if _clean(value)})
    normalized_warehouses = sorted({_clean(value) for value in warehouse_codes if _clean(value)})
    normalized_qualities = sorted({_clean(value) for value in quality_names if _clean(value)})
    if not normalized_codes or not normalized_warehouses or not normalized_qualities:
        return {}
    product_ref_query = text("""
        SELECT _IDRRef AS product_ref,
               NULLIF(LTRIM(RTRIM(_Code)), N'') AS product_code
        FROM dbo._Reference62 WITH (NOLOCK)
        WHERE NULLIF(LTRIM(RTRIM(_Code)), N'') IN :codes
        """).bindparams(bindparam("codes", expanding=True))
    warehouse_ref_query = text("""
        SELECT _IDRRef AS warehouse_ref,
               NULLIF(LTRIM(RTRIM(_Code)), N'') AS warehouse_code
        FROM dbo._Reference80 WITH (NOLOCK)
        WHERE NULLIF(LTRIM(RTRIM(_Code)), N'') IN :warehouse_codes
        """).bindparams(bindparam("warehouse_codes", expanding=True))
    quality_ref_query = text("""
        SELECT _IDRRef AS quality_ref
        FROM dbo._Reference48 WITH (NOLOCK)
        WHERE NULLIF(LTRIM(RTRIM(_Description)), N'') IN :quality_names
        """).bindparams(bindparam("quality_names", expanding=True))
    stock_query = text("""
        SELECT stock._Fld7738RRef AS product_ref,
               stock._Fld7742RRef AS warehouse_ref,
               SUM(CAST(stock._Fld7743 AS decimal(28, 3))) AS stock_qty
        FROM dbo._AccumRgT7745 AS stock WITH (NOLOCK)
        WHERE stock._Period = :current_period
          AND stock._Fld7738RRef IN :product_refs
          AND stock._Fld7742RRef IN :warehouse_refs
          AND stock._Fld7741RRef IN :quality_refs
        GROUP BY stock._Fld7738RRef, stock._Fld7742RRef
        """).bindparams(
        bindparam("product_refs", expanding=True, type_=LargeBinary(16)),
        bindparam("warehouse_refs", expanding=True, type_=LargeBinary(16)),
        bindparam("quality_refs", expanding=True, type_=LargeBinary(16)),
    )
    reserve_query = text("""
        SELECT reserve._Fld7655RRef AS product_ref,
               reserve._Fld7654RRef AS warehouse_ref,
               SUM(CAST(reserve._Fld7659 AS decimal(28, 3))) AS reserved_qty
        FROM dbo._AccumRgT7662 AS reserve WITH (NOLOCK)
        WHERE reserve._Period = :current_period
          AND reserve._Fld7657_RTRef = 0x00000084
          AND reserve._Fld7655RRef IN :product_refs
          AND reserve._Fld7654RRef IN :warehouse_refs
        GROUP BY reserve._Fld7655RRef, reserve._Fld7654RRef
        """).bindparams(
        bindparam("product_refs", expanding=True, type_=LargeBinary(16)),
        bindparam("warehouse_refs", expanding=True, type_=LargeBinary(16)),
    )
    stock: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    reserve: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    with engine.connect() as connection:
        product_ref_to_code: dict[bytes, str] = {}
        for code_chunk in _chunks(normalized_codes):
            for row in connection.execute(product_ref_query, {"codes": code_chunk}).mappings():
                reference = bytes(row["product_ref"])
                code = _clean(row.get("product_code"))
                if len(reference) == 16 and code:
                    product_ref_to_code[reference] = code
        warehouse_ref_to_code = {
            bytes(row["warehouse_ref"]): _clean(row.get("warehouse_code"))
            for row in connection.execute(
                warehouse_ref_query,
                {"warehouse_codes": normalized_warehouses},
            ).mappings()
            if len(bytes(row["warehouse_ref"])) == 16 and _clean(row.get("warehouse_code"))
        }
        quality_refs = tuple(
            bytes(row["quality_ref"])
            for row in connection.execute(
                quality_ref_query,
                {"quality_names": normalized_qualities},
            ).mappings()
            if len(bytes(row["quality_ref"])) == 16
        )
        warehouse_refs = tuple(warehouse_ref_to_code)
        if not product_ref_to_code or not warehouse_refs or not quality_refs:
            return {
                code: {
                    "point_safe_free_stock_qty": ZERO,
                    "point_safe_free_stock_by_warehouse": {
                        warehouse: ZERO for warehouse in normalized_warehouses
                    },
                }
                for code in normalized_codes
            }
        for product_ref_chunk in _chunks_binary(sorted(product_ref_to_code)):
            common = {
                "product_refs": product_ref_chunk,
                "warehouse_refs": warehouse_refs,
                "current_period": CURRENT_TOTALS_PERIOD,
            }
            for row in connection.execute(
                stock_query,
                {**common, "quality_refs": quality_refs},
            ).mappings():
                code = product_ref_to_code.get(bytes(row["product_ref"]), "")
                warehouse = warehouse_ref_to_code.get(bytes(row["warehouse_ref"]), "")
                if code and warehouse:
                    stock[(code, warehouse)] += _decimal(row.get("stock_qty"))
            for row in connection.execute(reserve_query, common).mappings():
                code = product_ref_to_code.get(bytes(row["product_ref"]), "")
                warehouse = warehouse_ref_to_code.get(bytes(row["warehouse_ref"]), "")
                if code and warehouse:
                    reserve[(code, warehouse)] += _decimal(row.get("reserved_qty"))
    result: dict[str, dict[str, Any]] = {}
    for code in normalized_codes:
        by_point = {
            warehouse: max(
                ZERO,
                stock.get((code, warehouse), ZERO) - reserve.get((code, warehouse), ZERO),
            )
            for warehouse in normalized_warehouses
        }
        result[code] = {
            "point_safe_free_stock_qty": sum(by_point.values(), ZERO),
            "point_safe_free_stock_by_warehouse": by_point,
        }
    return result


def fetch_rolling_unit_revenue(
    engine: Engine,
    *,
    codes: Sequence[str],
    as_of: date,
    history_days: int = 180,
) -> dict[str, dict[str, Decimal]]:
    normalized_codes = sorted({_clean(value) for value in codes if _clean(value)})
    if not normalized_codes or history_days <= 0:
        return {}
    query = text("""
        WITH target_organization AS (
            SELECT _IDRRef
            FROM dbo._Reference66 WITH (NOLOCK)
            WHERE _Description = N'MASTER MOBILE'
        )
        SELECT NULLIF(LTRIM(RTRIM(product._Code)), N'') AS product_code,
               SUM(CASE WHEN reg._RecorderTRef = 0x000000CB
                        THEN CAST(reg._Fld7560 AS decimal(28, 3)) ELSE 0 END)
                   AS gross_sale_qty,
               SUM(CAST(reg._Fld7561 AS decimal(28, 2))) AS net_revenue_rub
        FROM dbo._AccumRg7550 AS reg WITH (NOLOCK)
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
          ON product._IDRRef = reg._Fld7551RRef
        WHERE reg._Active = 0x01
          AND reg._RecorderTRef IN (0x000000CB, 0x0000006D)
          AND reg._Fld7558RRef IN (SELECT _IDRRef FROM target_organization)
          AND reg._Period >= :date_from
          AND reg._Period < :date_to
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
        GROUP BY NULLIF(LTRIM(RTRIM(product._Code)), N'')
        """).bindparams(bindparam("codes", expanding=True))
    result: dict[str, dict[str, Decimal]] = {}
    with engine.connect() as connection:
        for code_chunk in _chunks(normalized_codes):
            for row in connection.execute(
                query,
                {
                    "codes": code_chunk,
                    "date_from": datetime.combine(
                        as_of - timedelta(days=history_days - 1), time.min
                    ),
                    "date_to": datetime.combine(as_of + timedelta(days=1), time.min),
                },
            ).mappings():
                code = _clean(row.get("product_code"))
                if code:
                    result[code] = {
                        "gross_sale_qty": _decimal(row.get("gross_sale_qty")),
                        "net_revenue_rub": _decimal(row.get("net_revenue_rub")),
                    }
    return result


def build_margin_flow_facts(
    *,
    codes: Sequence[str],
    warehouse_codes: Sequence[str],
    point_sales: Mapping[str, Mapping[str, Mapping[int, Decimal]]],
    point_availability: Mapping[str, Mapping[str, Mapping[int, Decimal]]],
    party_costs: Mapping[str, Decimal],
    rolling_revenue: Mapping[str, Mapping[str, Decimal]],
    point_free_stock: Mapping[str, Mapping[str, Any]] | None = None,
    windows_days: Sequence[int] = DEFAULT_WINDOWS,
) -> dict[str, dict[str, Any]]:
    windows = tuple(sorted({int(value) for value in windows_days if int(value) > 0}))
    if not windows:
        return {}
    result: dict[str, dict[str, Any]] = {}
    normalized_warehouses = tuple(
        sorted({_clean(value) for value in warehouse_codes if _clean(value)})
    )
    for code in sorted({_clean(value) for value in codes if _clean(value)}):
        point_rates: dict[str, Decimal] = {}
        for warehouse in normalized_warehouses:
            sales = point_sales.get(code, {}).get(warehouse, {})
            availability = point_availability.get(code, {}).get(warehouse, {})
            point_rates[warehouse] = calculate_point_rate(
                sales=sales,
                availability_days=availability,
                windows_days=windows,
            )
        network_rate = sum(point_rates.values(), ZERO)
        party_cost = party_costs.get(code)
        economics = rolling_revenue.get(code, {})
        gross_qty = _decimal(economics.get("gross_sale_qty"))
        net_revenue = _decimal(economics.get("net_revenue_rub"))
        profitability = calculate_profitability_pct(
            gross_sale_qty=gross_qty,
            net_revenue_rub=net_revenue,
            party_cost_per_unit=party_cost,
        )
        result[code] = {
            "point_rate_sum": network_rate,
            "point_rates": point_rates,
            "party_cost_per_unit": party_cost,
            "gross_sale_qty_180": gross_qty,
            "net_revenue_rub_180": net_revenue,
            "profitability_pct": profitability,
            "point_safe_free_stock_qty": _decimal(
                (point_free_stock or {}).get(code, {}).get("point_safe_free_stock_qty")
            ),
        }
    return result


def calculate_point_rate(
    *,
    sales: Mapping[int, Decimal],
    availability_days: Mapping[int, Decimal],
    windows_days: Sequence[int] = DEFAULT_WINDOWS,
) -> Decimal:
    windows = tuple(sorted({int(value) for value in windows_days if int(value) > 0}))
    if not windows:
        return ZERO
    long_days = max(windows)
    long_available = availability_days.get(long_days)
    history_too_short = (
        long_available is not None and long_available < MIN_RELIABLE_AVAILABILITY_DAYS
    )
    rates: dict[int, Decimal] = {}
    for days in windows:
        quantity = _decimal(sales.get(days))
        window = Decimal(days)
        base_rate = quantity / window
        available = availability_days.get(days)
        if available is None or available <= ZERO or history_too_short:
            rates[days] = base_rate
            continue
        days_without_stock = max(ZERO, window - min(_decimal(available), window))
        rates[days] = (quantity + days_without_stock * base_rate) / window
    short = min(windows)
    medium = sorted(windows)[len(windows) // 2]
    accelerating = (
        rates[short] > ZERO and rates[short] >= rates[medium] * ACCELERATING_MIN_GROWTH_MULTIPLIER
    )
    value = max(rates.values()) if accelerating else sum(rates.values(), ZERO) / Decimal(len(rates))
    return value.quantize(Decimal("0.000000000001"))


def calculate_profitability_pct(
    *,
    gross_sale_qty: Decimal,
    net_revenue_rub: Decimal,
    party_cost_per_unit: Decimal | None,
) -> Decimal | None:
    cost = _decimal(party_cost_per_unit)
    quantity = _decimal(gross_sale_qty)
    revenue = _decimal(net_revenue_rub)
    if cost <= ZERO or quantity <= ZERO or revenue <= ZERO:
        return None
    return (revenue - quantity * cost) / revenue * Decimal("100")


def qualifies_for_margin_flow(
    *,
    status_code: str,
    point_rate_sum: Decimal,
    profitability_pct: Decimal | None,
    policy: MarginFlowPolicy,
) -> bool:
    return bool(
        policy.enabled
        and _clean(status_code).casefold() == policy.status_code.casefold()
        and policy.speed_min_inclusive <= point_rate_sum <= policy.speed_max_inclusive
        and profitability_pct is not None
        and profitability_pct > policy.profitability_min_exclusive
    )


def _chunks(values: Sequence[str]) -> Iterable[list[str]]:
    for offset in range(0, len(values), MAX_QUERY_CODES):
        yield list(values[offset : offset + MAX_QUERY_CODES])


def _chunks_binary(values: Sequence[bytes]) -> Iterable[list[bytes]]:
    for offset in range(0, len(values), MAX_QUERY_CODES):
        yield list(values[offset : offset + MAX_QUERY_CODES])


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value in (None, ""):
        return ZERO
    return Decimal(str(value).replace(" ", "").replace(",", "."))
