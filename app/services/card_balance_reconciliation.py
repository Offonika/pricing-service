from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.core.config import Settings, get_settings
from app.models import (
    CardBalanceCashbox,
    CardBalanceReconciliation,
    CardBalanceReconciliationEvent,
)
from app.services.card_balance_onec import clean_string, decimal_or_none

STATUS_MATCHED = "matched"
STATUS_MISMATCH = "mismatch"
STATUS_LOW_CONFIDENCE = "low_confidence"
STATUS_STALE_SCREENSHOT = "stale_screenshot"
STATUS_MISSING_SCREENSHOT = "missing_screenshot"
STATUS_MISSING_ONEC_BALANCE = "missing_onec_balance"
STATUS_UNMAPPED_CARD = "unmapped_card"
STATUS_AMBIGUOUS_MAPPING = "ambiguous_mapping"
STATUS_CLOSED_FINCONTROL = "closed_fincontrol"
STATUS_CANCELLED = "cancelled"

EXCEPTION_STATUSES = {
    STATUS_MISMATCH,
    STATUS_LOW_CONFIDENCE,
    STATUS_STALE_SCREENSHOT,
    STATUS_MISSING_SCREENSHOT,
    STATUS_MISSING_ONEC_BALANCE,
    STATUS_UNMAPPED_CARD,
    STATUS_AMBIGUOUS_MAPPING,
}
TERMINAL_STATUSES = {STATUS_MATCHED, STATUS_CLOSED_FINCONTROL, STATUS_CANCELLED}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _http_error(status: int, detail: Any) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


def _event_key(reconciliation_id: int, event_type: str, idempotency_key: str | None) -> str | None:
    if not idempotency_key:
        return None
    return f"{idempotency_key}:{reconciliation_id}:{event_type}"


