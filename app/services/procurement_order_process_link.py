from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.procurement_order_formation import (
    ProcurementOrderFormation,
    ProcurementOrderFormationEvent,
)
from app.services.procurement_order_formation import PROCUREMENT_PROCESS_ENTITY_TYPE_ID
from app.services.procurement_order_registry import normalize_onec_ref


@dataclass(frozen=True)
class ProcurementProcessCardSnapshot:
    item_id: str
    onec_ref: str
    onec_number: str
    onec_date: date | None
    category_id: int | None
    stage_id: str
    stage_name: str = ""
    entity_type_id: int = PROCUREMENT_PROCESS_ENTITY_TYPE_ID


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _audit_event(
    db: Session,
    *,
    order: ProcurementOrderFormation,
    event_type: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    actor: str,
) -> None:
    encoded = json.dumps(after, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:24]
    idempotency_key = f"procurement-process-link:{order.id}:{event_type}:{digest}"
    if db.scalar(
        select(ProcurementOrderFormationEvent).where(
            ProcurementOrderFormationEvent.idempotency_key == idempotency_key
        )
    ):
        return
    db.add(
        ProcurementOrderFormationEvent(
            order_id=order.id,
            entity_type="order",
            entity_id=str(order.id),
            event_type=event_type,
            actor=actor,
            idempotency_key=idempotency_key,
            before=dict(before),
            after=dict(after),
            payload={"entity_type_id": PROCUREMENT_PROCESS_ENTITY_TYPE_ID},
        )
    )


def _link_state(order: ProcurementOrderFormation) -> dict[str, Any]:
    return {
        "bitrix_entity_type_id": order.bitrix_entity_type_id,
        "bitrix_item_id": order.bitrix_item_id,
        "bitrix_category_id": order.bitrix_category_id,
        "bitrix_stage_id": order.bitrix_stage_id,
        "bitrix_stage_name": order.bitrix_stage_name,
        "bitrix_link_error": order.bitrix_link_error,
    }


def _safe_error_message(error: BaseException | str) -> str:
    message = str(error or "").strip() or "Неизвестная ошибка синхронизации"
    message = re.sub(r"https?://\S+", "[url]", message, flags=re.IGNORECASE)
    return message[:1000]


def record_procurement_process_sync_failure(
    db: Session,
    order_id: int,
    error: BaseException | str,
    *,
    confirmed_broken: bool = False,
    actor: str = "system:procurement-process-immediate-sync",
    checked_at: datetime | None = None,
) -> ProcurementOrderFormation:
    """Keep transient failures pending and make confirmed identity errors visible."""

    order = db.get(ProcurementOrderFormation, order_id)
    if order is None:
        raise LookupError("order formation card was not found")
    checked_at = (checked_at or datetime.now(UTC)).replace(tzinfo=None)
    before = _link_state(order)
    message = _safe_error_message(error)
    order.bitrix_link_checked_at = checked_at
    if confirmed_broken:
        order.bitrix_link_error = message
    elif order.bitrix_entity_type_id != PROCUREMENT_PROCESS_ENTITY_TYPE_ID:
        order.bitrix_link_error = None
    order.payload = {
        **(order.payload or {}),
        "bitrix_process_sync": {
            "state": "broken" if confirmed_broken else "pending",
            "last_error": message,
            "checked_at": checked_at.isoformat(),
        },
    }
    after = _link_state(order)
    _audit_event(
        db,
        order=order,
        event_type=(
            "bitrix_process_link_failed" if confirmed_broken else "bitrix_process_sync_deferred"
        ),
        before=before,
        after={**after, "sync_error": message},
        actor=actor,
    )
    db.commit()
    db.refresh(order)
    return order


