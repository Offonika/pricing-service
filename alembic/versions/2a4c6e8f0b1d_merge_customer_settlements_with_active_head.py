"""merge customer settlements with the active production migration head

Revision ID: 2a4c6e8f0b1d
Revises: 1b9d3f5a7c21, d9e1f3a5b7c9
Create Date: 2026-08-22 20:30:00.000000
"""

revision = "2a4c6e8f0b1d"
down_revision = ("1b9d3f5a7c21", "d9e1f3a5b7c9")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge two additive branches without changing either schema."""


def downgrade() -> None:
    """Split the migration graph without changing either schema."""
