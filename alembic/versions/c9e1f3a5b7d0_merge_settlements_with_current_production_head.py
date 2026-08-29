"""merge customer settlements with the current production migration head

Revision ID: c9e1f3a5b7d0
Revises: b8d0f2a4c6e8, 6e8f0a2b4c6d
Create Date: 2026-08-29 21:30:00.000000
"""

from __future__ import annotations

revision: str = "c9e1f3a5b7d0"
down_revision: tuple[str, str] = (
    "b8d0f2a4c6e8",
    "6e8f0a2b4c6d",
)
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Merge two additive branches without changing either schema."""


def downgrade() -> None:
    """Split the migration graph without changing either schema."""
