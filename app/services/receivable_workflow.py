from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models import ReceivableCase, ReceivableSmsLog, ReceivableWorkEvent, ReceivableWorkItem
from app.services.expertise_bitrix import BitrixRestClient
from app.services.receivables import CASE_BUYERS, CASE_OVERDUE

STATUS_NEW_DEBT = "new_debt"
STATUS_WAITING_PAYMENT = "waiting_payment"
STATUS_SMS_SENT = "sms_sent"
STATUS_NO_PHONE = "no_phone"
STATUS_CALLING = "calling"
STATUS_PROMISED_PAYMENT = "promised_payment"
STATUS_DISPUTE = "dispute_check"
STATUS_ESCALATED = "escalated"
STATUS_DATA_QUALITY = "data_quality_error"
STATUS_CLOSED = "closed"

SMS_PLANNED = "planned"
SMS_DRY_RUN = "dry_run"
SMS_SENT = "sent"
SMS_FAILED = "failed"
SMS_SKIPPED_NO_PHONE = "skipped_no_phone"

EVENT_CREATED = "created"
EVENT_UPDATED = "updated_from_onec"
EVENT_AMOUNT_CHANGED = "amount_changed"
EVENT_SMS_LOGGED = "sms_logged"
EVENT_NO_PHONE = "no_phone"
EVENT_ESCALATED = "escalated"
EVENT_CLOSED = "closed_by_onec"
EVENT_BITRIX_SYNC_ERROR = "bitrix_sync_error"
EVENT_DATA_QUALITY = "skipped_data_quality"


class ReceivableBitrixClient(Protocol):
    def add_smart_process_item(
        self,
        *,
        entity_type_id: int,
        fields: dict[str, Any],
    ) -> tuple[str, str | None]: ...

    def update_smart_process_item(
        self,
        *,
        entity_type_id: int,
        item_id: str,
        fields: dict[str, Any],
    ) -> None: ...

    def list_items_by_ref(
        self,
        *,
        entity_type_id: int,
        ref_field: str,
        ref_value: str,
    ) -> list[dict[str, Any]]: ...


class SmsAdapter(Protocol):
    def send(self, *, phone: str, text: str, idempotency_key: str) -> str | None: ...


@dataclass
class ReceivableWorkflowSummary:
    sms_created: int = 0
    sms_reused: int = 0
    sms_dry_run: int = 0
    sms_sent: int = 0
    sms_failed: int = 0
    sms_skipped_no_phone: int = 0
    work_items_created: int = 0
    work_items_updated: int = 0
    work_items_closed: int = 0
    bitrix_created: int = 0
    bitrix_updated: int = 0
    bitrix_errors: int = 0
    data_quality_skipped: int = 0
    events_created: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sms_created": self.sms_created,
            "sms_reused": self.sms_reused,
            "sms_dry_run": self.sms_dry_run,
            "sms_sent": self.sms_sent,
            "sms_failed": self.sms_failed,
            "sms_skipped_no_phone": self.sms_skipped_no_phone,
            "work_items_created": self.work_items_created,
            "work_items_updated": self.work_items_updated,
            "work_items_closed": self.work_items_closed,
            "bitrix_created": self.bitrix_created,
            "bitrix_updated": self.bitrix_updated,
            "bitrix_errors": self.bitrix_errors,
            "data_quality_skipped": self.data_quality_skipped,
            "events_created": self.events_created,
            "errors": list(self.errors),
        }


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def stable_key_for_counterparty(counterparty_ref: str) -> str:
    return f"receivables|buyers|{counterparty_ref}"


def debt_key_for_case(case: ReceivableCase) -> str:
    document_part = (
        case.origin_document_ref
        or case.origin_document_number
        or (case.origin_document_date.date().isoformat() if case.origin_document_date else "no-doc")
    )
    return f"{stable_key_for_counterparty(case.counterparty_ref)}|{document_part}"


