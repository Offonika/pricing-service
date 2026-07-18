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
    ReceivableBalanceSnapshot,
    ReceivableBitrixUserAccess,
    ReceivableCase,
    ReceivableFolderRecommendationCache,
    ReceivableLedgerEvent,
    ReceivableOpenDebtCache,
    ReceivableWorkEvent,
    ReceivableWorkItem,
    StaffMember,
    TelephonyUserLineSnapshot,
)
from app.schemas.receivable_workplace import ReceivableWorkplaceActionRequest
from app.services import bitrix_receivables_auth, receivable_workplace_cache
from app.services import receivable_workplace as receivable_workplace_service
from app.services.bitrix_receivables_auth import (
    ReceivablesAccess,
    create_receivables_session_token,
)
from app.services.counterparty_folder_recommendations import (
    evaluate_open_debt_source_freshness,
)
from app.services.receivable_department_aliases import (
    TEPLY_STAN_RECEIVABLES_REF,
    TEPLY_STAN_STAFF_DEPARTMENT_REF,
    TEPLY_STAN_TELEPHONY_STORE_REF,
)
from app.services.receivable_workflow import stable_key_for_counterparty, sync_receivable_workflow
from app.services.receivable_workplace import (
    apply_receivable_workplace_action,
    build_receivable_workplace,
)
from app.services.receivables import CASE_BUYERS, CASE_OVERDUE
from tests.test_receivable_workflow import _settings


def _ledger_event(
    *, document_date: datetime, business_key: str = "event-1"
) -> ReceivableLedgerEvent:
    return ReceivableLedgerEvent(
        source="onec",
        source_layer="regular_receivables",
        business_key=business_key,
        event_type="sale",
        external_document_ref=f"doc-{business_key}",
        external_document_number=business_key,
        external_document_date=document_date,
        counterparty_ref="cp-1",
        counterparty_name="Клиент 1",
        amount_delta=Decimal("1000.00"),
    )


