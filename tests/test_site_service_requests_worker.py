from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.site_service_requests import (
    SiteServiceRequestCase,
    SiteServiceRequestCommand,
    SiteServiceRequestEvent,
    SiteServiceRequestFile,
)
from app.schemas.site_service_requests import (
    SITE_SERVICE_REQUEST_REPLY_MAX_LENGTH,
    SiteServiceRequestEventPayload,
)
from app.services import site_service_requests_worker as worker_module
from app.services.site_service_requests import (
    SiteServiceRequestCipher,
    SiteServiceRequestConfigurationError,
    accept_site_service_request_event,
    build_site_service_request_health,
)
from app.services.site_service_requests_auth import content_sha256
from app.services.site_service_requests_worker import (
    SiteServiceRequestBitrixReader,
    SiteServiceRequestBitrixWriter,
    SiteServiceRequestFileCleanup,
    SiteServiceRequestFileDuplicateGuardError,
    SiteServiceRequestPermanentError,
    apply_site_service_request_worker_plans,
    build_site_service_request_worker_plans,
    choose_site_service_assignee,
    cleanup_uploaded_site_service_request_files,
    collect_site_service_request_outbound_commands,
    contains_exact_order_token,
    create_site_service_request_command,
    decide_site_service_assignment,
    next_site_service_request_retry_at,
    normalize_site_service_email,
    normalize_site_service_phone,
    preflight_site_service_request_users,
    reconcile_site_service_request_assignments,
    render_site_service_request_plans,
    sync_staged_site_service_request_files,
)

_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"w" * 32).decode("ascii")


class FakeBitrixApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[tuple[str, str]]]] = []
        self.phone_contacts: list[int] = [501]
        self.email_contacts: list[int] = []
        self.contacts: dict[int, dict] = {501: {"ID": "501", "ACTIVE": "Y", "COMPANY_ID": "601"}}
        self.exact_deals: list[dict] = [{"ID": "701", "TITLE": "Заказ 000001"}]
        self.fallback_deals: list[dict] = []
        self.timeman: dict[int, str] = {1001: "OPENED", 1002: "CLOSED"}
        self.next_contact_id = 900
        self.raise_after_contact_add = False
        self.next_item_id = 1000
        self.items: dict[int, dict] = {}
        self.raise_after_item_add = False
        self.disk_files: dict[str, dict] = {}
        self.next_crm_file_id = 3000
        self.crm_files: dict[int, bytes] = {}
        self.raise_after_crm_file_update = False
        self.timeline_comments: list[dict[str, str]] = []
        self.timeline_page_size: int | None = None
        self.notification_ids_by_tag: dict[str, int] = {}
        self.users: dict[int, dict] = {
            1001: {"ID": "1001", "ACTIVE": "Y", "NAME": "Анна", "LAST_NAME": "Гиря"},
            1002: {"ID": "1002", "ACTIVE": "Y", "NAME": "Ариф", "LAST_NAME": "Рахманов"},
            1003: {"ID": "1003", "ACTIVE": "Y", "NAME": "Тимур", "LAST_NAME": "Тибилов"},
            1004: {
                "ID": "1004",
                "ACTIVE": "Y",
                "NAME": "Александра",
                "LAST_NAME": "Живых",
            },
        }

    def call(self, method: str, params=None, **_kwargs):
        values = list(params or [])
        self.calls.append((method, values))
        mapped = dict(values)
        if method == "crm.duplicate.findbycomm":
            ids = self.phone_contacts if mapped.get("type") == "PHONE" else self.email_contacts
            return {"result": {"CONTACT": ids}}
        if method == "crm.contact.get":
            return {"result": self.contacts[int(mapped["id"])]}
        if method == "crm.contact.add":
            contact_id = self.next_contact_id
            self.next_contact_id += 1
            self.contacts[contact_id] = {
                "ID": str(contact_id),
                "ACTIVE": "Y",
                "ORIGINATOR_ID": mapped.get("fields[ORIGINATOR_ID]"),
                "ORIGIN_ID": mapped.get("fields[ORIGIN_ID]"),
            }
            if self.raise_after_contact_add:
                self.raise_after_contact_add = False
                raise RuntimeError("simulated timeout after contact add")
            return {"result": contact_id}
        if method == "crm.contact.list":
            originator_id = mapped.get("filter[=ORIGINATOR_ID]")
            origin_id = mapped.get("filter[=ORIGIN_ID]")
            rows = [
                {"ID": str(contact_id)}
                for contact_id, contact in self.contacts.items()
                if contact.get("ORIGINATOR_ID") == originator_id
                and contact.get("ORIGIN_ID") == origin_id
            ]
            return {"result": rows}
        if method == "crm.deal.list":
            is_exact = any(key.startswith("filter[=") for key, _value in values)
            return {"result": self.exact_deals if is_exact else self.fallback_deals}
        if method == "crm.item.list":
            filter_values = {
                key[len("filter[") : -1]: value
                for key, value in values
                if key.startswith("filter[")
            }
            items = [
                {"id": item_id, **fields}
                for item_id, fields in self.items.items()
                if all(str(fields.get(key)) == str(value) for key, value in filter_values.items())
            ]
            return {"result": {"items": items}}
        if method == "crm.item.add":
            item_id = self.next_item_id
            self.next_item_id += 1
            self.items[item_id] = _fields_from_params(values)
            if self.raise_after_item_add:
                self.raise_after_item_add = False
                raise RuntimeError("simulated timeout after add")
            return {"result": {"item": {"id": item_id}}}
        if method == "crm.item.update":
            item_id = int(mapped["id"])
            self.items[item_id].update(_fields_from_params(values))
            return {"result": {"item": {"id": item_id}}}
        if method == "crm.item.get":
            item_id = int(mapped["id"])
            rendered_item = dict(self.items[item_id])
            file_ids = rendered_item.get("ufCrm36Clientfiles")
            if isinstance(file_ids, list):
                rendered_item["ufCrm36Clientfiles"] = [
                    {
                        "id": str(file_id),
                        "urlMachine": ("https://fake.bitrix.local/rest/1/token/file/" f"{file_id}"),
                    }
                    for file_id in file_ids
                ]
            return {
                "result": {
                    "item": {
                        "id": item_id,
                        "ufSiteReplyAction": "",
                        **rendered_item,
                    }
                }
            }
        if method == "disk.folder.getchildren":
            name = mapped.get("filter[NAME]")
            item = self.disk_files.get(str(name))
            return {"result": [item] if item else []}
        if method == "crm.timeline.comment.add":
            self.timeline_comments.append(
                {
                    "ENTITY_TYPE": str(mapped["fields[ENTITY_TYPE]"]),
                    "ENTITY_ID": str(mapped["fields[ENTITY_ID]"]),
                    "COMMENT": str(mapped["fields[COMMENT]"]),
                }
            )
            return {"result": 1}
        if method == "crm.timeline.comment.list":
            matching = [
                row
                for row in reversed(self.timeline_comments)
                if row["ENTITY_TYPE"] == mapped["filter[ENTITY_TYPE]"]
                and row["ENTITY_ID"] == mapped["filter[ENTITY_ID]"]
            ]
            start = int(mapped.get("start") or 0)
            page_size = self.timeline_page_size or max(1, len(matching))
            response = {"result": matching[start : start + page_size]}
            if start + page_size < len(matching):
                response["next"] = start + page_size
            return response
        if method == "im.notify.personal.add":
            tag = mapped.get("TAG")
            assert isinstance(tag, str) and tag
            notification_id = self.notification_ids_by_tag.setdefault(
                tag,
                len(self.notification_ids_by_tag) + 1,
            )
            return {"result": notification_id}
        if method == "timeman.status":
            user_id = int(mapped["USER_ID"])
            return {"result": {"STATUS": self.timeman.get(user_id, "ERROR")}}
        if method == "user.get":
            user_id = int(mapped["ID"])
            user = self.users.get(user_id)
            return {"result": [user] if user else []}
        raise AssertionError(f"unexpected Bitrix method: {method}")

    def call_json(self, method: str, payload: dict, **_kwargs):
        if method == "disk.folder.uploadfile":
            name = str(payload["data"]["NAME"])
            item = {
                "ID": str(2000 + len(self.disk_files)),
                "NAME": name,
                "DETAIL_URL": "/disk/file",
            }
            self.disk_files[name] = item
            return {"result": item}
        if method == "crm.item.update":
            item_id = int(payload["id"])
            fields = payload["fields"]
            values = fields["ufCrm36Clientfiles"]
            current_ids = {
                int(file_id) for file_id in self.items[item_id].get("ufCrm36Clientfiles", [])
            }
            next_ids: list[str] = []
            for value in values:
                if isinstance(value, dict) and "ID" in value:
                    file_id = int(value["ID"])
                    if file_id in current_ids:
                        next_ids.append(str(file_id))
                    continue
                assert isinstance(value, list) and len(value) == 2
                content = base64.b64decode(value[1])
                file_id = self.next_crm_file_id
                self.next_crm_file_id += 1
                self.crm_files[file_id] = content
                next_ids.append(str(file_id))
            self.items[item_id]["ufCrm36Clientfiles"] = next_ids
            if self.raise_after_crm_file_update:
                self.raise_after_crm_file_update = False
                raise RuntimeError("simulated timeout after crm file update")
            return {"result": {"item": {"id": item_id}}}
        raise AssertionError(f"unexpected Bitrix JSON method: {method}")

    def download(self, url: str, *, max_bytes: int, **_kwargs) -> bytes:
        file_id = int(url.rstrip("/").rsplit("/", 1)[-1])
        content = self.crm_files[file_id]
        if len(content) > max_bytes:
            raise RuntimeError("fake file response too large")
        return content


def _fields_from_params(params: list[tuple[str, str]]) -> dict[str, str]:
    fields: dict[str, object] = {}
    for key, value in params:
        if not key.startswith("fields["):
            continue
        field = key[len("fields[") :].split("]", 1)[0]
        if key.endswith("[]"):
            fields.setdefault(field, [])
            assert isinstance(fields[field], list)
            fields[field].append(value)
        else:
            fields[field] = value
    return fields  # type: ignore[return-value]


def _event_payload() -> dict:
    return {
        "schemaVersion": 1,
        "eventId": "site-support:741:1201",
        "eventType": "ticket.created",
        "occurredAt": "2026-08-22T09:00:00+03:00",
        "ticket": {
            "id": 741,
            "siteId": "s1",
            "ownerUserId": 123,
            "title": "PRIVATE-TITLE",
            "phone": "+7 (900) 000-00-00",
            "email": "Private@Example.Invalid",
            "orderNumber": "000001",
            "requestType": "warranty",
            "isClosed": False,
        },
        "history": [
            {
                "messageId": 1201,
                "authorKind": "customer",
                "createdAt": "2026-08-22T09:00:00+03:00",
                "text": "PRIVATE-CUSTOMER-TEXT",
                "files": [],
            }
        ],
    }


def _case(**overrides) -> SiteServiceRequestCase:
    values = {
        "source_ticket_id": 741,
        "first_seen_at": datetime(2026, 8, 22, 6, 0, tzinfo=UTC),
        "assignment_state": "waiting",
        "round_robin_seq": 0,
        "sync_status": "pending",
    }
    values.update(overrides)
    return SiteServiceRequestCase(**values)


def _worker_settings(**overrides) -> Settings:
    values = {
        "site_service_requests_bitrix_writes_enabled": True,
        "site_service_requests_bitrix_entity_type_id": 1134,
        "site_service_requests_bitrix_working_category_id": 55,
        "site_service_requests_bitrix_field_map": {
            "site_ticket_id": "UF_SITE_TICKET_ID",
            "site_ticket_url": "UF_SITE_TICKET_URL",
            "site_history": "UF_SITE_HISTORY",
            "site_sync_status": "UF_SITE_SYNC_STATUS",
            "site_last_sync_at": "UF_SITE_LAST_SYNC_AT",
            "first_response_due_at": "UF_FIRST_RESPONSE_DUE_AT",
            "first_response_at": "UF_FIRST_RESPONSE_AT",
            "site_sync_error": "UF_SITE_SYNC_ERROR",
            "site_reply_text": "UF_SITE_REPLY_TEXT",
            "site_reply_action": "UF_SITE_REPLY_ACTION",
            "site_reply_status": "UF_SITE_REPLY_STATUS",
        },
        "site_service_requests_bitrix_stage_map": {
            "new": "DT1134_55:NEW",
            "success": "DT1134_55:SUCCESS",
            "failure": "DT1134_55:FAIL",
        },
        "site_service_requests_bitrix_enum_map": {
            "reply_action_send": "SEND",
            "reply_status_pending": "PENDING",
            "reply_status_sent": "SENT",
            "reply_status_error": "ERROR",
            "sync_status_synced": "SYNCED",
            "sync_status_client_match_required": "CLIENT_MATCH_REQUIRED",
            "sync_status_order_match_required": "ORDER_MATCH_REQUIRED",
            "sync_status_order_not_found": "ORDER_NOT_FOUND",
            "sync_status_file_sync_error": "FILE_SYNC_ERROR",
            "sync_status_assignment_waiting": "ASSIGNMENT_WAITING",
            "request_type_warranty": "WARRANTY",
            "request_type_refund_money": "REFUND_MONEY",
            "request_type_replacement": "REPLACEMENT",
            "request_type_delivery_return": "DELIVERY_RETURN",
            "request_type_consultation": "CONSULTATION",
            "request_type_other": "OTHER",
        },
        "site_service_requests_bitrix_root_folder_id": 777,
        "site_service_requests_crm_order_field": "UF_CRM_ORDER",
        "site_service_requests_first_line_user_ids": [1001, 1002],
        "site_service_requests_escalation_user_id": 1003,
        "site_service_requests_finance_user_id": 1004,
        "site_service_requests_expected_user_names": {
            "1001": "Анна Гиря",
            "1002": "Ариф Рахманов",
            "1003": "Тимур Тибилов",
            "1004": "Александра Живых",
        },
    }
    values.update(overrides)
    return Settings(**values)


