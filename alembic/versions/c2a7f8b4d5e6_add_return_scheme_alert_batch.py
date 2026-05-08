"""add return scheme alert batch outbox"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2a7f8b4d5e6"
down_revision: str | None = "af41c3e7b91a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "return_scheme_alert_batch",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("generated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("new_incidents_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notification_incidents_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("report_path", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("delivery_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_return_scheme_alert_batch_status_generated_at",
        "return_scheme_alert_batch",
        ["status", "generated_at"],
        unique=False,
    )
    op.add_column(
        "return_scheme_incident",
        sa.Column("alert_batch_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_return_scheme_incident_alert_batch_id",
        "return_scheme_incident",
        "return_scheme_alert_batch",
        ["alert_batch_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_return_scheme_incident_alert_batch_id",
        "return_scheme_incident",
        type_="foreignkey",
    )
    op.drop_column("return_scheme_incident", "alert_batch_id")
    op.drop_index(
        "ix_return_scheme_alert_batch_status_generated_at",
        table_name="return_scheme_alert_batch",
    )
    op.drop_table("return_scheme_alert_batch")
