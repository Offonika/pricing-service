"""add alias decision reason and device type

Revision ID: 9c1d2e3f4a5b
Revises: 8b7c6d5e4f3a
Create Date: 2026-03-15 18:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c1d2e3f4a5b"
down_revision: str | None = "8b7c6d5e4f3a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("phone_model_alias")}

    with op.batch_alter_table("phone_model_alias") as batch:
        if "decision_reason" not in cols:
            batch.add_column(sa.Column("decision_reason", sa.String(length=100), nullable=True))
        if "device_type" not in cols:
            batch.add_column(
                sa.Column(
                    "device_type",
                    sa.String(length=16),
                    nullable=False,
                    server_default=sa.text("'other'"),
                )
            )

    # убираем server_default после миграции данных
    cols_after = {c["name"] for c in sa.inspect(bind).get_columns("phone_model_alias")}
    with op.batch_alter_table("phone_model_alias") as batch:
        if "device_type" in cols_after:
            batch.alter_column("device_type", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("phone_model_alias")}
    with op.batch_alter_table("phone_model_alias") as batch:
        if "device_type" in cols:
            batch.drop_column("device_type")
        if "decision_reason" in cols:
            batch.drop_column("decision_reason")
