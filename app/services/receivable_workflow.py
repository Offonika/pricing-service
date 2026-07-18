from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models import ReceivableCase, ReceivableSmsLog, ReceivableWorkEvent, ReceivableWorkItem
from app.services.expertise_bitrix import BitrixRestClient
from app.services.receivable_workplace_cache import load_cached_open_debt_documents
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
STATUS_NO_ANSWER = "no_answer"
STATUS_CALL_BACK = "call_back"
STATUS_INTERVENTION_REQUIRED = "intervention_required"
STATUS_NOT_OURS_TRANSFER = "not_ours_transfer"
STATUS_REMIND = "remind"
STATUS_PAID = "paid"
STATUS_TRANSFER = "transfer"
STATUS_ON_CARD_ROUTE = "on_card_route"
WORKPLACE_MANUAL_STATUS_VALUES = {
    STATUS_WAITING_PAYMENT,
    STATUS_PROMISED_PAYMENT,
    STATUS_NO_ANSWER,
    STATUS_CALL_BACK,
    STATUS_INTERVENTION_REQUIRED,
    STATUS_NOT_OURS_TRANSFER,
    STATUS_REMIND,
    STATUS_PAID,
    STATUS_TRANSFER,
    STATUS_ON_CARD_ROUTE,
}
WORKPLACE_PAYLOAD_KEYS = {
    "contacted_staff_ref",
    "contacted_staff_name",
    "payment_postponed",
    "payment_postponed_count",
    "workplace_last_action_at",
}

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
    bitrix_conflicts: int = 0
    bitrix_errors: int = 0
    closure_deferred: int = 0
    data_quality_skipped: int = 0
    events_created: int = 0
    processed_counterparty_refs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def merge(self, other: ReceivableWorkflowSummary) -> None:
        for field_name in (
            "sms_created",
            "sms_reused",
            "sms_dry_run",
            "sms_sent",
            "sms_failed",
            "sms_skipped_no_phone",
            "work_items_created",
            "work_items_updated",
            "work_items_closed",
            "bitrix_created",
            "bitrix_updated",
            "bitrix_conflicts",
            "bitrix_errors",
            "closure_deferred",
            "data_quality_skipped",
            "events_created",
        ):
            setattr(self, field_name, getattr(self, field_name) + getattr(other, field_name))
        self.processed_counterparty_refs.extend(other.processed_counterparty_refs)
        self.errors.extend(other.errors)

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
            "bitrix_conflicts": self.bitrix_conflicts,
            "bitrix_errors": self.bitrix_errors,
            "closure_deferred": self.closure_deferred,
            "data_quality_skipped": self.data_quality_skipped,
            "events_created": self.events_created,
            "processed_counterparty_refs": list(self.processed_counterparty_refs),
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


def _due_date(case: ReceivableCase) -> date | None:
    if case.due_date is None:
        return None
    return case.due_date.date()


def _workflow_due_datetime(case: ReceivableCase) -> datetime | None:
    if case.due_date is not None:
        return case.due_date
    if case.planned_payment_date is not None:
        return case.planned_payment_date
    if case.origin_document_date is None:
        return None
    credit_depth_days = case.credit_depth_days
    if credit_depth_days is None or credit_depth_days < 0:
        credit_depth_days = 7
    return case.origin_document_date + timedelta(days=credit_depth_days)


def needs_sms_on_date(case: ReceivableCase, *, as_of: date) -> bool:
    due_date = _due_date(case)
    return due_date is not None and (due_date - as_of).days == 1


def needs_call_on_date(case: ReceivableCase, *, as_of: date) -> bool:
    due_date = _due_date(case)
    return due_date is not None and as_of > due_date


def _is_workflow_card_due(case: ReceivableCase, *, as_of: date) -> bool:
    due_at = _workflow_due_datetime(case)
    return due_at is not None and case.current_balance > 0 and as_of > due_at.date()


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


def _protect_existing_open_item(
    session: Session,
    *,
    stable_key: str,
    protected_stable_keys: set[str],
) -> None:
    item = session.scalar(
        select(ReceivableWorkItem).where(ReceivableWorkItem.stable_key == stable_key)
    )
    if item is not None and item.status != STATUS_CLOSED:
        protected_stable_keys.add(stable_key)


def _normalize_department_name(value: str | None) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _department_scope_enabled(settings: Settings) -> bool:
    return bool(settings.receivable_workflow_department_refs) or bool(
        settings.receivable_workflow_department_names
    )


