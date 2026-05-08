"""add matching property mapping

Revision ID: 4e2a9c8d7f10
Revises: 9d1e2f3a4b5c
Create Date: 2026-05-06 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "4e2a9c8d7f10"
down_revision: str | None = "9d1e2f3a4b5c"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "matching_property_profile",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("item_type", sa.String(length=32), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_matching_property_profile_code"),
    )
    op.create_index(
        op.f("ix_matching_property_profile_code"),
        "matching_property_profile",
        ["code"],
        unique=False,
    )
    op.create_index(
        op.f("ix_matching_property_profile_item_type"),
        "matching_property_profile",
        ["item_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_matching_property_profile_is_active"),
        "matching_property_profile",
        ["is_active"],
        unique=False,
    )

    op.create_table(
        "matching_property_rule",
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("property_key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("product_field", sa.String(length=128), nullable=False),
        sa.Column("competitor_field", sa.String(length=128), nullable=False),
        sa.Column("comparison_mode", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["matching_property_profile.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id",
            "property_key",
            name="uq_matching_property_rule_profile_key",
        ),
    )
    op.create_index(
        op.f("ix_matching_property_rule_profile_id"),
        "matching_property_rule",
        ["profile_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_matching_property_rule_is_active"),
        "matching_property_rule",
        ["is_active"],
        unique=False,
    )

    op.create_table(
        "matching_property_value_map",
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("competitor_source", sa.String(length=128), nullable=True),
        sa.Column("competitor_value", sa.String(length=255), nullable=False),
        sa.Column("mapped_value", sa.String(length=255), nullable=False),
        sa.Column("notes", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["matching_property_rule.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "rule_id",
            "competitor_source",
            "competitor_value",
            name="uq_matching_property_value_map_rule_source_value",
        ),
    )
    op.create_index(
        op.f("ix_matching_property_value_map_rule_id"),
        "matching_property_value_map",
        ["rule_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_matching_property_value_map_competitor_source"),
        "matching_property_value_map",
        ["competitor_source"],
        unique=False,
    )
    op.create_index(
        op.f("ix_matching_property_value_map_is_active"),
        "matching_property_value_map",
        ["is_active"],
        unique=False,
    )

    op.create_table(
        "matching_property_rule_audit",
        sa.Column("rule_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=True),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["matching_property_rule.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_matching_property_rule_audit_rule_id"),
        "matching_property_rule_audit",
        ["rule_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_matching_property_rule_audit_rule_id"),
        table_name="matching_property_rule_audit",
    )
    op.drop_table("matching_property_rule_audit")
    op.drop_index(
        op.f("ix_matching_property_value_map_is_active"),
        table_name="matching_property_value_map",
    )
    op.drop_index(
        op.f("ix_matching_property_value_map_competitor_source"),
        table_name="matching_property_value_map",
    )
    op.drop_index(
        op.f("ix_matching_property_value_map_rule_id"),
        table_name="matching_property_value_map",
    )
    op.drop_table("matching_property_value_map")
    op.drop_index(
        op.f("ix_matching_property_rule_is_active"),
        table_name="matching_property_rule",
    )
    op.drop_index(
        op.f("ix_matching_property_rule_profile_id"),
        table_name="matching_property_rule",
    )
    op.drop_table("matching_property_rule")
    op.drop_index(
        op.f("ix_matching_property_profile_is_active"),
        table_name="matching_property_profile",
    )
    op.drop_index(
        op.f("ix_matching_property_profile_item_type"),
        table_name="matching_property_profile",
    )
    op.drop_index(
        op.f("ix_matching_property_profile_code"),
        table_name="matching_property_profile",
    )
    op.drop_table("matching_property_profile")
