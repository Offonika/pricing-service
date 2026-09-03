from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings, get_settings
from app.infrastructure.db.engines import build_engine
from app.models.procurement_order_formation import (
    ProcurementClassificationProposal,
    ProcurementOrderFormation,
    ProcurementOrderFormationEvent,
    ProcurementOrderFormationLine,
)
from app.services.assortment_lifecycle import (
    ASSORTMENT_STATUS_LABELS,
    status_display_label,
)
from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE,
)
from app.services.bitrix_procurement_order_formation_auth import (
    ProcurementOrderFormationSession,
)
from app.services.exporters.ut103_exchange import resolve_ut103_exchange_root
from app.services.exporters.ut103_nomenclature_properties import (
    NomenclaturePropertyUpdateMessage,
    NomenclaturePropertyUpdateRow,
    PropertyUpdateExchangeResult,
)
from app.services.exporters.ut103_procurement_orders import (
    OneCReference,
    ProcurementSupplierOrder,
    ProcurementSupplierOrderExchangeResult,
    ProcurementSupplierOrderLine,
    ProcurementSupplierOrderMessage,
    build_procurement_supplier_orders_xml,
    write_procurement_supplier_orders_message,
)
from app.services.onec_nomenclature_snapshot import (
    fetch_onec_nomenclature_by_codes,
    fetch_onec_supplier_by_ref,
    main_supplier_payload,
    search_onec_suppliers,
)

# ВАЖНО: значения этих двух словарей — названия статусов в 1С и Bitrix, а не
# подписи для экрана. По ним распознаётся readback из учётной системы и
# заполняется свойство номенклатуры, поэтому переименование ломает обмен.
# Экранные подписи берутся из `status_screen_label` ниже.
MANUAL_STATUS_LABELS = {
    "working": "Рабочий",
    "matrix": "Матричный",
    "on_demand": "Под заказ",
    "replace_candidate": "Кандидат на замену",
    "nonliquid": "Кандидат на неликвид",
    "do_not_order": "Не закупать",
    "pension": "Допродаём",
}
LIFECYCLE_STATUS_LABELS = {
    "fruit": "Плод",
    "newborn": "Новорожденный",
    "new_item": "Новинка",
    "sales_start": "СП",
    "sale": "Продажа",
}
ALWAYS_BLOCKING_STATUSES = frozenset({"replace_candidate", "nonliquid", "do_not_order", "pension"})
APPROVED_PROPOSAL_STATUSES = frozenset({"approved", "sent_to_1c", "applied", "reflected"})
# Решение 2026-08-18: карточку снимают с ведения в пользу другой карточки семьи,
# поэтому код победителя обязателен. Пустым он остаётся только с явной отметкой
# «замены нет» — например когда модель снята с производства.
REPLACEMENT_REQUIRED_STATUSES = frozenset({"pension", "replace_candidate", "do_not_order"})
# Решение 2026-08-18: «Допродаём» назначает один человек, второе согласование не
# требуется. Остальные ручные статусы сохраняют запрет самоутверждения.
SELF_APPROVED_STATUSES = frozenset({"pension"})
STATUS_PROPERTY_NAME = "Статус ассортимента"
STATUS_REASON_PROPERTY_NAME = "Причина статуса ассортимента"
STATUS_CHANGED_AT_PROPERTY_NAME = "Дата изменения статуса ассортимента"
STATUS_SOURCE_PROPERTY_NAME = "Источник статуса ассортимента"
STATUS_APPROVED_BY_PROPERTY_NAME = "Утвердил статус ассортимента"
MANUAL_MINIMUM_PROPERTY_NAME = "Ручной минимальный остаток"
REVIEW_DATE_PROPERTY_NAME = "Дата пересмотра правила наличия"
PROPERTY_UPDATE_SOURCE = "pricing-service:procurement-order-formation"
MISSING_ONEC_DOCUMENT_NUMBER_ERROR = (
    "1С подтвердила обработку, но не вернула номер документа; требуется reconciliation"
)
PROCUREMENT_PROCESS_ENTITY_TYPE_ID = 1056
PROCUREMENT_PROCESS_TITLE = "Закупка/Заказ"
PROCUREMENT_PROCESS_CATEGORY_NAMES = {
    52: "Обычный",
    53: "Карго",
    12: "ВЭД импорт",
}


class VersionConflictError(ValueError):
    pass


def get_order_by_bitrix_item(db: Session, item_id: str) -> ProcurementOrderFormation:
    item_id = str(item_id).strip()
    if not item_id:
        raise ValueError("item_id is required")
    statement = _order_statement().where(
        ProcurementOrderFormation.bitrix_entity_type_id == PROCUREMENT_PROCESS_ENTITY_TYPE_ID,
        ProcurementOrderFormation.bitrix_item_id == item_id,
    )
    order = db.scalar(statement)
    if order is None:
        raise LookupError("order formation card was not found")
    return order


def get_order(db: Session, order_id: int) -> ProcurementOrderFormation:
    order = db.scalar(_order_statement().where(ProcurementOrderFormation.id == order_id))
    if order is None:
        raise LookupError("order formation card was not found")
    return order


def serialize_linked_process(order: ProcurementOrderFormation) -> dict[str, Any]:
    has_onec_document = bool(str(order.onec_document_ref or "").strip())
    canonical_link = bool(
        str(order.bitrix_item_id or "").strip()
        and order.bitrix_entity_type_id == PROCUREMENT_PROCESS_ENTITY_TYPE_ID
        and not str(order.bitrix_link_error or "").strip()
    )
    if canonical_link:
        state = "linked"
    elif has_onec_document and str(order.bitrix_link_error or "").strip():
        state = "broken"
    elif has_onec_document:
        state = "pending"
    else:
        state = "not_created"
    is_canonical_process = order.bitrix_entity_type_id == PROCUREMENT_PROCESS_ENTITY_TYPE_ID
    product_rows_state = order.bitrix_product_rows_sync_state
    if not is_canonical_process or not str(order.bitrix_item_id or "").strip():
        product_rows_state = "not_applicable"
    elif product_rows_state not in {"pending", "synced", "error"}:
        product_rows_state = "pending"
    product_rows_view: dict[str, Any] = {"rows": [], "excluded_count": 0}
    if is_canonical_process and str(order.bitrix_item_id or "").strip():
        from app.services.procurement_order_product_rows import (
            build_procurement_product_rows_view,
        )

        product_rows_view = build_procurement_product_rows_view(order)
    return {
        "state": state,
        "process_title": PROCUREMENT_PROCESS_TITLE,
        "entity_type_id": PROCUREMENT_PROCESS_ENTITY_TYPE_ID,
        "item_id": (
            str(order.bitrix_item_id or "").strip() or None if is_canonical_process else None
        ),
        "category_id": order.bitrix_category_id if is_canonical_process else None,
        "category_name": (
            PROCUREMENT_PROCESS_CATEGORY_NAMES.get(order.bitrix_category_id or 0)
            if is_canonical_process
            else None
        ),
        "stage_id": order.bitrix_stage_id if is_canonical_process else None,
        "stage_name": order.bitrix_stage_name if is_canonical_process else None,
        "checked_at": order.bitrix_link_checked_at,
        "error": str(order.bitrix_link_error or "").strip() or None,
        "product_rows_sync": {
            "state": product_rows_state,
            "expected_count": order.bitrix_product_rows_expected_count,
            "synced_count": order.bitrix_product_rows_synced_count,
            "checksum": order.bitrix_product_rows_checksum,
            "synced_at": order.bitrix_product_rows_synced_at,
            "error": str(order.bitrix_product_rows_error or "").strip() or None,
            "rows": product_rows_view["rows"],
            "excluded_count": product_rows_view["excluded_count"],
        },
    }


def list_supplier_options(
    *,
    query: str,
    limit: int = 20,
    settings: Settings | None = None,
) -> list[dict[str, str]]:
    settings = settings or get_settings()
    if not settings.onec_database_url:
        raise RuntimeError("ONEC_DATABASE_URL is not configured")
    engine = build_engine(settings.onec_database_url, pool_pre_ping=True)
    try:
        return search_onec_suppliers(engine, query=query, limit=limit)
    finally:
        engine.dispose()


