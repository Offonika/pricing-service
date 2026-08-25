from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.site_service_requests import (
    SiteServiceRequestCase,
    SiteServiceRequestCommand,
    SiteServiceRequestEvent,
    SiteServiceRequestFile,
    SiteServiceRequestNonce,
    SiteServiceRequestSource,
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


def _load_open_stage_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/3d5e7f901b34_add_site_service_request_open_stage.py"
    )
    spec = importlib.util.spec_from_file_location("site_service_request_open_stage", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _load_hardening_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/4e6f80912c45_harden_site_service_request_state.py"
    )
    spec = importlib.util.spec_from_file_location("site_service_request_hardening", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _load_delivery_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/5f7a9c1e3b24_harden_site_service_request_delivery.py"
    )
    spec = importlib.util.spec_from_file_location("site_service_request_delivery", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _load_outbound_error_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/6a8c0e2f4b35_add_site_request_outbound_error.py"
    )
    spec = importlib.util.spec_from_file_location("site_service_request_outbound_error", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _load_finalize_hardening_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/7b9d1f3a5c46_finalize_site_request_hardening.py"
    )
    spec = importlib.util.spec_from_file_location("site_service_request_finalize", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _load_email_sources_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/8c0e2a4b6d57_add_service_email_sources.py"
    )
    spec = importlib.util.spec_from_file_location("site_service_request_email_sources", path)
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
        assert case.base_sync_status == "pending"
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


def test_open_stage_migration_is_reversible_and_extends_site_request_head(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'site-service-open-stage.db'}")
    base_migration = _load_migration()
    open_stage_migration = _load_open_stage_migration()

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        base_migration.op = operations
        open_stage_migration.op = operations
        base_migration.upgrade()
        open_stage_migration.upgrade()
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("site_service_request_case")
        }
        assert "last_open_stage_id" in columns

        open_stage_migration.downgrade()
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("site_service_request_case")
        }
        assert "last_open_stage_id" not in columns

    assert open_stage_migration.down_revision == "2c4d6e8f0a12"
    engine.dispose()


def test_hardening_migration_backfills_status_and_is_reversible(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'site-service-hardening.db'}")
    base_migration = _load_migration()
    open_stage_migration = _load_open_stage_migration()
    hardening_migration = _load_hardening_migration()

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        base_migration.op = operations
        open_stage_migration.op = operations
        hardening_migration.op = operations
        base_migration.upgrade()
        open_stage_migration.upgrade()
        connection.exec_driver_sql(
            "INSERT INTO site_service_request_case "
            "(source_ticket_id, assignment_state, round_robin_seq, first_seen_at, "
            "sync_status, last_error_code, version, created_at, updated_at) VALUES "
            "(741, 'waiting', 0, CURRENT_TIMESTAMP, 'order_not_found', "
            "'order_not_found', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
            "(742, 'waiting', 0, CURRENT_TIMESTAMP, 'file_sync_error', "
            "'file_unavailable', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        hardening_migration.upgrade()

        rows = connection.exec_driver_sql(
            "SELECT base_sync_status, base_error_code "
            "FROM site_service_request_case ORDER BY source_ticket_id"
        ).all()
        assert rows == [
            ("order_not_found", "order_not_found"),
            ("pending", None),
        ]
        case_columns = {
            column["name"]
            for column in inspect(connection).get_columns("site_service_request_case")
        }
        assert {"assignment_checked_at", "outbound_checked_at"} <= case_columns
        command_columns = {
            column["name"]
            for column in inspect(connection).get_columns("site_service_request_command")
        }
        assert "card_action_cleared_at" in command_columns

        hardening_migration.downgrade()
        case_columns = {
            column["name"]
            for column in inspect(connection).get_columns("site_service_request_case")
        }
        assert "base_sync_status" not in case_columns

    assert hardening_migration.down_revision == "3d5e7f901b34"
    engine.dispose()


def test_delivery_migration_backfills_command_marker_and_is_reversible(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'site-service-delivery.db'}")
    base_migration = _load_migration()
    open_stage_migration = _load_open_stage_migration()
    hardening_migration = _load_hardening_migration()
    delivery_migration = _load_delivery_migration()

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        for migration in (
            base_migration,
            open_stage_migration,
            hardening_migration,
            delivery_migration,
        ):
            migration.op = operations
        base_migration.upgrade()
        open_stage_migration.upgrade()
        hardening_migration.upgrade()
        connection.exec_driver_sql(
            "INSERT INTO site_service_request_case "
            "(source_ticket_id, assignment_state, round_robin_seq, first_seen_at, "
            "escalated_at, sync_status, base_sync_status, version, created_at, updated_at) VALUES "
            "(741, 'escalated', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
            "'synced', 'synced', 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO site_service_request_command "
            "(case_id, command_key, reply_encrypted, reply_sha256, status, attempts, "
            "created_at, updated_at) VALUES "
            "(1, 'site-support-reply:741:test', X'01', 'abc', 'pending', 0, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )

        delivery_migration.upgrade()

        marker = connection.exec_driver_sql(
            "SELECT card_action_cleared_at FROM site_service_request_command"
        ).scalar_one()
        assert marker is not None
        escalation_markers = connection.exec_driver_sql(
            "SELECT escalation_timeline_delivered_at, "
            "escalation_notification_delivered_at FROM site_service_request_case"
        ).one()
        assert escalation_markers[0] is not None
        assert escalation_markers[1] is not None
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("site_service_request_case")
        }
        assert {
            "assignment_last_error_code",
            "escalation_timeline_delivered_at",
            "escalation_notification_delivered_at",
        } <= columns

        delivery_migration.downgrade()
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("site_service_request_case")
        }
        assert "escalation_timeline_delivered_at" not in columns

    assert delivery_migration.down_revision == "4e6f80912c45"
    engine.dispose()