def _identity_error(
    order: ProcurementOrderFormation, card: ProcurementProcessCardSnapshot
) -> str | None:
    expected_number = _clean(order.onec_document_number)
    if card.onec_number and expected_number and card.onec_number != expected_number:
        return (
            "Карточка процесса найдена по GUID 1С, но номер документа не совпадает: "
            f"{expected_number} / {card.onec_number}"
        )
    if card.onec_date and order.onec_document_date and card.onec_date != order.onec_document_date:
        return (
            "Карточка процесса найдена по GUID 1С, но дата документа не совпадает: "
            f"{order.onec_document_date.isoformat()} / {card.onec_date.isoformat()}"
        )
    if card.entity_type_id != PROCUREMENT_PROCESS_ENTITY_TYPE_ID:
        return f"Карточка относится к неподдерживаемому процессу {card.entity_type_id}"
    return None


def reconcile_procurement_order_process_links(
    db: Session,
    cards: Sequence[ProcurementProcessCardSnapshot],
    *,
    checked_at: datetime | None = None,
    actor: str = "system:procurement-process-link-reconciliation",
    mark_missing: bool = True,
) -> dict[str, int]:
    """Reconcile existing 1C orders with the canonical Bitrix process by 1C GUID."""

    checked_at = (checked_at or datetime.now(UTC)).replace(tzinfo=None)
    cards_by_ref: dict[str, list[ProcurementProcessCardSnapshot]] = defaultdict(list)
    for card in cards:
        ref = normalize_onec_ref(card.onec_ref)
        if ref and card.item_id:
            cards_by_ref[ref].append(card)

    orders = list(
        db.scalars(
            select(ProcurementOrderFormation).where(
                ProcurementOrderFormation.onec_document_ref.is_not(None),
                ProcurementOrderFormation.onec_document_number.is_not(None),
            )
        ).all()
    )
    item_owners = {
        _clean(order.bitrix_item_id): order.id
        for order in orders
        if order.bitrix_entity_type_id == PROCUREMENT_PROCESS_ENTITY_TYPE_ID
        and _clean(order.bitrix_item_id)
    }
    summary = {"checked": 0, "linked": 0, "unchanged": 0, "broken": 0}

    for order in orders:
        ref = normalize_onec_ref(order.onec_document_ref)
        if not mark_missing and ref not in cards_by_ref:
            continue
        summary["checked"] += 1
        before = _link_state(order)
        matches = cards_by_ref.get(ref, [])
        error: str | None = None
        card: ProcurementProcessCardSnapshot | None = None
        if not matches:
            error = "Карточка смарт-процесса Закупка/Заказ не найдена по GUID документа 1С"
        elif len(matches) > 1:
            error = "По GUID документа 1С найдено несколько карточек смарт-процесса"
        else:
            card = matches[0]
            error = _identity_error(order, card)
            owner_id = item_owners.get(card.item_id)
            if not error and owner_id not in (None, order.id):
                error = f"Карточка смарт-процесса уже связана с заказом #{owner_id}"

        order.bitrix_link_checked_at = checked_at
        if error or card is None:
            order.bitrix_link_error = error
            order.payload = {
                **(order.payload or {}),
                "bitrix_process_sync": {
                    "state": "broken",
                    "last_error": error,
                    "checked_at": checked_at.isoformat(),
                },
            }
            after = _link_state(order)
            if before != after:
                _audit_event(
                    db,
                    order=order,
                    event_type="bitrix_process_link_failed",
                    before=before,
                    after=after,
                    actor=actor,
                )
            summary["broken"] += 1
            continue

        order.bitrix_entity_type_id = PROCUREMENT_PROCESS_ENTITY_TYPE_ID
        order.bitrix_item_id = card.item_id
        order.bitrix_category_id = card.category_id
        order.bitrix_stage_id = card.stage_id or None
        order.bitrix_stage_name = card.stage_name or None
        order.bitrix_item_url = None
        order.bitrix_link_error = None
        order.payload = {
            **(order.payload or {}),
            "bitrix_process_sync": {
                "state": "linked",
                "last_error": None,
                "checked_at": checked_at.isoformat(),
            },
        }
        item_owners[card.item_id] = order.id
        after = _link_state(order)
        if before == after:
            summary["unchanged"] += 1
        else:
            _audit_event(
                db,
                order=order,
                event_type="bitrix_process_linked",
                before=before,
                after=after,
                actor=actor,
            )
            summary["linked"] += 1

    db.flush()
    return summary