def _case(
    *,
    snapshot_date: date,
    segment: str = CASE_BUYERS,
    counterparty_ref: str = "cp-1",
    balance: Decimal = Decimal("12500"),
    origin_date: datetime = datetime(2026, 6, 10, 12, 0),
    due_date: datetime | None = None,
    overdue_days: int | None = None,
    counterparty_code: str | None = None,
    counterparty_name: str = "Клиент 1",
    department_ref: str | None = "dep-1",
    department_name: str | None = "01. Горбушкин Двор",
    current_manager_ref: str = "staff-1",
    current_manager_name: str = "Менеджер 1",
    credit_depth_days: int | None = None,
) -> ReceivableCase:
    return ReceivableCase(
        snapshot_date=snapshot_date,
        segment=segment,
        owner_type="current_manager",
        recommendation="Проверить просрочку.",
        counterparty_ref=counterparty_ref,
        counterparty_code=counterparty_code,
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
        credit_depth_days=credit_depth_days,
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


def _staff_member(
    *,
    external_ref: str = "staff-1",
    full_name: str = "Менеджер 1",
    role_code: str = "manager",
    role_name: str = "Менеджер",
    department_ref: str = "dep-1",
    department_name: str = "01. Горбушкин Двор",
    store_ref: str | None = None,
    store_name: str | None = None,
    employment_status: str = "active",
) -> StaffMember:
    return StaffMember(
        source="onec_physical_person",
        external_ref=external_ref,
        full_name=full_name,
        role_code=role_code,
        role_name=role_name,
        department_ref=department_ref,
        department_name=department_name,
        store_ref=store_ref,
        store_name=store_name,
        employment_status=employment_status,
    )


def _telephony_snapshot(
    *,
    snapshot_date: date,
    bitrix_user_id: str,
    user_ref_hex: str = "user-1",
    staff_department_ref: str | None = "dep-1",
    department_ref_hex: str | None = None,
    staff_department_name: str | None = None,
    staff_store_ref: str | None = None,
    staff_store_name: str | None = None,
    store_ref_hex: str | None = None,
    store_name: str | None = None,
) -> TelephonyUserLineSnapshot:
    return TelephonyUserLineSnapshot(
        snapshot_date=snapshot_date,
        mapping_source="test",
        user_ref_hex=user_ref_hex,
        user_name="Менеджер 1",
        staff_department_ref=staff_department_ref,
        staff_department_name=staff_department_name,
        staff_store_ref=staff_store_ref,
        staff_store_name=staff_store_name,
        department_ref_hex=department_ref_hex,
        store_ref_hex=store_ref_hex,
        store_name=store_name,
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
    def __init__(
        self,
        user_id: str = "42",
        *,
        result: dict | list | None = None,
        result_extra: dict | None = None,
    ) -> None:
        self.user_id = user_id
        self.result = result
        self.result_extra = result_extra or {}

    def __enter__(self) -> _FakeBitrixResponse:
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        result = self.result
        if result is None:
            result = {"ID": self.user_id, "NAME": "Иван", "LAST_NAME": "Петров"}
            result.update(self.result_extra)
        return json.dumps({"result": result}).encode()


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

    assert result.source_status == "fallback_live"
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
    status_options = {option.value: option.label for option in result.status_options}
    assert (
        status_options["not_ours_transfer"]
        == "Не наш, прошу перенести ответственным (указать РТУ если возможно)"
    )


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
        status="not_ours_transfer",
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
    assert response.item.status == "not_ours_transfer"
    assert response.item.contacted_staff_name == "Менеджер 1"
    assert response.item.payment_postponed is False
    assert response.item.payment_postponed_count == 1
    assert response.item.comment == "Клиент обещал оплатить завтра."
    event = db_session.scalar(select(ReceivableWorkEvent))
    assert event is not None
    assert event.event_type == "manager_update"
    assert event.payload["payment_postponed_added"] is True
    assert event.payload["payment_postponed_count"] == 1

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
    assert item.status == "not_ours_transfer"
    assert item.last_contact_comment == "Клиент обещал оплатить завтра."
    assert item.payload is not None
    assert item.payload["contacted_staff_ref"] == "staff-1"
    assert item.payload["payment_postponed"] is False
    assert item.payload["payment_postponed_count"] == 1


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
    assert item.payload["payment_postponed_count"] == 1
    assert response.item.contacted_staff_ref is None
    assert response.item.payment_postponed is False
    assert response.item.payment_postponed_count == 1


def test_receivable_workplace_uses_open_debt_document_for_overdue(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(
                snapshot_date=as_of,
                origin_date=datetime(2026, 1, 1, 12, 0),
            ),
            ReceivableBalanceSnapshot(
                snapshot_date=as_of,
                counterparty_ref="cp-1",
                counterparty_name="Клиент 1",
                current_balance=Decimal("12500"),
                origin_document_ref="sale-old",
                origin_document_number="РБГУ0001",
                origin_document_date=datetime(2026, 1, 1, 12, 0),
                origin_manager_ref="mgr-origin",
                origin_manager_name="Старый ответственный",
                current_manager_ref="staff-1",
                current_manager_name="Менеджер 1",
                department_ref="dep-1",
                department_name="01. Горбушкин Двор",
                planned_payment_date=None,
                credit_depth_days=None,
                shipment_ban=False,
                payment_term_source=None,
                due_date=None,
                overdue_days=None,
                is_overdue=True,
                aged_bucket="1-30",
                activity_segment="active",
            ),
            _staff_member(),
        ]
    )

    def fake_build_open_debt_documents_by_counterparty(*args, **kwargs):  # noqa: ANN001, ANN202
        return {
            "cp-1": [
                {
                    "document_ref": "sale-open",
                    "document_number": "РБГУ9999",
                    "document_date": datetime(2026, 6, 10, 9, 0),
                    "open_amount": Decimal("12500"),
                    "manager_name": "Менеджер ведомости",
                    "document_responsible_ref": "staff-debt",
                    "document_responsible_name": "Ответственный РТУ",
                }
            ]
        }

    monkeypatch.setattr(
        receivable_workplace_service,
        "build_open_debt_documents_by_counterparty",
        fake_build_open_debt_documents_by_counterparty,
    )

    result = build_receivable_workplace(db_session, snapshot_date=as_of)

    assert result.summary.row_count == 1
    item = result.payload[0]
    assert item.oldest_overdue_date == datetime(2026, 6, 10, 9, 0)
    assert item.effective_due_date == datetime(2026, 6, 17, 9, 0)
    assert item.effective_overdue_days == 6
    assert item.responsible_ref == "staff-debt"
    assert item.responsible_name == "Ответственный РТУ"
    assert item.needs_credit_depth_default is True
    assert item.documents[0].document_number == "РБГУ9999"
    assert item.documents[0].due_date == datetime(2026, 6, 17, 9, 0)
    assert item.documents[0].overdue_days == 6


def test_payment_postponed_is_counter_action(db_session: Session) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all([_case(snapshot_date=as_of), _staff_member()])

    first = apply_receivable_workplace_action(
        db_session,
        snapshot_date=as_of,
        counterparty_ref="cp-1",
        payload=ReceivableWorkplaceActionRequest(payment_postponed=True),
    )
    second = apply_receivable_workplace_action(
        db_session,
        snapshot_date=as_of,
        counterparty_ref="cp-1",
        payload=ReceivableWorkplaceActionRequest(status="waiting_payment"),
    )
    third = apply_receivable_workplace_action(
        db_session,
        snapshot_date=as_of,
        counterparty_ref="cp-1",
        payload=ReceivableWorkplaceActionRequest(payment_postponed=True),
    )
    db_session.flush()

    item = db_session.scalar(select(ReceivableWorkItem))
    assert first is not None
    assert second is not None
    assert third is not None
    assert first.item.payment_postponed is False
    assert first.item.payment_postponed_count == 1
    assert second.item.payment_postponed_count == 1
    assert third.item.payment_postponed is False
    assert third.item.payment_postponed_count == 2
    assert item is not None
    assert item.payload["payment_postponed"] is False
    assert item.payload["payment_postponed_count"] == 2


def test_payment_postponed_action_id_is_idempotent(db_session: Session) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all([_case(snapshot_date=as_of), _staff_member()])
    payload = ReceivableWorkplaceActionRequest(
        action_id="same-click",
        payment_postponed=True,
    )

    first = apply_receivable_workplace_action(
        db_session,
        snapshot_date=as_of,
        counterparty_ref="cp-1",
        payload=payload,
    )
    second = apply_receivable_workplace_action(
        db_session,
        snapshot_date=as_of,
        counterparty_ref="cp-1",
        payload=payload,
    )
    db_session.flush()

    item = db_session.scalar(select(ReceivableWorkItem))
    events = db_session.scalars(select(ReceivableWorkEvent)).all()
    assert first is not None
    assert second is not None
    assert second.event["idempotent"] is True
    assert item is not None
    assert item.payload["payment_postponed_count"] == 1
    assert len(events) == 1


def test_last_contact_at_is_separate_from_any_manager_save(db_session: Session) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all([_case(snapshot_date=as_of), _staff_member()])

    response = apply_receivable_workplace_action(
        db_session,
        snapshot_date=as_of,
        counterparty_ref="cp-1",
        payload=ReceivableWorkplaceActionRequest(next_action_date=date(2026, 6, 24)),
    )
    item = db_session.scalar(select(ReceivableWorkItem))
    assert response is not None
    assert item is not None
    assert item.last_manager_update_at is not None
    assert item.last_contact_at is None

    response = apply_receivable_workplace_action(
        db_session,
        snapshot_date=as_of,
        counterparty_ref="cp-1",
        payload=ReceivableWorkplaceActionRequest(status="waiting_payment"),
    )
    db_session.flush()
    assert response is not None
    assert item.last_contact_at is not None


def test_receivable_workplace_summary_uses_full_filtered_set_not_visible_limit(
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(snapshot_date=as_of, counterparty_ref="cp-1", balance=Decimal("1000")),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-2",
                counterparty_name="Клиент 2",
                balance=Decimal("2000"),
                department_ref="dep-2",
                department_name="02. СПБ",
            ),
        ]
    )

    result = build_receivable_workplace(db_session, snapshot_date=as_of, limit=1)

    assert result.total_count == 2
    assert result.visible_count == 1
    assert result.summary.row_count == 2
    assert result.summary.total_receivable == Decimal("3000.00")
    assert {item.department_ref for item in result.department_options} == {"dep-1", "dep-2"}


