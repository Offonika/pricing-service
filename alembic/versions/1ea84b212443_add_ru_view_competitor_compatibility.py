"""add_ru_view_competitor_compatibility

Revision ID: 1ea84b212443
Revises: 4a8a48cdbf06
Create Date: 2026-02-08 20:03:40.112206

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1ea84b212443"
down_revision: str | None = "4a8a48cdbf06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_view() -> None:
    op.execute("DROP VIEW IF EXISTS vw_competitor_item_compatibility_ru;")


def upgrade() -> None:
    _drop_view()
    op.execute("""
        CREATE VIEW vw_competitor_item_compatibility_ru AS
        SELECT
            cic.id AS id,
            cic.competitor_item_id AS competitor_item_id,
            ci.competitor AS competitor,
            ci.external_id AS external_id,
            ci.category AS category,
            ci.item_type AS item_type,
            ci.name AS name,
            ci.item_brand AS "Бренд товара",
            ci.item_manufacturer AS "Производитель товара",
            ci.parsed_device_brand AS "Бренд устройства (parsed)",
            ci.parsed_device_model AS "Модель устройства (parsed)",
            ci.parsed_device_variant AS "Вариант устройства (parsed)",
            cic.device_brand AS "Бренд совместимости",
            cic.device_model AS "Модель совместимости",
            cic.device_variant AS "Вариант совместимости",
            cic.source AS "Источник",
            cic.notes AS "Примечание",
            cic.created_at AS created_at
        FROM competitor_item_compatibility cic
        JOIN competitor_item ci ON ci.id = cic.competitor_item_id;
        """)


def downgrade() -> None:
    _drop_view()
