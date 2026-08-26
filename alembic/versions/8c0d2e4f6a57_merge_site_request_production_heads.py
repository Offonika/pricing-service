"""merge site request branch with the active production migration head

Revision ID: 8c0d2e4f6a57
Revises: 2d6f8a0c4b13, 7b9d1f3a5c46
Create Date: 2026-08-24 18:00:00.000000
"""

from __future__ import annotations

revision: str = "8c0d2e4f6a57"
down_revision: tuple[str, str] = (
    "2d6f8a0c4b13",
    "7b9d1f3a5c46",
)
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