def append_event(
    session: Session,
    row: CardBalanceReconciliation,
    *,
    event_type: str,
    source: str = "backend",
    actor_external_id: str | None = None,
    comment: str | None = None,
    meta: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> CardBalanceReconciliationEvent:
    key = _event_key(row.id, event_type, idempotency_key)
    if key:
        existing = session.scalar(
            select(CardBalanceReconciliationEvent).where(
                CardBalanceReconciliationEvent.idempotency_key == key
            )
        )
        if existing is not None:
            return existing
    event = CardBalanceReconciliationEvent(
        reconciliation_id=row.id,
        event_type=event_type,
        event_at=utcnow(),
        actor_external_id=actor_external_id,
        source=source,
        comment=comment,
        meta=_json_safe(meta) if isinstance(meta, dict) else meta,
        idempotency_key=key,
    )
    session.add(event)
    return event


def serialize_cashbox(row: CardBalanceCashbox) -> dict[str, Any]:
    return {
        "id": row.id,
        "onec_cashbox_ref_hex": row.onec_cashbox_ref_hex,
        "onec_cashbox_code": row.onec_cashbox_code,
        "onec_cashbox_name": row.onec_cashbox_name,
        "currency_code": row.currency_code,
        "currency_name": row.currency_name,
        "card_last4": row.card_last4,
        "store_name": row.store_name,
        "employee_last_name": row.employee_last_name,
        "employee_id": row.employee_id,
        "is_active": row.is_active,
        "needs_manual_review": row.needs_manual_review,
        "review_reason": row.review_reason,
        "last_seen_at": row.last_seen_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def serialize_reconciliation(row: CardBalanceReconciliation) -> dict[str, Any]:
    return {
        "id": row.id,
        "external_id": row.external_id,
        "business_date": row.business_date,
        "cashbox_id": row.cashbox_id,
        "employee_id": row.employee_id,
        "employee_name": row.employee_name,
        "employee_last_name": row.employee_last_name,
        "card_last4": row.card_last4,
        "onec_cashbox_code": row.onec_cashbox_code,
        "onec_cashbox_name": row.onec_cashbox_name,
        "source_channel": row.source_channel,
        "bitrix_item_id": row.bitrix_item_id,
        "bitrix_stage_id": row.bitrix_stage_id,
        "screenshot_file_id": row.screenshot_file_id,
        "submitted_at": row.submitted_at,
        "screenshot_taken_at": row.screenshot_taken_at,
        "manual_balance": row.manual_balance,
        "recognized_balance": row.recognized_balance,
        "recognition_confidence": row.recognition_confidence,
        "onec_balance_at": row.onec_balance_at,
        "onec_balance": row.onec_balance,
        "diff_amount": row.diff_amount,
        "status": row.status,
        "reviewer_id": row.reviewer_id,
        "resolution_comment": row.resolution_comment,
        "resolved_at": row.resolved_at,
        "due_at": row.due_at,
        "bitrix_last_sync_at": row.bitrix_last_sync_at,
        "bitrix_last_error": row.bitrix_last_error,
        "payload": row.payload,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def serialize_event(row: CardBalanceReconciliationEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "reconciliation_id": row.reconciliation_id,
        "event_type": row.event_type,
        "event_at": row.event_at,
        "actor_external_id": row.actor_external_id,
        "source": row.source,
        "comment": row.comment,
        "meta": row.meta,
        "created_at": row.created_at,
    }


def _normalize_last_name(value: str | None) -> str | None:
    normalized = clean_string(value)
    if normalized and " " in normalized:
        normalized = normalized.split()[-1]
    return normalized.lower().replace("ё", "е") if normalized else None


def resolve_cashbox(
    session: Session,
    *,
    onec_cashbox_code: str | None = None,
    card_last4: str | None = None,
    employee_last_name: str | None = None,
) -> tuple[CardBalanceCashbox | None, str | None]:
    code = clean_string(onec_cashbox_code)
    if code:
        row = session.scalar(
            select(CardBalanceCashbox).where(
                CardBalanceCashbox.onec_cashbox_code == code,
                CardBalanceCashbox.is_active.is_(True),
            )
        )
        return (row, None) if row is not None else (None, STATUS_UNMAPPED_CARD)

    last4 = clean_string(card_last4)
    if not last4:
        return None, STATUS_UNMAPPED_CARD
    candidates = session.scalars(
        select(CardBalanceCashbox).where(
            CardBalanceCashbox.card_last4 == last4,
            CardBalanceCashbox.is_active.is_(True),
        )
    ).all()
    employee_norm = _normalize_last_name(employee_last_name)
    if employee_norm:
        candidates = [
            item
            for item in candidates
            if _normalize_last_name(item.employee_last_name) == employee_norm
        ]
    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) > 1:
        return None, STATUS_AMBIGUOUS_MAPPING
    return None, STATUS_UNMAPPED_CARD


def _mapping_error_reason(mapping_error: str) -> str:
    if mapping_error == STATUS_AMBIGUOUS_MAPPING:
        return (
            "Не удалось однозначно привязать карточку Bitrix: найдено несколько касс "
            "с такими последними 4 цифрами карты."
        )
    return (
        "Не удалось привязать карточку Bitrix к кассе 1С или сотруднику. "
        "Заполните дату, кассу 1С либо сотрудника и последние 4 цифры карты."
    )


def resolve_status(
    *,
    screenshot_file_id: str | None,
    business_date: date,
    balance_value: Decimal | None,
    onec_balance: Decimal | None,
    diff_amount: Decimal | None,
    mapping_error: str | None,
    settings: Settings | None = None,
    today: date | None = None,
) -> str:
    settings = settings or get_settings()
    if mapping_error:
        return mapping_error
    if not clean_string(screenshot_file_id):
        return STATUS_MISSING_SCREENSHOT
    if balance_value is None:
        return STATUS_LOW_CONFIDENCE
    current_day = today or utcnow().date()
    if business_date < current_day - timedelta(days=settings.card_balance_max_stale_days):
        return STATUS_STALE_SCREENSHOT
    if onec_balance is None:
        return STATUS_MISSING_ONEC_BALANCE
    if diff_amount is None:
        return STATUS_MISSING_ONEC_BALANCE
    tolerance = Decimal(str(settings.card_balance_tolerance_rub))
    if abs(diff_amount) <= tolerance:
        return STATUS_MATCHED
    return STATUS_MISMATCH


def status_from_bitrix_stage(stage_id: str | None, settings: Settings | None = None) -> str | None:
    normalized = clean_string(stage_id)
    if not normalized:
        return None
    settings = settings or get_settings()
    stage_to_status = {
        clean_string(settings.card_balance_bitrix_stage_map.get("closed_fincontrol")): (
            STATUS_CLOSED_FINCONTROL
        ),
        clean_string(settings.card_balance_bitrix_stage_map.get("cancelled")): STATUS_CANCELLED,
    }
    return stage_to_status.get(normalized)


def upsert_cashboxes(session: Session, rows: list[dict[str, Any]]) -> dict[str, int]:
    created = 0
    updated = 0
    now = utcnow()
    for item in rows:
        code = clean_string(item.get("onec_cashbox_code"))
        name = clean_string(item.get("onec_cashbox_name"))
        if not code or not name:
            continue
        row = session.scalar(
            select(CardBalanceCashbox).where(CardBalanceCashbox.onec_cashbox_code == code)
        )
        if row is None:
            row = CardBalanceCashbox(onec_cashbox_code=code, onec_cashbox_name=name)
            session.add(row)
            created += 1
        else:
            updated += 1
        row.onec_cashbox_ref_hex = clean_string(item.get("onec_cashbox_ref_hex"))
        row.onec_cashbox_name = name
        row.currency_code = clean_string(item.get("currency_code"))
        row.currency_name = clean_string(item.get("currency_name"))
        row.card_last4 = clean_string(item.get("card_last4"))
        row.store_name = clean_string(item.get("store_name"))
        row.employee_last_name = clean_string(item.get("employee_last_name"))
        employee_id = clean_string(item.get("employee_id"))
        if employee_id:
            row.employee_id = employee_id
        row.is_active = bool(item.get("is_active", True))
        row.needs_manual_review = bool(item.get("needs_manual_review", False))
        row.review_reason = clean_string(item.get("review_reason"))
        row.last_seen_at = now
        row.payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
    session.commit()
    return {"created": created, "updated": updated, "total": created + updated}


def list_reconciliations(
    session: Session,
    *,
    status: str | None = None,
    exception_only: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    stmt = select(CardBalanceReconciliation).order_by(
        CardBalanceReconciliation.business_date.desc(),
        CardBalanceReconciliation.id.desc(),
    )
    if status:
        stmt = stmt.where(CardBalanceReconciliation.status == status)
    elif exception_only:
        stmt = stmt.where(CardBalanceReconciliation.status.in_(EXCEPTION_STATUSES))
    rows = session.scalars(stmt.limit(limit)).all()
    return [serialize_reconciliation(row) for row in rows]


def get_reconciliation(session: Session, reconciliation_id: int) -> dict[str, Any]:
    row = session.scalar(
        select(CardBalanceReconciliation)
        .where(CardBalanceReconciliation.id == reconciliation_id)
        .options(joinedload(CardBalanceReconciliation.events))
    )
    if row is None:
        raise _http_error(404, "card balance reconciliation not found")
    result = serialize_reconciliation(row)
    result["events"] = [serialize_event(event) for event in row.events]
    return result


def upsert_reconciliation_from_payload(
    session: Session,
    *,
    payload: dict[str, Any],
    onec_balance: Decimal | None = None,
    settings: Settings | None = None,
) -> CardBalanceReconciliation:
    settings = settings or get_settings()
    payload = dict(payload)
    external_id = clean_string(payload.get("external_id")) or (
        f"bitrix:{clean_string(payload.get('bitrix_item_id'))}"
    )
    if not external_id or external_id == "bitrix:None":
        raise ValueError("external_id or bitrix_item_id is required")
    business_date_value = payload.get("business_date")
    if isinstance(business_date_value, datetime):
        business_date = business_date_value.date()
    elif isinstance(business_date_value, date):
        business_date = business_date_value
    elif isinstance(business_date_value, str) and business_date_value:
        business_date = date.fromisoformat(business_date_value[:10])
    else:
        business_date = utcnow().date()

    row = session.scalar(
        select(CardBalanceReconciliation).where(
            CardBalanceReconciliation.external_id == external_id
        )
    )
    created = row is None
    if row is None:
        row = CardBalanceReconciliation(
            external_id=external_id,
            business_date=business_date,
            status=STATUS_LOW_CONFIDENCE,
        )
        session.add(row)

    cashbox, mapping_error = resolve_cashbox(
        session,
        onec_cashbox_code=clean_string(payload.get("onec_cashbox_code")),
        card_last4=clean_string(payload.get("card_last4")),
        employee_last_name=clean_string(
            payload.get("employee_last_name") or payload.get("employee_name")
        ),
    )
    if mapping_error:
        reason = _mapping_error_reason(mapping_error)
        payload.setdefault("mapping_error", mapping_error)
        payload.setdefault("manual_review_reason", reason)
        if not clean_string(payload.get("resolution_comment")):
            payload["resolution_comment"] = reason
    balance_value = decimal_or_none(payload.get("recognized_balance"))
    if balance_value is None:
        balance_value = decimal_or_none(payload.get("manual_balance"))
    onec_balance = (
        onec_balance if onec_balance is not None else decimal_or_none(payload.get("onec_balance"))
    )
    diff_amount = (
        None if balance_value is None or onec_balance is None else balance_value - onec_balance
    )

    row.business_date = business_date
    row.cashbox_id = None if cashbox is None else cashbox.id
    row.employee_id = clean_string(payload.get("employee_id"))
    row.employee_name = clean_string(payload.get("employee_name"))
    row.employee_last_name = clean_string(
        payload.get("employee_last_name") or (cashbox.employee_last_name if cashbox else None)
    )
    row.card_last4 = clean_string(
        payload.get("card_last4") or (cashbox.card_last4 if cashbox else None)
    )
    row.onec_cashbox_code = clean_string(
        payload.get("onec_cashbox_code") or (cashbox.onec_cashbox_code if cashbox else None)
    )
    row.onec_cashbox_name = clean_string(
        payload.get("onec_cashbox_name") or (cashbox.onec_cashbox_name if cashbox else None)
    )
    row.source_channel = clean_string(payload.get("source_channel")) or "bitrix"
    row.bitrix_item_id = clean_string(payload.get("bitrix_item_id"))
    row.bitrix_stage_id = clean_string(payload.get("bitrix_stage_id"))
    row.screenshot_file_id = clean_string(payload.get("screenshot_file_id"))
    row.submitted_at = _datetime_or_none(payload.get("submitted_at"))
    row.screenshot_taken_at = _datetime_or_none(payload.get("screenshot_taken_at"))
    row.manual_balance = decimal_or_none(payload.get("manual_balance"))
    row.recognized_balance = decimal_or_none(payload.get("recognized_balance"))
    row.recognition_confidence = decimal_or_none(payload.get("recognition_confidence"))
    row.onec_balance_at = utcnow().replace(tzinfo=None) if onec_balance is not None else None
    row.onec_balance = onec_balance
    row.diff_amount = None if diff_amount is None else diff_amount.quantize(Decimal("0.01"))
    row.due_at = _datetime_or_none(payload.get("due_at"))
    row.resolution_comment = clean_string(payload.get("resolution_comment"))
    row.reviewer_id = clean_string(payload.get("reviewer_id"))
    row.payload = _json_safe(payload)
    manual_terminal_status = status_from_bitrix_stage(row.bitrix_stage_id, settings=settings)
    if manual_terminal_status:
        row.status = manual_terminal_status
        if row.resolved_at is None:
            row.resolved_at = utcnow().replace(tzinfo=None)
    else:
        row.status = resolve_status(
            screenshot_file_id=row.screenshot_file_id,
            business_date=row.business_date,
            balance_value=balance_value,
            onec_balance=row.onec_balance,
            diff_amount=row.diff_amount,
            mapping_error=mapping_error,
            settings=settings,
        )
    session.flush()
    append_event(
        session,
        row,
        event_type="created" if created else "updated",
        source=row.source_channel,
        meta={
            "status": row.status,
            "diff_amount": str(row.diff_amount) if row.diff_amount is not None else None,
        },
        idempotency_key=f"{external_id}:{row.status}:{row.business_date.isoformat()}",
    )
    session.commit()
    session.refresh(row)
    return row


def _datetime_or_none(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        return datetime.fromisoformat(normalized.replace("Z", "+00:00")).replace(tzinfo=None)
    return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value
