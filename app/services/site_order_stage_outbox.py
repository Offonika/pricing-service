from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, exists, or_, select
from sqlalchemy.orm import Session, aliased

from app.core.config import Settings, get_settings
from app.models import LogisticsManualReview, LogisticsWarehouse, SiteOrderStageOutbox
from app.services.site_order_fulfillment import (
    CRM_ORDER_NUMBER_FIELD,
    CRM_STAGE_PICKUP_TRANSIT,
    CRM_STAGE_PICKUP_WAITING,
    TERMINAL_CRM_STAGES,
    BitrixChatClient,
)

STATUS_PENDING = "pending"
STATUS_RETRY = "retry"
STATUS_APPLIED = "applied"
STATUS_MANUAL_REVIEW = "manual_review"
STATUS_TERMINAL = "terminal"
BLOCKING_PREDECESSOR_STATUSES = (STATUS_PENDING, STATUS_RETRY, STATUS_MANUAL_REVIEW)

CRM_STAGE_DELIVERY_REVIEW = "DELIVERY_REVIEW"
SMS_EVENT_FIELD = "UF_CRM_MM_PICKUP_READY_EVENT_ID"
SMS_STATUS_FIELD = "UF_CRM_MM_PICKUP_READY_SMS_STATUS"
SMS_SENT_AT_FIELD = "UF_CRM_MM_PICKUP_READY_SMS_SENT_AT"
STORAGE_DEADLINE_FIELD = "UF_CRM_MM_PICKUP_STORAGE_DEADLINE"
PICKUP_POINT_FIELD = "UF_CRM_MM_PICKUP_POINT_NAME"
PICKUP_ADDRESS_FIELD = "UF_CRM_MM_PICKUP_POINT_ADDRESS"

ALLOWED_FROM_STAGE = {
    CRM_STAGE_PICKUP_TRANSIT: "FINAL_INVOICE",
    CRM_STAGE_PICKUP_WAITING: CRM_STAGE_PICKUP_TRANSIT,
}
STAGE_ORDER = {
    "PREPAYMENT_INVOICE": 5,
    "EXECUTING": 8,
    "FINAL_INVOICE": 10,
    "PARTIALLY_SHIPPED": 15,
    CRM_STAGE_PICKUP_TRANSIT: 20,
    CRM_STAGE_PICKUP_WAITING: 30,
    "IN_DELIVERY": 40,
    "DISMANTLING": 50,
    "WON": 100,
    "LOSE": 100,
}
EXECUTION_TARGET_STAGES = {"PREPAYMENT_INVOICE", "FINAL_INVOICE", "WON", "LOSE"}
SHIPMENT_TARGET_STAGES = {
    "FINAL_INVOICE",
    "PARTIALLY_SHIPPED",
    "IN_DELIVERY",
    CRM_STAGE_PICKUP_TRANSIT,
}
FULL_ASSEMBLY_FIELD = "UF_CRM_MM_FULL_ASSEMBLY_CONFIRMED_AT"
EXECUTION_HISTORICAL_APPLY_BATCH_LIMIT = 20


@dataclass(slots=True)
class StageOutboxResult:
    outbox_id: int
    site_order_number: str
    target_stage: str
    result: str
    bitrix_deal_id: int | None = None
    live_stage: str | None = None
    reason: str | None = None
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _is_execution_reconciliation_row(row: SiteOrderStageOutbox) -> bool:
    payload = row.payload if isinstance(row.payload, dict) else {}
    return payload.get(
        "pipeline"
    ) == "execution_reconciliation" or row.source_event_type.startswith("execution_")


def _is_historical_execution_row(row: SiteOrderStageOutbox) -> bool:
    payload = row.payload if isinstance(row.payload, dict) else {}
    return _is_execution_reconciliation_row(row) and (
        payload.get("historical") is True
        or row.source_event_type.startswith("execution_historical_")
    )


def _is_dismantling_execution_row(row: SiteOrderStageOutbox) -> bool:
    if not _is_execution_reconciliation_row(row):
        return False
    payload = row.payload if isinstance(row.payload, dict) else {}
    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
    return _clean(snapshot.get("current_stage")).upper() == "DISMANTLING"


