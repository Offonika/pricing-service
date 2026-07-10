from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings, get_settings
from app.models.procurement_order_formation import (
    ProcurementClassificationProposal,
    ProcurementLifecycleTransitionProposal,
    ProcurementOrderFormation,
    ProcurementOrderFormationEvent,
    ProcurementOrderFormationLine,
)
from app.services.assortment_lifecycle import (
    AssortmentLifecycleInput,
    decide_assortment_status,
)
from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE,
    ASSORTMENT_LIFECYCLE_RUN_TABLE,
)
from app.services.bitrix_procurement_order_formation_auth import (
    ProcurementOrderFormationSession,
)
from app.services.exporters.ut103_exchange import resolve_ut103_exchange_root
from app.services.exporters.ut103_nomenclature_properties import (
    NomenclaturePropertyUpdateMessage,
    NomenclaturePropertyUpdateRow,
    PropertyUpdateExchangeResult,
    build_nomenclature_property_updates_xml,
    write_nomenclature_property_updates_message,
)
from app.services.procurement_order_formation import (
    PROPERTY_UPDATE_SOURCE,
    STATUS_APPROVED_BY_PROPERTY_NAME,
    STATUS_CHANGED_AT_PROPERTY_NAME,
    STATUS_PROPERTY_NAME,
    STATUS_REASON_PROPERTY_NAME,
    STATUS_SOURCE_PROPERTY_NAME,
    effective_assortment_status,
    normalize_guid,
    normalize_status,
    order_blockers,
    serialize_proposal,
    status_label,
)

DISPLAY_FOLDER = "дисплеи"
DISPLAY_RESPONSIBLE_NAME = "Омар"
LIFECYCLE_ORDER = ("fruit", "newborn", "new_item", "sales_start", "sale", "working")
LIFECYCLE_LABELS = {
    "fruit": "Плод",
    "newborn": "Новорожденный",
    "newborn_need": "ДН / Добор новорождённого",
    "new_item": "Новинка",
    "sales_start": "СП / Старт продаж",
    "sale": "ПРОДАЖА",
    "working": "Рабочий",
    "review": "Review / разбор",
}
MANUAL_STATUS_ORDER = (
    "matrix",
    "on_demand",
    "replace_candidate",
    "nonliquid",
    "do_not_order",
    "review",
)
MANUAL_STATUS_LABELS = {
    "matrix": "Матричный",
    "on_demand": "Под заказ",
    "replace_candidate": "Кандидат на замену",
    "nonliquid": "Кандидат на неликвид",
    "do_not_order": "Не закупать",
    "review": "Review / разбор",
}
APPROVAL_RESULT_KEYS = ("approved", "stale", "blocked", "conflict", "failed")


