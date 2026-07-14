"""add llm metadata to competitor items and matches table

Revision ID: 1e16b3d4e6c7
Revises: 9d3c8c4fd1a1
Create Date: 2025-12-18 12:00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1e16b3d4e6c7"
down_revision: str | None = "9d3c8c4fd1a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    jsonb_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        parse_status_enum_create = postgresql.ENUM(
            "ok",
            "invalid_json",
            "timeout",
            "low_confidence",
            "conflict",
            name="competitor_item_parse_status",
            create_type=True,
        )
        match_status_enum_create = postgresql.ENUM(
            "suggested",
            "accepted",
            "rejected",
            "needs_review",
            "ambiguous",
            name="competitor_item_match_status",
            create_type=True,
        )
        match_method_enum_create = postgresql.ENUM(
            "embedding_auto",
            "llm_arbitrate",
            "manual",
            name="competitor_item_match_method",
            create_type=True,
        )
        parse_status_enum_create.create(bind, checkfirst=True)
        match_status_enum_create.create(bind, checkfirst=True)
        match_method_enum_create.create(bind, checkfirst=True)

    parse_status_enum = (
        postgresql.ENUM(
            "ok",
            "invalid_json",
            "timeout",
            "low_confidence",
            "conflict",
            name="competitor_item_parse_status",
            create_type=False,
        )
        if is_pg
        else sa.Enum(
            "ok",
            "invalid_json",
            "timeout",
            "low_confidence",
            "conflict",
            name="competitor_item_parse_status",
        )
    )
    match_status_enum = (
        postgresql.ENUM(
            "suggested",
            "accepted",
            "rejected",
            "needs_review",
            "ambiguous",
            name="competitor_item_match_status",
            create_type=False,
        )
        if is_pg
        else sa.Enum(
            "suggested",
            "accepted",
            "rejected",
            "needs_review",
            "ambiguous",
            name="competitor_item_match_status",
        )
    )
    match_method_enum = (
        postgresql.ENUM(
            "embedding_auto",
            "llm_arbitrate",
            "manual",
            name="competitor_item_match_method",
            create_type=False,
        )
        if is_pg
        else sa.Enum(
            "embedding_auto",
            "llm_arbitrate",
            "manual",
            name="competitor_item_match_method",
        )
    )

    inspector = inspect(bind)
    existing_columns = {col["name"] for col in inspector.get_columns("competitor_item")}
    if "item_type" not in existing_columns:
        op.add_column(
            "competitor_item", sa.Column("item_type", sa.String(length=32), nullable=True)
        )
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("competitor_item")}
        if "ix_competitor_item_item_type" not in existing_indexes:
            op.create_index(
                "ix_competitor_item_item_type",
                "competitor_item",
                ["item_type"],
                unique=False,
            )

    op.add_column(
        "competitor_item", sa.Column("normalized_title", sa.String(length=1024), nullable=True)
    )
    op.add_column("competitor_item", sa.Column("attrs_json", jsonb_type, nullable=True))
    op.add_column("competitor_item", sa.Column("llm_confidence", sa.Numeric(4, 3), nullable=True))
    op.add_column("competitor_item", sa.Column("llm_raw_json", sa.Text(), nullable=True))
    op.add_column("competitor_item", sa.Column("parse_status", parse_status_enum, nullable=True))
    op.add_column("competitor_item", sa.Column("parse_error", sa.Text(), nullable=True))
    op.add_column(
        "competitor_item", sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("competitor_item", sa.Column("llm_model", sa.String(length=128), nullable=True))
    op.add_column("competitor_item", sa.Column("prompt_hash", sa.String(length=64), nullable=True))
    op.add_column(
        "competitor_item", sa.Column("parse_version", sa.String(length=50), nullable=True)
    )

    op.create_index(
        "ix_competitor_item_processed_at", "competitor_item", ["processed_at"], unique=False
    )

    if is_pg:
        op.create_index(
            "ix_competitor_item_attrs_json_gin",
            "competitor_item",
            ["attrs_json"],
            unique=False,
            postgresql_using="gin",
        )

    op.create_table(
        "competitor_item_match",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("competitor_item_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column(
            "status", match_status_enum, nullable=False, server_default=sa.text("'suggested'")
        ),
        sa.Column(
            "method", match_method_enum, nullable=False, server_default=sa.text("'embedding_auto'")
        ),
        sa.Column("score_embed_best", sa.Numeric(6, 4), nullable=True),
        sa.Column("score_embed_gap", sa.Numeric(6, 4), nullable=True),
        sa.Column("score_llm", sa.Numeric(6, 4), nullable=True),
        sa.Column("final_score", sa.Numeric(6, 4), nullable=True),
        sa.Column("rationale_json", jsonb_type, nullable=True),
        sa.Column("embed_model", sa.String(length=128), nullable=True),
        sa.Column("embed_dim", sa.Integer(), nullable=True),
        sa.Column("topk_used", sa.Integer(), nullable=True),
        sa.Column("llm_confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["competitor_item_id"], ["competitor_item.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["product.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("competitor_item_id", name="uq_comp_item_match_single"),
    )
    op.create_index(
        "ix_competitor_item_match_item_id",
        "competitor_item_match",
        ["competitor_item_id"],
        unique=False,
    )
    op.create_index(
        "ix_competitor_item_match_product_id", "competitor_item_match", ["product_id"], unique=False
    )
    op.create_index(
        "ix_competitor_item_match_status", "competitor_item_match", ["status"], unique=False
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    op.drop_index("ix_competitor_item_match_status", table_name="competitor_item_match")
    op.drop_index("ix_competitor_item_match_product_id", table_name="competitor_item_match")
    op.drop_index("ix_competitor_item_match_item_id", table_name="competitor_item_match")
    op.drop_table("competitor_item_match")

    if is_pg:
        op.drop_index("ix_competitor_item_attrs_json_gin", table_name="competitor_item")
    op.drop_index("ix_competitor_item_processed_at", table_name="competitor_item")

    op.drop_column("competitor_item", "parse_version")
    op.drop_column("competitor_item", "prompt_hash")
    op.drop_column("competitor_item", "llm_model")
    op.drop_column("competitor_item", "processed_at")
    op.drop_column("competitor_item", "parse_error")
    op.drop_column("competitor_item", "parse_status")
    op.drop_column("competitor_item", "llm_raw_json")
    op.drop_column("competitor_item", "llm_confidence")
    op.drop_column("competitor_item", "attrs_json")
    op.drop_column("competitor_item", "normalized_title")

    if is_pg:
        postgresql.ENUM(name="competitor_item_match_method").drop(bind, checkfirst=True)
        postgresql.ENUM(name="competitor_item_match_status").drop(bind, checkfirst=True)
        postgresql.ENUM(name="competitor_item_parse_status").drop(bind, checkfirst=True)
