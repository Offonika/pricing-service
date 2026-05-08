"""add_ru_view_competitor_item_catalog

Revision ID: d859aea2061e
Revises: 1ea84b212443
Create Date: 2026-02-08 20:12:53.803513

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d859aea2061e"
down_revision: str | None = "1ea84b212443"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_view() -> None:
    op.execute("DROP VIEW IF EXISTS vw_competitor_item_catalog_ru;")


def upgrade() -> None:
    _drop_view()
    op.execute("""
        CREATE VIEW vw_competitor_item_catalog_ru AS
        SELECT
            ci.id AS id,
            ci.competitor AS competitor,
            ci.external_id AS external_id,
            ci.name AS name,
            ci.category AS category,
            ci.category_group AS category_group,
            ci.item_type AS item_type,
            ci.url AS url,

            -- Бренд позиции (аксессуар/запчасть): HOCO/Borofone/...
            ci.item_brand AS "Бренд товара",
            ci.item_manufacturer AS "Производитель товара",

            -- Бренд устройства (телефона), к которому относится/для которого предназначено (из парсинга)
            ci.parsed_device_brand AS "Бренд устройства (parsed)",
            ci.parsed_device_model AS "Модель устройства (parsed)",
            ci.parsed_device_variant AS "Вариант устройства (parsed)",

            ci.parse_confidence AS "Уверенность парсинга",
            ci.parse_notes AS "Заметки парсинга",

            ci.price_opt AS price_opt,
            ci.price_roz AS price_roz,
            ci.availability AS availability,

            ci.scraped_at AS scraped_at,
            ci.first_seen_at AS first_seen_at,
            ci.last_seen_at AS last_seen_at,
            ci.created_at AS created_at,
            ci.updated_at AS updated_at
        FROM competitor_item ci;
        """)


def downgrade() -> None:
    _drop_view()
