"""deduplicate and constrain competitor item snapshots

Revision ID: 8b9c0d1e2f3a
Revises: 7a8b9c0d1e2f
Create Date: 2026-05-02 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "8b9c0d1e2f3a"
down_revision: str | Sequence[str] | None = "7a8b9c0d1e2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "competitor_item_snapshot"
CONSTRAINT_NAME = "uq_competitor_item_snapshot_item_scraped_at"


def _has_snapshot_table(inspector: sa.Inspector) -> bool:
    return TABLE_NAME in set(inspector.get_table_names())


def _has_unique_index(inspector: sa.Inspector) -> bool:
    indexes = {idx["name"] for idx in inspector.get_indexes(TABLE_NAME)}
    constraints = {idx["name"] for idx in inspector.get_unique_constraints(TABLE_NAME)}
    return CONSTRAINT_NAME in indexes or CONSTRAINT_NAME in constraints


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _has_snapshot_table(inspector):
        return

    op.execute(f"""
        DELETE FROM {TABLE_NAME}
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM {TABLE_NAME}
            GROUP BY competitor_item_id, scraped_at
        )
        """)

    inspector = sa.inspect(bind)
    if _has_unique_index(inspector):
        return

    if bind.dialect.name == "sqlite":
        op.create_index(
            CONSTRAINT_NAME,
            TABLE_NAME,
            ["competitor_item_id", "scraped_at"],
            unique=True,
        )
    else:
        op.create_unique_constraint(
            CONSTRAINT_NAME,
            TABLE_NAME,
            ["competitor_item_id", "scraped_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _has_snapshot_table(inspector):
        return
    if bind.dialect.name == "sqlite":
        op.drop_index(CONSTRAINT_NAME, table_name=TABLE_NAME)
    else:
        op.drop_constraint(CONSTRAINT_NAME, TABLE_NAME, type_="unique")