def _is_shipment_reconciliation_row(row: SiteOrderStageOutbox) -> bool:
    payload = row.payload if isinstance(row.payload, dict) else {}
    return payload.get("pipeline") == "shipment_reconciliation" or row.source_event_type.startswith(
        "shipment_"
    )


def _is_order_reconciliation_row(row: SiteOrderStageOutbox) -> bool:
    return _is_execution_reconciliation_row(row) or _is_shipment_reconciliation_row(row)


def _pilot_warehouse_allowed(
    session: Session,
    row: SiteOrderStageOutbox,
    external_ids: list[str],
) -> bool:
    allowed = {_clean(item).lower() for item in external_ids if _clean(item)}
    if not allowed:
        return False
    payload = row.payload if isinstance(row.payload, dict) else {}
    ids: set[int] = set()
    for key in ("warehouse_id", "dropoff_warehouse_id"):
        value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            ids.add(int(value))
        except (TypeError, ValueError):
            continue
    if not ids:
        return False
    warehouse_external_ids = {
        item.lower()
        for item in session.scalars(
            select(LogisticsWarehouse.external_id).where(LogisticsWarehouse.id.in_(ids))
        ).all()
        if item
    }
    return bool(allowed & warehouse_external_ids)


def _pickup_sms_allowed(
    session: Session,
    row: SiteOrderStageOutbox,
    settings: Settings,
) -> bool:
    return settings.pickup_ready_sms_enabled and _pilot_warehouse_allowed(
        session,
        row,
        settings.pickup_ready_sms_pilot_warehouse_external_ids,
    )


def _warehouse_for_event(
    session: Session,
    row: SiteOrderStageOutbox,
) -> LogisticsWarehouse | None:
    payload = row.payload if isinstance(row.payload, dict) else {}
    raw_id = payload.get("dropoff_warehouse_id") or payload.get("warehouse_id")
    try:
        warehouse_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    return session.get(LogisticsWarehouse, warehouse_id)


def _warehouse_address(warehouse: LogisticsWarehouse | None) -> str:
    if warehouse is None or not isinstance(warehouse.payload, dict):
        return ""
    return next(
        (
            _clean(warehouse.payload.get(key))
            for key in ("address", "full_address", "address_line", "location")
            if _clean(warehouse.payload.get(key))
        ),
        "",
    )


def _contact_phone(client: BitrixChatClient, deal_raw: dict[str, Any]) -> str:
    contact_ids = deal_raw.get("CONTACT_IDS")
    if isinstance(contact_ids, list):
        normalized_ids = {_clean(value) for value in contact_ids if _clean(value)}
        if len(normalized_ids) != 1:
            return ""
        raw_contact_id = normalized_ids.pop()
    else:
        raw_contact_id = _clean(deal_raw.get("CONTACT_ID"))
    try:
        contact_id = int(raw_contact_id)
    except (TypeError, ValueError):
        return ""
    if not hasattr(client, "get_contact_by_id"):
        return ""
    contact = client.get_contact_by_id(contact_id)
    phones = contact.get("PHONE") if isinstance(contact, dict) else None
    if not isinstance(phones, list):
        return ""
    values = {
        _clean(item.get("VALUE"))
        for item in phones
        if isinstance(item, dict) and _clean(item.get("VALUE"))
    }
    return values.pop() if len(values) == 1 else ""