def test_receivable_workplace_can_sort_by_overdue_days_before_limit(
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-big",
                counterparty_name="Крупный долг",
                balance=Decimal("9000"),
                due_date=datetime(2026, 6, 20, 0, 0),
            ),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-old",
                counterparty_name="Старый долг",
                balance=Decimal("1000"),
                due_date=datetime(2026, 5, 24, 0, 0),
            ),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-mid",
                counterparty_name="Средний долг",
                balance=Decimal("5000"),
                due_date=datetime(2026, 6, 10, 0, 0),
            ),
        ]
    )

    by_balance = build_receivable_workplace(db_session, snapshot_date=as_of, limit=1)
    by_days = build_receivable_workplace(
        db_session,
        snapshot_date=as_of,
        limit=1,
        sort_by="overdue_days",
        sort_dir="desc",
    )
    by_days_asc = build_receivable_workplace(
        db_session,
        snapshot_date=as_of,
        limit=1,
        sort_by="overdue_days",
        sort_dir="asc",
    )

    assert by_balance.payload[0].counterparty_ref == "cp-big"
    assert by_days.payload[0].counterparty_ref == "cp-old"
    assert by_days_asc.payload[0].counterparty_ref == "cp-big"


def test_receivable_workplace_uses_open_debt_cache_for_effective_overdue(
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(snapshot_date=as_of, origin_date=datetime(2026, 1, 1, 12, 0)),
            ReceivableOpenDebtCache(
                snapshot_date=as_of,
                counterparty_ref="cp-1",
                department_ref="dep-1",
                documents=[
                    {
                        "document_ref": "sale-cache",
                        "document_number": "РБГУ7777",
                        "document_date": "2026-06-12T09:00:00",
                        "open_amount": "12500.00",
                    }
                ],
            ),
        ]
    )

    result = build_receivable_workplace(db_session, snapshot_date=as_of)

    assert result.source_status == "cache_ready"
    assert result.cache_status["open_debt"].source_status == "cache_ready"
    assert result.payload[0].documents[0].document_number == "РБГУ7777"
    assert result.payload[0].effective_due_date == datetime(2026, 6, 19, 9, 0)
    assert result.payload[0].effective_overdue_days == 4


def test_open_debt_source_freshness_marks_old_ledger_as_stale(
    db_session: Session,
) -> None:
    db_session.add(_ledger_event(document_date=datetime(2026, 4, 21, 0, 50)))
    db_session.flush()

    freshness = evaluate_open_debt_source_freshness(
        db_session,
        snapshot_date=date(2026, 7, 13),
    )

    assert freshness.source_status == "source_stale"
    assert freshness.source_max_document_date == datetime(2026, 4, 21, 0, 50)
    assert freshness.source_lag_days == 83


def test_open_debt_source_freshness_accepts_recent_ledger(
    db_session: Session,
) -> None:
    db_session.add(_ledger_event(document_date=datetime(2026, 7, 12, 18, 0)))
    db_session.flush()

    freshness = evaluate_open_debt_source_freshness(
        db_session,
        snapshot_date=date(2026, 7, 13),
    )

    assert freshness.source_status == "cache_ready"
    assert freshness.source_lag_days == 1


def test_rebuild_open_debt_cache_reports_diagnostics_and_removes_extra_rows(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    as_of = date(2026, 7, 13)
    snapshots = [
        ReceivableBalanceSnapshot(
            snapshot_date=as_of,
            counterparty_ref=counterparty_ref,
            counterparty_name=counterparty_ref,
            current_balance=balance,
            department_ref="dep-1",
            aged_bucket="1-30",
            activity_segment="active",
            is_overdue=True,
        )
        for counterparty_ref, balance in (
            ("cp-match", Decimal("100.00")),
            ("cp-missing", Decimal("200.00")),
            ("cp-non-buyer", Decimal("300.00")),
        )
    ]
    db_session.add_all(
        [
            *snapshots,
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-match",
                balance=Decimal("100.00"),
            ),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-missing",
                balance=Decimal("200.00"),
            ),
            _ledger_event(
                document_date=datetime(2026, 7, 12, 18, 0),
                business_key="fresh-source",
            ),
            ReceivableOpenDebtCache(
                snapshot_date=as_of,
                counterparty_ref="cp-extra",
                department_ref="dep-1",
                source_status="ready",
                documents=[{"open_amount": "999.00"}],
            ),
            ReceivableOpenDebtCache(
                snapshot_date=as_of,
                counterparty_ref="cp-non-buyer",
                department_ref="dep-1",
                source_status="ready",
                documents=[{"open_amount": "300.00"}],
            ),
        ]
    )
    db_session.flush()

    def fake_build(*args, diagnostics=None, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        diagnostics["statement_sale_counts"] = {"cp-match": 1, "cp-missing": 0}
        return {
            "cp-match": [
                {
                    "document_ref": "sale-match",
                    "document_number": "РТУ-100",
                    "document_date": datetime(2026, 7, 10, 9, 0),
                    "document_structure_status": "confirmed_open",
                    "open_amount": Decimal("100.00"),
                }
            ]
        }

    monkeypatch.setattr(
        receivable_workplace_cache,
        "build_open_debt_documents_by_counterparty",
        fake_build,
    )

    result = receivable_workplace_cache.rebuild_open_debt_cache(
        db_session,
        snapshot_date=as_of,
    )
    db_session.flush()

    rows = db_session.scalars(
        select(ReceivableOpenDebtCache).where(ReceivableOpenDebtCache.snapshot_date == as_of)
    ).all()
    rows_by_ref = {row.counterparty_ref: row for row in rows}
    assert result["document_diagnostic_counts"] == {
        "matched": 1,
        "statement_missing": 1,
    }
    assert result["document_mismatch_count"] == 1
    assert result["revealed_document_mismatch_count"] == 0
    assert result["source_snapshot_count"] == 2
    assert result["deleted_count"] == 2
    assert result["extra_cache_rows"] == 0
    assert set(rows_by_ref) == {"cp-match", "cp-missing"}
    assert rows_by_ref["cp-match"].source_status == "ready"
    assert rows_by_ref["cp-match"].documents[0]["open_amount"] == "100.00"
    assert rows_by_ref["cp-missing"].source_status == "document_mismatch"
    assert rows_by_ref["cp-missing"].documents == []


def test_receivable_workplace_hides_documents_from_stale_cache(
    db_session: Session,
) -> None:
    as_of = date(2026, 7, 13)
    db_session.add_all(
        [
            _case(
                snapshot_date=as_of,
                due_date=datetime(2026, 7, 1, 12, 0),
                overdue_days=12,
            ),
            ReceivableOpenDebtCache(
                snapshot_date=as_of,
                counterparty_ref="cp-1",
                department_ref="dep-1",
                source_status="source_stale",
                documents=[
                    {
                        "document_ref": "sale-old",
                        "document_number": "РТУ-АПРЕЛЬ",
                        "document_date": "2026-04-21T10:00:00",
                        "open_amount": "1000.00",
                    }
                ],
            ),
        ]
    )

    result = build_receivable_workplace(db_session, snapshot_date=as_of)

    assert result.source_status == "source_stale"
    assert result.cache_status["open_debt"].source_status == "source_stale"
    assert result.payload[0].documents == []


def test_receivable_workplace_keeps_debt_visible_for_document_amount_mismatch(
    db_session: Session,
) -> None:
    as_of = date(2026, 7, 13)
    db_session.add_all(
        [
            _case(
                snapshot_date=as_of,
                due_date=datetime(2026, 7, 1, 12, 0),
                overdue_days=12,
            ),
            ReceivableOpenDebtCache(
                snapshot_date=as_of,
                counterparty_ref="cp-1",
                department_ref="dep-1",
                source_status="document_mismatch",
                documents=[],
            ),
        ]
    )

    result = build_receivable_workplace(db_session, snapshot_date=as_of)

    assert result.source_status == "cache_ready"
    assert result.payload[0].current_balance == Decimal("12500.00")
    assert result.payload[0].documents == []


def test_receivable_workplace_current_balance_uses_cached_open_debt_documents(
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(
                snapshot_date=as_of,
                balance=Decimal("1000"),
                origin_date=datetime(2026, 1, 1, 12, 0),
            ),
            ReceivableOpenDebtCache(
                snapshot_date=as_of,
                counterparty_ref="cp-1",
                department_ref="dep-1",
                documents=[
                    {
                        "document_ref": "sale-open-1",
                        "document_number": "РТУ-1",
                        "document_date": "2026-06-10T09:00:00",
                        "open_amount": "100.00",
                    },
                    {
                        "document_ref": "sale-open-2",
                        "document_number": "РТУ-2",
                        "document_date": "2026-06-12T09:00:00",
                        "open_amount": "50.00",
                    },
                ],
            ),
        ]
    )

    result = build_receivable_workplace(db_session, snapshot_date=as_of)
    item = result.payload[0]

    assert item.current_balance == Decimal("150.00")
    assert item.overdue_amount == Decimal("150.00")
    assert result.summary.total_receivable == Decimal("150.00")
    assert result.summary.total_overdue == Decimal("150.00")


