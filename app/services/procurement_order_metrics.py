"""Read-only 1C metrics for procurement-order decisions.

The module deliberately keeps product quality separate from supplier quality.
Supplier defect metrics are only accepted when an upstream fact contains an
explicit supplier/lot attribution; the current UT 10.3 extractor does not
invent that relationship.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

DEFAULT_METRICS_WINDOW_DAYS = 180
PERCENT_QUANT = Decimal("0.01")
MONEY_QUANT = Decimal("0.01")

DEFECT_REASON_SQL = """
(
    LOWER(COALESCE(return_reason._Description, return_line._Fld8914_S, N'')) LIKE N'%брак%'
 OR LOWER(COALESCE(return_reason._Description, return_line._Fld8914_S, N'')) LIKE N'%качеств%'
 OR LOWER(COALESCE(return_reason._Description, return_line._Fld8914_S, N'')) LIKE N'%дефект%'
 OR LOWER(COALESCE(return_reason._Description, return_line._Fld8914_S, N'')) LIKE N'%неисправ%'
 OR LOWER(COALESCE(return_reason._Description, return_line._Fld8914_S, N'')) LIKE N'%некоррект%'
 OR LOWER(COALESCE(return_reason._Description, return_line._Fld8914_S, N'')) LIKE N'%полос%'
 OR LOWER(COALESCE(return_reason._Description, return_line._Fld8914_S, N'')) LIKE N'%царап%'
 OR LOWER(COALESCE(return_reason._Description, return_line._Fld8914_S, N'')) LIKE N'%разъем%'
 OR LOWER(COALESCE(return_reason._Description, return_line._Fld8914_S, N'')) LIKE N'%не работает%'
)
"""


def profitability_pct(net_sales_amount: Any, cost_amount: Any) -> Decimal | None:
    revenue = _decimal(net_sales_amount)
    cost = _decimal(cost_amount)
    if revenue <= 0:
        return None
    return ((revenue - cost) / revenue * Decimal("100")).quantize(
        PERCENT_QUANT, rounding=ROUND_HALF_UP
    )


def rate_pct(numerator: Any, denominator: Any) -> Decimal | None:
    numerator_value = _decimal(numerator)
    denominator_value = _decimal(denominator)
    if denominator_value <= 0:
        return None
    return (numerator_value / denominator_value * Decimal("100")).quantize(
        PERCENT_QUANT, rounding=ROUND_HALF_UP
    )


def price_change_pct(latest_price: Any, previous_price: Any) -> Decimal | None:
    latest = _decimal(latest_price)
    previous = _decimal(previous_price)
    if previous <= 0:
        return None
    return ((latest - previous) / previous * Decimal("100")).quantize(
        PERCENT_QUANT, rounding=ROUND_HALF_UP
    )


def defect_confidence(basis_units: Any) -> str:
    basis = max(_integer(basis_units) or 0, 0)
    if basis < 30:
        return "weak"
    if basis < 100:
        return "warning"
    return "reliable"


def build_line_metric_payload(
    *,
    product_metrics: Mapping[str, Any] | None,
    price_metrics: Mapping[str, Any] | None,
    supplier_defect_metrics: Mapping[str, Any] | None = None,
    as_of: date,
    window_days: int = DEFAULT_METRICS_WINDOW_DAYS,
) -> dict[str, Any]:
    product = dict(product_metrics or {})
    price = dict(price_metrics or {})
    supplier_defect = dict(supplier_defect_metrics or {})
    payload: dict[str, Any] = {
        "metrics_as_of": as_of.isoformat(),
        "metrics_window_days": int(window_days),
        "profitability_calculation_basis": "net_sales_amount",
        "profitability_status": "history_missing",
        "product_defect_status": "history_missing",
        "supplier_defect_attribution": "unconfirmed",
        "supplier_defect_source_status": "not_traceable",
        "price_change_status": "history_missing",
    }

    sales_qty = _decimal(product.get("sales_qty"))
    sales_amount = _money(product.get("sales_amount"))
    return_amount = _money(product.get("return_amount"))
    cost_amount = _money(product.get("cost_amount"))
    net_sales_amount = (sales_amount - return_amount).quantize(MONEY_QUANT)
    profitability = profitability_pct(net_sales_amount, cost_amount)
    if product:
        payload.update(
            {
                "profitability_source": "onec_sku_sales_cost",
                "profitability_sales_amount": _out_decimal(net_sales_amount),
                "profitability_cost_amount": _out_decimal(cost_amount),
                "profitability_status": (
                    "ready" if profitability is not None else "revenue_missing"
                ),
            }
        )
        if profitability is not None:
            payload["profitability_pct"] = _out_decimal(profitability)

    defect_qty = _decimal(product.get("defect_return_qty"))
    product_defect = rate_pct(defect_qty, sales_qty)
    if product:
        payload.update(
            {
                "product_defect_history_units": _whole_or_decimal(sales_qty),
                "product_defect_return_units": _whole_or_decimal(defect_qty),
                "product_defect_confidence": defect_confidence(sales_qty),
                "product_defect_source": "onec_customer_returns_by_sku",
                "product_defect_status": (
                    "ready" if product_defect is not None else "sales_history_missing"
                ),
                "supplier_defect_attribution": "unconfirmed",
                "supplier_defect_source_status": "not_traceable",
            }
        )
        if product_defect is not None:
            payload["product_defect_pct"] = _out_decimal(product_defect)

    if supplier_defect.get("attribution") == "supplier_exact":
        supplier_basis = _integer(supplier_defect.get("history_units")) or 0
        supplier_rate = rate_pct(
            supplier_defect.get("defect_units"),
            supplier_basis,
        )
        payload.update(
            {
                "supplier_defect_attribution": "supplier_exact",
                "supplier_defect_source_status": "ready",
                "supplier_defect_history_units": supplier_basis,
                "supplier_defect_confidence": defect_confidence(supplier_basis),
                "supplier_defect_source": str(
                    supplier_defect.get("source") or "onec_supplier_lot_trace"
                ),
            }
        )
        if supplier_rate is not None:
            payload["supplier_defect_pct"] = _out_decimal(supplier_rate)

    latest_price = _money(price.get("latest_price"))
    previous_price = _money(price.get("previous_price"))
    price_change = price_change_pct(latest_price, previous_price)
    if price.get("status") == "currency_mismatch":
        payload.update(
            {
                "price_metrics_source": "onec_posted_supplier_orders",
                "price_change_status": "currency_mismatch",
                "price_history_expected_currency": str(price.get("expected_currency") or ""),
                "price_history_available_currencies": list(price.get("available_currencies") or []),
            }
        )
        return {key: value for key, value in payload.items() if value is not None}
    if price:
        payload.update(
            {
                "price_metrics_source": "onec_posted_supplier_orders",
                "latest_historical_purchase_price": _out_decimal(latest_price),
                "previous_purchase_price": (
                    _out_decimal(previous_price) if previous_price > 0 else None
                ),
                "price_history_count": _integer(price.get("history_count")) or 0,
                "price_history_currency_ref": str(price.get("currency_ref") or "") or None,
                "price_history_latest_at": _date_time_text(price.get("latest_at")),
                "price_history_previous_at": _date_time_text(price.get("previous_at")),
                "price_change_status": "ready" if price_change is not None else "history_missing",
            }
        )
        if price_change is not None:
            payload["price_change_pct"] = _out_decimal(price_change)

    return {key: value for key, value in payload.items() if value is not None}


def fetch_procurement_line_metrics_from_onec(
    engine: Engine,
    *,
    items: Sequence[Mapping[str, Any]],
    as_of: date,
    window_days: int = DEFAULT_METRICS_WINDOW_DAYS,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    normalized_items = [
        {
            "code": _clean(item.get("nomenclature_code")),
            "supplier_ref": _clean(item.get("supplier_ref")).lower(),
            "currency": _normalize_currency(item.get("currency")),
        }
        for item in items
    ]
    codes = sorted({item["code"] for item in normalized_items if item["code"]})
    supplier_refs = sorted(
        {item["supplier_ref"] for item in normalized_items if item["supplier_ref"]}
    )
    if not codes:
        return {}

    period_end = datetime.combine(as_of, time.min)
    date_from = datetime.combine(as_of - timedelta(days=window_days), time.min)
    sales_by_code = _fetch_sales(engine, codes=codes, date_from=date_from, period_end=period_end)
    returns_by_code = _fetch_returns(
        engine,
        codes=codes,
        date_from=date_from,
        period_end=period_end,
    )
    costs_by_code = _fetch_costs(
        engine,
        codes=codes,
        date_from=date_from,
        period_end=period_end,
    )
    prices = _fetch_prices(
        engine,
        codes=codes,
        supplier_refs=supplier_refs,
        period_end=period_end,
    )
    price_currencies: dict[tuple[str, str], set[str]] = defaultdict(set)
    for price_code, price_supplier_ref, price_currency in prices:
        price_currencies[(price_code, price_supplier_ref)].add(price_currency)

    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in normalized_items:
        code = item["code"]
        supplier_ref = item["supplier_ref"]
        currency = item["currency"]
        product_metrics = {
            **sales_by_code.get(code, {}),
            **returns_by_code.get(code, {}),
            **costs_by_code.get(code, {}),
        }
        price_metrics = prices.get((code, supplier_ref, currency))
        if price_metrics is None and price_currencies.get((code, supplier_ref)):
            price_metrics = {
                "status": "currency_mismatch",
                "expected_currency": currency,
                "available_currencies": sorted(price_currencies[(code, supplier_ref)]),
            }
        result[(code, supplier_ref, currency)] = build_line_metric_payload(
            product_metrics=product_metrics or None,
            price_metrics=price_metrics,
            as_of=as_of,
            window_days=window_days,
        )
    return result


def fetch_supplier_order_counts(
    engine: Engine,
    *,
    supplier_refs: Sequence[str],
    period_end: datetime,
) -> dict[str, int]:
    refs = sorted({_clean(value).lower() for value in supplier_refs if _clean(value)})
    if not refs:
        return {}
    statement = _expanding_text(
        """
        SELECT
            LOWER(CONVERT(varchar(34), supplier_order._Fld2498RRef, 1)) AS supplier_ref,
            COUNT_BIG(DISTINCT supplier_order._IDRRef) AS history_order_count
        FROM dbo._Document133 AS supplier_order WITH (NOLOCK)
        WHERE supplier_order._Marked = 0x00
          AND supplier_order._Posted = 0x01
          AND supplier_order._Date_Time < :period_end
          AND LOWER(CONVERT(varchar(34), supplier_order._Fld2498RRef, 1))
                IN :supplier_refs
        GROUP BY LOWER(CONVERT(varchar(34), supplier_order._Fld2498RRef, 1))
        """,
        supplier_refs=refs,
    ).bindparams(bindparam("period_end", value=period_end))
    with engine.connect() as connection:
        rows = connection.execute(statement).mappings().all()
    return {
        _clean(row.get("supplier_ref")).lower(): int(row.get("history_order_count") or 0)
        for row in rows
        if _clean(row.get("supplier_ref"))
    }


def fetch_supplier_contract_terms(
    engine: Engine,
    *,
    items: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Verify exact supplier contracts without reusing customer credit controls.

    The current UT 10.3 contract catalog has no approved structured supplier-side
    payment-term attribute for this contour.  Therefore an exact contract can be
    confirmed, but its payment terms remain explicitly missing instead of being
    inferred from customer debt-limit fields.
    """

    contract_refs = sorted(
        {
            _clean(item.get("contract_ref")).lower()
            for item in items
            if _clean(item.get("contract_ref"))
        }
    )
    if not contract_refs:
        return {}
    statement = _expanding_text(
        """
        SELECT
            LOWER(CONVERT(varchar(34), contract._IDRRef, 1)) AS contract_ref,
            LOWER(CONVERT(varchar(34), contract._OwnerIDRRef, 1)) AS supplier_ref,
            NULLIF(LTRIM(RTRIM(contract._Code)), N'') AS contract_code,
            NULLIF(LTRIM(RTRIM(contract._Description)), N'') AS contract_name
        FROM dbo._Reference37 AS contract WITH (NOLOCK)
        WHERE contract._Marked = 0x00
          AND LOWER(CONVERT(varchar(34), contract._IDRRef, 1)) IN :contract_refs
        """,
        contract_refs=contract_refs,
    )
    with engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(statement).mappings()]
    return _contract_rows_to_terms(rows, items=items)


