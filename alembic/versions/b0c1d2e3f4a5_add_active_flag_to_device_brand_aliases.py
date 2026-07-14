"""add active flag to device brand aliases"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "b0c1d2e3f4a5"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "device_brand_aliases",
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.create_index(
        "ix_device_brand_aliases_is_active",
        "device_brand_aliases",
        ["is_active"],
        unique=False,
    )
    op.alter_column("device_brand_aliases", "is_active", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_device_brand_aliases_is_active", table_name="device_brand_aliases")
    op.drop_column("device_brand_aliases", "is_active")
