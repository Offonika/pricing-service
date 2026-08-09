from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from app.core.config import Settings
from app.services.bitrix_procurement_order_formation_auth import (
    ProcurementOrderFormationSession,
)
from app.services.procurement_supplier_profiles import (
    serialize_supplier_profile,
    update_supplier_profile,
)
from tests.test_procurement_order_formation import _order


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/f3a4b5c6d7e9_add_procurement_supplier_profiles.py"
    )
    spec = importlib.util.spec_from_file_location("procurement_supplier_profile_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _session(user_id: str) -> ProcurementOrderFormationSession:
    return ProcurementOrderFormationSession(
        actor=f"bitrix:member:{user_id}",
        domain="crm.example.test",
        member_id="member",
        user_id=user_id,
        expires_at=datetime.now(UTC),
        user_name="Согласующий",
    )


def test_supplier_profile_is_versioned_and_restricted_to_approvers(db_session) -> None:
    order = _order(db_session)
    settings = Settings(procurement_order_formation_classification_approver_user_ids=["42"])
    with pytest.raises(PermissionError):
        update_supplier_profile(
            db_session,
            supplier_ref=order.supplier_ref,
            values={
                "expected_version": 0,
                "qualification_class": "A",
                "advantages": [],
            },
            session=_session("99"),
            settings=settings,
        )
    profile = update_supplier_profile(
        db_session,
        supplier_ref=order.supplier_ref,
        values={
            "expected_version": 0,
            "qualification_class": "A",
            "qualification_label": "Приоритетный партнёр",
            "advantages": ["Компенсация брака", "Быстрый ответ"],
            "internal_note": "Проверять упаковку",
        },
        session=_session("42"),
        settings=settings,
    )
    db_session.commit()
    payload = serialize_supplier_profile(profile)
    assert payload["version"] == 1
    assert payload["qualification_class"] == "A"
    assert payload["class_description"] == "Лучшие условия и надёжность"
    assert payload["manual_updated_by_name"] == "Согласующий"

    with pytest.raises(ValueError, match="version conflict"):
        update_supplier_profile(
            db_session,
            supplier_ref=order.supplier_ref,
            values={
                "expected_version": 0,
                "qualification_class": "B",
                "advantages": [],
            },
            session=_session("42"),
            settings=settings,
        )


def test_supplier_profile_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'supplier-profile-migration.db'}")
    migration = _load_migration()
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE procurement_classification_proposal "
                "(id INTEGER PRIMARY KEY, status VARCHAR(32) NOT NULL)"
            )
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        inspector = inspect(connection)
        assert "procurement_supplier_profile" in inspector.get_table_names()
        proposal_columns = {
            column["name"]
            for column in inspector.get_columns("procurement_classification_proposal")
        }
        assert {"rejected_at", "rejection_reason"}.issubset(proposal_columns)

        migration.downgrade()
        inspector = inspect(connection)
        assert "procurement_supplier_profile" not in inspector.get_table_names()
        proposal_columns = {
            column["name"]
            for column in inspector.get_columns("procurement_classification_proposal")
        }
        assert "rejected_at" not in proposal_columns
        assert "rejection_reason" not in proposal_columns
