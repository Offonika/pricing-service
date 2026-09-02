"""constrain normalized procurement 1C document GUID

Revision ID: d8e0f2a4b6c8
Revises: c7d9e1f3a5b8
Create Date: 2026-09-02 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d8e0f2a4b6c8"
down_revision = "c7d9e1f3a5b8"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_proc_order_formation_onec_ref_normalized"
TABLE_NAME = "procurement_order_formation"


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.execute(sa.text(f"""
        SELECT lower(trim(onec_document_ref)) AS normalized_ref,
               COUNT(*) AS duplicate_count
        FROM {TABLE_NAME}
        WHERE onec_document_ref IS NOT NULL
          AND trim(onec_document_ref) <> ''
        GROUP BY lower(trim(onec_document_ref))
        HAVING COUNT(*) > 1
        ORDER BY normalized_ref
    """)).fetchall()
    if duplicates:
        details = ", ".join(f"{row[0]} ({row[1]})" for row in duplicates[:20])
        raise RuntimeError(
            "Cannot create normalized 1C GUID unique index; duplicate procurement orders exist: "
            f"{details}"
        )
    op.create_index(
        INDEX_NAME,
        TABLE_NAME,
        [sa.text("lower(trim(onec_document_ref))")],
        unique=True,
        sqlite_where=sa.text("onec_document_ref IS NOT NULL AND trim(onec_document_ref) <> ''"),
        postgresql_where=sa.text("onec_document_ref IS NOT NULL AND trim(onec_document_ref) <> ''"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
