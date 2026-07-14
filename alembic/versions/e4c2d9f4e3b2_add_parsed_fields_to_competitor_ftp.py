"""add parsed fields to competitor ftp records

Revision ID: e4c2d9f4e3b2
Revises: 7beb0da54240
Create Date: 2025-12-09 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4c2d9f4e3b2"
down_revision: str | None = "7beb0da54240"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "competitor_ftp_record",
        sa.Column("parsed_brand", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "competitor_ftp_record",
        sa.Column("parsed_model", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "competitor_ftp_record",
        sa.Column("parsed_variant", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "competitor_ftp_record",
        sa.Column("parse_confidence", sa.Numeric(4, 3), nullable=True),
    )
    op.add_column(
        "competitor_ftp_record",
        sa.Column("parse_notes", sa.Text(), nullable=True),
    )
    op.create_index(
        op.f("ix_competitor_ftp_record_parsed_brand"),
        "competitor_ftp_record",
        ["parsed_brand"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_competitor_ftp_record_parsed_brand"),
        table_name="competitor_ftp_record",
    )
    op.drop_column("competitor_ftp_record", "parse_notes")
    op.drop_column("competitor_ftp_record", "parse_confidence")
    op.drop_column("competitor_ftp_record", "parsed_variant")
    op.drop_column("competitor_ftp_record", "parsed_model")
    op.drop_column("competitor_ftp_record", "parsed_brand")