def _case_matches_department_scope(case: ReceivableCase, settings: Settings) -> bool:
    if not _department_scope_enabled(settings):
        return True
    refs = {str(item).strip() for item in settings.receivable_workflow_department_refs if item}
    names = {
        _normalize_department_name(item)
        for item in settings.receivable_workflow_department_names
        if item
    }
    return bool(
        (case.department_ref and str(case.department_ref).strip() in refs)
        or (_normalize_department_name(case.department_name) in names)
    )


def _work_item_matches_department_scope(item: ReceivableWorkItem, settings: Settings) -> bool:
    if not _department_scope_enabled(settings):
        return True
    refs = {str(value).strip() for value in settings.receivable_workflow_department_refs if value}
    names = {
        _normalize_department_name(value)
        for value in settings.receivable_workflow_department_names
        if value
    }
    return bool(
        (item.department_ref and str(item.department_ref).strip() in refs)
        or (_normalize_department_name(item.department_name) in names)
    )


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
        if not _case_matches_department_scope(case, settings):
            continue
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
    if item.status in WORKPLACE_MANUAL_STATUS_VALUES:
        return item.status
    if (item.overdue_days or 0) >= 15:
        return STATUS_ESCALATED
    if item.phone_status == "missing":
        return STATUS_NO_PHONE
    if item.needs_call_today:
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


def _bitrix_enum_value(settings: Settings, alias: str, value: Any) -> Any:
    if value in (None, ""):
        return value
    enum_map = settings.receivable_bitrix_enum_map.get(alias) or {}
    return enum_map.get(str(value), value)


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


def _open_debt_rule_label(value: Any) -> str:
    labels = {
        "statement_direct_payment_match": "закрыто ближайшей оплатой",
        "statement_multi_sale_payment_match": "группа закрыта одной оплатой",
        "statement_bottom_up_balance_cutoff": "подбор от текущего остатка",
        "statement_unmatched_open_sale": "нет закрывающего документа",
        "statement_structure_confirmed_open": "подтверждено структурой 1С",
        "confirmed_open": "подтверждено структурой 1С",
    }
    raw = str(value or "").strip()
    return labels.get(raw, raw)


