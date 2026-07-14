from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.services.receivable_decision_portrait import (
    PaymentFormMetrics,
    ProfitabilityWindowMetrics,
)
from app.services.receivables import (
    _build_ref_filter_clause,
    _clean_string,
    _hex_ref_expr,
    _to_decimal,
    _with_nolock,
)

MONEY_QUANT = Decimal("0.01")
RATIO_QUANT = Decimal("0.01")
ONEC_REF_CHUNK_SIZE = 900

ONEC_COUNTERPARTY_PROFITABILITY_SQL = """
WITH target_organization AS (
    SELECT _IDRRef
    FROM _Reference66 {nolock}
    WHERE _Description = N'MASTER MOBILE'
),
revenue_register AS (
    SELECT
        r._RecorderTRef AS recorder_tref,
        r._RecorderRRef AS recorder_rref,
        r._Fld7559RRef AS counterparty_id,
        {counterparty_ref_expr} AS counterparty_ref,
        CAST(r._Period AS date) AS event_date,
        SUM(
            CASE
                WHEN r._RecorderTRef = 0x0000006D AND r._Fld7561 > 0
                    THEN -ABS(CAST(r._Fld7561 AS decimal(18, 2)))
                ELSE CAST(r._Fld7561 AS decimal(18, 2))
            END
        ) AS revenue
    FROM _AccumRg7550 AS r {nolock}
    JOIN _Reference54 AS counterparty {nolock}
        ON counterparty._IDRRef = r._Fld7559RRef
    JOIN _Reference37 AS contract {nolock}
        ON contract._IDRRef = r._Fld7554RRef
    WHERE r._RecorderTRef IN (0x000000CB, 0x0000006D)
      AND r._Active = 0x01
      AND r._Fld7559RRef <> 0x00000000000000000000000000000000
      AND r._Fld7558RRef IN (SELECT _IDRRef FROM target_organization)
      AND contract._Fld515RRef = 0x9363c6f0a10557bf4822a55db4862286
      AND r._Period >= :start_90
      AND r._Period < :period_end
      {ref_filter}
    GROUP BY
        r._RecorderTRef,
        r._RecorderRRef,
        r._Fld7559RRef,
        counterparty._IDRRef,
        CAST(r._Period AS date)
),
cost_register AS (
    SELECT
        reg._RecorderTRef AS recorder_tref,
        reg._RecorderRRef AS recorder_rref,
        SUM(CAST(reg._Fld7588 AS decimal(18, 2))) AS cost_of_sales
    FROM _AccumRg7580 AS reg {nolock}
    WHERE reg._Active = 0x01
      AND reg._RecorderTRef IN (0x000000CB, 0x0000006D)
      AND reg._Period >= :start_90
      AND reg._Period < :period_end
    GROUP BY
        reg._RecorderTRef,
        reg._RecorderRRef
),
return_reason_totals AS (
    SELECT
        ret._IDRRef AS return_doc_id,
        SUM(ABS(CAST(ret_line._Fld1707 AS decimal(18, 2)))) AS total_return_amount,
        SUM(
            CASE
                WHEN LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%брак%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%качеств%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%дефект%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%неисправ%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%некоррект%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%полос%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%царап%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%разъем%'
                  OR LOWER(COALESCE(return_reason._Description, ret_line._Fld8914_S, N'')) LIKE N'%не работает%'
                    THEN ABS(CAST(ret_line._Fld1707 AS decimal(18, 2)))
                ELSE 0
            END
        ) AS defect_return_amount
    FROM _Document109 AS ret {nolock}
    JOIN _Document109_VT1698 AS ret_line {nolock}
        ON ret_line._Document109_IDRRef = ret._IDRRef
    LEFT JOIN _Reference8913 AS return_reason {nolock}
        ON return_reason._IDRRef = ret_line._Fld8914_RRRef
    WHERE ret._Marked = 0x00
      AND ret._Posted = 0x01
      AND ret._Date_Time >= :start_90
      AND ret._Date_Time < :period_end
    GROUP BY ret._IDRRef
),
combined AS (
    SELECT
        revenue_register.counterparty_ref,
        revenue_register.event_date,
        CAST(revenue_register.revenue AS decimal(18, 2)) AS revenue,
        CAST(COALESCE(cost_register.cost_of_sales, 0) AS decimal(18, 2)) AS cost_of_sales,
        CAST(0 AS decimal(18, 2)) AS defect_return_amount
    FROM revenue_register
    LEFT JOIN cost_register
        ON cost_register.recorder_tref = revenue_register.recorder_tref
       AND cost_register.recorder_rref = revenue_register.recorder_rref
    WHERE revenue_register.recorder_tref = 0x000000CB

    UNION ALL

    SELECT
        revenue_register.counterparty_ref,
        revenue_register.event_date,
        CAST(
            CASE
                WHEN return_reason_totals.total_return_amount > 0
                    THEN revenue_register.revenue
                         * return_reason_totals.defect_return_amount
                         / return_reason_totals.total_return_amount
                ELSE 0
            END AS decimal(18, 2)
        ) AS revenue,
        CAST(
            CASE
                WHEN return_reason_totals.total_return_amount > 0
                    THEN COALESCE(cost_register.cost_of_sales, 0)
                         * return_reason_totals.defect_return_amount
                         / return_reason_totals.total_return_amount
                ELSE 0
            END AS decimal(18, 2)
        ) AS cost_of_sales,
        CAST(return_reason_totals.defect_return_amount AS decimal(18, 2)) AS defect_return_amount
    FROM revenue_register
    JOIN return_reason_totals
        ON return_reason_totals.return_doc_id = revenue_register.recorder_rref
       AND return_reason_totals.defect_return_amount > 0
    LEFT JOIN cost_register
        ON cost_register.recorder_tref = revenue_register.recorder_tref
       AND cost_register.recorder_rref = revenue_register.recorder_rref
    WHERE revenue_register.recorder_tref = 0x0000006D
)
SELECT
    counterparty_ref,
    CAST(SUM(CASE WHEN event_date >= :start_30 THEN revenue ELSE 0 END) AS decimal(18, 2)) AS revenue_30,
    CAST(SUM(CASE WHEN event_date >= :start_60 THEN revenue ELSE 0 END) AS decimal(18, 2)) AS revenue_60,
    CAST(SUM(revenue) AS decimal(18, 2)) AS revenue_90,
    CAST(SUM(CASE WHEN event_date >= :start_30 THEN cost_of_sales ELSE 0 END) AS decimal(18, 2)) AS cost_of_sales_30,
    CAST(SUM(CASE WHEN event_date >= :start_60 THEN cost_of_sales ELSE 0 END) AS decimal(18, 2)) AS cost_of_sales_60,
    CAST(SUM(cost_of_sales) AS decimal(18, 2)) AS cost_of_sales_90,
    CAST(SUM(CASE WHEN event_date >= :start_30 THEN defect_return_amount ELSE 0 END) AS decimal(18, 2)) AS defect_return_amount_30,
    CAST(SUM(CASE WHEN event_date >= :start_60 THEN defect_return_amount ELSE 0 END) AS decimal(18, 2)) AS defect_return_amount_60,
    CAST(SUM(defect_return_amount) AS decimal(18, 2)) AS defect_return_amount_90
FROM combined
GROUP BY counterparty_ref
"""