def debt_age_days(case: ReceivableCase, *, as_of: date) -> int | None:
    if case.origin_document_date is None:
        return None
    return max((as_of - case.origin_document_date.date()).days, 0)


def needs_sms_on_date(case: ReceivableCase, *, as_of: date) -> bool:
    return debt_age_days(case, as_of=as_of) == 6


def needs_call_on_date(case: ReceivableCase, *, as_of: date) -> bool:
    age = debt_age_days(case, as_of=as_of)
    return age is not None and age >= 7


def _sms_text(case: ReceivableCase) -> str:
    name = (case.counterparty_name or "Добрый день").split()[0]
    number = case.origin_document_number or case.origin_document_ref or "-"
    amount = f"{case.current_balance:.2f}".rstrip("0").rstrip(".")
    return (
        f"{name}, добрый день!\n"
        f"Завтра истекает срок оплаты по заказу №{number}.\n"
        f"Сумма к оплате: {amount} руб.\n"
        "Напоминаем, что при наличии просрочки платежа отгрузки будут приостановлены "
        "и не возобновляются до отдельного распоряжения руководства.\n"
        "Благодарим за сотрудничество компания Master Mobile."
    )


def _normalize_phone(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _case_sort_key(case: ReceivableCase) -> tuple[int, date, str]:
    age = case.overdue_days or 0
    origin_date = case.origin_document_date.date() if case.origin_document_date else date.min
    return (age, origin_date, case.counterparty_ref)


def _select_current_case(cases: list[ReceivableCase]) -> ReceivableCase:
    overdue = [item for item in cases if item.segment == CASE_OVERDUE]
    return sorted(overdue or cases, key=_case_sort_key, reverse=True)[0]


def _is_workflow_case_eligible(case: ReceivableCase) -> bool:
    return case.origin_document_date is not None and bool(
        case.origin_document_ref or case.origin_document_number
    )


def _group_cases(cases: list[ReceivableCase]) -> dict[str, list[ReceivableCase]]:
    grouped: dict[str, list[ReceivableCase]] = {}
    for case in cases:
        grouped.setdefault(case.counterparty_ref, []).append(case)
    return grouped


def _append_event(
    session: Session,
    *,
    item: ReceivableWorkItem,
    event_type: str,
    comment: str | None = None,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    source: str = "automation",
    summary: ReceivableWorkflowSummary | None = None,
) -> ReceivableWorkEvent | None:
    event_key = (
        None if idempotency_key is None else f"{item.stable_key}|{event_type}|{idempotency_key}"
    )
    if event_key is not None:
        existing = session.scalar(
            select(ReceivableWorkEvent).where(ReceivableWorkEvent.idempotency_key == event_key)
        )
        if existing is not None:
            return None
    event = ReceivableWorkEvent(
        work_item=item,
        event_type=event_type,
        event_at=utcnow(),
        source=source,
        comment=comment,
        payload=payload,
        idempotency_key=event_key,
    )
    session.add(event)
    if summary is not None:
        summary.events_created += 1
    return event


def _latest_sms_for_item(
    session: Session,
    *,
    item: ReceivableWorkItem,
) -> ReceivableSmsLog | None:
    return session.scalar(
        select(ReceivableSmsLog)
        .where(ReceivableSmsLog.stable_key == item.stable_key)
        .order_by(ReceivableSmsLog.business_date.desc(), ReceivableSmsLog.id.desc())
    )


def _sync_item_sms_state(session: Session, *, item: ReceivableWorkItem) -> None:
    session.flush()
    logs = (
        session.execute(
            select(ReceivableSmsLog).where(
                ReceivableSmsLog.stable_key == item.stable_key,
                ReceivableSmsLog.work_item_id.is_(None),
            )
        )
        .scalars()
        .all()
    )
    for log in logs:
        log.work_item = item
    latest = _latest_sms_for_item(session, item=item)
    if latest is not None:
        item.last_sms_status = latest.status
        item.last_sms_error = latest.error
        item.last_sms_at = latest.sent_at or datetime.combine(latest.business_date, time.min)


def plan_receivable_sms(
    session: Session,
    *,
    as_of: date,
    phone_by_counterparty: dict[str, str] | None = None,
    settings: Settings | None = None,
    sms_adapter: SmsAdapter | None = None,
    summary: ReceivableWorkflowSummary | None = None,
) -> ReceivableWorkflowSummary:
    settings = settings or get_settings()
    summary = summary or ReceivableWorkflowSummary()
    phone_by_counterparty = phone_by_counterparty or {}
    cases = (
        session.execute(
            select(ReceivableCase)
            .where(
                ReceivableCase.snapshot_date == as_of,
                ReceivableCase.segment == CASE_BUYERS,
            )
            .order_by(ReceivableCase.counterparty_ref)
        )
        .scalars()
        .all()
    )
    for case in cases:
        if not needs_sms_on_date(case, as_of=as_of):
            continue
        debt_key = debt_key_for_case(case)
        existing = session.scalar(
            select(ReceivableSmsLog).where(
                ReceivableSmsLog.debt_key == debt_key,
                ReceivableSmsLog.business_date == as_of,
            )
        )
        if existing is not None:
            summary.sms_reused += 1
            continue

        stable_key = stable_key_for_counterparty(case.counterparty_ref)
        phone = _normalize_phone(phone_by_counterparty.get(case.counterparty_ref))
        item = session.scalar(
            select(ReceivableWorkItem).where(ReceivableWorkItem.stable_key == stable_key)
        )
        idempotency_key = f"sms|{debt_key}|{as_of.isoformat()}"
        message_text = _sms_text(case)
        status = SMS_PLANNED
        error = None
        sent_at = None
        if not phone:
            status = SMS_SKIPPED_NO_PHONE
            error = "Нет телефона"
            summary.sms_skipped_no_phone += 1
        elif settings.receivable_sms_mode == "live":
            if sms_adapter is None:
                status = SMS_FAILED
                error = "Live SMS adapter is not configured"
                summary.sms_failed += 1
            else:
                try:
                    sms_adapter.send(
                        phone=phone, text=message_text, idempotency_key=idempotency_key
                    )
                    status = SMS_SENT
                    sent_at = utcnow()
                    summary.sms_sent += 1
                except Exception as exc:  # noqa: BLE001
                    status = SMS_FAILED
                    error = str(exc)[:1000]
                    summary.sms_failed += 1
        else:
            status = SMS_DRY_RUN
            summary.sms_dry_run += 1

        log = ReceivableSmsLog(
            work_item=item,
            stable_key=stable_key,
            counterparty_ref=case.counterparty_ref,
            debt_key=debt_key,
            business_date=as_of,
            phone=phone,
            status=status,
            message_text=message_text,
            error=error,
            sent_at=sent_at,
            idempotency_key=idempotency_key,
        )
        session.add(log)
        summary.sms_created += 1
        if item is not None:
            item.last_sms_status = status
            item.last_sms_error = error
            item.last_sms_at = sent_at or datetime.combine(as_of, time.min)
            _append_event(
                session,
                item=item,
                event_type=EVENT_SMS_LOGGED,
                comment="SMS зафиксирована в outbox.",
                payload={"status": status, "phone": phone, "error": error},
                idempotency_key=idempotency_key,
                summary=summary,
            )
    return summary


def _resolve_assignment(
    *,
    case: ReceivableCase,
    settings: Settings,
    status: str,
) -> tuple[int | None, str | None]:
    if status == STATUS_ESCALATED and settings.receivable_retail_network_head_user_id:
        return settings.receivable_retail_network_head_user_id, "retail_network_head"
    return None, None


def _resolve_status(
    *,
    item: ReceivableWorkItem,
    case: ReceivableCase,
    as_of: date,
) -> str:
    if item.status == STATUS_DISPUTE:
        return STATUS_DISPUTE
    if (case.overdue_days or 0) >= 15:
        return STATUS_ESCALATED
    if item.phone_status == "missing":
        return STATUS_NO_PHONE
    if needs_call_on_date(case, as_of=as_of):
        return STATUS_CALLING
    if item.last_sms_status in {SMS_DRY_RUN, SMS_SENT}:
        return STATUS_SMS_SENT
    return STATUS_NEW_DEBT


def _item_title(item: ReceivableWorkItem) -> str:
    name = item.counterparty_name or item.counterparty_ref
    amount = f"{item.current_balance:.2f}".rstrip("0").rstrip(".")
    return f"Дебиторка: {name} / {amount} руб."


def _bitrix_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _format_money(value: Any) -> str:
    try:
        amount = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return str(value or "")
    formatted = f"{amount:,.2f}".replace(",", " ")
    return formatted.rstrip("0").rstrip(".")


def _format_document_date(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)
    return parsed.strftime("%d.%m.%Y %H:%M")


def _chain_document_label(event_type: str | None) -> str:
    return {
        "sale": "Реализация",
        "return": "Возврат",
        "payment": "Оплата",
        "settlement": "Взаимозачет",
        "debt_adjustment": "Корректировка",
    }.get(str(event_type or ""), "Документ")


def format_chain_documents_for_bitrix(documents: list[dict[str, Any]] | None) -> str:
    if not documents:
        return ""
    lines: list[str] = []
    for index, document in enumerate(documents, start=1):
        label = _chain_document_label(document.get("event_type"))
        number = document.get("document_number") or "без номера"
        document_date = _format_document_date(document.get("document_date"))
        amount = _format_money(document.get("amount_delta"))
        parts = [f"{index}. {label} {number}"]
        if document_date:
            parts.append(f"от {document_date}")
        if amount:
            parts.append(f"на {amount} руб.")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _build_bitrix_fields(
    *,
    item: ReceivableWorkItem,
    settings: Settings,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}

    def put(alias: str, value: Any) -> None:
        field_name = settings.receivable_bitrix_field_map.get(alias)
        if field_name:
            fields[field_name] = _bitrix_value(value)

    put("title", _item_title(item))
    put("stable_key", item.stable_key)
    put("counterparty_ref", item.counterparty_ref)
    put("counterparty_name", item.counterparty_name)
    put("current_balance", item.current_balance)
    put("origin_document_number", item.origin_document_number)
    put("origin_document_date", item.origin_document_date)
    put("due_date", item.due_date)
    put("overdue_days", item.overdue_days)
    put("age_days", item.age_days)
    put("manager_name", item.current_manager_name or item.origin_manager_name)
    put("phone", item.phone)
    put("phone_status", item.phone_status)
    put("status", item.status)
    put("sms_status", item.last_sms_status)
    put("last_sms_at", item.last_sms_at)
    put("needs_call_today", item.needs_call_today)
    put("promised_payment_date", item.promised_payment_date)
    put("next_action_date", item.next_action_date)
    put("last_contact_comment", item.last_contact_comment)
    put("escalation_level", item.escalation_level)
    put("chain_documents", format_chain_documents_for_bitrix(item.chain_documents))
    put("source", "pricing-service")

    fields.setdefault("title", _item_title(item))
    stage_id = settings.receivable_bitrix_stage_map.get(item.status)
    if stage_id:
        fields["stageId"] = stage_id
        item.bitrix_stage_id = stage_id
    if settings.receivable_bitrix_category_id is not None:
        fields["categoryId"] = settings.receivable_bitrix_category_id
    assigned_by_field = settings.receivable_bitrix_field_map.get("assigned_by")
    if assigned_by_field and item.assigned_bitrix_user_id is not None:
        fields[assigned_by_field] = item.assigned_bitrix_user_id
    elif item.assigned_bitrix_user_id is not None:
        fields["assignedById"] = item.assigned_bitrix_user_id
    return fields


def _sync_bitrix_item(
    *,
    item: ReceivableWorkItem,
    settings: Settings,
    client: ReceivableBitrixClient | None,
    summary: ReceivableWorkflowSummary,
    dry_run_bitrix: bool,
) -> None:
    if dry_run_bitrix or client is None or settings.receivable_bitrix_entity_type_id is None:
        return
    fields = _build_bitrix_fields(item=item, settings=settings)
    try:
        if item.bitrix_item_id:
            client.update_smart_process_item(
                entity_type_id=settings.receivable_bitrix_entity_type_id,
                item_id=str(item.bitrix_item_id),
                fields=fields,
            )
            summary.bitrix_updated += 1
        else:
            stable_key_field = settings.receivable_bitrix_field_map.get("stable_key")
            if not stable_key_field:
                raise RuntimeError("RECEIVABLE_BITRIX_FIELD_MAP не содержит stable_key")
            matches = client.list_items_by_ref(
                entity_type_id=settings.receivable_bitrix_entity_type_id,
                ref_field=stable_key_field,
                ref_value=item.stable_key,
            )
            if len(matches) > 1:
                raise RuntimeError(
                    f"В Bitrix найдено несколько карточек с stable_key={item.stable_key}"
                )
            if matches:
                match = matches[0]
                item.bitrix_item_id = int(match.get("id") or match.get("ID"))
                item.bitrix_detail_url = match.get("detailUrl") or match.get("DETAIL_URL")
                client.update_smart_process_item(
                    entity_type_id=settings.receivable_bitrix_entity_type_id,
                    item_id=str(item.bitrix_item_id),
                    fields=fields,
                )
                summary.bitrix_updated += 1
            else:
                item_id, detail_url = client.add_smart_process_item(
                    entity_type_id=settings.receivable_bitrix_entity_type_id,
                    fields=fields,
                )
                item.bitrix_item_id = int(item_id)
                item.bitrix_detail_url = detail_url
                summary.bitrix_created += 1
        item.bitrix_last_sync_at = utcnow()
        item.bitrix_last_error = None
    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:1000]
        item.bitrix_last_error = error
        summary.bitrix_errors += 1
        summary.errors.append(f"{item.stable_key}: {error}")


