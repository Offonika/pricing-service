from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.site_service_requests import (
    SiteServiceRequestCase,
    SiteServiceRequestCommand,
    SiteServiceRequestEvent,
    SiteServiceRequestFile,
    SiteServiceRequestNonce,
)


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/2c4d6e8f0a12_add_site_service_requests.py"
    )
    spec = importlib.util.spec_from_file_location("site_service_requests_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_models_persist_encrypted_delivery_state_and_relationships() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)

    with Session(engine) as session:
        case = SiteServiceRequestCase(
            source_ticket_id=741,
            intake_mode="during_open_shift",
        )
        case.events.append(
            SiteServiceRequestEvent(
                event_id="site-support:741:1201",
                source_message_id=1201,
                event_type="ticket.created",
                direction="inbound",
                payload_encrypted=b"encrypted-event",
                payload_sha256="a" * 64,
            )
        )
        case.files.append(
            SiteServiceRequestFile(
                source_message_id=1201,
                source_file_id=93287,
                safe_filename="photo.jpg",
                mime_type="image/jpeg",
                byte_size=1024,
                sha256="b" * 64,
            )
        )
        case.commands.append(
            SiteServiceRequestCommand(
                command_key="site-support-reply:741:1",
                reply_encrypted=b"encrypted-reply",
                reply_sha256="c" * 64,
            )
        )
        session.add(case)
        session.add(
            SiteServiceRequestNonce(
                nonce="11111111-1111-4111-8111-111111111111",
                expires_at=now + timedelta(minutes=10),
            )
        )
        session.commit()

        assert case.assignment_state == "waiting"
        assert case.sync_status == "pending"
        assert case.version == 1
        assert case.events[0].payload_encrypted == b"encrypted-event"
        assert case.files[0].temporary_path is None
        assert case.commands[0].status == "pending"

    engine.dispose()


def test_source_ticket_and_event_ids_are_durable_dedupe_keys() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(SiteServiceRequestCase(source_ticket_id=741))
        session.commit()
        session.add(SiteServiceRequestCase(source_ticket_id=741))
        with pytest.raises(IntegrityError):
            session.commit()

    engine.dispose()


def test_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'site-service-requests.db'}")
    migration = _load_migration()

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        inspector = inspect(connection)
        expected_tables = {
            "site_service_request_case",
            "site_service_request_event",
            "site_service_request_file",
            "site_service_request_command",
            "site_service_request_nonce",
        }
        assert expected_tables <= set(inspector.get_table_names())
        case_unique = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("site_service_request_case")
        }
        assert case_unique == {
            "uq_site_service_request_case_bitrix_item",
            "uq_site_service_request_case_source_ticket",
        }
        event_columns = {
            column["name"]: column for column in inspector.get_columns("site_service_request_event")
        }
        assert event_columns["payload_encrypted"]["nullable"] is True
        assert {
            index["name"] for index in inspector.get_indexes("site_service_request_command")
        } == {"ix_site_service_request_command_lease"}

        migration.downgrade()
        assert not expected_tables & set(inspect(connection).get_table_names())

    engine.dispose()


def test_migration_extends_current_production_head() -> None:
    assert _load_migration().down_revision == "1b9d3f5a7c21"
