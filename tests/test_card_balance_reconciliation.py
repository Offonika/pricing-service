from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, get_engine
from app.core.config import get_settings
from app.main import app
from app.models import (
    Base,
    CardBalanceCashbox,
    CardBalanceReconciliation,
    CardBalanceReconciliationEvent,
    StoreShiftFact,
)
from app.services import card_balance_bitrix
from app.services import card_balance_reconciliation as reconciliation_service
from app.services.card_balance_onec import (
    calculate_closing_balance,
    normalize_cashbox_registry_rows,
    parse_cashbox_name,
)
from app.workers import card_balance_reconciliation as reconciliation_worker


def _setup_db():
    fd, path = tempfile.mkstemp(prefix="card_balance_", suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine, path


def _override_db(engine):
    def _override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    return _override


def _configure_env(monkeypatch, token: str = "card-token") -> dict[str, str]:
    monkeypatch.setenv("CARD_BALANCE_RECONCILIATION_INTERNAL_API_TOKEN", token)
    monkeypatch.setenv("CARD_BALANCE_BITRIX_WEBHOOK_URL", "https://bitrix.example/rest/1/token")
    monkeypatch.setenv("CARD_BALANCE_BITRIX_ENTITY_TYPE_ID", "188")
    monkeypatch.setenv("CARD_BALANCE_BITRIX_CATEGORY_ID", "1")
    monkeypatch.setenv(
        "CARD_BALANCE_BITRIX_STAGE_MAP",
        json.dumps(
            {
                "waiting_screenshot": "DT188_1:NEW",
                "recognition": "DT188_1:RECOGNITION",
                "matched": "DT188_1:MATCHED",
                "mismatch": "DT188_1:MISMATCH",
                "manual_review": "DT188_1:MANUAL",
                "closed_fincontrol": "DT188_1:CLOSED",
                "overdue": "DT188_1:OVERDUE",
                "cancelled": "DT188_1:CANCELLED",
            }
        ),
    )
    monkeypatch.setenv(
        "CARD_BALANCE_BITRIX_FIELD_MAP",
        json.dumps(
            {
                "business_date": "UF_CRM_CARD_BALANCE_BUSINESS_DATE",
                "employee_user": "UF_CRM_CARD_BALANCE_EMPLOYEE_USER",
                "employee_id": "UF_CRM_CARD_BALANCE_EMPLOYEE_ID",
                "employee_name": "UF_CRM_CARD_BALANCE_EMPLOYEE_NAME",
                "employee_last_name": "UF_CRM_CARD_BALANCE_EMPLOYEE_LAST_NAME",
                "card_last4": "UF_CRM_CARD_BALANCE_CARD_LAST4",
                "onec_cashbox_code": "UF_CRM_CARD_BALANCE_ONEC_CASHBOX_CODE",
                "onec_cashbox_name": "UF_CRM_CARD_BALANCE_ONEC_CASHBOX_NAME",
                "screenshot_file": "UF_CRM_CARD_BALANCE_SCREENSHOT_FILE",
                "manual_balance": "UF_CRM_CARD_BALANCE_MANUAL_BALANCE",
                "recognized_balance": "UF_CRM_CARD_BALANCE_RECOGNIZED_BALANCE",
                "recognition_confidence": "UF_CRM_CARD_BALANCE_RECOGNITION_CONFIDENCE",
                "onec_balance": "UF_CRM_CARD_BALANCE_ONEC_BALANCE",
                "diff_amount": "UF_CRM_CARD_BALANCE_DIFF_AMOUNT",
                "status": "UF_CRM_CARD_BALANCE_STATUS",
                "resolution_comment": "UF_CRM_CARD_BALANCE_RESOLUTION_COMMENT",
                "due_at": "UF_CRM_CARD_BALANCE_DUE_AT",
            }
        ),
    )
    monkeypatch.setenv("CARD_BALANCE_TOLERANCE_RUB", "0")
    monkeypatch.setenv("CARD_BALANCE_MAX_STALE_DAYS", "1")
    monkeypatch.setenv("CARD_BALANCE_PILOT_CASHBOX_CODES", "")
    monkeypatch.setenv("CARD_BALANCE_REQUIRE_WORKDAY", "false")
    monkeypatch.setenv("CARD_BALANCE_OCR_REQUIRED", "false")
    get_settings.cache_clear()
    get_engine.cache_clear()
    return {"Authorization": f"Bearer {token}"}


def test_parse_cashbox_name_and_registry_flags_duplicates() -> None:
    parsed = parse_cashbox_name("1223 Горбушкин Двор карта Тюрнин")
    assert parsed.card_last4 == "1223"
    assert parsed.store_name == "Горбушкин Двор"
    assert parsed.employee_last_name == "Тюрнин"
    assert parsed.needs_manual_review is False

    no_store = parse_cashbox_name("6426 карта Куценко Дмитрий")
    assert no_store.card_last4 == "6426"
    assert no_store.store_name is None
    assert no_store.employee_last_name == "Куценко Дмитрий"
    assert no_store.needs_manual_review is False

    rows = normalize_cashbox_registry_rows(
        [
            {
                "onec_cashbox_code": "РБ0000107",
                "onec_cashbox_name": "1223 Горбушкин Двор карта Тюрнин",
            },
            {"onec_cashbox_code": "РБ0000199", "onec_cashbox_name": "1223 Щелковская карта Иванов"},
            {
                "onec_cashbox_code": "РБ0000201",
                "onec_cashbox_name": "1223 Павелецкая карта Тюрнин",
            },
            {"onec_cashbox_code": "РБ0000200", "onec_cashbox_name": "Без номера карта Петров"},
        ]
    )
    assert rows[0]["review_reason"] == "duplicate_last4_employee"
    assert rows[1]["review_reason"] is None
    assert rows[2]["review_reason"] == "duplicate_last4_employee"
    assert rows[3]["review_reason"] == "missing_leading_last4"


def test_upsert_cashboxes_keeps_existing_bitrix_employee_id() -> None:
    engine, path = _setup_db()
    try:
        with Session(engine) as session:
            session.add(
                CardBalanceCashbox(
                    onec_cashbox_code="РБ0000107",
                    onec_cashbox_name="1223 Горбушкин Двор карта Тюрнин",
                    employee_id="130768",
                )
            )
            session.commit()

            reconciliation_service.upsert_cashboxes(
                session,
                [
                    {
                        "onec_cashbox_code": "РБ0000107",
                        "onec_cashbox_name": "1223 Горбушкин Двор карта Тюрнин",
                        "card_last4": "1223",
                        "employee_last_name": "Тюрнин",
                    }
                ],
            )

            cashbox = session.scalar(
                select(CardBalanceCashbox).where(
                    CardBalanceCashbox.onec_cashbox_code == "РБ0000107"
                )
            )
            assert cashbox is not None
            assert cashbox.employee_id == "130768"
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_calculate_onec_statement_balances_from_screenshot_examples() -> None:
    assert calculate_closing_balance(
        opening_balance=1050, inflow_amount=692640, outflow_amount=693690
    ) == Decimal("0.00")
    assert calculate_closing_balance(
        opening_balance=0, inflow_amount=880240, outflow_amount=790850
    ) == Decimal("89390.00")
    assert calculate_closing_balance(
        opening_balance=0, inflow_amount=402530, outflow_amount=402530
    ) == Decimal("0.00")


def test_reconciliation_statuses(monkeypatch) -> None:
    _configure_env(monkeypatch)
    settings = get_settings()
    today = date(2026, 4, 23)
    assert (
        reconciliation_service.resolve_status(
            screenshot_file_id="file-1",
            business_date=today,
            balance_value=Decimal("100"),
            onec_balance=Decimal("100"),
            diff_amount=Decimal("0"),
            mapping_error=None,
            settings=settings,
            today=today,
        )
        == "matched"
    )
    assert (
        reconciliation_service.resolve_status(
            screenshot_file_id="file-1",
            business_date=today,
            balance_value=Decimal("120"),
            onec_balance=Decimal("100"),
            diff_amount=Decimal("20"),
            mapping_error=None,
            settings=settings,
            today=today,
        )
        == "mismatch"
    )
    assert (
        reconciliation_service.resolve_status(
            screenshot_file_id="",
            business_date=today,
            balance_value=Decimal("100"),
            onec_balance=Decimal("100"),
            diff_amount=Decimal("0"),
            mapping_error=None,
            settings=settings,
            today=today,
        )
        == "missing_screenshot"
    )
    assert (
        reconciliation_service.resolve_status(
            screenshot_file_id="file-1",
            business_date=date(2026, 4, 20),
            balance_value=None,
            onec_balance=Decimal("100"),
            diff_amount=None,
            mapping_error=None,
            settings=settings,
            today=today,
        )
        == "low_confidence"
    )
    assert (
        reconciliation_service.resolve_status(
            screenshot_file_id="file-1",
            business_date=date(2026, 4, 20),
            balance_value=Decimal("100"),
            onec_balance=Decimal("100"),
            diff_amount=Decimal("0"),
            mapping_error=None,
            settings=settings,
            today=today,
        )
        == "stale_screenshot"
    )
    assert (
        reconciliation_service.resolve_status(
            screenshot_file_id="file-1",
            business_date=today,
            balance_value=None,
            onec_balance=Decimal("100"),
            diff_amount=None,
            mapping_error=None,
            settings=settings,
            today=today,
        )
        == "low_confidence"
    )
    assert (
        reconciliation_service.resolve_status(
            screenshot_file_id="file-1",
            business_date=today,
            balance_value=Decimal("100"),
            onec_balance=None,
            diff_amount=None,
            mapping_error=None,
            settings=settings,
            today=today,
        )
        == "missing_onec_balance"
    )
    assert (
        reconciliation_service.resolve_status(
            screenshot_file_id="file-1",
            business_date=today,
            balance_value=Decimal("100"),
            onec_balance=Decimal("100"),
            diff_amount=Decimal("0"),
            mapping_error="unmapped_card",
            settings=settings,
            today=today,
        )
        == "unmapped_card"
    )


def test_bitrix_closed_stage_stays_closed_fincontrol(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_env(monkeypatch)
    try:
        with Session(engine) as session:
            session.add(
                CardBalanceCashbox(
                    onec_cashbox_code="РБ0000107",
                    onec_cashbox_name="1223 Горбушкин Двор карта Тюрнин",
                    card_last4="1223",
                    employee_last_name="Тюрнин",
                )
            )
            session.commit()

            row = reconciliation_service.upsert_reconciliation_from_payload(
                session,
                payload={
                    "external_id": "bitrix:1",
                    "business_date": "2026-04-23",
                    "bitrix_item_id": "1",
                    "bitrix_stage_id": "DT188_1:CLOSED",
                    "onec_cashbox_code": "РБ0000107",
                    "screenshot_file_id": "file-1",
                    "manual_balance": "120",
                },
                onec_balance=Decimal("100"),
            )

            assert row.status == "closed_fincontrol"
            assert row.resolved_at is not None
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_bitrix_decode_and_update_payload(monkeypatch) -> None:
    _configure_env(monkeypatch)
    settings = get_settings()
    item = {
        "id": 555,
        "stageId": "DT188_1:RECEIVED",
        "ufCrmCardBalanceBusinessDate": "2026-04-22",
        "ufCrmCardBalanceEmployeeUser": 10837,
        "ufCrmCardBalanceEmployeeName": "Арсен Тюрнин",
        "ufCrmCardBalanceCardLast4": "1223",
        "ufCrmCardBalanceOnecCashboxCode": "РБ0000107",
        "ufCrmCardBalanceScreenshotFile": [{"id": "file-1"}],
        "ufCrmCardBalanceManualBalance": "0",
    }
    payload = card_balance_bitrix.decode_bitrix_item(item, settings=settings)
    assert payload["external_id"] == "bitrix:555"
    assert payload["employee_id"] == "10837"
    assert payload["employee_last_name"] == "Тюрнин"
    assert payload["screenshot_file_id"] == "file-1"
    assert payload["manual_balance"] == Decimal("0")

    row = CardBalanceReconciliation(
        external_id="bitrix:555",
        business_date=date(2026, 4, 22),
        status="matched",
        bitrix_item_id="555",
        employee_id="10837",
        recognized_balance=Decimal("0"),
        onec_balance=Decimal("0"),
        diff_amount=Decimal("0"),
        onec_cashbox_code="РБ0000107",
    )
    fields = card_balance_bitrix.build_bitrix_update_fields(row, settings=settings)
    assert fields["stageId"] == "DT188_1:MATCHED"
    assert fields["ufCrmCardBalanceStatus"] == "matched"
    assert fields["ufCrmCardBalanceDiffAmount"] == "0"
    assert fields["ufCrmCardBalanceEmployeeUser"] == 10837
    assert fields["ufCrmCardBalanceEmployeeId"] == "10837"
    assert fields["ASSIGNED_BY_ID"] == 10837


def test_build_bitrix_update_fields_falls_back_to_webhook_assignee(monkeypatch) -> None:
    _configure_env(monkeypatch)
    settings = get_settings()
    row = CardBalanceReconciliation(
        external_id="bitrix:777",
        business_date=date(2026, 4, 22),
        status="missing_screenshot",
        bitrix_item_id="777",
        employee_id=None,
    )
    fields = card_balance_bitrix.build_bitrix_update_fields(row, settings=settings)
    assert fields["ASSIGNED_BY_ID"] == 1


def test_build_bitrix_update_fields_uses_recognition_stage_for_uploaded_screenshot(
    monkeypatch,
) -> None:
    _configure_env(monkeypatch)
    monkeypatch.setenv(
        "CARD_BALANCE_BITRIX_STAGE_MAP",
        json.dumps(
            {
                "waiting_screenshot": "DT188_1:NEW",
                "recognition": "DT188_1:RECOGNITION",
                "matched": "DT188_1:MATCHED",
                "mismatch": "DT188_1:MISMATCH",
                "manual_review": "DT188_1:MANUAL",
                "closed_fincontrol": "DT188_1:CLOSED",
                "overdue": "DT188_1:OVERDUE",
                "cancelled": "DT188_1:CANCELLED",
            }
        ),
    )
    get_settings.cache_clear()
    get_engine.cache_clear()
    settings = get_settings()
    row = CardBalanceReconciliation(
        external_id="bitrix:778",
        business_date=date(2026, 4, 28),
        status="low_confidence",
        bitrix_item_id="778",
        screenshot_file_id="file-1",
        employee_id="10837",
    )
    fields = card_balance_bitrix.build_bitrix_update_fields(row, settings=settings)
    assert fields["stageId"] == "DT188_1:RECOGNITION"


def test_bitrix_update_uses_crm_item_update(monkeypatch) -> None:
    _configure_env(monkeypatch)
    calls: list[tuple[str, dict[str, list[str]]]] = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"result":{"item":{"id":555}}}'

    def fake_urlopen(request, timeout=60):
        method = request.full_url.rsplit("/", 1)[-1].replace(".json", "")
        calls.append((method, urllib.parse.parse_qs((request.data or b"").decode("utf-8"))))
        return _Response()

    monkeypatch.setattr(card_balance_bitrix.urllib.request, "urlopen", fake_urlopen)
    row = CardBalanceReconciliation(
        external_id="bitrix:555",
        business_date=date(2026, 4, 22),
        status="mismatch",
        bitrix_item_id="555",
        onec_balance=Decimal("100"),
        diff_amount=Decimal("20"),
    )

    card_balance_bitrix.update_bitrix_item(row, settings=get_settings())

    assert calls[0][0] == "crm.item.update"
    assert calls[0][1]["entityTypeId"] == ["188"]
    assert calls[0][1]["id"] == ["555"]
    assert calls[0][1]["fields[stageId]"] == ["DT188_1:MISMATCH"]


def test_bitrix_list_items_paginates_and_slices(monkeypatch) -> None:
    _configure_env(monkeypatch)
    calls: list[dict[str, list[str]]] = []

    class _Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, timeout=60):
        params = urllib.parse.parse_qs((request.data or b"").decode("utf-8"))
        calls.append(params)
        if len(calls) == 1:
            return _Response({"result": {"items": [{"id": 1}, {"id": 2}]}, "next": 2})
        return _Response({"result": {"items": [{"id": 3}, {"id": 4}]}})

    monkeypatch.setattr(card_balance_bitrix.urllib.request, "urlopen", fake_urlopen)

    items = card_balance_bitrix.list_bitrix_items(settings=get_settings(), limit=3)

    assert [item["id"] for item in items] == [1, 2, 3]
    assert calls[0]["start"] == ["0"]
    assert calls[1]["start"] == ["2"]
    assert calls[0]["filter[categoryId]"] == ["1"]