def _mark_data_quality_issue(
    session: Session,
    *,
    item: ReceivableWorkItem | None,
    counterparty_ref: str,
    case: ReceivableCase,
    as_of: date,
    summary: ReceivableWorkflowSummary,
) -> None:
    summary.data_quality_skipped += 1
    message = "Долг пропущен: нет документа возникновения долга из 1С."
    summary.errors.append(f"{stable_key_for_counterparty(counterparty_ref)}: {message}")
    if item is None:
        return
    if item.status == STATUS_CLOSED or item.closed_at is not None:
        item.status = STATUS_CLOSED
        item.current_balance = Decimal("0")
        return
    item.status = STATUS_DATA_QUALITY
    item.bitrix_last_error = message
    _append_event(
        session,
        item=item,
        event_type=EVENT_DATA_QUALITY,
        comment=message,
        payload={
            "snapshot_date": as_of.isoformat(),
            "counterparty_ref": counterparty_ref,
            "current_balance": str(case.current_balance),
            "overdue_days": case.overdue_days,
        },
        idempotency_key=f"{as_of.isoformat()}|data_quality",
        summary=summary,
    )


def _update_work_item_from_case(
    session: Session,
    *,
    item: ReceivableWorkItem,
    case: ReceivableCase,
    as_of: date,
    phone_by_counterparty: dict[str, str],
    settings: Settings,
    summary: ReceivableWorkflowSummary,
) -> None:
    old_balance = item.current_balance
    old_status = item.status
    old_debt_key = item.current_debt_key
    debt_key = debt_key_for_case(case)
    phone = _normalize_phone(phone_by_counterparty.get(case.counterparty_ref) or item.phone)

    item.counterparty_ref = case.counterparty_ref
    item.counterparty_name = case.counterparty_name
    item.current_debt_key = debt_key
    item.current_balance = case.current_balance
    item.origin_document_ref = case.origin_document_ref
    item.origin_document_number = case.origin_document_number
    item.origin_document_date = case.origin_document_date
    item.due_date = case.due_date
    item.overdue_days = case.overdue_days
    item.age_days = debt_age_days(case, as_of=as_of)
    item.origin_manager_ref = case.origin_manager_ref
    item.origin_manager_name = case.origin_manager_name
    item.current_manager_ref = case.current_manager_ref
    item.current_manager_name = case.current_manager_name
    item.phone = phone
    item.phone_status = "present" if phone else "missing"
    item.needs_call_today = needs_call_on_date(case, as_of=as_of)
    item.chain_documents = case.chain_documents or []
    item.payload = {
        "snapshot_date": case.snapshot_date.isoformat(),
        "segment": case.segment,
        "aged_bucket": case.aged_bucket,
        "activity_segment": case.activity_segment,
        "payment_term_source": case.payment_term_source,
        "shipment_ban": case.shipment_ban,
    }
    _sync_item_sms_state(session, item=item)
    item.status = _resolve_status(item=item, case=case, as_of=as_of)
    assigned_user_id, assigned_source = _resolve_assignment(
        case=case, settings=settings, status=item.status
    )
    item.assigned_bitrix_user_id = assigned_user_id
    item.assigned_source = assigned_source
    if item.status == STATUS_ESCALATED and item.escalated_at is None:
        item.escalated_at = utcnow()
        item.escalation_level = "retail_network_head"

    if old_debt_key != debt_key:
        _append_event(
            session,
            item=item,
            event_type=EVENT_UPDATED,
            comment="Рабочая карточка обновлена из 1С.",
            payload={"debt_key": debt_key},
            idempotency_key=f"{as_of.isoformat()}|{debt_key}",
            summary=summary,
        )
    if old_balance != item.current_balance:
        _append_event(
            session,
            item=item,
            event_type=EVENT_AMOUNT_CHANGED,
            comment="Изменилась сумма долга по данным 1С.",
            payload={
                "old_balance": str(old_balance),
                "new_balance": str(item.current_balance),
            },
            idempotency_key=f"{as_of.isoformat()}|{debt_key}|amount",
            summary=summary,
        )
    if item.phone_status == "missing":
        _append_event(
            session,
            item=item,
            event_type=EVENT_NO_PHONE,
            comment="SMS не отправлена: нет телефона клиента.",
            payload={"counterparty_ref": item.counterparty_ref},
            idempotency_key=f"{as_of.isoformat()}|{debt_key}|no_phone",
            summary=summary,
        )
    if old_status != item.status and item.status == STATUS_ESCALATED:
        _append_event(
            session,
            item=item,
            event_type=EVENT_ESCALATED,
            comment="Просрочка достигла 15 дней, карточка передана руководителю розничной сети.",
            payload={"overdue_days": item.overdue_days},
            idempotency_key=f"{as_of.isoformat()}|{debt_key}|escalated",
            summary=summary,
        )