def _persist_event(db_session) -> SiteServiceRequestCipher:
    payload_dict = _event_payload()
    raw_body = json.dumps(
        payload_dict,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    payload = SiteServiceRequestEventPayload.model_validate(payload_dict)
    cipher = SiteServiceRequestCipher(_ENCRYPTION_KEY)
    accept_site_service_request_event(
        db_session,
        payload=payload,
        raw_body=raw_body,
        payload_sha256=content_sha256(raw_body),
        cipher=cipher,
        max_file_bytes=10 * 1024 * 1024,
    )
    db_session.commit()
    return cipher


def _persist_payload(db_session, payload_dict: dict, cipher: SiteServiceRequestCipher) -> None:
    raw_body = json.dumps(
        payload_dict,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    accept_site_service_request_event(
        db_session,
        payload=SiteServiceRequestEventPayload.model_validate(payload_dict),
        raw_body=raw_body,
        payload_sha256=content_sha256(raw_body),
        cipher=cipher,
        max_file_bytes=10 * 1024 * 1024,
    )
    db_session.commit()


def test_normalization_and_exact_order_token() -> None:
    assert normalize_site_service_phone("8 (900) 123-45-67") == "79001234567"
    assert normalize_site_service_phone("9001234567") == "79001234567"
    assert normalize_site_service_phone("123") is None
    assert normalize_site_service_email(" User@Example.COM ") == "user@example.com"
    assert contains_exact_order_token("Заказ 000001 / сайт", "000001") is True
    assert contains_exact_order_token("Заказ 1000001 / сайт", "000001") is False


@pytest.mark.parametrize("value", [True, 1.5, "1.5", "+1", "١"])
def test_positive_int_rejects_noncanonical_rest_ids(value: object) -> None:
    assert worker_module._positive_int(value) is None


@pytest.mark.parametrize(("value", "expected"), [(1, 1), (" 42 ", 42)])
def test_positive_int_accepts_canonical_rest_ids(value: object, expected: int) -> None:
    assert worker_module._positive_int(value) == expected


def test_user_preflight_requires_active_users_with_expected_identity() -> None:
    api = FakeBitrixApi()
    settings = _worker_settings()

    result = preflight_site_service_request_users(api=api, settings=settings)

    assert [row["role"] for row in result] == [
        "first_line_1",
        "first_line_2",
        "escalation",
        "finance",
    ]
    api.users[1002]["ACTIVE"] = "N"
    with pytest.raises(RuntimeError, match="inactive"):
        preflight_site_service_request_users(api=api, settings=settings)

    mismatched_api = FakeBitrixApi()
    mismatched_api.users[1001]["ID"] = "9999"
    with pytest.raises(RuntimeError, match="readback failed"):
        preflight_site_service_request_users(api=mismatched_api, settings=settings)


def test_user_preflight_accepts_boolean_active_from_box() -> None:
    api = FakeBitrixApi()
    for user in api.users.values():
        user["ACTIVE"] = True

    result = preflight_site_service_request_users(api=api, settings=_worker_settings())

    assert len(result) == 4


@pytest.mark.parametrize("active", [1, 1.0, "true", " Y", "", None, [True]])
def test_user_preflight_rejects_malformed_active(active: object) -> None:
    api = FakeBitrixApi()
    api.users[1001]["ACTIVE"] = active

    with pytest.raises(
        SiteServiceRequestConfigurationError,
        match="pilot user readback failed",
    ):
        preflight_site_service_request_users(api=api, settings=_worker_settings())


def test_user_preflight_rejects_malformed_extra_user_row() -> None:
    class MalformedUserApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method == "user.get":
                response = super().call(method, params, **kwargs)
                return {"result": [*response["result"], "unknown-row"]}
            return super().call(method, params, **kwargs)

    with pytest.raises(RuntimeError, match="readback failed"):
        preflight_site_service_request_users(
            api=MalformedUserApi(),
            settings=_worker_settings(),
        )


@pytest.mark.parametrize(
    ("alias", "conflicting_value"),
    [
        ("id", "9999"),
        ("active", "N"),
        ("name", "Другая"),
        ("lastName", "Фамилия"),
    ],
)
def test_user_preflight_rejects_conflicting_user_aliases(
    alias: str,
    conflicting_value: str,
) -> None:
    api = FakeBitrixApi()
    api.users[1001][alias] = conflicting_value

    with pytest.raises(
        SiteServiceRequestConfigurationError,
        match="pilot user readback failed",
    ):
        preflight_site_service_request_users(api=api, settings=_worker_settings())


def test_contact_lookup_rejects_missing_contract_fields() -> None:
    class MalformedContactApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method == "crm.duplicate.findbycomm":
                return {"result": {}}
            return super().call(method, params, **kwargs)

    with pytest.raises(RuntimeError, match="bitrix_contact_search_invalid"):
        SiteServiceRequestBitrixReader(MalformedContactApi()).find_contact(
            phone="+79000000000",
            email=None,
        )

    missing_id = FakeBitrixApi()
    missing_id.contacts[501].pop("ID")
    with pytest.raises(RuntimeError, match="bitrix_contact_readback_failed"):
        SiteServiceRequestBitrixReader(missing_id).find_contact(
            phone="+79000000000",
            email=None,
        )


def test_contact_lookup_accepts_bitrix_contact_without_active_field() -> None:
    api = FakeBitrixApi()
    api.contacts[501].pop("ACTIVE")

    match = SiteServiceRequestBitrixReader(api).find_contact(
        phone="+79000000000",
        email=None,
    )

    assert match.status == "matched"
    assert match.contact_id == 501
    assert match.company_id == 601


@pytest.mark.parametrize(
    "malformed_company_id",
    [True, 601.5, "601.5", [601], {"id": 601}],
)
def test_contact_lookup_rejects_malformed_company_id(
    malformed_company_id: object,
) -> None:
    api = FakeBitrixApi()
    api.contacts[501]["COMPANY_ID"] = malformed_company_id

    with pytest.raises(RuntimeError, match="bitrix_contact_readback_failed"):
        SiteServiceRequestBitrixReader(api).find_contact(
            phone="+79000000000",
            email=None,
        )


def test_contact_lookup_rejects_conflicting_company_id_aliases() -> None:
    api = FakeBitrixApi()
    api.contacts[501]["companyId"] = "602"

    with pytest.raises(RuntimeError, match="bitrix_contact_readback_failed"):
        SiteServiceRequestBitrixReader(api).find_contact(
            phone="+79000000000",
            email=None,
        )


def test_contact_lookup_rejects_conflicting_id_aliases() -> None:
    api = FakeBitrixApi()
    api.contacts[501]["id"] = "502"

    with pytest.raises(RuntimeError, match="bitrix_contact_readback_failed"):
        SiteServiceRequestBitrixReader(api).find_contact(
            phone="+79000000000",
            email=None,
        )


def test_contact_and_order_matching_never_choose_ambiguous_candidate() -> None:
    api = FakeBitrixApi()
    reader = SiteServiceRequestBitrixReader(api)
    matched = reader.find_contact(phone="+79000000000", email="x@example.invalid")
    assert matched.contact_id == 501
    assert matched.company_id == 601

    exact = reader.find_order(
        contact_id=501,
        order_number="000001",
        order_field="UF_CRM_ORDER",
    )
    assert exact.status == "matched"
    assert exact.deal_id == 701

    api.phone_contacts = [501, 502]
    api.contacts[502] = {"ID": "502", "ACTIVE": "Y"}
    ambiguous = reader.find_contact(phone="+79000000000", email=None)
    assert ambiguous.status == "ambiguous"
    assert ambiguous.contact_id is None

    api.exact_deals = []
    api.fallback_deals = [
        {"ID": "801", "TITLE": "Заказ 1000001"},
        {"ID": "802", "TITLE": "Заказ 000001"},
    ]
    fallback = reader.find_order(
        contact_id=501,
        order_number="000001",
        order_field="UF_CRM_ORDER",
    )
    assert fallback.status == "matched"
    assert fallback.deal_id == 802


def test_contact_matching_intersects_phone_and_email_candidates() -> None:
    api = FakeBitrixApi()
    api.phone_contacts = [501, 502]
    api.email_contacts = [502]
    api.contacts[502] = {"ID": "502", "COMPANY_ID": "602"}
    reader = SiteServiceRequestBitrixReader(api)

    matched = reader.find_contact(
        phone="+79000000000",
        email="customer@example.invalid",
    )

    assert matched.status == "matched"
    assert matched.contact_id == 502
    assert matched.company_id == 602


def test_contact_matching_keeps_conflicting_phone_and_email_fail_closed() -> None:
    api = FakeBitrixApi()
    api.phone_contacts = [501]
    api.email_contacts = [502]
    api.contacts[502] = {"ID": "502"}
    reader = SiteServiceRequestBitrixReader(api)

    ambiguous = reader.find_contact(
        phone="+79000000000",
        email="customer@example.invalid",
    )

    assert ambiguous.status == "ambiguous"
    assert ambiguous.contact_id is None
    assert ambiguous.candidate_ids == (501, 502)


def test_unique_order_safely_disambiguates_duplicate_contacts() -> None:
    class ContactScopedOrderApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            values = list(params or [])
            if method == "crm.deal.list":
                mapped = dict(values)
                contact_id = int(mapped["filter[CONTACT_ID]"])
                exact = any(key.startswith("filter[=") for key, _value in values)
                if contact_id == 502:
                    return {"result": ([{"ID": "702", "TITLE": "Заказ 000001"}] if exact else [])}
                return {"result": []}
            return super().call(method, values, **kwargs)

    api = ContactScopedOrderApi()
    api.phone_contacts = [501, 502]
    api.contacts[502] = {"ID": "502", "COMPANY_ID": "602"}
    reader = SiteServiceRequestBitrixReader(api)

    ambiguous_contact = reader.find_contact(phone="+79000000000", email=None)
    contact, order = reader.resolve_contact_and_order(
        contact=ambiguous_contact,
        order_number="000001",
        order_field="UF_CRM_ORDER",
    )

    assert contact.status == "matched"
    assert contact.contact_id == 502
    assert contact.company_id == 602
    assert order.status == "matched"
    assert order.deal_id == 702


def test_order_disambiguation_remains_fail_closed_when_two_contacts_match() -> None:
    class TwoMatchingContactsApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            values = list(params or [])
            if method == "crm.deal.list":
                mapped = dict(values)
                contact_id = int(mapped["filter[CONTACT_ID]"])
                exact = any(key.startswith("filter[=") for key, _value in values)
                return {
                    "result": (
                        [{"ID": str(700 + contact_id), "TITLE": "Заказ 000001"}] if exact else []
                    )
                }
            return super().call(method, values, **kwargs)

    api = TwoMatchingContactsApi()
    api.phone_contacts = [501, 502]
    api.contacts[502] = {"ID": "502"}
    reader = SiteServiceRequestBitrixReader(api)

    ambiguous_contact = reader.find_contact(phone="+79000000000", email=None)
    contact, order = reader.resolve_contact_and_order(
        contact=ambiguous_contact,
        order_number="000001",
        order_field="UF_CRM_ORDER",
    )

    assert contact.status == "ambiguous"
    assert contact.contact_id is None
    assert order.status == "ambiguous"
    assert order.deal_id is None
    assert order.candidate_ids == (1201, 1202)


def test_order_fallback_deduplicates_same_deal_across_pages() -> None:
    class RepeatedDealApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method == "crm.deal.list":
                values = list(params or [])
                self.calls.append((method, values))
                if any(key.startswith("filter[=") for key, _value in values):
                    return {"result": []}
                start = int(dict(values).get("start") or 0)
                row = {"ID": "802", "TITLE": "Заказ 000001"}
                if start == 0:
                    return {"result": {"items": [row], "next": 50}}
                return {"result": {"items": [row]}}
            return super().call(method, params, **kwargs)

    match = SiteServiceRequestBitrixReader(RepeatedDealApi()).find_order(
        contact_id=501,
        order_number="000001",
        order_field="UF_CRM_ORDER",
    )

    assert match.status == "matched"
    assert match.candidate_ids == (802,)


def test_deal_pagination_cycle_fails_closed() -> None:
    class CyclicDealApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method == "crm.deal.list":
                self.calls.append((method, list(params or [])))
                return {"result": {"items": [], "next": 0}}
            return super().call(method, params, **kwargs)

    with pytest.raises(RuntimeError, match="bitrix_deal_pagination_cycle"):
        SiteServiceRequestBitrixReader(CyclicDealApi()).find_order(
            contact_id=501,
            order_number="000001",
            order_field="UF_CRM_ORDER",
        )


def test_deal_pagination_stops_after_100_pages() -> None:
    class EndlessDealApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method == "crm.deal.list":
                values = list(params or [])
                self.calls.append((method, values))
                start = int(dict(values).get("start") or 0)
                return {"result": {"items": [], "next": start + 1}}
            return super().call(method, params, **kwargs)

    api = EndlessDealApi()
    with pytest.raises(RuntimeError, match="bitrix_deal_pagination_invalid"):
        SiteServiceRequestBitrixReader(api).find_order(
            contact_id=501,
            order_number="000001",
            order_field="UF_CRM_ORDER",
        )
    assert len(api.calls) == 100


def test_deal_pagination_rejects_conflicting_offsets() -> None:
    class ConflictingNextApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method == "crm.deal.list":
                return {"result": {"items": [], "next": 1}, "next": 2}
            return super().call(method, params, **kwargs)

    with pytest.raises(RuntimeError, match="bitrix_deal_pagination_invalid"):
        SiteServiceRequestBitrixReader(ConflictingNextApi()).find_order(
            contact_id=501,
            order_number="000001",
            order_field="UF_CRM_ORDER",
        )


def test_bitrix_pagination_rejects_fractional_offsets() -> None:
    class FractionalNextApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method == "crm.deal.list":
                return {"result": [], "next": 1.5}
            if method == "crm.contact.list":
                return {"result": [], "next": 1.5}
            if method == "crm.timeline.comment.list":
                return {"result": [], "next": 1.5}
            if method == "crm.item.list":
                return {"result": {"items": []}, "next": 1.5}
            return super().call(method, params, **kwargs)

    api = FractionalNextApi()
    reader = SiteServiceRequestBitrixReader(api)
    writer = SiteServiceRequestBitrixWriter(api)

    with pytest.raises(RuntimeError, match="bitrix_deal_pagination_invalid"):
        reader.find_order(
            contact_id=501,
            order_number="000001",
            order_field="UF_CRM_ORDER",
        )
    with pytest.raises(RuntimeError, match="crm_contact_pagination_invalid"):
        writer._recover_created_contact(origin_id="site-support-ticket:741")
    with pytest.raises(RuntimeError, match="bitrix_timeline_pagination_invalid"):
        writer.timeline_comment_exists(
            entity_type_id=1134,
            item_id=1000,
            marker="missing marker",
        )
    with pytest.raises(RuntimeError, match="bitrix_item_pagination_invalid"):
        writer._find_items(
            entity_type_id=1134,
            idempotency_field="UF_BACKEND_KEY",
            idempotency_key="key",
        )


def test_deal_pagination_rejects_ambiguous_page_containers() -> None:
    class AmbiguousDealPageApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method == "crm.deal.list":
                return {"result": {"items": [], "deals": []}}
            return super().call(method, params, **kwargs)

    with pytest.raises(RuntimeError, match="bitrix_deal_readback_invalid"):
        SiteServiceRequestBitrixReader(AmbiguousDealPageApi()).find_order(
            contact_id=501,
            order_number="000001",
            order_field="UF_CRM_ORDER",
        )


@pytest.mark.parametrize(
    "malformed_title",
    [["Заказ 000001"], {"text": "Заказ 000001"}],
)
def test_deal_lookup_rejects_non_string_title(malformed_title: object) -> None:
    class MalformedDealTitleApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method == "crm.deal.list":
                return {"result": [{"ID": "701", "TITLE": malformed_title}]}
            return super().call(method, params, **kwargs)

    with pytest.raises(RuntimeError, match="bitrix_deal_readback_invalid"):
        SiteServiceRequestBitrixReader(MalformedDealTitleApi()).find_order(
            contact_id=501,
            order_number="000001",
            order_field="UF_CRM_ORDER",
        )


def test_deal_lookup_rejects_conflicting_id_aliases() -> None:
    class ConflictingDealIdApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method == "crm.deal.list":
                return {
                    "result": [
                        {
                            "ID": "701",
                            "id": "702",
                            "TITLE": "Заказ 000001",
                        }
                    ]
                }
            return super().call(method, params, **kwargs)

    with pytest.raises(RuntimeError, match="bitrix_deal_readback_invalid"):
        SiteServiceRequestBitrixReader(ConflictingDealIdApi()).find_order(
            contact_id=501,
            order_number="000001",
            order_field="UF_CRM_ORDER",
        )


def test_contact_create_timeout_recovers_by_origin_id() -> None:
    api = FakeBitrixApi()
    api.raise_after_contact_add = True
    payload = SiteServiceRequestEventPayload.model_validate(_event_payload())

    contact_id = SiteServiceRequestBitrixWriter(api).create_contact(payload)

    assert contact_id == 900
    methods = [method for method, _params in api.calls]
    assert methods.count("crm.contact.add") == 1
    assert methods.count("crm.contact.list") == 1


def test_contact_recovery_rejects_ambiguous_page_containers() -> None:
    class AmbiguousContactPageApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.raise_after_contact_add = True

        def call(self, method: str, params=None, **kwargs):
            if method == "crm.contact.list":
                return {"result": {"items": [], "contacts": []}}
            return super().call(method, params, **kwargs)

    with pytest.raises(RuntimeError, match="crm_contact_readback_invalid"):
        SiteServiceRequestBitrixWriter(AmbiguousContactPageApi()).create_contact(
            SiteServiceRequestEventPayload.model_validate(_event_payload())
        )


def test_contact_create_requires_origin_readback() -> None:
    class WrongOriginApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            response = super().call(method, params, **kwargs)
            if method == "crm.contact.get":
                response["result"]["ORIGIN_ID"] = "wrong-origin"
            return response

    with pytest.raises(RuntimeError, match="crm_contact_write_readback_failed"):
        SiteServiceRequestBitrixWriter(WrongOriginApi()).create_contact(
            SiteServiceRequestEventPayload.model_validate(_event_payload())
        )


def test_contact_create_rejects_conflicting_id_aliases() -> None:
    class ConflictingContactIdApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            response = super().call(method, params, **kwargs)
            if method == "crm.contact.get":
                response["result"]["id"] = "901"
            return response

    with pytest.raises(RuntimeError, match="crm_contact_write_readback_failed"):
        SiteServiceRequestBitrixWriter(ConflictingContactIdApi()).create_contact(
            SiteServiceRequestEventPayload.model_validate(_event_payload())
        )


def test_assignment_round_robin_outside_shift_pause_and_escalation() -> None:
    assert (
        choose_site_service_assignee(
            configured_user_ids=[1001, 1002],
            available_user_ids=[1001, 1002],
            last_assigned_user_id=1001,
        )
        == 1002
    )

    arrived_without_shift = _case()
    waiting = decide_site_service_assignment(
        case=arrived_without_shift,
        configured_user_ids=[1001, 1002],
        timeman_statuses={1001: "CLOSED", 1002: "PAUSED"},
        last_assigned_user_id=None,
        next_round_robin_seq=1,
        escalation_user_id=1003,
        first_response_hours=4,
        timezone_name="Europe/Moscow",
        now=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
    )
    assert waiting.state == "waiting"
    assert waiting.intake_mode == "outside_open_shift"
    assert waiting.first_response_due_at is None

    arrived_without_shift.intake_mode = waiting.intake_mode
    opened = decide_site_service_assignment(
        case=arrived_without_shift,
        configured_user_ids=[1001, 1002],
        timeman_statuses={1001: "OPENED", 1002: "CLOSED"},
        last_assigned_user_id=None,
        next_round_robin_seq=1,
        escalation_user_id=1003,
        first_response_hours=4,
        timezone_name="Europe/Moscow",
        now=datetime(2026, 8, 22, 8, 30, tzinfo=UTC),
    )
    assert opened.assigned_user_id == 1001
    assert opened.first_response_due_at == datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
    assert opened.state == "assigned"

    overdue = _case(
        assigned_user_id=1001,
        assignment_state="assigned",
        intake_mode="during_open_shift",
        first_response_due_at=datetime(2026, 8, 22, 8, 0, tzinfo=UTC),
    )
    escalated = decide_site_service_assignment(
        case=overdue,
        configured_user_ids=[1001, 1002],
        timeman_statuses={1001: "OPENED", 1002: "OPENED"},
        last_assigned_user_id=1001,
        next_round_robin_seq=2,
        escalation_user_id=1003,
        first_response_hours=4,
        timezone_name="Europe/Moscow",
        now=datetime(2026, 8, 22, 8, 1, tzinfo=UTC),
    )
    assert escalated.assigned_user_id == 1003
    assert escalated.state == "escalated"
    assert escalated.escalated_at == datetime(2026, 8, 22, 8, 1, tzinfo=UTC)

    paused = _case(
        assigned_user_id=1001,
        assignment_state="assigned",
        intake_mode="during_open_shift",
        first_response_due_at=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
        sla_paused_at=datetime(2026, 8, 22, 9, 0, tzinfo=UTC),
    )
    resumed = decide_site_service_assignment(
        case=paused,
        configured_user_ids=[1001, 1002],
        timeman_statuses={1001: "OPENED", 1002: "CLOSED"},
        last_assigned_user_id=1001,
        next_round_robin_seq=2,
        escalation_user_id=1003,
        first_response_hours=4,
        timezone_name="Europe/Moscow",
        now=datetime(2026, 8, 22, 10, 0, tzinfo=UTC),
    )
    assert resumed.sla_paused_at is None
    assert resumed.first_response_due_at == datetime(2026, 8, 22, 13, 0, tzinfo=UTC)


def test_retry_schedule_uses_required_backoff() -> None:
    now = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)
    assert next_site_service_request_retry_at(attempts=1, now=now) == now + timedelta(minutes=1)
    assert next_site_service_request_retry_at(attempts=5, now=now) == now + timedelta(minutes=30)
    assert next_site_service_request_retry_at(attempts=6, now=now) == now + timedelta(hours=1)


def test_worker_dry_run_builds_safe_plan_without_mutating_event(db_session) -> None:
    cipher = _persist_event(db_session)
    event = db_session.scalar(select(SiteServiceRequestEvent))
    assert event is not None
    encrypted_before = event.payload_encrypted

    plans = build_site_service_request_worker_plans(
        db_session,
        settings=Settings(
            site_service_requests_first_line_user_ids=[1001, 1002],
            site_service_requests_escalation_user_id=1003,
            site_service_requests_crm_order_field="UF_CRM_ORDER",
        ),
        reader=SiteServiceRequestBitrixReader(FakeBitrixApi()),
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
    )

    assert len(plans) == 1
    assert plans[0].contact_id == 501
    assert plans[0].deal_id == 701
    assert plans[0].assigned_user_id == 1001
    rendered = render_site_service_request_plans(plans)
    assert "PRIVATE-TITLE" not in rendered
    assert "PRIVATE-CUSTOMER-TEXT" not in rendered
    assert "+7 (900) 000-00-00" not in rendered
    db_session.refresh(event)
    assert event.status == "pending"
    assert event.payload_encrypted == encrypted_before


def test_worker_apply_resolves_duplicate_contact_by_unique_order(db_session) -> None:
    class ContactScopedOrderApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            values = list(params or [])
            if method == "crm.deal.list":
                mapped = dict(values)
                contact_id = int(mapped["filter[CONTACT_ID]"])
                exact = any(key.startswith("filter[=") for key, _value in values)
                if contact_id == 502:
                    return {"result": ([{"ID": "702", "TITLE": "Заказ 000001"}] if exact else [])}
                return {"result": []}
            return super().call(method, values, **kwargs)

    cipher = _persist_event(db_session)
    api = ContactScopedOrderApi()
    api.phone_contacts = [501, 502]
    api.contacts[502] = {"ID": "502", "COMPANY_ID": "602"}
    reader = SiteServiceRequestBitrixReader(api)
    settings = _worker_settings()
    now = datetime(2026, 8, 22, 7, 0, tzinfo=UTC)

    plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
        now=now,
    )
    results = apply_site_service_request_worker_plans(
        db_session,
        plans=plans,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
        now=now + timedelta(minutes=1),
    )

    case = db_session.scalar(select(SiteServiceRequestCase))
    assert plans[0].contact_status == "matched"
    assert plans[0].contact_id == 502
    assert plans[0].deal_id == 702
    assert results[0].status == "processed"
    assert case is not None
    assert case.crm_contact_id == 502
    assert case.crm_company_id == 602
    assert case.crm_deal_id == 702


