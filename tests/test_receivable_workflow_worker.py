from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import ReceivableCase, ReceivableSmsLog, ReceivableWorkItem
from app.services.receivable_workflow import STATUS_CALLING, STATUS_CLOSED
from app.services.receivables import CASE_BUYERS
from app.workers import receivable_workflow as worker


class FakeBitrixClient:
    def __init__(self, *, fail_stable_key: str | None = None) -> None:
        self.fail_stable_key = fail_stable_key
        self.added: list[str] = []
        self.updated: list[str] = []
        self.next_id = 100

    @staticmethod
    def _stable_key(fields: dict[str, Any]) -> str:
        return next(
            str(value) for value in fields.values() if str(value).startswith("receivables|buyers|")
        )

    def list_items_by_ref(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def add_smart_process_item(
        self, *, entity_type_id: int, fields: dict[str, Any]
    ) -> tuple[str, str | None]:
        stable_key = self._stable_key(fields)
        if stable_key == self.fail_stable_key:
            raise RuntimeError("forced Bitrix error")
        self.added.append(stable_key)
        self.next_id += 1
        return str(self.next_id), f"/crm/type/{entity_type_id}/details/{self.next_id}/"

    def update_smart_process_item(
        self, *, entity_type_id: int, item_id: str, fields: dict[str, Any]
    ) -> None:
        stable_key = self._stable_key(fields)
        if stable_key == self.fail_stable_key:
            raise RuntimeError("forced Bitrix error")
        self.updated.append(stable_key)


def _settings(**overrides: Any) -> Settings:
    data: dict[str, Any] = {
        "receivable_workflow_enabled": True,
        "receivable_sms_mode": "live",
        "receivable_bitrix_webhook_url": "https://example.invalid/rest/1/token",
        "receivable_bitrix_entity_type_id": 187,
        "receivable_bitrix_field_map": {
            "title": "TITLE",
            "stable_key": "UF_CRM_RECEIVABLE_STABLE_KEY",
            "counterparty_ref": "UF_CRM_RECEIVABLE_COUNTERPARTY_REF",
            "current_balance": "UF_CRM_RECEIVABLE_CURRENT_BALANCE",
        },
        "receivable_bitrix_stage_map": {
            "new_debt": "DT187_1:NEW",
            "calling": "DT187_1:CALLING",
            "closed": "DT187_1:CLOSED",
        },
        "receivable_workflow_department_refs": [],
        "receivable_workflow_department_names": [],
    }
    data.update(overrides)
    return Settings(**data)


def _case(snapshot_date: date, counterparty_ref: str, *, department_ref: str = "dep-1"):
    return ReceivableCase(
        snapshot_date=snapshot_date,
        segment=CASE_BUYERS,
        owner_type="sales_manager",
        recommendation="Позвонить клиенту",
        counterparty_ref=counterparty_ref,
        counterparty_name=counterparty_ref,
        current_balance=Decimal("1000.00"),
        aged_bucket="1-7",
        activity_segment="active",
        origin_document_ref=f"sale-{counterparty_ref}",
        origin_document_number=f"РТУ-{counterparty_ref}",
        origin_document_date=datetime.combine(snapshot_date, datetime.min.time()),
        department_ref=department_ref,
        department_name=department_ref,
        due_date=datetime.combine(snapshot_date, datetime.min.time()),
        overdue_days=1,
        is_overdue=True,
        chain_documents=[],
    )


def _workplace(refs_by_date: dict[date, tuple[str, ...]]):
    def build(*args: Any, snapshot_date: date, **kwargs: Any):
        return SimpleNamespace(
            payload=[
                SimpleNamespace(
                    counterparty_ref=ref,
                    department_ref="dep-1",
                    department_name="dep-1",
                )
                for ref in refs_by_date.get(snapshot_date, ())
            ]
        )

    return build


def _configure(
    monkeypatch,
    sqlite_engine,
    *,
    settings: Settings,
    client: FakeBitrixClient,
    refs_by_date: dict[date, tuple[str, ...]],
) -> None:
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "_get_app_engine", lambda: sqlite_engine)
    monkeypatch.setattr(worker, "build_bitrix_client_from_settings", lambda value: client)
    monkeypatch.setattr(worker, "build_receivable_workplace", _workplace(refs_by_date))


def test_plan_rolls_back_and_all_departments_bypasses_scope(
    monkeypatch, sqlite_engine, db_session: Session
) -> None:
    as_of = date(2026, 7, 16)
    db_session.add(_case(as_of, "cp-a"))
    db_session.commit()
    client = FakeBitrixClient()
    _configure(
        monkeypatch,
        sqlite_engine,
        settings=_settings(receivable_workflow_department_refs=["other-department"]),
        client=client,
        refs_by_date={as_of: ("cp-a",)},
    )

    result = worker.run_receivable_workflow_sync(
        as_of=as_of,
        plan=True,
        bitrix_only=True,
        allow_closure=False,
        all_departments=True,
    )

    db_session.expire_all()
    assert result["status"] == "ok"
    assert result["selected_counterparty_count"] == 1
    assert [action["action"] for action in result["plan_actions"]] == ["create"]
    assert client.added == []
    assert db_session.scalar(select(ReceivableWorkItem)) is None
    assert db_session.scalar(select(func.count()).select_from(ReceivableSmsLog)) == 0


