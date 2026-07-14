"""rename product nomenclature kind column

Revision ID: e2b3c4d5f6a7
Revises: d4c1b2a3f4e5
Create Date: 2026-01-25 15:25:00.000000
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e2b3c4d5f6a7"
down_revision = "d4c1b2a3f4e5"
branch_labels = None
depends_on = None

OLD_COLUMN = "nomenclature_kind"
NEW_COLUMN = "Вид_номенклатуры"


def upgrade() -> None:
    op.alter_column("product", OLD_COLUMN, new_column_name=NEW_COLUMN)
    op.alter_column(
        "product",
        NEW_COLUMN,
        existing_type=sa.String(length=32),
        type_=sa.String(length=128),
    )


def downgrade() -> None:
    op.alter_column(
        "product",
        NEW_COLUMN,
        existing_type=sa.String(length=128),
        type_=sa.String(length=32),
    )
    op.alter_column("product", NEW_COLUMN, new_column_name=OLD_COLUMN)