def test_worker_apply_creates_one_item_and_recovers_add_timeout_by_readback(
    db_session,
) -> None:
    cipher = _persist_event(db_session)
    api = FakeBitrixApi()
    api.raise_after_item_add = True
    reader = SiteServiceRequestBitrixReader(api)
    settings = _worker_settings()
    plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
    )

    results = apply_site_service_request_worker_plans(
        db_session,
        plans=plans,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 1, tzinfo=UTC),
    )

    assert len(results) == 1
    assert results[0].status == "processed"
    assert len(api.items) == 1
    item_id, fields = next(iter(api.items.items()))
    assert fields["categoryId"] == "55"
    assert fields["stageId"] == "DT1134_55:NEW"
    assert fields["ufCrm36Idempotencykey"] == "site-support-ticket:741"
    assert fields["ufSiteTicketId"] == "741"
    assert "PRIVATE-CUSTOMER-TEXT" in fields["ufSiteHistory"]
    assert fields["ufSiteSyncError"] == ""
    assert "None" not in fields.values()

    event = db_session.scalar(select(SiteServiceRequestEvent))
    case = db_session.scalar(select(SiteServiceRequestCase))
    assert event is not None and event.status == "processed"
    assert event.payload_encrypted is None
    assert case is not None and case.bitrix_item_id == item_id
    assert case.crm_contact_id == 501
    assert case.crm_company_id == 601
    assert case.crm_deal_id == 701
    assert case.assigned_user_id == 1001


def test_worker_creates_service_contact_but_never_sales_lead(db_session) -> None:
    cipher = _persist_event(db_session)
    api = FakeBitrixApi()
    api.phone_contacts = []
    api.email_contacts = []
    reader = SiteServiceRequestBitrixReader(api)
    settings = _worker_settings()
    plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
    )
    apply_site_service_request_worker_plans(
        db_session,
        plans=plans,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 1, tzinfo=UTC),
    )

    methods = [method for method, _params in api.calls]
    assert methods.count("crm.contact.add") == 1
    assert "crm.lead.add" not in methods
    case = db_session.scalar(select(SiteServiceRequestCase))
    assert case is not None and case.crm_contact_id == 900


def test_worker_coalesces_same_case_per_batch_and_round_robins_distinct_cases(
    db_session,
) -> None:
    cipher = _persist_event(db_session)
    same_case = _event_payload()
    same_case["eventId"] = "site-support:741:1202"
    same_case["eventType"] = "ticket.message_added"
    same_case["history"].append(
        {
            "messageId": 1202,
            "authorKind": "customer",
            "createdAt": "2026-08-22T09:01:00+03:00",
            "text": "Повторное сообщение",
            "files": [],
        }
    )
    _persist_payload(db_session, same_case, cipher)
    other_case = _event_payload()
    other_case["eventId"] = "site-support:742:2201"
    other_case["ticket"]["id"] = 742
    other_case["history"][0]["messageId"] = 2201
    _persist_payload(db_session, other_case, cipher)

    api = FakeBitrixApi()
    api.phone_contacts = []
    api.email_contacts = []
    api.timeman = {1001: "OPENED", 1002: "OPENED"}
    reader = SiteServiceRequestBitrixReader(api)
    settings = _worker_settings()
    plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
    )

    assert [plan.ticket_id for plan in plans] == [741, 742]
    assert [plan.assigned_user_id for plan in plans] == [1001, 1002]
    apply_site_service_request_worker_plans(
        db_session,
        plans=plans,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 1, tzinfo=UTC),
    )
    remaining = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 2, tzinfo=UTC),
    )
    assert [plan.event_id for plan in remaining] == ["site-support:741:1202"]
    apply_site_service_request_worker_plans(
        db_session,
        plans=remaining,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 3, tzinfo=UTC),
    )

    methods = [method for method, _params in api.calls]
    assert methods.count("crm.contact.add") == 2
    assert methods.count("crm.item.add") == 2


def test_worker_does_not_overtake_deferred_retry_in_the_same_case(db_session) -> None:
    cipher = _persist_event(db_session)
    later = _event_payload()
    later["eventId"] = "site-support:741:1202"
    later["eventType"] = "ticket.message_added"
    later["history"].append(
        {
            "messageId": 1202,
            "authorKind": "customer",
            "createdAt": "2026-08-22T09:01:00+03:00",
            "text": "Более новое сообщение",
            "files": [],
        }
    )
    _persist_payload(db_session, later, cipher)
    events = db_session.scalars(
        select(SiteServiceRequestEvent).order_by(SiteServiceRequestEvent.id)
    ).all()
    events[0].status = "retry"
    events[0].next_retry_at = datetime(2026, 8, 22, 7, 5, tzinfo=UTC)
    db_session.commit()

    before_retry = build_site_service_request_worker_plans(
        db_session,
        settings=_worker_settings(),
        reader=SiteServiceRequestBitrixReader(FakeBitrixApi()),
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
    )
    after_retry = build_site_service_request_worker_plans(
        db_session,
        settings=_worker_settings(),
        reader=SiteServiceRequestBitrixReader(FakeBitrixApi()),
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 5, tzinfo=UTC),
    )

    assert before_retry == []
    assert [plan.event_id for plan in after_retry] == ["site-support:741:1201"]


def test_worker_orders_out_of_order_delivery_by_source_message(db_session) -> None:
    cipher = SiteServiceRequestCipher(_ENCRYPTION_KEY)
    later = _event_payload()
    later["eventId"] = "site-support:741:1202"
    later["eventType"] = "ticket.message_added"
    later["occurredAt"] = "2026-08-22T12:01:00+03:00"
    later["history"].append(
        {
            "messageId": 1202,
            "authorKind": "customer",
            "createdAt": "2026-08-22T12:01:00+03:00",
            "text": "Более новое сообщение",
            "files": [],
        }
    )
    _persist_payload(db_session, later, cipher)
    _persist_payload(db_session, _event_payload(), cipher)

    case = db_session.scalar(select(SiteServiceRequestCase))
    assert case is not None
    assert case.first_seen_at.replace(tzinfo=UTC) == datetime(2026, 8, 22, 6, 0, tzinfo=UTC)

    plans = build_site_service_request_worker_plans(
        db_session,
        settings=_worker_settings(),
        reader=SiteServiceRequestBitrixReader(FakeBitrixApi()),
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
    )

    assert [plan.event_id for plan in plans] == ["site-support:741:1201"]


def test_outbound_command_creation_encrypts_text_and_deduplicates(db_session) -> None:
    _persist_event(db_session)
    case = db_session.scalar(select(SiteServiceRequestCase))
    assert case is not None
    cipher = SiteServiceRequestCipher(_ENCRYPTION_KEY)

    first, first_duplicate = create_site_service_request_command(
        db_session,
        case=case,
        reply_text=" Ответ клиенту ",
        cipher=cipher,
    )
    second, second_duplicate = create_site_service_request_command(
        db_session,
        case=case,
        reply_text="Ответ клиенту",
        cipher=cipher,
    )

    assert first_duplicate is False
    assert second_duplicate is True
    assert first.id == second.id
    assert "Ответ клиенту".encode() not in first.reply_encrypted
    assert (
        cipher.decrypt(first.reply_encrypted, event_id=first.command_key).decode("utf-8")
        == "Ответ клиенту"
    )
    assert db_session.scalar(select(func.count(SiteServiceRequestCommand.id))) == 1


def test_outbound_command_recovers_from_concurrent_unique_insert(
    db_session,
    monkeypatch,
) -> None:
    cipher = _persist_event(db_session)
    case = db_session.scalar(select(SiteServiceRequestCase))
    assert case is not None
    existing, duplicate = create_site_service_request_command(
        db_session,
        case=case,
        reply_text="Параллельный ответ",
        cipher=cipher,
    )
    assert duplicate is False
    db_session.commit()

    original_scalar = db_session.scalar
    first_lookup = True

    def miss_first_command_lookup(statement, *args, **kwargs):
        nonlocal first_lookup
        if first_lookup and "site_service_request_command.command_key" in str(statement):
            first_lookup = False
            return None
        return original_scalar(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "scalar", miss_first_command_lookup)
    recovered, duplicate = create_site_service_request_command(
        db_session,
        case=case,
        reply_text="Параллельный ответ",
        cipher=cipher,
    )

    assert duplicate is True
    assert recovered.id == existing.id
    assert db_session.scalar(select(func.count(SiteServiceRequestCommand.id))) == 1


def test_staged_file_upload_requires_item_readback_before_local_cleanup(
    db_session,
    tmp_path,
) -> None:
    cipher = _persist_event(db_session)
    api = FakeBitrixApi()
    reader = SiteServiceRequestBitrixReader(api)
    settings = _worker_settings()
    plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
    )
    apply_site_service_request_worker_plans(
        db_session,
        plans=plans,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 1, tzinfo=UTC),
    )
    case = db_session.scalar(select(SiteServiceRequestCase))
    assert case is not None and case.bitrix_item_id is not None
    content = b"file-content"
    path = tmp_path / "staged.bin"
    path.write_bytes(content)
    file = SiteServiceRequestFile(
        case_id=case.id,
        source_message_id=1201,
        source_file_id=93287,
        safe_filename="photo.jpg",
        mime_type="image/jpeg",
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        status="staged",
        temporary_path=str(path),
    )
    db_session.add(file)
    case.base_sync_status = "order_not_found"
    case.base_error_code = "order_not_found"
    case.sync_status = "file_sync_error"
    case.last_error_code = "file_sync_error"
    db_session.commit()

    results = sync_staged_site_service_request_files(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cleanup_paths=(cleanup_paths := []),
    )
    assert path.exists() is True
    db_session.commit()
    cleanup_uploaded_site_service_request_files(db_session, cleanup_paths)
    db_session.commit()

    assert results[0]["status"] == "uploaded"
    db_session.refresh(file)
    assert file.status == "uploaded"
    assert file.bitrix_object_id == "2000"
    assert file.bitrix_file_id == "3000"
    assert file.temporary_path is None
    assert path.exists() is False
    assert api.items[int(case.bitrix_item_id)]["ufCrm36Clientfiles"] == ["3000"]
    assert api.crm_files[3000] == content
    assert case.sync_status == "order_not_found"
    assert case.last_error_code == "order_not_found"
    assert api.items[int(case.bitrix_item_id)]["ufSiteSyncStatus"] == "ORDER_NOT_FOUND"


