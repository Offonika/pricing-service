from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.core.config import Settings
from app.models.site_service_requests import (
    SiteServiceRequestCase,
    SiteServiceRequestEvent,
)
from app.schemas.site_service_requests import SiteServiceEmailEventPayload
from app.services.site_service_request_email_worker import (
    _verify_email_event,
    process_site_service_email_events,
)
from app.services.site_service_requests import (
    SiteServiceRequestCipher,
    accept_site_service_email_event,
)

_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"m" * 32).decode("ascii")


def _settings(**overrides) -> Settings:
    field_map = {
        "site_ticket_id": "UF_TEST_SITE_TICKET_ID",
        "site_ticket_url": "UF_TEST_SITE_TICKET_URL",
        "site_history": "UF_TEST_SITE_HISTORY",
        "site_sync_status": "UF_TEST_SYNC_STATUS",
        "site_last_sync_at": "UF_TEST_LAST_SYNC",
        "first_response_due_at": "UF_TEST_RESPONSE_DUE",
        "first_response_at": "UF_TEST_RESPONSE_AT",
        "site_sync_error": "UF_TEST_SYNC_ERROR",
        "mail_activity_id": "UF_TEST_MAIL_ACTIVITY_ID",
        "mail_activity_url": "UF_TEST_MAIL_ACTIVITY_URL",
        "mail_thread_key": "UF_TEST_MAIL_THREAD_KEY",
    }
    enum_map = {
        "sync_status_synced": "1",
        "sync_status_client_match_required": "2",
        "sync_status_order_match_required": "3",
        "sync_status_order_not_found": "4",
        "sync_status_file_sync_error": "5",
        "sync_status_assignment_waiting": "6",
        "request_type_warranty": "11",
        "request_type_refund_money": "12",
        "request_type_replacement": "13",
        "request_type_delivery_return": "14",
        "request_type_consultation": "15",
        "request_type_other": "16",
    }
    values = {
        "site_service_requests_email_ingest_enabled": True,
        "site_service_requests_bitrix_writes_enabled": True,
        "site_service_requests_event_encryption_key": _ENCRYPTION_KEY,
        "site_service_requests_bitrix_webhook_url": ("https://crm.master-mobile.ru/rest/1/token"),
        "site_service_requests_bitrix_field_map": field_map,
        "site_service_requests_bitrix_stage_map": {"new": "DT1134_55:NEW"},
        "site_service_requests_bitrix_enum_map": enum_map,
        "site_service_requests_crm_order_field": "UF_CRM_ORDER",
        "site_service_requests_first_line_user_ids": [1001, 1002],
        "site_service_requests_escalation_user_id": 1003,
        "site_service_requests_first_response_hours": 4,
    }
    values.update(overrides)
    return Settings(**values)


def _payload(
    *,
    message_id: int = 88001,
    activity_id: int = 99001,
    event_type: str = "email.received",
    existing_item_id: int | None = None,
    thread_id: int = 777,
) -> SiteServiceEmailEventPayload:
    values: dict[str, Any] = {
        "schemaVersion": 1,
        "eventId": f"bitrix-mail:shop:{message_id}",
        "eventType": event_type,
        "occurredAt": "2026-08-25T12:00:00+03:00",
        "mailbox": "shop",
        "messageId": message_id,
        "activityId": activity_id,
        "threadId": thread_id,
        "requestType": "warranty",
        "orderNumber": "241887",
        "crmContactId": 501,
        "crmDealId": 601,
    }
    if existing_item_id is not None:
        values["existingServiceItemId"] = existing_item_id
    return SiteServiceEmailEventPayload.model_validate(values)


