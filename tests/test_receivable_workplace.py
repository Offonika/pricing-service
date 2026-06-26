from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import bitrix_receivables as bitrix_receivables_api
from app.api import receivable_workplace as receivable_workplace_api
from app.api.dependencies import get_db
from app.core.config import Settings, get_settings
from app.main import app
from app.models import (
    ReceivableCase,
    ReceivableWorkEvent,
    ReceivableWorkItem,
    StaffMember,
    TelephonyUserLineSnapshot,
)
from app.schemas.receivable_workplace import ReceivableWorkplaceActionRequest
from app.services import bitrix_receivables_auth
from app.services.bitrix_receivables_auth import (
    ReceivablesAccess,
    create_receivables_session_token,
)
from app.services.receivable_workflow import sync_receivable_workflow
from app.services.receivable_workplace import (
    apply_receivable_workplace_action,
    build_receivable_workplace,
)
from app.services.receivables import CASE_BUYERS, CASE_OVERDUE
from tests.test_receivable_workflow import _settings


def _case(
    *,
    snapshot_date: date,
    segment: str = CASE_BUYERS,
    counterparty_ref: str = "cp-1",
    balance: Decimal = Decimal("12500"),
    origin_date: datetime = datetime(2026, 6, 10, 12, 0),
    due_date: datetime | None = None,
    overdue_days: int | None = None,
    counterparty_name: str = "Клиент 1",
    department_ref: str = "dep-1",
    department_name: str = "01. Горбушкин Двор",
    current_manager_ref: str = "staff-1",
    current_manager_name: str = "Менеджер 1",
) -> ReceivableCase:
    return ReceivableCase(
        snapshot_date=snapshot_date,
        segment=segment,
        owner_type="current_manager",
        recommendation="Проверить просрочку.",
        counterparty_ref=counterparty_ref,
        counterparty_name=counterparty_name,
        current_balance=balance,
        aged_bucket="1-30",
        activity_segment="active",
        origin_document_ref="sale-1",
        origin_document_number="РБГУ0001",
        origin_document_date=origin_date,
        origin_manager_ref="mgr-origin",
        origin_manager_name="Ответственный по накладной",
        current_manager_ref=current_manager_ref,
        current_manager_name=current_manager_name,
        department_ref=department_ref,
        department_name=department_name,
        planned_payment_date=due_date,
        credit_depth_days=None,
        shipment_ban=False,
        payment_term_source=None if due_date is None else "planned_payment_date",
        due_date=due_date,
        overdue_days=overdue_days,
        is_overdue=segment == CASE_OVERDUE,
        chain_documents=[
            {
                "event_type": "sale",
                "document_ref": "sale-1",
                "document_number": "РБГУ0001",
                "document_date": origin_date.isoformat(),
                "amount_delta": "12500",
            }
        ],
    )


def _staff_member() -> StaffMember:
    return StaffMember(
        source="onec_physical_person",
        external_ref="staff-1",
        full_name="Менеджер 1",
        role_code="manager",
        role_name="Менеджер",
        department_ref="dep-1",
        department_name="01. Горбушкин Двор",
        store_ref=None,
        store_name=None,
        employment_status="active",
    )


def _telephony_snapshot(
    *,
    snapshot_date: date,
    bitrix_user_id: str,
    user_ref_hex: str = "user-1",
    staff_department_ref: str | None = "dep-1",
    department_ref_hex: str | None = None,
) -> TelephonyUserLineSnapshot:
    return TelephonyUserLineSnapshot(
        snapshot_date=snapshot_date,
        mapping_source="test",
        user_ref_hex=user_ref_hex,
        user_name="Менеджер 1",
        staff_department_ref=staff_department_ref,
        department_ref_hex=department_ref_hex,
        bitrix_user_id=bitrix_user_id,
        bitrix_full_name="Иван Петров",
        employment_status="active",
        is_marked=False,
        has_extension=False,
        has_bitrix=True,
    )


def _bitrix_settings() -> Settings:
    return Settings(
        management_internal_api_token="secret-token",
        receivable_workplace_bitrix_enabled=True,
        receivable_workplace_bitrix_allowed_domains=["crm.master-mobile.ru"],
        receivable_workplace_bitrix_allowed_member_ids=["member-1"],
        receivable_workplace_bitrix_full_access_user_ids=["42"],
        receivable_workplace_bitrix_session_secret="test-receivables-session-secret",
        receivable_workplace_bitrix_session_ttl_seconds=3600,
    )