def test_uploaded_file_cleanup_retries_after_unlink_failure(
    db_session,
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "uploaded.bin"
    path.write_bytes(b"uploaded")
    case = _case(bitrix_item_id=1000, base_sync_status="synced", sync_status="synced")
    file = SiteServiceRequestFile(
        case=case,
        source_message_id=1201,
        source_file_id=93287,
        safe_filename="photo.jpg",
        mime_type="image/jpeg",
        byte_size=8,
        sha256=hashlib.sha256(b"uploaded").hexdigest(),
        status="uploaded",
        bitrix_object_id="2000",
        bitrix_file_id="2000",
        temporary_path=str(path),
    )
    db_session.add_all([case, file])
    db_session.commit()
    original_unlink = Path.unlink

    def fail_unlink(self, *args, **kwargs):
        if self == path:
            raise OSError("simulated unlink failure")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    cleanup_uploaded_site_service_request_files(db_session, [])
    db_session.commit()

    db_session.refresh(file)
    assert path.exists() is True
    assert file.temporary_path == str(path)

    monkeypatch.setattr(Path, "unlink", original_unlink)
    cleanup_uploaded_site_service_request_files(db_session, [])
    db_session.commit()

    db_session.refresh(file)
    assert path.exists() is False
    assert file.temporary_path is None


def test_uploaded_file_cleanup_preserves_concurrently_restaged_payload(
    db_session,
    tmp_path,
) -> None:
    path = tmp_path / "restaged.bin"
    path.write_bytes(b"invalid")
    case = _case(
        bitrix_item_id=1000,
        base_sync_status="synced",
        sync_status="file_sync_error",
    )
    file = SiteServiceRequestFile(
        case=case,
        source_message_id=1201,
        source_file_id=93287,
        safe_filename="photo.jpg",
        mime_type="image/jpeg",
        byte_size=7,
        sha256=hashlib.sha256(b"invalid").hexdigest(),
        status="failed",
        last_error_code="file_payload_invalid",
        temporary_path=str(path),
    )
    db_session.add_all([case, file])
    db_session.commit()
    stale_cleanup_paths = [
        SiteServiceRequestFileCleanup(
            case_id=case.id,
            file_id=file.id,
            path=path,
        )
    ]

    path.write_bytes(b"recovered")
    file.status = "staged"
    file.last_error_code = None
    file.byte_size = 9
    file.sha256 = hashlib.sha256(b"recovered").hexdigest()
    db_session.commit()

    cleanup_uploaded_site_service_request_files(db_session, stale_cleanup_paths)
    db_session.commit()

    db_session.refresh(file)
    assert path.read_bytes() == b"recovered"
    assert file.status == "staged"
    assert file.temporary_path == str(path)


def test_staged_file_upload_rejects_conflicting_file_id_aliases(
    db_session,
    tmp_path,
) -> None:
    class ConflictingFileFieldApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            response = super().call(method, params, **kwargs)
            if method == "crm.item.get":
                response["result"]["item"]["ufCrm36Clientfiles"] = [{"id": "2000", "ID": "9999"}]
            return response

    content = b"file-content"
    path = tmp_path / "staged.bin"
    path.write_bytes(content)
    case = _case(bitrix_item_id=1000, base_sync_status="synced", sync_status="synced")
    file = SiteServiceRequestFile(
        case=case,
        source_message_id=1201,
        source_file_id=93287,
        safe_filename="photo.jpg",
        mime_type="image/jpeg",
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        status="staged",
        temporary_path=str(path),
    )
    db_session.add_all([case, file])
    db_session.commit()
    api = ConflictingFileFieldApi()
    api.items[1000] = {}
    cleanup_paths = []

    results = sync_staged_site_service_request_files(
        db_session,
        settings=_worker_settings(),
        writer=SiteServiceRequestBitrixWriter(api),
        cleanup_paths=cleanup_paths,
    )

    db_session.refresh(file)
    assert results[0]["status"] == "failed"
    assert file.status == "failed"
    assert file.bitrix_object_id is None
    assert file.temporary_path == str(path)
    assert cleanup_paths == []
    assert path.exists() is True


def test_staged_file_upload_preserves_every_existing_file_id(db_session, tmp_path) -> None:
    case = _case(bitrix_item_id=1000, base_sync_status="synced", sync_status="synced")
    existing_file = SiteServiceRequestFile(
        case=case,
        source_message_id=1101,
        source_file_id=9001,
        safe_filename="existing.jpg",
        mime_type="image/jpeg",
        byte_size=1,
        sha256=hashlib.sha256(b"x").hexdigest(),
        status="uploaded",
        bitrix_object_id="1999",
        bitrix_file_id="1999",
    )
    content = b"new-file"
    path = tmp_path / "new-file.bin"
    path.write_bytes(content)
    staged_file = SiteServiceRequestFile(
        case=case,
        source_message_id=1201,
        source_file_id=9002,
        safe_filename="new.jpg",
        mime_type="image/jpeg",
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        status="staged",
        temporary_path=str(path),
    )
    db_session.add_all([case, existing_file, staged_file])
    db_session.commit()
    api = FakeBitrixApi()
    api.items[1000] = {"ufCrm36Clientfiles": ["1999"]}
    api.crm_files[1999] = b"x"

    results = sync_staged_site_service_request_files(
        db_session,
        settings=_worker_settings(),
        writer=SiteServiceRequestBitrixWriter(api),
    )

    assert results[0]["status"] == "uploaded"
    assert api.items[1000]["ufCrm36Clientfiles"] == ["1999", "3000"]
    assert staged_file.bitrix_object_id == "2000"
    assert staged_file.bitrix_file_id == "3000"
    assert staged_file.bitrix_attach_baseline_file_ids == [1999]
    assert api.crm_files[3000] == content


def test_file_attach_marker_is_committed_before_crm_write(db_session, tmp_path) -> None:
    class CommitCheckingApi(FakeBitrixApi):
        def call_json(self, method: str, payload: dict, **kwargs):
            if method == "crm.item.update":
                with Session(db_session.get_bind()) as observer:
                    durable_file = observer.scalar(select(SiteServiceRequestFile))
                    assert durable_file is not None
                    assert durable_file.bitrix_attach_attempted_at is not None
                    assert durable_file.bitrix_attach_baseline_file_ids == []
            return super().call_json(method, payload, **kwargs)

    content = b"commit-before-attach"
    path = tmp_path / "commit-before-attach.bin"
    path.write_bytes(content)
    case = _case(bitrix_item_id=1000, base_sync_status="synced", sync_status="synced")
    file = SiteServiceRequestFile(
        case=case,
        source_message_id=1201,
        source_file_id=93287,
        safe_filename="photo.jpg",
        mime_type="image/jpeg",
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        status="staged",
        temporary_path=str(path),
    )
    db_session.add_all([case, file])
    db_session.commit()
    api = CommitCheckingApi()
    api.items[1000] = {"ufCrm36Clientfiles": []}

    result = sync_staged_site_service_request_files(
        db_session,
        settings=_worker_settings(),
        writer=SiteServiceRequestBitrixWriter(api),
    )

    db_session.refresh(file)
    assert result[0]["status"] == "uploaded"
    assert file.bitrix_attach_attempted_at is not None


def test_ambiguous_attach_retry_is_readback_only_and_does_not_duplicate(
    db_session,
    tmp_path,
) -> None:
    class AmbiguousAttachApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.crm_write_calls = 0
            self.fail_next_readback = False

        def call(self, method: str, params=None, **kwargs):
            if method == "crm.item.get" and self.fail_next_readback:
                self.fail_next_readback = False
                raise RuntimeError("simulated ambiguous readback")
            return super().call(method, params, **kwargs)

        def call_json(self, method: str, payload: dict, **kwargs):
            result = super().call_json(method, payload, **kwargs)
            if method == "crm.item.update":
                self.crm_write_calls += 1
                new_file_id = self.next_crm_file_id - 1
                self.crm_files[new_file_id] += b"-transformed-by-bitrix"
                self.fail_next_readback = True
                raise RuntimeError("simulated timeout after crm write")
            return result

    content = b"ambiguous-attach"
    path = tmp_path / "ambiguous-attach.bin"
    path.write_bytes(content)
    case = _case(bitrix_item_id=1000, base_sync_status="synced", sync_status="synced")
    file = SiteServiceRequestFile(
        case=case,
        source_message_id=1201,
        source_file_id=93287,
        safe_filename="photo.jpg",
        mime_type="image/jpeg",
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        status="staged",
        temporary_path=str(path),
    )
    db_session.add_all([case, file])
    db_session.commit()
    api = AmbiguousAttachApi()
    api.items[1000] = {"ufCrm36Clientfiles": []}
    settings = _worker_settings()

    first = sync_staged_site_service_request_files(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
    )
    db_session.commit()
    second = sync_staged_site_service_request_files(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
    )

    db_session.refresh(file)
    assert first[0]["errorCode"] == "file_duplicate_guard"
    assert second[0]["status"] == "uploaded"
    assert file.status == "uploaded"
    assert api.crm_write_calls == 1
    assert api.next_crm_file_id == 3001
    assert api.crm_files[3000] != content


def test_guarded_file_without_readback_match_never_writes_again(db_session, tmp_path) -> None:
    class CountingApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.crm_write_calls = 0

        def call_json(self, method: str, payload: dict, **kwargs):
            if method == "crm.item.update":
                self.crm_write_calls += 1
            return super().call_json(method, payload, **kwargs)

    content = b"guarded-without-match"
    path = tmp_path / "guarded-without-match.bin"
    path.write_bytes(content)
    marker = datetime.now(UTC)
    case = _case(bitrix_item_id=1000, base_sync_status="synced", sync_status="synced")
    file = SiteServiceRequestFile(
        case=case,
        source_message_id=1201,
        source_file_id=93287,
        safe_filename="photo.jpg",
        mime_type="image/jpeg",
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        status="failed",
        temporary_path=str(path),
        bitrix_attach_attempted_at=marker,
        bitrix_attach_baseline_file_ids=[],
        last_error_code="file_duplicate_guard",
    )
    db_session.add_all([case, file])
    db_session.commit()
    api = CountingApi()
    api.items[1000] = {"ufCrm36Clientfiles": []}
    settings = _worker_settings()

    first = sync_staged_site_service_request_files(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
    )
    db_session.commit()
    second = sync_staged_site_service_request_files(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
    )

    assert first[0]["errorCode"] == "file_duplicate_guard"
    assert second[0]["errorCode"] == "file_duplicate_guard"
    assert api.crm_write_calls == 0
    assert file.temporary_path == str(path)
    assert path.exists() is True


def test_transformed_crm_file_is_confirmed_by_persisted_id_delta(
    db_session,
    tmp_path,
) -> None:
    class TransformingApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.crm_write_calls = 0

        def call_json(self, method: str, payload: dict, **kwargs):
            result = super().call_json(method, payload, **kwargs)
            if method == "crm.item.update":
                self.crm_write_calls += 1
                new_file_id = self.next_crm_file_id - 1
                self.crm_files[new_file_id] += b"-transformed-by-bitrix"
            return result

    content = b"jpeg-source-content"
    path = tmp_path / "transformed.jpg"
    path.write_bytes(content)
    case = _case(bitrix_item_id=1000, base_sync_status="synced", sync_status="synced")
    file = SiteServiceRequestFile(
        case=case,
        source_message_id=1201,
        source_file_id=93287,
        safe_filename="photo.jpg",
        mime_type="image/jpeg",
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        status="staged",
        temporary_path=str(path),
    )
    db_session.add_all([case, file])
    db_session.commit()
    api = TransformingApi()
    api.items[1000] = {"ufCrm36Clientfiles": []}

    first = sync_staged_site_service_request_files(
        db_session,
        settings=_worker_settings(),
        writer=SiteServiceRequestBitrixWriter(api),
    )
    db_session.commit()
    second = sync_staged_site_service_request_files(
        db_session,
        settings=_worker_settings(),
        writer=SiteServiceRequestBitrixWriter(api),
    )

    db_session.refresh(file)
    assert first[0]["status"] == "uploaded"
    assert second == []
    assert file.status == "uploaded"
    assert file.bitrix_file_id == "3000"
    assert file.bitrix_attach_baseline_file_ids == []
    assert api.crm_files[3000] != content
    assert api.crm_write_calls == 1
    assert api.next_crm_file_id == 3001


def test_file_count_guard_fails_before_download_or_crm_write(db_session, tmp_path) -> None:
    class GuardCountingApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.download_calls = 0
            self.crm_write_calls = 0
            self.disk_upload_calls = 0

        def call_json(self, method: str, payload: dict, **kwargs):
            if method == "disk.folder.uploadfile":
                self.disk_upload_calls += 1
            if method == "crm.item.update":
                self.crm_write_calls += 1
            return super().call_json(method, payload, **kwargs)

        def download(self, url: str, *, max_bytes: int, **kwargs) -> bytes:
            self.download_calls += 1
            return super().download(url, max_bytes=max_bytes, **kwargs)

    content = b"over-limit"
    path = tmp_path / "over-limit.bin"
    path.write_bytes(content)
    case = _case(bitrix_item_id=1000, base_sync_status="synced", sync_status="synced")
    file = SiteServiceRequestFile(
        case=case,
        source_message_id=1201,
        source_file_id=93287,
        safe_filename="photo.jpg",
        mime_type="image/jpeg",
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        status="staged",
        temporary_path=str(path),
    )
    db_session.add_all([case, file])
    db_session.commit()
    api = GuardCountingApi()
    existing_ids = [str(file_id) for file_id in range(1, 52)]
    api.items[1000] = {"ufCrm36Clientfiles": existing_ids}
    api.crm_files.update({file_id: b"existing" for file_id in range(1, 52)})

    result = sync_staged_site_service_request_files(
        db_session,
        settings=_worker_settings(site_service_requests_max_crm_files_per_item=50),
        writer=SiteServiceRequestBitrixWriter(api),
    )

    db_session.refresh(file)
    assert result[0]["errorCode"] == "file_duplicate_guard"
    assert file.status == "failed"
    assert file.bitrix_attach_attempted_at is None
    assert api.download_calls == 0
    assert api.disk_upload_calls == 0
    assert api.crm_write_calls == 0
    assert path.exists() is True


def test_file_content_attach_recovers_timeout_and_reuses_hash() -> None:
    api = FakeBitrixApi()
    api.items[1000] = {"ufCrm36Clientfiles": []}
    api.raise_after_crm_file_update = True
    writer = SiteServiceRequestBitrixWriter(api)
    content = b"file-content"
    expected_sha256 = hashlib.sha256(content).hexdigest()

    first_file_id, first_item = writer.attach_file_content(
        entity_type_id=1134,
        item_id=1000,
        field_name="UF_CRM_36_CLIENTFILES",
        deterministic_name="ticket-744-file.bin",
        content=content,
        expected_sha256=expected_sha256,
        max_bytes=1024,
    )
    second_file_id, second_item = writer.attach_file_content(
        entity_type_id=1134,
        item_id=1000,
        field_name="UF_CRM_36_CLIENTFILES",
        deterministic_name="ticket-744-file.bin",
        content=content,
        expected_sha256=expected_sha256,
        max_bytes=1024,
    )

    assert first_file_id == second_file_id == "3000"
    assert api.next_crm_file_id == 3001
    assert worker_module._item_field_contains(first_item, "UF_CRM_36_CLIENTFILES", "3000")
    assert worker_module._item_field_contains(second_item, "UF_CRM_36_CLIENTFILES", "3000")


@pytest.mark.parametrize("baseline", [["1"], [1, 1], {"id": 1}])
def test_file_content_attach_rejects_malformed_persisted_baseline(baseline) -> None:
    api = FakeBitrixApi()
    api.items[1000] = {"ufCrm36Clientfiles": []}
    writer = SiteServiceRequestBitrixWriter(api)
    content = b"file-content"

    with pytest.raises(
        SiteServiceRequestFileDuplicateGuardError,
        match="file_duplicate_guard",
    ):
        writer.attach_file_content(
            entity_type_id=1134,
            item_id=1000,
            field_name="UF_CRM_36_CLIENTFILES",
            deterministic_name="ticket-file.bin",
            content=content,
            expected_sha256=hashlib.sha256(content).hexdigest(),
            max_bytes=1024,
            baseline_file_ids=baseline,
            allow_write=False,
        )

    assert api.next_crm_file_id == 3000


def test_file_content_attach_rejects_multiple_ids_after_persisted_baseline() -> None:
    api = FakeBitrixApi()
    api.items[1000] = {"ufCrm36Clientfiles": [3000, 3001]}
    api.crm_files.update({3000: b"first", 3001: b"second"})
    writer = SiteServiceRequestBitrixWriter(api)
    content = b"file-content"

    with pytest.raises(
        SiteServiceRequestFileDuplicateGuardError,
        match="file_duplicate_guard",
    ):
        writer.attach_file_content(
            entity_type_id=1134,
            item_id=1000,
            field_name="UF_CRM_36_CLIENTFILES",
            deterministic_name="ticket-file.bin",
            content=content,
            expected_sha256=hashlib.sha256(content).hexdigest(),
            max_bytes=1024,
            baseline_file_ids=[],
            allow_write=False,
        )

    assert api.next_crm_file_id == 3000


def test_file_content_attach_prefers_machine_url_when_browser_url_differs() -> None:
    class DualUrlBitrixApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            response = super().call(method, params, **kwargs)
            if method == "crm.item.get":
                files = response["result"]["item"].get("ufCrm36Clientfiles") or []
                for file in files:
                    file["url"] = "https://fake.bitrix.local/bitrix/services/main/ajax.php"
            return response

    api = DualUrlBitrixApi()
    api.items[1000] = {"ufCrm36Clientfiles": [2999]}
    api.crm_files[2999] = b"file-content"
    writer = SiteServiceRequestBitrixWriter(api)
    content = b"file-content"

    file_id, item = writer.attach_file_content(
        entity_type_id=1134,
        item_id=1000,
        field_name="UF_CRM_36_CLIENTFILES",
        deterministic_name="ticket-file.bin",
        content=content,
        expected_sha256=hashlib.sha256(content).hexdigest(),
        max_bytes=1024,
    )

    assert file_id == "2999"
    assert item["ufCrm36Clientfiles"][0]["urlMachine"].endswith("/2999")
    assert api.next_crm_file_id == 3000


def test_terminal_file_error_is_delivered_once_after_event_processing(db_session) -> None:
    case = _case(
        bitrix_item_id=1000,
        base_sync_status="synced",
        sync_status="file_sync_error",
    )
    case.last_error_code = "file_unavailable"
    file = SiteServiceRequestFile(
        case=case,
        source_message_id=1201,
        source_file_id=93287,
        safe_filename="attachment-93287.bin",
        mime_type="application/octet-stream",
        byte_size=0,
        sha256="0" * 64,
        status="failed",
        last_error_code="file_unavailable",
    )
    db_session.add_all([case, file])
    db_session.commit()
    api = FakeBitrixApi()
    api.items[1000] = {}
    settings = _worker_settings()

    first = sync_staged_site_service_request_files(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
    )
    second = sync_staged_site_service_request_files(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
    )

    assert first[0]["errorReported"] is True
    assert second == []
    db_session.refresh(file)
    assert file.bitrix_error_reported_at is not None
    assert api.items[1000]["ufSiteSyncStatus"] == "FILE_SYNC_ERROR"
    assert api.items[1000]["ufSiteSyncError"] == "file_sync_error"


def test_event_file_error_preserves_underlying_base_status_for_recovery(db_session) -> None:
    cipher = _persist_event(db_session)
    case = db_session.scalar(select(SiteServiceRequestCase))
    assert case is not None
    file = SiteServiceRequestFile(
        case=case,
        source_message_id=1201,
        source_file_id=93287,
        safe_filename="attachment-93287.bin",
        mime_type="application/octet-stream",
        byte_size=0,
        sha256="0" * 64,
        status="failed",
        last_error_code="file_unavailable",
    )
    db_session.add(file)
    db_session.commit()
    api = FakeBitrixApi()
    reader = SiteServiceRequestBitrixReader(api)
    settings = _worker_settings()
    plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
    )

    apply_site_service_request_worker_plans(
        db_session,
        plans=plans,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 1, tzinfo=UTC),
    )

    db_session.refresh(case)
    assert case.sync_status == "file_sync_error"
    assert case.last_error_code == "file_sync_error"
    assert case.base_sync_status == "synced"
    assert case.base_error_code is None


def test_outbound_poll_creates_one_command_and_updates_pending_status(db_session) -> None:
    cipher = _persist_event(db_session)
    api = FakeBitrixApi()
    reader = SiteServiceRequestBitrixReader(api)
    settings = _worker_settings(site_service_requests_outbound_replies_enabled=True)
    plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
    )
    apply_site_service_request_worker_plans(
        db_session,
        plans=plans,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 1, tzinfo=UTC),
    )
    case = db_session.scalar(select(SiteServiceRequestCase))
    assert case is not None and case.bitrix_item_id is not None
    case.sync_status = "file_sync_error"
    case.last_error_code = "file_sync_error"
    db_session.commit()
    api.items[int(case.bitrix_item_id)].update(
        {
            "ufSiteReplyAction": "SEND",
            "ufSiteReplyText": "Ответ из карточки",
            "ufSiteSyncError": "file_sync_error",
        }
    )

    first = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    second = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )

    assert first[0]["duplicate"] is False
    assert second == []
    assert db_session.scalar(select(func.count(SiteServiceRequestCommand.id))) == 1
    assert api.items[int(case.bitrix_item_id)]["ufSiteReplyStatus"] == "PENDING"
    assert api.items[int(case.bitrix_item_id)]["ufSiteReplyAction"] == ""
    assert api.items[int(case.bitrix_item_id)]["ufSiteSyncError"] == "file_sync_error"

    command = db_session.scalar(select(SiteServiceRequestCommand))
    assert command is not None
    api.items[int(case.bitrix_item_id)]["ufSiteReplyAction"] = "SEND"
    pending_duplicate = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    assert pending_duplicate[0]["duplicate"] is True
    assert pending_duplicate[0]["status"] == "pending"
    assert db_session.scalar(select(func.count(SiteServiceRequestCommand.id))) == 1

    command.status = "applied"
    api.items[int(case.bitrix_item_id)]["ufSiteReplyAction"] = "SEND"
    db_session.commit()
    repeated_applied_text = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    commands = db_session.scalars(
        select(SiteServiceRequestCommand).order_by(SiteServiceRequestCommand.id)
    ).all()
    assert repeated_applied_text[0]["duplicate"] is False
    assert repeated_applied_text[0]["status"] == "pending"
    assert len(commands) == 2
    assert commands[0].status == "applied"
    assert commands[1].status == "pending"
    assert api.items[int(case.bitrix_item_id)]["ufSiteReplyStatus"] == "PENDING"
    assert api.items[int(case.bitrix_item_id)]["ufSiteSyncError"] == "file_sync_error"

    repeated_command = commands[1]
    repeated_command.status = "failed"
    repeated_command.last_error_code = "message_write_failed"
    db_session.commit()
    failed = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    assert failed[0]["status"] == "failed"
    assert api.items[int(case.bitrix_item_id)]["ufSiteReplyStatus"] == "ERROR"
    assert api.items[int(case.bitrix_item_id)]["ufSiteSyncError"] == "message_write_failed"

    api.items[int(case.bitrix_item_id)]["ufSiteReplyAction"] = "SEND"
    retried_failed_text = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    commands = db_session.scalars(
        select(SiteServiceRequestCommand).order_by(SiteServiceRequestCommand.id)
    ).all()
    assert retried_failed_text[0]["duplicate"] is False
    assert retried_failed_text[0]["status"] == "pending"
    assert len(commands) == 3
    assert api.items[int(case.bitrix_item_id)]["ufSiteReplyStatus"] == "PENDING"
    assert api.items[int(case.bitrix_item_id)]["ufSiteSyncError"] == "file_sync_error"


@pytest.mark.parametrize("malformed_reply", [[], {"text": "Ответ"}])
def test_outbound_rejects_non_string_reply_text(db_session, malformed_reply: object) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()
    api = FakeBitrixApi()
    api.items[1000] = {
        "ufSiteReplyAction": "SEND",
        "ufSiteReplyText": malformed_reply,
    }

    results = collect_site_service_request_outbound_commands(
        db_session,
        settings=_worker_settings(site_service_requests_outbound_replies_enabled=True),
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )

    db_session.refresh(case)
    assert results[0]["errorCode"] == "outbound_reconcile_failed"
    assert db_session.scalar(select(func.count(SiteServiceRequestCommand.id))) == 0
    assert api.items[1000]["ufSiteReplyAction"] == "SEND"
    assert api.items[1000]["ufSiteReplyText"] == malformed_reply
    assert case.outbound_last_error_code == "outbound_reconcile_failed"


@pytest.mark.parametrize("malformed_action", [True, ["SEND"], {"value": "SEND"}])
def test_outbound_rejects_non_scalar_reply_action(
    db_session,
    malformed_action: object,
) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()
    api = FakeBitrixApi()
    api.items[1000] = {
        "ufSiteReplyAction": malformed_action,
        "ufSiteReplyText": "Ответ не должен быть отправлен",
    }

    results = collect_site_service_request_outbound_commands(
        db_session,
        settings=_worker_settings(site_service_requests_outbound_replies_enabled=True),
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )

    db_session.refresh(case)
    assert results[0]["errorCode"] == "outbound_reconcile_failed"
    assert db_session.scalar(select(func.count(SiteServiceRequestCommand.id))) == 0
    assert api.items[1000]["ufSiteReplyAction"] == malformed_action
    assert case.outbound_last_error_code == "outbound_reconcile_failed"


def test_outbound_rejects_conflicting_item_field_aliases(db_session) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()
    api = FakeBitrixApi()
    api.items[1000] = {
        "UF_SITE_REPLY_ACTION": "",
        "ufSiteReplyAction": "SEND",
        "ufSiteReplyText": "Ответ не должен быть потерян",
    }

    results = collect_site_service_request_outbound_commands(
        db_session,
        settings=_worker_settings(site_service_requests_outbound_replies_enabled=True),
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )

    db_session.refresh(case)
    assert results[0]["errorCode"] == "outbound_reconcile_failed"
    assert db_session.scalar(select(func.count(SiteServiceRequestCommand.id))) == 0
    assert api.items[1000]["ufSiteReplyAction"] == "SEND"
    assert case.outbound_last_error_code == "outbound_reconcile_failed"


def test_outbound_empty_send_sets_explicit_error_until_text_is_fixed(db_session) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()
    api = FakeBitrixApi()
    api.items[1000] = {
        "ufSiteReplyAction": "SEND",
        "ufSiteReplyText": "   ",
    }
    settings = _worker_settings(
        site_service_requests_ingest_enabled=True,
        site_service_requests_outbound_replies_enabled=True,
    )

    failed = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )

    db_session.refresh(case)
    degraded = build_site_service_request_health(db_session, settings=settings)
    assert failed[0]["status"] == "failed"
    assert failed[0]["errorCode"] == "reply_text_empty"
    assert db_session.scalar(select(func.count(SiteServiceRequestCommand.id))) == 0
    assert api.items[1000]["ufSiteReplyAction"] == "SEND"
    assert api.items[1000]["ufSiteReplyStatus"] == "ERROR"
    assert api.items[1000]["ufSiteSyncError"] == "reply_text_empty"
    assert case.outbound_last_error_code == "reply_text_empty"
    assert degraded["status"] == "degraded"
    assert degraded["alert_codes"] == ["outbound_failure"]

    api.items[1000]["ufSiteReplyAction"] = ""
    cancelled = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )
    db_session.refresh(case)
    still_degraded = build_site_service_request_health(db_session, settings=settings)
    assert cancelled == []
    assert case.outbound_last_error_code == "reply_text_empty"
    assert api.items[1000]["ufSiteReplyStatus"] == "ERROR"
    assert api.items[1000]["ufSiteSyncError"] == "reply_text_empty"
    assert still_degraded["status"] == "degraded"

    api.items[1000]["ufSiteReplyAction"] = "SEND"
    api.items[1000]["ufSiteReplyText"] = "Исправленный ответ"
    retried = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )

    db_session.refresh(case)
    assert retried[0]["status"] == "pending"
    assert db_session.scalar(select(func.count(SiteServiceRequestCommand.id))) == 1
    assert api.items[1000]["ufSiteReplyAction"] == ""
    assert api.items[1000]["ufSiteReplyStatus"] == "PENDING"
    assert case.outbound_last_error_code is None