def select_line_main_supplier(
    db: Session,
    order_id: int,
    line_id: int,
    values: dict[str, Any],
    session: ProcurementOrderFormationSession,
    *,
    settings: Settings | None = None,
) -> ProcurementOrderFormation:
    """Store an in-app supplier choice without writing to 1C."""

    settings = settings or get_settings()
    order = get_order(db, order_id)
    ensure_order_editable(order)
    if order.version != int(values["expected_order_version"]):
        raise VersionConflictError("order version changed; refresh the order")
    line = _line_from_order(order, line_id)
    if line.version != int(values["expected_line_version"]):
        raise VersionConflictError("order line version changed; refresh the order")
    if not line.nomenclature_code:
        raise ValueError("nomenclature code is required for supplier selection")
    if not settings.onec_database_url:
        raise RuntimeError("ONEC_DATABASE_URL is not configured")

    engine = build_engine(settings.onec_database_url, pool_pre_ping=True)
    try:
        supplier = fetch_onec_supplier_by_ref(
            engine,
            supplier_ref=str(values.get("supplier_ref") or ""),
        )
        if supplier is None:
            raise ValueError("selected supplier was not found in 1C")
        snapshot = fetch_onec_nomenclature_by_codes(engine, codes=[line.nomenclature_code])
    finally:
        engine.dispose()
    card_supplier = main_supplier_payload(snapshot.get(line.nomenclature_code) or {})
    if card_supplier and card_supplier["ref"].casefold() != supplier["ref"].casefold():
        raise ValueError("main supplier changed in 1C; refresh the order and use the 1C value")

    status = "confirmed_in_1c" if card_supplier else "pending_onec_write"
    selected_supplier = card_supplier or supplier
    before = dict(line.payload or {})
    line.payload = {
        **before,
        "main_supplier_selection": {
            **selected_supplier,
            "status": status,
            "selected_at": datetime.now(UTC).isoformat(),
            "selected_by_bitrix_user_id": session.user_id,
            "selected_by_name": session.user_name or session.actor,
        },
    }
    line.version += 1
    invalidate_order_approval(order)
    db.add(
        ProcurementOrderFormationEvent(
            order=order,
            entity_type="line",
            entity_id=str(line.id),
            event_type="line_main_supplier_selected",
            actor=session.actor,
            bitrix_user_id=session.user_id,
            user_name=session.user_name,
            before={"main_supplier_selection": before.get("main_supplier_selection")},
            after={"main_supplier_selection": line.payload["main_supplier_selection"]},
            payload={"onec_write": False},
        )
    )
    db.commit()
    return get_order(db, order_id)


