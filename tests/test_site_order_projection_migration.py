from __future__ import annotations

import importlib.util
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
MIGRATION = PROJECT / "alembic/versions/a9b0c1d2e3f4_index_site_order_execution_case_onec_order.py"


def test_site_order_projection_migration_declares_reversible_index() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "a9b0c1d2e3f4"' in source
    assert 'down_revision = "e8f9012345a6"' in source
    assert source.count('"ix_site_order_execution_case_onec_order"') == 2
    assert "op.create_index" in source
    assert "op.drop_index" in source


def test_site_order_execution_case_metadata_contains_projection_index() -> None:
    from app.models import SiteOrderExecutionCase

    indexes = {item.name for item in SiteOrderExecutionCase.__table__.indexes}
    assert "ix_site_order_execution_case_onec_order" in indexes


def test_site_order_projection_migration_module_imports() -> None:
    spec = importlib.util.spec_from_file_location("site_order_projection_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "a9b0c1d2e3f4"