def format_chain_documents_for_bitrix(documents: list[dict[str, Any]] | None) -> str:
    if not documents:
        return ""
    lines: list[str] = []
    for index, document in enumerate(documents, start=1):
        has_open_debt_shape = any(
            key in document
            for key in (
                "open_amount",
                "sale_amount",
                "gross_amount",
                "closing_amount",
                "return_amount",
                "statement_selection_rule",
            )
        )
        label = (
            _chain_document_label(document.get("event_type"))
            if document.get("event_type")
            else ("Открытый долг" if has_open_debt_shape else "Документ")
        )
        number = document.get("document_number") or "без номера"
        document_date = _format_document_date(document.get("document_date"))
        amount = _format_money(
            document.get("amount_delta")
            or document.get("open_amount")
            or document.get("sale_amount")
            or document.get("gross_amount")
        )
        parts = [f"{index}. {label} {number}"]
        if document_date:
            parts.append(f"от {document_date}")
        if amount:
            parts.append(f"на {amount} руб.")
        if has_open_debt_shape:
            gross_amount = _format_money(
                document.get("gross_amount") or document.get("sale_amount")
            )
            closing_amount = _format_money(document.get("closing_amount"))
            return_amount = _format_money(document.get("return_amount"))
            rule = _open_debt_rule_label(
                document.get("statement_selection_rule")
                or document.get("document_structure_status")
            )
            details = []
            if gross_amount:
                details.append(f"исходно {gross_amount} руб.")
            if closing_amount:
                details.append(f"закрытия {closing_amount} руб.")
            if return_amount:
                details.append(f"возвраты {return_amount} руб.")
            if rule:
                details.append(f"правило: {rule}")
            if details:
                parts.append(f"({'; '.join(details)})")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _build_bitrix_fields(
    *,
    item: ReceivableWorkItem,
    settings: Settings,
    bitrix_documents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}

    def put(alias: str, value: Any) -> None:
        field_name = settings.receivable_bitrix_field_map.get(alias)
        if field_name:
            fields[field_name] = _bitrix_value(value)

    def put_enum(alias: str, value: Any) -> None:
        put(alias, _bitrix_enum_value(settings, alias, value))

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
    put("department_ref", item.department_ref)
    put("department_name", item.department_name)
    put("manager_name", item.current_manager_name or item.origin_manager_name)
    put("phone", item.phone)
    put_enum("phone_status", item.phone_status)
    put("status", item.status)
    put_enum("sms_status", item.last_sms_status)
    put("last_sms_at", item.last_sms_at)
    put("needs_call_today", item.needs_call_today)
    put("promised_payment_date", item.promised_payment_date)
    put("next_action_date", item.next_action_date)
    put("last_contact_comment", item.last_contact_comment)
    put_enum("escalation_level", item.escalation_level)
    put(
        "chain_documents",
        format_chain_documents_for_bitrix(
            item.chain_documents if bitrix_documents is None else bitrix_documents
        ),
    )
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
    bitrix_documents: list[dict[str, Any]] | None = None,
) -> None:
    if dry_run_bitrix or client is None or settings.receivable_bitrix_entity_type_id is None:
        return
    fields = _build_bitrix_fields(
        item=item,
        settings=settings,
        bitrix_documents=bitrix_documents,
    )
    try:
        if item.bitrix_item_id:
            if not item.bitrix_detail_url:
                item.bitrix_detail_url = (
                    f"/crm/type/{settings.receivable_bitrix_entity_type_id}/"
                    f"details/{item.bitrix_item_id}/"
                )
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
                summary.bitrix_conflicts += 1
                raise RuntimeError(
                    f"В Bitrix найдено несколько карточек с stable_key={item.stable_key}"
                )
            if matches:
                match = matches[0]
                item.bitrix_item_id = int(match.get("id") or match.get("ID"))
                item.bitrix_detail_url = (
                    match.get("detailUrl")
                    or match.get("DETAIL_URL")
                    or (
                        f"/crm/type/{settings.receivable_bitrix_entity_type_id}/"
                        f"details/{item.bitrix_item_id}/"
                    )
                )
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
                item.bitrix_detail_url = detail_url or (
                    f"/crm/type/{settings.receivable_bitrix_entity_type_id}/"
                    f"details/{item.bitrix_item_id}/"
                )
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
    effective_due_at = _workflow_due_datetime(case)
    item.due_date = effective_due_at
    item.overdue_days = (
        max((as_of - effective_due_at.date()).days, 0) if effective_due_at is not None else None
    )
    item.age_days = debt_age_days(case, as_of=as_of)
    item.origin_manager_ref = case.origin_manager_ref
    item.origin_manager_name = case.origin_manager_name
    item.current_manager_ref = case.current_manager_ref
    item.current_manager_name = case.current_manager_name
    item.department_ref = case.department_ref
    item.department_name = case.department_name
    item.phone = phone
    item.phone_status = "present" if phone else "missing"
    item.needs_call_today = bool(item.overdue_days and item.overdue_days > 0)
    item.chain_documents = case.chain_documents or []
    preserved_payload = {}
    if isinstance(item.payload, dict):
        preserved_payload = {
            key: item.payload.get(key) for key in WORKPLACE_PAYLOAD_KEYS if key in item.payload
        }
    item.payload = {
        "snapshot_date": case.snapshot_date.isoformat(),
        "segment": case.segment,
        "aged_bucket": case.aged_bucket,
        "activity_segment": case.activity_segment,
        "payment_term_source": case.payment_term_source,
        "shipment_ban": case.shipment_ban,
        **preserved_payload,
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


def _workflow_candidate_groups(
    grouped: dict[str, list[ReceivableCase]],
    *,
    as_of: date,
    settings: Settings,
) -> list[tuple[str, list[ReceivableCase]]]:
    candidates: list[tuple[str, list[ReceivableCase]]] = []
    for counterparty_ref, counterparty_cases in grouped.items():
        segments = {item.segment for item in counterparty_cases}
        if CASE_BUYERS not in segments or CASE_OVERDUE not in segments:
            continue
        case = _select_current_case(counterparty_cases)
        if not _case_matches_department_scope(case, settings):
            continue
        if not _is_workflow_card_due(case, as_of=as_of):
            continue
        candidates.append((counterparty_ref, counterparty_cases))
    return sorted(
        candidates,
        key=lambda entry: (
            not _is_workflow_case_eligible(_select_current_case(entry[1])),
            -_select_current_case(entry[1]).current_balance,
            entry[0],
        ),
    )


def _previous_snapshot_active_stable_keys(
    session: Session,
    *,
    as_of: date,
) -> set[str] | None:
    previous_date = session.scalar(
        select(func.max(ReceivableCase.snapshot_date)).where(
            ReceivableCase.snapshot_date < as_of,
            ReceivableCase.segment == CASE_BUYERS,
        )
    )
    if previous_date is None:
        return None
    previous_cases = (
        session.execute(select(ReceivableCase).where(ReceivableCase.snapshot_date == previous_date))
        .scalars()
        .all()
    )
    previous_grouped = _group_cases(previous_cases)
    return {
        stable_key_for_counterparty(counterparty_ref)
        for counterparty_ref, counterparty_cases in previous_grouped.items()
        if CASE_BUYERS in {item.segment for item in counterparty_cases}
        and _is_workflow_card_due(_select_current_case(counterparty_cases), as_of=previous_date)
    }


def close_stale_receivable_work_items(
    session: Session,
    *,
    as_of: date,
    settings: Settings,
    bitrix_client: ReceivableBitrixClient | None,
    summary: ReceivableWorkflowSummary,
    current_active_stable_keys: set[str],
    previous_active_stable_keys: set[str] | None = None,
    dry_run_bitrix: bool = False,
) -> None:
    previous_active = previous_active_stable_keys
    if previous_active is None:
        previous_active = _previous_snapshot_active_stable_keys(session, as_of=as_of)
    if previous_active is None:
        previous_active = set()
    open_items = (
        session.execute(
            select(ReceivableWorkItem).where(ReceivableWorkItem.status != STATUS_CLOSED)
        )
        .scalars()
        .all()
    )
    for item in open_items:
        if item.stable_key in current_active_stable_keys:
            continue
        if item.stable_key in previous_active:
            summary.closure_deferred += 1
            continue
        if _department_scope_enabled(settings) and not _work_item_matches_department_scope(
            item, settings
        ):
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


def sync_receivable_workflow(
    session: Session,
    *,
    as_of: date,
    phone_by_counterparty: dict[str, str] | None = None,
    settings: Settings | None = None,
    bitrix_client: ReceivableBitrixClient | None = None,
    sms_adapter: SmsAdapter | None = None,
    dry_run_bitrix: bool = False,
    sync_sms: bool = True,
    allow_closure: bool = True,
    only_counterparty_refs: Sequence[str] | set[str] | frozenset[str] | None = None,
    full_active_selection: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> ReceivableWorkflowSummary:
    settings = settings or get_settings()
    phone_by_counterparty = phone_by_counterparty or {}
    summary = ReceivableWorkflowSummary()
    if sync_sms:
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
    open_debt_documents_by_counterparty = load_cached_open_debt_documents(
        session,
        snapshot_date=as_of,
        counterparty_refs=list(grouped),
    ).documents_by_counterparty
    candidates = _workflow_candidate_groups(grouped, as_of=as_of, settings=settings)
    if only_counterparty_refs is not None:
        ordered_refs = [str(value).strip() for value in only_counterparty_refs if value]
        candidates = []
        for counterparty_ref in ordered_refs:
            counterparty_cases = grouped.get(counterparty_ref)
            if not counterparty_cases:
                continue
            if CASE_BUYERS not in {item.segment for item in counterparty_cases}:
                continue
            case = _select_current_case(counterparty_cases)
            if case.current_balance <= 0 or not _case_matches_department_scope(case, settings):
                continue
            candidates.append((counterparty_ref, counterparty_cases))
    start = max(offset, 0)
    stop = None if limit is None else start + max(limit, 0)
    selected_refs = {entry[0] for entry in candidates[start:stop]}
    active_refs: set[str] = set()
    protected_stable_keys: set[str] = set()
    for counterparty_ref, counterparty_cases in grouped.items():
        segments = {item.segment for item in counterparty_cases}
        if CASE_BUYERS not in segments:
            continue
        stable_key = stable_key_for_counterparty(counterparty_ref)
        case = _select_current_case(counterparty_cases)
        if not _case_matches_department_scope(case, settings):
            continue
        if counterparty_ref not in selected_refs:
            continue
        if only_counterparty_refs is None and not _is_workflow_card_due(case, as_of=as_of):
            continue
        protected_stable_keys.add(stable_key)
        summary.processed_counterparty_refs.append(counterparty_ref)
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
            bitrix_documents=open_debt_documents_by_counterparty.get(
                str(counterparty_ref or "").strip().casefold()
            ),
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
    selection_limited = (
        (only_counterparty_refs is not None and not full_active_selection)
        or limit is not None
        or offset > 0
    )
    if not allow_stale_closure or not allow_closure or selection_limited:
        return summary

    close_stale_receivable_work_items(
        session,
        as_of=as_of,
        settings=settings,
        bitrix_client=bitrix_client,
        summary=summary,
        current_active_stable_keys=protected_stable_keys,
        dry_run_bitrix=dry_run_bitrix,
    )

    return summary


def build_bitrix_client_from_settings(settings: Settings | None = None) -> BitrixRestClient | None:
    settings = settings or get_settings()
    if not settings.receivable_bitrix_webhook_url or not settings.receivable_bitrix_entity_type_id:
        return None
    return BitrixRestClient(settings.receivable_bitrix_webhook_url)