def test_receivable_workplace_open_debt_documents_override_stale_case_due_date(
    db_session: Session,
) -> None:
    as_of = date(2026, 7, 4)
    stale_case = _case(
        snapshot_date=as_of,
        balance=Decimal("57377"),
        origin_date=datetime(2026, 4, 13, 13, 4, 4),
        credit_depth_days=14,
    )
    stale_case.due_date = datetime(2026, 4, 27, 13, 4, 4)
    stale_case.overdue_days = 68
    db_session.add_all(
        [
            stale_case,
            ReceivableOpenDebtCache(
                snapshot_date=as_of,
                counterparty_ref="cp-1",
                department_ref="dep-1",
                documents=[
                    {
                        "document_ref": "sale-open-fresh",
                        "document_number": "РТУ-СВЕЖАЯ",
                        "document_date": "2026-06-25T15:33:20",
                        "open_amount": "57377.00",
                    }
                ],
            ),
        ]
    )

    result = build_receivable_workplace(db_session, snapshot_date=as_of)

    assert result.total_count == 0
    assert result.payload == []


def test_receivable_workplace_empty_open_debt_cache_does_not_show_old_chain(
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(
                snapshot_date=as_of,
                balance=Decimal("1000"),
                origin_date=datetime(2026, 1, 1, 12, 0),
            ),
            ReceivableOpenDebtCache(
                snapshot_date=as_of,
                counterparty_ref="cp-1",
                department_ref="dep-1",
                documents=[],
            ),
        ]
    )

    result = build_receivable_workplace(db_session, snapshot_date=as_of)

    assert result.total_count == 0
    assert result.payload == []


def test_receivable_workplace_planned_payment_date_overrides_credit_depth(
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    planned_payment_date = datetime(2026, 6, 20)
    db_session.add_all(
        [
            _case(
                snapshot_date=as_of,
                due_date=planned_payment_date,
                credit_depth_days=30,
                origin_date=datetime(2026, 6, 1, 12, 0),
            ),
            ReceivableOpenDebtCache(
                snapshot_date=as_of,
                counterparty_ref="cp-1",
                department_ref="dep-1",
                documents=[
                    {
                        "document_ref": "sale-cache",
                        "document_number": "РТУ-ПЛАН",
                        "document_date": "2026-06-01T09:00:00",
                        "open_amount": "12500.00",
                    }
                ],
            ),
        ]
    )

    result = build_receivable_workplace(db_session, snapshot_date=as_of)
    item = result.payload[0]

    assert item.effective_due_date == planned_payment_date
    assert item.effective_overdue_days == 3
    assert item.documents[0].due_date == planned_payment_date
    assert item.documents[0].overdue_days == 3


def test_receivable_workplace_uses_counterparty_code_from_case_and_folder_cache(
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(snapshot_date=as_of, counterparty_code="РБ039414"),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-2",
                counterparty_name="Клиент 2",
            ),
            ReceivableFolderRecommendationCache(
                snapshot_date=as_of,
                status_scope="all",
                report_revision="cached-codes",
                summary={"source_snapshot_count": 2},
                payload=[
                    {
                        "counterparty_ref": "cp-2",
                        "counterparty_code": "РБ000222",
                    }
                ],
                source_status="cached",
            ),
        ]
    )

    result = build_receivable_workplace(db_session, snapshot_date=as_of)
    codes = {item.counterparty_ref: item.counterparty_code for item in result.payload}

    assert codes["cp-1"] == "РБ039414"
    assert codes["cp-2"] == "РБ000222"


def test_receivable_workplace_excludes_couriers_from_staff_options(
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(snapshot_date=as_of),
            _staff_member(),
            _staff_member(
                external_ref="courier-1",
                full_name="Курьер 1",
                role_code="courier",
                role_name="Курьер",
            ),
        ]
    )

    result = build_receivable_workplace(db_session, snapshot_date=as_of)

    assert [staff.staff_ref for staff in result.payload[0].staff_options] == ["staff-1"]