def _sms_fields_after_readback(
    session: Session,
    row: SiteOrderStageOutbox,
    settings: Settings,
    *,
    client: BitrixChatClient,
    deal_raw: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    if row.target_stage != CRM_STAGE_PICKUP_WAITING:
        return {}, None
    existing_event = _clean(deal_raw.get(SMS_EVENT_FIELD))
    existing_status = _clean(deal_raw.get(SMS_STATUS_FIELD)).lower()
    if existing_event == str(row.event_id) and existing_status == "sent":
        return {}, None
    if existing_event and existing_event != str(row.event_id) and existing_status == "sent":
        return {}, "pickup_sms_already_sent_for_another_event"

    payload = row.payload if isinstance(row.payload, dict) else {}
    raw_event_at = _clean(payload.get("event_at"))
    event_at = datetime.fromisoformat(raw_event_at) if raw_event_at else datetime.now()
    warehouse = _warehouse_for_event(session, row)
    address = _warehouse_address(warehouse)
    phone = _contact_phone(client, deal_raw)
    blockers = []
    if not row.site_order_number:
        blockers.append("missing_order")
    if warehouse is None:
        blockers.append("missing_pickup_point")
    if not address:
        blockers.append("missing_pickup_address")
    if not phone:
        blockers.append("missing_contact_phone")
    sms_allowed = _pickup_sms_allowed(session, row, settings) and not blockers
    fields: dict[str, Any] = {
        SMS_EVENT_FIELD: str(row.event_id),
        SMS_STATUS_FIELD: "ready" if sms_allowed else "shadow",
        PICKUP_POINT_FIELD: warehouse.name if warehouse is not None else "",
        PICKUP_ADDRESS_FIELD: address,
    }
    if not _clean(deal_raw.get(STORAGE_DEADLINE_FIELD)):
        fields[STORAGE_DEADLINE_FIELD] = (event_at + timedelta(days=3)).isoformat()
    return fields, ",".join(blockers) or None


def _timeline_marker(row: SiteOrderStageOutbox) -> str:
    return f"[MM_LOGISTICS_STAGE:{row.idempotency_key}]"


def _timeline_comment(row: SiteOrderStageOutbox) -> str:
    payload = row.payload if isinstance(row.payload, dict) else {}
    if _is_order_reconciliation_row(row):
        decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
        return "\n".join(
            [
                _timeline_marker(row),
                f"Контроль интернет-заказа №{row.site_order_number}",
                f"Основание: {decision.get('reason') or row.source_event_type}",
                f"Новая стадия: {row.target_stage}",
                f"Ревизия evidence: {payload.get('evidence_fingerprint') or '-'}",
            ]
        )
    return "\n".join(
        [
            _timeline_marker(row),
            f"Логистика: заказ №{row.site_order_number}",
            f"Событие: {row.source_event_type}",
            f"Новая стадия: {row.target_stage}",
            f"Склад отправления: {payload.get('warehouse_name') or payload.get('warehouse_id') or '-'}",
            f"Точка выгрузки: {payload.get('dropoff_warehouse_name') or payload.get('dropoff_warehouse_id') or '-'}",
            f"Водитель: {payload.get('driver_name') or payload.get('driver_id') or '-'}",
            f"Подтвердил: {payload.get('user_name') or payload.get('user_id') or '-'}",
            f"Канал: {payload.get('source_channel') or '-'}",
            f"Время: {payload.get('event_at') or '-'}",
            f"Событие логистики: {row.event_id}",
        ]
    )


def _timeline_comment_exists(
    client: BitrixChatClient,
    *,
    deal_id: int,
    marker: str,
) -> bool:
    response = client.call(
        "crm.timeline.comment.list",
        {
            "filter": {"ENTITY_TYPE": "deal", "ENTITY_ID": deal_id},
            "order": {"ID": "DESC"},
            "start": 0,
        },
    )
    result = response.get("result")
    if isinstance(result, dict):
        result = result.get("items") or result.get("comments") or []
    if not isinstance(result, list):
        raise RuntimeError("bitrix_timeline_readback_invalid")
    return any(marker in _clean(item.get("COMMENT")) for item in result if isinstance(item, dict))


def _ensure_timeline(
    client: BitrixChatClient,
    row: SiteOrderStageOutbox,
    *,
    deal_id: int,
) -> None:
    marker = _timeline_marker(row)
    if _timeline_comment_exists(client, deal_id=deal_id, marker=marker):
        return
    client.call(
        "crm.timeline.comment.add",
        {
            "fields": {
                "ENTITY_TYPE": "deal",
                "ENTITY_ID": deal_id,
                "COMMENT": _timeline_comment(row),
            }
        },
    )


def _ensure_timeline_warning(
    client: BitrixChatClient,
    row: SiteOrderStageOutbox,
    *,
    deal_id: int,
    reason: str,
) -> None:
    marker = f"{_timeline_marker(row)}:SMS_WARNING"
    if _timeline_comment_exists(client, deal_id=deal_id, marker=marker):
        return
    client.call(
        "crm.timeline.comment.add",
        {
            "fields": {
                "ENTITY_TYPE": "deal",
                "ENTITY_ID": deal_id,
                "COMMENT": (
                    f"{marker}\nSMS готовности не активирована: {reason}. "
                    "Стадия сделки обновлена, требуется проверка данных."
                ),
            }
        },
    )


def _ensure_review_timeline(
    client: BitrixChatClient,
    row: SiteOrderStageOutbox,
    *,
    deal_id: int,
    reason: str,
) -> None:
    marker = f"{_timeline_marker(row)}:DELIVERY_REVIEW"
    if _timeline_comment_exists(client, deal_id=deal_id, marker=marker):
        return
    client.call(
        "crm.timeline.comment.add",
        {
            "fields": {
                "ENTITY_TYPE": "deal",
                "ENTITY_ID": deal_id,
                "COMMENT": (
                    f"{marker}\nЛогистическая стадия отправлена на ручную проверку. "
                    f"Причина: {reason}. Событие: {row.event_id}."
                ),
            }
        },
    )


def _result(
    row: SiteOrderStageOutbox,
    result: str,
    *,
    live_stage: str | None = None,
    reason: str | None = None,
    applied: bool = False,
) -> StageOutboxResult:
    return StageOutboxResult(
        outbox_id=row.id,
        site_order_number=row.site_order_number,
        target_stage=row.target_stage,
        result=result,
        bitrix_deal_id=row.bitrix_deal_id,
        live_stage=live_stage,
        reason=reason,
        applied=applied,
    )


def _mark_retry(row: SiteOrderStageOutbox, exc: Exception, now: datetime) -> None:
    row.attempts += 1
    row.status = STATUS_RETRY
    row.last_error = _clean(exc)[:1000] or exc.__class__.__name__
    row.next_attempt_at = now + timedelta(seconds=min(300, 5 * (2 ** min(row.attempts, 6))))
    row.updated_at = now


def _mark_manual_review(
    session: Session,
    row: SiteOrderStageOutbox,
    *,
    reason: str,
    live_stage: str | None,
) -> None:
    row.status = STATUS_MANUAL_REVIEW
    row.last_live_stage = live_stage
    row.last_error = reason[:1000]
    row.updated_at = datetime.now()
    session.add(
        LogisticsManualReview(
            review_type="site_order_stage_conflict",
            source_document_type=("site_order" if _is_order_reconciliation_row(row) else "rtu"),
            source_external_id=row.site_order_number,
            reason=reason,
            payload={
                "site_order_stage_outbox_id": row.id,
                "bitrix_deal_id": row.bitrix_deal_id,
                "live_stage": live_stage,
                "target_stage": row.target_stage,
            },
        )
    )


def _is_terminal_stage(stage: str) -> bool:
    normalized = stage.upper()
    return (
        normalized in TERMINAL_CRM_STAGES
        or normalized.endswith(":WON")
        or normalized.endswith(":LOSE")
        or any(marker in normalized for marker in ("CANCEL", "CANCELLED", "CANCELED"))
    )


def _has_blocking_predecessor(session: Session, row: SiteOrderStageOutbox) -> bool:
    predecessors = session.scalars(
        select(SiteOrderStageOutbox).where(
            SiteOrderStageOutbox.case_id == row.case_id,
            SiteOrderStageOutbox.id < row.id,
            SiteOrderStageOutbox.status.in_(BLOCKING_PREDECESSOR_STATUSES),
        )
    ).all()
    for predecessor in predecessors:
        if (
            _is_order_reconciliation_row(row)
            and _is_order_reconciliation_row(predecessor)
            and row.case.last_evidence_event_id == row.event_id
            and predecessor.event_id != row.event_id
        ):
            continue
        return True
    return False


def _supersede_stale_shipment_predecessors(
    session: Session,
    row: SiteOrderStageOutbox,
    *,
    apply: bool,
    now: datetime,
) -> None:
    if (
        not apply
        or not _is_shipment_reconciliation_row(row)
        or row.case.last_evidence_event_id != row.event_id
    ):
        return
    predecessors = session.scalars(
        select(SiteOrderStageOutbox).where(
            SiteOrderStageOutbox.case_id == row.case_id,
            SiteOrderStageOutbox.id < row.id,
            SiteOrderStageOutbox.status.in_(BLOCKING_PREDECESSOR_STATUSES),
        )
    ).all()
    for predecessor in predecessors:
        if not _is_shipment_reconciliation_row(predecessor):
            continue
        predecessor.status = STATUS_APPLIED
        predecessor.applied_at = predecessor.applied_at or now
        predecessor.next_attempt_at = None
        predecessor.last_error = "superseded_by_newer_evidence"
        predecessor.updated_at = now


def _resolve_deal(
    client: BitrixChatClient, row: SiteOrderStageOutbox
) -> tuple[int | None, str | None]:
    if _is_order_reconciliation_row(row):
        deals = client.list_deals_by_site_order(row.site_order_number)
        if not deals:
            return None, "bitrix_deal_not_found"
        if len(deals) != 1:
            return None, "multiple_bitrix_deals"
        live_deal_id = deals[0].deal_id
        if row.bitrix_deal_id not in (None, live_deal_id):
            return None, f"bitrix_deal_mismatch:{live_deal_id}"
        row.bitrix_deal_id = live_deal_id
        return live_deal_id, None
    if row.bitrix_deal_id is not None:
        return row.bitrix_deal_id, None
    deals = client.list_deals_by_site_order(row.site_order_number)
    if len(deals) == 1:
        row.bitrix_deal_id = deals[0].deal_id
        return deals[0].deal_id, None
    if not deals:
        return None, "bitrix_deal_not_found"
    return None, "multiple_bitrix_deals"


def _process_row(
    session: Session,
    row: SiteOrderStageOutbox,
    *,
    client: BitrixChatClient,
    settings: Settings,
    apply: bool,
    now: datetime,
) -> StageOutboxResult:
    _supersede_stale_shipment_predecessors(session, row, apply=apply, now=now)
    if _has_blocking_predecessor(session, row):
        return _result(row, "waiting_for_predecessor")
    execution_row = _is_execution_reconciliation_row(row)
    shipment_row = _is_shipment_reconciliation_row(row)
    if (execution_row or shipment_row) and row.case.last_evidence_event_id != row.event_id:
        if apply:
            row.status = STATUS_APPLIED
            row.last_error = "superseded_by_newer_evidence"
            row.updated_at = now
        return _result(row, "superseded_by_newer_evidence")
    if not (execution_row or shipment_row) and not _pilot_warehouse_allowed(
        session,
        row,
        settings.logistics_stage_pilot_warehouse_external_ids,
    ):
        return _result(row, "blocked_not_pilot", reason="pilot_warehouse_not_allowed")

    deal_id, resolve_error = _resolve_deal(client, row)
    if resolve_error:
        if apply:
            _mark_manual_review(session, row, reason=resolve_error, live_stage=None)
        return _result(row, "manual_review", reason=resolve_error)
    assert deal_id is not None

    deal = client.get_deal_by_id(deal_id)
    if deal is None:
        raise RuntimeError("bitrix_deal_not_found_after_resolution")
    live_stage = _clean(deal.stage_id)
    live_order_number = _clean((deal.raw or {}).get(CRM_ORDER_NUMBER_FIELD))
    if live_order_number != row.site_order_number:
        reason = f"bitrix_order_mismatch:{live_order_number or '-'}"
        if apply:
            _mark_manual_review(session, row, reason=reason, live_stage=live_stage)
        return _result(row, "manual_review", live_stage=live_stage, reason=reason)
    row.last_live_stage = live_stage or None
    if _is_terminal_stage(live_stage):
        if apply:
            row.status = STATUS_TERMINAL
            row.last_error = f"terminal_live_stage:{live_stage}"
            row.updated_at = now
        return _result(row, "terminal_live_stage", live_stage=live_stage)

    if execution_row:
        if row.target_stage not in EXECUTION_TARGET_STAGES:
            reason = f"execution_target_not_allowed:{row.target_stage or '-'}"
            if apply:
                _mark_manual_review(session, row, reason=reason, live_stage=live_stage)
            return _result(row, "manual_review", live_stage=live_stage, reason=reason)
        expected_stages = {"DISMANTLING"} if _is_dismantling_execution_row(row) else {"EXECUTING"}
    elif shipment_row:
        if row.target_stage not in SHIPMENT_TARGET_STAGES:
            reason = f"shipment_target_not_allowed:{row.target_stage or '-'}"
            if apply:
                _mark_manual_review(session, row, reason=reason, live_stage=live_stage)
            return _result(row, "manual_review", live_stage=live_stage, reason=reason)
        expected_stages = {
            "FINAL_INVOICE": {"EXECUTING"},
            "PARTIALLY_SHIPPED": {"EXECUTING", "FINAL_INVOICE"},
            "IN_DELIVERY": {"EXECUTING", "FINAL_INVOICE", "PARTIALLY_SHIPPED"},
            CRM_STAGE_PICKUP_TRANSIT: {
                "EXECUTING",
                "FINAL_INVOICE",
                "PARTIALLY_SHIPPED",
            },
        }[row.target_stage]
    else:
        expected_stages = {ALLOWED_FROM_STAGE[row.target_stage]}
    if live_stage not in {*expected_stages, row.target_stage}:
        if STAGE_ORDER.get(live_stage, 0) > STAGE_ORDER.get(row.target_stage, 0):
            if apply:
                row.status = STATUS_APPLIED
                row.applied_at = now
                row.last_error = "superseded_by_later_stage"
            return _result(row, "superseded", live_stage=live_stage)
        if apply and not execution_row:
            conflict_reason = f"unexpected_live_stage:{live_stage or '-'}"
            client.update_deal_stage(deal_id, CRM_STAGE_DELIVERY_REVIEW)
            review_readback = client.get_deal_by_id(deal_id)
            if (
                review_readback is None
                or _clean(review_readback.stage_id) != CRM_STAGE_DELIVERY_REVIEW
            ):
                raise RuntimeError("bitrix_delivery_review_readback_mismatch")
            _ensure_review_timeline(
                client,
                row,
                deal_id=deal_id,
                reason=conflict_reason,
            )
            _mark_manual_review(
                session,
                row,
                reason=conflict_reason,
                live_stage=live_stage,
            )
        elif apply:
            _mark_manual_review(
                session,
                row,
                reason=f"unexpected_live_stage:{live_stage or '-'}",
                live_stage=live_stage,
            )
        return _result(
            row,
            "manual_review",
            live_stage=live_stage,
            reason=f"unexpected_live_stage:{live_stage or '-'}",
        )

    if not apply:
        return _result(row, "dry_run_ready", live_stage=live_stage)

    if live_stage != row.target_stage:
        if row.target_stage == "FINAL_INVOICE" and (execution_row or shipment_row):
            payload = row.payload if isinstance(row.payload, dict) else {}
            snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
            coverage_status = (
                snapshot.get("line_coverage_status")
                if execution_row
                else payload.get("coverage_status")
            )
            if coverage_status != "complete":
                raise RuntimeError("full_assembly_readback_guard_missing")
            confirmed_at = (
                snapshot.get("latest_assembled_at")
                or payload.get("event_at")
                or (row.event.event_at.isoformat() if row.event.event_at else now.isoformat())
            )
            client.update_deal_fields(deal_id, {FULL_ASSEMBLY_FIELD: confirmed_at})
            assembly_readback = client.get_deal_by_id(deal_id)
            if assembly_readback is None or _clean(
                (assembly_readback.raw or {}).get(FULL_ASSEMBLY_FIELD)
            ) != _clean(confirmed_at):
                raise RuntimeError("full_assembly_field_readback_mismatch")
        client.update_deal_fields(deal_id, {"STAGE_ID": row.target_stage})
        readback = client.get_deal_by_id(deal_id)
        if readback is None or _clean(readback.stage_id) != row.target_stage:
            raise RuntimeError("bitrix_stage_readback_mismatch")
    else:
        readback = deal

    sms_fields, sms_warning = _sms_fields_after_readback(
        session,
        row,
        settings,
        client=client,
        deal_raw=readback.raw or {},
    )
    if sms_fields:
        client.update_deal_fields(deal_id, sms_fields)
    if sms_warning:
        _ensure_timeline_warning(client, row, deal_id=deal_id, reason=sms_warning)

    _ensure_timeline(client, row, deal_id=deal_id)
    row.status = STATUS_APPLIED
    row.applied_at = row.applied_at or now
    row.timeline_written_at = now
    row.next_attempt_at = None
    row.last_error = None
    row.updated_at = now
    row.case.current_crm_stage = row.target_stage
    row.case.updated_at = now
    return _result(row, "applied", live_stage=row.target_stage, applied=True)


def process_stage_outbox(
    session: Session,
    *,
    client: BitrixChatClient,
    apply: bool = False,
    limit: int | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    site_order_numbers: list[str] | None = None,
) -> list[StageOutboxResult]:
    settings = settings or get_settings()
    current_time = now or datetime.now()
    batch_size = min(limit or settings.logistics_stage_worker_batch_size, 500)
    predecessor = aliased(SiteOrderStageOutbox)
    has_blocking_predecessor = (
        exists()
        .where(
            predecessor.case_id == SiteOrderStageOutbox.case_id,
            predecessor.id < SiteOrderStageOutbox.id,
            predecessor.status.in_(BLOCKING_PREDECESSOR_STATUSES),
        )
        .correlate(SiteOrderStageOutbox)
    )
    query = select(SiteOrderStageOutbox).where(
        SiteOrderStageOutbox.status.in_([STATUS_PENDING, STATUS_RETRY]),
        or_(
            SiteOrderStageOutbox.next_attempt_at.is_(None),
            SiteOrderStageOutbox.next_attempt_at <= current_time,
        ),
    )
    if site_order_numbers is not None:
        normalized_orders = [
            item.strip() for item in dict.fromkeys(site_order_numbers) if item.strip()
        ]
        if not normalized_orders:
            return []
        query = query.where(SiteOrderStageOutbox.site_order_number.in_(normalized_orders))
    rows = session.scalars(
        query.order_by(
            case((has_blocking_predecessor, 1), else_=0),
            case(
                (
                    SiteOrderStageOutbox.source_event_type.like("execution_historical_%"),
                    1,
                ),
                else_=0,
            ),
            SiteOrderStageOutbox.id,
        )
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    ).all()
    results: list[StageOutboxResult] = []
    historical_apply_attempts = 0
    for row in rows:
        if apply:
            if _is_execution_reconciliation_row(row):
                if not (
                    settings.order_fulfillment_execution_master_enabled
                    and settings.order_fulfillment_execution_stage_apply_enabled
                ):
                    results.append(_result(row, "automation_disabled"))
                    continue
                if (
                    _is_dismantling_execution_row(row)
                    and not settings.order_fulfillment_dismantling_auto_apply_enabled
                ):
                    results.append(_result(row, "dismantling_auto_apply_disabled"))
                    continue
                if (
                    _is_historical_execution_row(row)
                    and not settings.order_fulfillment_execution_historical_apply_enabled
                ):
                    results.append(_result(row, "historical_apply_disabled"))
                    continue
                if _is_historical_execution_row(row):
                    if historical_apply_attempts >= EXECUTION_HISTORICAL_APPLY_BATCH_LIMIT:
                        results.append(_result(row, "historical_batch_limit"))
                        continue
                    historical_apply_attempts += 1
            elif _is_shipment_reconciliation_row(row):
                if not (
                    settings.order_fulfillment_bot_apply_enabled
                    and settings.order_fulfillment_shipments_master_enabled
                    and settings.order_fulfillment_shipments_stage_apply_enabled
                ):
                    results.append(_result(row, "shipment_automation_disabled"))
                    continue
            elif not settings.logistics_stage_automation_enabled:
                results.append(_result(row, "automation_disabled"))
                continue
        try:
            result = _process_row(
                session,
                row,
                client=client,
                settings=settings,
                apply=apply,
                now=current_time,
            )
        except Exception as exc:  # noqa: BLE001 - persisted retry boundary.
            if apply:
                _mark_retry(row, exc, current_time)
            result = _result(row, "retry", reason=_clean(exc)[:1000])
        results.append(result)
        if apply:
            # Keep one transaction for the batch, but make the row status visible to
            # the predecessor query before processing the next event of this order.
            session.flush()
    if apply:
        session.commit()
    return results
