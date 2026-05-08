"""add BI daily sales KPI view"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "fc23de45bc67"
down_revision = "fb12cd34ab56"
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


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS vw_bi_sales_daily_kpi;")
