"""merge lifecycle and pending heads

Revision ID: 029d35e11f46
Revises: d1a2b3c4e5f7, ef56ab78cd90
Create Date: 2026-08-06 20:20:08.599173

"""

from __future__ import annotations

from typing import Sequence

# revision identifiers, used by Alembic.
revision: str = "029d35e11f46"
down_revision: str | Sequence[str] | None = (
    "d1a2b3c4e5f7",
    "ef56ab78cd90",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