def sync_receivable_workflow(
    session: Session,
    *,
    as_of: date,
    phone_by_counterparty: dict[str, str] | None = None,
    settings: Settings | None = None,
    bitrix_client: ReceivableBitrixClient | None = None,
    sms_adapter: SmsAdapter | None = None,
    dry_run_bitrix: bool = False,
) -> ReceivableWorkflowSummary:
    settings = settings or get_settings()
    phone_by_counterparty = phone_by_counterparty or {}
    summary = ReceivableWorkflowSummary()
    plan_receivable_sms(
        session,
        as_of=as_of,
        phone_by_counterparty=phone_by_counterparty,
        settings=settings,
        sms_adapter=sms_adapter,
        summary=summary,
    )

    cases = (
        session.execute(
            select(ReceivableCase)
            .where(ReceivableCase.snapshot_date == as_of)
            .order_by(ReceivableCase.counterparty_ref)
        )
        .scalars()
        .all()
    )
    allow_stale_closure = bool(cases)
    grouped = _group_cases(cases)
    active_refs: set[str] = set()
    protected_stable_keys: set[str] = set()
    for counterparty_ref, counterparty_cases in grouped.items():
        segments = {item.segment for item in counterparty_cases}
        if CASE_BUYERS not in segments or CASE_OVERDUE not in segments:
            continue
        case = _select_current_case(counterparty_cases)
        stable_key = stable_key_for_counterparty(counterparty_ref)
        if not _is_workflow_case_eligible(case):
            item = session.scalar(
                select(ReceivableWorkItem).where(ReceivableWorkItem.stable_key == stable_key)
            )
            if item is not None and item.status != STATUS_CLOSED:
                protected_stable_keys.add(stable_key)
            _mark_data_quality_issue(
                session,
                item=item,
                counterparty_ref=counterparty_ref,
                case=case,
                as_of=as_of,
                summary=summary,
            )
            continue
        active_refs.add(counterparty_ref)
        item = session.scalar(
            select(ReceivableWorkItem).where(ReceivableWorkItem.stable_key == stable_key)
        )
        created = False
        if item is None:
            item = ReceivableWorkItem(
                stable_key=stable_key,
                counterparty_ref=counterparty_ref,
                counterparty_name=case.counterparty_name,
                status=STATUS_NEW_DEBT,
                current_balance=Decimal("0"),
            )
            session.add(item)
            session.flush()
            created = True
            summary.work_items_created += 1
            _append_event(
                session,
                item=item,
                event_type=EVENT_CREATED,
                comment="Создана рабочая карточка дебиторки.",
                payload={"snapshot_date": as_of.isoformat()},
                idempotency_key=f"{as_of.isoformat()}|created",
                summary=summary,
            )
        else:
            summary.work_items_updated += 1
        if item.status == STATUS_CLOSED:
            item.closed_at = None
        _update_work_item_from_case(
            session,
            item=item,
            case=case,
            as_of=as_of,
            phone_by_counterparty=phone_by_counterparty,
            settings=settings,
            summary=summary,
        )
        _sync_bitrix_item(
            item=item,
            settings=settings,
            client=bitrix_client,
            summary=summary,
            dry_run_bitrix=dry_run_bitrix,
        )
        if item.bitrix_last_error:
            _append_event(
                session,
                item=item,
                event_type=EVENT_BITRIX_SYNC_ERROR,
                comment=item.bitrix_last_error,
                payload={"bitrix_item_id": item.bitrix_item_id},
                idempotency_key=f"{as_of.isoformat()}|{item.current_debt_key}|bitrix_error",
                summary=summary,
            )
        if created:
            _sync_item_sms_state(session, item=item)

    active_stable_keys = {stable_key_for_counterparty(ref) for ref in active_refs}
    protected_stable_keys.update(active_stable_keys)
    if not allow_stale_closure:
        return summary

    open_items = (
        session.execute(
            select(ReceivableWorkItem).where(ReceivableWorkItem.status != STATUS_CLOSED)
        )
        .scalars()
        .all()
    )
    for item in open_items:
        if item.stable_key in protected_stable_keys:
            continue
        old_balance = item.current_balance
        item.status = STATUS_CLOSED
        item.current_balance = Decimal("0")
        item.closed_at = utcnow()
        item.needs_call_today = False
        summary.work_items_closed += 1
        _append_event(
            session,
            item=item,
            event_type=EVENT_CLOSED,
            comment="Долг закрыт по данным 1С.",
            payload={"old_balance": str(old_balance)},
            idempotency_key=f"{as_of.isoformat()}|closed",
            summary=summary,
        )
        _sync_bitrix_item(
            item=item,
            settings=settings,
            client=bitrix_client,
            summary=summary,
            dry_run_bitrix=dry_run_bitrix,
        )

    return summary


def build_bitrix_client_from_settings(settings: Settings | None = None) -> BitrixRestClient | None:
    settings = settings or get_settings()
    if not settings.receivable_bitrix_webhook_url or not settings.receivable_bitrix_entity_type_id:
        return None
    return BitrixRestClient(settings.receivable_bitrix_webhook_url)