def _contract_rows_to_terms(
    rows: Sequence[Mapping[str, Any]],
    *,
    items: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    rows_by_ref = {
        _clean(row.get("contract_ref")).lower(): dict(row)
        for row in rows
        if _clean(row.get("contract_ref"))
    }
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        supplier_ref = _clean(item.get("supplier_ref")).lower()
        contract_ref = _clean(item.get("contract_ref")).lower()
        if not supplier_ref or not contract_ref:
            continue
        row = rows_by_ref.get(contract_ref)
        exact = bool(row and _clean(row.get("supplier_ref")).lower() == supplier_ref)
        result[(supplier_ref, contract_ref)] = {
            "payment_terms": None,
            "credit_days": None,
            "credit_limit": None,
            "terms_source": "onec_contract",
            "terms_status": "missing",
            "contract_ref": contract_ref,
            "contract_code": _clean((row or {}).get("contract_code")) or None,
            "contract_name": _clean((row or {}).get("contract_name")) or None,
            "contract_source_status": (
                "exact_contract_verified"
                if exact
                else "supplier_mismatch" if row else "contract_not_found"
            ),
        }
    return result


def _fetch_sales(
    engine: Engine,
    *,
    codes: Sequence[str],
    date_from: datetime,
    period_end: datetime,
) -> dict[str, dict[str, Any]]:
    statement = _expanding_text(
        """
        SELECT
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS code,
            SUM(CAST(sale_line._Fld4971 AS decimal(28, 3))) AS sales_qty,
            SUM(CAST(sale_line._Fld4982 AS decimal(28, 2))) AS sales_amount
        FROM dbo._Document203 AS sale WITH (NOLOCK)
        JOIN dbo._Document203_VT4966 AS sale_line WITH (NOLOCK)
          ON sale_line._Document203_IDRRef = sale._IDRRef
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
          ON product._IDRRef = sale_line._Fld4974RRef
        WHERE sale._Marked = 0x00
          AND sale._Posted = 0x01
          AND sale._Date_Time >= :date_from
          AND sale._Date_Time < :period_end
          AND sale_line._Fld4971 > 0
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
        GROUP BY NULLIF(LTRIM(RTRIM(product._Code)), N'')
        """,
        codes=codes,
    ).bindparams(
        bindparam("date_from", value=date_from),
        bindparam("period_end", value=period_end),
    )
    return _rows_by_code(engine, statement)


def _fetch_returns(
    engine: Engine,
    *,
    codes: Sequence[str],
    date_from: datetime,
    period_end: datetime,
) -> dict[str, dict[str, Any]]:
    statement = _expanding_text(
        f"""
        SELECT
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS code,
            SUM(ABS(CAST(return_line._Fld1707 AS decimal(28, 2)))) AS return_amount,
            SUM(
                CASE WHEN {DEFECT_REASON_SQL}
                    THEN ABS(CAST(return_line._Fld1701 AS decimal(28, 3)))
                    ELSE 0 END
            ) AS defect_return_qty
        FROM dbo._Document109 AS customer_return WITH (NOLOCK)
        JOIN dbo._Document109_VT1698 AS return_line WITH (NOLOCK)
          ON return_line._Document109_IDRRef = customer_return._IDRRef
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
          ON product._IDRRef = return_line._Fld1700RRef
        LEFT JOIN dbo._Reference8913 AS return_reason WITH (NOLOCK)
          ON return_reason._IDRRef = return_line._Fld8914_RRRef
        WHERE customer_return._Marked = 0x00
          AND customer_return._Posted = 0x01
          AND customer_return._Date_Time >= :date_from
          AND customer_return._Date_Time < :period_end
          AND return_line._Fld1701 > 0
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
        GROUP BY NULLIF(LTRIM(RTRIM(product._Code)), N'')
        """,
        codes=codes,
    ).bindparams(
        bindparam("date_from", value=date_from),
        bindparam("period_end", value=period_end),
    )
    return _rows_by_code(engine, statement)


def _fetch_costs(
    engine: Engine,
    *,
    codes: Sequence[str],
    date_from: datetime,
    period_end: datetime,
) -> dict[str, dict[str, Any]]:
    statement = _expanding_text(
        """
        SELECT
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS code,
            SUM(CAST(cost._Fld7588 AS decimal(28, 2))) AS cost_amount
        FROM dbo._AccumRg7580 AS cost WITH (NOLOCK)
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
          ON product._IDRRef = cost._Fld7581RRef
        WHERE cost._Active = 0x01
          AND cost._RecorderTRef IN (0x000000CB, 0x0000006D)
          AND cost._Period >= :date_from
          AND cost._Period < :period_end
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
        GROUP BY NULLIF(LTRIM(RTRIM(product._Code)), N'')
        """,
        codes=codes,
    ).bindparams(
        bindparam("date_from", value=date_from),
        bindparam("period_end", value=period_end),
    )
    return _rows_by_code(engine, statement)


def _fetch_prices(
    engine: Engine,
    *,
    codes: Sequence[str],
    supplier_refs: Sequence[str],
    period_end: datetime,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    if not supplier_refs:
        return {}
    statement = _expanding_text(
        """
        SELECT
            NULLIF(LTRIM(RTRIM(product._Code)), N'') AS code,
            LOWER(CONVERT(varchar(34), supplier_order._Fld2498RRef, 1)) AS supplier_ref,
            LOWER(CONVERT(varchar(34), contract._Fld498RRef, 1)) AS currency_ref,
            NULLIF(LTRIM(RTRIM(currency._Code)), N'') AS currency_code,
            NULLIF(LTRIM(RTRIM(currency._Description)), N'') AS currency_name,
            LOWER(CONVERT(varchar(34), supplier_order._IDRRef, 1)) AS order_ref,
            supplier_order._Number AS order_number,
            supplier_line._LineNo2516 AS line_number,
            CAST(supplier_line._Fld2529 AS decimal(28, 2)) AS price,
            supplier_order._Date_Time AS price_at
        FROM dbo._Document133 AS supplier_order WITH (NOLOCK)
        JOIN dbo._Document133_VT2515 AS supplier_line WITH (NOLOCK)
          ON supplier_line._Document133_IDRRef = supplier_order._IDRRef
        JOIN dbo._Reference62 AS product WITH (NOLOCK)
          ON product._IDRRef = supplier_line._Fld2523RRef
        LEFT JOIN dbo._Reference37 AS contract WITH (NOLOCK)
          ON contract._IDRRef = supplier_order._Fld2494RRef
        LEFT JOIN dbo._Reference20 AS currency WITH (NOLOCK)
          ON currency._IDRRef = contract._Fld498RRef
        WHERE supplier_order._Marked = 0x00
          AND supplier_order._Posted = 0x01
          AND supplier_order._Date_Time < :period_end
          AND supplier_line._Fld2520 > 0
          AND supplier_line._Fld2529 > 0
          AND NULLIF(LTRIM(RTRIM(product._Code)), N'') IN :codes
          AND LOWER(CONVERT(varchar(34), supplier_order._Fld2498RRef, 1))
                IN :supplier_refs
        ORDER BY code, supplier_ref, price_at DESC, order_number DESC, line_number DESC
        """,
        codes=codes,
        supplier_refs=supplier_refs,
    ).bindparams(bindparam("period_end", value=period_end))
    with engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(statement).mappings()]
    return _price_rows_to_metrics(rows)


def _price_rows_to_metrics(
    rows: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Collapse duplicate SKU lines and compare the last two distinct orders."""

    by_order: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        currency = _normalize_currency(
            row.get("currency_code"),
            row.get("currency_name"),
            row.get("currency_ref"),
        )
        order_key = (
            _clean(row.get("code")),
            _clean(row.get("supplier_ref")).lower(),
            currency,
            _clean(row.get("order_ref")).lower(),
        )
        if not all(order_key):
            continue
        candidate = dict(row)
        current = by_order.get(order_key)
        if current is None or _price_row_sort_key(candidate) > _price_row_sort_key(current):
            by_order[order_key] = candidate

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for (code, supplier_ref, currency, _order_ref), row in by_order.items():
        grouped[(code, supplier_ref, currency)].append(row)

    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for key, group_rows in grouped.items():
        group_rows.sort(key=_price_row_sort_key, reverse=True)
        result[key] = {
            "latest_price": group_rows[0].get("price"),
            "latest_at": group_rows[0].get("price_at"),
            "previous_price": group_rows[1].get("price") if len(group_rows) > 1 else None,
            "previous_at": group_rows[1].get("price_at") if len(group_rows) > 1 else None,
            "history_count": len(group_rows),
            "currency_ref": _clean(group_rows[0].get("currency_ref")).lower(),
        }
    return result


def _price_row_sort_key(row: Mapping[str, Any]) -> tuple[datetime, str, int]:
    return (
        row.get("price_at") if isinstance(row.get("price_at"), datetime) else datetime.min,
        _clean(row.get("order_number")),
        _integer(row.get("line_number")) or 0,
    )


def _normalize_currency(*values: Any) -> str:
    aliases = {
        "643": "RUB",
        "RUR": "RUB",
        "RUB": "RUB",
        "РУБ": "RUB",
        "РУБЛЬ": "RUB",
        "РУБЛИ": "RUB",
        "840": "USD",
        "USD": "USD",
        "ДОЛЛАРСША": "USD",
        "784": "AED",
        "AED": "AED",
        "978": "EUR",
        "EUR": "EUR",
        "ЕВРО": "EUR",
        "156": "CNY",
        "CNY": "CNY",
        "ЮАНЬ": "CNY",
    }
    fallback = ""
    for value in values:
        normalized = "".join(
            character for character in _clean(value).upper() if character.isalnum()
        )
        if not normalized:
            continue
        normalized = str(int(normalized)) if normalized.isdigit() else normalized
        if normalized in aliases:
            return aliases[normalized]
        fallback = fallback or normalized
    return fallback


def _rows_by_code(engine: Engine, statement: Any) -> dict[str, dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(statement).mappings().all()
    return {_clean(row.get("code")): dict(row) for row in rows if _clean(row.get("code"))}


def _expanding_text(sql: str, **values: Sequence[str]):
    statement = text(sql)
    for name, items in values.items():
        statement = statement.bindparams(bindparam(name, value=list(items), expanding=True))
    return statement


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _money(value: Any) -> Decimal:
    return _decimal(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _out_decimal(value: Decimal) -> str:
    return format(value, "f")


def _whole_or_decimal(value: Decimal) -> int | str:
    integral = value.to_integral_value()
    return int(integral) if value == integral else _out_decimal(value)


def _date_time_text(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value).strip() if value not in (None, "") else None