ONEC_COUNTERPARTY_PAYMENT_FORM_SQL = """
WITH target_organization AS (
    SELECT _IDRRef
    FROM _Reference66 {nolock}
    WHERE _Description = N'MASTER MOBILE'
),
cash_payments AS (
    SELECT
        {counterparty_ref_expr} AS counterparty_ref,
        CAST(SUM(ABS(CAST(pko._Fld4688 AS decimal(18, 2)))) AS decimal(18, 2)) AS cash_amount,
        CAST(0 AS decimal(18, 2)) AS bank_amount
    FROM _Document196 AS pko {nolock}
    JOIN _Reference54 AS counterparty {nolock}
        ON counterparty._IDRRef = pko._Fld4684_RRRef
    WHERE pko._Marked = 0x00
      AND pko._Posted = 0x01
      AND pko._Fld4680RRef IN (SELECT _IDRRef FROM target_organization)
      AND pko._Fld4684_RTRef = 0x00000036
      AND pko._Fld4684_RRRef <> 0x00000000000000000000000000000000
      AND pko._Date_Time >= :start_90
      AND pko._Date_Time < :period_end
      AND pko._Fld4688 > 0
      {ref_filter}
    GROUP BY counterparty._IDRRef
),
bank_register AS (
    SELECT
        r._RecorderTRef AS recorder_tref,
        r._RecorderRRef AS recorder_rref,
        r._Fld7619RRef AS counterparty_rref,
        CAST(
            SUM(
                CASE
                    WHEN r._RecordKind = 0 THEN r._Fld7620
                    ELSE -r._Fld7620
                END
            ) AS decimal(18, 2)
        ) AS amount_delta
    FROM _AccumRg7614 AS r {nolock}
    WHERE r._Active = 0x01
      AND r._Fld7619RRef <> 0x00000000000000000000000000000000
      AND r._Fld7618RRef IN (SELECT _IDRRef FROM target_organization)
      AND r._RecorderTRef IN (0x000000BA, 0x000000A9)
      AND r._Period >= :start_90
      AND r._Period < :period_end
    GROUP BY
        r._RecorderTRef,
        r._RecorderRRef,
        r._Fld7619RRef
),
bank_payments AS (
    SELECT
        {counterparty_ref_expr} AS counterparty_ref,
        CAST(0 AS decimal(18, 2)) AS cash_amount,
        CAST(SUM(ABS(bank_register.amount_delta)) AS decimal(18, 2)) AS bank_amount
    FROM bank_register
    JOIN _Reference54 AS counterparty {nolock}
        ON counterparty._IDRRef = bank_register.counterparty_rref
    WHERE bank_register.amount_delta < 0
      {ref_filter}
    GROUP BY counterparty._IDRRef
),
combined AS (
    SELECT * FROM cash_payments
    UNION ALL
    SELECT * FROM bank_payments
)
SELECT
    counterparty_ref,
    CAST(SUM(cash_amount) AS decimal(18, 2)) AS cash_amount_90,
    CAST(SUM(bank_amount) AS decimal(18, 2)) AS bank_amount_90
FROM combined
GROUP BY counterparty_ref
"""


