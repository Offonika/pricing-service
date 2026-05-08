"""split product classification sources

Revision ID: 8b7c6d5e4f3a
Revises: e6b4a2c9d111
Create Date: 2026-03-12 13:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8b7c6d5e4f3a"
down_revision: str | None = "e6b4a2c9d111"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("product")}

    with op.batch_alter_table("product") as batch:
        if "subject_1c" not in cols:
            batch.add_column(sa.Column("subject_1c", sa.String(length=100), nullable=True))
        if "subject_generated" not in cols:
            batch.add_column(sa.Column("subject_generated", sa.String(length=100), nullable=True))
        if "subject_source" not in cols:
            batch.add_column(sa.Column("subject_source", sa.String(length=32), nullable=True))
        if "vid_nomenklatury_1c" not in cols:
            batch.add_column(sa.Column("vid_nomenklatury_1c", sa.String(length=128), nullable=True))
        if "vid_nomenklatury_generated" not in cols:
            batch.add_column(
                sa.Column("vid_nomenklatury_generated", sa.String(length=128), nullable=True)
            )
        if "vid_nomenklatury_source" not in cols:
            batch.add_column(
                sa.Column("vid_nomenklatury_source", sa.String(length=32), nullable=True)
            )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("product")}

    with op.batch_alter_table("product") as batch:
        if "vid_nomenklatury_source" in cols:
            batch.drop_column("vid_nomenklatury_source")
        if "vid_nomenklatury_generated" in cols:
            batch.drop_column("vid_nomenklatury_generated")
        if "vid_nomenklatury_1c" in cols:
            batch.drop_column("vid_nomenklatury_1c")
        if "subject_source" in cols:
            batch.drop_column("subject_source")
        if "subject_generated" in cols:
            batch.drop_column("subject_generated")
        if "subject_1c" in cols:
            batch.drop_column("subject_1c")