def test_outbound_error_migration_is_reversible(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'site-service-outbound-error.db'}")
    migrations = (
        _load_migration(),
        _load_open_stage_migration(),
        _load_hardening_migration(),
        _load_delivery_migration(),
        _load_outbound_error_migration(),
    )

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        for migration in migrations:
            migration.op = operations
            migration.upgrade()
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("site_service_request_case")
        }
        assert "outbound_last_error_code" in columns

        migrations[-1].downgrade()
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("site_service_request_case")
        }
        assert "outbound_last_error_code" not in columns

    assert migrations[-1].down_revision == "5f7a9c1e3b24"
    engine.dispose()


def test_finalize_hardening_migration_corrects_delivery_backfills(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'site-service-finalize.db'}")
    migrations = (
        _load_migration(),
        _load_open_stage_migration(),
        _load_hardening_migration(),
        _load_delivery_migration(),
        _load_outbound_error_migration(),
        _load_finalize_hardening_migration(),
    )

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        for migration in migrations:
            migration.op = operations
        for migration in migrations[:-1]:
            migration.upgrade()
        connection.exec_driver_sql(
            "INSERT INTO site_service_request_case "
            "(source_ticket_id, assignment_state, round_robin_seq, first_seen_at, "
            "escalated_at, escalation_timeline_delivered_at, "
            "escalation_notification_delivered_at, sync_status, base_sync_status, "
            "version, created_at, updated_at) VALUES "
            "(741, 'escalated', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'synced', 'synced', 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO site_service_request_event "
            "(event_id, case_id, source_message_id, event_type, direction, "
            "payload_sha256, status, attempts, created_at, updated_at) VALUES "
            "('site-support:741:1201', 1, 1201, 'ticket.created', 'inbound', "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "'processed', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO site_service_request_command "
            "(case_id, command_key, reply_encrypted, reply_sha256, status, attempts, "
            "card_action_cleared_at, last_error_code, created_at, updated_at) VALUES "
            "(1, 'site-support-reply:741:failed', X'01', 'abc', 'failed', 1, "
            "CURRENT_TIMESTAMP, 'message_write_failed', "
            "'2026-08-23 10:00:00', '2026-08-23 10:00:00'), "
            "(1, 'site-support-reply:741:pending', X'02', 'def', 'pending', 0, "
            "CURRENT_TIMESTAMP, NULL, "
            "'2026-08-23 11:00:00', '2026-08-23 11:00:00')"
        )

        migrations[-1].upgrade()

        event_columns = {
            column["name"]
            for column in inspect(connection).get_columns("site_service_request_event")
        }
        assert {"consecutive_permanent_failures", "source_message_sha256"} <= event_columns
        command_columns = {
            column["name"]
            for column in inspect(connection).get_columns("site_service_request_command")
        }
        assert "lease_token" in command_columns
        file_columns = {
            column["name"]
            for column in inspect(connection).get_columns("site_service_request_file")
        }
        assert "bitrix_error_reported_at" in file_columns
        assert {
            index["name"]
            for index in inspect(connection).get_indexes("site_service_request_command")
        } >= {"ix_site_service_request_command_case"}
        cleared_markers = connection.exec_driver_sql(
            "SELECT card_action_cleared_at FROM site_service_request_command ORDER BY id"
        ).all()
        assert cleared_markers[0][0] is not None
        assert cleared_markers[1] == (None,)
        case_state = connection.exec_driver_sql(
            "SELECT escalation_notification_delivered_at, outbound_last_error_code "
            "FROM site_service_request_case"
        ).one()
        assert case_state == (None, "message_write_failed")

        migrations[-1].downgrade()
        assert "lease_token" not in {
            column["name"]
            for column in inspect(connection).get_columns("site_service_request_command")
        }
        assert "bitrix_error_reported_at" not in {
            column["name"]
            for column in inspect(connection).get_columns("site_service_request_file")
        }

    assert migrations[-1].down_revision == "6a8c0e2f4b35"
    engine.dispose()