def test_build_daily_item_fields_sets_stage_and_core_fields(monkeypatch) -> None:
    _configure_env(monkeypatch)
    fields = card_balance_bitrix.build_bitrix_daily_item_fields(
        business_date=date(2026, 4, 23),
        onec_cashbox_code="РБ0000107",
        onec_cashbox_name="1223 Горбушкин Двор карта Тюрнин",
        card_last4="1223",
        employee_name="Тюрнин",
        employee_last_name="Тюрнин",
        settings=get_settings(),
    )
    assert fields["categoryId"] == 1
    assert fields["stageId"] == "DT188_1:NEW"
    assert fields["ufCrmCardBalanceBusinessDate"] == "2026-04-23"
    assert fields["ufCrmCardBalanceOnecCashboxCode"] == "РБ0000107"
    assert fields["ufCrmCardBalanceCardLast4"] == "1223"
    assert fields["ufCrmCardBalanceStatus"] == "missing_screenshot"


def test_build_daily_item_fields_uses_dynamic_cardlast4_key(monkeypatch) -> None:
    monkeypatch.setenv("CARD_BALANCE_BITRIX_WEBHOOK_URL", "https://bitrix.example/rest/1/token")
    monkeypatch.setenv("CARD_BALANCE_BITRIX_ENTITY_TYPE_ID", "1118")
    monkeypatch.setenv("CARD_BALANCE_BITRIX_CATEGORY_ID", "39")
    monkeypatch.setenv(
        "CARD_BALANCE_BITRIX_STAGE_MAP",
        json.dumps({"waiting_screenshot": "DT1118_39:NEW"}),
    )
    monkeypatch.setenv(
        "CARD_BALANCE_BITRIX_FIELD_MAP",
        json.dumps(
            {
                "title": "TITLE",
                "assigned_by": "ASSIGNED_BY_ID",
                "business_date": "UF_CRM_28_BUSINESSDATE",
                "card_last4": "UF_CRM_28_CARDLAST4",
                "onec_cashbox_code": "UF_CRM_28_ONECCASHBOXCODE",
                "onec_cashbox_name": "UF_CRM_28_ONECCASHBOXNAME",
                "status": "UF_CRM_28_STATUS",
            }
        ),
    )
    get_settings.cache_clear()
    get_engine.cache_clear()

    fields = card_balance_bitrix.build_bitrix_daily_item_fields(
        business_date=date(2026, 4, 24),
        onec_cashbox_code="РБ0000176",
        onec_cashbox_name="1285 Горбушкин Двор карта Олимжонов",
        card_last4="1285",
        assigned_by_id="10837",
        settings=get_settings(),
    )
    assert fields["ufCrm_28_CARDLAST4"] == "1285"
    assert fields["ufCrm28Oneccashboxcode"] == "РБ0000176"
    assert fields["ASSIGNED_BY_ID"] == 10837
    get_settings.cache_clear()
    get_engine.cache_clear()


