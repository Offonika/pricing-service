"""add return scheme incident table"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "af41c3e7b91a"
down_revision: str | None = "9c1d2e3f4a5b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "return_scheme_incident",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("product_ref", sa.String(length=128), nullable=False),
        sa.Column("product_name", sa.String(length=255), nullable=True),
        sa.Column("store_ref", sa.String(length=128), nullable=False),
        sa.Column("store_name", sa.String(length=255), nullable=True),
        sa.Column("manager_ref", sa.String(length=128), nullable=True),
        sa.Column("manager_name", sa.String(length=255), nullable=True),
        sa.Column("first_sale_doc_ref", sa.String(length=128), nullable=False),
        sa.Column("first_sale_doc_number", sa.String(length=64), nullable=False),
        sa.Column("first_sale_doc_datetime", sa.DateTime(), nullable=False),
        sa.Column("return_doc_ref", sa.String(length=128), nullable=False),
        sa.Column("return_doc_number", sa.String(length=64), nullable=False),
        sa.Column("return_doc_datetime", sa.DateTime(), nullable=False),
        sa.Column("second_sale_doc_ref", sa.String(length=128), nullable=False),
        sa.Column("second_sale_doc_number", sa.String(length=64), nullable=False),
        sa.Column("second_sale_doc_datetime", sa.DateTime(), nullable=False),
        sa.Column("second_price_type", sa.String(length=128), nullable=True),
        sa.Column("matched_qty", sa.Numeric(12, 3), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("first_detected_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint", name="uq_return_scheme_incident_fingerprint"),
    )
    op.create_index(
        "ix_return_scheme_incident_notified_at",
        "return_scheme_incident",
        ["notified_at"],
        unique=False,
    )
    op.create_index(
        "ix_return_scheme_incident_product_ref",
        "return_scheme_incident",
        ["product_ref"],
        unique=False,
    )
    op.create_index(
        "ix_return_scheme_incident_second_sale_doc_datetime",
        "return_scheme_incident",
        ["second_sale_doc_datetime"],
        unique=False,
    )
    op.create_index(
        "ix_return_scheme_incident_store_ref",
        "return_scheme_incident",
        ["store_ref"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_return_scheme_incident_store_ref", table_name="return_scheme_incident")
    op.drop_index(
        "ix_return_scheme_incident_second_sale_doc_datetime",
        table_name="return_scheme_incident",
    )
    op.drop_index("ix_return_scheme_incident_product_ref", table_name="return_scheme_incident")
    op.drop_index("ix_return_scheme_incident_notified_at", table_name="return_scheme_incident")
    op.drop_table("return_scheme_incident")
