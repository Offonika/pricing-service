"""add BI view for onec to competitor phone model mapping

Revision ID: 0aa1b2c3d4e6
Revises: fa1b2c3d4e5f
Create Date: 2026-03-26 13:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0aa1b2c3d4e6"
down_revision: str | None = "fa1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_view() -> None:
    op.execute("DROP VIEW IF EXISTS vw_bi_onec_competitor_model_map;")


def upgrade() -> None:
    _drop_view()
    op.execute("""
        CREATE VIEW vw_bi_onec_competitor_model_map AS
        SELECT
            ppm.id AS product_phone_model_link_id,
            ppm.product_id AS product_id,
            p.article AS product_article,
            p.fact_sku AS product_fact_sku,
            p.planned_sku AS product_planned_sku,
            p.code_1c AS product_code_1c,
            p.info_system_code AS product_info_system_code,
            p.name AS product_name,
            p.brand AS product_brand,
            p.category AS product_category,
            p.subject AS product_subject,
            p.is_active AS product_is_active,
            ppm.source AS product_link_source,
            ppm.raw_value AS product_raw_value,
            ppm.confidence AS product_confidence,
            ppm.is_manual AS product_is_manual,
            pm.id AS phone_model_id,
            pm.brand AS canonical_brand,
            pm.model_name AS canonical_model_name,
            pm.variant AS canonical_variant,
            pm.is_active AS canonical_is_active,
            cic.id AS competitor_compatibility_id,
            cic.competitor_item_id AS competitor_item_id,
            ci.competitor AS competitor,
            ci.external_id AS competitor_sku,
            ci.name AS competitor_name,
            ci.category AS competitor_category,
            ci.item_type AS competitor_item_type,
            ci.is_active AS competitor_is_active,
            ci.parsed_device_brand AS competitor_parsed_brand,
            ci.parsed_device_model AS competitor_parsed_model,
            ci.parsed_device_variant AS competitor_parsed_variant,
            ci.parse_status AS competitor_parse_status,
            ci.parse_error AS competitor_parse_error,
            ci.parse_version AS competitor_parse_version,
            cic.source AS competitor_link_source,
            cic.device_brand AS competitor_raw_brand,
            cic.device_model AS competitor_raw_model,
            cic.device_variant AS competitor_raw_variant,
            cic.notes AS competitor_notes,
            CASE
                WHEN cic.id IS NULL THEN 'only_1c'
                ELSE 'matched_1c_to_competitor'
            END AS mapping_status
        FROM product_phone_model ppm
        JOIN product p ON p.id = ppm.product_id
        JOIN phone_models pm ON pm.id = ppm.phone_model_id
        LEFT JOIN competitor_item_compatibility cic ON cic.phone_model_id = pm.id
        LEFT JOIN competitor_item ci ON ci.id = cic.competitor_item_id
        WHERE ppm.source = 'onec';
        """)


def downgrade() -> None:
    _drop_view()