def test_outbound_oversized_send_sets_explicit_error_until_text_is_fixed(
    db_session,
) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()
    api = FakeBitrixApi()
    api.items[1000] = {
        "ufSiteReplyAction": "SEND",
        "ufSiteReplyText": "x" * (SITE_SERVICE_REQUEST_REPLY_MAX_LENGTH + 1),
    }
    settings = _worker_settings(
        site_service_requests_ingest_enabled=True,
        site_service_requests_outbound_replies_enabled=True,
    )

    failed = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )

    db_session.refresh(case)
    assert failed[0]["status"] == "failed"
    assert failed[0]["errorCode"] == "reply_text_too_long"
    assert db_session.scalar(select(func.count(SiteServiceRequestCommand.id))) == 0
    assert api.items[1000]["ufSiteReplyAction"] == "SEND"
    assert api.items[1000]["ufSiteReplyStatus"] == "ERROR"
    assert api.items[1000]["ufSiteSyncError"] == "reply_text_too_long"
    assert case.outbound_last_error_code == "reply_text_too_long"

    api.items[1000]["ufSiteReplyText"] = "Исправленный короткий ответ"
    retried = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )

    db_session.refresh(case)
    assert retried[0]["status"] == "pending"
    assert db_session.scalar(select(func.count(SiteServiceRequestCommand.id))) == 1
    assert api.items[1000]["ufSiteReplyAction"] == ""
    assert api.items[1000]["ufSiteReplyStatus"] == "PENDING"
    assert case.outbound_last_error_code is None


def test_command_factory_rejects_oversized_reply(db_session) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.flush()

    with pytest.raises(SiteServiceRequestPermanentError, match="reply_text_too_long"):
        create_site_service_request_command(
            db_session,
            case=case,
            reply_text="x" * (SITE_SERVICE_REQUEST_REPLY_MAX_LENGTH + 1),
            cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
        )

    assert db_session.scalar(select(func.count(SiteServiceRequestCommand.id))) == 0


def test_outbound_empty_send_error_survives_cancel_with_terminal_history(
    db_session,
) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.flush()
    previous, _duplicate = create_site_service_request_command(
        db_session,
        case=case,
        reply_text="Предыдущий ответ",
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )
    previous.status = "applied"
    previous.card_action_cleared_at = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
    db_session.commit()
    api = FakeBitrixApi()
    api.items[1000] = {
        "ufSiteReplyAction": "SEND",
        "ufSiteReplyText": "",
        "ufSiteReplyStatus": "SENT",
    }
    settings = _worker_settings(site_service_requests_outbound_replies_enabled=True)

    failed = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )
    api.items[1000]["ufSiteReplyAction"] = ""
    cancelled = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )

    db_session.refresh(case)
    assert failed[0]["errorCode"] == "reply_text_empty"
    assert cancelled == []
    assert case.outbound_last_error_code == "reply_text_empty"
    assert api.items[1000]["ufSiteReplyStatus"] == "ERROR"


def test_outbound_empty_send_concurrent_cancel_preserves_durable_error(
    db_session,
) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()

    class ConcurrentCancelApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.items[1000] = {
                "ufSiteReplyAction": "SEND",
                "ufSiteReplyText": "",
            }
            self.cancelled = False

        def call(self, method: str, params=None, **kwargs):
            mapped = dict(params or [])
            if (
                method == "crm.item.update"
                and mapped.get("fields[ufSiteReplyStatus]") == "ERROR"
                and not self.cancelled
            ):
                self.cancelled = True
                self.items[1000]["ufSiteReplyAction"] = ""
            return super().call(method, params, **kwargs)

    api = ConcurrentCancelApi()
    settings = _worker_settings(
        site_service_requests_ingest_enabled=True,
        site_service_requests_outbound_replies_enabled=True,
    )

    failed = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )
    repeated = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )

    db_session.refresh(case)
    health = build_site_service_request_health(db_session, settings=settings)
    assert failed[0]["errorCode"] == "reply_text_empty"
    assert repeated == []
    assert case.outbound_last_error_code == "reply_text_empty"
    assert api.items[1000]["ufSiteReplyAction"] == ""
    assert api.items[1000]["ufSiteReplyStatus"] == "ERROR"
    assert api.items[1000]["ufSiteSyncError"] == "reply_text_empty"
    assert health["status"] == "degraded"
    assert health["alert_codes"] == ["outbound_failure"]


def test_outbound_success_checkpoint_never_moves_backward(db_session) -> None:
    newer_time = datetime(2026, 8, 23, 9, 0, tzinfo=UTC)
    case = _case(bitrix_item_id=1000, outbound_checked_at=newer_time)
    case.outbound_last_error_code = "newer_outbound_failure"
    db_session.add(case)
    db_session.commit()
    api = FakeBitrixApi()
    api.items[1000] = {"ufSiteReplyAction": ""}

    results = collect_site_service_request_outbound_commands(
        db_session,
        settings=_worker_settings(site_service_requests_outbound_replies_enabled=True),
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
        now=datetime(2026, 8, 23, 8, 0, tzinfo=UTC),
    )

    db_session.refresh(case)
    assert results == []
    assert case.outbound_checked_at is not None
    assert case.outbound_checked_at.replace(tzinfo=UTC) == newer_time
    assert case.outbound_last_error_code == "newer_outbound_failure"
    assert (
        build_site_service_request_health(
            db_session,
            settings=_worker_settings(site_service_requests_outbound_replies_enabled=True),
        )["status"]
        == "degraded"
    )


def test_outbound_command_is_durable_before_card_action_clear(db_session) -> None:
    cipher = SiteServiceRequestCipher(_ENCRYPTION_KEY)
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()

    class FailingClearApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.items[1000] = {
                "ufSiteReplyAction": "SEND",
                "ufSiteReplyText": "Ответ из карточки",
            }
            self.fail_clear = True

        def call(self, method: str, params=None, **kwargs):
            if method == "crm.item.update" and self.fail_clear:
                self.fail_clear = False
                raise RuntimeError("temporary clear failure")
            return super().call(method, params, **kwargs)

    api = FailingClearApi()
    settings = _worker_settings(site_service_requests_outbound_replies_enabled=True)
    failed = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )

    command = db_session.scalar(select(SiteServiceRequestCommand))
    assert failed[0]["status"] == "retry"
    assert command is not None
    assert command.card_action_cleared_at is None
    retried = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    assert retried[0]["commandId"] == command.id
    assert retried[0]["duplicate"] is True
    assert db_session.scalar(select(func.count(SiteServiceRequestCommand.id))) == 1
    db_session.refresh(command)
    assert command.card_action_cleared_at is not None


def test_outbound_readback_failure_after_command_commit_is_durable_and_degrades_health(
    db_session,
) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()

    class FailingGuardReadApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.items[1000] = {
                "ufSiteReplyAction": "SEND",
                "ufSiteReplyText": "Ответ перед сбоем readback",
            }
            self.get_calls = 0

        def call(self, method: str, params=None, **kwargs):
            if method == "crm.item.get":
                self.get_calls += 1
                if self.get_calls == 2:
                    raise RuntimeError("temporary guard readback failure")
            return super().call(method, params, **kwargs)

    api = FailingGuardReadApi()
    settings = _worker_settings(
        site_service_requests_ingest_enabled=True,
        site_service_requests_outbound_replies_enabled=True,
    )
    failed = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
        now=datetime(2026, 8, 23, 8, 0, tzinfo=UTC),
    )

    command = db_session.scalar(select(SiteServiceRequestCommand))
    db_session.refresh(case)
    degraded = build_site_service_request_health(db_session, settings=settings)

    assert failed[0]["errorCode"] == "outbound_reconcile_failed"
    assert command is not None
    assert command.status == "pending"
    assert command.card_action_cleared_at is None
    assert case.outbound_last_error_code == "outbound_reconcile_failed"
    assert degraded["status"] == "degraded"
    assert degraded["alert_codes"] == ["outbound_failure"]
    assert degraded["outbound_failures"] == 1


def test_outbound_stale_worker_does_not_clear_repeated_send_after_ack(
    db_session,
    monkeypatch,
) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()

    api = FakeBitrixApi()
    api.items[1000] = {
        "ufSiteReplyAction": "SEND",
        "ufSiteReplyText": "Повторяемый ответ",
    }
    original_lock = worker_module._lock_site_service_request_outbound_sequence
    lock_calls = 0

    def finish_command_before_stale_worker_relocks(session) -> None:
        nonlocal lock_calls
        lock_calls += 1
        original_lock(session)
        if lock_calls != 2:
            return
        with Session(db_session.get_bind()) as competing_session:
            competing_command = competing_session.scalar(
                select(SiteServiceRequestCommand).with_for_update()
            )
            assert competing_command is not None
            competing_command.status = "applied"
            competing_command.ack_at = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
            competing_command.card_action_cleared_at = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
            competing_session.commit()
        api.items[1000]["ufSiteReplyAction"] = ""
        api.items[1000]["ufSiteReplyAction"] = "SEND"

    monkeypatch.setattr(
        worker_module,
        "_lock_site_service_request_outbound_sequence",
        finish_command_before_stale_worker_relocks,
    )
    first = collect_site_service_request_outbound_commands(
        db_session,
        settings=_worker_settings(site_service_requests_outbound_replies_enabled=True),
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
        now=datetime(2026, 8, 23, 8, 1, tzinfo=UTC),
    )

    commands = db_session.scalars(select(SiteServiceRequestCommand)).all()
    assert first == []
    assert len(commands) == 1
    assert commands[0].status == "applied"
    assert commands[0].card_action_cleared_at is not None
    assert api.items[1000]["ufSiteReplyAction"] == "SEND"

    second = collect_site_service_request_outbound_commands(
        db_session,
        settings=_worker_settings(site_service_requests_outbound_replies_enabled=True),
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
        now=datetime(2026, 8, 23, 8, 2, tzinfo=UTC),
    )
    commands = db_session.scalars(
        select(SiteServiceRequestCommand).order_by(SiteServiceRequestCommand.id)
    ).all()

    assert second[0]["duplicate"] is False
    assert len(commands) == 2
    assert commands[1].status == "pending"
    assert api.items[1000]["ufSiteReplyAction"] == ""


def test_applied_outbound_with_changed_text_restores_send_before_finishing_cleanup(
    db_session,
) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.flush()
    command, _duplicate = create_site_service_request_command(
        db_session,
        case=case,
        reply_text="Старый доставленный ответ",
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )
    command.status = "applied"
    command.source_message_id = 1301
    command.ack_at = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
    command.card_action_cleared_at = None
    db_session.commit()

    api = FakeBitrixApi()
    api.items[1000] = {
        "ufSiteReplyAction": "",
        "ufSiteReplyText": "Позднее редактирование без SEND",
        "ufSiteReplyStatus": "PENDING",
    }
    result = collect_site_service_request_outbound_commands(
        db_session,
        settings=_worker_settings(site_service_requests_outbound_replies_enabled=True),
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )

    assert result[0]["status"] == "applied"
    assert result[0]["duplicate"] is True
    assert api.items[1000]["ufSiteReplyStatus"] == "PENDING"
    assert api.items[1000]["ufSiteReplyAction"] == "SEND"
    assert api.items[1000]["ufSiteReplyText"] == "Позднее редактирование без SEND"
    assert db_session.scalar(select(func.count(SiteServiceRequestCommand.id))) == 1
    db_session.refresh(command)
    assert command.card_action_cleared_at is not None

    second = collect_site_service_request_outbound_commands(
        db_session,
        settings=_worker_settings(site_service_requests_outbound_replies_enabled=True),
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )
    commands = db_session.scalars(
        select(SiteServiceRequestCommand).order_by(SiteServiceRequestCommand.id)
    ).all()

    assert second[0]["duplicate"] is False
    assert len(commands) == 2
    assert commands[1].reply_sha256 != commands[0].reply_sha256
    assert api.items[1000]["ufSiteReplyAction"] == ""


@pytest.mark.parametrize("terminal_status", ["applied", "failed"])
def test_terminal_status_reconcile_preserves_concurrent_new_send(
    db_session,
    terminal_status: str,
) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.flush()
    command, _duplicate = create_site_service_request_command(
        db_session,
        case=case,
        reply_text="Предыдущий ответ",
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )
    command.status = terminal_status
    command.last_error_code = "message_write_failed" if terminal_status == "failed" else None
    command.card_action_cleared_at = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
    db_session.commit()

    class ConcurrentSendApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.items[1000] = {
                "ufSiteReplyAction": "",
                "ufSiteReplyText": "Предыдущий ответ",
                "ufSiteReplyStatus": "PENDING",
            }
            self.injected_send = False

        def call(self, method: str, params=None, **kwargs):
            if method == "crm.item.update" and not self.injected_send:
                self.injected_send = True
                self.items[1000]["ufSiteReplyAction"] = "SEND"
                self.items[1000]["ufSiteReplyText"] = "Новый конкурентный ответ"
            return super().call(method, params, **kwargs)

    api = ConcurrentSendApi()
    settings = _worker_settings(site_service_requests_outbound_replies_enabled=True)
    cipher = SiteServiceRequestCipher(_ENCRYPTION_KEY)

    first = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )

    assert first[0]["status"] == terminal_status
    assert api.items[1000]["ufSiteReplyAction"] == "SEND"
    assert api.items[1000]["ufSiteReplyText"] == "Новый конкурентный ответ"

    second = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    commands = db_session.scalars(
        select(SiteServiceRequestCommand).order_by(SiteServiceRequestCommand.id)
    ).all()

    assert second[0]["duplicate"] is False
    assert len(commands) == 2
    assert commands[1].reply_sha256 != commands[0].reply_sha256


@pytest.mark.parametrize(
    ("lane", "checked_at_field", "error_field"),
    [
        ("assignment", "assignment_checked_at", "assignment_last_error_code"),
        ("outbound", "outbound_checked_at", "outbound_last_error_code"),
    ],
)
def test_reconcile_failure_checkpoint_does_not_overwrite_same_time_success(
    db_session,
    lane: str,
    checked_at_field: str,
    error_field: str,
) -> None:
    newer_time = datetime(2026, 8, 23, 8, 1, tzinfo=UTC)
    case = _case(bitrix_item_id=1000)
    setattr(case, checked_at_field, newer_time)
    setattr(case, error_field, None)
    db_session.add(case)
    db_session.commit()

    checkpoint_case, recorded = worker_module._checkpoint_site_service_request_reconcile_failure(
        db_session,
        case_id=case.id,
        lane=lane,
        current_time=newer_time,
    )

    db_session.refresh(case)
    stored_time = getattr(case, checked_at_field)
    assert checkpoint_case is not None and checkpoint_case.id == case.id
    assert recorded is False
    assert stored_time is not None and stored_time.replace(tzinfo=UTC) == newer_time
    assert getattr(case, error_field) is None


@pytest.mark.parametrize(
    ("lane", "checked_at_field", "error_field", "expected_error"),
    [
        (
            "assignment",
            "assignment_checked_at",
            "assignment_last_error_code",
            "assignment_reconcile_failed",
        ),
        (
            "outbound",
            "outbound_checked_at",
            "outbound_last_error_code",
            "outbound_reconcile_failed",
        ),
    ],
)
def test_selection_failure_checkpoint_skips_cases_already_processed_in_same_tick(
    db_session,
    lane: str,
    checked_at_field: str,
    error_field: str,
    expected_error: str,
) -> None:
    current_time = datetime(2026, 8, 23, 8, 1, tzinfo=UTC)
    processed = _case(source_ticket_id=741, bitrix_item_id=1000)
    pending = _case(source_ticket_id=742, bitrix_item_id=1001)
    setattr(processed, checked_at_field, current_time)
    db_session.add_all([processed, pending])
    db_session.commit()

    checkpoint_case, recorded = worker_module._checkpoint_site_service_request_reconcile_failure(
        db_session,
        case_id=None,
        lane=lane,
        current_time=current_time,
        exclude_case_ids={processed.id},
    )

    db_session.refresh(processed)
    db_session.refresh(pending)
    assert checkpoint_case is not None and checkpoint_case.id == pending.id
    assert recorded is True
    assert getattr(processed, error_field) is None
    assert getattr(pending, error_field) == expected_error


@pytest.mark.parametrize(
    ("lane", "checked_at_field", "error_field", "expected_error"),
    [
        (
            "assignment",
            "assignment_checked_at",
            "assignment_last_error_code",
            "assignment_reconcile_failed",
        ),
        (
            "outbound",
            "outbound_checked_at",
            "outbound_last_error_code",
            "outbound_reconcile_failed",
        ),
    ],
)
def test_selection_failure_checkpoint_falls_back_after_all_cases_were_processed(
    db_session,
    lane: str,
    checked_at_field: str,
    error_field: str,
    expected_error: str,
) -> None:
    current_time = datetime(2026, 8, 23, 8, 1, tzinfo=UTC)
    processed = _case(bitrix_item_id=1000)
    setattr(processed, checked_at_field, current_time)
    db_session.add(processed)
    db_session.commit()

    checkpoint_case, recorded = worker_module._checkpoint_site_service_request_reconcile_failure(
        db_session,
        case_id=None,
        lane=lane,
        current_time=current_time,
        exclude_case_ids={processed.id},
    )

    db_session.refresh(processed)
    assert checkpoint_case is not None and checkpoint_case.id == processed.id
    assert recorded is True
    assert getattr(processed, error_field) == expected_error


def test_outbound_text_changed_after_command_commit_is_preserved_for_next_tick(
    db_session,
) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()

    class EditBeforeGuardReadApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.items[1000] = {
                "ufSiteReplyAction": "SEND",
                "ufSiteReplyText": "Первый ответ",
            }
            self.get_calls = 0

        def call(self, method: str, params=None, **kwargs):
            if method == "crm.item.get":
                self.get_calls += 1
                if self.get_calls == 2:
                    self.items[1000]["ufSiteReplyText"] = "Исправленный ответ"
            return super().call(method, params, **kwargs)

    api = EditBeforeGuardReadApi()
    settings = _worker_settings(site_service_requests_outbound_replies_enabled=True)
    cipher = SiteServiceRequestCipher(_ENCRYPTION_KEY)

    first = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    first_command = db_session.scalar(select(SiteServiceRequestCommand))

    assert first[0]["duplicate"] is False
    assert first_command is not None and first_command.card_action_cleared_at is not None
    assert api.items[1000]["ufSiteReplyText"] == "Исправленный ответ"
    assert api.items[1000]["ufSiteReplyAction"] == "SEND"
    assert api.items[1000]["ufSiteReplyStatus"] == "PENDING"

    second = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    commands = db_session.scalars(
        select(SiteServiceRequestCommand).order_by(SiteServiceRequestCommand.id)
    ).all()

    assert second[0]["duplicate"] is False
    assert len(commands) == 2
    assert commands[0].reply_sha256 != commands[1].reply_sha256
    assert api.items[1000]["ufSiteReplyAction"] == ""


