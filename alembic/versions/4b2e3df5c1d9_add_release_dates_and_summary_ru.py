"""add release dates, published_at, and summary_ru to smartphone_releases"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "4b2e3df5c1d9"
down_revision: str = "b8ed5c3e72b9"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("smartphone_releases", sa.Column("summary_ru", sa.Text(), nullable=True))
    op.add_column("smartphone_releases", sa.Column("published_at", sa.DateTime(), nullable=True))
    op.add_column("smartphone_releases", sa.Column("market_release_date", sa.Date(), nullable=True))
    op.add_column("smartphone_releases", sa.Column("market_release_date_ru", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("smartphone_releases", "market_release_date_ru")
    op.drop_column("smartphone_releases", "market_release_date")
    op.drop_column("smartphone_releases", "published_at")
    op.drop_column("smartphone_releases", "summary_ru")