def test_receivable_workplace_staff_options_use_teply_stan_equivalent_refs_and_roles(
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 30)
    db_session.add_all(
        [
            _case(
                snapshot_date=as_of,
                department_ref=TEPLY_STAN_RECEIVABLES_REF,
                department_name="04.Теплый Стан",
                current_manager_ref="manager-1",
                current_manager_name="Менеджер Теплый Стан",
            ),
            _staff_member(
                external_ref="manager-1",
                full_name="Менеджер Теплый Стан",
                role_code="manager",
                role_name="Менеджер",
                department_ref=TEPLY_STAN_STAFF_DEPARTMENT_REF,
                department_name="Радиорынок «Электромир»",
            ),
            _staff_member(
                external_ref="merch-1",
                full_name="Товаровед Теплый Стан",
                role_code="merchandiser",
                role_name="Товаровед",
                department_ref=None,
                department_name=None,
                store_ref=TEPLY_STAN_TELEPHONY_STORE_REF,
                store_name='Теплый стан Радиорынок "Электромир" пав. 652',
            ),
            _staff_member(
                external_ref="head-1",
                full_name="Руководитель Теплый Стан",
                role_code="head",
                role_name="Руководитель подразделения",
                department_ref=TEPLY_STAN_STAFF_DEPARTMENT_REF,
                department_name="Радиорынок «Электромир»",
            ),
            _staff_member(
                external_ref="courier-1",
                full_name="Курьер Теплый Стан",
                role_code="courier",
                role_name="Курьер",
                store_ref=TEPLY_STAN_TELEPHONY_STORE_REF,
                store_name='Теплый стан Радиорынок "Электромир" пав. 652',
            ),
            _staff_member(
                external_ref="admin-1",
                full_name="Администратор Теплый Стан",
                role_code="admin",
                role_name="Администратор",
                department_ref=TEPLY_STAN_STAFF_DEPARTMENT_REF,
                department_name="Радиорынок «Электромир»",
            ),
        ]
    )

    result = build_receivable_workplace(db_session, snapshot_date=as_of)

    assert {staff.staff_ref for staff in result.payload[0].staff_options} == {
        "manager-1",
        "head-1",
        "merch-1",
    }


def test_receivable_workplace_staff_options_use_store_aliases_from_staff_source(
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-savelovskiy",
                department_ref="dep-savelovskiy",
                department_name="02. Савеловский",
                current_manager_ref="none",
            ),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-grand",
                department_ref="dep-grand",
                department_name="06. Гранд Юг",
                current_manager_ref="none",
            ),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-presnya",
                department_ref="dep-presnya",
                department_name="07. Электроника на пресне",
                current_manager_ref="none",
            ),
            _staff_member(
                external_ref="staff-savelovskiy",
                full_name="Менеджер Савеловский",
                department_ref="staff-savelovskiy-dep",
                department_name="ТК «Савеловский» Мобильный",
            ),
            _staff_member(
                external_ref="staff-grand",
                full_name="Менеджер Гранд Юг",
                department_ref="staff-grand-dep",
                department_name="ТЦ Гранд Юг «Электронный рай»",
            ),
            _staff_member(
                external_ref="staff-presnya",
                full_name="Менеджер Пресня",
                department_ref="staff-presnya-dep",
                department_name="ТЦ «Электроника на Пресне»",
            ),
        ]
    )

    result = build_receivable_workplace(db_session, snapshot_date=as_of)
    by_ref = {item.counterparty_ref: item for item in result.payload}

    assert [staff.staff_ref for staff in by_ref["cp-savelovskiy"].staff_options] == [
        "staff-savelovskiy"
    ]
    assert [staff.staff_ref for staff in by_ref["cp-grand"].staff_options] == ["staff-grand"]
    presnya_options = by_ref["cp-presnya"].staff_options
    assert "staff-presnya" in {staff.staff_ref for staff in presnya_options}
    assert "Храброва Маргарита" in {staff.staff_name for staff in presnya_options}


def test_receivable_workplace_staff_options_apply_manual_staff_safeguards(
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(snapshot_date=as_of),
            _staff_member(),
            _staff_member(
                external_ref="fired-zelensky",
                full_name="Зеленский Андрей Владимирович",
            ),
            _staff_member(
                external_ref="fired-brylev",
                full_name="Брылев Дмитрий",
            ),
            _staff_member(
                external_ref="courier-mekan",
                full_name="Мекан Аннаев",
                role_code=None,
                role_name=None,
            ),
            _staff_member(
                external_ref="courier-davydenkova",
                full_name="Давыденкова Мария Ивановна",
            ),
            _staff_member(
                external_ref="courier-bulatova",
                full_name="Булатова Елена",
            ),
            _staff_member(
                external_ref="courier-sunagatullin",
                full_name="Сунагатуллин Алексей",
            ),
            _staff_member(
                external_ref="courier-suagatullin",
                full_name="Суагатуллин Алексей",
            ),
            _staff_member(
                external_ref="courier-kabaev",
                full_name="Кабаев Артем",
            ),
            _staff_member(
                external_ref="fired-krayukhin",
                full_name="Краюхин Сергей",
            ),
            _staff_member(
                external_ref="fired-sadykov",
                full_name="Садыков Ильдар",
            ),
        ]
    )

    result = build_receivable_workplace(db_session, snapshot_date=as_of)

    assert [staff.staff_ref for staff in result.payload[0].staff_options] == ["staff-1"]


def test_receivable_workplace_staff_options_add_configured_fallback_staff(
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-mitino",
                department_ref="dep-mitino",
                department_name="03. Митино",
                current_manager_ref="none",
            ),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-pyatigorsk",
                department_ref="dep-pyatigorsk",
                department_name="05. Пятигорск",
                current_manager_ref="none",
            ),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-presnya",
                department_ref="dep-presnya",
                department_name="07. Электроника на пресне",
                current_manager_ref="none",
            ),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-site",
                department_ref="dep-site",
                department_name="08. Сайт",
                current_manager_ref="none",
            ),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-prosveshcheniya",
                department_ref="dep-prosveshcheniya",
                department_name="10. СПБ Просвещения",
                current_manager_ref="none",
            ),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-wholesale",
                department_ref="dep-wholesale",
                department_name="11. Оптовый отдел",
                current_manager_ref="none",
            ),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-moskovskaya",
                department_ref="dep-moskovskaya",
                department_name="13. СПБ Московская",
                current_manager_ref="none",
            ),
        ]
    )

    result = build_receivable_workplace(db_session, snapshot_date=as_of)
    by_ref = {item.counterparty_ref: item for item in result.payload}

    assert {staff.staff_name for staff in by_ref["cp-mitino"].staff_options} == {"Булгаков Артем"}
    assert {staff.staff_name for staff in by_ref["cp-pyatigorsk"].staff_options} == {
        "Руднев Александр",
        "Шевцов Вячеслав Николаевич",
        "Кочиян Михаил Альбертович",
        "Золотарев Илья",
        "Малеева Полина Михайловна",
        "Богатырев Михаил",
        "Хачян Елена Камоевна",
        "Кургаев Олег",
        "Пигунов Глеб Аркадьевич",
    }
    assert {staff.staff_name for staff in by_ref["cp-presnya"].staff_options} == {
        "Храброва Маргарита"
    }
    assert {staff.staff_name for staff in by_ref["cp-site"].staff_options} == {"Гиря Анна"}
    assert {staff.staff_name for staff in by_ref["cp-prosveshcheniya"].staff_options} == {
        "Бухман Владислав"
    }
    assert {staff.staff_name for staff in by_ref["cp-wholesale"].staff_options} == {
        "Карданов Рамазан"
    }
    assert {staff.staff_name for staff in by_ref["cp-moskovskaya"].staff_options} == {
        "Гаджимурадов Камиль"
    }


