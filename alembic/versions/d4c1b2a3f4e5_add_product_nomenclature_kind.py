"""add product nomenclature kind

Revision ID: d4c1b2a3f4e5
Revises: c9d1e2f3a4b5
Create Date: 2026-01-25 15:10:00.000000
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "d4c1b2a3f4e5"
down_revision = "c9d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("product", sa.Column("nomenclature_kind", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("product", "nomenclature_kind")