def fetch_counterparty_profitability_metrics_from_onec(
    onec_engine: Engine,
    *,
    snapshot_date: date,
    counterparty_refs: Sequence[str],
) -> dict[str, ProfitabilityWindowMetrics]:
    if not counterparty_refs:
        return {}
    if len(counterparty_refs) > ONEC_REF_CHUNK_SIZE:
        items: dict[str, ProfitabilityWindowMetrics] = {}
        for chunk in _chunks(counterparty_refs, ONEC_REF_CHUNK_SIZE):
            items.update(
                fetch_counterparty_profitability_metrics_from_onec(
                    onec_engine,
                    snapshot_date=snapshot_date,
                    counterparty_refs=chunk,
                )
            )
        return items
    dialect_name = onec_engine.dialect.name
    counterparty_ref_expr = _hex_ref_expr("counterparty._IDRRef", dialect_name=dialect_name)
    nolock = _with_nolock(dialect_name=dialect_name)
    ref_filter, ref_params = _build_ref_filter_clause(
        dialect_name=dialect_name,
        refs=counterparty_refs,
        column_name="counterparty._IDRRef",
        prefix="counterparty_ref",
    )
    period_start = datetime.combine(snapshot_date - timedelta(days=89), time.min)
    params: dict[str, Any] = {
        "start_30": datetime.combine(snapshot_date - timedelta(days=29), time.min),
        "start_60": datetime.combine(snapshot_date - timedelta(days=59), time.min),
        "start_90": period_start,
        "period_end": datetime.combine(snapshot_date + timedelta(days=1), time.min),
        **ref_params,
    }
    sql = ONEC_COUNTERPARTY_PROFITABILITY_SQL.format(
        counterparty_ref_expr=counterparty_ref_expr,
        nolock=nolock,
        ref_filter=f"AND {ref_filter}",
    )
    with onec_engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return build_profitability_metrics_from_rows(rows)


def fetch_counterparty_payment_form_metrics_from_onec(
    onec_engine: Engine,
    *,
    snapshot_date: date,
    counterparty_refs: Sequence[str],
) -> dict[str, PaymentFormMetrics]:
    if not counterparty_refs:
        return {}
    if len(counterparty_refs) > ONEC_REF_CHUNK_SIZE:
        items: dict[str, PaymentFormMetrics] = {}
        for chunk in _chunks(counterparty_refs, ONEC_REF_CHUNK_SIZE):
            items.update(
                fetch_counterparty_payment_form_metrics_from_onec(
                    onec_engine,
                    snapshot_date=snapshot_date,
                    counterparty_refs=chunk,
                )
            )
        return items
    dialect_name = onec_engine.dialect.name
    counterparty_ref_expr = _hex_ref_expr("counterparty._IDRRef", dialect_name=dialect_name)
    nolock = _with_nolock(dialect_name=dialect_name)
    ref_filter, ref_params = _build_ref_filter_clause(
        dialect_name=dialect_name,
        refs=counterparty_refs,
        column_name="counterparty._IDRRef",
        prefix="payment_form_ref",
    )
    params: dict[str, Any] = {
        "start_90": datetime.combine(snapshot_date - timedelta(days=89), time.min),
        "period_end": datetime.combine(snapshot_date + timedelta(days=1), time.min),
        **ref_params,
    }
    sql = ONEC_COUNTERPARTY_PAYMENT_FORM_SQL.format(
        counterparty_ref_expr=counterparty_ref_expr,
        nolock=nolock,
        ref_filter=f"AND {ref_filter}",
    )
    with onec_engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return build_payment_form_metrics_from_rows(rows)


