from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.core.config import Settings
from app.models.site_service_requests import (
    SiteServiceRequestCase,
    SiteServiceRequestCommand,
    SiteServiceRequestEvent,
    SiteServiceRequestFile,
)
from app.schemas.site_service_requests import SiteServiceRequestEventPayload
from app.services.site_service_requests import (
    SiteServiceRequestCipher,
    accept_site_service_request_event,
)
from app.services.site_service_requests_auth import content_sha256
from app.services.site_service_requests_worker import (
    SiteServiceRequestBitrixReader,
    SiteServiceRequestBitrixWriter,
    apply_site_service_request_worker_plans,
    build_site_service_request_worker_plans,
    cleanup_uploaded_site_service_request_files,
    choose_site_service_assignee,
    collect_site_service_request_outbound_commands,
    contains_exact_order_token,
    create_site_service_request_command,
    decide_site_service_assignment,
    next_site_service_request_retry_at,
    normalize_site_service_email,
    normalize_site_service_phone,
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
        self.next_item_id = 1000
        self.items: dict[int, dict] = {}
        self.raise_after_item_add = False
        self.disk_files: dict[str, dict] = {}

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
            self.contacts[contact_id] = {"ID": str(contact_id), "ACTIVE": "Y"}
            return {"result": contact_id}
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
            return {"result": {"item": {"id": item_id, **self.items[item_id]}}}
        if method == "disk.folder.getchildren":
            name = mapped.get("filter[NAME]")
            item = self.disk_files.get(str(name))
            return {"result": [item] if item else []}
        if method == "crm.timeline.comment.add":
            return {"result": 1}
        if method == "im.notify.personal.add":
            return {"result": 1}
        if method == "timeman.status":
            user_id = int(mapped["USER_ID"])
            return {"result": {"STATUS": self.timeman.get(user_id, "ERROR")}}
        raise AssertionError(f"unexpected Bitrix method: {method}")

    def call_json(self, method: str, payload: dict, **_kwargs):
        if method != "disk.folder.uploadfile":
            raise AssertionError(f"unexpected Bitrix JSON method: {method}")
        name = str(payload["data"]["NAME"])
        item = {"ID": str(2000 + len(self.disk_files)), "DETAIL_URL": "/disk/file"}
        self.disk_files[name] = item
        return {"result": item}


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
            "request_type_warranty": "WARRANTY",
        },
        "site_service_requests_bitrix_root_folder_id": 777,
        "site_service_requests_crm_order_field": "UF_CRM_ORDER",
        "site_service_requests_first_line_user_ids": [1001, 1002],
        "site_service_requests_escalation_user_id": 1003,
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
    db_session.commit()

    results = sync_staged_site_service_request_files(
        db_session,
        settings=settings,
        writer=SiteServiceRequestBitrixWriter(api),
        cleanup_paths=(cleanup_paths := []),
    )
    assert path.exists() is True
    db_session.commit()
    cleanup_uploaded_site_service_request_files(cleanup_paths)

    assert results[0]["status"] == "uploaded"
    db_session.refresh(file)
    assert file.status == "uploaded"
    assert file.bitrix_object_id == "2000"
    assert file.temporary_path is None
    assert path.exists() is False
    assert api.items[int(case.bitrix_item_id)]["ufCrm36Clientfiles"] == ["2000"]


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
    api.items[int(case.bitrix_item_id)].update(
        {
            "ufSiteReplyAction": "SEND",
            "ufSiteReplyText": "Ответ из карточки",
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
            "authorKind": "support-team",
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
        now=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
        failure_results=failures,
        failure_writer=SiteServiceRequestBitrixWriter(api),
    )

    assert plans == []
    assert len(failures) == 1
    assert failures[0].status == "retry"
    event = db_session.scalar(select(SiteServiceRequestEvent))
    assert event is not None and event.status == "retry"
    assert event.last_error_code == "bitrix_unavailable"


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
