"""add product display modification fields

Revision ID: 6d5c4b3a2f10
Revises: f7a8b9c0d1e2
Create Date: 2026-04-27 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "6d5c4b3a2f10"
down_revision: str | Sequence[str] | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    bind = op.get_bind()
    cols = {col["name"] for col in sa.inspect(bind).get_columns(table)}
    if column.name not in cols:
        op.add_column(table, column)


def upgrade() -> None:
    _add_column_if_missing(
        "product", sa.Column("display_screen_kit", sa.String(length=32), nullable=True)
    )
    _add_column_if_missing("product", sa.Column("display_has_frame", sa.Boolean(), nullable=True))
    _add_column_if_missing("product", sa.Column("display_has_touch", sa.Boolean(), nullable=True))
    _add_column_if_missing("product", sa.Column("display_has_ic_pad", sa.Boolean(), nullable=True))
    _add_column_if_missing(
        "product", sa.Column("display_has_binding_no_solder", sa.Boolean(), nullable=True)
    )
    _add_column_if_missing(
        "product", sa.Column("display_backlight", sa.String(length=32), nullable=True)
    )
    _add_column_if_missing(
        "product",
        sa.Column(
            "display_matrix_tags",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )
    _add_column_if_missing(
        "product", sa.Column("display_modification_status", sa.String(length=32), nullable=True)
    )
    _add_column_if_missing(
        "product", sa.Column("display_modification_source", sa.String(length=32), nullable=True)
    )
    _add_column_if_missing(
        "product", sa.Column("display_modification_confidence", sa.Float(), nullable=True)
    )
    _add_column_if_missing(
        "product", sa.Column("display_parse_version", sa.String(length=50), nullable=True)
    )


def downgrade() -> None:
    bind = op.get_bind()
    cols = {col["name"] for col in sa.inspect(bind).get_columns("product")}
    for name in (
        "display_parse_version",
        "display_modification_confidence",
        "display_modification_source",
        "display_modification_status",
        "display_matrix_tags",
        "display_backlight",
        "display_has_binding_no_solder",
        "display_has_ic_pad",
        "display_has_touch",
        "display_has_frame",
        "display_screen_kit",
    ):
        if name in cols:
            op.drop_column("product", name)
