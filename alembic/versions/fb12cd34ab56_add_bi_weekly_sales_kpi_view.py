"""add BI weekly sales KPI view"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "fb12cd34ab56"
down_revision = "fa1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    op.execute("DROP VIEW IF EXISTS vw_bi_sales_weekly_kpi;")

    if is_sqlite:
        op.execute("""
            CREATE VIEW vw_bi_sales_weekly_kpi AS
            WITH sales AS (
                SELECT
                    date(
                        external_document_date,
                        '-' || ((CAST(strftime('%w', external_document_date) AS integer) + 6) % 7) || ' days'
                    ) AS week_start,
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
                date(week_start, '+6 days') AS week_end,
                manager_ref,
                manager_name,
                store_ref,
                store_name,
                CAST(SUM(amount_delta) AS NUMERIC(18, 2)) AS revenue,
                COUNT(DISTINCT external_document_ref) AS sales_count
            FROM sales
            GROUP BY week_start, manager_ref, manager_name, store_ref, store_name;
            """)
    else:
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


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS vw_bi_sales_weekly_kpi;")
