from __future__ import annotations

import importlib.util
import os
from datetime import datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.services import pickup_history
from scripts import backfill_order_fulfillment_chats as backfill
from scripts import ensure_pickup_deal_fields as ensure_fields
from scripts import reconcile_historical_pickup_orders as reconcile_history


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/3e7a9c1d5f24_add_pickup_inventory_and_sla.py"
    )
    spec = importlib.util.spec_from_file_location("pickup_control_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pickup_control_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    migration = _load_migration()
    engine = create_engine(f"sqlite:///{tmp_path / 'pickup-migration.db'}")
    metadata = MetaData()
    Table("logistics_warehouse", metadata, Column("id", Integer, primary_key=True))
    Table("bitrix_chat_message", metadata, Column("id", Integer, primary_key=True))
    Table("site_order_execution_case", metadata, Column("id", Integer, primary_key=True))
    Table("site_order_execution_event", metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)

    try:
        with engine.begin() as connection:
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            inspector = inspect(connection)
            assert {
                "bitrix_chat_reaction",
                "pickup_inventory_run",
                "pickup_inventory_submission",
                "pickup_inventory_item",
            } <= set(inspector.get_table_names())
            assert {"notification_confirmed_at", "sla_started_at", "hold_until"} <= {
                item["name"] for item in inspector.get_columns("site_order_execution_case")
            }
            assert {"warehouse_id", "actor_ref"} <= {
                item["name"] for item in inspector.get_columns("site_order_execution_event")
            }
            submission_columns = {
                item["name"]: item for item in inspector.get_columns("pickup_inventory_submission")
            }
            assert submission_columns["warehouse_id"]["nullable"] is True
            message_constraint = next(
                item
                for item in inspector.get_unique_constraints("pickup_inventory_submission")
                if item["name"] == "uq_pickup_inventory_submission_message_warehouse"
            )
            assert message_constraint["column_names"] == [
                "source_message_id",
                "warehouse_id",
                "revision",
            ]

            migration.downgrade()
            inspector = inspect(connection)
            assert "pickup_inventory_item" not in inspector.get_table_names()
            assert "hold_until" not in {
                item["name"] for item in inspector.get_columns("site_order_execution_case")
            }
    finally:
        engine.dispose()


def _field(name: str, field_type: str, field_id: str) -> dict[str, str]:
    return {"ID": field_id, "FIELD_NAME": name, "USER_TYPE_ID": field_type}


def test_ensure_fields_requires_existing_sms_marker_and_detects_duplicates() -> None:
    empty_plan = ensure_fields.build_plan([])
    assert sum(item["action"] == "add" for item in empty_plan) == 5
    assert {
        item.get("reason")
        for item in empty_plan
        if item["field_name"] == "UF_CRM_MM_PICKUP_READY_SMS_AT"
    } == {"reused_field_missing"}

    current = [
        _field(name, field_type, str(index))
        for index, (name, field_type, _) in enumerate(
            (*ensure_fields.REQUIRED_FIELDS, *ensure_fields.REUSED_FIELDS),
            start=1,
        )
    ]
    assert ensure_fields.build_plan(current) == []

    duplicated = [
        *current,
        _field("UF_CRM_MM_PICKUP_READY_SMS_AT", "datetime", "99"),
    ]
    duplicate_plan = ensure_fields.build_plan(duplicated)
    assert duplicate_plan == [
        {
            "action": "manual_review",
            "field_name": "UF_CRM_MM_PICKUP_READY_SMS_AT",
            "reason": "duplicate_field_code",
            "field_ids": ["6", "99"],
        }
    ]


def test_ensure_fields_apply_is_blocked_by_any_manual_review() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def add_deal_user_field(self, fields: dict):
            self.calls.append(fields)
            return 42

    client = Client()
    result = ensure_fields.apply_plan(
        client,
        [
            {
                "action": "manual_review",
                "field_name": "UF_DUPLICATE",
                "reason": "duplicate_field_code",
            },
            {
                "action": "add",
                "field_name": "UF_NEW",
                "user_type_id": "string",
                "label": "Новое поле",
            },
        ],
    )

    assert all(item["applied"] is False for item in result)
    assert all(item["blocked_by_preflight"] is True for item in result)
    assert client.calls == []


def test_ensure_fields_reads_every_bitrix_page_before_planning() -> None:
    class Client:
        def __init__(self) -> None:
            self.starts: list[int | str] = []

        def call(self, method: str, params: dict):
            assert method == "crm.deal.userfield.list"
            start = params["start"]
            self.starts.append(start)
            if start == 0:
                return {
                    "result": [_field("UF_FIRST", "string", "1")],
                    "next": 50,
                }
            return {"result": [_field("UF_SECOND", "date", "2")]}

    client = Client()

    rows = ensure_fields.fetch_all_deal_user_fields(client)

    assert client.starts == [0, 50]
    assert [row["FIELD_NAME"] for row in rows] == ["UF_FIRST", "UF_SECOND"]


def test_backfill_dry_run_paginates_without_persistence() -> None:
    class Client:
        def __init__(self) -> None:
            self.last_ids: list[int | None] = []

        def get_dialog_messages(self, dialog_id: str, *, limit: int, last_id: int | None = None):
            self.last_ids.append(last_id)
            pages = {None: [10, 9], 9: [8], 8: []}
            return {
                "messages": [
                    {
                        "id": value,
                        "date": datetime(2026, 8, 24, 10, value).isoformat(),
                        "params": {"LIKE": ["131016"]} if value == 10 else {"LIKE": []},
                    }
                    for value in pages[last_id]
                ]
            }

    client = Client()
    result = backfill.inspect_pages(
        client,
        dialog_id="chat8729",
        page_size=50,
        max_pages=10,
        lookback_since=None,
    )

    assert client.last_ids == [None, 9, 8]
    assert result["pages"] == 2
    assert result["messages"] == 3
    assert result["reaction_messages"] == 1


def test_backfill_defaults_include_all_control_chats() -> None:
    settings = Settings(_env_file=None)
    sources = dict(backfill.chat_sources(settings, []))
    assert set(sources) == {
        "site_master_mobile",
        "pickup_ready",
        "pickup_inventory",
        "pickup_movement",
        "pickup_exception",
        "courier_spb",
    }


def test_backfill_runtime_env_prefers_release_database_over_shared_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shared_env = tmp_path / "shared.env"
    application_env = tmp_path / "application.env"
    shared_env.write_text(
        "DATABASE_URL=postgresql://shared/call_analytics\n"
        "BITRIX_BOX_WEBHOOK_BASE=https://shared.example/rest/\n",
        encoding="utf-8",
    )
    application_env.write_text(
        "DATABASE_URL=postgresql://application/pricing\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(backfill, "SHARED_ENV_FILE", shared_env)
    monkeypatch.setattr(backfill, "APPLICATION_ENV_FILE", application_env)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("BITRIX_BOX_WEBHOOK_BASE", raising=False)

    backfill.configure_runtime_environment(require_database=True)

    assert os.environ["DATABASE_URL"] == "postgresql://application/pricing"
    assert os.environ["BITRIX_BOX_WEBHOOK_BASE"] == "https://shared.example/rest/"


def test_backfill_runtime_env_never_borrows_shared_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shared_env = tmp_path / "shared.env"
    shared_env.write_text(
        "DATABASE_URL=postgresql://shared/call_analytics\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(backfill, "SHARED_ENV_FILE", shared_env)
    monkeypatch.setattr(backfill, "APPLICATION_ENV_FILE", tmp_path / "missing.env")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit, match="Application DATABASE_URL is not configured"):
        backfill.configure_runtime_environment(require_database=True)

    assert "DATABASE_URL" not in os.environ


def test_backfill_schema_preflight_fails_closed_on_wrong_database(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'wrong.db'}")
    metadata = MetaData()
    Table("bitrix_chat_message", metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)

    try:
        with Session(engine) as session:
            with pytest.raises(RuntimeError, match="Raw backfill database preflight failed"):
                backfill.ensure_raw_backfill_schema(session)
    finally:
        engine.dispose()


def test_backfill_schema_preflight_accepts_complete_schema(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'application.db'}")
    metadata = MetaData()
    for table_name, column_names in backfill.RAW_BACKFILL_SCHEMA.items():
        Table(
            table_name,
            metadata,
            Column("id", Integer, primary_key=True),
            *[Column(column_name, String) for column_name in sorted(column_names - {"id"})],
        )
    metadata.create_all(engine)

    try:
        with Session(engine) as session:
            backfill.ensure_raw_backfill_schema(session)
    finally:
        engine.dispose()


def test_historical_batch_skips_deals_already_in_target_stage() -> None:
    rows = [
        pickup_history.HistoricalPickupAssessment(
            site_order_number="241500",
            bitrix_deal_id=500,
            current_stage="WON",
            queue=pickup_history.QUEUE_WON,
            target_stage="WON",
            reason="confirmed_onec_issue",
        ),
        pickup_history.HistoricalPickupAssessment(
            site_order_number="241501",
            bitrix_deal_id=501,
            current_stage="PICKUP_WAITING",
            queue=pickup_history.QUEUE_WON,
            target_stage="WON",
            reason="confirmed_onec_issue",
        ),
    ]

    selected = reconcile_history.select_batch(rows, queue=None, batch_size=20)

    assert [row.site_order_number for row in selected] == ["241501"]
