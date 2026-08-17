from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from app.services.onec_inventory_cost import CURRENT_TOTALS_PERIOD

MAX_SQLSERVER_EXPANDING_VALUES = 1700
ZERO_REF = "0x00000000000000000000000000000000"


DISPLAY_SALE_GRAIN_SQL = text(r"""
    SELECT
      NULLIF(LTRIM(RTRIM(product._Code)), N'') AS product_code,
      sale._Date_Time AS occurred_at,
      CONVERT(varchar(34), sale._IDRRef, 1) AS document_ref,
      COALESCE(NULLIF(LTRIM(RTRIM(warehouse._Code)), N''), N'<unknown>')
        AS warehouse_code,
      COALESCE(NULLIF(LTRIM(RTRIM(warehouse._Description)), N''), N'<unknown>')
        AS warehouse_name,
      CASE
        WHEN NULLIF(LTRIM(RTRIM(customer_order._Fld2425)), N'') IS NOT NULL
        THEN N'online'
        ELSE N'offline'
      END AS demand_channel,
      COUNT_BIG(*) AS raw_line_count,
      SUM(CAST(line._Fld4971 AS decimal(28, 3))) AS quantity
    FROM dbo._Document203 AS sale WITH (NOLOCK)
    JOIN dbo._Document203_VT4966 AS line WITH (NOLOCK)
      ON line._Document203_IDRRef = sale._IDRRef
    JOIN dbo._Reference62 AS product WITH (NOLOCK)
      ON product._IDRRef = line._Fld4974RRef
    LEFT JOIN dbo._Document132 AS customer_order WITH (NOLOCK)
      ON customer_order._IDRRef = sale._Fld4939_RRRef
     AND sale._Fld4939_RTRef = 0x00000084
    LEFT JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
      ON warehouse._IDRRef = CASE
        WHEN line._Fld4983RRef <> 0x00000000000000000000000000000000
        THEN line._Fld4983RRef
        ELSE sale._Fld4940RRef
      END
    WHERE sale._Marked = 0x00
      AND sale._Posted = 0x01
      AND sale._Date_Time >= :date_from
      AND sale._Date_Time < :date_to
      AND line._Fld4971 > 0
      AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
    GROUP BY
      product._Code,
      sale._Date_Time,
      sale._IDRRef,
      warehouse._Code,
      warehouse._Description,
      CASE
        WHEN NULLIF(LTRIM(RTRIM(customer_order._Fld2425)), N'') IS NOT NULL
        THEN N'online'
        ELSE N'offline'
      END
    """).bindparams(bindparam("codes", expanding=True))


DISPLAY_RETURN_GRAIN_SQL = text(r"""
    SELECT
      NULLIF(LTRIM(RTRIM(product._Code)), N'') AS product_code,
      customer_return._Date_Time AS occurred_at,
      CONVERT(varchar(34), customer_return._IDRRef, 1) AS return_ref,
      CONVERT(varchar(34), return_line._Fld1712_RRRef, 1) AS source_sale_ref,
      COALESCE(NULLIF(LTRIM(RTRIM(warehouse._Code)), N''), N'<unknown>')
        AS warehouse_code,
      COALESCE(NULLIF(LTRIM(RTRIM(warehouse._Description)), N''), N'<unknown>')
        AS warehouse_name,
      CASE
        WHEN NULLIF(LTRIM(RTRIM(customer_order._Fld2425)), N'') IS NOT NULL
        THEN N'online'
        ELSE N'offline'
      END AS demand_channel,
      COUNT_BIG(*) AS raw_line_count,
      SUM(CAST(return_line._Fld1701 AS decimal(28, 3))) AS quantity
    FROM dbo._Document109 AS customer_return WITH (NOLOCK)
    JOIN dbo._Document109_VT1698 AS return_line WITH (NOLOCK)
      ON return_line._Document109_IDRRef = customer_return._IDRRef
    JOIN dbo._Reference62 AS product WITH (NOLOCK)
      ON product._IDRRef = return_line._Fld1700RRef
    LEFT JOIN dbo._Document203 AS sale WITH (NOLOCK)
      ON sale._IDRRef = return_line._Fld1712_RRRef
    LEFT JOIN dbo._Document132 AS customer_order WITH (NOLOCK)
      ON customer_order._IDRRef = sale._Fld4939_RRRef
     AND sale._Fld4939_RTRef = 0x00000084
    LEFT JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
      ON warehouse._IDRRef = return_line._Fld1716RRef
    WHERE customer_return._Marked = 0x00
      AND customer_return._Posted = 0x01
      AND customer_return._Date_Time >= :date_from
      AND customer_return._Date_Time < :date_to
      AND return_line._Fld1701 > 0
      AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
    GROUP BY
      product._Code,
      customer_return._Date_Time,
      customer_return._IDRRef,
      return_line._Fld1712_RRRef,
      warehouse._Code,
      warehouse._Description,
      CASE
        WHEN NULLIF(LTRIM(RTRIM(customer_order._Fld2425)), N'') IS NOT NULL
        THEN N'online'
        ELSE N'offline'
      END
    """).bindparams(bindparam("codes", expanding=True))