def sync_lifecycle_transition_proposals(
    db: Session,
    *,
    folder: str = DISPLAY_FOLDER,
    run_id: int | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    settings = settings or get_settings()
    run = _load_run(db, folder=folder, run_id=run_id)
    if run is None:
        return {"created": 0, "updated": 0, "automatic": 0, "stale": 0, "run_id": 0}
    rows = _snapshot_rows(db, folder=folder, run_id=int(run["id"]))
    created = 0
    updated = 0
    automatic = 0
    active_keys: set[str] = set()
    for row in rows:
        candidate = _transition_candidate(row, run=run, settings=settings)
        if candidate is None:
            continue
        is_automatic = _is_automatic_lifecycle_transition(row, candidate)
        active_keys.add(candidate["idempotency_key"])
        if is_automatic:
            approved_at = datetime.now(UTC).replace(tzinfo=None)
            candidate.update(
                {
                    "status": "auto_applied",
                    "approved_at": approved_at,
                    "approved_by_actor": "system:onec-facts",
                    "approved_by_name": "Автоматически по фактам 1С",
                    "onec_status": "not_required",
                    "payload": {
                        **candidate["payload"],
                        "automatic": True,
                        "automatic_reason": "first_supplier_order_created",
                    },
                }
            )
        proposal = db.scalar(
            select(ProcurementLifecycleTransitionProposal).where(
                ProcurementLifecycleTransitionProposal.idempotency_key
                == candidate["idempotency_key"]
            )
        )
        if proposal is None:
            db.add(ProcurementLifecycleTransitionProposal(**candidate))
            created += 1
            automatic += int(is_automatic)
        elif proposal.status == "pending":
            for key in (
                "nomenclature_ref",
                "product_guid",
                "product_name",
                "folder",
                "reason",
                "facts",
                "blockers",
                "risk_codes",
                "facts_hash",
                "responsible_bitrix_user_id",
                "responsible_name",
                "payload",
            ):
                setattr(proposal, key, candidate[key])
            if is_automatic:
                proposal.status = candidate["status"]
                proposal.approved_at = candidate["approved_at"]
                proposal.approved_by_actor = candidate["approved_by_actor"]
                proposal.approved_by_name = candidate["approved_by_name"]
                proposal.onec_status = candidate["onec_status"]
                automatic += 1
            updated += 1

    pending_rows = db.scalars(
        select(ProcurementLifecycleTransitionProposal).where(
            ProcurementLifecycleTransitionProposal.folder.ilike(f"%{folder}%"),
            ProcurementLifecycleTransitionProposal.status == "pending",
        )
    ).all()
    stale_rows = [
        proposal
        for proposal in pending_rows
        if proposal.run_id != int(run["id"])
        or proposal.idempotency_key not in active_keys
    ]
    for proposal in stale_rows:
        proposal.status = "stale"
        proposal.payload = {**(proposal.payload or {}), "stale_reason": "new_run_available"}
    db.commit()
    return {
        "created": created,
        "updated": updated,
        "automatic": automatic,
        "stale": len(stale_rows),
        "run_id": int(run["id"]),
    }


def build_dashboard(
    db: Session,
    *,
    folder: str = DISPLAY_FOLDER,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    run = _load_run(db, folder=folder)
    rows = _snapshot_rows(db, folder=folder, run_id=int(run["id"]) if run else None)
    transitions = db.scalars(
        select(ProcurementLifecycleTransitionProposal).where(
            ProcurementLifecycleTransitionProposal.folder.ilike(f"%{folder}%"),
            ProcurementLifecycleTransitionProposal.status == "pending",
        )
    ).all()
    status_counts: Counter[str] = Counter()
    review_counts: Counter[str] = Counter()
    for row in rows:
        code = _dashboard_status(str(row.get("status") or ""))
        if code in LIFECYCLE_ORDER:
            status_counts[code] += 1
        if bool(row.get("manual_review_required")) and code in LIFECYCLE_ORDER:
            review_counts[code] += 1
    action_counts = Counter(_dashboard_status(item.current_status) for item in transitions)
    cards: list[dict[str, Any]] = []
    for status_code in LIFECYCLE_ORDER:
        is_working = status_code == "working"
        target = None if is_working else _next_lifecycle_status(status_code)
        action_count = action_counts[status_code]
        cards.append(
            {
                "status": status_code,
                "label": LIFECYCLE_LABELS[status_code],
                "total_count": status_counts[status_code],
                "action_count": action_count,
                "action_kind": "review" if is_working else "transition",
                "action_label": "На пересмотр" if is_working else "К переходу",
                "target_status": target,
                "review_count": review_counts[status_code],
                "overdue_count": 0,
                "urgency": _card_urgency(action_count, review_counts[status_code]),
            }
        )
    manual_counts = Counter(str(row.get("status") or "") for row in rows)
    manual_status_counts = {
        code: (review_counts["working"] if code == "review" else manual_counts[code])
        for code in MANUAL_STATUS_ORDER
    }
    attention = [
        {
            "nomenclature_code": item.nomenclature_code,
            "product_name": item.product_name,
            "current_status": item.current_status,
            "current_status_label": _status_label(item.current_status),
            "reason": item.reason,
            "recommendation": (
                _status_label(item.target_status) if item.target_status else "Открыть разбор"
            ),
            "deadline_label": "сегодня" if not item.blockers else "блокер",
            "urgency": "action" if not item.blockers else "blocked",
        }
        for item in sorted(
            transitions,
            key=lambda value: (bool(value.blockers), value.created_at or datetime.min),
        )[:5]
    ]
    return {
        "folder": folder,
        "responsible_user_id": settings.procurement_order_formation_display_responsible_user_id,
        "responsible_name": DISPLAY_RESPONSIBLE_NAME,
        "run_id": int(run["id"]) if run else None,
        "run_key": str(run["run_key"]) if run else None,
        "updated_at": run.get("finished_at") if run else None,
        "cards": cards,
        "manual_status_counts": manual_status_counts,
        "attention": attention,
    }


def list_lifecycle_transitions(
    db: Session,
    *,
    status: str,
    scope: str = "action",
    readiness: str = "all",
    search: str = "",
    page: int = 1,
    page_size: int = 50,
    folder: str = DISPLAY_FOLDER,
) -> dict[str, Any]:
    normalized_status = _dashboard_status(normalize_status(status) or status)
    if normalized_status not in LIFECYCLE_ORDER:
        raise ValueError("unsupported lifecycle status")
    if scope not in {"action", "all"}:
        raise ValueError("scope must be action or all")
    if readiness not in {"all", "ready", "blocked", "stale"}:
        raise ValueError("unsupported readiness filter")
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    search_key = search.strip().casefold()
    latest_run = _load_run(db, folder=folder)
    latest_run_id = int(latest_run["id"]) if latest_run else 0

    if scope == "all":
        rows = _snapshot_rows(db, folder=folder, run_id=latest_run_id or None)
        serialized = [
            _serialize_snapshot_row(row, run=latest_run)
            for row in rows
            if _dashboard_status(str(row.get("status") or "")) == normalized_status
        ]
    else:
        proposals = db.scalars(
            select(ProcurementLifecycleTransitionProposal)
            .where(
                ProcurementLifecycleTransitionProposal.folder.ilike(f"%{folder}%"),
                ProcurementLifecycleTransitionProposal.current_status.in_(
                    _status_query_values(normalized_status)
                ),
                ProcurementLifecycleTransitionProposal.status.in_(("pending", "stale")),
            )
            .order_by(
                ProcurementLifecycleTransitionProposal.status,
                ProcurementLifecycleTransitionProposal.created_at,
            )
        ).all()
        serialized = [serialize_transition(item, latest_run_id=latest_run_id) for item in proposals]
    if search_key:
        serialized = [
            item
            for item in serialized
            if search_key in f"{item['nomenclature_code']} {item['product_name']}".casefold()
        ]
    if readiness != "all":
        serialized = [
            item
            for item in serialized
            if (
                readiness == "ready"
                and item["ready"]
                or readiness == "blocked"
                and bool(item["blockers"])
                or readiness == "stale"
                and item["stale"]
            )
        ]
    total = len(serialized)
    start = (page - 1) * page_size
    items = serialized[start : start + page_size]
    return {
        "status": normalized_status,
        "scope": scope,
        "total": total,
        "page": page,
        "page_size": page_size,
        "ready_count": sum(1 for item in serialized if item["ready"]),
        "blocked_count": sum(1 for item in serialized if item["blockers"]),
        "stale_count": sum(1 for item in serialized if item["stale"]),
        "items": items,
    }


def approve_lifecycle_transitions(
    db: Session,
    *,
    items: list[Mapping[str, Any]],
    idempotency_key: str,
    session: ProcurementOrderFormationSession,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    if not 1 <= len(items) <= 100:
        raise ValueError("lifecycle approval batch must contain 1..100 rows")
    ensure_lifecycle_approver(session.user_id, settings=settings)
    existing_event = db.scalar(
        select(ProcurementOrderFormationEvent).where(
            ProcurementOrderFormationEvent.idempotency_key == idempotency_key
        )
    )
    if existing_event is not None:
        return dict(existing_event.payload or {})

    results: list[dict[str, Any]] = []
    approved: list[ProcurementLifecycleTransitionProposal] = []
    latest_run = _load_run(db, folder=DISPLAY_FOLDER)
    latest_run_id = int(latest_run["id"]) if latest_run else 0
    for raw in items:
        proposal_id = int(raw.get("proposal_id") or 0)
        proposal = db.get(ProcurementLifecycleTransitionProposal, proposal_id)
        if proposal is None:
            results.append(_approval_result(proposal_id, "failed", "Предложение не найдено"))
            continue
        if proposal.status != "pending":
            results.append(_approval_result(proposal_id, "conflict", "Предложение уже обработано"))
            continue
        if proposal.action_kind != "transition" or not proposal.target_status:
            results.append(
                _approval_result(proposal_id, "blocked", "Нужно отдельное ручное решение")
            )
            continue
        if (
            proposal.run_id != int(raw.get("expected_run_id") or 0)
            or proposal.run_id != latest_run_id
            or proposal.facts_hash != str(raw.get("facts_hash") or "")
        ):
            proposal.status = "stale"
            results.append(_approval_result(proposal_id, "stale", "Расчёт устарел"))
            continue
        expected_status = normalize_status(raw.get("expected_current_status"))
        if normalize_status(proposal.current_status) != expected_status:
            results.append(_approval_result(proposal_id, "conflict", "Текущий статус изменился"))
            continue
        if proposal.blockers:
            results.append(
                _approval_result(
                    proposal_id,
                    "blocked",
                    "; ".join(str(item) for item in proposal.blockers),
                )
            )
            continue
        if normalize_status(proposal.target_status) == "working":
            responsible_id = settings.procurement_order_formation_display_responsible_user_id
            if str(session.user_id) != str(responsible_id):
                results.append(
                    _approval_result(
                        proposal_id,
                        "blocked",
                        "Переход в Рабочий подтверждает ответственный за папку",
                    )
                )
                continue
        approved.append(proposal)

    mode = "apply" if settings.procurement_order_formation_property_apply_enabled else "dry_run"
    message_id: str | None = None
    xml_preview = ""
    written_path: Path | None = None
    if approved:
        message_id = f"proc-lifecycle-{uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key).hex}"
        message = _build_transition_message(
            approved,
            message_id=message_id,
            mode=mode,
            approved_by=session.user_name or session.actor,
        )
        xml_preview = build_nomenclature_property_updates_xml(message).decode("windows-1251")
        if mode == "apply":
            written_path = write_nomenclature_property_updates_message(
                resolve_ut103_exchange_root(None),
                message,
            )
        approved_at = datetime.now(UTC).replace(tzinfo=None)
        for proposal in approved:
            proposal.status = "sent_to_1c" if mode == "apply" else "approved"
            proposal.approved_at = approved_at
            proposal.approved_by_actor = session.actor
            proposal.approved_by_bitrix_user_id = session.user_id
            proposal.approved_by_name = session.user_name or session.actor
            proposal.onec_message_id = message_id
            proposal.onec_status = "pending" if mode == "apply" else "dry_run"
            proposal.payload = {**(proposal.payload or {}), "xml_preview": xml_preview}
            results.append(_approval_result(proposal.id, "approved", "Переход утверждён"))

    summary = {key: 0 for key in APPROVAL_RESULT_KEYS}
    for item in results:
        result = item["result"]
        if result in summary:
            summary[result] += 1
    response = {
        "mode": mode,
        "message_id": message_id,
        "xml_preview": xml_preview,
        "written_path": str(written_path) if written_path else None,
        "summary": summary,
        "items": sorted(results, key=lambda item: item["proposal_id"]),
    }
    record_event(
        db,
        entity_type="lifecycle_batch",
        entity_id=idempotency_key,
        event_type="lifecycle_transitions_approved",
        session=session,
        idempotency_key=idempotency_key,
        payload=response,
    )
    db.commit()
    return response


def list_orders(
    db: Session,
    *,
    search: str = "",
    status: str = "",
    supplier: str = "",
    blockers: str = "all",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    statement = _order_list_statement()
    orders = list(db.scalars(statement).unique().all())
    search_key = search.strip().casefold()
    supplier_key = supplier.strip().casefold()
    filtered: list[ProcurementOrderFormation] = []
    for order in orders:
        order_blocker_list = order_blockers(order)
        if status and order.status != status:
            continue
        if supplier_key and supplier_key not in order.supplier_name.casefold():
            continue
        if blockers == "without" and order_blocker_list:
            continue
        if blockers == "with" and not order_blocker_list:
            continue
        if search_key:
            haystack = " ".join(
                [
                    order.supplier_name,
                    order.contract_name,
                    *(line.nomenclature_name for line in order.lines),
                    *(str(line.nomenclature_code or "") for line in order.lines),
                ]
            ).casefold()
            if search_key not in haystack:
                continue
        filtered.append(order)
    filtered.sort(key=lambda item: (item.order_date, item.updated_at), reverse=True)
    summary = _orders_summary(filtered)
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    start = (page - 1) * page_size
    return {
        "total": len(filtered),
        "page": page,
        "page_size": page_size,
        "summary": summary,
        "items": [serialize_order_list_item(item) for item in filtered[start : start + page_size]],
    }


def list_classification_proposals(
    db: Session,
    *,
    status: str = "",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    proposals = list(
        db.scalars(
            select(ProcurementClassificationProposal)
            .options(
                selectinload(ProcurementClassificationProposal.line).selectinload(
                    ProcurementOrderFormationLine.order
                )
            )
            .order_by(ProcurementClassificationProposal.requested_at.desc())
        ).all()
    )
    filtered = [item for item in proposals if not status or item.status == status]
    today = datetime.now(UTC).date()
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    start = (page - 1) * page_size
    items = []
    for proposal in filtered[start : start + page_size]:
        line = proposal.line
        order = line.order
        items.append(
            {
                "proposal": serialize_proposal(proposal),
                "order_id": order.id,
                "order_version": order.version,
                "line_id": line.id,
                "line_version": line.version,
                "nomenclature_code": line.nomenclature_code,
                "nomenclature_ref": line.nomenclature_ref,
                "product_name": line.nomenclature_name,
                "supplier_name": order.supplier_name,
                "effective_status": effective_assortment_status(line),
            }
        )
    return {
        "total": len(filtered),
        "page": page,
        "page_size": page_size,
        "pending": sum(1 for item in proposals if item.status == "proposed"),
        "approved_today": sum(
            1
            for item in proposals
            if item.approved_at is not None and item.approved_at.date() == today
        ),
        "readback_conflicts": sum(1 for item in proposals if item.status == "conflict"),
        "items": items,
    }


def list_events(
    db: Session,
    *,
    order_id: int | None = None,
    event_type: str = "",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    statement = select(ProcurementOrderFormationEvent)
    if order_id is not None:
        statement = statement.where(ProcurementOrderFormationEvent.order_id == order_id)
    if event_type:
        statement = statement.where(ProcurementOrderFormationEvent.event_type == event_type)
    events = list(
        db.scalars(statement.order_by(ProcurementOrderFormationEvent.created_at.desc())).all()
    )
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    start = (page - 1) * page_size
    return {
        "total": len(events),
        "page": page,
        "page_size": page_size,
        "items": [serialize_event(item) for item in events[start : start + page_size]],
    }


def record_event(
    db: Session,
    *,
    entity_type: str,
    entity_id: str | int,
    event_type: str,
    session: ProcurementOrderFormationSession | None = None,
    order_id: int | None = None,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
    actor: str = "system:procurement-order-formation",
) -> ProcurementOrderFormationEvent:
    if idempotency_key:
        existing = db.scalar(
            select(ProcurementOrderFormationEvent).where(
                ProcurementOrderFormationEvent.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return existing
    event = ProcurementOrderFormationEvent(
        order_id=order_id,
        entity_type=entity_type,
        entity_id=str(entity_id),
        event_type=event_type,
        actor=session.actor if session else actor,
        bitrix_user_id=session.user_id if session else None,
        user_name=session.user_name if session else None,
        idempotency_key=idempotency_key,
        before=_jsonable(dict(before or {})),
        after=_jsonable(dict(after or {})),
        payload=_jsonable(dict(payload or {})),
    )
    db.add(event)
    db.flush()
    return event


def serialize_transition(
    proposal: ProcurementLifecycleTransitionProposal,
    *,
    latest_run_id: int,
) -> dict[str, Any]:
    stale = proposal.status == "stale" or proposal.run_id != latest_run_id
    ready = (
        proposal.status == "pending"
        and proposal.action_kind == "transition"
        and bool(proposal.target_status)
        and not proposal.blockers
        and not stale
    )
    return {
        "proposal_id": proposal.id,
        "nomenclature_code": proposal.nomenclature_code,
        "nomenclature_ref": proposal.nomenclature_ref,
        "product_guid": proposal.product_guid,
        "product_name": proposal.product_name,
        "folder": proposal.folder,
        "action_kind": proposal.action_kind,
        "current_status": proposal.current_status,
        "current_status_label": _status_label(proposal.current_status),
        "target_status": proposal.target_status,
        "target_status_label": _status_label(proposal.target_status),
        "proposal_status": proposal.status,
        "reason": proposal.reason,
        "facts": dict(proposal.facts or {}),
        "blockers": list(proposal.blockers or []),
        "risk_codes": list(proposal.risk_codes or []),
        "run_id": proposal.run_id,
        "run_key": proposal.run_key,
        "facts_hash": proposal.facts_hash,
        "responsible_bitrix_user_id": proposal.responsible_bitrix_user_id,
        "responsible_name": proposal.responsible_name,
        "ready": ready,
        "selectable": ready,
        "stale": stale,
        "created_at": proposal.created_at,
    }


def serialize_order_list_item(order: ProcurementOrderFormation) -> dict[str, Any]:
    active_lines = [line for line in order.lines if not line.removed]
    return {
        "id": order.id,
        "stable_key": order.stable_key,
        "status": order.status,
        "version": order.version,
        "supplier_name": order.supplier_name,
        "contract_name": order.contract_name,
        "warehouse_name": order.warehouse_name,
        "currency": order.currency,
        "route": order.route,
        "batch_id": order.batch_id,
        "order_date": order.order_date,
        "responsible_name": order.responsible_name,
        "source_run_id": order.source_run_id,
        "onec_status": order.onec_status,
        "onec_document_number": order.onec_document_number,
        "onec_error": order.onec_error,
        "line_count": len(active_lines),
        "total_quantity": sum((line.final_quantity for line in active_lines), Decimal("0")),
        "total_amount": sum((line.amount for line in active_lines), Decimal("0")),
        "blockers": order_blockers(order),
        "updated_at": order.updated_at,
    }


def serialize_event(event: ProcurementOrderFormationEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "order_id": event.order_id,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "event_type": event.event_type,
        "actor": event.actor,
        "bitrix_user_id": event.bitrix_user_id,
        "user_name": event.user_name,
        "before": dict(event.before or {}),
        "after": dict(event.after or {}),
        "payload": dict(event.payload or {}),
        "created_at": event.created_at,
    }


def record_lifecycle_property_update_exchange_result(
    db: Session,
    result: PropertyUpdateExchangeResult,
) -> list[ProcurementLifecycleTransitionProposal]:
    proposals = list(
        db.scalars(
            select(ProcurementLifecycleTransitionProposal).where(
                ProcurementLifecycleTransitionProposal.onec_message_id == result.message_id
            )
        ).all()
    )
    if not proposals:
        return []
    conflict = any(
        "conflict" in f"{item.result} {item.message}".casefold()
        or "конфликт" in f"{item.result} {item.message}".casefold()
        for item in result.item_results
    )
    for proposal in proposals:
        if result.ok:
            proposal.status = "applied"
            proposal.onec_status = "success"
            proposal.onec_error = None
        elif conflict:
            proposal.status = "conflict"
            proposal.onec_status = "conflict"
            proposal.onec_error = result.errors or "1C current value conflict"
        else:
            proposal.status = "failed"
            proposal.onec_status = "error"
            proposal.onec_error = result.errors or "1C property update failed"
    db.commit()
    return proposals


def ensure_lifecycle_approver(user_id: str, *, settings: Settings) -> None:
    allowed = {
        str(item).strip()
        for item in settings.procurement_order_formation_lifecycle_approver_user_ids
        if str(item).strip()
    }
    if str(user_id).strip() not in allowed:
        raise PermissionError("user cannot approve lifecycle transitions")


def _load_run(
    db: Session,
    *,
    folder: str,
    run_id: int | None = None,
) -> Mapping[str, Any] | None:
    statement = select(ASSORTMENT_LIFECYCLE_RUN_TABLE)
    if run_id is not None:
        statement = statement.where(ASSORTMENT_LIFECYCLE_RUN_TABLE.c.id == run_id)
    else:
        statement = statement.where(
            ASSORTMENT_LIFECYCLE_RUN_TABLE.c.folder.ilike(f"%{folder}%"),
            ASSORTMENT_LIFECYCLE_RUN_TABLE.c.source_status == "ready",
        ).order_by(ASSORTMENT_LIFECYCLE_RUN_TABLE.c.finished_at.desc())
    return db.execute(statement.limit(1)).mappings().first()


def _snapshot_rows(
    db: Session,
    *,
    folder: str,
    run_id: int | None,
) -> list[Mapping[str, Any]]:
    statement = select(ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE).where(
        ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.c.folder.ilike(f"%{folder}%")
    )
    if run_id is not None:
        statement = statement.where(
            ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.c.last_run_id == run_id
        )
    return list(db.execute(statement).mappings().all())


def _transition_candidate(
    row: Mapping[str, Any],
    *,
    run: Mapping[str, Any],
    settings: Settings,
) -> dict[str, Any] | None:
    source = dict(row.get("source_record") or {})
    row_status = normalize_status(row.get("status")) or str(row.get("status") or "")
    manual_status = normalize_status(
        source.get("manual_status")
        or source.get("ManualStatus")
        or source.get("current_status")
        or source.get("assortment_status")
    )
    action_kind = "transition"
    current_status = manual_status or row_status
    target_status = normalize_status(row.get("recommended_status"))
    reason = str(row.get("reason_text") or "")

    if row_status == "newborn_need":
        action_kind = "review"
        current_status = "newborn"
        target_status = None
    elif row_status == "working" and (
        bool(row.get("manual_review_required")) or bool(row.get("blockers"))
    ):
        action_kind = "review"
        current_status = "working"
        target_status = None
    elif target_status is None and manual_status in LIFECYCLE_ORDER:
        fact_target = _fact_target_from_source(source, str(row.get("nomenclature_code") or ""))
        if fact_target and fact_target != manual_status:
            target_status = fact_target
    elif target_status is None and "fact_status_decision_requires_1c_approval" in list(
        row.get("export_blockers") or []
    ):
        fact_decision = dict(source.get("fact_status_decision") or {})
        current_status = normalize_status(fact_decision.get("source_status")) or "fruit"
        target_status = row_status

    if action_kind == "transition":
        if not target_status or target_status == current_status:
            return None
        if target_status not in LIFECYCLE_ORDER:
            return None
    elif current_status != "working" and row_status != "newborn_need":
        return None

    product_ref = str(row.get("product_ref") or "").strip() or None
    product_guid = _safe_guid(product_ref)
    blockers = [
        str(item)
        for item in list(row.get("blockers") or []) + list(row.get("export_blockers") or [])
        if str(item) not in {"ut103_export_blocked", "fact_status_decision_requires_1c_approval"}
    ]
    if action_kind == "transition" and not product_guid:
        blockers.append("catalog_guid_missing")
    facts = {
        "reason_codes": list(row.get("reason_codes") or []),
        "source": row.get("source"),
        "classified_at": _jsonable(row.get("classified_at")),
        "evidence": dict((source.get("fact_status_decision") or {}).get("evidence") or {}),
    }
    run_id = int(run["id"])
    idempotency_key = (
        f"proc-lifecycle:{row.get('nomenclature_code')}:{run_id}:"
        f"{current_status}:{target_status or 'review'}:{action_kind}"
    )
    return {
        "nomenclature_code": str(row.get("nomenclature_code") or ""),
        "nomenclature_ref": product_ref,
        "product_guid": product_guid,
        "product_name": str(row.get("name") or ""),
        "folder": str(row.get("folder") or ""),
        "action_kind": action_kind,
        "current_status": current_status,
        "target_status": target_status,
        "status": "pending",
        "reason": reason,
        "facts": facts,
        "blockers": sorted(set(blockers)),
        "risk_codes": list(row.get("reason_codes") or []),
        "run_id": run_id,
        "run_key": str(run.get("run_key") or run_id),
        "facts_hash": str(row.get("source_hash") or ""),
        "idempotency_key": idempotency_key,
        "responsible_bitrix_user_id": (
            settings.procurement_order_formation_display_responsible_user_id
        ),
        "responsible_name": DISPLAY_RESPONSIBLE_NAME,
        "payload": {"snapshot_id": row.get("id")},
    }


def _is_automatic_lifecycle_transition(
    row: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> bool:
    if candidate.get("current_status") != "fruit" or candidate.get("target_status") != "newborn":
        return False
    reason_codes = {str(item) for item in row.get("reason_codes") or []}
    source = dict(row.get("source_record") or {})
    evidence = dict((source.get("fact_status_decision") or {}).get("evidence") or {})
    try:
        supplier_order_count = int(evidence.get("supplier_order_count_1c") or 0)
    except (TypeError, ValueError):
        supplier_order_count = 0
    return (
        "supplier_order_without_cargo" in reason_codes
        or bool(source.get("first_supplier_order_at"))
        or supplier_order_count > 0
    )


def _fact_target_from_source(source: Mapping[str, Any], nomenclature_code: str) -> str | None:
    try:
        decision = decide_assortment_status(
            AssortmentLifecycleInput(
                nomenclature_code=nomenclature_code,
                created_at=_parse_date(source.get("created_at")),
                first_supplier_order_at=_parse_date(source.get("first_supplier_order_at")),
                supplier_order_cargo_handoff_dates=_parse_dates(
                    source.get("supplier_order_cargo_handoff_dates")
                ),
                receipt_dates=_parse_dates(source.get("receipt_dates")),
                has_need_signal=bool(source.get("has_need_signal")),
            )
        )
    except (TypeError, ValueError):
        return None
    target = decision.recommended_status or decision.status
    return target.value


def _build_transition_message(
    proposals: Iterable[ProcurementLifecycleTransitionProposal],
    *,
    message_id: str,
    mode: str,
    approved_by: str,
) -> NomenclaturePropertyUpdateMessage:
    rows: list[NomenclaturePropertyUpdateRow] = []
    changed_at = datetime.now(UTC).date()
    for proposal in proposals:
        reason = proposal.reason
        base_key = proposal.idempotency_key
        rows.extend(
            (
                NomenclaturePropertyUpdateRow(
                    idempotency_key=f"{base_key}:status",
                    nomenclature_code=proposal.nomenclature_code,
                    property_name=STATUS_PROPERTY_NAME,
                    value_type="property_value",
                    new_value_name=_status_label(proposal.target_status),
                    new_value_tag=str(proposal.target_status or ""),
                    expected_current_value_name=_status_label(proposal.current_status),
                    expected_current_value_tag=proposal.current_status,
                    reason=reason,
                    approved_by=approved_by,
                ),
                NomenclaturePropertyUpdateRow(
                    idempotency_key=f"{base_key}:reason",
                    nomenclature_code=proposal.nomenclature_code,
                    property_name=STATUS_REASON_PROPERTY_NAME,
                    value_type="string",
                    new_value=reason,
                    reason=reason,
                    approved_by=approved_by,
                ),
                NomenclaturePropertyUpdateRow(
                    idempotency_key=f"{base_key}:changed-at",
                    nomenclature_code=proposal.nomenclature_code,
                    property_name=STATUS_CHANGED_AT_PROPERTY_NAME,
                    value_type="date",
                    new_value=changed_at,
                    reason=reason,
                    approved_by=approved_by,
                ),
                NomenclaturePropertyUpdateRow(
                    idempotency_key=f"{base_key}:source",
                    nomenclature_code=proposal.nomenclature_code,
                    property_name=STATUS_SOURCE_PROPERTY_NAME,
                    value_type="string",
                    new_value=PROPERTY_UPDATE_SOURCE,
                    reason=reason,
                    approved_by=approved_by,
                ),
                NomenclaturePropertyUpdateRow(
                    idempotency_key=f"{base_key}:approved-by",
                    nomenclature_code=proposal.nomenclature_code,
                    property_name=STATUS_APPROVED_BY_PROPERTY_NAME,
                    value_type="string",
                    new_value=approved_by,
                    reason=reason,
                    approved_by=approved_by,
                ),
            )
        )
    return NomenclaturePropertyUpdateMessage(
        message_id=message_id,
        rows=tuple(rows),
        mode=mode,
        approved_by=approved_by,
        source=PROPERTY_UPDATE_SOURCE,
    )


def _serialize_snapshot_row(
    row: Mapping[str, Any],
    *,
    run: Mapping[str, Any] | None,
) -> dict[str, Any]:
    status_code = _dashboard_status(str(row.get("status") or ""))
    run_id = int(row.get("last_run_id") or (run.get("id") if run else 0) or 0)
    return {
        "proposal_id": None,
        "nomenclature_code": str(row.get("nomenclature_code") or ""),
        "nomenclature_ref": str(row.get("product_ref") or "") or None,
        "product_guid": _safe_guid(row.get("product_ref")),
        "product_name": str(row.get("name") or ""),
        "folder": str(row.get("folder") or ""),
        "action_kind": "view",
        "current_status": status_code,
        "current_status_label": _status_label(status_code),
        "target_status": None,
        "target_status_label": None,
        "proposal_status": "snapshot",
        "reason": str(row.get("reason_text") or ""),
        "facts": {"reason_codes": list(row.get("reason_codes") or [])},
        "blockers": list(row.get("blockers") or []),
        "risk_codes": list(row.get("reason_codes") or []),
        "run_id": run_id,
        "run_key": str(run.get("run_key") if run else run_id),
        "facts_hash": str(row.get("source_hash") or ""),
        "responsible_bitrix_user_id": None,
        "responsible_name": None,
        "ready": False,
        "selectable": False,
        "stale": False,
        "created_at": row.get("classified_at"),
    }


def _order_list_statement():
    return select(ProcurementOrderFormation).options(
        selectinload(ProcurementOrderFormation.lines).selectinload(
            ProcurementOrderFormationLine.classification_proposals
        )
    )


def _orders_summary(orders: Iterable[ProcurementOrderFormation]) -> dict[str, Any]:
    orders_list = list(orders)
    active_lines = [line for order in orders_list for line in order.lines if not line.removed]
    return {
        "orders": len(orders_list),
        "lines": len(active_lines),
        "quantity": sum((line.final_quantity for line in active_lines), Decimal("0")),
        "amount": sum((line.amount for line in active_lines), Decimal("0")),
    }


def _approval_result(proposal_id: int, result: str, message: str) -> dict[str, Any]:
    return {"proposal_id": proposal_id, "result": result, "message": message}


def _status_query_values(status: str) -> tuple[str, ...]:
    return ("newborn", "newborn_need") if status == "newborn" else (status,)


def _dashboard_status(status: str) -> str:
    return "newborn" if status == "newborn_need" else status


def _next_lifecycle_status(status: str) -> str | None:
    try:
        index = LIFECYCLE_ORDER.index(status)
    except ValueError:
        return None
    return LIFECYCLE_ORDER[index + 1] if index + 1 < len(LIFECYCLE_ORDER) else None


def _status_label(status: str | None) -> str:
    if status is None:
        return ""
    normalized = normalize_status(status) or status
    return LIFECYCLE_LABELS.get(normalized) or status_label(normalized) or str(status)


def _card_urgency(action_count: int, review_count: int) -> str:
    if action_count > 0:
        return "action"
    if review_count > 0:
        return "warning"
    return "normal"


def _safe_guid(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        normalized = normalize_guid(text)
    except ValueError:
        return None
    if len(normalized) != 36 or normalized.count("-") != 4:
        return None
    return normalized


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _parse_dates(value: Any) -> tuple[date, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in (_parse_date(raw) for raw in value) if item is not None)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    try:
        json.dumps(value)
    except TypeError:
        return str(value)
    return value
