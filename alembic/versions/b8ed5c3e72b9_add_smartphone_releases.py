"""add smartphone releases table"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b8ed5c3e72b9"
down_revision: Union[str, None] = "1a4fb0e69e78"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # На случай прерванного запуска миграции или ручного создания типов
    op.execute("DROP TYPE IF EXISTS smartphone_release_status")
    op.execute("DROP TYPE IF EXISTS smartphone_release_source")

    release_status_enum = sa.Enum(
        "rumor", "announced", "released", name="smartphone_release_status"
    )
    source_type_enum = sa.Enum("news_api", "catalog", "manual", name="smartphone_release_source")

    jsonb_type = sa.JSON().with_variant(
        postgresql.JSONB(astext_type=sa.Text()), "postgresql"
    )

    op.create_table(
        "smartphone_releases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("brand", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=150), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("announcement_date", sa.Date(), nullable=True),
        sa.Column("release_status", release_status_enum, nullable=True),
        sa.Column(
            "source_type",
            source_type_enum,
            nullable=False,
            server_default=sa.text("'news_api'"),
        ),
        sa.Column("source_name", sa.String(length=100), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        sa.Column("region", sa.String(length=50), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("raw_payload", jsonb_type, nullable=True),
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
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_name", "source_url", name="uq_smartphone_release_source"),
        sa.UniqueConstraint("brand", "model", "announcement_date", name="uq_smartphone_release_identity"),
    )
    op.create_index(
        "ix_smartphone_releases_brand",
        "smartphone_releases",
        ["brand"],
        unique=False,
    )
    op.create_index(
        "ix_smartphone_releases_model",
        "smartphone_releases",
        ["model"],
        unique=False,
    )
    op.create_index(
        "ix_smartphone_releases_announcement_date",
        "smartphone_releases",
        ["announcement_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_smartphone_releases_announcement_date", table_name="smartphone_releases")
    op.drop_index("ix_smartphone_releases_model", table_name="smartphone_releases")
    op.drop_index("ix_smartphone_releases_brand", table_name="smartphone_releases")
    op.drop_table("smartphone_releases")

    source_type_enum = sa.Enum("news_api", "catalog", "manual", name="smartphone_release_source")
    release_status_enum = sa.Enum(
        "rumor", "announced", "released", name="smartphone_release_status"
    )
    source_type_enum.drop(op.get_bind(), checkfirst=True)
    release_status_enum.drop(op.get_bind(), checkfirst=True)