def build_profitability_metrics_from_rows(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, ProfitabilityWindowMetrics]:
    items: dict[str, ProfitabilityWindowMetrics] = {}
    for row in rows:
        counterparty_ref = _clean_string(row.get("counterparty_ref"))
        if not counterparty_ref:
            continue
        revenue_30 = _money(_to_decimal(row.get("revenue_30")))
        revenue_60 = _money(_to_decimal(row.get("revenue_60")))
        revenue_90 = _money(_to_decimal(row.get("revenue_90")))
        cost_30 = _money(_to_decimal(row.get("cost_of_sales_30")))
        cost_60 = _money(_to_decimal(row.get("cost_of_sales_60")))
        cost_90 = _money(_to_decimal(row.get("cost_of_sales_90")))
        gross_profit_30 = _money(revenue_30 - cost_30)
        gross_profit_60 = _money(revenue_60 - cost_60)
        gross_profit_90 = _money(revenue_90 - cost_90)
        items[counterparty_ref.upper()] = ProfitabilityWindowMetrics(
            revenue_30=revenue_30,
            revenue_60=revenue_60,
            revenue_90=revenue_90,
            cost_of_sales_30=cost_30,
            cost_of_sales_60=cost_60,
            cost_of_sales_90=cost_90,
            gross_profit_30=gross_profit_30,
            gross_profit_60=gross_profit_60,
            gross_profit_90=gross_profit_90,
            gross_margin_pct_90=_percent(gross_profit_90, revenue_90),
            profitability_pct_90=_percent(gross_profit_90, cost_90),
            defect_return_amount_30=_money(_to_decimal(row.get("defect_return_amount_30"))),
            defect_return_amount_60=_money(_to_decimal(row.get("defect_return_amount_60"))),
            defect_return_amount_90=_money(_to_decimal(row.get("defect_return_amount_90"))),
            source_status="ready",
            source_note=(
                "1С read-only: выручка из _AccumRg7550, себестоимость из _AccumRg7580, "
                "возвраты только по причинам брака/качества из _Document109_VT1698._Fld8914_*."
            ),
        )
    return items


def build_payment_form_metrics_from_rows(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, PaymentFormMetrics]:
    items: dict[str, PaymentFormMetrics] = {}
    for row in rows:
        counterparty_ref = _clean_string(row.get("counterparty_ref"))
        if not counterparty_ref:
            continue
        cash_amount = _money(_to_decimal(row.get("cash_amount_90")))
        bank_amount = _money(_to_decimal(row.get("bank_amount_90")))
        total_amount = cash_amount + bank_amount
        if total_amount <= 0:
            continue
        cash_share = _percent(cash_amount, total_amount)
        bank_share = _percent(bank_amount, total_amount)
        if cash_share is not None and cash_share >= Decimal("70.00"):
            primary = "cash"
        elif bank_share is not None and bank_share >= Decimal("70.00"):
            primary = "bank"
        else:
            primary = "mixed"
        items[counterparty_ref.upper()] = PaymentFormMetrics(
            payment_form_primary=primary,
            cash_share_90=cash_share,
            bank_share_90=bank_share,
            source_status="ready",
            source_note=(
                "1С read-only: наличные из _Document196, безнал/эквайринг из "
                "_AccumRg7614 recorder types 0x000000BA/0x000000A9."
            ),
        )
    return items


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _percent(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == 0:
        return None
    return (numerator / denominator * Decimal("100")).quantize(
        RATIO_QUANT,
        rounding=ROUND_HALF_UP,
    )


def _chunks(values: Sequence[str], size: int):
    for index in range(0, len(values), size):
        yield values[index : index + size]
