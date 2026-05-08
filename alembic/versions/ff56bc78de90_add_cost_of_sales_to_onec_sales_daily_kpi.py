"""add cost_of_sales to onec sales daily kpi"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "ff56bc78de90"
down_revision = "0a12bc34de56"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "onec_sales_daily_kpi",
        sa.Column("cost_of_sales", sa.Numeric(18, 2), nullable=False, server_default="0"),
    )
    op.alter_column("onec_sales_daily_kpi", "cost_of_sales", server_default=None)

    op.execute("DROP VIEW IF EXISTS vw_bi_sales_weekly_kpi;")
    op.execute("DROP VIEW IF EXISTS vw_bi_sales_daily_kpi;")
    op.execute("""
        CREATE VIEW vw_bi_sales_daily_kpi AS
        SELECT
            sales_date,
            manager_ref,
            manager_name,
            store_ref,
            store_name,
            revenue,
            sales_count,
            cost_of_sales
        FROM onec_sales_daily_kpi;
        """)
    op.execute("""
        CREATE VIEW vw_bi_sales_weekly_kpi AS
        SELECT
            date_trunc('week', sales_date)::date AS week_start,
            (date_trunc('week', sales_date)::date + INTERVAL '6 days')::date AS week_end,
            manager_ref,
            manager_name,
            store_ref,
            store_name,
            SUM(revenue)::numeric(18, 2) AS revenue,
            SUM(sales_count)::numeric(18, 3) AS sales_count,
            SUM(cost_of_sales)::numeric(18, 2) AS cost_of_sales
        FROM onec_sales_daily_kpi
        GROUP BY
            date_trunc('week', sales_date)::date,
            manager_ref,
            manager_name,
            store_ref,
            store_name;
        """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS vw_bi_sales_weekly_kpi;")
    op.execute("DROP VIEW IF EXISTS vw_bi_sales_daily_kpi;")
    op.drop_column("onec_sales_daily_kpi", "cost_of_sales")

    op.execute("""
        CREATE VIEW vw_bi_sales_daily_kpi AS
        SELECT
            sales_date,
            manager_ref,
            manager_name,
            store_ref,
            store_name,
            revenue,
            sales_count
        FROM onec_sales_daily_kpi;
        """)
    op.execute("""
        CREATE VIEW vw_bi_sales_weekly_kpi AS
        SELECT
            date_trunc('week', sales_date)::date AS week_start,
            (date_trunc('week', sales_date)::date + INTERVAL '6 days')::date AS week_end,
            manager_ref,
            manager_name,
            store_ref,
            store_name,
            SUM(revenue)::numeric(18, 2) AS revenue,
            SUM(sales_count) AS sales_count
        FROM onec_sales_daily_kpi
        GROUP BY
            date_trunc('week', sales_date)::date,
            manager_ref,
            manager_name,
            store_ref,
            store_name;
        """)
