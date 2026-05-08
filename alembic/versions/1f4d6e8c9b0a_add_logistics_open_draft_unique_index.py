"""add logistics open draft unique index

Revision ID: 1f4d6e8c9b0a
Revises: c6d7e8f9a0b1
Create Date: 2026-04-02 20:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1f4d6e8c9b0a"
down_revision: str | Sequence[str] | None = "c6d7e8f9a0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.execute(sa.text("""
            SELECT actor_user_id
            FROM logistics_draft
            WHERE status = 'open'
            GROUP BY actor_user_id
            HAVING COUNT(*) > 1
            ORDER BY actor_user_id
            """)).fetchall()
    if duplicates:
        actor_ids = ", ".join(str(row[0]) for row in duplicates)
        raise RuntimeError(
            "Cannot create unique open-draft index: duplicate open drafts exist for actor_user_id(s): "
            f"{actor_ids}"
        )

    op.create_index(
        "ix_logistics_draft_actor_open_unique",
        "logistics_draft",
        ["actor_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("ix_logistics_draft_actor_open_unique", table_name="logistics_draft")
