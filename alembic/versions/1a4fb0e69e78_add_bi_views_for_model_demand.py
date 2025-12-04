"""add BI views for model demand analytics"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "1a4fb0e69e78"
down_revision = "7c0a6c1d6f6c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    # SQLite doesn't support CREATE OR REPLACE VIEW; drop-if-exists first.
    op.execute("DROP VIEW IF EXISTS vw_bi_model_demand_daily;")
    op.execute("DROP VIEW IF EXISTS vw_bi_model_demand_30d;")

    if is_sqlite:
        op.execute(
            """
            CREATE VIEW vw_bi_model_demand_daily AS
            SELECT
                kd.date AS date,
                pm.brand,
                pm.model_name,
                pm.variant,
                pm.id AS device_model_id,
                kd.region,
                SUM(COALESCE(kd.impressions, 0)) AS impressions,
                SUM(COALESCE(kd.clicks, 0)) AS clicks,
                COUNT(DISTINCT kw.id) AS keywords_count,
                MAX(kd.received_at) AS last_updated_at
            FROM keyword_demands kd
            JOIN keywords kw ON kd.keyword_id = kw.id
            JOIN phone_models pm ON kw.phone_model_id = pm.id
            WHERE kw.is_active = 1
              AND pm.is_active = 1
              AND kw.category = 'display'
            GROUP BY kd.date, pm.brand, pm.model_name, pm.variant, pm.id, kd.region;
            """
        )
        op.execute(
            """
            CREATE VIEW vw_bi_model_demand_30d AS
            SELECT
                pm.brand,
                pm.model_name,
                pm.variant,
                pm.id AS device_model_id,
                kd.region,
                SUM(COALESCE(kd.impressions, 0)) AS impressions_30d,
                SUM(COALESCE(kd.clicks, 0)) AS clicks_30d,
                SUM(COALESCE(kd.impressions, 0)) / 30.0 AS avg_impressions_per_day_30d,
                COUNT(DISTINCT kd.id) AS keyword_demands_count,
                NULL AS trend_flag,
                MAX(kd.date) AS last_date
            FROM keyword_demands kd
            JOIN keywords kw ON kd.keyword_id = kw.id
            JOIN phone_models pm ON kw.phone_model_id = pm.id
            WHERE kw.is_active = 1
              AND pm.is_active = 1
              AND kw.category = 'display'
              AND kd.date >= DATE('now', '-30 day')
            GROUP BY pm.brand, pm.model_name, pm.variant, pm.id, kd.region;
            """
        )
    else:
        op.execute(
            """
            CREATE VIEW vw_bi_model_demand_daily AS
            SELECT
                kd.date AS date,
                pm.brand,
                pm.model_name,
                pm.variant,
                pm.id AS device_model_id,
                kd.region,
                SUM(COALESCE(kd.impressions, 0)) AS impressions,
                SUM(COALESCE(kd.clicks, 0)) AS clicks,
                COUNT(DISTINCT kw.id) AS keywords_count,
                MAX(kd.received_at) AS last_updated_at
            FROM keyword_demands kd
            JOIN keywords kw ON kd.keyword_id = kw.id
            JOIN phone_models pm ON kw.phone_model_id = pm.id
            WHERE kw.is_active = TRUE
              AND pm.is_active = TRUE
              AND kw.category = 'display'
            GROUP BY kd.date, pm.brand, pm.model_name, pm.variant, pm.id, kd.region;
            """
        )
        op.execute(
            """
            CREATE VIEW vw_bi_model_demand_30d AS
            SELECT
                pm.brand,
                pm.model_name,
                pm.variant,
                pm.id AS device_model_id,
                kd.region,
                SUM(COALESCE(kd.impressions, 0)) AS impressions_30d,
                SUM(COALESCE(kd.clicks, 0)) AS clicks_30d,
                SUM(COALESCE(kd.impressions, 0)) / 30.0 AS avg_impressions_per_day_30d,
                COUNT(DISTINCT kd.id) AS keyword_demands_count,
                NULL::text AS trend_flag,
                MAX(kd.date) AS last_date
            FROM keyword_demands kd
            JOIN keywords kw ON kd.keyword_id = kw.id
            JOIN phone_models pm ON kw.phone_model_id = pm.id
            WHERE kw.is_active = TRUE
              AND pm.is_active = TRUE
              AND kw.category = 'display'
              AND kd.date >= CURRENT_DATE - INTERVAL '30 days'
            GROUP BY pm.brand, pm.model_name, pm.variant, pm.id, kd.region;
            """
        )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS vw_bi_model_demand_30d;")
    op.execute("DROP VIEW IF EXISTS vw_bi_model_demand_daily;")
