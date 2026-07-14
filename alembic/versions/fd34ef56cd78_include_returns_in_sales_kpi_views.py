"""include returns in sales KPI views"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "fd34ef56cd78"
down_revision = "fc23de45bc67"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS vw_bi_sales_daily_kpi;")
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

    op.execute("DROP VIEW IF EXISTS vw_bi_sales_weekly_kpi;")
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


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS vw_bi_sales_weekly_kpi;")
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
                amount_delta
            FROM receivable_ledger_event
            WHERE event_type = 'sale'
        )
        SELECT
            week_start,
            (week_start + INTERVAL '6 days')::date AS week_end,
            manager_ref,
            manager_name,
            store_ref,
            store_name,
            SUM(amount_delta)::numeric(18, 2) AS revenue,
            COUNT(DISTINCT external_document_ref) AS sales_count
        FROM sales
        GROUP BY week_start, manager_ref, manager_name, store_ref, store_name;
        """)

    op.execute("DROP VIEW IF EXISTS vw_bi_sales_daily_kpi;")
    op.execute("""
        CREATE VIEW vw_bi_sales_daily_kpi AS
        SELECT
            external_document_date::date AS sales_date,
            manager_ref,
            manager_name,
            store_ref,
            store_name,
            SUM(amount_delta)::numeric(18, 2) AS revenue,
            COUNT(DISTINCT external_document_ref) AS sales_count
        FROM receivable_ledger_event
        WHERE event_type = 'sale'
        GROUP BY
            external_document_date::date,
            manager_ref,
            manager_name,
            store_ref,
            store_name;
        """)
