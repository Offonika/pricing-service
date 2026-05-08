from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import OneCSalesDailyKpi

ONEC_DAILY_SALES_KPI_SQL = """
WITH sale_store AS (
    SELECT
        v._Document203_IDRRef AS sale_doc_id,
        CONVERT(varchar(34), MAX(v._Fld4983RRef), 1) AS store_ref,
        MAX(store_ref._Description) AS store_name
    FROM dbo._Document203_VT4966 AS v
    LEFT JOIN dbo._Reference80 AS store_ref
        ON store_ref._IDRRef = v._Fld4983RRef
    GROUP BY v._Document203_IDRRef
),
sale_goods_quantity AS (
    SELECT
        v._Document203_IDRRef AS sale_doc_id,
        SUM(CAST(v._Fld4971 AS decimal(18, 3))) AS goods_qty
    FROM dbo._Document203_VT4966 AS v
    GROUP BY v._Document203_IDRRef
),
sale_delivery_quantity AS (
    SELECT
        v._Document203_IDRRef AS sale_doc_id,
        CAST(COUNT(*) AS decimal(18, 3)) AS delivery_qty
    FROM dbo._Document203_VT5002 AS v
    GROUP BY v._Document203_IDRRef
),
sale_shipping AS (
    SELECT
        v._Document203_IDRRef AS sale_doc_id,
        SUM(CAST(v._Fld5009 AS decimal(18, 2))) AS ship_vat
    FROM dbo._Document203_VT5002 AS v
    GROUP BY v._Document203_IDRRef
),
revenue_register AS (
    SELECT
        reg._RecorderTRef AS recorder_tref,
        reg._RecorderRRef AS recorder_rref,
        SUM(CAST(reg._Fld7561 AS decimal(18, 2))) AS revenue
    FROM dbo._AccumRg7550 AS reg
    WHERE reg._Active = 0x01
      AND reg._RecorderTRef IN (0x000000CB, 0x0000006D)
      AND CAST(reg._Period AS date) BETWEEN :date_from AND :date_to
    GROUP BY
        reg._RecorderTRef,
        reg._RecorderRRef
),
cost_register AS (
    SELECT
        reg._RecorderTRef AS recorder_tref,
        reg._RecorderRRef AS recorder_rref,
        SUM(CAST(reg._Fld7588 AS decimal(18, 2))) AS cost_of_sales
    FROM dbo._AccumRg7580 AS reg
    WHERE reg._Active = 0x01
      AND reg._RecorderTRef IN (0x000000CB, 0x0000006D)
      AND CAST(reg._Period AS date) BETWEEN :date_from AND :date_to
    GROUP BY
        reg._RecorderTRef,
        reg._RecorderRRef
),
return_quantity AS (
    SELECT
        reg._RecorderRRef AS return_doc_id,
        SUM(CAST(reg._Fld7743 AS decimal(18, 3))) AS quantity
    FROM dbo._AccumRg7735 AS reg
    WHERE reg._RecorderTRef = 0x0000006D
      AND CAST(reg._Period AS date) BETWEEN :date_from AND :date_to
    GROUP BY reg._RecorderRRef
),
sale_adjustment_194 AS (
    SELECT
        doc._Fld8870_RRRef AS sale_doc_id,
        SUM(CAST(reg._Fld7463 AS decimal(18, 2))) AS override_amount
    FROM dbo._Document194 AS doc
    JOIN dbo._AccumRg7453 AS reg
        ON reg._RecorderTRef = 0x000000C2
       AND reg._RecorderRRef = doc._IDRRef
    WHERE doc._Marked = 0x00
      AND doc._Posted = 0x01
      AND doc._Fld8870_RTRef = 0x000000CB
      AND CAST(doc._Date_Time AS date) BETWEEN :date_from AND :date_to
    GROUP BY doc._Fld8870_RRRef
),
sales_docs AS (
    SELECT
        CAST(sale._Date_Time AS date) AS sales_date,
        CONVERT(varchar(34), sale._Fld4950RRef, 1) AS manager_ref,
        manager_ref._Description AS manager_name,
        sale_store.store_ref AS store_ref,
        sale_store.store_name AS store_name,
        CAST(
            CASE
                WHEN sale_revenue_register.revenue IS NOT NULL THEN sale_revenue_register.revenue
                ELSE COALESCE(
                    sale_adjustment_194.override_amount,
                    CAST(sale._Fld4948 AS decimal(18, 2))
                )
                + CASE
                    WHEN sale._Fld4945 = 0x00 THEN COALESCE(sale_shipping.ship_vat, 0)
                    ELSE 0
                END
            END AS decimal(18, 2)
        ) AS revenue,
        CAST(
            COALESCE(sale_goods_quantity.goods_qty, 0)
            + COALESCE(sale_delivery_quantity.delivery_qty, 0) AS decimal(18, 3)
        ) AS sales_count,
        CAST(COALESCE(sale_cost_register.cost_of_sales, 0) AS decimal(18, 2)) AS cost_of_sales
    FROM dbo._Document203 AS sale
    LEFT JOIN sale_goods_quantity
        ON sale_goods_quantity.sale_doc_id = sale._IDRRef
    LEFT JOIN sale_delivery_quantity
        ON sale_delivery_quantity.sale_doc_id = sale._IDRRef
    LEFT JOIN sale_shipping
        ON sale_shipping.sale_doc_id = sale._IDRRef
    LEFT JOIN sale_adjustment_194
        ON sale_adjustment_194.sale_doc_id = sale._IDRRef
    LEFT JOIN revenue_register AS sale_revenue_register
        ON sale_revenue_register.recorder_tref = 0x000000CB
       AND sale_revenue_register.recorder_rref = sale._IDRRef
    LEFT JOIN cost_register AS sale_cost_register
        ON sale_cost_register.recorder_tref = 0x000000CB
       AND sale_cost_register.recorder_rref = sale._IDRRef
    LEFT JOIN sale_store
        ON sale_store.sale_doc_id = sale._IDRRef
    LEFT JOIN dbo._Reference69 AS manager_ref
        ON manager_ref._IDRRef = sale._Fld4950RRef
    WHERE sale._Marked = 0x00
      AND sale._Posted = 0x01
      AND CAST(sale._Date_Time AS date) BETWEEN :date_from AND :date_to
),
return_docs AS (
    SELECT
        CAST(ret._Date_Time AS date) AS sales_date,
        CONVERT(varchar(34), ret._Fld1689RRef, 1) AS manager_ref,
        manager_ref._Description AS manager_name,
        CONVERT(varchar(34), MAX(ret_line._Fld1716RRef), 1) AS store_ref,
        MAX(store_ref._Description) AS store_name,
        CAST(
            COALESCE(
                MAX(return_revenue_register.revenue),
                -SUM(CAST(ret_line._Fld1707 AS decimal(18, 2)))
            ) AS decimal(18, 2)
        ) AS revenue,
        COALESCE(MAX(return_quantity.quantity), CAST(0 AS decimal(18, 3))) AS sales_count,
        CAST(COALESCE(MAX(return_cost_register.cost_of_sales), 0) AS decimal(18, 2)) AS cost_of_sales
    FROM dbo._Document109 AS ret
    JOIN dbo._Document109_VT1698 AS ret_line
        ON ret_line._Document109_IDRRef = ret._IDRRef
    LEFT JOIN return_quantity
        ON return_quantity.return_doc_id = ret._IDRRef
    LEFT JOIN revenue_register AS return_revenue_register
        ON return_revenue_register.recorder_tref = 0x0000006D
       AND return_revenue_register.recorder_rref = ret._IDRRef
    LEFT JOIN cost_register AS return_cost_register
        ON return_cost_register.recorder_tref = 0x0000006D
       AND return_cost_register.recorder_rref = ret._IDRRef
    LEFT JOIN dbo._Reference69 AS manager_ref
        ON manager_ref._IDRRef = ret._Fld1689RRef
    LEFT JOIN dbo._Reference80 AS store_ref
        ON store_ref._IDRRef = ret_line._Fld1716RRef
    WHERE ret._Marked = 0x00
      AND ret._Posted = 0x01
      AND CAST(ret._Date_Time AS date) BETWEEN :date_from AND :date_to
    GROUP BY
        CAST(ret._Date_Time AS date),
        ret._Fld1689RRef,
        manager_ref._Description,
        ret._IDRRef
),
combined AS (
    SELECT * FROM sales_docs
    UNION ALL
    SELECT * FROM return_docs
)
SELECT
    sales_date,
    manager_ref,
    manager_name,
    store_ref,
    store_name,
    CAST(SUM(revenue) AS decimal(18, 2)) AS revenue,
    SUM(sales_count) AS sales_count,
    CAST(SUM(cost_of_sales) AS decimal(18, 2)) AS cost_of_sales
FROM combined
GROUP BY
    sales_date,
    manager_ref,
    manager_name,
    store_ref,
    store_name
ORDER BY sales_date, manager_name, store_name
"""


