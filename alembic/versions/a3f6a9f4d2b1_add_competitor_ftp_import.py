"""add competitor ftp import tables

Revision ID: a3f6a9f4d2b1
Revises: 1d2b9d39720a
Create Date: 2025-11-30 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a3f6a9f4d2b1"
down_revision: Union[str, None] = "1d2b9d39720a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "competitor_ftp_file",
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("file_date", sa.Date(), nullable=False),
        sa.Column("mtime", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("rows_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_valid", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_invalid", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("date_mismatch", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "file_date", name="uq_competitor_ftp_file_source_date"),
    )
    op.create_index(
        op.f("ix_competitor_ftp_file_file_date"),
        "competitor_ftp_file",
        ["file_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_competitor_ftp_file_source"),
        "competitor_ftp_file",
        ["source"],
        unique=False,
    )

    op.create_table(
        "competitor_ftp_raw_row",
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("file_date", sa.Date(), nullable=False),
        sa.Column("group_name", sa.String(length=255), nullable=True),
        sa.Column("sku", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=1024), nullable=True),
        sa.Column("price_opt", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("price_roz", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("link", sa.String(length=1024), nullable=True),
        sa.Column("stock", sa.Boolean(), nullable=True),
        sa.Column("amount", sa.Integer(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("is_valid", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["competitor_ftp_file.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_competitor_ftp_raw_row_file_date"),
        "competitor_ftp_raw_row",
        ["file_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_competitor_ftp_raw_row_file_id"),
        "competitor_ftp_raw_row",
        ["file_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_competitor_ftp_raw_row_source"),
        "competitor_ftp_raw_row",
        ["source"],
        unique=False,
    )

    op.create_table(
        "competitor_ftp_record",
        sa.Column("raw_row_id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("file_date", sa.Date(), nullable=False),
        sa.Column("group_name", sa.String(length=255), nullable=True),
        sa.Column("sku", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=1024), nullable=True),
        sa.Column("price_opt", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("price_roz", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("link", sa.String(length=1024), nullable=True),
        sa.Column("in_stock", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("amount", sa.Integer(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["competitor_ftp_file.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["raw_row_id"],
            ["competitor_ftp_raw_row.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("raw_row_id"),
    )
    op.create_index(
        op.f("ix_competitor_ftp_record_file_date"),
        "competitor_ftp_record",
        ["file_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_competitor_ftp_record_file_id"),
        "competitor_ftp_record",
        ["file_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_competitor_ftp_record_observed_at"),
        "competitor_ftp_record",
        ["observed_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_competitor_ftp_record_sku"),
        "competitor_ftp_record",
        ["sku"],
        unique=False,
    )
    op.create_index(
        op.f("ix_competitor_ftp_record_source"),
        "competitor_ftp_record",
        ["source"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_competitor_ftp_record_source"), table_name="competitor_ftp_record")
    op.drop_index(op.f("ix_competitor_ftp_record_sku"), table_name="competitor_ftp_record")
    op.drop_index(op.f("ix_competitor_ftp_record_observed_at"), table_name="competitor_ftp_record")
    op.drop_index(op.f("ix_competitor_ftp_record_file_id"), table_name="competitor_ftp_record")
    op.drop_index(op.f("ix_competitor_ftp_record_file_date"), table_name="competitor_ftp_record")
    op.drop_table("competitor_ftp_record")

    op.drop_index(op.f("ix_competitor_ftp_raw_row_source"), table_name="competitor_ftp_raw_row")
    op.drop_index(op.f("ix_competitor_ftp_raw_row_file_id"), table_name="competitor_ftp_raw_row")
    op.drop_index(op.f("ix_competitor_ftp_raw_row_file_date"), table_name="competitor_ftp_raw_row")
    op.drop_table("competitor_ftp_raw_row")

    op.drop_index(op.f("ix_competitor_ftp_file_source"), table_name="competitor_ftp_file")
    op.drop_index(op.f("ix_competitor_ftp_file_file_date"), table_name="competitor_ftp_file")
    op.drop_table("competitor_ftp_file")