def test_outbound_text_changed_during_clear_restores_send(db_session) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()

    class EditDuringClearApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.items[1000] = {
                "ufSiteReplyAction": "SEND",
                "ufSiteReplyText": "Первый ответ",
            }
            self.changed_during_clear = False

        def call(self, method: str, params=None, **kwargs):
            mapped = dict(params or [])
            response = super().call(method, params, **kwargs)
            if (
                method == "crm.item.update"
                and mapped.get("fields[ufSiteReplyAction]") == ""
                and not self.changed_during_clear
            ):
                self.changed_during_clear = True
                self.items[1000]["ufSiteReplyText"] = "Ответ во время очистки"
            return response

    api = EditDuringClearApi()
    settings = _worker_settings(site_service_requests_outbound_replies_enabled=True)
    cipher = SiteServiceRequestCipher(_ENCRYPTION_KEY)

    collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    first_command = db_session.scalar(select(SiteServiceRequestCommand))

    assert first_command is not None and first_command.card_action_cleared_at is not None
    assert api.items[1000]["ufSiteReplyText"] == "Ответ во время очистки"
    assert api.items[1000]["ufSiteReplyAction"] == "SEND"
    assert api.items[1000]["ufSiteReplyStatus"] == "PENDING"

    collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    assert db_session.scalar(select(func.count(SiteServiceRequestCommand.id))) == 2


def test_outbound_restore_timeout_retries_without_losing_changed_text(db_session) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()

    class RestoreTimeoutApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.items[1000] = {
                "ufSiteReplyAction": "SEND",
                "ufSiteReplyText": "Первый ответ",
            }
            self.changed_during_clear = False
            self.fail_restore_once = True

        def call(self, method: str, params=None, **kwargs):
            mapped = dict(params or [])
            if method == "crm.item.update":
                response = super().call(method, params, **kwargs)
                action = mapped.get("fields[ufSiteReplyAction]")
                if action == "" and not self.changed_during_clear:
                    self.changed_during_clear = True
                    self.items[1000]["ufSiteReplyText"] = "Сохранённый новый ответ"
                elif action == "SEND" and self.fail_restore_once:
                    self.fail_restore_once = False
                    raise RuntimeError("restore timeout after update")
                return response
            return super().call(method, params, **kwargs)

    api = RestoreTimeoutApi()
    settings = _worker_settings(site_service_requests_outbound_replies_enabled=True)
    cipher = SiteServiceRequestCipher(_ENCRYPTION_KEY)

    failed = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    command = db_session.scalar(select(SiteServiceRequestCommand))

    assert failed[0]["status"] == "retry"
    assert command is not None and command.card_action_cleared_at is None
    assert api.items[1000]["ufSiteReplyText"] == "Сохранённый новый ответ"
    assert api.items[1000]["ufSiteReplyAction"] == "SEND"

    restored = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    db_session.refresh(command)
    assert restored[0]["commandId"] == command.id
    assert command.card_action_cleared_at is not None
    assert api.items[1000]["ufSiteReplyAction"] == "SEND"
    assert api.items[1000]["ufSiteReplyStatus"] == "PENDING"

    collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    assert db_session.scalar(select(func.count(SiteServiceRequestCommand.id))) == 2


def test_outbound_database_error_degrades_health_until_successful_retry(db_session) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()

    class DatabaseFailureApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.items[1000] = {
                "ufSiteReplyAction": "SEND",
                "ufSiteReplyText": "Ответ из карточки",
            }
            self.get_calls = 0
            self.fail_once = True

        def call(self, method: str, params=None, **kwargs):
            if method == "crm.item.get":
                self.get_calls += 1
                if self.get_calls == 2 and self.fail_once:
                    self.fail_once = False
                    raise OperationalError(
                        "SELECT site_service_request_command FOR UPDATE",
                        {},
                        RuntimeError("deadlock detected"),
                    )
            return super().call(method, params, **kwargs)

    api = DatabaseFailureApi()
    settings = _worker_settings(
        site_service_requests_ingest_enabled=True,
        site_service_requests_outbound_replies_enabled=True,
    )
    cipher = SiteServiceRequestCipher(_ENCRYPTION_KEY)

    failed = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    degraded = build_site_service_request_health(db_session, settings=settings)

    assert failed[0]["errorCode"] == "outbound_reconcile_failed"
    assert degraded["status"] == "degraded"
    assert degraded["alert_codes"] == ["outbound_failure"]
    assert degraded["outbound_failures"] == 1

    retried = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    recovered = build_site_service_request_health(db_session, settings=settings)

    assert retried[0]["status"] == "pending"
    assert recovered["status"] == "healthy"
    assert recovered["alert_codes"] == []
    assert recovered["outbound_failures"] == 0


def test_outbound_selection_database_error_checkpoints_health(
    db_session,
    monkeypatch,
) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()

    api = FakeBitrixApi()
    api.items[1000] = {}
    settings = _worker_settings(
        site_service_requests_ingest_enabled=True,
        site_service_requests_outbound_replies_enabled=True,
    )
    original_scalar = db_session.scalar
    fail_selection_once = True

    def fail_first_lane_selection(statement, *args, **kwargs):
        nonlocal fail_selection_once
        rendered = str(statement)
        if (
            fail_selection_once
            and "site_service_request_case.outbound_checked_at" in rendered
            and "ORDER BY" in rendered
        ):
            fail_selection_once = False
            raise OperationalError(
                "SELECT site_service_request_case FOR UPDATE",
                {},
                RuntimeError("connection reset"),
            )
        return original_scalar(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "scalar", fail_first_lane_selection)
    failed = collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )
    degraded = build_site_service_request_health(db_session, settings=settings)

    assert failed[0]["errorCode"] == "outbound_reconcile_failed"
    assert degraded["status"] == "degraded"
    assert degraded["alert_codes"] == ["outbound_failure"]

    collect_site_service_request_outbound_commands(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )
    recovered = build_site_service_request_health(db_session, settings=settings)
    assert recovered["status"] == "healthy"
    assert recovered["outbound_failures"] == 0


def test_unexpected_outbound_error_checkpoints_health_before_reraise(db_session) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()

    class UnexpectedFailureApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.items[1000] = {}

        def call(self, method: str, params=None, **kwargs):
            if method == "crm.item.get":
                raise ValueError("unexpected parser failure")
            return super().call(method, params, **kwargs)

    settings = _worker_settings(
        site_service_requests_ingest_enabled=True,
        site_service_requests_outbound_replies_enabled=True,
    )
    with pytest.raises(ValueError, match="unexpected parser failure"):
        collect_site_service_request_outbound_commands(
            db_session,
            settings=settings,
            writer=SiteServiceRequestBitrixWriter(UnexpectedFailureApi()),
            cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
        )

    degraded = build_site_service_request_health(db_session, settings=settings)
    assert degraded["status"] == "degraded"
    assert degraded["alert_codes"] == ["outbound_failure"]
    assert degraded["outbound_failures"] == 1


def test_outbound_clear_readback_requires_action_field_presence(db_session) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()

    class MissingActionReadbackApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.items[1000] = {
                "ufSiteReplyAction": "SEND",
                "ufSiteReplyText": "Ответ из карточки",
            }
            self.clear_attempted = False

        def call(self, method: str, params=None, **kwargs):
            response = super().call(method, params, **kwargs)
            if method == "crm.item.update":
                self.clear_attempted = True
            if method == "crm.item.get" and self.clear_attempted:
                response["result"]["item"].pop("ufSiteReplyAction", None)
            return response

    api = MissingActionReadbackApi()
    failed = collect_site_service_request_outbound_commands(
        db_session,
        settings=_worker_settings(site_service_requests_outbound_replies_enabled=True),
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )

    command = db_session.scalar(select(SiteServiceRequestCommand))
    assert failed[0]["status"] == "retry"
    assert command is not None
    assert command.card_action_cleared_at is None


def test_outbound_initial_readback_requires_action_field_presence(db_session) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()

    class MissingInitialActionApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.items[1000] = {"ufSiteReplyText": "Ответ из карточки"}

        def call(self, method: str, params=None, **kwargs):
            response = super().call(method, params, **kwargs)
            if method == "crm.item.get":
                response["result"]["item"].pop("ufSiteReplyAction", None)
            return response

    results = collect_site_service_request_outbound_commands(
        db_session,
        settings=_worker_settings(site_service_requests_outbound_replies_enabled=True),
        writer=SiteServiceRequestBitrixWriter(MissingInitialActionApi()),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
    )

    db_session.refresh(case)
    assert results[0]["status"] == "retry"
    assert case.outbound_last_error_code == "outbound_reconcile_failed"
    assert db_session.scalar(select(SiteServiceRequestCommand.id)) is None


def test_outbound_scan_rotates_past_the_first_batch(db_session) -> None:
    api = FakeBitrixApi()
    for ticket_id in range(1, 22):
        db_session.add(_case(source_ticket_id=ticket_id, bitrix_item_id=1000 + ticket_id))
        api.items[1000 + ticket_id] = {}
    db_session.commit()
    settings = _worker_settings(site_service_requests_outbound_replies_enabled=True)
    cipher = SiteServiceRequestCipher(_ENCRYPTION_KEY)

    assert (
        collect_site_service_request_outbound_commands(
            db_session,
            settings=settings,
            writer=SiteServiceRequestBitrixWriter(api),
            cipher=cipher,
            limit=20,
        )
        == []
    )
    assert (
        collect_site_service_request_outbound_commands(
            db_session,
            settings=settings,
            writer=SiteServiceRequestBitrixWriter(api),
            cipher=cipher,
            limit=1,
        )
        == []
    )

    last_case = db_session.scalar(
        select(SiteServiceRequestCase).where(SiteServiceRequestCase.source_ticket_id == 21)
    )
    assert last_case is not None and last_case.outbound_checked_at is not None


def test_later_outbound_card_failure_cannot_rollback_previous_command(db_session) -> None:
    first_case = _case(source_ticket_id=741, bitrix_item_id=1000)
    second_case = _case(source_ticket_id=742, bitrix_item_id=1001)
    db_session.add_all([first_case, second_case])
    db_session.commit()

    class SecondCardFailureApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.items[1000] = {
                "ufSiteReplyAction": "SEND",
                "ufSiteReplyText": "Первый ответ",
            }
            self.items[1001] = {}

        def call(self, method: str, params=None, **kwargs):
            mapped = dict(params or [])
            if method == "crm.item.get" and mapped.get("id") == "1001":
                raise RuntimeError("second card unavailable")
            return super().call(method, params, **kwargs)

    api = SecondCardFailureApi()
    results = collect_site_service_request_outbound_commands(
        db_session,
        settings=_worker_settings(site_service_requests_outbound_replies_enabled=True),
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
        limit=2,
    )

    commands = db_session.scalars(select(SiteServiceRequestCommand)).all()
    assert len(commands) == 1
    assert commands[0].case_id == first_case.id
    assert commands[0].card_action_cleared_at is not None
    assert results[1]["status"] == "retry"
    db_session.refresh(second_case)
    assert second_case.outbound_checked_at is not None
    assert second_case.outbound_last_error_code == "outbound_reconcile_failed"


def test_first_outbound_card_failure_does_not_starve_next_case(db_session) -> None:
    first = _case(source_ticket_id=1, bitrix_item_id=1001)
    second = _case(source_ticket_id=2, bitrix_item_id=1002)
    db_session.add_all([first, second])
    db_session.commit()

    class FirstCardFailureApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.items = {
                1001: {},
                1002: {
                    "ufSiteReplyAction": "SEND",
                    "ufSiteReplyText": "Второй ответ",
                },
            }

        def call(self, method: str, params=None, **kwargs):
            mapped = dict(params or [])
            if method == "crm.item.get" and mapped.get("id") == "1001":
                raise RuntimeError("poison outbound card")
            return super().call(method, params, **kwargs)

    api = FirstCardFailureApi()
    results = collect_site_service_request_outbound_commands(
        db_session,
        settings=_worker_settings(site_service_requests_outbound_replies_enabled=True),
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=SiteServiceRequestCipher(_ENCRYPTION_KEY),
        limit=2,
        now=datetime(2026, 8, 23, 8, 0, tzinfo=UTC),
    )

    db_session.refresh(first)
    command = db_session.scalar(select(SiteServiceRequestCommand))
    assert results[0]["status"] == "retry"
    assert results[1]["ticketId"] == second.source_ticket_id
    assert first.outbound_last_error_code == "outbound_reconcile_failed"
    assert command is not None and command.case_id == second.id


def test_assignment_reconcile_escalates_once_and_adds_one_timeline_comment(
    db_session,
) -> None:
    cipher = _persist_event(db_session)
    api = FakeBitrixApi()
    api.timeman = {1001: "OPENED", 1002: "OPENED"}
    reader = SiteServiceRequestBitrixReader(api)
    settings = _worker_settings()
    plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
    )
    apply_site_service_request_worker_plans(
        db_session,
        plans=plans,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 1, tzinfo=UTC),
    )
    case = db_session.scalar(select(SiteServiceRequestCase))
    assert case is not None and case.bitrix_item_id is not None
    case.first_response_due_at = datetime(2026, 8, 22, 7, 30, tzinfo=UTC)
    case.last_open_stage_id = "DT1134_55:WORK"
    api.items[int(case.bitrix_item_id)]["stageId"] = "DT1134_55:SUCCESS"
    db_session.commit()

    first = reconcile_site_service_request_assignments(
        db_session,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        now=datetime(2026, 8, 22, 8, 0, tzinfo=UTC),
    )
    second = reconcile_site_service_request_assignments(
        db_session,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        now=datetime(2026, 8, 22, 8, 1, tzinfo=UTC),
    )

    assert first[0]["escalated"] is True
    assert first[0]["closeReverted"] is True
    assert second[0]["escalated"] is False
    db_session.refresh(case)
    assert case.assigned_user_id == 1003
    assert case.escalated_at is not None
    assert api.items[int(case.bitrix_item_id)]["stageId"] == "DT1134_55:WORK"
    assert [method for method, _params in api.calls].count("crm.timeline.comment.add") == 1
    assert [method for method, _params in api.calls].count("im.notify.personal.add") == 1

    api.items[int(case.bitrix_item_id)]["stageId"] = "DT1134_55:FAIL"
    failed_close = reconcile_site_service_request_assignments(
        db_session,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        now=datetime(2026, 8, 22, 8, 2, tzinfo=UTC),
    )
    assert failed_close[0]["closeReverted"] is True
    assert api.items[int(case.bitrix_item_id)]["stageId"] == "DT1134_55:WORK"


def test_support_message_readback_is_required_before_first_response_is_recorded(
    db_session,
) -> None:
    cipher = _persist_event(db_session)
    api = FakeBitrixApi()
    reader = SiteServiceRequestBitrixReader(api)
    settings = _worker_settings()
    initial_plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
    )
    apply_site_service_request_worker_plans(
        db_session,
        plans=initial_plans,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 1, tzinfo=UTC),
    )
    case = db_session.scalar(select(SiteServiceRequestCase))
    assert case is not None and case.first_response_at is None
    command, _duplicate = create_site_service_request_command(
        db_session,
        case=case,
        reply_text="Ответ клиенту",
        cipher=cipher,
    )
    command.status = "applied"
    command.source_message_id = 1301
    command.ack_at = datetime(2026, 8, 22, 8, 0, tzinfo=UTC)
    db_session.commit()
    assert case.first_response_at is None

    payload_dict = _event_payload()
    payload_dict["eventId"] = "site-support:741:1301"
    payload_dict["eventType"] = "ticket.message_added"
    payload_dict["occurredAt"] = "2026-08-22T11:01:00+03:00"
    payload_dict["history"].append(
        {
            "messageId": 1301,
            "authorKind": "support",
            "createdAt": "2026-08-22T11:00:00+03:00",
            "text": "Ответ клиенту",
            "files": [],
        }
    )
    raw_body = json.dumps(
        payload_dict,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    accept_site_service_request_event(
        db_session,
        payload=SiteServiceRequestEventPayload.model_validate(payload_dict),
        raw_body=raw_body,
        payload_sha256=content_sha256(raw_body),
        cipher=cipher,
        max_file_bytes=10 * 1024 * 1024,
    )
    db_session.commit()
    readback_plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
        now=datetime(2026, 8, 22, 8, 2, tzinfo=UTC),
    )
    apply_site_service_request_worker_plans(
        db_session,
        plans=readback_plans,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
        now=datetime(2026, 8, 22, 8, 3, tzinfo=UTC),
    )

    db_session.refresh(case)
    assert case.first_response_at is not None
    assert case.first_response_at.replace(tzinfo=UTC) == datetime(2026, 8, 22, 8, 0, tzinfo=UTC)
    item = api.items[int(case.bitrix_item_id)]
    assert item["ufFirstResponseAt"].startswith("2026-08-22T08:00:00")
    assert item["ufSiteReplyStatus"] == "SENT"
    assert item["ufSiteReplyAction"] == ""


def test_direct_support_reply_closes_first_response_without_backend_command(db_session) -> None:
    cipher = _persist_event(db_session)
    api = FakeBitrixApi()
    reader = SiteServiceRequestBitrixReader(api)
    settings = _worker_settings()
    initial = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
    )
    apply_site_service_request_worker_plans(
        db_session,
        plans=initial,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 1, tzinfo=UTC),
    )
    payload = _event_payload()
    payload["eventId"] = "site-support:741:1301"
    payload["eventType"] = "ticket.message_added"
    payload["history"].insert(
        0,
        {
            "messageId": 1101,
            "authorKind": "support-team",
            "createdAt": "2026-08-22T08:00:00+03:00",
            "text": "Старый ответ до начала SLA",
            "files": [],
        },
    )
    payload["history"].append(
        {
            "messageId": 1301,
            "authorKind": "support-team",
            "createdAt": "2026-08-22T11:00:00+03:00",
            "text": "Прямой ответ поддержки",
            "files": [],
        }
    )
    _persist_payload(db_session, payload, cipher)
    plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
        now=datetime(2026, 8, 22, 8, 1, tzinfo=UTC),
    )
    apply_site_service_request_worker_plans(
        db_session,
        plans=plans,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
        now=datetime(2026, 8, 22, 8, 2, tzinfo=UTC),
    )

    case = db_session.scalar(select(SiteServiceRequestCase))
    assert case is not None
    assert case.first_response_at is not None
    assert case.first_response_at.replace(tzinfo=UTC) == datetime(2026, 8, 22, 8, 0, tzinfo=UTC)


def test_planning_failure_is_recorded_for_retry_in_apply_mode(db_session) -> None:
    cipher = _persist_event(db_session)
    event = db_session.scalar(select(SiteServiceRequestEvent))
    assert event is not None
    failure_time = worker_module._as_utc(event.updated_at) + timedelta(minutes=1)

    class FailingApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method == "crm.duplicate.findbycomm":
                raise RuntimeError("temporary Bitrix outage")
            return super().call(method, params, **kwargs)

    api = FailingApi()
    failures = []
    plans = build_site_service_request_worker_plans(
        db_session,
        settings=_worker_settings(),
        reader=SiteServiceRequestBitrixReader(api),
        cipher=cipher,
        now=failure_time,
        failure_results=failures,
        failure_writer=SiteServiceRequestBitrixWriter(api),
    )

    assert plans == []
    assert len(failures) == 1
    assert failures[0].status == "retry"
    db_session.refresh(event)
    assert event.status == "retry"
    assert event.last_error_code == "bitrix_unavailable"


