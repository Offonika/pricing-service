"""add competitor item trigram search indexes

Revision ID: 9d1e2f3a4b5c
Revises: 9c0d1e2f3a4b
Create Date: 2026-05-02 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "9d1e2f3a4b5c"
down_revision: str | Sequence[str] | None = "9c0d1e2f3a4b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEXES = {
    "ix_competitor_item_name_trgm": "name",
    "ix_competitor_item_normalized_title_trgm": "normalized_title",
    "ix_competitor_item_external_id_trgm": "external_id",
    "ix_competitor_item_attrs_model_trgm": "attrs_model",
    "ix_competitor_item_parsed_device_model_trgm": "parsed_device_model",
}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for index_name, column_name in INDEXES.items():
        op.execute(f"""
            CREATE INDEX IF NOT EXISTS {index_name}
            ON competitor_item
            USING gin ({column_name} gin_trgm_ops)
            """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for index_name in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")