def test_receivable_workplace_action_accepts_configured_fallback_staff(
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add(
        _case(
            snapshot_date=as_of,
            department_ref="dep-pyatigorsk",
            department_name="05. Пятигорск",
            current_manager_ref="none",
        )
    )
    item = build_receivable_workplace(db_session, snapshot_date=as_of).payload[0]
    staff_ref = next(
        staff.staff_ref for staff in item.staff_options if staff.staff_name == "Руднев Александр"
    )

    response = apply_receivable_workplace_action(
        db_session,
        snapshot_date=as_of,
        counterparty_ref="cp-1",
        payload=ReceivableWorkplaceActionRequest(
            contacted_staff_ref=staff_ref,
            contacted_staff_name=None,
        ),
    )
    event = db_session.scalar(select(ReceivableWorkEvent))

    assert response is not None
    assert response.item.contacted_staff_ref == staff_ref
    assert response.item.contacted_staff_name == "Руднев Александр"
    assert event is not None
    assert event.payload["contacted_staff_name"] == "Руднев Александр"


def test_receivable_workplace_staff_options_move_kopyev_to_sadovaya_only(
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-sadovaya",
                department_ref="dep-sadovaya",
                department_name="09. СПБ Садовая",
                current_manager_ref="none",
            ),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-prosveshcheniya",
                department_ref="dep-prosveshcheniya",
                department_name="10. СПБ Просвещения",
                current_manager_ref="kopyev",
                current_manager_name="Копьев Михаил Андреевич",
            ),
            _staff_member(
                external_ref="kopyev",
                full_name="Копьев Михаил Андреевич",
                role_code=None,
                role_name=None,
                department_ref="staff-prosveshcheniya",
                department_name="Проспект Просвещения",
            ),
        ]
    )

    result = build_receivable_workplace(db_session, snapshot_date=as_of)
    by_ref = {item.counterparty_ref: item for item in result.payload}

    assert [staff.staff_ref for staff in by_ref["cp-sadovaya"].staff_options] == ["kopyev"]
    prosveshcheniya_options = by_ref["cp-prosveshcheniya"].staff_options
    assert {staff.staff_name for staff in prosveshcheniya_options} == {"Бухман Владислав"}
    assert "kopyev" not in {staff.staff_ref for staff in prosveshcheniya_options}


def test_receivable_workplace_manual_last_contact_date_roundtrip(
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all([_case(snapshot_date=as_of), _staff_member()])

    response = apply_receivable_workplace_action(
        db_session,
        snapshot_date=as_of,
        counterparty_ref="cp-1",
        payload=ReceivableWorkplaceActionRequest(last_contact_at=date(2026, 6, 20)),
    )
    db_session.flush()

    item = db_session.scalar(select(ReceivableWorkItem))
    assert response is not None
    assert item is not None
    assert item.last_contact_at == datetime(2026, 6, 20)
    assert response.item.last_contact_at == datetime(2026, 6, 20)


def test_receivable_workplace_cached_documents_are_sorted_and_explained(
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(snapshot_date=as_of, origin_date=datetime(2026, 1, 1, 12, 0)),
            ReceivableOpenDebtCache(
                snapshot_date=as_of,
                counterparty_ref="cp-1",
                department_ref="dep-1",
                documents=[
                    {
                        "document_ref": "sale-new",
                        "document_number": "РТУ-2",
                        "document_date": "2026-06-15T09:00:00",
                        "open_amount": "500.00",
                        "sale_amount": "1000.00",
                        "closing_amount": "-500.00",
                        "return_amount": "0.00",
                        "statement_selection_rule": "statement_structure_confirmed_open",
                        "statement_balance_after": "500.00",
                        "statement_match_details": [{"document_number": "ПКО-1", "amount": "-500"}],
                    },
                    {
                        "document_ref": "sale-old",
                        "document_number": "РТУ-1",
                        "document_date": "2026-06-10T09:00:00",
                        "open_amount": "12000.00",
                        "statement_selection_rule": "statement_bottom_up_balance_cutoff",
                    },
                ],
            ),
        ]
    )

    result = build_receivable_workplace(db_session, snapshot_date=as_of)
    item = result.payload[0]

    assert item.oldest_overdue_date == datetime(2026, 6, 10, 9, 0)
    assert [document.document_number for document in item.documents] == ["РТУ-1", "РТУ-2"]
    assert item.documents[1].selection_rule == "statement_structure_confirmed_open"
    assert item.documents[1].open_amount == Decimal("500.00")
    assert item.documents[1].gross_amount == Decimal("1000.00")
    assert item.documents[1].closing_amount == Decimal("-500.00")
    assert item.documents[1].statement_balance_after == Decimal("500.00")
    assert item.documents[1].match_details[0]["document_number"] == "ПКО-1"


def test_receivable_workplace_patch_uses_cache_without_live_open_debt_recompute(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(snapshot_date=as_of),
            _staff_member(),
            ReceivableOpenDebtCache(
                snapshot_date=as_of,
                counterparty_ref="cp-1",
                department_ref="dep-1",
                documents=[
                    {
                        "document_ref": "sale-cache",
                        "document_number": "РТУ-КЕШ",
                        "document_date": "2026-06-12T09:00:00",
                        "open_amount": "12500.00",
                    }
                ],
            ),
        ]
    )
    monkeypatch.setattr(
        receivable_workplace_service,
        "_load_open_debt_documents",
        lambda *args, **kwargs: pytest.fail("PATCH must not run live open-debt recompute"),
    )

    response = apply_receivable_workplace_action(
        db_session,
        snapshot_date=as_of,
        counterparty_ref="cp-1",
        payload=ReceivableWorkplaceActionRequest(status="waiting_payment"),
    )

    assert response is not None
    assert response.item.documents[0].document_number == "РТУ-КЕШ"
    assert response.cache_status["open_debt"].source_status == "cache_ready"