@pytest.mark.parametrize("missing_field", ["ufSiteSyncStatus", "ufSiteSyncError"])
def test_worker_rejects_missing_sync_readback_fields(db_session, missing_field: str) -> None:
    cipher = _persist_event(db_session)

    class MissingSyncFieldApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            response = super().call(method, params, **kwargs)
            if method == "crm.item.get":
                response["result"]["item"].pop(missing_field, None)
            return response

    api = MissingSyncFieldApi()
    settings = _worker_settings()
    reader = SiteServiceRequestBitrixReader(api)
    plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
    )

    results = apply_site_service_request_worker_plans(
        db_session,
        plans=plans,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )

    assert results[0].status == "retry"
    assert results[0].error_code == "bitrix_unavailable"


def test_worker_rejects_missing_stage_on_existing_item(db_session) -> None:
    cipher = _persist_event(db_session)

    class MissingStageApi(FakeBitrixApi):
        omit_stage = False

        def call(self, method: str, params=None, **kwargs):
            response = super().call(method, params, **kwargs)
            if method == "crm.item.get" and self.omit_stage:
                response["result"]["item"].pop("stageId", None)
            return response

    api = MissingStageApi()
    settings = _worker_settings()
    reader = SiteServiceRequestBitrixReader(api)
    initial = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
    )
    apply_site_service_request_worker_plans(
        db_session,
        plans=initial,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    payload = _event_payload()
    payload["eventId"] = "site-support:741:1301"
    payload["eventType"] = "ticket.updated"
    payload["history"].append(
        {
            "messageId": 1301,
            "authorKind": "customer",
            "createdAt": "2026-08-22T11:00:00+03:00",
            "text": "Новое сообщение",
            "files": [],
        }
    )
    _persist_payload(db_session, payload, cipher)
    plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
    )
    api.omit_stage = True

    results = apply_site_service_request_worker_plans(
        db_session,
        plans=plans,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )

    assert results[0].status == "retry"
    assert results[0].error_code == "bitrix_unavailable"


def test_event_retry_preserves_active_file_error_overlay(db_session) -> None:
    _persist_event(db_session)
    event = db_session.scalar(select(SiteServiceRequestEvent))
    case = db_session.scalar(select(SiteServiceRequestCase))
    assert event is not None and case is not None
    case.base_sync_status = "pending"
    case.sync_status = "file_sync_error"
    case.last_error_code = "file_sync_error"
    checkpoint_time = worker_module._as_utc(event.updated_at) + timedelta(minutes=1)

    result = worker_module._record_site_service_request_failure(
        db_session,
        event_id=event.event_id,
        error_code="bitrix_unavailable",
        permanent=False,
        now=checkpoint_time,
    )

    assert result.status == "retry"
    assert case.base_sync_status == "retry"
    assert case.base_error_code == "bitrix_unavailable"
    assert case.sync_status == "file_sync_error"
    assert case.last_error_code == "file_sync_error"


def test_needs_attention_state_survives_notification_outage(db_session) -> None:
    cipher = _persist_event(db_session)
    event = db_session.scalar(select(SiteServiceRequestEvent))
    assert event is not None
    event.created_at = datetime(2026, 8, 21, 6, 0, tzinfo=UTC)
    event.updated_at = datetime(2026, 8, 21, 6, 0, tzinfo=UTC)
    db_session.commit()

    class FailingApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method in {"crm.duplicate.findbycomm", "im.notify.personal.add"}:
                raise RuntimeError("temporary Bitrix outage")
            return super().call(method, params, **kwargs)

    failures = []
    api = FailingApi()
    plans = build_site_service_request_worker_plans(
        db_session,
        settings=_worker_settings(),
        reader=SiteServiceRequestBitrixReader(api),
        cipher=cipher,
        now=datetime(2026, 8, 23, 7, 0, tzinfo=UTC),
        failure_results=failures,
        failure_writer=SiteServiceRequestBitrixWriter(api),
    )

    assert plans == []
    assert failures[0].status == "needs_attention"
    db_session.expire_all()
    stored_event = db_session.scalar(select(SiteServiceRequestEvent))
    assert stored_event is not None
    assert stored_event.status == "needs_attention"


def test_worker_limit_is_clamped_to_safe_range() -> None:
    settings = _worker_settings(site_service_requests_worker_batch_size=100)

    assert worker_module._site_service_request_worker_limit(settings, limit=None) == 100
    assert worker_module._site_service_request_worker_limit(settings, limit=0) == 1
    assert worker_module._site_service_request_worker_limit(settings, limit=500) == 100


def test_stale_plan_does_not_reopen_event_processed_by_another_worker(db_session) -> None:
    cipher = _persist_event(db_session)
    api = FakeBitrixApi()
    settings = _worker_settings()
    plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=SiteServiceRequestBitrixReader(api),
        cipher=cipher,
    )
    event = db_session.scalar(select(SiteServiceRequestEvent))
    assert event is not None
    event.status = "processed"
    event.payload_encrypted = None
    db_session.commit()

    results = apply_site_service_request_worker_plans(
        db_session,
        plans=plans,
        settings=settings,
        reader=SiteServiceRequestBitrixReader(api),
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )

    assert results[0].status == "processed"
    db_session.refresh(event)
    assert event.status == "processed"
    assert event.last_error_code is None


def test_stale_plan_does_not_process_event_already_marked_needs_attention(
    db_session,
) -> None:
    cipher = _persist_event(db_session)
    api = FakeBitrixApi()
    settings = _worker_settings()
    plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=SiteServiceRequestBitrixReader(api),
        cipher=cipher,
    )
    event = db_session.scalar(select(SiteServiceRequestEvent))
    assert event is not None
    event.status = "needs_attention"
    event.last_error_code = "manual_review_required"
    db_session.commit()

    results = apply_site_service_request_worker_plans(
        db_session,
        plans=plans,
        settings=settings,
        reader=SiteServiceRequestBitrixReader(api),
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )

    assert results[0].status == "needs_attention"
    assert results[0].error_code == "manual_review_required"
    assert api.items == {}


def test_dynamic_item_writer_uses_rest_camel_case_and_clears_null_values() -> None:
    api = FakeBitrixApi()
    api.items[1000] = {}
    writer = SiteServiceRequestBitrixWriter(api)

    writer.update_item_fields(
        entity_type_id=1134,
        item_id=1000,
        fields={"UF_CRM_36_SITE_REPLY_ACTION": None},
    )

    assert api.items[1000] == {"ufCrm36SiteReplyAction": ""}


@pytest.mark.parametrize(
    "malformed_status",
    ["", "UNKNOWN", True, ["OPENED"], {"STATUS": "OPENED"}],
)
def test_timeman_reader_maps_malformed_status_to_error(
    malformed_status: object,
) -> None:
    api = FakeBitrixApi()
    api.timeman[1001] = malformed_status

    assert SiteServiceRequestBitrixReader(api).timeman_statuses([1001]) == {1001: "ERROR"}


def test_timeman_exception_creates_card_in_assignment_waiting(db_session) -> None:
    cipher = _persist_event(db_session)

    class TimemanFailureApi(FakeBitrixApi):
        fail_timeman = True

        def call(self, method: str, params=None, **kwargs):
            if method == "timeman.status" and self.fail_timeman:
                raise RuntimeError("timeman unavailable")
            return super().call(method, params, **kwargs)

    api = TimemanFailureApi()
    reader = SiteServiceRequestBitrixReader(api)
    settings = _worker_settings(site_service_requests_ingest_enabled=True)
    plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
    )
    results = apply_site_service_request_worker_plans(
        db_session,
        plans=plans,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 1, tzinfo=UTC),
    )

    case = db_session.scalar(select(SiteServiceRequestCase))
    assert results[0].status == "processed"
    assert case is not None and case.bitrix_item_id is not None
    assert case.assignment_state == "waiting"
    assert case.assignment_last_error_code == "timeman_unavailable"
    assert case.sync_status == "assignment_waiting"
    assert api.items[int(case.bitrix_item_id)]["ufSiteSyncStatus"] == "ASSIGNMENT_WAITING"
    assert api.items[int(case.bitrix_item_id)]["assignedById"] == ""
    degraded = build_site_service_request_health(db_session, settings=settings)
    assert degraded["status"] == "degraded"
    assert degraded["assignment_failures"] == 1

    api.fail_timeman = False
    reconcile_site_service_request_assignments(
        db_session,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        now=datetime(2026, 8, 22, 7, 2, tzinfo=UTC),
    )
    db_session.refresh(case)
    assert case.assignment_last_error_code is None
    recovered = build_site_service_request_health(db_session, settings=settings)
    assert recovered["status"] == "healthy"


def test_assignment_reconcile_clears_stale_assignee_when_every_shift_is_closed(
    db_session,
) -> None:
    case = _case(
        bitrix_item_id=1000,
        assigned_user_id=1001,
        assignment_state="assigned",
    )
    db_session.add(case)
    db_session.commit()
    api = FakeBitrixApi()
    api.items[1000] = {"stageId": "DT1134_55:NEW", "assignedById": "1001"}
    api.timeman = {1001: "CLOSED", 1002: "CLOSED"}

    reconcile_site_service_request_assignments(
        db_session,
        settings=_worker_settings(),
        reader=SiteServiceRequestBitrixReader(api),
        writer=SiteServiceRequestBitrixWriter(api),
        now=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
    )

    db_session.refresh(case)
    assert case.assignment_state == "waiting"
    assert case.assigned_user_id is None
    assert api.items[1000]["assignedById"] == ""


def test_assignment_clear_readback_requires_assignee_field_presence(db_session) -> None:
    case = _case(
        bitrix_item_id=1000,
        assigned_user_id=1001,
        assignment_state="assigned",
    )
    db_session.add(case)
    db_session.commit()

    class MissingAssigneeReadbackApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.items[1000] = {"stageId": "DT1134_55:NEW", "assignedById": "1001"}
            self.timeman = {1001: "CLOSED", 1002: "CLOSED"}
            self.updated = False

        def call(self, method: str, params=None, **kwargs):
            response = super().call(method, params, **kwargs)
            if method == "crm.item.update":
                self.updated = True
            if method == "crm.item.get" and self.updated:
                response["result"]["item"].pop("assignedById", None)
            return response

    api = MissingAssigneeReadbackApi()
    results = reconcile_site_service_request_assignments(
        db_session,
        settings=_worker_settings(),
        reader=SiteServiceRequestBitrixReader(api),
        writer=SiteServiceRequestBitrixWriter(api),
        now=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
    )

    db_session.refresh(case)
    assert results[0]["errorCode"] == "assignment_reconcile_failed"
    assert case.assignment_last_error_code == "assignment_reconcile_failed"


def test_assignment_reassigns_only_while_card_is_new(db_session) -> None:
    cipher = _persist_event(db_session)
    api = FakeBitrixApi()
    reader = SiteServiceRequestBitrixReader(api)
    settings = _worker_settings()
    plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
        now=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
    )
    apply_site_service_request_worker_plans(
        db_session,
        plans=plans,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        now=datetime(2026, 8, 22, 7, 1, tzinfo=UTC),
        cipher=cipher,
    )
    case = db_session.scalar(select(SiteServiceRequestCase))
    assert case is not None and case.bitrix_item_id is not None
    assert case.assigned_user_id == 1001

    api.timeman = {1001: "CLOSED", 1002: "OPENED"}
    reconcile_site_service_request_assignments(
        db_session,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        now=datetime(2026, 8, 22, 7, 2, tzinfo=UTC),
    )
    assert case.assigned_user_id == 1002
    assert api.items[int(case.bitrix_item_id)]["assignedById"] == "1002"

    api.items[int(case.bitrix_item_id)]["stageId"] = "DT1134_55:PREPARATION"
    api.timeman = {1001: "OPENED", 1002: "CLOSED"}
    reconcile_site_service_request_assignments(
        db_session,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        now=datetime(2026, 8, 22, 7, 3, tzinfo=UTC),
    )
    assert case.assigned_user_id == 1002
    assert api.items[int(case.bitrix_item_id)]["assignedById"] == "1002"


def test_assignment_scan_rotates_past_the_first_batch(db_session) -> None:
    api = FakeBitrixApi()
    for ticket_id in range(1, 22):
        case = _case(source_ticket_id=ticket_id, bitrix_item_id=1000 + ticket_id)
        db_session.add(case)
        api.items[1000 + ticket_id] = {"stageId": "DT1134_55:NEW"}
    db_session.commit()
    settings = _worker_settings()
    reader = SiteServiceRequestBitrixReader(api)

    reconcile_site_service_request_assignments(
        db_session,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        limit=20,
    )
    reconcile_site_service_request_assignments(
        db_session,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        limit=1,
    )

    last_case = db_session.scalar(
        select(SiteServiceRequestCase).where(SiteServiceRequestCase.source_ticket_id == 21)
    )
    assert last_case is not None and last_case.assignment_checked_at is not None


def test_assignment_failure_is_recorded_and_does_not_starve_next_case(db_session) -> None:
    first = _case(source_ticket_id=1, bitrix_item_id=1001)
    second = _case(source_ticket_id=2, bitrix_item_id=1002)
    db_session.add_all([first, second])
    db_session.commit()

    class FirstCardFailureApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.items = {
                1001: {"stageId": "DT1134_55:NEW"},
                1002: {"stageId": "DT1134_55:NEW"},
            }

        def call(self, method: str, params=None, **kwargs):
            mapped = dict(params or [])
            if method == "crm.item.get" and mapped.get("id") == "1001":
                raise RuntimeError("poison card")
            return super().call(method, params, **kwargs)

    api = FirstCardFailureApi()
    results = reconcile_site_service_request_assignments(
        db_session,
        settings=_worker_settings(),
        reader=SiteServiceRequestBitrixReader(api),
        writer=SiteServiceRequestBitrixWriter(api),
        limit=2,
        now=datetime(2026, 8, 23, 8, 0, tzinfo=UTC),
    )

    db_session.refresh(first)
    db_session.refresh(second)
    assert results[0]["errorCode"] == "assignment_reconcile_failed"
    assert results[1]["caseId"] == second.id
    assert first.assignment_checked_at is not None
    assert first.assignment_last_error_code == "assignment_reconcile_failed"
    assert second.assignment_checked_at is not None
    assert second.assignment_last_error_code is None


def test_stale_assignment_success_does_not_regress_newer_failure_checkpoint(
    db_session,
) -> None:
    newer_time = datetime(2026, 8, 23, 9, 0, tzinfo=UTC)
    case = _case(
        bitrix_item_id=1000,
        assignment_checked_at=newer_time,
        assignment_last_error_code="timeman_unavailable",
        updated_at=newer_time,
    )
    db_session.add(case)
    db_session.commit()
    api = FakeBitrixApi()
    api.items[1000] = {"stageId": "DT1134_55:NEW"}
    settings = _worker_settings(site_service_requests_ingest_enabled=True)

    results = reconcile_site_service_request_assignments(
        db_session,
        settings=settings,
        reader=SiteServiceRequestBitrixReader(api),
        writer=SiteServiceRequestBitrixWriter(api),
        now=datetime(2026, 8, 23, 8, 0, tzinfo=UTC),
    )

    db_session.refresh(case)
    health = build_site_service_request_health(db_session, settings=settings)
    assert results[0].get("errorCode") is None
    assert case.assignment_checked_at is not None
    assert case.assignment_checked_at.replace(tzinfo=UTC) == newer_time
    assert case.assignment_last_error_code == "timeman_unavailable"
    assert case.updated_at.replace(tzinfo=UTC) >= newer_time
    assert health["status"] == "degraded"
    assert health["alert_codes"] == ["assignment_failure"]


def test_assignment_database_error_degrades_health_until_successful_retry(db_session) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()

    class DatabaseFailureApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.items[1000] = {"stageId": "DT1134_55:NEW"}
            self.fail_once = True

        def call(self, method: str, params=None, **kwargs):
            if method == "crm.item.get" and self.fail_once:
                self.fail_once = False
                raise OperationalError(
                    "SELECT site_service_request_case FOR UPDATE",
                    {},
                    RuntimeError("deadlock detected"),
                )
            return super().call(method, params, **kwargs)

    api = DatabaseFailureApi()
    settings = _worker_settings(site_service_requests_ingest_enabled=True)
    reader = SiteServiceRequestBitrixReader(api)
    writer = SiteServiceRequestBitrixWriter(api)

    failed = reconcile_site_service_request_assignments(
        db_session,
        settings=settings,
        reader=reader,
        writer=writer,
        now=datetime(2026, 8, 23, 8, 0, tzinfo=UTC),
    )
    degraded = build_site_service_request_health(db_session, settings=settings)

    assert failed[0]["errorCode"] == "assignment_reconcile_failed"
    assert degraded["status"] == "degraded"
    assert degraded["alert_codes"] == ["assignment_failure"]
    assert degraded["assignment_failures"] == 1

    retried = reconcile_site_service_request_assignments(
        db_session,
        settings=settings,
        reader=reader,
        writer=writer,
        now=datetime(2026, 8, 23, 8, 1, tzinfo=UTC),
    )
    recovered = build_site_service_request_health(db_session, settings=settings)

    assert retried[0].get("errorCode") is None
    assert recovered["status"] == "healthy"
    assert recovered["alert_codes"] == []
    assert recovered["assignment_failures"] == 0


def test_assignment_failure_clears_after_first_response(db_session) -> None:
    case = _case(bitrix_item_id=1000)
    case.assignment_last_error_code = "assignment_reconcile_failed"
    case.first_response_at = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
    db_session.add(case)
    db_session.commit()

    api = FakeBitrixApi()
    api.items[1000] = {"stageId": "DT1134_55:NEW"}
    settings = _worker_settings(site_service_requests_ingest_enabled=True)
    results = reconcile_site_service_request_assignments(
        db_session,
        settings=settings,
        reader=SiteServiceRequestBitrixReader(api),
        writer=SiteServiceRequestBitrixWriter(api),
        now=datetime(2026, 8, 23, 8, 1, tzinfo=UTC),
    )
    health = build_site_service_request_health(db_session, settings=settings)

    db_session.refresh(case)
    assert results[0]["deliveryRetried"] is True
    assert case.assignment_last_error_code is None
    assert health["status"] == "healthy"
    assert health["assignment_failures"] == 0


@pytest.mark.parametrize(
    "malformed_stage",
    [55, True, ["DT1134_55:NEW"], {"value": "DT1134_55:NEW"}],
)
def test_assignment_reconcile_rejects_non_string_stage_without_saving_it(
    db_session,
    malformed_stage: object,
) -> None:
    case = _case(bitrix_item_id=1000)
    case.last_open_stage_id = "DT1134_55:PREPARATION"
    db_session.add(case)
    db_session.commit()
    api = FakeBitrixApi()
    api.items[1000] = {"stageId": malformed_stage}
    settings = _worker_settings(site_service_requests_ingest_enabled=True)

    results = reconcile_site_service_request_assignments(
        db_session,
        settings=settings,
        reader=SiteServiceRequestBitrixReader(api),
        writer=SiteServiceRequestBitrixWriter(api),
        now=datetime(2026, 8, 23, 8, 1, tzinfo=UTC),
    )

    db_session.refresh(case)
    assert results[0]["errorCode"] == "assignment_reconcile_failed"
    assert case.assignment_last_error_code == "assignment_reconcile_failed"
    assert case.last_open_stage_id == "DT1134_55:PREPARATION"


