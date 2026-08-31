from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import (
    CustomerReturnAction,
    CustomerReturnEvent,
    CustomerReturnShipment,
)
from app.services import customer_returns
from app.services.customer_return_bitrix import (
    CustomerReturnBitrixConfig,
    CustomerReturnBitrixWriter,
)
from tasks.customer_return_bitrix_worker import main as worker_main


class FakeCustomerReturnBitrixApi:
    def __init__(self, *, fail_task: bool = False):
        self.fail_task = fail_task
        self.tasks: dict[str, str] = {}
        self.comments: dict[str, str] = {}
        self.completed_task_ids: list[str] = []
        self.calls: list[str] = []

    def ensure_task(
        self,
        *,
        title: str,
        description: str,
        marker: str,
        config: CustomerReturnBitrixConfig,
        deadline: datetime | None,
    ) -> str:
        self.calls.append("ensure_task")
        if self.fail_task:
            raise RuntimeError("simulated secret-bearing failure must not be stored")
        assert title
        assert marker in description
        assert config.group_id == 13
        assert config.responsible_user_id == 456
        assert deadline is not None
        return self.tasks.setdefault(marker, "9001")

    def ensure_comment(self, *, task_id: str, marker: str, message: str) -> str:
        self.calls.append("ensure_comment")
        assert task_id == "9001"
        assert message
        return self.comments.setdefault(marker, str(7000 + len(self.comments) + 1))

    def ensure_completed(self, *, task_id: str) -> None:
        self.calls.append("ensure_completed")
        if task_id not in self.completed_task_ids:
            self.completed_task_ids.append(task_id)


