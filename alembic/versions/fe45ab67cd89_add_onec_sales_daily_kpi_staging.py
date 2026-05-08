"""add onec sales daily kpi staging"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "fe45ab67cd89"
down_revision = "fd34ef56cd78"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "onec_sales_daily_kpi",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sales_date", sa.Date(), nullable=False),
        sa.Column("manager_ref", sa.String(length=64), nullable=True),
        sa.Column("manager_name", sa.String(length=255), nullable=True),
        sa.Column("store_ref", sa.String(length=64), nullable=True),
        sa.Column("store_name", sa.String(length=255), nullable=True),
        sa.Column("revenue", sa.Numeric(18, 2), nullable=False),
        sa.Column("sales_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "sales_date",
            "manager_ref",
            "store_ref",
            name="uq_onec_sales_daily_kpi_date_manager_store",
        ),
    )
    op.create_index(
        "ix_onec_sales_daily_kpi_sales_date",
        "onec_sales_daily_kpi",
        ["sales_date"],
        unique=False,
    )
    op.create_index(
        "ix_onec_sales_daily_kpi_manager_ref",
        "onec_sales_daily_kpi",
        ["manager_ref"],
        unique=False,
    )
    op.create_index(
        "ix_onec_sales_daily_kpi_store_ref",
        "onec_sales_daily_kpi",
        ["store_ref"],
        unique=False,
    )

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


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS vw_bi_sales_weekly_kpi;")
    op.execute("DROP VIEW IF EXISTS vw_bi_sales_daily_kpi;")
    op.drop_index("ix_onec_sales_daily_kpi_store_ref", table_name="onec_sales_daily_kpi")
    op.drop_index("ix_onec_sales_daily_kpi_manager_ref", table_name="onec_sales_daily_kpi")
    op.drop_index("ix_onec_sales_daily_kpi_sales_date", table_name="onec_sales_daily_kpi")
    op.drop_table("onec_sales_daily_kpi")

    op.execute("""
        CREATE VIEW vw_bi_sales_daily_kpi AS
        SELECT
            external_document_date::date AS sales_date,
            manager_ref,
            manager_name,
            store_ref,
            store_name,
            SUM(amount_delta)::numeric(18, 2) AS revenue,
            COUNT(DISTINCT external_document_ref) FILTER (WHERE event_type = 'sale') AS sales_count
        FROM receivable_ledger_event
        WHERE event_type IN ('sale', 'return')
        GROUP BY
            external_document_date::date,
            manager_ref,
            manager_name,
            store_ref,
            store_name;
        """)
    op.execute("""
        CREATE VIEW vw_bi_sales_weekly_kpi AS
        WITH sales AS (
            SELECT
                date_trunc('week', external_document_date)::date AS week_start,
                manager_ref,
                manager_name,
                store_ref,
                store_name,
                external_document_ref,
                amount_delta,
                event_type
            FROM receivable_ledger_event
            WHERE event_type IN ('sale', 'return')
        )
        SELECT
            week_start,
            (week_start + INTERVAL '6 days')::date AS week_end,
            manager_ref,
            manager_name,
            store_ref,
            store_name,
            SUM(amount_delta)::numeric(18, 2) AS revenue,
            COUNT(DISTINCT external_document_ref) FILTER (WHERE event_type = 'sale') AS sales_count
        FROM sales
        GROUP BY week_start, manager_ref, manager_name, store_ref, store_name;
        """)
