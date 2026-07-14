"""make sales count numeric in onec sales kpi"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0a12bc34de56"
down_revision = "fe45ab67cd89"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS vw_bi_sales_weekly_kpi;")
    op.execute("DROP VIEW IF EXISTS vw_bi_sales_daily_kpi;")

    op.alter_column(
        "onec_sales_daily_kpi",
        "sales_count",
        existing_type=sa.Integer(),
        type_=sa.Numeric(18, 3),
        existing_nullable=False,
        existing_server_default="0",
        postgresql_using="sales_count::numeric(18, 3)",
    )

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
            SUM(sales_count)::numeric(18, 3) AS sales_count
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

    op.alter_column(
        "onec_sales_daily_kpi",
        "sales_count",
        existing_type=sa.Numeric(18, 3),
        type_=sa.Integer(),
        existing_nullable=False,
        existing_server_default="0",
        postgresql_using="round(sales_count)::integer",
    )

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
