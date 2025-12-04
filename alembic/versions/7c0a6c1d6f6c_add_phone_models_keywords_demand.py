"""add phone models, keywords, and keyword demand tables"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "7c0a6c1d6f6c"
down_revision = "5bcd1a7a2c3e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "phone_models",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("brand", sa.String(length=100), nullable=False),
        sa.Column("model_name", sa.String(length=150), nullable=False),
        sa.Column("variant", sa.String(length=50), nullable=True),
        sa.Column("announce_date", sa.Date(), nullable=True),
        sa.Column("release_date", sa.Date(), nullable=True),
        sa.Column("screen_size_inch", sa.Float(), nullable=True),
        sa.Column("screen_technology", sa.String(length=80), nullable=True),
        sa.Column("screen_refresh_rate_hz", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("brand", "model_name", "variant", name="uq_phone_model_identity"),
    )
    op.create_index("ix_phone_models_brand", "phone_models", ["brand"])
    op.create_index("ix_phone_models_model_name", "phone_models", ["model_name"])

    op.create_table(
        "keywords",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("phrase", sa.String(length=255), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=False, server_default="ru"),
        sa.Column("category", sa.String(length=50), nullable=False, server_default="display"),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("phone_model_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["phone_model_id"], ["phone_models.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("phone_model_id", "phrase", name="uq_keyword_phrase_per_model"),
    )
    op.create_index("ix_keywords_phrase", "keywords", ["phrase"])
    op.create_index("ix_keywords_phone_model_id", "keywords", ["phone_model_id"])

    op.create_table(
        "keyword_demands",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("keyword_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("region", sa.String(length=50), nullable=True),
        sa.Column("impressions", sa.Integer(), nullable=True),
        sa.Column("clicks", sa.Integer(), nullable=True),
        sa.Column("ctr", sa.Float(), nullable=True),
        sa.Column("bid_metrics", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False, server_default="yandex_direct"),
        sa.Column(
            "received_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["keyword_id"], ["keywords.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("keyword_id", "date", "region", name="uq_keyword_demand_date_region"),
    )
    op.create_index("ix_keyword_demands_keyword_id", "keyword_demands", ["keyword_id"])
    op.create_index("ix_keyword_demands_date", "keyword_demands", ["date"])
    op.create_index("ix_keyword_demands_region", "keyword_demands", ["region"])


def downgrade() -> None:
    op.drop_index("ix_keyword_demands_region", table_name="keyword_demands")
    op.drop_index("ix_keyword_demands_date", table_name="keyword_demands")
    op.drop_index("ix_keyword_demands_keyword_id", table_name="keyword_demands")
    op.drop_table("keyword_demands")

    op.drop_index("ix_keywords_phone_model_id", table_name="keywords")
    op.drop_index("ix_keywords_phrase", table_name="keywords")
    op.drop_table("keywords")

    op.drop_index("ix_phone_models_model_name", table_name="phone_models")
    op.drop_index("ix_phone_models_brand", table_name="phone_models")
    op.drop_table("phone_models")