def test_batch_sync_stops_on_error_without_sms_and_reruns_idempotently(
    monkeypatch, sqlite_engine, db_session: Session
) -> None:
    as_of = date(2026, 7, 16)
    refs = ("cp-a", "cp-b", "cp-c")
    db_session.add_all([_case(as_of, ref) for ref in refs])
    db_session.commit()
    failing = FakeBitrixClient(fail_stable_key="receivables|buyers|cp-b")
    _configure(
        monkeypatch,
        sqlite_engine,
        settings=_settings(),
        client=failing,
        refs_by_date={as_of: refs},
    )

    failed = worker.run_receivable_workflow_sync(
        as_of=as_of,
        force=True,
        bitrix_only=True,
        allow_closure=False,
        all_departments=True,
        batch_size=1,
    )

    assert failed["status"] == "error"
    assert failed["processed_counterparty_refs"] == ["cp-a", "cp-b"]
    assert failed["sms_created"] == 0
    assert db_session.scalar(select(func.count()).select_from(ReceivableSmsLog)) == 0

    healthy = FakeBitrixClient()
    monkeypatch.setattr(worker, "build_bitrix_client_from_settings", lambda value: healthy)
    rerun = worker.run_receivable_workflow_sync(
        as_of=as_of,
        force=True,
        bitrix_only=True,
        allow_closure=False,
        all_departments=True,
        batch_size=1,
    )

    db_session.expire_all()
    assert rerun["status"] == "ok"
    assert len(rerun["processed_counterparty_refs"]) == 3
    assert db_session.scalar(select(func.count()).select_from(ReceivableWorkItem)) == 3
    assert db_session.scalar(select(func.count()).select_from(ReceivableSmsLog)) == 0


def test_full_sync_closes_only_after_two_snapshots(
    monkeypatch, sqlite_engine, db_session: Session
) -> None:
    first_date = date(2026, 7, 15)
    second_date = date(2026, 7, 16)
    third_date = date(2026, 7, 17)
    db_session.add_all(
        [
            _case(first_date, "cp-a"),
            _case(second_date, "cp-b"),
            _case(third_date, "cp-b"),
            ReceivableWorkItem(
                stable_key="receivables|buyers|cp-a",
                counterparty_ref="cp-a",
                counterparty_name="cp-a",
                status=STATUS_CALLING,
                current_balance=Decimal("1000.00"),
                bitrix_item_id=10,
            ),
        ]
    )
    db_session.commit()
    client = FakeBitrixClient()
    _configure(
        monkeypatch,
        sqlite_engine,
        settings=_settings(),
        client=client,
        refs_by_date={
            first_date: ("cp-a",),
            second_date: ("cp-b",),
            third_date: ("cp-b",),
        },
    )

    first = worker.run_receivable_workflow_sync(
        as_of=second_date,
        force=True,
        bitrix_only=True,
        all_departments=True,
    )
    db_session.expire_all()
    item = db_session.scalar(
        select(ReceivableWorkItem).where(ReceivableWorkItem.counterparty_ref == "cp-a")
    )
    assert first["closure_deferred"] == 1
    assert item is not None and item.status == STATUS_CALLING

    second = worker.run_receivable_workflow_sync(
        as_of=third_date,
        force=True,
        bitrix_only=True,
        all_departments=True,
    )
    db_session.expire_all()
    assert second["work_items_closed"] == 1
    assert item is not None and item.status == STATUS_CLOSED


def test_full_sync_does_not_create_missing_bitrix_card_while_closing(
    monkeypatch, sqlite_engine, db_session: Session
) -> None:
    first_date = date(2026, 7, 15)
    second_date = date(2026, 7, 16)
    missing_ref = "cp-missing-in-bitrix"
    db_session.add(
        ReceivableWorkItem(
            stable_key=f"receivables|buyers|{missing_ref}",
            counterparty_ref=missing_ref,
            counterparty_name=missing_ref,
            status=STATUS_CALLING,
            current_balance=Decimal("1000.00"),
        )
    )
    db_session.commit()
    client = FakeBitrixClient()
    _configure(
        monkeypatch,
        sqlite_engine,
        settings=_settings(),
        client=client,
        refs_by_date={first_date: (), second_date: ("cp-active",)},
    )
    db_session.add(_case(second_date, "cp-active"))
    db_session.commit()

    result = worker.run_receivable_workflow_sync(
        as_of=second_date,
        force=True,
        bitrix_only=True,
        all_departments=True,
    )

    db_session.expire_all()
    item = db_session.scalar(
        select(ReceivableWorkItem).where(ReceivableWorkItem.counterparty_ref == missing_ref)
    )
    assert result["work_items_closed"] == 1
    assert item is not None and item.status == STATUS_CLOSED
    assert f"receivables|buyers|{missing_ref}" not in client.added