def preview_supplier_distribution(
    db: Session,
    order_id: int,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    order = get_order(db, order_id)
    ensure_order_editable(order)
    if order.supplier_ref or order.supplier_code:
        raise ValueError("only the supplier review room can be distributed")
    resolved, unresolved = _resolved_line_suppliers(order, settings=settings)
    groups: list[dict[str, Any]] = []
    for supplier_ref, items in sorted(resolved.items(), key=lambda item: item[1][0][1]["name"]):
        supplier = items[0][1]
        target = _open_supplier_target(db, order, supplier_ref)
        groups.append(
            {
                "supplier_ref": supplier["ref"],
                "supplier_code": supplier["code"],
                "supplier_name": supplier["name"],
                "line_ids": [int(line.id) for line, _supplier, _status in items],
                "line_numbers": [line.line_number for line, _supplier, _status in items],
                "nomenclature_codes": [
                    str(line.nomenclature_code or "") for line, _supplier, _status in items
                ],
                "target_order_id": target.id if target else None,
                "target_order_status": "existing" if target else "new",
            }
        )
    return {
        "source_order_id": int(order.id),
        "source_order_version": order.version,
        "groups": groups,
        "unresolved_line_numbers": sorted(line.line_number for line in unresolved),
    }


def distribute_lines_by_suppliers(
    db: Session,
    order_id: int,
    *,
    expected_order_version: int,
    session: ProcurementOrderFormationSession,
    settings: Settings | None = None,
) -> tuple[ProcurementOrderFormation, list[int], int]:
    source = get_order(db, order_id)
    ensure_order_editable(source)
    if source.version != expected_order_version:
        raise VersionConflictError("order version changed; refresh the order")
    if source.supplier_ref or source.supplier_code:
        raise ValueError("only the supplier review room can be distributed")
    resolved, _unresolved = _resolved_line_suppliers(source, settings=settings)
    if not resolved:
        raise ValueError("no lines with a selected supplier to distribute")

    target_ids: list[int] = []
    moved = 0
    for supplier_ref, items in resolved.items():
        supplier = items[0][1]
        target = _open_supplier_target(db, source, supplier_ref)
        if target is None:
            target = _new_supplier_target(db, source, supplier)
            db.flush()
        ensure_order_editable(target)
        next_number = max((line.line_number for line in target.lines), default=0) + 1
        for line, effective_supplier, status in items:
            payload = dict(line.payload or {})
            payload["main_supplier_selection"] = {
                **effective_supplier,
                "status": status,
                "distributed_at": datetime.now(UTC).isoformat(),
                "distributed_by_bitrix_user_id": session.user_id,
                "distributed_by_name": session.user_name or session.actor,
            }
            line.payload = payload
            line.blockers = [
                code
                for code in list(line.blockers or [])
                if code != "supplier_1c_reference_missing"
            ]
            line.line_number = next_number
            next_number += 1
            target.lines.append(line)
            line.version += 1
            moved += 1
        invalidate_order_approval(target)
        target_ids.append(int(target.id))

    invalidate_order_approval(source)
    if not any(not line.removed for line in source.lines):
        source.status = "superseded"
    db.add(
        ProcurementOrderFormationEvent(
            order=source,
            entity_type="order",
            entity_id=str(source.id),
            event_type="supplier_review_room_distributed",
            actor=session.actor,
            bitrix_user_id=session.user_id,
            user_name=session.user_name,
            before={},
            after={"target_order_ids": target_ids, "moved_line_count": moved},
            payload={"onec_write": False},
        )
    )
    db.commit()
    return get_order(db, order_id), sorted(set(target_ids)), moved


def _resolved_line_suppliers(
    order: ProcurementOrderFormation,
    *,
    settings: Settings | None,
) -> tuple[
    dict[str, list[tuple[ProcurementOrderFormationLine, dict[str, str], str]]],
    list[ProcurementOrderFormationLine],
]:
    settings = settings or get_settings()
    active_lines = [line for line in order.lines if not line.removed]
    codes = [line.nomenclature_code for line in active_lines if line.nomenclature_code]
    if not settings.onec_database_url:
        raise RuntimeError("ONEC_DATABASE_URL is not configured")
    engine = build_engine(settings.onec_database_url, pool_pre_ping=True)
    try:
        snapshots = fetch_onec_nomenclature_by_codes(engine, codes=codes)
    finally:
        engine.dispose()
    resolved: dict[str, list[tuple[ProcurementOrderFormationLine, dict[str, str], str]]] = {}
    unresolved: list[ProcurementOrderFormationLine] = []
    for line in active_lines:
        card_supplier = main_supplier_payload(
            snapshots.get(str(line.nomenclature_code or "")) or {}
        )
        pending = (line.payload or {}).get("main_supplier_selection") or {}
        supplier = card_supplier or (
            {
                "ref": str(pending.get("ref") or "").strip(),
                "code": str(pending.get("code") or "").strip(),
                "name": str(pending.get("name") or "").strip(),
            }
            if str(pending.get("ref") or "").strip()
            else None
        )
        if not supplier:
            unresolved.append(line)
            continue
        status = "confirmed_in_1c" if card_supplier else "pending_onec_write"
        resolved.setdefault(supplier["ref"].casefold(), []).append((line, supplier, status))
    return resolved, unresolved


def _open_supplier_target(
    db: Session,
    source: ProcurementOrderFormation,
    supplier_ref: str,
) -> ProcurementOrderFormation | None:
    # A review-room line joins the latest supplier order that has not entered the
    # 1C exchange yet. The order may come from an earlier daily batch: the
    # business document remains open until it is actually sent to 1C.
    return db.scalar(
        _order_statement()
        .where(
            ProcurementOrderFormation.id != source.id,
            ProcurementOrderFormation.supplier_ref.ilike(supplier_ref),
            ProcurementOrderFormation.status.in_(("draft", "review", "error")),
            ProcurementOrderFormation.onec_status.notin_(("pending", "transmitted")),
        )
        .order_by(ProcurementOrderFormation.updated_at.desc(), ProcurementOrderFormation.id.desc())
    )


def _new_supplier_target(
    db: Session,
    source: ProcurementOrderFormation,
    supplier: dict[str, str],
) -> ProcurementOrderFormation:
    previous = db.scalar(
        select(ProcurementOrderFormation)
        .where(ProcurementOrderFormation.supplier_ref.ilike(supplier["ref"]))
        .order_by(ProcurementOrderFormation.updated_at.desc(), ProcurementOrderFormation.id.desc())
    )
    digest = hashlib.sha256(f"{source.stable_key}|{supplier['ref']}".encode()).hexdigest()[:20]
    target = ProcurementOrderFormation(
        stable_key=f"{source.stable_key}:supplier:{digest}:v{source.version}",
        status="draft",
        version=1,
        supplier_ref=supplier["ref"],
        supplier_code=supplier["code"] or None,
        supplier_name=supplier["name"] or "Не определён",
        contract_ref=previous.contract_ref if previous else source.contract_ref,
        contract_code=previous.contract_code if previous else source.contract_code,
        contract_name=previous.contract_name if previous else source.contract_name,
        warehouse_ref=source.warehouse_ref,
        warehouse_code=source.warehouse_code,
        warehouse_name=source.warehouse_name,
        currency=source.currency,
        procurement_contour=source.procurement_contour,
        route=source.route,
        batch_id=source.batch_id,
        order_date=source.order_date,
        responsible_bitrix_user_id=source.responsible_bitrix_user_id,
        responsible_name=source.responsible_name,
        calculation_id=source.calculation_id,
        source_run_id=source.source_run_id,
        onec_status="not_sent",
        payload={
            **(source.payload or {}),
            "distributed_from_order_id": source.id,
            "main_supplier_write_status": "pending_onec_write",
        },
    )
    db.add(target)
    return target


def serialize_order(order: ProcurementOrderFormation) -> dict[str, Any]:
    active_lines = [line for line in order.lines if not line.removed]
    line_payloads = [serialize_line(line) for line in order.lines]
    total_amount = sum((line.amount for line in active_lines), Decimal("0"))
    blockers = order_blockers(order)
    from app.services.procurement_order_registry import (
        LIFECYCLE_STATUS_LABELS,
        lifecycle_display_status,
    )

    display_status = lifecycle_display_status(order, blockers)
    return {
        "id": order.id,
        "stable_key": order.stable_key,
        "status": order.status,
        "lifecycle_status": display_status,
        "lifecycle_status_label": LIFECYCLE_STATUS_LABELS[display_status],
        "origin": order.origin,
        "version": order.version,
        "bitrix_entity_type_id": order.bitrix_entity_type_id,
        "bitrix_item_id": order.bitrix_item_id,
        "bitrix_category_id": order.bitrix_category_id,
        "bitrix_stage_id": order.bitrix_stage_id,
        "bitrix_item_url": order.bitrix_item_url,
        "linked_process": serialize_linked_process(order),
        "supplier_ref": order.supplier_ref,
        "supplier_code": order.supplier_code,
        "supplier_name": order.supplier_name,
        "contract_ref": order.contract_ref,
        "contract_code": order.contract_code,
        "contract_name": order.contract_name,
        "warehouse_ref": order.warehouse_ref,
        "warehouse_code": order.warehouse_code,
        "warehouse_name": order.warehouse_name,
        "currency": order.currency,
        "procurement_contour": order.procurement_contour,
        "route": order.route,
        "batch_id": order.batch_id,
        "order_date": order.order_date,
        "responsible_bitrix_user_id": order.responsible_bitrix_user_id,
        "responsible_name": order.responsible_name,
        "calculation_id": order.calculation_id,
        "source_run_id": order.source_run_id,
        "approved_version": order.approved_version,
        "approved_at": order.approved_at,
        "approved_by_bitrix_user_id": order.approved_by_bitrix_user_id,
        "approved_by_name": order.approved_by_name,
        "onec_status": order.onec_status,
        "onec_message_id": order.onec_message_id,
        "onec_document_ref": order.onec_document_ref,
        "onec_document_number": order.onec_document_number,
        "onec_document_date": order.onec_document_date,
        "onec_error": order.onec_error,
        "onec_posted": order.onec_posted,
        "onec_marked": order.onec_marked,
        "supplier_dispatch_date": order.supplier_dispatch_date,
        "cargo_dropoff_date": order.cargo_dropoff_date,
        "expected_receipt_date": order.expected_receipt_date,
        "onec_ordered_quantity": order.onec_ordered_quantity,
        "onec_open_quantity": order.onec_open_quantity,
        "onec_received_quantity": order.onec_received_quantity,
        "last_onec_sync_at": order.last_onec_sync_at,
        "last_onec_seen_at": order.last_onec_seen_at,
        "sync_conflict": order.sync_conflict,
        "label_source": serialize_order_label_source(order),
        "blockers": blockers,
        "blocker_details": order_blocker_details(order),
        "total_amount": total_amount,
        "lines": line_payloads,
        "manual_status_options": manual_status_screen_options(),
        "supplier_profile": supplier_profile(order),
    }


def serialize_order_label_source(order: ProcurementOrderFormation) -> dict[str, Any] | None:
    exchange_number = str(order.onec_document_number or "").strip()
    if exchange_number:
        return {
            "origin": "exchange",
            "onec_number": exchange_number,
            "onec_date": order.onec_document_date,
            "linked_at": None,
        }
    manual_number = str(order.label_onec_document_number or "").strip()
    if manual_number:
        return {
            "origin": "manual",
            "onec_number": manual_number,
            "onec_date": order.label_onec_document_date,
            "linked_at": order.label_source_linked_at,
        }
    return None


def serialize_line(line: ProcurementOrderFormationLine) -> dict[str, Any]:
    latest = latest_classification_proposal(line)
    effective_status = effective_assortment_status(line)
    payload = dict(line.payload or {})
    photos = _line_photos(payload)
    return {
        "id": line.id,
        "line_number": line.line_number,
        "version": line.version,
        "bitrix_product_id": line.bitrix_product_id,
        "bitrix_product_xml_id": line.bitrix_product_xml_id,
        "nomenclature_ref": line.nomenclature_ref,
        "nomenclature_code": line.nomenclature_code,
        "nomenclature_name": line.nomenclature_name,
        "recommended_quantity": line.recommended_quantity,
        "final_quantity": line.final_quantity,
        "onec_open_quantity": line.onec_open_quantity,
        "onec_received_quantity": line.onec_received_quantity,
        "purchase_price": line.purchase_price,
        "amount": line.amount,
        "currency": line.currency,
        "source_kind": line.source_kind,
        "explicit_demand": line.explicit_demand,
        "risk_level": line.risk_level,
        "risk_codes": list(line.risk_codes or []),
        "recommendation_reason": line.recommendation_reason,
        "blockers": line_blockers(line),
        "blocker_details": line_blocker_details(line),
        "assortment_status": line.assortment_status,
        "lifecycle_status": line.lifecycle_status,
        "quality": line.quality,
        "procurement_profile": line.procurement_profile,
        "manual_minimum": line.manual_minimum,
        "payload": payload,
        "display_family_recommendation": _display_family_recommendation(payload),
        "removed": line.removed,
        "effective_assortment_status": effective_status,
        "effective_assortment_status_label": status_screen_label(effective_status),
        "latest_classification": serialize_proposal(latest) if latest else None,
        "photo_thumbnail_url": _photo_url(photos, "thumbnail") or _photo_url(photos, "original"),
        "photo_original_url": _photo_url(photos, "original"),
        "product_card_url": _safe_media_url(payload.get("product_card_url")) or None,
        "photo_source": _payload_text(payload, "photo_source"),
        "photo_count": len(photos),
        "profitability_pct": _payload_decimal(
            payload,
            "profitability_pct",
            "gross_margin_pct",
            "margin_pct",
        ),
        "profitability_calculation_basis": _payload_text(
            payload, "profitability_calculation_basis"
        ),
        "profitability_status": _payload_text(payload, "profitability_status"),
        "profitability_source": _payload_text(payload, "profitability_source"),
        "profitability_explanation": _profitability_explanation(
            _payload_text(payload, "profitability_status")
        ),
        "metrics_as_of": _payload_text(payload, "metrics_as_of"),
        "metrics_window_days": _payload_int(payload, "metrics_window_days"),
        "product_defect_pct": _payload_decimal(payload, "product_defect_pct"),
        "product_defect_history_units": _payload_int(payload, "product_defect_history_units"),
        "product_defect_confidence": _payload_text(payload, "product_defect_confidence"),
        "product_defect_source": _payload_text(payload, "product_defect_source"),
        "supplier_defect_pct": _payload_decimal(
            payload,
            "supplier_defect_pct",
            "defect_pct",
        ),
        "supplier_defect_history_units": _payload_int(
            payload,
            "supplier_defect_history_units",
            "defect_history_units",
        ),
        "supplier_defect_confidence": _payload_text(payload, "supplier_defect_confidence"),
        "supplier_defect_attribution": _payload_text(payload, "supplier_defect_attribution"),
        "supplier_defect_source_status": _payload_text(payload, "supplier_defect_source_status"),
        "price_change_pct": _payload_decimal(payload, "price_change_pct"),
        "price_change_status": _payload_text(payload, "price_change_status"),
        "price_history_count": _payload_int(payload, "price_history_count"),
        "price_history_currency_ref": _payload_text(payload, "price_history_currency_ref"),
        "price_history_expected_currency": _payload_text(
            payload, "price_history_expected_currency"
        ),
        "price_history_available_currencies": _payload_list(
            payload, "price_history_available_currencies"
        ),
        "supplier_prepare_days": _payload_int(
            payload,
            "supplier_prepare_days",
            "recommended_supplier_prepare_days",
        ),
        "logistics_days": _payload_int(
            payload,
            "logistics_days",
            "recommended_logistics_days",
        ),
        "lead_time_days": _payload_int(payload, "lead_time_days"),
        "lead_time_source_level": _payload_text(
            payload,
            "lead_time_source_level",
            "lead_time_match_level",
        ),
        "lead_time_confidence": _payload_text(payload, "lead_time_confidence"),
        "supplier_selection_rule": _payload_text(payload, "supplier_selection_rule"),
        "supplier_selection_reason": _payload_text(payload, "supplier_selection_reason"),
        "supplier_cost_tie_pct": _payload_decimal(payload, "supplier_cost_tie_pct"),
        "supplier_price_candidate_count": _payload_int(payload, "supplier_price_candidate_count"),
        "supplier_price_min": _payload_decimal(payload, "supplier_price_min"),
        "supplier_selected_purchase_price": _payload_decimal(
            payload, "supplier_selected_purchase_price"
        ),
        "supplier_selected_price_currency": _payload_text(
            payload, "supplier_selected_price_currency"
        ),
        "delivery_days": _payload_int(
            payload,
            "delivery_days",
            "recommended_supplier_prepare_days",
        ),
    }


def _display_family_recommendation(payload: dict[str, Any]) -> dict[str, Any] | None:
    value = payload.get("display_family_recommendation")
    return dict(value) if isinstance(value, dict) else None


def supplier_profile(order: ProcurementOrderFormation) -> dict[str, Any]:
    payload = dict(order.payload or {})
    raw = payload.get("supplier_profile")
    profile = dict(raw) if isinstance(raw, dict) else {}
    advantages = profile.get("advantages")
    if isinstance(advantages, str):
        advantages = [item.strip() for item in advantages.split(";") if item.strip()]
    elif not isinstance(advantages, list):
        advantages = []
    qualification_class = _payload_text(
        profile,
        "qualification_class",
        "supplier_class",
    )
    data_values = [
        qualification_class,
        _payload_text(profile, "qualification_label"),
        _payload_decimal(profile, "profitability_pct"),
        _payload_decimal(profile, "defect_pct"),
        _payload_int(profile, "defect_history_units"),
        _payload_decimal(profile, "on_time_pct"),
        _payload_text(profile, "payment_terms"),
        _payload_int(profile, "credit_days"),
        _payload_decimal(profile, "credit_limit"),
        _payload_int(profile, "history_order_count"),
        _payload_text(profile, "updated_at"),
        *advantages,
    ]
    populated = sum(value not in (None, "") for value in data_values)
    return {
        "qualification_class": qualification_class.upper() if qualification_class else None,
        "qualification_label": _payload_text(profile, "qualification_label"),
        "profitability_pct": _payload_decimal(profile, "profitability_pct"),
        "defect_pct": _payload_decimal(profile, "defect_pct"),
        "defect_history_units": _payload_int(profile, "defect_history_units"),
        "on_time_pct": _payload_decimal(profile, "on_time_pct"),
        "payment_terms": _payload_text(profile, "payment_terms"),
        "credit_days": _payload_int(profile, "credit_days"),
        "credit_limit": _payload_decimal(profile, "credit_limit"),
        "advantages": [str(item).strip() for item in advantages if str(item).strip()],
        "history_order_count": _payload_int(profile, "history_order_count"),
        "updated_at": _payload_text(profile, "updated_at") or None,
        "data_status": "ready" if populated >= 5 else "partial" if populated else "missing",
    }


def serialize_proposal(proposal: ProcurementClassificationProposal) -> dict[str, Any]:
    return {
        "id": proposal.id,
        "status": proposal.status,
        "previous_status": proposal.previous_status,
        "proposed_status": proposal.proposed_status,
        "proposed_status_label": status_screen_label(proposal.proposed_status),
        "reason": proposal.reason,
        "manual_minimum": proposal.manual_minimum,
        "review_date": proposal.review_date,
        "replacement_sku_code": proposal.replacement_sku_code,
        "replacement_sku_name": proposal.replacement_sku_name,
        "blocks_order_line": proposal.blocks_order_line,
        "requested_at": proposal.requested_at,
        "requested_by_bitrix_user_id": proposal.requested_by_bitrix_user_id,
        "requested_by_name": proposal.requested_by_name,
        "approved_at": proposal.approved_at,
        "approved_by_bitrix_user_id": proposal.approved_by_bitrix_user_id,
        "approved_by_name": proposal.approved_by_name,
        "rejected_at": proposal.rejected_at,
        "rejected_by_bitrix_user_id": proposal.rejected_by_bitrix_user_id,
        "rejected_by_name": proposal.rejected_by_name,
        "rejection_reason": proposal.rejection_reason,
        "onec_status": proposal.onec_status,
        "onec_message_id": proposal.onec_message_id,
        "onec_error": proposal.onec_error,
        "bitrix_readback_value": proposal.bitrix_readback_value,
        "reflected_at": proposal.reflected_at,
    }


def update_order_conditions(
    db: Session,
    order_id: int,
    values: dict[str, Any],
) -> ProcurementOrderFormation:
    order = get_order(db, order_id)
    ensure_order_editable(order)
    expected_order_version = values.pop("expected_order_version", None)
    if expected_order_version is not None and order.version != int(expected_order_version):
        raise VersionConflictError("order version changed; refresh the order")
    allowed_fields = {
        "supplier_ref",
        "supplier_code",
        "supplier_name",
        "contract_ref",
        "contract_code",
        "contract_name",
        "warehouse_ref",
        "warehouse_code",
        "warehouse_name",
        "currency",
        "procurement_contour",
        "route",
        "batch_id",
        "order_date",
        "responsible_bitrix_user_id",
        "responsible_name",
    }
    changed = False
    for field_name, value in values.items():
        if field_name not in allowed_fields or value is None:
            continue
        normalized = value.strip() if isinstance(value, str) else value
        if getattr(order, field_name) != normalized:
            setattr(order, field_name, normalized)
            changed = True
    if changed:
        invalidate_order_approval(order)
        db.commit()
    return get_order(db, order_id)


def update_order_line(
    db: Session,
    order_id: int,
    line_id: int,
    values: dict[str, Any],
) -> ProcurementOrderFormation:
    order = get_order(db, order_id)
    ensure_order_editable(order)
    line = _line_from_order(order, line_id)
    expected_order_version = values.pop("expected_order_version", None)
    expected_line_version = values.pop("expected_line_version", None)
    removal_reason = str(values.pop("removal_reason", "") or "").strip()
    replacement_sku_code = str(values.pop("replacement_sku_code", "") or "").strip()
    removal_actor = str(values.pop("_removal_actor", "") or "").strip()
    removal_actor_name = str(values.pop("_removal_actor_name", "") or "").strip()
    if expected_order_version is not None and order.version != int(expected_order_version):
        raise VersionConflictError("order version changed; refresh the order")
    if expected_line_version is not None and line.version != int(expected_line_version):
        raise VersionConflictError("order line version changed; refresh the order")
    changed = False
    manual_overrides = dict((line.payload or {}).get("manual_overrides") or {})
    for field_name in ("final_quantity", "purchase_price"):
        value = values.get(field_name)
        if value is None:
            continue
        decimal_value = Decimal(str(value))
        if decimal_value < 0:
            raise ValueError(f"{field_name} cannot be negative")
        if getattr(line, field_name) != decimal_value:
            setattr(line, field_name, decimal_value)
            manual_overrides[field_name] = True
            changed = True
    for field_name in ("removed", "explicit_demand"):
        value = values.get(field_name)
        if value is not None and getattr(line, field_name) != bool(value):
            if field_name == "removed" and bool(value) and not removal_reason:
                raise ValueError("removal reason is required")
            setattr(line, field_name, bool(value))
            changed = True
    if changed:
        payload = dict(line.payload or {})
        if values.get("removed") is True:
            payload["manual_removal"] = {
                "reason": removal_reason,
                "replacement_sku_code": replacement_sku_code or None,
                "actor": removal_actor or None,
                "actor_name": removal_actor_name or None,
                "removed_at": datetime.now(UTC).isoformat(),
            }
        elif values.get("removed") is False:
            previous_removal = dict(payload.get("manual_removal") or {})
            payload["manual_removal"] = {
                **previous_removal,
                "restored_at": datetime.now(UTC).isoformat(),
                "restored_by": removal_actor or None,
                "restored_by_name": removal_actor_name or None,
            }
        line.payload = {
            **payload,
            "manual_overrides": manual_overrides,
        }
        line.amount = _money(line.final_quantity * line.purchase_price)
        line.version += 1
        invalidate_order_approval(order)
        db.commit()
    return get_order(db, order_id)


def create_classification_proposal(
    db: Session,
    order_id: int,
    line_id: int,
    values: dict[str, Any],
    session: ProcurementOrderFormationSession,
) -> ProcurementOrderFormation:
    order = get_order(db, order_id)
    ensure_order_editable(order)
    line = _line_from_order(order, line_id)
    expected_order_version = values.pop("expected_order_version", None)
    expected_line_version = values.pop("expected_line_version", None)
    if expected_order_version is not None and order.version != int(expected_order_version):
        raise VersionConflictError("order version changed; refresh the order")
    if expected_line_version is not None and line.version != int(expected_line_version):
        raise VersionConflictError("order line version changed; refresh the order")
    proposed_status = normalize_manual_status(values.get("proposed_status"))
    reason = str(values.get("reason") or "").strip()
    if not reason:
        raise ValueError("classification reason is required")
    manual_minimum_raw = values.get("manual_minimum")
    manual_minimum = Decimal(str(manual_minimum_raw)) if manual_minimum_raw is not None else None
    if manual_minimum is not None and manual_minimum < 0:
        raise ValueError("manual minimum cannot be negative")
    review_date = values.get("review_date")
    if manual_minimum is not None and review_date is None:
        raise ValueError("review date is required when manual minimum is set")
    replacement_code, replacement_name = resolve_replacement_sku(
        db,
        proposed_status,
        values.get("replacement_sku_code"),
        no_replacement=bool(values.get("no_replacement")),
    )

    for proposal in line.classification_proposals:
        if proposal.status == "proposed":
            proposal.status = "superseded"

    proposal = ProcurementClassificationProposal(
        line=line,
        status="proposed",
        previous_status=effective_assortment_status(line),
        proposed_status=proposed_status,
        reason=reason,
        manual_minimum=manual_minimum,
        review_date=review_date,
        replacement_sku_code=replacement_code,
        replacement_sku_name=replacement_name,
        blocks_order_line=classification_blocks_line(
            proposed_status,
            explicit_demand=line.explicit_demand,
        ),
        requested_by_actor=session.actor,
        requested_by_bitrix_user_id=session.user_id,
        requested_by_name=session.user_name or session.actor,
        idempotency_key=f"proc-class:{line.stable_key}:{uuid.uuid4().hex}",
    )
    db.add(proposal)
    if proposed_status in SELF_APPROVED_STATUSES:
        _apply_classification_approval(proposal, line, session)
    invalidate_order_approval(order)
    db.commit()
    return get_order(db, order_id)


def resolve_replacement_sku(
    db: Session,
    proposed_status: str,
    raw_code: Any,
    *,
    no_replacement: bool,
) -> tuple[str | None, str | None]:
    """Код карточки-победителя семьи для статусов, снимающих позицию с ведения."""
    code = str(raw_code or "").strip()
    if proposed_status not in REPLACEMENT_REQUIRED_STATUSES:
        if not code:
            return None, None
    elif not code:
        if not no_replacement:
            raise ValueError(
                "replacement nomenclature code is required for status "
                f"{proposed_status!r}; set no_replacement when the model is discontinued"
            )
        return None, None
    if len(code) > 64:
        raise ValueError("replacement nomenclature code is too long")
    bind = db.get_bind()
    if not inspect(bind).has_table(ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.name):
        # Витрина классификации ещё не построена: код сохраняем как есть, чтобы
        # решение менеджера не блокировалось состоянием служебной таблицы.
        return code, None
    row = (
        db.execute(
            select(
                ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.c.nomenclature_code,
                ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.c.name,
            ).where(ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.c.nomenclature_code == code)
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ValueError(f"replacement nomenclature code was not found: {code}")
    return str(row["nomenclature_code"]), str(row["name"] or "") or None


def _apply_classification_approval(
    proposal: ProcurementClassificationProposal,
    line: ProcurementOrderFormationLine,
    session: ProcurementOrderFormationSession,
) -> None:
    """Согласование ручного статуса; см. approve_classification_proposal."""
    proposal.approved_at = datetime.now(UTC).replace(tzinfo=None)
    proposal.approved_by_actor = session.actor
    proposal.approved_by_bitrix_user_id = session.user_id
    proposal.approved_by_name = session.user_name or session.actor
    # Ручные статусы являются внутренним решением pricing-service. Исторический
    # общий флаг property apply больше не может превратить это решение в XML для
    # УТ 10.3: иначе снова появятся два источника жизненного статуса.
    proposal.onec_message_id = None
    proposal.onec_status = "not_applicable"
    proposal.status = "approved"
    proposal.payload = {
        **(proposal.payload or {}),
        "storage": "pricing-service",
        "legacy_onec_export_disabled": True,
    }
    line.version += 1


def approve_classification_proposal(
    db: Session,
    order_id: int,
    line_id: int,
    proposal_id: int,
    session: ProcurementOrderFormationSession,
    *,
    settings: Settings | None = None,
    commit: bool = True,
) -> tuple[ProcurementOrderFormation, ProcurementClassificationProposal, str, str, Path | None]:
    settings = settings or get_settings()
    order = get_order(db, order_id)
    line = _line_from_order(order, line_id)
    proposal = next(
        (item for item in line.classification_proposals if item.id == proposal_id),
        None,
    )
    if proposal is None:
        raise LookupError("classification proposal was not found")
    if proposal.status != "proposed":
        raise ValueError("only proposed classification can be approved")
    # Решение 2026-08-18: «Допродаём» назначает один человек, поэтому для него не
    # нужны ни отдельный утверждающий, ни второй сотрудник.
    if proposal.proposed_status not in SELF_APPROVED_STATUSES:
        ensure_classification_approver(session.user_id, settings=settings)
        if str(proposal.requested_by_bitrix_user_id) == str(session.user_id):
            raise PermissionError("classification proposal cannot be self-approved")

    mode = "internal"
    xml_preview = ""
    written_path: Path | None = None
    _apply_classification_approval(proposal, line, session)
    invalidate_order_approval(order)
    if commit:
        db.commit()
    else:
        db.flush()
    refreshed_order = get_order(db, order_id)
    refreshed_line = _line_from_order(refreshed_order, line_id)
    refreshed_proposal = next(
        item for item in refreshed_line.classification_proposals if item.id == proposal_id
    )
    return refreshed_order, refreshed_proposal, mode, xml_preview, written_path


def reject_classification_proposal(
    db: Session,
    order_id: int,
    line_id: int,
    proposal_id: int,
    values: dict[str, Any],
    session: ProcurementOrderFormationSession,
    *,
    settings: Settings | None = None,
    commit: bool = True,
) -> tuple[ProcurementOrderFormation, ProcurementClassificationProposal]:
    settings = settings or get_settings()
    ensure_classification_approver(session.user_id, settings=settings)
    order = get_order(db, order_id)
    ensure_order_editable(order)
    line = _line_from_order(order, line_id)
    expected_order_version = int(values.get("expected_order_version") or 0)
    expected_line_version = int(values.get("expected_line_version") or 0)
    if order.version != expected_order_version:
        raise VersionConflictError("order version changed; refresh the order")
    if line.version != expected_line_version:
        raise VersionConflictError("order line version changed; refresh the order")
    proposal = next(
        (item for item in line.classification_proposals if item.id == proposal_id),
        None,
    )
    if proposal is None:
        raise LookupError("classification proposal was not found")
    if proposal.status != "proposed":
        raise ValueError("only proposed classification can be rejected")
    if str(proposal.requested_by_bitrix_user_id) == str(session.user_id):
        raise PermissionError("classification proposal cannot be self-rejected")
    reason = str(values.get("reason") or "").strip()
    if not reason:
        raise ValueError("classification rejection reason is required")
    proposal.status = "rejected"
    proposal.rejected_at = datetime.now(UTC).replace(tzinfo=None)
    proposal.rejected_by_actor = session.actor
    proposal.rejected_by_bitrix_user_id = session.user_id
    proposal.rejected_by_name = session.user_name or session.actor
    proposal.rejection_reason = reason
    proposal.onec_status = "not_sent"
    proposal.onec_message_id = None
    proposal.onec_error = None
    proposal.payload = {
        **(proposal.payload or {}),
        "rejection": {"reason": reason, "onec_write": False},
    }
    line.version += 1
    invalidate_order_approval(order)
    if commit:
        db.commit()
    else:
        db.flush()
    refreshed_order = get_order(db, order_id)
    refreshed_line = _line_from_order(refreshed_order, line_id)
    refreshed_proposal = next(
        item for item in refreshed_line.classification_proposals if item.id == proposal_id
    )
    return refreshed_order, refreshed_proposal


def approve_order(
    db: Session,
    order_id: int,
    session: ProcurementOrderFormationSession,
) -> ProcurementOrderFormation:
    order = get_order(db, order_id)
    blockers = order_blockers(order)
    if blockers:
        raise ValueError("order has blockers: " + "; ".join(blockers))
    order.status = "approved"
    order.lifecycle_status = "review"
    order.approved_version = order.version
    order.approved_at = datetime.now(UTC).replace(tzinfo=None)
    order.approved_by_actor = session.actor
    order.approved_by_bitrix_user_id = session.user_id
    order.approved_by_name = session.user_name or session.actor
    db.commit()
    return get_order(db, order_id)


def transmit_order(
    db: Session,
    order_id: int,
    session: ProcurementOrderFormationSession,
    *,
    settings: Settings | None = None,
) -> tuple[ProcurementOrderFormation, str, str, str, Path | None]:
    settings = settings or get_settings()
    order = get_order(db, order_id)
    blockers = order_blockers(order)
    if blockers:
        raise ValueError("order has blockers: " + "; ".join(blockers))
    mode = "apply" if settings.procurement_order_formation_onec_apply_enabled else "dry_run"
    expected_message_id = f"proc-order-{order.id}-v{order.version}"
    already_processed = order.onec_message_id == expected_message_id and (
        (mode == "dry_run" and order.onec_status == "dry_run")
        or (mode == "apply" and order.onec_status in {"pending", "transmitted"})
    )
    if already_processed:
        return (
            order,
            mode,
            expected_message_id,
            str((order.payload or {}).get("xml_preview") or ""),
            None,
        )
    order.approved_version = order.version
    order.approved_at = datetime.now(UTC).replace(tzinfo=None)
    order.approved_by_actor = session.actor
    order.approved_by_bitrix_user_id = session.user_id
    order.approved_by_name = session.user_name or session.actor
    message = build_order_message(order, mode=mode, approved_by=session.user_name or session.actor)
    xml_preview = build_procurement_supplier_orders_xml(message).decode("windows-1251")
    written_path: Path | None = None
    order.onec_message_id = message.message_id
    order.onec_error = None
    order.payload = {
        **(order.payload or {}),
        "xml_preview": xml_preview,
        "transmission_mode": mode,
    }
    if mode == "apply":
        exchange_root = resolve_ut103_exchange_root(None)
        written_path = write_procurement_supplier_orders_message(exchange_root, message)
        order.status = "transmitting"
        order.lifecycle_status = "transmitting"
        order.onec_status = "pending"
    else:
        order.status = "draft"
        order.lifecycle_status = "draft"
        order.onec_status = "dry_run"
    db.commit()
    order = get_order(db, order_id)
    return order, mode, message.message_id, xml_preview, written_path


def record_order_exchange_result(
    db: Session,
    result: ProcurementSupplierOrderExchangeResult,
) -> ProcurementOrderFormation | None:
    order = db.scalar(
        select(ProcurementOrderFormation).where(
            ProcurementOrderFormation.onec_message_id == result.message_id
        )
    )
    if order is None:
        return None
    item = result.item_results[0] if result.item_results else None
    document_number = str(item.onec_document_number or "").strip() if item else ""
    document_date = (
        date.fromisoformat(item.onec_document_date)
        if item is not None and item.onec_document_date
        else None
    )
    document_ref = str(item.onec_document_ref or "").strip() if item else ""
    if result.ok and item is not None and document_number and document_ref:
        from app.services.procurement_order_registry import normalize_onec_ref

        order.status = "transmitted"
        order.lifecycle_status = "active"
        order.onec_status = "transmitted"
        order.onec_document_ref = normalize_onec_ref(document_ref)
        order.onec_document_number = document_number
        order.onec_document_date = document_date
        order.onec_error = None
        if order.bitrix_entity_type_id != PROCUREMENT_PROCESS_ENTITY_TYPE_ID:
            order.bitrix_link_error = None
    else:
        order.status = "error"
        order.lifecycle_status = "review"
        order.onec_status = "error"
        if item is not None:
            order.onec_document_ref = item.onec_document_ref or None
            order.onec_document_date = document_date
        order.onec_document_number = None
        if result.ok and item is not None and (not document_number or not document_ref):
            order.onec_error = (
                MISSING_ONEC_DOCUMENT_NUMBER_ERROR
                if not document_number
                else "1С вернула номер заказа без GUID; требуется reconciliation"
            )
        else:
            order.onec_error = result.errors or (item.message if item else "1C transfer failed")
    db.commit()
    return get_order(db, order.id)


def mark_transmitted_order_for_number_reconciliation(
    db: Session, order_id: int
) -> tuple[ProcurementOrderFormation, dict[str, Any], dict[str, Any], bool]:
    order = get_order(db, order_id)
    if str(order.onec_document_number or "").strip():
        raise ValueError("У заказа уже заполнен номер документа 1С")
    if (
        order.status == "error"
        and order.onec_status == "error"
        and order.onec_error == MISSING_ONEC_DOCUMENT_NUMBER_ERROR
    ):
        state = {
            "status": order.status,
            "onec_status": order.onec_status,
            "onec_document_number": order.onec_document_number,
            "onec_error": order.onec_error,
        }
        return order, state, state, False
    if order.status != "transmitted" and order.onec_status != "transmitted":
        raise ValueError("Заказ не находится в ошибочном статусе transmitted без номера")
    before = {
        "status": order.status,
        "onec_status": order.onec_status,
        "onec_document_number": order.onec_document_number,
        "onec_error": order.onec_error,
    }
    order.status = "error"
    order.onec_status = "error"
    order.onec_error = MISSING_ONEC_DOCUMENT_NUMBER_ERROR
    db.flush()
    after = {
        "status": order.status,
        "onec_status": order.onec_status,
        "onec_document_number": order.onec_document_number,
        "onec_error": order.onec_error,
    }
    return order, before, after, True


def record_property_update_exchange_result(
    db: Session,
    result: PropertyUpdateExchangeResult,
) -> ProcurementClassificationProposal | None:
    proposal = db.scalar(
        select(ProcurementClassificationProposal).where(
            ProcurementClassificationProposal.onec_message_id == result.message_id
        )
    )
    if proposal is None:
        return None
    conflict = any(
        "conflict" in f"{item.result} {item.message}".casefold()
        or "конфликт" in f"{item.result} {item.message}".casefold()
        for item in result.item_results
    )
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
    return proposal


def build_order_message(
    order: ProcurementOrderFormation,
    *,
    mode: str,
    approved_by: str,
) -> ProcurementSupplierOrderMessage:
    active_lines = [line for line in order.lines if not line.removed]
    message_id = f"proc-order-{order.id}-v{order.version}"
    supplier_order = ProcurementSupplierOrder(
        idempotency_key=f"proc-order:{order.stable_key}:v{order.version}",
        order_date=order.order_date,
        procurement_contour=order.procurement_contour,
        supplier=OneCReference(
            ref=order.supplier_ref or "",
            code=order.supplier_code or "",
            name=order.supplier_name,
        ),
        contract=OneCReference(
            ref=order.contract_ref or "",
            code=order.contract_code or "",
            name=order.contract_name,
        ),
        warehouse=OneCReference(
            ref=order.warehouse_ref or "",
            code=order.warehouse_code or "",
            name=order.warehouse_name,
        ),
        currency=order.currency,
        bitrix_item_url=order.bitrix_item_url or "",
        confirmation_id=f"order:{order.id}:v{order.version}",
        calculation_id=order.calculation_id,
        lines=tuple(
            ProcurementSupplierOrderLine(
                line_number=line.line_number,
                nomenclature=OneCReference(
                    ref=line.nomenclature_ref,
                    code=line.nomenclature_code or "",
                    name=line.nomenclature_name,
                ),
                quantity=line.final_quantity,
                price=line.purchase_price,
                currency=line.currency,
                calculation_line_id=line.stable_key,
                bitrix_line_id=str(line.id),
                comment=line.recommendation_reason or "",
            )
            for line in active_lines
        ),
        draft_only=True,
        approved_by=approved_by,
    )
    return ProcurementSupplierOrderMessage(
        message_id=message_id,
        orders=(supplier_order,),
        mode=mode,
        approved_by=approved_by,
    )


def build_classification_update_message(
    proposal: ProcurementClassificationProposal,
    *,
    line: ProcurementOrderFormationLine,
    mode: str,
) -> NomenclaturePropertyUpdateMessage:
    nomenclature_code = str(line.nomenclature_code or "").strip()
    if not nomenclature_code:
        raise ValueError("1C nomenclature code is required for classification update")
    approved_by = proposal.approved_by_name or proposal.approved_by_actor or ""
    changed_at = (proposal.approved_at or datetime.now()).date()
    base_key = proposal.idempotency_key
    rows = [
        NomenclaturePropertyUpdateRow(
            idempotency_key=f"{base_key}:status",
            nomenclature_code=nomenclature_code,
            property_name=STATUS_PROPERTY_NAME,
            value_type="property_value",
            new_value_name=status_label(proposal.proposed_status) or proposal.proposed_status,
            new_value_tag=proposal.proposed_status,
            expected_current_value_name=status_label(proposal.previous_status) or "",
            expected_current_value_tag=normalize_status(proposal.previous_status) or "",
            reason=proposal.reason,
            approved_by=approved_by,
        ),
        NomenclaturePropertyUpdateRow(
            idempotency_key=f"{base_key}:reason",
            nomenclature_code=nomenclature_code,
            property_name=STATUS_REASON_PROPERTY_NAME,
            value_type="string",
            new_value=proposal.reason,
            reason=proposal.reason,
            approved_by=approved_by,
        ),
        NomenclaturePropertyUpdateRow(
            idempotency_key=f"{base_key}:changed-at",
            nomenclature_code=nomenclature_code,
            property_name=STATUS_CHANGED_AT_PROPERTY_NAME,
            value_type="date",
            new_value=changed_at,
            reason=proposal.reason,
            approved_by=approved_by,
        ),
        NomenclaturePropertyUpdateRow(
            idempotency_key=f"{base_key}:source",
            nomenclature_code=nomenclature_code,
            property_name=STATUS_SOURCE_PROPERTY_NAME,
            value_type="string",
            new_value=PROPERTY_UPDATE_SOURCE,
            reason=proposal.reason,
            approved_by=approved_by,
        ),
        NomenclaturePropertyUpdateRow(
            idempotency_key=f"{base_key}:approved-by",
            nomenclature_code=nomenclature_code,
            property_name=STATUS_APPROVED_BY_PROPERTY_NAME,
            value_type="string",
            new_value=approved_by,
            reason=proposal.reason,
            approved_by=approved_by,
        ),
    ]
    if proposal.manual_minimum is not None:
        rows.append(
            NomenclaturePropertyUpdateRow(
                idempotency_key=f"{base_key}:manual-minimum",
                nomenclature_code=nomenclature_code,
                property_name=MANUAL_MINIMUM_PROPERTY_NAME,
                value_type="number",
                new_value=proposal.manual_minimum,
                reason=proposal.reason,
                approved_by=approved_by,
            )
        )
    if proposal.review_date is not None:
        rows.append(
            NomenclaturePropertyUpdateRow(
                idempotency_key=f"{base_key}:review-date",
                nomenclature_code=nomenclature_code,
                property_name=REVIEW_DATE_PROPERTY_NAME,
                value_type="date",
                new_value=proposal.review_date,
                reason=proposal.reason,
                approved_by=approved_by,
            )
        )
    return NomenclaturePropertyUpdateMessage(
        message_id=f"proc-classification-{proposal.id or uuid.uuid4().hex}",
        rows=tuple(rows),
        mode=mode,
        approved_by=approved_by,
        source=PROPERTY_UPDATE_SOURCE,
    )


def order_blockers(order: ProcurementOrderFormation) -> list[str]:
    blockers: list[str] = []
    required_references = (
        ("supplier", order.supplier_ref, order.supplier_code),
        ("contract", order.contract_ref, order.contract_code),
        ("warehouse", order.warehouse_ref, order.warehouse_code),
    )
    for label, ref, code in required_references:
        if not (str(ref or "").strip() or str(code or "").strip()):
            blockers.append(f"{label}_1c_reference_missing")
    if not order.currency.strip():
        blockers.append("currency_missing")
    active_lines = [line for line in order.lines if not line.removed]
    if not active_lines:
        blockers.append("order_has_no_active_lines")
    for line in active_lines:
        for blocker in line_blockers(line):
            blockers.append(f"line_{line.line_number}:{blocker}")
    return _unique(blockers)


def line_blockers(line: ProcurementOrderFormationLine) -> list[str]:
    imported_from_onec = line.source_kind == "onec_import"
    blockers = [
        blocker
        for blocker in (line.blockers or [])
        if not (
            imported_from_onec and blocker in {"catalog_product_missing", "catalog_xml_id_mismatch"}
        )
    ]
    if not imported_from_onec:
        if not str(line.bitrix_product_id or "").strip():
            blockers.append("catalog_product_missing")
        if normalize_guid(line.bitrix_product_xml_id) != normalize_guid(line.nomenclature_ref):
            blockers.append("catalog_xml_id_mismatch")
    if line.final_quantity <= 0:
        blockers.append("quantity_must_be_positive")
    if line.purchase_price <= 0:
        blockers.append("purchase_price_must_be_positive")
    price_change = _payload_decimal(line.payload or {}, "price_change_pct")
    if price_change is not None and abs(price_change) > Decimal("10"):
        blockers.append("purchase_price_change_over_10_pct")
    supplier_defect = _payload_decimal(line.payload or {}, "supplier_defect_pct")
    supplier_defect_basis = _payload_int(line.payload or {}, "supplier_defect_history_units")
    supplier_defect_attribution = _payload_text(line.payload or {}, "supplier_defect_attribution")
    if (
        supplier_defect_attribution == "supplier_exact"
        and supplier_defect is not None
        and supplier_defect > Decimal("10")
        and (supplier_defect_basis or 0) >= 100
    ):
        blockers.append("supplier_defect_over_10_pct_reliable")
    latest = latest_classification_proposal(line)
    if latest and latest.status == "proposed":
        blockers.append("classification_approval_pending")
    effective_status = effective_assortment_status(line)
    if classification_blocks_line(effective_status, explicit_demand=line.explicit_demand):
        blockers.append(f"classification_blocks_order:{effective_status}")
    return _unique(blockers)


def line_blocker_details(line: ProcurementOrderFormationLine) -> list[dict[str, Any]]:
    return [
        _blocker_detail(
            code,
            line=line,
            scope="line",
        )
        for code in line_blockers(line)
    ]


def order_blocker_details(order: ProcurementOrderFormation) -> list[dict[str, Any]]:
    by_number = {line.line_number: line for line in order.lines if not line.removed}
    details: list[dict[str, Any]] = []
    for raw_code in order_blockers(order):
        line_number: int | None = None
        code = raw_code
        if raw_code.startswith("line_") and ":" in raw_code:
            prefix, code = raw_code.split(":", 1)
            try:
                line_number = int(prefix.removeprefix("line_"))
            except ValueError:
                line_number = None
        details.append(
            _blocker_detail(
                code,
                line=by_number.get(line_number) if line_number is not None else None,
                scope="order",
                line_number=line_number,
            )
        )
    return details


def _blocker_detail(
    code: str,
    *,
    line: ProcurementOrderFormationLine | None,
    scope: str,
    line_number: int | None = None,
) -> dict[str, Any]:
    payload = dict(line.payload or {}) if line is not None else {}
    evidence: dict[str, Any] = {}
    severity = "hard"
    message = _BLOCKER_MESSAGES.get(code, code.replace("_", " "))
    actions = _resolution_actions(code, has_line=line is not None)

    if code == "batch_error_suspected":
        returned = _payload_decimal(payload, "batch_error_return_qty")
        share = _payload_decimal(payload, "batch_error_share_pct")
        evidence = {
            "return_qty": returned,
            "share_pct": share,
            "minimum_return_qty": Decimal("5"),
            "minimum_share_pct": Decimal("40"),
            "window_days": 90,
            "suspected_batch": _payload_text(payload, "suspected_batch") or None,
        }
        if returned is None or share is None:
            severity = "technical"
            message = (
                "Не хватает данных для подтверждения подозрения на партийную ошибку. "
                "Выполните повторный расчёт."
            )
        else:
            message = (
                "Подозрение на партийную ошибку: "
                f"{_return_count_text(returned)} качества «Новый», "
                f"{_decimal_text_ru(share)}% от продаж за 90 дней "
                "(порог: 5 возвратов и 40%)."
            )
    elif code == "defect_rate_suspected":
        returned = _payload_decimal(payload, "defect_return_qty")
        share = _payload_decimal(payload, "defect_share_pct")
        evidence = {
            "return_qty": returned,
            "share_pct": share,
            "minimum_return_qty": Decimal("5"),
            "minimum_share_pct": Decimal("5"),
            "window_days": 90,
        }
        if returned is None or share is None:
            severity = "technical"
            message = (
                "Не хватает данных для подтверждения высокого процента брака. "
                "Выполните повторный расчёт."
            )
        else:
            message = (
                f"Подтверждённый брак: {_decimal_text_ru(share)}% "
                f"({_return_count_text(returned)} за 90 дней)."
            )
    elif code == "supplier_defect_over_10_pct_reliable" and line is not None:
        defect_pct = _payload_decimal(payload, "supplier_defect_pct", "defect_pct")
        history_units = _payload_int(payload, "supplier_defect_history_units")
        evidence = {
            "defect_pct": defect_pct,
            "history_units": history_units,
            "minimum_defect_pct": Decimal("10"),
            "minimum_history_units": 100,
        }
        if defect_pct is None or history_units is None:
            severity = "technical"
            message = (
                "Не хватает данных для подтверждения брака поставщика. "
                "Выполните повторный расчёт."
            )
        else:
            message = (
                f"Брак поставщика {_decimal_text_ru(defect_pct)}% на базе "
                f"{history_units} шт. (порог: больше 10% на базе от 100 шт.)."
            )
    elif code == "purchase_price_change_over_10_pct" and line is not None:
        price_change_pct = _payload_decimal(payload, "price_change_pct")
        evidence = {
            "price_change_pct": price_change_pct,
            "maximum_change_pct": Decimal("10"),
            "history_count": _payload_int(payload, "price_history_count"),
        }
        message = (
            f"Закупочная цена изменилась на {_decimal_text_ru(price_change_pct)}% (порог: 10%)."
            if price_change_pct is not None
            else "Не хватает истории для проверки изменения закупочной цены."
        )

    return {
        "code": code,
        "scope": scope,
        "severity": severity,
        "line_id": line.id if line is not None else None,
        "line_number": line.line_number if line is not None else line_number,
        "message": message,
        "evidence": evidence,
        "resolution_actions": actions,
    }


def _resolution_actions(code: str, *, has_line: bool) -> list[dict[str, Any]]:
    if (
        code
        in {
            "batch_error_suspected",
            "defect_rate_suspected",
            "supplier_defect_over_10_pct_reliable",
        }
        and has_line
    ):
        return [
            {"kind": "remove_line", "label": "Исключить строку", "requires_reason": True},
            {
                "kind": "remove_with_replacement",
                "label": "Исключить и указать «Взамен ведём»",
                "requires_reason": True,
                "requires_replacement": True,
            },
            {"kind": "recalculate", "label": "Дождаться нового расчёта"},
        ]
    if code.startswith("classification_") and has_line:
        return [{"kind": "review_classification", "label": "Принять решение по классификации"}]
    if code in {"quantity_must_be_positive", "purchase_price_must_be_positive"} and has_line:
        return [{"kind": "update_line", "label": "Исправить строку"}]
    if code == "purchase_price_change_over_10_pct" and has_line:
        return [
            {"kind": "update_line", "label": "Проверить цену"},
            {"kind": "recalculate", "label": "Обновить расчёт"},
        ]
    if code.endswith("_1c_reference_missing") or code == "currency_missing":
        return [{"kind": "update_order", "label": "Заполнить условия проекта"}]
    return [{"kind": "recalculate", "label": "Обновить расчёт"}]


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _decimal_text_ru(value: Decimal) -> str:
    return _decimal_text(value).replace(".", ",")


def _return_count_text(value: Decimal) -> str:
    text = _decimal_text_ru(value)
    if value != value.to_integral_value():
        return f"{text} возврата"
    count = abs(int(value))
    last_two = count % 100
    last = count % 10
    if 11 <= last_two <= 14:
        word = "возвратов"
    elif last == 1:
        word = "возврат"
    elif 2 <= last <= 4:
        word = "возврата"
    else:
        word = "возвратов"
    return f"{text} {word}"


_BLOCKER_MESSAGES = {
    "supplier_1c_reference_missing": "Не указан поставщик 1С.",
    "contract_1c_reference_missing": "Не указан договор поставщика.",
    "warehouse_1c_reference_missing": "Не указан склад получения.",
    "currency_missing": "Не указана валюта заказа.",
    "order_has_no_active_lines": "В проекте не осталось активных строк.",
    "catalog_product_missing": "Не найдена точная карточка товара.",
    "catalog_xml_id_mismatch": "Карточка товара не совпадает с номенклатурой 1С.",
    "quantity_must_be_positive": "Количество должно быть больше нуля.",
    "purchase_price_must_be_positive": "Закупочная цена должна быть больше нуля.",
    "classification_approval_pending": "Ожидается решение по классификации.",
    "display_family_manual_approval_required": (
        "Требуется проверить и подтвердить распределение заказа внутри семейства дисплеев."
    ),
    # Совместимость с проектами, сохранёнными до унификации имени блокера.
    "display_family_recommendation_review_required": (
        "Требуется проверить и подтвердить распределение заказа внутри семейства дисплеев."
    ),
}


def classification_blocks_line(status: str | None, *, explicit_demand: bool) -> bool:
    normalized = normalize_status(status)
    if normalized in ALWAYS_BLOCKING_STATUSES:
        return True
    return normalized == "on_demand" and not explicit_demand


def effective_assortment_status(line: ProcurementOrderFormationLine) -> str | None:
    for proposal in sorted(
        line.classification_proposals,
        key=lambda item: (item.created_at or datetime.min, item.id or 0),
        reverse=True,
    ):
        if proposal.status in APPROVED_PROPOSAL_STATUSES:
            return proposal.proposed_status
    return normalize_status(line.assortment_status) or line.assortment_status


def latest_classification_proposal(
    line: ProcurementOrderFormationLine,
) -> ProcurementClassificationProposal | None:
    if not line.classification_proposals:
        return None
    return max(
        line.classification_proposals,
        key=lambda item: (item.created_at or datetime.min, item.id or 0),
    )


def invalidate_order_approval(order: ProcurementOrderFormation) -> None:
    order.version += 1
    order.approved_version = None
    order.approved_at = None
    order.approved_by_actor = None
    order.approved_by_bitrix_user_id = None
    order.approved_by_name = None
    if order.status in {"approved", "review", "transmitting", "error"}:
        order.status = "draft"
        order.lifecycle_status = "draft"
    if order.onec_status not in {"transmitted"}:
        order.onec_status = "not_sent"
        order.onec_message_id = None
        order.onec_error = None


def ensure_order_editable(order: ProcurementOrderFormation) -> None:
    if (
        order.status in {"approved", "transmitting", "transmitted"}
        or order.approved_version is not None
        or order.onec_status
        in {
            "pending",
            "transmitted",
        }
    ):
        raise ValueError("approved order is read-only; create a new revision")


def ensure_classification_approver(user_id: str, *, settings: Settings) -> None:
    configured = {
        str(item).strip()
        for item in settings.procurement_order_formation_classification_approver_user_ids
        if str(item).strip()
    }
    if not configured:
        raise RuntimeError("classification approver user IDs are not configured")
    if str(user_id).strip() not in configured:
        raise PermissionError("user cannot approve product classification")


def normalize_manual_status(value: Any) -> str:
    normalized = normalize_status(value)
    if normalized not in MANUAL_STATUS_LABELS:
        raise ValueError(
            "manual classification status must be one of: " + ", ".join(MANUAL_STATUS_LABELS)
        )
    return normalized


def normalize_status(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    known = {**MANUAL_STATUS_LABELS, **LIFECYCLE_STATUS_LABELS}
    if text in known:
        return text
    # Значения в 1С заводятся под действующими названиями (решение 2026-08-18),
    # а прежние остались в исторических карточках. Понимаем оба набора, иначе
    # readback молча висит в "pending".
    by_label = {label.casefold(): code for code, label in known.items()}
    for code, label in ASSORTMENT_STATUS_LABELS.items():
        by_label.setdefault(label.casefold(), str(code))
    return by_label.get(text.casefold(), text)


def status_label(status: str | None) -> str | None:
    """Название статуса для 1С и Bitrix. В обмен уходит именно оно."""

    normalized = normalize_status(status)
    if normalized is None:
        return None
    return {**MANUAL_STATUS_LABELS, **LIFECYCLE_STATUS_LABELS}.get(normalized, str(status))


def status_screen_label(status: str | None) -> str | None:
    """Подпись статуса для экрана: действующее название плюс прежнее в скобках."""

    normalized = normalize_status(status)
    if normalized is None:
        return None
    if normalized in ASSORTMENT_STATUS_LABELS:
        return status_display_label(normalized)
    return status_label(normalized)


def manual_status_screen_options() -> dict[str, str]:
    """Варианты ручного статуса для выпадающего списка приложения."""

    return {
        code: status_screen_label(code) or label for code, label in MANUAL_STATUS_LABELS.items()
    }


def normalize_guid(value: str | None) -> str:
    text = str(value or "").strip().strip("{}").lower()
    if text.startswith("0x") and len(text) == 34:
        return onec_binary_ref_to_guid(text)
    if len(text) == 32 and all(character in "0123456789abcdef" for character in text):
        return onec_binary_ref_to_guid(text)
    return text


def onec_binary_ref_to_guid(value: str) -> str:
    text = str(value or "").strip().lower().removeprefix("0x")
    if len(text) != 32 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError("1C binary reference must contain 16 hexadecimal bytes")
    return "-".join((text[24:32], text[20:24], text[16:20], text[0:4], text[4:16]))


def _line_from_order(
    order: ProcurementOrderFormation,
    line_id: int,
) -> ProcurementOrderFormationLine:
    line = next((item for item in order.lines if item.id == line_id), None)
    if line is None:
        raise LookupError("order line was not found")
    return line


def _order_statement():
    return select(ProcurementOrderFormation).options(
        selectinload(ProcurementOrderFormation.lines).selectinload(
            ProcurementOrderFormationLine.classification_proposals
        )
    )


def _line_photos(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw_photos = payload.get("photos") or payload.get("product_photos") or []
    if not isinstance(raw_photos, list):
        raw_photos = [raw_photos]
    photos: list[dict[str, str]] = []
    for raw in raw_photos:
        if isinstance(raw, str):
            url = _safe_media_url(raw)
            if url:
                photos.append({"thumbnail": url, "original": url})
            continue
        if not isinstance(raw, dict):
            continue
        thumbnail = _safe_media_url(
            raw.get("thumbnail") or raw.get("thumbnail_url") or raw.get("preview_url")
        )
        original = _safe_media_url(raw.get("original") or raw.get("original_url") or raw.get("url"))
        if thumbnail or original:
            photos.append(
                {
                    "thumbnail": thumbnail or original,
                    "original": original,
                }
            )
    if not photos:
        thumbnail = _safe_media_url(
            payload.get("photo_thumbnail_url")
            or payload.get("preview_image_url")
            or payload.get("image_url")
        )
        original = _safe_media_url(
            payload.get("photo_original_url")
            or payload.get("detail_image_url")
            or payload.get("photo_url")
            or payload.get("image_url")
        )
        if thumbnail or original:
            photos.append(
                {
                    "thumbnail": thumbnail or original,
                    "original": original,
                }
            )
    return photos


def _photo_url(photos: list[dict[str, str]], kind: str) -> str | None:
    if not photos:
        return None
    return photos[0].get(kind) or None


def _safe_media_url(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith(("https://", "http://", "/")):
        return text
    return ""


def _payload_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value).strip() or None
    return None


def _profitability_explanation(status: str | None) -> str | None:
    return {
        "revenue_missing": "Чистая выручка за период отсутствует или равна нулю",
        "cost_missing": "Себестоимость за период отсутствует или равна нулю",
        "history_missing": "Нет истории продаж и себестоимости за период",
    }.get(status or "")


def _payload_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := str(item or "").strip())]


def _payload_decimal(payload: dict[str, Any], *keys: str) -> Decimal | None:
    value = _payload_text(payload, *keys)
    if value is None:
        return None
    try:
        return Decimal(value.replace(" ", "").replace(",", "."))
    except (ValueError, ArithmeticError):
        return None


def _payload_int(payload: dict[str, Any], *keys: str) -> int | None:
    value = _payload_decimal(payload, *keys)
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, ArithmeticError):
        return None


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