def test_email_sources_migration_backfills_site_identity_and_is_reversible(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'site-service-email-sources.db'}")
    migrations = [
        _load_migration(),
        _load_open_stage_migration(),
        _load_hardening_migration(),
        _load_delivery_migration(),
        _load_outbound_error_migration(),
        _load_finalize_hardening_migration(),
        _load_email_sources_migration(),
    ]

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        for migration in migrations:
            migration.op = operations
        for migration in migrations[:-1]:
            migration.upgrade()
        connection.execute(
            text(
                "INSERT INTO site_service_request_case "
                "(source_ticket_id, assignment_state, round_robin_seq, first_seen_at, "
                "base_sync_status, sync_status, version, created_at, updated_at) "
                "VALUES (741, 'waiting', 0, CURRENT_TIMESTAMP, 'pending', 'pending', 1, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

        migrations[-1].upgrade()

        case_row = connection.execute(
            text(
                "SELECT source_kind, source_key FROM site_service_request_case "
                "WHERE source_ticket_id=741"
            )
        ).one()
        source_row = connection.execute(
            text(
                "SELECT source_kind, source_key FROM site_service_request_source"
            )
        ).one()
        assert case_row == ("site_ticket", "site-support-ticket:741")
        assert source_row == case_row
        assert "source_activity_id" in {
            column["name"]
            for column in inspect(connection).get_columns("site_service_request_event")
        }

        migrations[-1].downgrade()
        assert "site_service_request_source" not in inspect(connection).get_table_names()
        assert "source_kind" not in {
            column["name"]
            for column in inspect(connection).get_columns("site_service_request_case")
        }

    assert migrations[-1].down_revision == "7b9d1f3a5c46"
    engine.dispose()


def test_email_source_identity_is_unique_in_models() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        first = SiteServiceRequestCase(source_ticket_id=-1)
        second = SiteServiceRequestCase(source_ticket_id=-2)
        first.sources.append(
            SiteServiceRequestSource(
                source_kind="bitrix_mail",
                source_key="bitrix-mail:shop:777",
            )
        )
        second.sources.append(
            SiteServiceRequestSource(
                source_kind="bitrix_mail",
                source_key="bitrix-mail:shop:777",
            )
        )
        session.add_all((first, second))
        with pytest.raises(IntegrityError):
            session.commit()

    engine.dispose()
