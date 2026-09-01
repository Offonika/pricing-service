from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.procurement_order_formation import (
    ProcurementOrderFormation,
    ProcurementOrderFormationEvent,
    ProcurementOrderFormationLine,
)

LIFECYCLE_STATUS_LABELS = {
    "draft": "Черновик",
    "review": "На проверке",
    "blocked": "Заблокирован",
    "transmitting": "Передаётся в 1С",
    "active": "Активен",
    "in_transit": "В пути",
    "partially_received": "Частично поступил",
    "received": "Поступил",
    "cancelled": "Отменён",
}
ACTIVE_LIFECYCLE_STATUSES = {"active", "in_transit", "partially_received"}


@dataclass(frozen=True)
class RegistryUpsertResult:
    action: str
    order_id: int | None
    onec_ref: str
    lifecycle_status: str | None = None
    conflict: str | None = None


def decimal_value(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def date_value(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value if value.year > 1900 else None
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    return parsed if parsed.year > 1900 else None


def normalize_onec_ref(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "")
    if text.startswith("0x"):
        text = text[2:]
    if len(text) == 32 and all(char in "0123456789abcdef" for char in text):
        return "0x" + text
    return str(value or "").strip().lower()


def lifecycle_status_for_snapshot(
    snapshot: Mapping[str, Any], *, previous_status: str | None = None
) -> str:
    posted = snapshot.get("posted")
    marked = bool(snapshot.get("marked"))
    if marked or (posted is False and previous_status in ACTIVE_LIFECYCLE_STATUSES):
        return "cancelled"

    ordered = decimal_value(snapshot.get("ordered_qty"))
    open_quantity = (
        decimal_value(snapshot.get("open_qty")) if snapshot.get("open_qty") is not None else None
    )
    if open_quantity is not None and posted is not False:
        if open_quantity <= 0:
            return "received"
        if ordered > 0 and open_quantity < ordered:
            return "partially_received"

    if date_value(snapshot.get("cargo_dropoff_date")) or date_value(
        snapshot.get("supplier_dispatch_date")
    ):
        return "in_transit"
    return "active" if posted is not False else (previous_status or "review")


def lifecycle_display_status(order: ProcurementOrderFormation, blockers: Sequence[str]) -> str:
    if order.lifecycle_status == "review" and blockers:
        return "blocked"
    return order.lifecycle_status


def snapshot_checksum(snapshot: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        default=lambda value: value.isoformat() if hasattr(value, "isoformat") else str(value),
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _currency(value: Any) -> str:
    text = str(value or "RUB").strip()
    folded = text.casefold()
    if "руб" in folded or folded == "rur":
        return "RUB"
    if "юан" in folded or folded in {"rmb", "cny"}:
        return "CNY"
    if "дол" in folded or folded == "usd":
        return "USD"
    if "евро" in folded or folded == "eur":
        return "EUR"
    return text[:8] or "RUB"


def _order_candidates(
    db: Session, *, onec_ref: str, number: str, document_date: date | None
) -> tuple[ProcurementOrderFormation | None, str | None]:
    by_ref = list(
        db.scalars(
            select(ProcurementOrderFormation).where(
                func.lower(ProcurementOrderFormation.onec_document_ref) == onec_ref
            )
        ).all()
    )
    if len(by_ref) == 1:
        return by_ref[0], None
    if len(by_ref) > 1:
        return None, f"duplicate onec GUID: {onec_ref}"

    if not number or document_date is None:
        return None, None
    by_legacy_identity = list(
        db.scalars(
            select(ProcurementOrderFormation).where(
                ProcurementOrderFormation.onec_document_number == number,
                ProcurementOrderFormation.onec_document_date == document_date,
            )
        ).all()
    )
    if len(by_legacy_identity) == 1:
        return by_legacy_identity[0], None
    if len(by_legacy_identity) > 1:
        return None, f"ambiguous legacy identity: {number}/{document_date.year}"
    return None, None


def _event(
    db: Session,
    *,
    order: ProcurementOrderFormation,
    event_type: str,
    checksum: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    idempotency_key = f"onec-registry:{order.id}:{checksum}"
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
            actor="system:onec-procurement-registry",
            idempotency_key=idempotency_key,
            before=dict(before),
            after=dict(after),
            payload={"onec_ref": order.onec_document_ref, "snapshot_checksum": checksum},
        )
    )


def _sync_lines(order: ProcurementOrderFormation, lines: Sequence[Mapping[str, Any]]) -> None:
    if not lines:
        return
    existing = {line.line_number: line for line in order.lines}
    seen: set[int] = set()
    for index, source in enumerate(lines, start=1):
        line_number = int(source.get("line_no") or source.get("line_number") or index)
        seen.add(line_number)
        item_ref = normalize_onec_ref(source.get("item_ref_hex") or source.get("nomenclature_ref"))
        item_code = str(
            source.get("onec_item_code") or source.get("nomenclature_code") or ""
        ).strip()
        quantity = decimal_value(source.get("quantity"))
        price = decimal_value(source.get("price"))
        amount = decimal_value(source.get("amount"), quantity * price)
        open_quantity = (
            decimal_value(source.get("open_quantity"))
            if source.get("open_quantity") is not None
            else None
        )
        line = existing.get(line_number)
        if line is None:
            line = ProcurementOrderFormationLine(
                order=order,
                stable_key=f"{order.stable_key}:onec-line:{line_number}",
                line_number=line_number,
                bitrix_product_xml_id=item_ref or item_code or f"line-{line_number}",
                nomenclature_ref=item_ref or item_code or f"line-{line_number}",
                nomenclature_code=item_code or None,
                nomenclature_name=str(source.get("item_name") or "Позиция 1С").strip(),
                recommended_quantity=quantity,
                final_quantity=quantity,
                purchase_price=price,
                amount=amount,
                currency=order.currency,
                source_kind="onec_import",
                payload={},
            )
            order.lines.append(line)
        else:
            line.nomenclature_ref = item_ref or line.nomenclature_ref
            line.nomenclature_code = item_code or line.nomenclature_code
            line.nomenclature_name = str(source.get("item_name") or line.nomenclature_name)
            line.recommended_quantity = quantity
            line.final_quantity = quantity
            line.purchase_price = price
            line.amount = amount
            line.currency = order.currency
            line.removed = False
        line.onec_open_quantity = (
            max(open_quantity, Decimal("0")) if open_quantity is not None else None
        )
        line.onec_received_quantity = (
            max(quantity - line.onec_open_quantity, Decimal("0"))
            if line.onec_open_quantity is not None
            else None
        )
        line.payload = {
            **(line.payload or {}),
            "article_1c": str(source.get("article_1c") or "").strip(),
            "sku": str(source.get("sku") or "").strip(),
            "barcode": str(source.get("barcode") or "").strip(),
            "unit": str(source.get("unit") or "").strip(),
        }
    if order.origin == "onec_import":
        for line_number, line in existing.items():
            if line_number not in seen:
                line.removed = True


def upsert_onec_order_snapshot(
    db: Session,
    snapshot: Mapping[str, Any],
    *,
    synced_at: datetime | None = None,
) -> RegistryUpsertResult:
    synced_at = synced_at or datetime.now(UTC).replace(tzinfo=None)
    onec_ref = normalize_onec_ref(snapshot.get("onec_ref"))
    if not re.fullmatch(r"0x[0-9a-f]{32}", onec_ref):
        return RegistryUpsertResult(
            "conflict", None, onec_ref, conflict="missing or invalid onec GUID"
        )
    number = str(snapshot.get("number") or snapshot.get("onec_source_number") or "").strip()
    document_date = date_value(snapshot.get("date") or snapshot.get("order_date"))
    if not number or document_date is None:
        return RegistryUpsertResult(
            "conflict", None, onec_ref, conflict="missing canonical 1C number or date"
        )

    order, conflict = _order_candidates(
        db, onec_ref=onec_ref, number=number, document_date=document_date
    )
    if conflict:
        return RegistryUpsertResult("conflict", None, onec_ref, conflict=conflict)

    checksum = snapshot_checksum(snapshot)
    created = order is None
    if order is None:
        supplier = snapshot.get("supplier") if isinstance(snapshot.get("supplier"), Mapping) else {}
        order = ProcurementOrderFormation(
            stable_key=f"onec:supplier-order:{onec_ref}",
            status="transmitted",
            lifecycle_status="active",
            origin="onec_import",
            supplier_ref=normalize_onec_ref(
                supplier.get("onec_ref") or snapshot.get("supplier_ref")
            )
            or None,
            supplier_name=str(
                supplier.get("title") or snapshot.get("supplier_name") or "Поставщик не указан"
            ),
            contract_ref=normalize_onec_ref(snapshot.get("contract_ref")) or None,
            contract_name=str(snapshot.get("contract_name") or "Договор не указан"),
            warehouse_ref=normalize_onec_ref(
                snapshot.get("store_ref") or snapshot.get("warehouse_ref")
            )
            or None,
            warehouse_name=str(
                snapshot.get("planned_warehouse") or snapshot.get("store_name") or "Склад не указан"
            ),
            currency=_currency(snapshot.get("currency") or snapshot.get("currency_name")),
            procurement_contour=str(snapshot.get("procurement_contour_key") or "ordinary"),
            route=str(snapshot.get("procurement_contour_key") or "ordinary"),
            batch_id=f"onec-{number}",
            order_date=document_date,
            responsible_name=str(snapshot.get("responsible_name") or "").strip() or None,
            calculation_id=f"onec-import:{onec_ref}",
            source_run_id="onec-open-orders",
            onec_status="transmitted",
            onec_document_ref=onec_ref,
            onec_document_number=number,
            onec_document_date=document_date,
        )
        db.add(order)
        db.flush()

    previous = {
        "lifecycle_status": order.lifecycle_status,
        "onec_snapshot_hash": order.onec_snapshot_hash,
        "onec_open_quantity": str(order.onec_open_quantity or ""),
    }
    order.onec_document_ref = onec_ref
    order.onec_document_number = number
    order.onec_document_date = document_date
    order.onec_status = "transmitted"
    order.onec_posted = bool(snapshot.get("posted"))
    order.onec_marked = bool(snapshot.get("marked"))
    order.supplier_dispatch_date = date_value(snapshot.get("supplier_dispatch_date"))
    order.cargo_dropoff_date = date_value(snapshot.get("cargo_dropoff_date"))
    order.expected_receipt_date = date_value(snapshot.get("expected_receipt_date"))
    ordered_quantity = decimal_value(snapshot.get("ordered_qty"))
    if ordered_quantity <= 0:
        ordered_quantity = sum(
            (decimal_value(item.get("quantity")) for item in snapshot.get("lines") or []),
            Decimal("0"),
        )
    open_quantity = decimal_value(snapshot.get("open_qty"))
    order.onec_ordered_quantity = max(ordered_quantity, open_quantity)
    order.onec_open_quantity = max(open_quantity, Decimal("0"))
    order.onec_received_quantity = max(
        order.onec_ordered_quantity - order.onec_open_quantity, Decimal("0")
    )
    order.lifecycle_status = lifecycle_status_for_snapshot(
        {**snapshot, "ordered_qty": order.onec_ordered_quantity},
        previous_status=order.lifecycle_status,
    )
    order.onec_snapshot_hash = checksum
    order.last_onec_sync_at = synced_at
    order.last_onec_seen_at = synced_at
    order.onec_error = None
    order.sync_conflict = None
    _sync_lines(order, snapshot.get("lines") or [])
    after = {
        "lifecycle_status": order.lifecycle_status,
        "onec_snapshot_hash": checksum,
        "onec_open_quantity": str(order.onec_open_quantity),
    }
    if created or previous != after:
        _event(
            db,
            order=order,
            event_type="onec_order_imported" if created else "onec_order_synchronized",
            checksum=checksum,
            before=previous,
            after=after,
        )
    db.flush()
    return RegistryUpsertResult(
        "created" if created else ("noop" if previous == after else "updated"),
        order.id,
        onec_ref,
        lifecycle_status=order.lifecycle_status,
    )


def synchronize_onec_snapshots(
    db: Session, snapshots: Sequence[Mapping[str, Any]], *, synced_at: datetime | None = None
) -> list[RegistryUpsertResult]:
    synced_at = synced_at or datetime.now(UTC).replace(tzinfo=None)
    results = [upsert_onec_order_snapshot(db, item, synced_at=synced_at) for item in snapshots]
    db.commit()
    return results
