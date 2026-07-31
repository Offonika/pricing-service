from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from app.models import CompetitorItem, CompetitorItemMatch, Product
from app.services.manual_matching_decisions import (
    LEGACY_REASON_CODE,
    build_decision_snapshot,
    normalize_reason_code,
    snapshot_summary,
)


def test_decision_snapshot_captures_replay_fields():
    product = Product(id=10, article="P-10", name="Камера iPhone 15", subject="камера")
    item = CompetitorItem(
        id=20,
        competitor="moba",
        external_id="CAM-IP15",
        name="Камера для iPhone 15",
        item_type="camera",
        attrs_json={"model": "iphone 15"},
    )
    match = CompetitorItemMatch(
        competitor_item_id=20,
        product_id=10,
        final_score=0.93,
        score_embed_gap=0.08,
        embed_model="text-embedding-3-small",
        embed_dim=1536,
        topk_used=5,
        rationale_json={"auto_accept_camera": {"reason": "matching_camera_position"}},
    )

    snapshot = build_decision_snapshot(
        product=product,
        item=item,
        match=match,
        reason_code="confirmed_attributes",
    )

    assert snapshot["schema_version"] == 1
    assert snapshot["selected_rank"] == 1
    assert snapshot["scores"]["final"] == 0.93
    assert snapshot["versions"]["embed_model"] == "text-embedding-3-small"
    assert snapshot["top_k"]["candidates"][0]["product_id"] == 10
    assert snapshot_summary(snapshot) == {
        "snapshot_schema_version": 1,
        "snapshot_score": 0.93,
        "snapshot_rank": 1,
        "snapshot_top_k_count": 1,
    }


def test_unknown_reason_code_is_backward_compatible():
    assert normalize_reason_code(None) == LEGACY_REASON_CODE
    assert normalize_reason_code("old-client-text") == LEGACY_REASON_CODE


def test_decision_feedback_migration_backfills_legacy_rows(tmp_path: Path):
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/c8d9e0f1a2b3_add_matching_decision_feedback.py"
    )
    spec = importlib.util.spec_from_file_location(
        "matching_decision_feedback_migration", migration_path
    )
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE product_competitor_item_decision "
                "(id INTEGER PRIMARY KEY, action VARCHAR(16) NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO product_competitor_item_decision (id, action) VALUES (1, 'reject')")
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        row = connection.execute(
            text(
                "SELECT reason_code, snapshot_json "
                "FROM product_competitor_item_decision WHERE id = 1"
            )
        ).one()
        assert row.reason_code == LEGACY_REASON_CODE
        assert row.snapshot_json is None
        assert {
            "reason_code",
            "snapshot_json",
        }.issubset(
            {
                column["name"]
                for column in inspect(connection).get_columns("product_competitor_item_decision")
            }
        )

        migration.downgrade()
        column_names = {
            column["name"]
            for column in inspect(connection).get_columns("product_competitor_item_decision")
        }
        assert "reason_code" not in column_names
        assert "snapshot_json" not in column_names