def test_ensure_daily_bitrix_items_creates_only_eligible_cashboxes(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_env(monkeypatch)
    created_fields: list[dict[str, object]] = []

    monkeypatch.setattr(
        card_balance_bitrix,
        "list_existing_cashbox_codes_for_business_date",
        lambda *args, **kwargs: {"РБ0000108"},
    )
    monkeypatch.setattr(
        card_balance_bitrix,
        "create_bitrix_item",
        lambda *, fields, settings=None: created_fields.append(fields) or {"id": "1"},
    )

    try:
        with Session(engine) as session:
            session.add_all(
                [
                    CardBalanceCashbox(
                        onec_cashbox_code="РБ0000107",
                        onec_cashbox_name="1223 Горбушкин Двор карта Тюрнин",
                        card_last4="1223",
                        employee_last_name="Тюрнин",
                        needs_manual_review=False,
                    ),
                    CardBalanceCashbox(
                        onec_cashbox_code="РБ0000108",
                        onec_cashbox_name="1285 Горбушкин Двор карта Олимжонов",
                        card_last4="1285",
                        employee_last_name="Олимжонов",
                        needs_manual_review=False,
                    ),
                    CardBalanceCashbox(
                        onec_cashbox_code="РБ0000109",
                        onec_cashbox_name="8804 Горбушкин Двор карта Букренев",
                        card_last4="8804",
                        employee_last_name="Букренев",
                        needs_manual_review=True,
                    ),
                ]
            )
            session.commit()

            stats = reconciliation_worker._ensure_daily_bitrix_items(
                session,
                business_date=date(2026, 4, 23),
                settings=get_settings(),
            )

        assert stats["created"] == 1
        assert stats["skipped_existing"] == 1
        assert stats["skipped_manual_review"] == 1
        assert stats["eligible_cashboxes"] == 2
        assert len(created_fields) == 1
        assert created_fields[0]["ufCrmCardBalanceOnecCashboxCode"] == "РБ0000107"
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_ensure_daily_bitrix_items_respects_pilot_allowlist(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_env(monkeypatch)
    monkeypatch.setenv("CARD_BALANCE_PILOT_CASHBOX_CODES", "РБ0000107")
    get_settings.cache_clear()
    created_fields: list[dict[str, object]] = []

    monkeypatch.setattr(
        card_balance_bitrix,
        "list_existing_cashbox_codes_for_business_date",
        lambda *args, **kwargs: set(),
    )
    monkeypatch.setattr(
        card_balance_bitrix,
        "create_bitrix_item",
        lambda *, fields, settings=None: created_fields.append(fields) or {"id": "1"},
    )

    try:
        with Session(engine) as session:
            session.add_all(
                [
                    CardBalanceCashbox(
                        onec_cashbox_code="РБ0000107",
                        onec_cashbox_name="1223 Горбушкин Двор карта Тюрнин",
                        card_last4="1223",
                        employee_last_name="Тюрнин",
                    ),
                    CardBalanceCashbox(
                        onec_cashbox_code="РБ0000108",
                        onec_cashbox_name="1285 Горбушкин Двор карта Олимжонов",
                        card_last4="1285",
                        employee_last_name="Олимжонов",
                    ),
                ]
            )
            session.commit()

            stats = reconciliation_worker._ensure_daily_bitrix_items(
                session,
                business_date=date(2026, 4, 23),
                settings=get_settings(),
            )

        assert stats["created"] == 1
        assert stats["skipped_not_in_pilot"] == 1
        assert stats["eligible_cashboxes"] == 1
        assert [field["ufCrmCardBalanceOnecCashboxCode"] for field in created_fields] == [
            "РБ0000107"
        ]
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_ensure_daily_bitrix_items_skips_without_workday_data(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_env(monkeypatch)
    monkeypatch.setenv("CARD_BALANCE_PILOT_CASHBOX_CODES", "РБ0000107")
    monkeypatch.setenv("CARD_BALANCE_REQUIRE_WORKDAY", "true")
    get_settings.cache_clear()
    created_fields: list[dict[str, object]] = []

    monkeypatch.setattr(
        card_balance_bitrix,
        "list_existing_cashbox_codes_for_business_date",
        lambda *args, **kwargs: set(),
    )
    monkeypatch.setattr(
        card_balance_bitrix,
        "create_bitrix_item",
        lambda *, fields, settings=None: created_fields.append(fields) or {"id": "1"},
    )

    try:
        with Session(engine) as session:
            session.add(
                CardBalanceCashbox(
                    onec_cashbox_code="РБ0000107",
                    onec_cashbox_name="1223 Горбушкин Двор карта Тюрнин",
                    card_last4="1223",
                    employee_last_name="Тюрнин",
                    employee_id="130768",
                )
            )
            session.commit()

            stats = reconciliation_worker._ensure_daily_bitrix_items(
                session,
                business_date=date(2026, 4, 23),
                settings=get_settings(),
            )

        assert stats["created"] == 0
        assert stats["skipped_no_workday_data"] == 1
        assert stats["eligible_cashboxes"] == 0
        assert created_fields == []
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_ensure_daily_bitrix_items_creates_for_pilot_workday(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_env(monkeypatch)
    monkeypatch.setenv("CARD_BALANCE_PILOT_CASHBOX_CODES", "РБ0000107")
    monkeypatch.setenv("CARD_BALANCE_REQUIRE_WORKDAY", "true")
    get_settings.cache_clear()
    created_fields: list[dict[str, object]] = []

    monkeypatch.setattr(
        card_balance_bitrix,
        "list_existing_cashbox_codes_for_business_date",
        lambda *args, **kwargs: set(),
    )
    monkeypatch.setattr(
        card_balance_bitrix,
        "create_bitrix_item",
        lambda *, fields, settings=None: created_fields.append(fields) or {"id": "1"},
    )
    monkeypatch.setattr(
        reconciliation_worker,
        "_resolve_cashbox_bitrix_employee",
        lambda *args, **kwargs: {
            "bitrix_user_id": "10837",
            "full_name": "Асадбек Олимжонов",
        },
    )

    try:
        with Session(engine) as session:
            session.add_all(
                [
                    CardBalanceCashbox(
                        onec_cashbox_code="РБ0000107",
                        onec_cashbox_name="1285 Горбушкин Двор карта Олимжонов",
                        card_last4="1285",
                        employee_last_name="Олимжонов",
                        employee_id="10837",
                    ),
                    StoreShiftFact(
                        source="b24_schedule",
                        business_key="2026-04-23:store-1:1",
                        external_shift_ref="shift-1",
                        slot_no=1,
                        shift_date=date(2026, 4, 23),
                        shift_code="open",
                        store_ref="store-1",
                        staff_ref="10837",
                        staff_name="Асадбек Олимжонов",
                        attendance_status="confirmed",
                    ),
                ]
            )
            session.commit()

            stats = reconciliation_worker._ensure_daily_bitrix_items(
                session,
                business_date=date(2026, 4, 23),
                settings=get_settings(),
            )

        assert stats["created"] == 1
        assert stats["skipped_no_workday_data"] == 0
        assert stats["eligible_cashboxes"] == 1
        assert created_fields[0]["ufCrmCardBalanceEmployeeUser"] == 10837
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_resolve_cashbox_bitrix_employee_by_last_name_and_store(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_env(monkeypatch)
    reconciliation_worker._BITRIX_USER_SEARCH_CACHE.clear()
    reconciliation_worker._BITRIX_USER_BY_ID_CACHE.clear()
    monkeypatch.setattr(
        reconciliation_worker,
        "_search_bitrix_users",
        lambda *, settings, query: [
            {"ID": "10837", "NAME": "Асадбек", "LAST_NAME": "Олимжонов", "ACTIVE": True}
        ],
    )
    try:
        with Session(engine) as session:
            cashbox = CardBalanceCashbox(
                onec_cashbox_code="РБ0000176",
                onec_cashbox_name="1285 Горбушкин Двор карта Олимжонов",
                card_last4="1285",
                store_name="Горбушкин Двор",
                employee_last_name="Олимжонов",
                is_active=True,
                needs_manual_review=False,
            )
            session.add(cashbox)
            session.commit()

            resolved = reconciliation_worker._resolve_cashbox_bitrix_employee(
                session,
                cashbox,
                settings=get_settings(),
            )

            assert resolved is not None
            assert resolved["bitrix_user_id"] == "10837"
            assert "Олимжонов" in resolved["full_name"]
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_resolve_cashbox_bitrix_employee_prefers_override(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_env(monkeypatch)
    monkeypatch.setenv(
        "CARD_BALANCE_BITRIX_EMPLOYEE_OVERRIDES",
        json.dumps({"РБ0000167": "130742"}),
    )
    get_settings.cache_clear()
    get_engine.cache_clear()
    reconciliation_worker._BITRIX_USER_SEARCH_CACHE.clear()
    reconciliation_worker._BITRIX_USER_BY_ID_CACHE.clear()

    monkeypatch.setattr(
        reconciliation_worker,
        "_load_bitrix_user_by_id",
        lambda *, settings, user_id: (
            {
                "ID": user_id,
                "NAME": "Асадбек",
                "LAST_NAME": "Олимжонов",
                "ACTIVE": True,
            }
            if user_id == "130742"
            else None
        ),
    )
    monkeypatch.setattr(
        reconciliation_worker,
        "_search_bitrix_users",
        lambda *, settings, query: pytest.fail("override must skip user.search"),
    )

    try:
        with Session(engine) as session:
            cashbox = CardBalanceCashbox(
                onec_cashbox_code="РБ0000167",
                onec_cashbox_name="0832 СПБ Садовая карта Пальщиков",
                card_last4="0832",
                employee_last_name="Пальщиков",
                is_active=True,
                needs_manual_review=False,
            )
            session.add(cashbox)
            session.commit()

            resolved = reconciliation_worker._resolve_cashbox_bitrix_employee(
                session,
                cashbox,
                settings=get_settings(),
            )

            assert resolved is not None
            assert resolved["bitrix_user_id"] == "130742"
            assert cashbox.employee_id == "130742"
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_resolve_cashbox_bitrix_employee_uses_saved_employee_id(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_env(monkeypatch)
    reconciliation_worker._BITRIX_USER_SEARCH_CACHE.clear()
    reconciliation_worker._BITRIX_USER_BY_ID_CACHE.clear()
    monkeypatch.setattr(
        reconciliation_worker,
        "_load_bitrix_user_by_id",
        lambda *, settings, user_id: (
            {
                "ID": user_id,
                "NAME": "Вадим",
                "LAST_NAME": "Тюрнин",
                "ACTIVE": True,
            }
            if user_id == "130768"
            else None
        ),
    )
    monkeypatch.setattr(
        reconciliation_worker,
        "_search_bitrix_users",
        lambda *, settings, query: pytest.fail("saved employee_id must skip user.search"),
    )
    try:
        with Session(engine) as session:
            cashbox = CardBalanceCashbox(
                onec_cashbox_code="РБ0000107",
                onec_cashbox_name="1223 Горбушкин Двор карта Тюрнин",
                card_last4="1223",
                employee_last_name="Тюрнин",
                employee_id="130768",
                is_active=True,
                needs_manual_review=False,
            )
            session.add(cashbox)
            session.commit()

            resolved = reconciliation_worker._resolve_cashbox_bitrix_employee(
                session,
                cashbox,
                settings=get_settings(),
            )

            assert resolved is not None
            assert resolved["bitrix_user_id"] == "130768"
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_enrich_payload_with_cashbox_keeps_code_and_adds_employee_id(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_env(monkeypatch)
    reconciliation_worker._BITRIX_USER_SEARCH_CACHE.clear()
    reconciliation_worker._BITRIX_USER_BY_ID_CACHE.clear()
    monkeypatch.setattr(
        reconciliation_worker,
        "_search_bitrix_users",
        lambda *, settings, query: [
            {"ID": "10837", "NAME": "Асадбек", "LAST_NAME": "Олимжонов", "ACTIVE": True}
        ],
    )
    try:
        with Session(engine) as session:
            cashbox = CardBalanceCashbox(
                onec_cashbox_code="РБ0000176",
                onec_cashbox_name="1285 Горбушкин Двор карта Олимжонов",
                card_last4="1285",
                store_name="Горбушкин Двор",
                employee_last_name="Олимжонов",
                is_active=True,
                needs_manual_review=False,
            )
            session.add(cashbox)
            session.commit()

            enriched = reconciliation_worker._enrich_payload_with_cashbox(
                session,
                {
                    "external_id": "bitrix:23",
                    "business_date": "2026-04-24",
                    "onec_cashbox_code": "РБ0000176",
                    "employee_last_name": "Олимжонов",
                },
            )

            assert enriched["onec_cashbox_code"] == "РБ0000176"
            assert enriched["card_last4"] == "1285"
            assert enriched["employee_id"] == "10837"
            assert "Олимжонов" in (enriched.get("employee_name") or "")
    finally:
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_score_employee_match_rejects_single_token_hit_for_multi_token_name() -> None:
    tokens = reconciliation_worker._employee_tokens("сбер Владислав Аннамурадов")
    score = reconciliation_worker._score_employee_match(
        tokens,
        {"NAME": "Билевич", "LAST_NAME": "Владислав"},
    )
    assert score == 0


def test_score_employee_match_accepts_two_token_match() -> None:
    tokens = reconciliation_worker._employee_tokens("Т-Банк Аскеров Фарман")
    score = reconciliation_worker._score_employee_match(
        tokens,
        {"NAME": "Фарман", "LAST_NAME": "Аскеров"},
    )
    assert score > 0


def test_enrich_payload_clears_implausible_saved_employee_id(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_env(monkeypatch)
    reconciliation_worker._BITRIX_USER_SEARCH_CACHE.clear()
    reconciliation_worker._BITRIX_USER_BY_ID_CACHE.clear()
    monkeypatch.setattr(
        reconciliation_worker,
        "_load_bitrix_user_by_id",
        lambda *, settings, user_id: (
            {
                "ID": user_id,
                "NAME": "Билевич",
                "LAST_NAME": "Владислав",
                "ACTIVE": True,
            }
            if user_id == "533"
            else None
        ),
    )
    monkeypatch.setattr(
        reconciliation_worker,
        "_search_bitrix_users",
        lambda *, settings, query: [],
    )
    try:
        with Session(engine) as session:
            cashbox = CardBalanceCashbox(
                onec_cashbox_code="РБ0000195",
                onec_cashbox_name="9016  карта сбер Владислав Аннамурадов",
                card_last4="9016",
                employee_last_name="сбер Владислав Аннамурадов",
                employee_id="533",
                is_active=True,
                needs_manual_review=False,
            )
            session.add(cashbox)
            session.commit()

            enriched = reconciliation_worker._enrich_payload_with_cashbox(
                session,
                {
                    "external_id": "bitrix:12",
                    "business_date": "2026-04-23",
                    "onec_cashbox_code": "РБ0000195",
                    "employee_last_name": "сбер Владислав Аннамурадов",
                    "employee_id": "533",
                },
            )
            assert enriched["employee_id"] is None
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_bitrix_sync_item_records_update_error(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_env(monkeypatch)

    def fail_update(row, *, settings=None):
        raise RuntimeError("Bitrix update failed")

    monkeypatch.setattr(card_balance_bitrix, "update_bitrix_item", fail_update)

    try:
        business_date = reconciliation_service.utcnow().date().isoformat()
        with Session(engine) as session:
            session.add(
                CardBalanceCashbox(
                    onec_cashbox_code="РБ0000107",
                    onec_cashbox_name="1223 Горбушкин Двор карта Тюрнин",
                    card_last4="1223",
                    employee_last_name="Тюрнин",
                )
            )
            session.commit()

            with pytest.raises(RuntimeError, match="Bitrix update failed"):
                card_balance_bitrix.sync_bitrix_item(
                    session,
                    item={"id": 555, "stageId": "DT188_1:RECEIVED"},
                    decoded_payload={
                        "external_id": "bitrix:555",
                        "business_date": business_date,
                        "employee_last_name": "Тюрнин",
                        "card_last4": "1223",
                        "onec_cashbox_code": "РБ0000107",
                        "bitrix_item_id": "555",
                        "screenshot_file_id": "file-1",
                        "manual_balance": "0",
                    },
                    onec_balances={"РБ0000107": Decimal("0")},
                    settings=get_settings(),
                )

            stored = session.scalar(
                select(CardBalanceReconciliation).where(
                    CardBalanceReconciliation.external_id == "bitrix:555"
                )
            )
            assert stored is not None
            assert stored.status == "matched"
            assert stored.bitrix_last_error == "Bitrix update failed"
            event = session.scalar(
                select(CardBalanceReconciliationEvent).where(
                    CardBalanceReconciliationEvent.reconciliation_id == stored.id,
                    CardBalanceReconciliationEvent.event_type == "bitrix_sync_error",
                )
            )
            assert event is not None
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_bitrix_sync_item_applies_ocr_result(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    get_settings.cache_clear()
    business_date = reconciliation_service.utcnow().date().isoformat()

    class FakeOCRClient:
        def __init__(self, *, settings=None, client=None):
            self.settings = settings

        def extract_balance(self, *, image_bytes, mime_type, item_title=None):
            assert image_bytes == b"png-bytes"
            assert mime_type == "image/png"
            assert item_title == "24.04.2026 6426 карта Куценко Дмитрий"
            from app.services.card_balance_ocr import CardBalanceOCRResult

            return CardBalanceOCRResult(
                recognized_balance=Decimal("1060.00"),
                confidence=Decimal("0.9800"),
                evidence="Баланс: 1 060 ₽",
                raw_response_text='{"balance":"1060.00","confidence":0.98,"evidence":"Баланс: 1 060 ₽"}',
            )

    monkeypatch.setattr(card_balance_bitrix, "CardBalanceOCRClient", FakeOCRClient)
    monkeypatch.setattr(
        card_balance_bitrix,
        "download_bitrix_item_screenshot",
        lambda item, settings=None: (b"png-bytes", "image/png"),
    )
    monkeypatch.setattr(
        card_balance_bitrix, "update_and_mark_bitrix_item", lambda *args, **kwargs: None
    )

    try:
        with Session(engine) as session:
            session.add(
                CardBalanceCashbox(
                    onec_cashbox_code="РБ0000220",
                    onec_cashbox_name="6426 карта Куценко Дмитрий",
                    card_last4="6426",
                    employee_last_name="Куценко Дмитрий",
                )
            )
            session.commit()

            row = card_balance_bitrix.sync_bitrix_item(
                session,
                item={"id": 48, "title": "24.04.2026 6426 карта Куценко Дмитрий"},
                decoded_payload={
                    "external_id": "bitrix:48",
                    "business_date": business_date,
                    "employee_last_name": "Куценко Дмитрий",
                    "card_last4": "6426",
                    "onec_cashbox_code": "РБ0000220",
                    "bitrix_item_id": "48",
                    "screenshot_file_id": "2710162",
                },
                onec_balances={"РБ0000220": Decimal("1060.00")},
                settings=get_settings(),
            )

            assert row.recognized_balance == Decimal("1060.00")
            assert row.recognition_confidence == Decimal("0.9800")
            assert row.status == "matched"
            assert row.payload["ocr_evidence"] == "Баланс: 1 060 ₽"
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_bitrix_sync_item_manual_balance_does_not_require_ocr(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    get_settings.cache_clear()
    business_date = reconciliation_service.utcnow().date().isoformat()

    class FailingOCRClient:
        def __init__(self, *, settings=None, client=None):
            self.settings = settings

        def extract_balance(self, *, image_bytes, mime_type, item_title=None):
            pytest.fail("manual_balance must bypass OCR")

    monkeypatch.setattr(card_balance_bitrix, "CardBalanceOCRClient", FailingOCRClient)
    monkeypatch.setattr(
        card_balance_bitrix,
        "download_bitrix_item_screenshot",
        lambda item, settings=None: pytest.fail("manual_balance must skip screenshot download"),
    )
    monkeypatch.setattr(
        card_balance_bitrix, "update_and_mark_bitrix_item", lambda *args, **kwargs: None
    )

    try:
        with Session(engine) as session:
            session.add(
                CardBalanceCashbox(
                    onec_cashbox_code="РБ0000220",
                    onec_cashbox_name="6426 карта Куценко Дмитрий",
                    card_last4="6426",
                    employee_last_name="Куценко Дмитрий",
                )
            )
            session.commit()

            row = card_balance_bitrix.sync_bitrix_item(
                session,
                item={"id": 49, "title": "24.04.2026 6426 карта Куценко Дмитрий"},
                decoded_payload={
                    "external_id": "bitrix:49",
                    "business_date": business_date,
                    "employee_last_name": "Куценко Дмитрий",
                    "card_last4": "6426",
                    "onec_cashbox_code": "РБ0000220",
                    "bitrix_item_id": "49",
                    "screenshot_file_id": "2710162",
                    "manual_balance": "1060.00",
                },
                onec_balances={"РБ0000220": Decimal("1060.00")},
                settings=get_settings(),
            )

            assert row.status == "matched"
            assert row.manual_balance == Decimal("1060.00")
            assert row.recognized_balance is None
            assert "ocr_error" not in row.payload
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_bitrix_sync_counts_unmapped_item_and_ocr_error(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    get_settings.cache_clear()
    business_date = reconciliation_service.utcnow().date().isoformat()

    class FakeExtractor:
        def fetch_balance_by_cashbox_codes(self, *, business_date, cashbox_codes):
            return {}

    monkeypatch.setattr(reconciliation_worker, "_get_app_engine", lambda: engine)
    monkeypatch.setattr(
        card_balance_bitrix,
        "list_bitrix_items",
        lambda *, settings=None, limit=50: [{"id": 49, "title": "Карточка без привязки"}],
    )
    monkeypatch.setattr(
        card_balance_bitrix,
        "decode_bitrix_item",
        lambda item, settings=None: {
            "external_id": "bitrix:49",
            "business_date": business_date,
            "bitrix_item_id": "49",
            "screenshot_file_id": "2710162",
        },
    )
    monkeypatch.setattr(
        card_balance_bitrix,
        "download_bitrix_item_screenshot",
        lambda item, settings=None: (_ for _ in ()).throw(RuntimeError("403 Forbidden")),
    )
    monkeypatch.setattr(
        card_balance_bitrix, "update_and_mark_bitrix_item", lambda *args, **kwargs: None
    )

    try:
        result = reconciliation_worker.run_card_balance_bitrix_sync(extractor=FakeExtractor())

        assert result["processed"] == 1
        assert result["exceptions"] == 1
        assert result["skipped_unmapped_bitrix_item"] == 1
        assert result["ocr_errors"] == 1
        with Session(engine) as session:
            row = session.scalar(
                select(CardBalanceReconciliation).where(
                    CardBalanceReconciliation.external_id == "bitrix:49"
                )
            )
            assert row is not None
            assert row.status == "unmapped_card"
            assert row.payload["mapping_error"] == "unmapped_card"
            assert "кассе 1С" in row.payload["manual_review_reason"]
            assert row.payload["ocr_error"] == "403 Forbidden"
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_bitrix_sync_item_keeps_low_confidence_when_ocr_is_uncertain(monkeypatch) -> None:
    engine, path = _setup_db()
    _configure_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    get_settings.cache_clear()
    business_date = reconciliation_service.utcnow().date().isoformat()

    class FakeOCRClient:
        def __init__(self, *, settings=None, client=None):
            self.settings = settings

        def extract_balance(self, *, image_bytes, mime_type, item_title=None):
            from app.services.card_balance_ocr import CardBalanceOCRResult

            return CardBalanceOCRResult(
                recognized_balance=None,
                confidence=Decimal("0.4100"),
                evidence="Вижу несколько сумм, баланс неочевиден",
                raw_response_text='{"balance":null,"confidence":0.41,"evidence":"Вижу несколько сумм"}',
            )

    monkeypatch.setattr(card_balance_bitrix, "CardBalanceOCRClient", FakeOCRClient)
    monkeypatch.setattr(
        card_balance_bitrix,
        "download_bitrix_item_screenshot",
        lambda item, settings=None: (b"png-bytes", "image/png"),
    )
    monkeypatch.setattr(
        card_balance_bitrix, "update_and_mark_bitrix_item", lambda *args, **kwargs: None
    )

    try:
        with Session(engine) as session:
            session.add(
                CardBalanceCashbox(
                    onec_cashbox_code="РБ0000220",
                    onec_cashbox_name="6426 карта Куценко Дмитрий",
                    card_last4="6426",
                    employee_last_name="Куценко Дмитрий",
                )
            )
            session.commit()

            row = card_balance_bitrix.sync_bitrix_item(
                session,
                item={"id": 48, "title": "24.04.2026 6426 карта Куценко Дмитрий"},
                decoded_payload={
                    "external_id": "bitrix:48",
                    "business_date": business_date,
                    "employee_last_name": "Куценко Дмитрий",
                    "card_last4": "6426",
                    "onec_cashbox_code": "РБ0000220",
                    "bitrix_item_id": "48",
                    "screenshot_file_id": "2710162",
                },
                onec_balances={"РБ0000220": Decimal("1060.00")},
                settings=get_settings(),
            )

            assert row.recognized_balance is None
            assert row.recognition_confidence == Decimal("0.4100")
            assert row.status == "low_confidence"
            assert row.payload["ocr_error"] == "OCR did not extract confident balance for item 48"
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_card_balance_api_is_protected_and_returns_exceptions(monkeypatch) -> None:
    engine, path = _setup_db()
    headers = _configure_env(monkeypatch)
    app.dependency_overrides = {get_db: _override_db(engine)}
    client = TestClient(app)

    try:
        business_date = reconciliation_service.utcnow().date().isoformat()
        assert client.get("/api/card-balance-reconciliation/events").status_code == 401
        with Session(engine) as session:
            session.add(
                CardBalanceCashbox(
                    onec_cashbox_code="РБ0000107",
                    onec_cashbox_name="1223 Горбушкин Двор карта Тюрнин",
                    card_last4="1223",
                    employee_last_name="Тюрнин",
                )
            )
            session.commit()

        created = client.post(
            "/api/card-balance-reconciliation/events",
            headers=headers,
            json={
                "external_id": "manual-1",
                "business_date": business_date,
                "employee_name": "Арсен Тюрнин",
                "card_last4": "1223",
                "screenshot_file_id": "file-1",
                "manual_balance": "120",
                "onec_balance": "100",
            },
        )
        assert created.status_code == 200
        assert created.json()["status"] == "mismatch"
        assert created.json()["diff_amount"] == "20.00"

        exceptions = client.get("/api/card-balance-reconciliation/exceptions", headers=headers)
        assert exceptions.status_code == 200
        assert [item["external_id"] for item in exceptions.json()] == ["manual-1"]

        with Session(engine) as session:
            stored = session.scalar(
                select(CardBalanceReconciliation).where(
                    CardBalanceReconciliation.external_id == "manual-1"
                )
            )
            assert stored is not None
            assert stored.onec_cashbox_code == "РБ0000107"
    finally:
        app.dependency_overrides = {}
        get_settings.cache_clear()
        get_engine.cache_clear()
        engine.dispose()
        if os.path.exists(path):
            os.remove(path)


def test_ensure_bitrix_process_dry_run_writes_mapping(tmp_path) -> None:
    mapping_path = tmp_path / "mapping.json"
    details_path = tmp_path / "details.json"
    result_path = tmp_path / "result.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/ensure_card_balance_bitrix_process.py",
            "--dry-run",
            "--mapping-path",
            str(mapping_path),
            "--details-config-path",
            str(details_path),
            "--result-path",
            str(result_path),
        ],
        cwd=os.getcwd(),
        check=True,
        capture_output=True,
        text=True,
    )

    stdout = json.loads(result.stdout)
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    details = json.loads(details_path.read_text(encoding="utf-8"))
    written_result = json.loads(result_path.read_text(encoding="utf-8"))
    assert stdout["dry_run"] is True
    assert written_result["dry_run"] is True
    assert mapping["process"]["title"] == "Сверка балансов карт менеджеров"
    assert mapping["stage_map"]["waiting_screenshot"] == "DT0_0:NEW"
    assert mapping["stage_map"]["matched"] == "DT0_0:MATCHED"
    assert mapping["fields"]["employee_user"] == "UF_CRM_CARD_BALANCE_EMPLOYEE_USER"
    assert mapping["fields"]["manual_balance"] == "UF_CRM_CARD_BALANCE_MANUAL_BALANCE"
    assert details[0]["title"] == "Сверка"