DISPLAY_STOCK_GRAIN_SQL = text(r"""
    SELECT
      NULLIF(LTRIM(RTRIM(product._Code)), N'') AS product_code,
      NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') AS warehouse_code,
      NULLIF(LTRIM(RTRIM(warehouse._Description)), N'') AS warehouse_name,
      CAST(SUM(stock._Fld7743) AS decimal(28, 3)) AS stock_qty
    FROM dbo._AccumRgT7745 AS stock WITH (NOLOCK)
    JOIN dbo._Reference62 AS product WITH (NOLOCK)
      ON product._IDRRef = stock._Fld7738RRef
    JOIN dbo._Reference48 AS quality WITH (NOLOCK)
      ON quality._IDRRef = stock._Fld7741RRef
    JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
      ON warehouse._IDRRef = stock._Fld7742RRef
    WHERE stock._Period = :current_period
      AND stock._Fld7743 <> 0
      AND product._Code IN :codes
      AND warehouse._Code IN :warehouse_codes
      AND quality._Description IN :quality_names
    GROUP BY product._Code, warehouse._Code, warehouse._Description
    """).bindparams(
    bindparam("codes", expanding=True),
    bindparam("warehouse_codes", expanding=True),
    bindparam("quality_names", expanding=True),
)


DISPLAY_RESERVE_GRAIN_SQL = text(r"""
    SELECT
      NULLIF(LTRIM(RTRIM(product._Code)), N'') AS product_code,
      NULLIF(LTRIM(RTRIM(warehouse._Code)), N'') AS warehouse_code,
      NULLIF(LTRIM(RTRIM(warehouse._Description)), N'') AS warehouse_name,
      CAST(SUM(reserve._Fld7659) AS decimal(28, 3)) AS reserved_qty
    FROM dbo._AccumRgT7662 AS reserve WITH (NOLOCK)
    JOIN dbo._Reference62 AS product WITH (NOLOCK)
      ON product._IDRRef = reserve._Fld7655RRef
    JOIN dbo._Reference80 AS warehouse WITH (NOLOCK)
      ON warehouse._IDRRef = reserve._Fld7654RRef
    WHERE reserve._Period = :current_period
      AND reserve._Fld7657_RTRef = 0x00000084
      AND reserve._Fld7659 <> 0
      AND product._Code IN :codes
      AND warehouse._Code IN :warehouse_codes
    GROUP BY product._Code, warehouse._Code, warehouse._Description
    """).bindparams(
    bindparam("codes", expanding=True),
    bindparam("warehouse_codes", expanding=True),
)