def _persist(db_session, payload: SiteServiceEmailEventPayload) -> None:
    body = json.dumps(
        payload.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    accept_site_service_email_event(
        db_session,
        payload=payload,
        raw_body=body,
        payload_sha256=hashlib.sha256(body).hexdigest(),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
        now=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
    )
    db_session.commit()


class FakeEmailBitrixApi:
    def __init__(self) -> None:
        self.activities: dict[int, dict[str, Any]] = {
            99001: {
                "ID": "99001",
                "PROVIDER_ID": "CRM_EMAIL",
                "DIRECTION": "1",
                "THREAD_ID": "777",
                "COMPLETED": "N",
                "BINDINGS": [
                    {"OWNER_TYPE_ID": "2", "OWNER_ID": "601"},
                    {"OWNER_TYPE_ID": "3", "OWNER_ID": "501"},
                ],
                "COMMUNICATIONS": [
                    {
                        "ENTITY_TYPE_ID": "3",
                        "ENTITY_ID": "501",
                        "VALUE": "buyer@example.test",
                    }
                ],
            }
        }
        self.deal = {
            "ID": "601",
            "CONTACT_ID": "501",
            "ASSIGNED_BY_ID": "7777",
            "UF_CRM_ORDER": "241887",
        }
        self.contact = {
            "ID": "501",
            "EMAIL": [{"VALUE": "buyer@example.test"}],
        }
        self.timeman = {1001: "OPENED", 1002: "CLOSED"}
        self.items: dict[int, dict[str, Any]] = {}
        self.next_item_id = 4321
        self.notifications: list[tuple[int, str]] = []
        self.calls: list[str] = []
        self.activity_get_views: dict[int, dict[str, Any]] = {}

    def call(self, method: str, params=None, **_kwargs):
        values = list(params or [])
        mapped = dict(values)
        self.calls.append(method)
        if method == "crm.activity.get":
            activity_id = int(mapped["id"])
            activity = deepcopy(self.activities[activity_id])
            activity.update(deepcopy(self.activity_get_views.get(activity_id, {})))
            return {"result": deepcopy(activity)}
        if method == "crm.activity.list":
            return {"result": [deepcopy(activity) for activity in self.activities.values()]}
        if method == "crm.deal.get":
            return {"result": deepcopy(self.deal)}
        if method == "crm.contact.get":
            return {"result": deepcopy(self.contact)}
        if method == "timeman.status":
            return {"result": {"STATUS": self.timeman[int(mapped["USER_ID"])]}}
        if method == "crm.item.list":
            filters = {
                key[len("filter[") : -1]: value
                for key, value in values
                if key.startswith("filter[")
            }
            rows = [
                {"id": item_id, **item}
                for item_id, item in self.items.items()
                if all(str(item.get(key)) == str(value) for key, value in filters.items())
            ]
            return {"result": {"items": rows}}
        if method == "crm.item.add":
            item_id = self.next_item_id
            self.next_item_id += 1
            self.items[item_id] = _fields(values)
            return {"result": {"item": {"id": item_id}}}
        if method == "crm.item.update":
            item_id = int(mapped["id"])
            self.items[item_id].update(_fields(values))
            return {"result": {"item": {"id": item_id}}}
        if method == "crm.item.get":
            item_id = int(mapped["id"])
            return {"result": {"item": {"id": item_id, **self.items[item_id]}}}
        if method == "im.notify.personal.add":
            self.notifications.append((int(mapped["USER_ID"]), str(mapped["TAG"])))
            return {"result": len(self.notifications)}
        raise AssertionError(f"unexpected method {method}")

    def call_json(self, method: str, payload: dict, **_kwargs):
        self.calls.append(method)
        if method == "crm.activity.update":
            activity = self.activities[int(payload["id"])]
            activity.update(deepcopy(payload["fields"]))
            return {"result": True}
        raise AssertionError(f"unexpected JSON method {method}")


def _fields(params: list[tuple[str, str]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in params:
        if not key.startswith("fields["):
            continue
        field = key[len("fields[") :].split("]", 1)[0]
        if key.endswith("[]"):
            result.setdefault(field, []).append(value)
        else:
            result[field] = value
    return result


def test_received_email_creates_one_card_without_creating_contact_or_lead(
    db_session,
) -> None:
    _persist(db_session, _payload())
    api = FakeEmailBitrixApi()

    results = process_site_service_email_events(
        db_session,
        settings=_settings(),
        api=api,
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
        now=datetime(2026, 8, 25, 9, 5, tzinfo=UTC),
    )

    assert len(results) == 1 and results[0].status == "processed"
    case = db_session.scalar(select(SiteServiceRequestCase))
    event = db_session.scalar(select(SiteServiceRequestEvent))
    assert case is not None and case.bitrix_item_id == 4321
    assert case.assigned_user_id == 1001
    assert case.deal_manager_notified_at is not None
    assert event is not None and event.payload_encrypted is None
    assert "crm.contact.add" not in api.calls
    assert "crm.lead.add" not in api.calls
    assert api.notifications == [(7777, f"mm-service-email-manager:{case.id}")]
    assert {"OWNER_TYPE_ID": 1134, "OWNER_ID": 4321} in api.activities[99001]["BINDINGS"]


def test_standalone_first_email_uses_activity_id_as_thread_key(db_session) -> None:
    _persist(db_session, _payload(thread_id=99001))
    api = FakeEmailBitrixApi()
    api.activities[99001]["THREAD_ID"] = "0"

    results = process_site_service_email_events(
        db_session,
        settings=_settings(),
        api=api,
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
        now=datetime(2026, 8, 25, 9, 5, tzinfo=UTC),
    )

    assert len(results) == 1 and results[0].status == "processed"
    case = db_session.scalar(select(SiteServiceRequestCase))
    event = db_session.scalar(select(SiteServiceRequestEvent))
    assert case is not None and case.source_thread_id == 99001
    assert case.bitrix_item_id == 4321
    assert event is not None and event.payload_encrypted is None
    assert "crm.contact.add" not in api.calls
    assert "crm.lead.add" not in api.calls
    assert api.notifications == [(7777, f"mm-service-email-manager:{case.id}")]
    assert {"OWNER_TYPE_ID": 1134, "OWNER_ID": 4321} in api.activities[99001]["BINDINGS"]


def test_box_activity_uses_primary_deal_and_contact_communication(db_session) -> None:
    _persist(db_session, _payload(thread_id=99001))
    api = FakeEmailBitrixApi()
    api.activities[99001] = {
        "ID": "99001",
        "PROVIDER_ID": "CRM_EMAIL",
        "DIRECTION": "1",
        "THREAD_ID": None,
        "OWNER_TYPE_ID": "2",
        "OWNER_ID": "601",
        "COMPLETED": "N",
        "BINDINGS": None,
        "COMMUNICATIONS": [
            {
                "ENTITY_TYPE_ID": "3",
                "ENTITY_ID": "501",
                "VALUE": "buyer@example.test",
            }
        ],
    }
    api.activity_get_views[99001] = {
        "ID": "99001",
        "PROVIDER_ID": "CRM_EMAIL",
        "DIRECTION": "1",
        "THREAD_ID": None,
        "OWNER_TYPE_ID": "2",
        "OWNER_ID": "601",
        "COMPLETED": "N",
        "BINDINGS": None,
        "COMMUNICATIONS": [],
    }

    results = process_site_service_email_events(
        db_session,
        settings=_settings(),
        api=api,
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
        now=datetime(2026, 8, 25, 9, 5, tzinfo=UTC),
    )

    assert len(results) == 1 and results[0].status == "processed"
    case = db_session.scalar(select(SiteServiceRequestCase))
    assert case is not None and case.bitrix_item_id == 4321
    assert "crm.activity.list" in api.calls
    assert {"OWNER_TYPE_ID": 2, "OWNER_ID": 601} in api.activities[99001]["BINDINGS"]
    assert {"OWNER_TYPE_ID": 1134, "OWNER_ID": 4321} in api.activities[99001]["BINDINGS"]


def test_outgoing_reply_stops_sla_and_closes_email_not_card(db_session) -> None:
    _persist(db_session, _payload())
    api = FakeEmailBitrixApi()
    settings = _settings()
    process_site_service_email_events(
        db_session,
        settings=settings,
        api=api,
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
        now=datetime(2026, 8, 25, 9, 5, tzinfo=UTC),
    )
    api.activities[99002] = {
        "ID": "99002",
        "PROVIDER_ID": "CRM_EMAIL",
        "DIRECTION": "2",
        "THREAD_ID": "777",
        "COMPLETED": "Y",
        "BINDINGS": [
            {"OWNER_TYPE_ID": "2", "OWNER_ID": "601"},
            {"OWNER_TYPE_ID": "3", "OWNER_ID": "501"},
            {"OWNER_TYPE_ID": "1134", "OWNER_ID": "4321"},
        ],
        "COMMUNICATIONS": [
            {
                "ENTITY_TYPE_ID": "3",
                "ENTITY_ID": "501",
                "VALUE": "buyer@example.test",
            }
        ],
    }
    _persist(
        db_session,
        _payload(message_id=88002, activity_id=99002, event_type="email.replied"),
    )

    results = process_site_service_email_events(
        db_session,
        settings=settings,
        api=api,
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
        now=datetime(2026, 8, 25, 9, 10, tzinfo=UTC),
    )

    case = db_session.scalar(select(SiteServiceRequestCase))
    assert len(results) == 1 and results[0].status == "processed"
    assert case is not None and case.first_response_at is not None
    assert api.activities[99001]["COMPLETED"] == "Y"
    assert api.items[4321]["stageId"] == "DT1134_55:NEW"
    assert db_session.scalar(select(func.count(SiteServiceRequestCase.id))) == 1
    assert len(api.notifications) == 1


def test_existing_card_may_be_confirmed_by_inherited_thread_binding() -> None:
    api = FakeEmailBitrixApi()
    api.activities[99000] = {
        "ID": "99000",
        "PROVIDER_ID": "CRM_EMAIL",
        "DIRECTION": "2",
        "THREAD_ID": "777",
        "COMPLETED": "Y",
        "BINDINGS": [
            {"OWNER_TYPE_ID": "2", "OWNER_ID": "601"},
            {"OWNER_TYPE_ID": "3", "OWNER_ID": "501"},
            {"OWNER_TYPE_ID": "1134", "OWNER_ID": "4321"},
        ],
        "COMMUNICATIONS": [],
    }

    result = _verify_email_event(
        payload=_payload(existing_item_id=4321),
        api=api,
        settings=_settings(),
    )

    assert result.deal_manager_user_id == 7777


def test_existing_site_card_keeps_original_idempotency_key(db_session) -> None:
    site_case = SiteServiceRequestCase(
        source_ticket_id=741,
        source_kind="site_ticket",
        source_key="site-support-ticket:741",
        bitrix_item_id=4321,
    )
    db_session.add(site_case)
    db_session.commit()
    _persist(db_session, _payload(existing_item_id=4321))
    api = FakeEmailBitrixApi()
    api.activities[99001]["BINDINGS"].append({"OWNER_TYPE_ID": "1134", "OWNER_ID": "4321"})
    api.items[4321] = {
        "stageId": "DT1134_55:NEW",
        "ufCrm36Idempotencykey": "site-support-ticket:741",
        "ufCrm36Source": "site-support-ticket",
        "ufCrm36Problemdescription": "Исходное описание сайта",
        "title": "Исходный тикет сайта",
    }
    api.next_item_id = 5000

    results = process_site_service_email_events(
        db_session,
        settings=_settings(),
        api=api,
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
        now=datetime(2026, 8, 25, 9, 5, tzinfo=UTC),
    )

    assert len(results) == 1 and results[0].bitrix_item_id == 4321
    assert api.items[4321]["ufCrm36Idempotencykey"] == "site-support-ticket:741"
    assert api.items[4321]["ufCrm36Source"] == "site-support-ticket"
    assert api.items[4321]["ufCrm36Problemdescription"] == "Исходное описание сайта"
    assert api.items[4321]["title"] == "Исходный тикет сайта"
    assert db_session.scalar(select(func.count(SiteServiceRequestCase.id))) == 1