class _FakeBitrixResponse:
    def __init__(self, user_id: str = "42") -> None:
        self.user_id = user_id

    def __enter__(self) -> _FakeBitrixResponse:
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(
            {"result": {"ID": self.user_id, "NAME": "Иван", "LAST_NAME": "Петров"}}
        ).encode()


def _override_receivables_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(bitrix_receivables_api, "get_settings", lambda: settings)
    monkeypatch.setattr(bitrix_receivables_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(receivable_workplace_api, "get_settings", lambda: settings)


def _bitrix_token(
    settings: Settings,
    *,
    user_id: str,
    access: ReceivablesAccess,
) -> str:
    token, _ = create_receivables_session_token(
        domain="crm.master-mobile.ru",
        member_id="member-1",
        user_id=user_id,
        user_name="Иван Петров",
        access=access,
        settings=settings,
    )
    return token


def test_receivable_workplace_uses_default_credit_depth_without_onec_write(
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all([_case(snapshot_date=as_of), _staff_member()])

    result = build_receivable_workplace(db_session, snapshot_date=as_of)

    assert result.source_status == "ready"
    assert result.summary.row_count == 1
    assert result.summary.total_receivable == Decimal("12500.00")
    assert result.summary.total_overdue == Decimal("12500.00")
    assert result.summary.credit_depth_default_count == 1
    item = result.payload[0]
    assert item.needs_credit_depth_default is True
    assert item.effective_due_date == datetime(2026, 6, 17, 12, 0)
    assert item.effective_overdue_days == 6
    assert item.no_phone_marker is True
    assert item.staff_options[0].staff_ref == "staff-1"
    assert item.documents[0].document_number == "РБГУ0001"


def test_receivable_workplace_action_survives_daily_workflow_sync(db_session: Session) -> None:
    as_of = date(2026, 6, 23)
    due_date = datetime(2026, 6, 16)
    db_session.add_all(
        [
            _case(snapshot_date=as_of, due_date=due_date, overdue_days=7),
            _case(snapshot_date=as_of, segment=CASE_OVERDUE, due_date=due_date, overdue_days=7),
            _staff_member(),
        ]
    )
    payload = ReceivableWorkplaceActionRequest(
        status="waiting_payment",
        contacted_staff_ref="staff-1",
        promised_payment_date=date(2026, 6, 25),
        next_action_date=date(2026, 6, 24),
        payment_postponed=True,
        comment="Клиент обещал оплатить завтра.",
    )

    response = apply_receivable_workplace_action(
        db_session,
        snapshot_date=as_of,
        counterparty_ref="cp-1",
        payload=payload,
    )
    db_session.commit()

    assert response is not None
    assert response.item.status == "waiting_payment"
    assert response.item.contacted_staff_name == "Менеджер 1"
    assert response.item.payment_postponed is True
    assert response.item.comment == "Клиент обещал оплатить завтра."
    event = db_session.scalar(select(ReceivableWorkEvent))
    assert event is not None
    assert event.event_type == "manager_update"

    sync_receivable_workflow(
        db_session,
        as_of=as_of,
        phone_by_counterparty={"cp-1": "+79990000000"},
        settings=_settings(),
        dry_run_bitrix=True,
    )
    db_session.commit()

    item = db_session.scalar(select(ReceivableWorkItem))
    assert item is not None
    assert item.status == "waiting_payment"
    assert item.last_contact_comment == "Клиент обещал оплатить завтра."
    assert item.payload is not None
    assert item.payload["contacted_staff_ref"] == "staff-1"
    assert item.payload["payment_postponed"] is True


def test_receivable_workplace_action_can_clear_manual_fields(db_session: Session) -> None:
    as_of = date(2026, 6, 23)
    due_date = datetime(2026, 6, 16)
    db_session.add_all(
        [
            _case(snapshot_date=as_of, due_date=due_date, overdue_days=7),
            _staff_member(),
        ]
    )
    apply_receivable_workplace_action(
        db_session,
        snapshot_date=as_of,
        counterparty_ref="cp-1",
        payload=ReceivableWorkplaceActionRequest(
            status="waiting_payment",
            contacted_staff_ref="staff-1",
            promised_payment_date=date(2026, 6, 25),
            next_action_date=date(2026, 6, 24),
            payment_postponed=True,
            comment="Обещали оплату.",
        ),
    )

    response = apply_receivable_workplace_action(
        db_session,
        snapshot_date=as_of,
        counterparty_ref="cp-1",
        payload=ReceivableWorkplaceActionRequest(
            contacted_staff_ref=None,
            contacted_staff_name=None,
            promised_payment_date=None,
            next_action_date=None,
            payment_postponed=False,
            comment="",
        ),
    )
    db_session.flush()

    item = db_session.scalar(select(ReceivableWorkItem))
    assert response is not None
    assert item is not None
    assert item.status == "waiting_payment"
    assert item.promised_payment_date is None
    assert item.next_action_date is None
    assert item.last_contact_comment is None
    assert item.payload is not None
    assert item.payload["contacted_staff_ref"] is None
    assert item.payload["contacted_staff_name"] is None
    assert item.payload["payment_postponed"] is False
    assert response.item.contacted_staff_ref is None
    assert response.item.payment_postponed is False


def test_receivable_workplace_api_requires_token_and_returns_payload(
    monkeypatch,
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add(_case(snapshot_date=as_of))
    db_session.commit()

    def override_db():
        yield db_session

    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
    get_settings.cache_clear()
    app.dependency_overrides = {get_db: override_db}
    client = TestClient(app)
    try:
        unauthorized = client.get("/api/receivables/workplace", params={"date": as_of.isoformat()})
        assert unauthorized.status_code == 401

        response = client.get(
            "/api/receivables/workplace",
            params={"date": as_of.isoformat()},
            headers={"Authorization": "Bearer secret-token"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["summary"]["row_count"] == 1
        assert payload["payload"][0]["counterparty_ref"] == "cp-1"
    finally:
        app.dependency_overrides = {}
        get_settings.cache_clear()


def test_bitrix_receivables_session_endpoint_issues_full_access_token(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    settings = _bitrix_settings()
    _override_receivables_settings(monkeypatch, settings)

    def fake_urlopen(request, timeout):  # noqa: ANN001, ANN202
        assert request.full_url == "https://crm.master-mobile.ru/rest/user.current.json"
        assert timeout == settings.receivable_workplace_bitrix_rest_timeout_seconds
        assert json.loads(request.data.decode()) == {"auth": "bitrix-access-token"}
        return _FakeBitrixResponse(user_id="42")

    monkeypatch.setattr(bitrix_receivables_auth.urllib.request, "urlopen", fake_urlopen)

    def override_db():
        yield db_session

    app.dependency_overrides = {get_db: override_db}
    client = TestClient(app)
    try:
        response = client.post(
            "/api/bitrix/receivables/session",
            json={
                "access_token": "bitrix-access-token",
                "domain": "crm.master-mobile.ru",
                "member_id": "member-1",
            },
        )
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["session_token"]
    assert body["access_level"] == "full"
    assert body["department_refs"] == []
    assert body["user"] == {"user_id": "42", "name": "Иван Петров"}


def test_bitrix_receivables_session_endpoint_rejects_unknown_domain(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    settings = _bitrix_settings()
    _override_receivables_settings(monkeypatch, settings)

    def override_db():
        yield db_session

    app.dependency_overrides = {get_db: override_db}
    client = TestClient(app)
    try:
        response = client.post(
            "/api/bitrix/receivables/session",
            json={
                "access_token": "bitrix-access-token",
                "domain": "other.example",
                "member_id": "member-1",
            },
        )
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 403


def test_bitrix_receivables_session_endpoint_rejects_user_without_department(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    settings = _bitrix_settings()
    _override_receivables_settings(monkeypatch, settings)
    monkeypatch.setattr(
        bitrix_receivables_auth.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeBitrixResponse(user_id="77"),
    )

    def override_db():
        yield db_session

    app.dependency_overrides = {get_db: override_db}
    client = TestClient(app)
    try:
        response = client.post(
            "/api/bitrix/receivables/session",
            json={
                "access_token": "bitrix-access-token",
                "domain": "crm.master-mobile.ru",
                "member_id": "member-1",
            },
        )
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 403
    assert response.json()["detail"] == "не найдено подразделение для доступа"


def test_bitrix_receivables_department_user_sees_only_allowed_department(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(snapshot_date=as_of, counterparty_ref="cp-1", department_ref="dep-1"),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-2",
                counterparty_name="Клиент 2",
                department_ref="dep-2",
                department_name="02. СПБ",
                current_manager_ref="staff-2",
                current_manager_name="Менеджер 2",
            ),
        ]
    )
    db_session.commit()
    settings = _bitrix_settings()
    _override_receivables_settings(monkeypatch, settings)
    token = _bitrix_token(
        settings,
        user_id="77",
        access=ReceivablesAccess(access_level="department", department_refs=frozenset({"dep-1"})),
    )

    def override_db():
        yield db_session

    app.dependency_overrides = {get_db: override_db}
    client = TestClient(app)
    try:
        response = client.get(
            "/api/receivables/workplace",
            params={"date": as_of.isoformat()},
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["row_count"] == 1
    assert [item["counterparty_ref"] for item in body["payload"]] == ["cp-1"]


def test_bitrix_receivables_full_access_user_sees_all_departments(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(snapshot_date=as_of, counterparty_ref="cp-1", department_ref="dep-1"),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-2",
                counterparty_name="Клиент 2",
                department_ref="dep-2",
                department_name="02. СПБ",
            ),
        ]
    )
    db_session.commit()
    settings = _bitrix_settings()
    _override_receivables_settings(monkeypatch, settings)
    token = _bitrix_token(
        settings,
        user_id="42",
        access=ReceivablesAccess(access_level="full", department_refs=frozenset()),
    )

    def override_db():
        yield db_session

    app.dependency_overrides = {get_db: override_db}
    client = TestClient(app)
    try:
        response = client.get(
            "/api/receivables/workplace",
            params={"date": as_of.isoformat()},
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["row_count"] == 2
    assert {item["counterparty_ref"] for item in body["payload"]} == {"cp-1", "cp-2"}


def test_bitrix_receivables_department_user_cannot_patch_other_department(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add(
        _case(
            snapshot_date=as_of,
            counterparty_ref="cp-2",
            counterparty_name="Клиент 2",
            department_ref="dep-2",
            department_name="02. СПБ",
        )
    )
    db_session.commit()
    settings = _bitrix_settings()
    _override_receivables_settings(monkeypatch, settings)
    token = _bitrix_token(
        settings,
        user_id="77",
        access=ReceivablesAccess(access_level="department", department_refs=frozenset({"dep-1"})),
    )

    def override_db():
        yield db_session

    app.dependency_overrides = {get_db: override_db}
    client = TestClient(app)
    try:
        response = client.patch(
            "/api/receivables/workplace/cp-2",
            params={"date": as_of.isoformat()},
            headers={"Authorization": f"Bearer {token}"},
            json={"status": "waiting_payment"},
        )
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 403


def test_internal_token_remains_full_access(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(snapshot_date=as_of, counterparty_ref="cp-1", department_ref="dep-1"),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-2",
                counterparty_name="Клиент 2",
                department_ref="dep-2",
                department_name="02. СПБ",
            ),
        ]
    )
    db_session.commit()
    settings = _bitrix_settings()
    _override_receivables_settings(monkeypatch, settings)

    def override_db():
        yield db_session

    app.dependency_overrides = {get_db: override_db}
    client = TestClient(app)
    try:
        response = client.get(
            "/api/receivables/workplace",
            params={"date": as_of.isoformat()},
            headers={"Authorization": "Bearer secret-token"},
        )
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 200
    assert response.json()["summary"]["row_count"] == 2


def test_receivables_folder_recommendations_are_filtered_for_bitrix_department(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    settings = _bitrix_settings()
    _override_receivables_settings(monkeypatch, settings)
    token = _bitrix_token(
        settings,
        user_id="77",
        access=ReceivablesAccess(access_level="department", department_refs=frozenset({"dep-1"})),
    )

    class FakeEngine:
        def dispose(self) -> None:
            return None

    monkeypatch.setattr(receivable_workplace_api, "_build_onec_engine", lambda: FakeEngine())
    monkeypatch.setattr(
        receivable_workplace_api,
        "build_counterparty_folder_recommendations",
        lambda *args, **kwargs: {
            "snapshot_date": as_of,
            "report_revision": "test",
            "summary": {"source_snapshot_count": 2, "total_count": 2},
            "payload": [
                {
                    "snapshot_date": as_of,
                    "counterparty_ref": "cp-1",
                    "counterparty_name": "Клиент 1",
                    "current_balance": Decimal("1000"),
                    "debt_department_ref": "dep-1",
                    "is_overdue": True,
                    "status": "move_recommended",
                },
                {
                    "snapshot_date": as_of,
                    "counterparty_ref": "cp-2",
                    "counterparty_name": "Клиент 2",
                    "current_balance": Decimal("2000"),
                    "debt_department_ref": "dep-2",
                    "is_overdue": True,
                    "status": "move_recommended",
                },
            ],
        },
    )

    def override_db():
        yield db_session

    app.dependency_overrides = {get_db: override_db}
    client = TestClient(app)
    try:
        response = client.get(
            "/api/receivables/workplace/folder-recommendations",
            params={"date": as_of.isoformat()},
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 200
    body = response.json()
    assert [item["counterparty_ref"] for item in body["payload"]] == ["cp-1"]
    assert body["summary"]["total_count"] == 1