def test_receivable_workplace_api_requires_token_and_returns_payload(
    monkeypatch,
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(snapshot_date=as_of),
            ReceivableWorkItem(
                stable_key=stable_key_for_counterparty("cp-1"),
                counterparty_ref="cp-1",
                counterparty_name="Клиент 1",
                status="new_debt",
                current_balance=Decimal("12500"),
                bitrix_detail_url="/crm/type/187/details/555/",
            ),
        ]
    )
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
        assert payload["payload"][0]["bitrix_detail_url"] == "/crm/type/187/details/555/"
    finally:
        app.dependency_overrides = {}
        get_settings.cache_clear()


def test_receivable_workplace_api_accepts_sort_params(
    monkeypatch,
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-big",
                counterparty_name="Крупный долг",
                balance=Decimal("9000"),
                due_date=datetime(2026, 6, 20, 0, 0),
            ),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-old",
                counterparty_name="Старый долг",
                balance=Decimal("1000"),
                due_date=datetime(2026, 5, 24, 0, 0),
            ),
        ]
    )
    db_session.commit()

    def override_db():
        yield db_session

    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
    get_settings.cache_clear()
    app.dependency_overrides = {get_db: override_db}
    client = TestClient(app)
    try:
        response = client.get(
            "/api/receivables/workplace",
            params={
                "date": as_of.isoformat(),
                "sort_by": "overdue_days",
                "sort_dir": "desc",
            },
            headers={"Authorization": "Bearer secret-token"},
        )
        assert response.status_code == 200
        assert response.json()["payload"][0]["counterparty_ref"] == "cp-old"
    finally:
        app.dependency_overrides = {}
        get_settings.cache_clear()