class FakeRestClient:
    def __init__(self):
        self.task_rows: list[dict[str, Any]] = []
        self.comment_rows: list[dict[str, Any]] = []
        self.added_tasks = 0
        self.added_comments = 0
        self.completed = 0
        self.task_status = "2"

    def call_json(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert method == "tasks.task.list"
        assert payload["filter"]["GROUP_ID"] == 13
        return {"result": {"tasks": self.task_rows}}

    def call(self, method: str, params=None, **_kwargs) -> dict[str, Any]:
        if method == "task.commentitem.getlist":
            return {"result": self.comment_rows}
        if method == "task.commentitem.add":
            self.added_comments += 1
            return {"result": "7001"}
        raise AssertionError(f"unexpected method: {method}")

    def add_task(self, **_kwargs) -> str:
        self.added_tasks += 1
        return "9001"

    def get_task(self, *, task_id: str) -> dict[str, Any]:
        assert task_id == "9001"
        return {"id": task_id, "status": self.task_status}

    def complete_task(self, *, task_id: str) -> None:
        assert task_id == "9001"
        self.completed += 1


def _database(tmp_path: Path, name: str) -> tuple[str, Any]:
    path = tmp_path / name
    database_url = f"sqlite:///{path}"
    engine = create_engine(database_url)
    CustomerReturnShipment.__table__.create(engine)
    CustomerReturnEvent.__table__.create(engine)
    CustomerReturnAction.__table__.create(engine)
    return database_url, engine


def _settings(*, max_attempts: int = 5) -> Settings:
    return Settings(
        _env_file=None,
        customer_return_bitrix_writes_enabled=True,
        customer_return_bitrix_webhook_url="https://crm.example.invalid/rest/1/token",
        customer_return_bitrix_group_id=13,
        customer_return_bitrix_created_by_user_id=123,
        customer_return_bitrix_responsible_user_id=456,
        customer_return_bitrix_accomplice_user_ids=[6357],
        customer_return_bitrix_auditor_user_ids=[130757, 131016],
        customer_return_worker_batch_size=25,
        customer_return_worker_lease_seconds=300,
        customer_return_worker_max_attempts=max_attempts,
    )


def _seed_arrival(engine, *, now: datetime) -> int:
    with Session(engine) as session:
        shipment = CustomerReturnShipment(
            carrier="cdek",
            tracking_number="CDEK-3507",
            status="arrived_at_pickup_point",
            status_changed_at=now,
            source="manual",
            onec_order_ref="ORDER-3507",
            storage_deadline_at=now + timedelta(days=5),
            arrived_at=now,
            updated_at=now,
        )
        session.add(shipment)
        session.flush()
        session.add(
            CustomerReturnAction(
                shipment_id=shipment.id,
                action_type=customer_returns.ACTION_ARRIVAL_TASK,
                status="pending",
                due_at=now,
                dedupe_key=_dedupe("arrival", shipment.id),
                updated_at=now,
            )
        )
        session.commit()
        return shipment.id


def _add_action(engine, *, shipment_id: int, action_type: str, now: datetime) -> None:
    with Session(engine) as session:
        session.add(
            CustomerReturnAction(
                shipment_id=shipment_id,
                action_type=action_type,
                status="pending",
                due_at=now,
                dedupe_key=_dedupe(action_type, shipment_id),
                updated_at=now,
            )
        )
        session.commit()


def _dedupe(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def test_worker_dry_run_is_read_only_and_apply_uses_one_task(tmp_path: Path) -> None:
    database_url, engine = _database(tmp_path, "worker-flow.db")
    now = datetime.now(timezone.utc)
    shipment_id = _seed_arrival(engine, now=now)
    api = FakeCustomerReturnBitrixApi()

    dry_run = worker_main(
        ["--database-url", database_url, "--compact"],
        settings_override=_settings(),
        api=api,
        now=now + timedelta(seconds=1),
    )
    assert dry_run["wouldDeliver"] == 1
    assert api.calls == []
    with Session(engine) as session:
        action = session.scalar(select(CustomerReturnAction))
        assert action is not None
        assert action.status == "pending"
        assert action.attempt_count == 0

    created = worker_main(
        ["--apply", "--database-url", database_url, "--compact"],
        settings_override=_settings(),
        api=api,
        now=now + timedelta(seconds=2),
    )
    assert created["completed"] == 1
    assert api.calls == ["ensure_task"]
    assert len(api.tasks) == 1

    _add_action(
        engine,
        shipment_id=shipment_id,
        action_type=customer_returns.ACTION_STORAGE_REMINDER_3D,
        now=now,
    )
    reminder = worker_main(
        ["--apply", "--database-url", database_url, "--compact"],
        settings_override=_settings(),
        api=api,
        now=now + timedelta(seconds=3),
    )
    assert reminder["completed"] == 1
    assert api.calls[-1] == "ensure_comment"

    with Session(engine) as session:
        customer_returns.confirm_pickup(
            session,
            shipment_id,
            actor_bitrix_user_id="6357",
            occurred_at=now + timedelta(minutes=1),
        )
    control = worker_main(
        ["--apply", "--database-url", database_url, "--compact"],
        settings_override=_settings(),
        api=api,
        now=now + timedelta(minutes=2),
    )
    assert control["completed"] == 1
    assert api.calls[-1] == "ensure_comment"

    with Session(engine) as session:
        customer_returns.confirm_onec_return(
            session,
            shipment_id,
            onec_return_ref="0xRETURN3507",
            occurred_at=now + timedelta(minutes=3),
        )
    closed = worker_main(
        ["--apply", "--database-url", database_url, "--compact"],
        settings_override=_settings(),
        api=api,
        now=now + timedelta(minutes=4),
    )
    assert closed["completed"] == 1
    assert api.calls[-2:] == ["ensure_comment", "ensure_completed"]
    assert api.completed_task_ids == ["9001"]
    assert len(api.tasks) == 1

    with Session(engine) as session:
        actions = list(session.scalars(select(CustomerReturnAction)).all())
        assert {action.status for action in actions} == {"completed"}
        assert all(action.lease_token is None for action in actions)
        assert all(action.leased_until is None for action in actions)

    engine.dispose()


def test_worker_failure_retries_without_storing_exception_text(tmp_path: Path) -> None:
    database_url, engine = _database(tmp_path, "worker-failure.db")
    now = datetime.now(timezone.utc)
    _seed_arrival(engine, now=now)

    result = worker_main(
        ["--apply", "--database-url", database_url, "--compact"],
        settings_override=_settings(max_attempts=2),
        api=FakeCustomerReturnBitrixApi(fail_task=True),
        now=now + timedelta(seconds=1),
    )

    assert result["retryPending"] == 1
    assert result["results"][0]["errorCode"] == "RuntimeError"
    with Session(engine) as session:
        action = session.scalar(select(CustomerReturnAction))
        assert action is not None
        assert action.status == "pending"
        assert action.attempt_count == 1
        assert action.last_error == "RuntimeError"
        assert "secret-bearing" not in action.last_error
        assert action.lease_token is None
        assert action.next_attempt_at is not None

    too_early = worker_main(
        ["--apply", "--database-url", database_url, "--compact"],
        settings_override=_settings(max_attempts=2),
        api=FakeCustomerReturnBitrixApi(fail_task=True),
        now=now + timedelta(seconds=30),
    )
    assert too_early["claimed"] == 0

    exhausted = worker_main(
        ["--apply", "--database-url", database_url, "--compact"],
        settings_override=_settings(max_attempts=2),
        api=FakeCustomerReturnBitrixApi(fail_task=True),
        now=now + timedelta(seconds=62),
    )
    assert exhausted["failed"] == 1
    with Session(engine) as session:
        action = session.scalar(select(CustomerReturnAction))
        assert action is not None
        assert action.status == "failed"
        assert action.attempt_count == 2
        assert action.next_attempt_at is None

    engine.dispose()


def test_worker_check_reports_incomplete_enabled_configuration() -> None:
    result = worker_main(
        ["--check", "--compact"],
        settings_override=Settings(
            _env_file=None,
            customer_return_bitrix_writes_enabled=True,
        ),
    )

    assert result["ready"] is False
    assert result["errors"] == [
        "bitrix_webhook_missing",
        "group_missing",
        "responsible_user_missing",
    ]


def test_concrete_writer_deduplicates_task_comment_and_completion() -> None:
    client = FakeRestClient()
    writer = CustomerReturnBitrixWriter(client)  # type: ignore[arg-type]
    config = CustomerReturnBitrixConfig.from_settings(_settings())
    client.task_rows = [
        {
            "id": "9001",
            "title": "Возврат",
            "description": "Служебная метка [#mm-customer-return:1]",
        }
    ]
    client.comment_rows = [{"ID": "7001", "POST_MESSAGE": "[#mm-customer-return-action:2]"}]
    client.task_status = "5"

    task_id = writer.ensure_task(
        title="Возврат",
        description="Описание [#mm-customer-return:1]",
        marker="[#mm-customer-return:1]",
        config=config,
        deadline=None,
    )
    comment_id = writer.ensure_comment(
        task_id="9001",
        marker="[#mm-customer-return-action:2]",
        message="Напоминание",
    )
    writer.ensure_completed(task_id="9001")

    assert task_id == "9001"
    assert comment_id == "7001"
    assert client.added_tasks == 0
    assert client.added_comments == 0
    assert client.completed == 0