def fetch_display_demand_grain(
    engine: Engine,
    *,
    product_codes: Sequence[str],
    date_from: date,
    date_to_exclusive: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    codes = sorted({_clean(value) for value in product_codes if _clean(value)})
    if not codes or date_from >= date_to_exclusive:
        return [], []
    params = {
        "date_from": datetime.combine(date_from, time.min),
        "date_to": datetime.combine(date_to_exclusive, time.min),
    }
    sales: list[dict[str, Any]] = []
    returns: list[dict[str, Any]] = []
    with engine.connect() as connection:
        for chunk in _chunks(codes):
            sales.extend(
                dict(row)
                for row in connection.execute(
                    DISPLAY_SALE_GRAIN_SQL,
                    {**params, "codes": chunk},
                ).mappings()
            )
            returns.extend(
                dict(row)
                for row in connection.execute(
                    DISPLAY_RETURN_GRAIN_SQL,
                    {**params, "codes": chunk},
                ).mappings()
            )
    return sales, returns


def fetch_display_stock_grain(
    engine: Engine,
    *,
    product_codes: Sequence[str],
    warehouse_codes: Sequence[str],
    quality_names: Sequence[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    codes = sorted({_clean(value) for value in product_codes if _clean(value)})
    warehouses = sorted({_clean(value) for value in warehouse_codes if _clean(value)})
    qualities = sorted({_clean(value) for value in quality_names if _clean(value)})
    if not codes or not warehouses or not qualities:
        return [], []
    stocks: list[dict[str, Any]] = []
    reserves: list[dict[str, Any]] = []
    with engine.connect() as connection:
        for chunk in _chunks(codes):
            stocks.extend(
                dict(row)
                for row in connection.execute(
                    DISPLAY_STOCK_GRAIN_SQL,
                    {
                        "codes": chunk,
                        "warehouse_codes": warehouses,
                        "quality_names": qualities,
                        "current_period": CURRENT_TOTALS_PERIOD,
                    },
                ).mappings()
            )
            reserves.extend(
                dict(row)
                for row in connection.execute(
                    DISPLAY_RESERVE_GRAIN_SQL,
                    {
                        "codes": chunk,
                        "warehouse_codes": warehouses,
                        "current_period": CURRENT_TOTALS_PERIOD,
                    },
                ).mappings()
            )
    return stocks, reserves


def build_display_demand_location_shadow(
    sale_rows: Sequence[Mapping[str, Any]],
    return_rows: Sequence[Mapping[str, Any]],
    *,
    date_to: date,
    windows_days: Sequence[int] = (30, 90, 180),
) -> dict[str, Any]:
    windows = tuple(sorted({int(value) for value in windows_days if int(value) > 0}))
    if not windows:
        raise ValueError("demand_windows_required")
    sale_grain: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    return_grain: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    sale_duplicate_count = 0
    return_duplicate_count = 0
    legacy_return_keys: list[tuple[str, str, str]] = []
    missing_source_ref_qty = Decimal(0)
    missing_source_ref_rows = 0

    for raw in sale_rows:
        row = _normalize_demand_row(raw, is_return=False)
        key = (
            row["document_ref"],
            row["product_code"],
            row["warehouse_code"],
            row["demand_channel"],
        )
        if key in sale_grain:
            sale_duplicate_count += 1
            sale_grain[key]["quantity"] += row["quantity"]
            sale_grain[key]["raw_line_count"] += row["raw_line_count"]
            sale_grain[key]["occurred_at"] = min(sale_grain[key]["occurred_at"], row["occurred_at"])
        else:
            sale_grain[key] = row

    for raw in return_rows:
        row = _normalize_demand_row(raw, is_return=True)
        source_sale_ref = row["source_sale_ref"]
        if not source_sale_ref or source_sale_ref == ZERO_REF:
            missing_source_ref_rows += 1
            missing_source_ref_qty += row["quantity"]
        key = (
            row["return_ref"],
            source_sale_ref,
            row["product_code"],
            row["warehouse_code"],
            row["demand_channel"],
        )
        legacy_return_keys.append((row["return_ref"], row["product_code"], row["warehouse_code"]))
        if key in return_grain:
            return_duplicate_count += 1
            return_grain[key]["quantity"] += row["quantity"]
            return_grain[key]["raw_line_count"] += row["raw_line_count"]
            return_grain[key]["occurred_at"] = min(
                return_grain[key]["occurred_at"], row["occurred_at"]
            )
        else:
            return_grain[key] = row

    point_values: defaultdict[tuple[str, str, str, str], dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "gross_sale_qty": Decimal(0),
            "return_qty": Decimal(0),
            "sale_grain_row_count": 0,
            "return_grain_row_count": 0,
        }
    )
    for row in sale_grain.values():
        for window_days in windows:
            if _in_window(row["occurred_at"], date_to=date_to, window_days=window_days):
                target = point_values[
                    (
                        row["product_code"],
                        row["warehouse_code"],
                        row["warehouse_name"],
                        row["demand_channel"],
                    )
                    + (str(window_days),)
                ]
                target["gross_sale_qty"] += row["quantity"]
                target["sale_grain_row_count"] += 1
    for row in return_grain.values():
        for window_days in windows:
            if _in_window(row["occurred_at"], date_to=date_to, window_days=window_days):
                target = point_values[
                    (
                        row["product_code"],
                        row["warehouse_code"],
                        row["warehouse_name"],
                        row["demand_channel"],
                    )
                    + (str(window_days),)
                ]
                target["return_qty"] += row["quantity"]
                target["return_grain_row_count"] += 1

    facts_by_point: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    by_code: defaultdict[str, dict[str, Decimal]] = defaultdict(dict)
    totals: dict[str, dict[str, Decimal]] = {
        str(window): {
            "gross_sale_qty": Decimal(0),
            "return_qty": Decimal(0),
            "net_fulfilled_sale_qty": Decimal(0),
        }
        for window in windows
    }
    for key_with_window, value in sorted(point_values.items()):
        product_code, warehouse_code, warehouse_name, demand_channel, window = key_with_window
        key = (product_code, warehouse_code, warehouse_name, demand_channel)
        target = facts_by_point.setdefault(
            key,
            {
                "product_code": product_code,
                "warehouse_code": warehouse_code,
                "warehouse_name": warehouse_name,
                "demand_channel": demand_channel,
                "attribution_method": (
                    "site_order_link" if demand_channel == "online" else "rtu_warehouse"
                ),
            },
        )
        gross = Decimal(value["gross_sale_qty"])
        returned = Decimal(value["return_qty"])
        net = gross - returned
        target[f"gross_sale_qty_{window}d"] = gross
        target[f"return_qty_{window}d"] = returned
        target[f"net_fulfilled_sale_qty_{window}d"] = net
        target[f"sale_grain_row_count_{window}d"] = int(value["sale_grain_row_count"])
        target[f"return_grain_row_count_{window}d"] = int(value["return_grain_row_count"])
        code_target = by_code[product_code]
        for field_name, quantity in (
            (f"gross_sale_qty_{window}d", gross),
            (f"return_qty_{window}d", returned),
            (f"net_fulfilled_sale_qty_{window}d", net),
        ):
            code_target[field_name] = code_target.get(field_name, Decimal(0)) + quantity
        totals[window]["gross_sale_qty"] += gross
        totals[window]["return_qty"] += returned
        totals[window]["net_fulfilled_sale_qty"] += net

    return {
        "schema": "display_demand_location_shadow.v1",
        "date_to": date_to,
        "windows_days": list(windows),
        "facts_by_point": list(facts_by_point.values()),
        "facts_by_code": [
            {"product_code": code, **values} for code, values in sorted(by_code.items())
        ],
        "totals_by_window": totals,
        "quality": {
            "sale_input_row_count": len(sale_rows),
            "sale_canonical_grain_row_count": len(sale_grain),
            "sale_duplicate_canonical_key_count": sale_duplicate_count,
            "return_input_row_count": len(return_rows),
            "return_canonical_grain_row_count": len(return_grain),
            "return_duplicate_causal_key_count": return_duplicate_count,
            "return_legacy_key_collision_count": len(legacy_return_keys)
            - len(set(legacy_return_keys)),
            "return_missing_source_sale_ref_row_count": missing_source_ref_rows,
            "return_missing_source_sale_ref_qty": missing_source_ref_qty,
        },
    }


def build_display_stock_location_shadow(
    stock_rows: Sequence[Mapping[str, Any]],
    reserve_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    stock_by_key: defaultdict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    reserve_by_key: defaultdict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    for raw in stock_rows:
        key = _stock_key(raw)
        stock_by_key[key] += _decimal(raw.get("stock_qty"))
    for raw in reserve_rows:
        key = _stock_key(raw)
        reserve_by_key[key] += _decimal(raw.get("reserved_qty"))

    point_rows: list[dict[str, Any]] = []
    by_code: defaultdict[str, dict[str, Decimal]] = defaultdict(
        lambda: {
            "stock_qty": Decimal(0),
            "reserved_qty": Decimal(0),
            "naive_net_qty": Decimal(0),
            "point_safe_free_qty": Decimal(0),
            "uncovered_qty": Decimal(0),
        }
    )
    for key in sorted(set(stock_by_key) | set(reserve_by_key)):
        product_code, warehouse_code, warehouse_name = key
        stock_qty = stock_by_key[key]
        reserved_qty = reserve_by_key[key]
        naive_net_qty = stock_qty - reserved_qty
        point_safe_free_qty = max(naive_net_qty, Decimal(0))
        uncovered_qty = max(-naive_net_qty, Decimal(0))
        point_rows.append(
            {
                "product_code": product_code,
                "warehouse_code": warehouse_code,
                "warehouse_name": warehouse_name,
                "stock_qty": stock_qty,
                "reserved_qty": reserved_qty,
                "naive_net_qty": naive_net_qty,
                "point_safe_free_qty": point_safe_free_qty,
                "uncovered_qty": uncovered_qty,
            }
        )
        target = by_code[product_code]
        target["stock_qty"] += stock_qty
        target["reserved_qty"] += reserved_qty
        target["naive_net_qty"] += naive_net_qty
        target["point_safe_free_qty"] += point_safe_free_qty
        target["uncovered_qty"] += uncovered_qty

    code_rows = [{"product_code": code, **values} for code, values in sorted(by_code.items())]
    return {
        "schema": "display_stock_location_shadow.v1",
        "facts_by_point": point_rows,
        "facts_by_code": code_rows,
        "network": {
            field_name: sum(
                (Decimal(row[field_name]) for row in code_rows),
                Decimal(0),
            )
            for field_name in (
                "stock_qty",
                "reserved_qty",
                "naive_net_qty",
                "point_safe_free_qty",
                "uncovered_qty",
            )
        },
        "quality": {
            "point_row_count": len(point_rows),
            "negative_point_count": sum(Decimal(row["naive_net_qty"]) < 0 for row in point_rows),
        },
    }


def _normalize_demand_row(raw: Mapping[str, Any], *, is_return: bool) -> dict[str, Any]:
    occurred_at = _datetime(raw.get("occurred_at"))
    if occurred_at is None:
        raise ValueError("demand_occurred_at_required")
    product_code = _clean(raw.get("product_code"))
    warehouse_code = _clean(raw.get("warehouse_code")) or "<unknown>"
    if not product_code:
        raise ValueError("demand_product_code_required")
    quantity = _decimal(raw.get("quantity"))
    if quantity < 0:
        raise ValueError("demand_quantity_negative")
    result = {
        "occurred_at": occurred_at,
        "product_code": product_code,
        "warehouse_code": warehouse_code,
        "warehouse_name": _clean(raw.get("warehouse_name")) or warehouse_code,
        "demand_channel": _clean(raw.get("demand_channel")) or "offline",
        "quantity": quantity,
        "raw_line_count": int(raw.get("raw_line_count") or 1),
    }
    if is_return:
        result.update(
            {
                "return_ref": _clean(raw.get("return_ref")),
                "source_sale_ref": _clean(raw.get("source_sale_ref")),
            }
        )
        if not result["return_ref"]:
            raise ValueError("return_ref_required")
    else:
        result["document_ref"] = _clean(raw.get("document_ref"))
        if not result["document_ref"]:
            raise ValueError("sale_document_ref_required")
    return result


def _stock_key(raw: Mapping[str, Any]) -> tuple[str, str, str]:
    product_code = _clean(raw.get("product_code"))
    warehouse_code = _clean(raw.get("warehouse_code"))
    if not product_code or not warehouse_code:
        raise ValueError("stock_product_and_warehouse_required")
    return (
        product_code,
        warehouse_code,
        _clean(raw.get("warehouse_name")) or warehouse_code,
    )


def _in_window(occurred_at: datetime, *, date_to: date, window_days: int) -> bool:
    # Совпадает с действующим auto-order dry-run: date_from = as_of - N,
    # date_to = as_of + 1 day. Это N дней lookback плюс сам день решения.
    window_from = date_to - timedelta(days=window_days)
    return window_from <= occurred_at.date() <= date_to


def _chunks(values: Sequence[str]) -> Iterable[list[str]]:
    for offset in range(0, len(values), MAX_SQLSERVER_EXPANDING_VALUES):
        yield list(values[offset : offset + MAX_SQLSERVER_EXPANDING_VALUES])


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    text_value = _clean(value)
    if not text_value:
        return None
    return datetime.fromisoformat(text_value.replace("Z", "+00:00"))


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value in (None, ""):
        return Decimal(0)
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid_decimal:{value}") from exc


def _clean(value: Any) -> str:
    return str(value or "").strip()