def test_receivable_workplace_meta_returns_latest_date_and_departments(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    old_date = date(2026, 6, 22)
    as_of = date(2026, 6, 23)
    db_session.add_all(
        [
            _case(snapshot_date=old_date, counterparty_ref="cp-old"),
            _case(snapshot_date=as_of, counterparty_ref="cp-1"),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-2",
                counterparty_name="Клиент 2",
                department_ref="dep-2",
                department_name="02. СПБ",
            ),
            ReceivableOpenDebtCache(
                snapshot_date=as_of,
                counterparty_ref="cp-1",
                department_ref="dep-1",
                documents=[],
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
            "/api/receivables/workplace/meta",
            headers={"Authorization": "Bearer secret-token"},
        )
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 200
    body = response.json()
    assert body["latest_snapshot_date"] == as_of.isoformat()
    assert {item["department_ref"] for item in body["department_options"]} == {
        "dep-1",
        "dep-2",
    }
    assert body["cache_status"]["open_debt"]["source_status"] == "cache_ready"


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
    assert (
        response.json()["detail"]
        == "Не найдено подразделение для доступа: проверьте привязку пользователя к подразделению"
    )


def test_bitrix_receivables_session_endpoint_uses_bitrix_profile_department_fallback(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    as_of = date(2026, 7, 7)
    settings = _bitrix_settings()
    _override_receivables_settings(monkeypatch, settings)
    db_session.add(
        _case(
            snapshot_date=as_of,
            counterparty_ref="cp-pyatigorsk",
            department_ref="dep-pyatigorsk",
            department_name="05. Пятигорск",
        )
    )

    def fake_urlopen(request, timeout):  # noqa: ANN001, ANN202
        assert timeout == settings.receivable_workplace_bitrix_rest_timeout_seconds
        payload = json.loads(request.data.decode())
        if request.full_url == "https://crm.master-mobile.ru/rest/user.current.json":
            assert payload == {"auth": "bitrix-access-token"}
            return _FakeBitrixResponse(
                result={
                    "ID": "132228",
                    "NAME": "Елена",
                    "LAST_NAME": "Хачян",
                    "ACTIVE": True,
                    "WORK_POSITION": "Менеджер по продажам",
                    "UF_DEPARTMENT": [3273],
                }
            )
        if request.full_url == "https://crm.master-mobile.ru/rest/department.get.json":
            assert payload == {"auth": "bitrix-access-token", "ID": "3273"}
            return _FakeBitrixResponse(result=[{"ID": "3273", "NAME": "Георгиевская"}])
        raise AssertionError(f"unexpected Bitrix request: {request.full_url}")

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
    assert body["access_level"] == "department"
    assert body["department_refs"] == ["dep-pyatigorsk"]


def test_bitrix_receivables_session_endpoint_rejects_bitrix_courier(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    settings = _bitrix_settings()
    _override_receivables_settings(monkeypatch, settings)

    def fake_urlopen(request, timeout):  # noqa: ANN001, ANN202
        assert request.full_url == "https://crm.master-mobile.ru/rest/user.current.json"
        assert timeout == settings.receivable_workplace_bitrix_rest_timeout_seconds
        return _FakeBitrixResponse(
            result={
                "ID": "132229",
                "NAME": "Иван",
                "LAST_NAME": "Иванов",
                "ACTIVE": True,
                "WORK_POSITION": "Курьер",
                "UF_DEPARTMENT": [3273],
            }
        )

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

    assert response.status_code == 403
    assert response.json()["detail"] == "Доступ к рабочему месту дебиторки закрыт для курьеров"


def test_bitrix_receivables_session_endpoint_resolves_department_by_name_fallback(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    settings = _bitrix_settings()
    _override_receivables_settings(monkeypatch, settings)
    snapshot = _telephony_snapshot(
        snapshot_date=as_of,
        bitrix_user_id="77",
        staff_department_ref=None,
        department_ref_hex=None,
    )
    snapshot.staff_department_name = "01. Горбушкин Двор"
    db_session.add_all([_case(snapshot_date=as_of), snapshot])
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

    assert response.status_code == 200
    body = response.json()
    assert body["access_level"] == "department"
    assert body["department_refs"] == ["dep-1"]


def test_bitrix_receivables_session_endpoint_expands_teply_stan_staff_department(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 30)
    settings = _bitrix_settings()
    _override_receivables_settings(monkeypatch, settings)
    db_session.add_all(
        [
            _case(
                snapshot_date=as_of,
                department_ref=TEPLY_STAN_RECEIVABLES_REF,
                department_name="04.Теплый Стан",
            ),
            _telephony_snapshot(
                snapshot_date=as_of,
                bitrix_user_id="77",
                staff_department_ref=TEPLY_STAN_STAFF_DEPARTMENT_REF,
                staff_department_name="Радиорынок «Электромир»",
            ),
        ]
    )
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

    assert response.status_code == 200
    body = response.json()
    assert body["access_level"] == "department"
    assert set(body["department_refs"]) == {
        TEPLY_STAN_RECEIVABLES_REF,
        TEPLY_STAN_STAFF_DEPARTMENT_REF,
        TEPLY_STAN_TELEPHONY_STORE_REF,
    }


def test_bitrix_receivables_session_endpoint_expands_teply_stan_store_ref(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 30)
    settings = _bitrix_settings()
    _override_receivables_settings(monkeypatch, settings)
    db_session.add_all(
        [
            _case(
                snapshot_date=as_of,
                department_ref=TEPLY_STAN_RECEIVABLES_REF,
                department_name="04.Теплый Стан",
            ),
            _telephony_snapshot(
                snapshot_date=as_of,
                bitrix_user_id="77",
                staff_department_ref=None,
                staff_store_ref=TEPLY_STAN_TELEPHONY_STORE_REF,
                staff_store_name='Теплый стан Радиорынок "Электромир" пав. 652',
            ),
        ]
    )
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

    assert response.status_code == 200
    assert TEPLY_STAN_RECEIVABLES_REF in response.json()["department_refs"]


def test_bitrix_receivables_session_endpoint_uses_access_table_before_env(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    settings = _bitrix_settings()
    _override_receivables_settings(monkeypatch, settings)
    db_session.add(
        ReceivableBitrixUserAccess(
            bitrix_user_id="88",
            access_level="full",
            department_refs=[],
            is_active=True,
            comment="test admin",
        )
    )
    monkeypatch.setattr(
        bitrix_receivables_auth.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeBitrixResponse(user_id="88"),
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

    assert response.status_code == 200
    body = response.json()
    assert body["access_level"] == "full"
    assert body["department_refs"] == []


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


def test_bitrix_receivables_department_user_with_teply_stan_staff_ref_sees_receivables_ref(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 30)
    db_session.add_all(
        [
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-teply",
                department_ref=TEPLY_STAN_RECEIVABLES_REF,
                department_name="04.Теплый Стан",
            ),
            _case(
                snapshot_date=as_of,
                counterparty_ref="cp-other",
                counterparty_name="Клиент другой",
                department_ref="dep-other",
                department_name="02. СПБ",
            ),
        ]
    )
    db_session.commit()
    settings = _bitrix_settings()
    _override_receivables_settings(monkeypatch, settings)
    token = _bitrix_token(
        settings,
        user_id="77",
        access=ReceivablesAccess(
            access_level="department",
            department_refs=frozenset({TEPLY_STAN_STAFF_DEPARTMENT_REF}),
        ),
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
    assert [item["counterparty_ref"] for item in body["payload"]] == ["cp-teply"]


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

    captured_kwargs = {}
    db_session.add(_case(snapshot_date=as_of, counterparty_ref="cp-1"))
    db_session.flush()

    def fake_build_counterparty_folder_recommendations(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return {
            "snapshot_date": as_of,
            "report_revision": "test",
            "summary": {"source_snapshot_count": 2, "total_count": 2},
            "payload": [
                {
                    "snapshot_date": as_of,
                    "counterparty_ref": "cp-1",
                    "counterparty_name": "Клиент 1",
                    "current_balance": Decimal("1000"),
                    "snapshot_department_ref": "dep-1",
                    "debt_department_ref": None,
                    "is_overdue": True,
                    "status": "move_recommended",
                },
                {
                    "snapshot_date": as_of,
                    "counterparty_ref": "cp-2",
                    "counterparty_name": "Клиент 2",
                    "current_balance": Decimal("2000"),
                    "snapshot_department_ref": "dep-2",
                    "debt_department_ref": "dep-2",
                    "is_overdue": True,
                    "status": "move_recommended",
                },
            ],
        }

    monkeypatch.setattr(receivable_workplace_api, "_build_onec_engine", lambda: FakeEngine())
    monkeypatch.setattr(
        receivable_workplace_api,
        "build_counterparty_folder_recommendations",
        fake_build_counterparty_folder_recommendations,
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
    assert captured_kwargs["snapshot_department_refs"] == frozenset({"dep-1"})
    assert captured_kwargs["counterparty_refs"] == frozenset({"cp-1"})


def test_receivables_folder_recommendations_use_cache_without_onec(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    as_of = date(2026, 6, 23)
    settings = _bitrix_settings()
    _override_receivables_settings(monkeypatch, settings)
    db_session.add_all(
        [
            _case(snapshot_date=as_of, counterparty_ref="cp-1"),
            ReceivableFolderRecommendationCache(
                snapshot_date=as_of,
                status_scope="all",
                report_revision="cached-1",
                summary={"source_snapshot_count": 2, "total_count": 2},
                payload=[
                    {
                        "snapshot_date": as_of.isoformat(),
                        "counterparty_ref": "cp-1",
                        "counterparty_name": "Клиент 1",
                        "current_balance": "1000.00",
                        "snapshot_department_ref": "dep-1",
                        "debt_department_ref": "dep-1",
                        "is_overdue": True,
                        "status": "needs_review",
                    },
                    {
                        "snapshot_date": as_of.isoformat(),
                        "counterparty_ref": "cp-non-buyer",
                        "counterparty_name": "Поставщик",
                        "current_balance": "5000.00",
                        "snapshot_department_ref": "dep-1",
                        "debt_department_ref": "dep-1",
                        "is_overdue": True,
                        "status": "needs_review",
                    },
                ],
                source_status="cached",
            ),
        ]
    )
    db_session.commit()
    token = _bitrix_token(
        settings,
        user_id="77",
        access=ReceivablesAccess(access_level="department", department_refs=frozenset({"dep-1"})),
    )
    monkeypatch.setattr(
        receivable_workplace_api,
        "_build_onec_engine",
        lambda: pytest.fail("folder endpoint should use cache before 1C"),
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
    assert body["source_status"] == "cache_ready"
    assert body["report_revision"] == "cached-1"
    assert [item["counterparty_ref"] for item in body["payload"]] == ["cp-1"]
    assert body["summary"]["source_snapshot_count"] == 1