@dataclass(slots=True)
class OneCSalesDailyKpiRow:
    sales_date: date
    manager_ref: str | None
    manager_name: str | None
    store_ref: str | None
    store_name: str | None
    revenue: Decimal
    sales_count: Decimal
    cost_of_sales: Decimal


def fetch_onec_daily_sales_kpi(
    onec_engine: Engine,
    *,
    date_from: date,
    date_to: date,
) -> list[OneCSalesDailyKpiRow]:
    with onec_engine.connect() as conn:
        rows = conn.execute(
            text(ONEC_DAILY_SALES_KPI_SQL),
            {"date_from": date_from, "date_to": date_to},
        ).mappings()
        return [
            OneCSalesDailyKpiRow(
                sales_date=row["sales_date"],
                manager_ref=row["manager_ref"],
                manager_name=row["manager_name"],
                store_ref=row["store_ref"],
                store_name=row["store_name"],
                revenue=row["revenue"],
                sales_count=row["sales_count"] or Decimal("0.000"),
                cost_of_sales=row["cost_of_sales"] or Decimal("0.00"),
            )
            for row in rows
        ]


def sync_onec_daily_sales_kpi(
    session: Session,
    *,
    rows: list[OneCSalesDailyKpiRow],
    date_from: date,
    date_to: date,
) -> dict[str, int]:
    deleted = (
        session.query(OneCSalesDailyKpi)
        .filter(
            OneCSalesDailyKpi.sales_date >= date_from,
            OneCSalesDailyKpi.sales_date <= date_to,
        )
        .delete(synchronize_session=False)
    )

    for row in rows:
        session.add(
            OneCSalesDailyKpi(
                sales_date=row.sales_date,
                manager_ref=row.manager_ref,
                manager_name=row.manager_name,
                store_ref=row.store_ref,
                store_name=row.store_name,
                revenue=row.revenue,
                sales_count=row.sales_count,
                cost_of_sales=row.cost_of_sales,
            )
        )

    return {
        "deleted": deleted,
        "inserted": len(rows),
    }