def test_unexpected_timeman_error_checkpoints_health_before_reraise(db_session) -> None:
    case = _case(bitrix_item_id=1000)
    db_session.add(case)
    db_session.commit()

    class UnexpectedTimemanApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.items[1000] = {"stageId": "DT1134_55:NEW"}
            self.fail_timeman = True

        def call(self, method: str, params=None, **kwargs):
            if method == "timeman.status" and self.fail_timeman:
                self.fail_timeman = False
                raise ValueError("unexpected timeman payload")
            return super().call(method, params, **kwargs)

    api = UnexpectedTimemanApi()
    settings = _worker_settings(site_service_requests_ingest_enabled=True)
    with pytest.raises(ValueError, match="unexpected timeman payload"):
        reconcile_site_service_request_assignments(
            db_session,
            settings=settings,
            reader=SiteServiceRequestBitrixReader(api),
            writer=SiteServiceRequestBitrixWriter(api),
            now=datetime(2026, 8, 23, 8, 0, tzinfo=UTC),
        )

    degraded = build_site_service_request_health(db_session, settings=settings)
    assert degraded["status"] == "degraded"
    assert degraded["alert_codes"] == ["assignment_failure"]

    reconcile_site_service_request_assignments(
        db_session,
        settings=settings,
        reader=SiteServiceRequestBitrixReader(api),
        writer=SiteServiceRequestBitrixWriter(api),
        now=datetime(2026, 8, 23, 8, 1, tzinfo=UTC),
    )
    recovered = build_site_service_request_health(db_session, settings=settings)
    assert recovered["status"] == "healthy"
    assert recovered["assignment_failures"] == 0


def test_escalation_recovers_after_notification_failure_without_duplicate_timeline(
    db_session,
) -> None:
    case = _case(bitrix_item_id=1000)
    case.first_response_due_at = datetime(2026, 8, 23, 7, 0, tzinfo=UTC)
    db_session.add(case)
    db_session.commit()

    class NotificationFailureApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.items[1000] = {"stageId": "DT1134_55:NEW"}
            self.fail_notification = True

        def call(self, method: str, params=None, **kwargs):
            if method == "im.notify.personal.add" and self.fail_notification:
                self.fail_notification = False
                # Simulate an ambiguous timeout after Bitrix accepted the tagged
                # notification. A retry with the same TAG must not create another.
                super().call(method, params, **kwargs)
                raise RuntimeError("notification timeout")
            return super().call(method, params, **kwargs)

    api = NotificationFailureApi()
    settings = _worker_settings()
    writer = SiteServiceRequestBitrixWriter(api)
    first = reconcile_site_service_request_assignments(
        db_session,
        settings=settings,
        reader=SiteServiceRequestBitrixReader(api),
        writer=writer,
        now=datetime(2026, 8, 23, 8, 0, tzinfo=UTC),
    )
    second = reconcile_site_service_request_assignments(
        db_session,
        settings=settings,
        reader=SiteServiceRequestBitrixReader(api),
        writer=writer,
        now=datetime(2026, 8, 23, 8, 1, tzinfo=UTC),
    )

    db_session.refresh(case)
    assert first[0]["errorCode"] == "assignment_reconcile_failed"
    assert second[0]["escalated"] is False
    assert len(api.timeline_comments) == 1
    assert [method for method, _params in api.calls].count("im.notify.personal.add") == 2
    assert api.notification_ids_by_tag == {
        f"mm-site-service-escalation:{case.id}:1003": 1,
    }
    assert case.escalation_timeline_delivered_at is not None
    assert case.escalation_notification_delivered_at is not None


@pytest.mark.parametrize(
    "malformed_result",
    [None, False, 0, "", "0", 1.5, "accepted", [1], {"id": 1}],
)
def test_notification_rejects_malformed_success_result(
    malformed_result: object,
) -> None:
    class MalformedNotificationApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method == "im.notify.personal.add":
                return {"result": malformed_result}
            return super().call(method, params, **kwargs)

    with pytest.raises(RuntimeError, match="bitrix_notification_failed"):
        SiteServiceRequestBitrixWriter(MalformedNotificationApi()).notify_user(
            user_id=1003,
            message="Тестовое уведомление",
            tag="mm-site-service-test:1:1003",
        )


def test_pending_escalation_delivery_retries_after_first_response(db_session) -> None:
    case = _case(bitrix_item_id=1000)
    case.assigned_user_id = 1003
    case.assignment_state = "escalated"
    case.escalated_at = datetime(2026, 8, 23, 7, 0, tzinfo=UTC)
    case.first_response_at = datetime(2026, 8, 23, 7, 30, tzinfo=UTC)
    db_session.add(case)
    db_session.commit()
    api = FakeBitrixApi()
    api.items[1000] = {"stageId": "DT1134_55:WORK"}

    results = reconcile_site_service_request_assignments(
        db_session,
        settings=_worker_settings(),
        reader=SiteServiceRequestBitrixReader(api),
        writer=SiteServiceRequestBitrixWriter(api),
        now=datetime(2026, 8, 23, 8, 0, tzinfo=UTC),
    )

    db_session.refresh(case)
    assert results[0]["deliveryRetried"] is True
    assert case.escalation_timeline_delivered_at is not None
    assert case.escalation_notification_delivered_at is not None
    assert len(api.timeline_comments) == 1


def test_timeline_writer_uses_dynamic_entity_type_and_follows_pagination() -> None:
    api = FakeBitrixApi()
    api.timeline_page_size = 1
    api.timeline_comments = [
        {"ENTITY_TYPE": "dynamic_1134", "ENTITY_ID": "1000", "COMMENT": "marker"},
        {"ENTITY_TYPE": "dynamic_1134", "ENTITY_ID": "1000", "COMMENT": "newer"},
    ]
    writer = SiteServiceRequestBitrixWriter(api)

    assert writer.timeline_comment_exists(
        entity_type_id=1134,
        item_id=1000,
        marker="marker",
    )
    writer.add_timeline_comment(
        entity_type_id=1134,
        item_id=1000,
        comment="contract",
    )

    list_calls = [params for method, params in api.calls if method == "crm.timeline.comment.list"]
    add_call = next(params for method, params in api.calls if method == "crm.timeline.comment.add")
    assert dict(list_calls[0])["filter[ENTITY_TYPE]"] == "dynamic_1134"
    assert dict(list_calls[1])["start"] == "1"
    assert dict(add_call)["fields[ENTITY_TYPE]"] == "dynamic_1134"


def test_timeline_writer_follows_nested_result_next() -> None:
    class NestedNextApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method == "crm.timeline.comment.list":
                values = list(params or [])
                self.calls.append((method, values))
                start = int(dict(values).get("start") or 0)
                if start == 0:
                    return {"result": {"items": [{"COMMENT": "newer"}], "next": 1}}
                return {"result": {"items": [{"COMMENT": "nested marker"}]}}
            return super().call(method, params, **kwargs)

    writer = SiteServiceRequestBitrixWriter(NestedNextApi())

    assert writer.timeline_comment_exists(
        entity_type_id=1134,
        item_id=1000,
        marker="nested marker",
    )


def test_timeline_writer_rejects_conflicting_offsets() -> None:
    class ConflictingNextApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method == "crm.timeline.comment.list":
                return {"result": {"items": [], "next": 1}, "next": 2}
            return super().call(method, params, **kwargs)

    with pytest.raises(RuntimeError, match="bitrix_timeline_pagination_invalid"):
        SiteServiceRequestBitrixWriter(ConflictingNextApi()).timeline_comment_exists(
            entity_type_id=1134,
            item_id=1000,
            marker="missing marker",
        )


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"result": None},
        {"result": {}},
        {"result": {"items": [], "comments": []}},
        {"result": {"items": None}},
        {"result": {"comments": 1}},
        {"result": {"items": ["unknown-row"]}},
        {"result": [{"ID": "1"}]},
        {"result": [{"COMMENT": ["missing marker"]}]},
        {"result": [{"COMMENT": {"text": "missing marker"}}]},
        {"result": [{"COMMENT": "one", "comment": "two"}]},
    ],
)
def test_timeline_writer_rejects_malformed_pages(response: dict) -> None:
    class MalformedTimelineApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method == "crm.timeline.comment.list":
                return response
            return super().call(method, params, **kwargs)

    with pytest.raises(RuntimeError, match="bitrix_timeline_readback_invalid"):
        SiteServiceRequestBitrixWriter(MalformedTimelineApi()).timeline_comment_exists(
            entity_type_id=1134,
            item_id=1000,
            marker="missing marker",
        )


def test_timeline_writer_rejects_repeated_offset_fail_closed() -> None:
    class RepeatedOffsetApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method == "crm.timeline.comment.list":
                self.calls.append((method, list(params or [])))
                return {"result": {"items": [], "next": 0}}
            return super().call(method, params, **kwargs)

    with pytest.raises(RuntimeError, match="bitrix_timeline_pagination_cycle"):
        SiteServiceRequestBitrixWriter(RepeatedOffsetApi()).timeline_comment_exists(
            entity_type_id=1134,
            item_id=1000,
            marker="missing marker",
        )


def test_timeline_writer_rejects_more_than_100_pages_fail_closed() -> None:
    class EndlessPaginationApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method == "crm.timeline.comment.list":
                values = list(params or [])
                self.calls.append((method, values))
                start = int(dict(values).get("start") or 0)
                return {"result": {"items": [], "next": start + 1}}
            return super().call(method, params, **kwargs)

    api = EndlessPaginationApi()
    with pytest.raises(RuntimeError, match="bitrix_timeline_pagination_limit"):
        SiteServiceRequestBitrixWriter(api).timeline_comment_exists(
            entity_type_id=1134,
            item_id=1000,
            marker="missing marker",
        )
    assert len(api.calls) == 100


def test_item_lookup_rejects_malformed_and_conflicting_pagination() -> None:
    class MalformedItemApi(FakeBitrixApi):
        def __init__(self, response: dict) -> None:
            super().__init__()
            self.response = response

        def call(self, method: str, params=None, **kwargs):
            if method == "crm.item.list":
                return self.response
            return super().call(method, params, **kwargs)

    writer = SiteServiceRequestBitrixWriter(MalformedItemApi({"result": []}))
    with pytest.raises(RuntimeError, match="bitrix_item_readback_invalid"):
        writer._find_items(
            entity_type_id=1134,
            idempotency_field="UF_BACKEND_KEY",
            idempotency_key="key",
        )

    writer = SiteServiceRequestBitrixWriter(
        MalformedItemApi({"result": {"items": [], "next": 1}, "next": 2})
    )
    with pytest.raises(RuntimeError, match="bitrix_item_pagination_invalid"):
        writer._find_items(
            entity_type_id=1134,
            idempotency_field="UF_BACKEND_KEY",
            idempotency_key="key",
        )

    writer = SiteServiceRequestBitrixWriter(
        MalformedItemApi(
            {
                "result": {
                    "items": [
                        {
                            "id": "1000",
                            "ID": "1001",
                        }
                    ]
                }
            }
        )
    )
    with pytest.raises(RuntimeError, match="bitrix_item_readback_invalid"):
        writer._find_items(
            entity_type_id=1134,
            idempotency_field="UF_BACKEND_KEY",
            idempotency_key="key",
        )


def test_disk_lookup_rejects_wrong_name_and_pagination() -> None:
    wrong_name_api = FakeBitrixApi()
    wrong_name_api.disk_files["expected.bin"] = {
        "ID": "2000",
        "NAME": "different.bin",
    }
    with pytest.raises(RuntimeError, match="bitrix_file_readback_invalid"):
        SiteServiceRequestBitrixWriter(wrong_name_api).upload_file(
            folder_id=777,
            deterministic_name="expected.bin",
            content=b"payload",
        )

    class PaginatedDiskApi(FakeBitrixApi):
        def call(self, method: str, params=None, **kwargs):
            if method == "disk.folder.getchildren":
                return {
                    "result": [{"ID": "2000", "NAME": "expected.bin"}],
                    "next": 50,
                }
            return super().call(method, params, **kwargs)

    with pytest.raises(RuntimeError, match="bitrix_file_readback_ambiguous"):
        SiteServiceRequestBitrixWriter(PaginatedDiskApi()).upload_file(
            folder_id=777,
            deterministic_name="expected.bin",
            content=b"payload",
        )

    conflicting_id_api = FakeBitrixApi()
    conflicting_id_api.disk_files["expected.bin"] = {
        "ID": "2000",
        "id": "2001",
        "NAME": "expected.bin",
    }
    with pytest.raises(RuntimeError, match="bitrix_file_readback_invalid"):
        SiteServiceRequestBitrixWriter(conflicting_id_api).upload_file(
            folder_id=777,
            deterministic_name="expected.bin",
            content=b"payload",
        )


def test_hidden_support_note_does_not_close_first_response_sla(db_session) -> None:
    cipher = _persist_event(db_session)
    api = FakeBitrixApi()
    reader = SiteServiceRequestBitrixReader(api)
    settings = _worker_settings()
    initial = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
    )
    apply_site_service_request_worker_plans(
        db_session,
        plans=initial,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    payload = _event_payload()
    payload["eventId"] = "site-support:741:1301"
    payload["eventType"] = "ticket.message_added"
    payload["history"].append(
        {
            "messageId": 1301,
            "authorKind": "support-team",
            "isVisibleToCustomer": False,
            "createdAt": "2026-08-22T11:00:00+03:00",
            "text": "Внутренняя заметка",
            "files": [],
        }
    )
    _persist_payload(db_session, payload, cipher)
    plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
    )
    apply_site_service_request_worker_plans(
        db_session,
        plans=plans,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )

    case = db_session.scalar(select(SiteServiceRequestCase))
    assert case is not None and case.first_response_at is None


def test_permanent_failure_counter_is_consecutive_and_resets_on_transient_failure(
    db_session,
) -> None:
    _persist_event(db_session)
    event = db_session.scalar(select(SiteServiceRequestEvent))
    assert event is not None
    started_at = worker_module._as_utc(event.updated_at) + timedelta(minutes=1)

    for attempt in range(4):
        result = worker_module._record_site_service_request_failure(
            db_session,
            event_id=event.event_id,
            error_code="permanent_failure",
            permanent=True,
            now=started_at + timedelta(minutes=attempt),
        )
        db_session.commit()
        assert result.status == "retry"
    db_session.refresh(event)
    assert event.consecutive_permanent_failures == 4

    worker_module._record_site_service_request_failure(
        db_session,
        event_id=event.event_id,
        error_code="temporary_failure",
        permanent=False,
        now=started_at + timedelta(minutes=5),
    )
    db_session.commit()
    db_session.refresh(event)
    assert event.consecutive_permanent_failures == 0

    for attempt in range(5):
        result = worker_module._record_site_service_request_failure(
            db_session,
            event_id=event.event_id,
            error_code="permanent_failure",
            permanent=True,
            now=started_at + timedelta(minutes=6 + attempt),
        )
        db_session.commit()
    assert result.status == "needs_attention"
    db_session.refresh(event)
    assert event.consecutive_permanent_failures == 5


def test_stale_failure_checkpoint_does_not_regress_processed_event(db_session) -> None:
    _persist_event(db_session)
    event = db_session.scalar(select(SiteServiceRequestEvent))
    assert event is not None
    processed_at = datetime(2026, 8, 22, 8, 0, tzinfo=UTC)
    event.status = "processed"
    event.payload_encrypted = None
    event.processed_at = processed_at
    event.updated_at = processed_at
    db_session.commit()

    result = worker_module._record_site_service_request_failure(
        db_session,
        event_id=event.event_id,
        error_code="stale_failure",
        permanent=True,
        now=processed_at - timedelta(minutes=1),
    )

    assert result.status == "processed"
    db_session.refresh(event)
    assert event.status == "processed"
    assert event.last_error_code is None


def test_worker_clears_stale_deal_and_order_reference_with_readback(db_session) -> None:
    cipher = _persist_event(db_session)
    api = FakeBitrixApi()
    reader = SiteServiceRequestBitrixReader(api)
    settings = _worker_settings()
    initial = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
    )
    apply_site_service_request_worker_plans(
        db_session,
        plans=initial,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )
    case = db_session.scalar(select(SiteServiceRequestCase))
    assert case is not None and case.bitrix_item_id is not None
    assert api.items[int(case.bitrix_item_id)]["ufCrm36Crmdeal"] == "701"

    payload = _event_payload()
    payload["eventId"] = "site-support:741:1301"
    payload["eventType"] = "ticket.updated"
    payload["ticket"]["orderNumber"] = None
    payload["history"].append(
        {
            "messageId": 1301,
            "authorKind": "customer",
            "createdAt": "2026-08-22T11:00:00+03:00",
            "text": "Заказ не относится к обращению",
            "files": [],
        }
    )
    _persist_payload(db_session, payload, cipher)
    plans = build_site_service_request_worker_plans(
        db_session,
        settings=settings,
        reader=reader,
        cipher=cipher,
    )
    apply_site_service_request_worker_plans(
        db_session,
        plans=plans,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )

    assert api.items[int(case.bitrix_item_id)]["ufCrm36Crmdeal"] == ""
    assert api.items[int(case.bitrix_item_id)]["ufCrm36Orderrefs"] == ""


def test_worker_rejects_missing_deal_and_order_fields_during_clear_readback(
    db_session,
) -> None:
    cipher = _persist_event(db_session)

    class MissingLinkReadbackApi(FakeBitrixApi):
        def __init__(self) -> None:
            super().__init__()
            self.omit_link_fields = False

        def call(self, method: str, params=None, **kwargs):
            response = super().call(method, params, **kwargs)
            if method == "crm.item.get" and self.omit_link_fields:
                response["result"]["item"].pop("ufCrm36Crmdeal", None)
                response["result"]["item"].pop("ufCrm36Orderrefs", None)
            return response

    api = MissingLinkReadbackApi()
    settings = _worker_settings()
    reader = SiteServiceRequestBitrixReader(api)
    initial = build_site_service_request_worker_plans(
        db_session, settings=settings, reader=reader, cipher=cipher
    )
    apply_site_service_request_worker_plans(
        db_session,
        plans=initial,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )

    payload = _event_payload()
    payload["eventId"] = "site-support:741:1301"
    payload["eventType"] = "ticket.updated"
    payload["ticket"]["orderNumber"] = None
    payload["history"].append(
        {
            "messageId": 1301,
            "authorKind": "customer",
            "createdAt": "2026-08-22T11:00:00+03:00",
            "text": "Заказ не относится к обращению",
            "files": [],
        }
    )
    _persist_payload(db_session, payload, cipher)
    api.omit_link_fields = True
    plans = build_site_service_request_worker_plans(
        db_session, settings=settings, reader=reader, cipher=cipher
    )
    results = apply_site_service_request_worker_plans(
        db_session,
        plans=plans,
        settings=settings,
        reader=reader,
        writer=SiteServiceRequestBitrixWriter(api),
        cipher=cipher,
    )

    assert results[0].status == "retry"
    assert results[0].error_code == "bitrix_unavailable"
