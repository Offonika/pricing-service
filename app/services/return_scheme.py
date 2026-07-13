from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

import httpx
from openpyxl import Workbook
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.infrastructure.db import SqlAlchemyUnitOfWork
from app.models import ReturnSchemeAlertBatch, ReturnSchemeIncident

# Direct MSSQL mapping for the observed 1C UT schema:
# - _Document203 / _Document203_VT4966: Реализация товаров и услуг
# - _Document109 / _Document109_VT1698: Возврат товаров от покупателя
# Store is taken from line-level _Reference80 links.
# Price type is resolved from _Reference87.
# `_Document203._Fld4942RRef` and `_Document109._Fld1682RRef` are the document field
# `Ответственный`. Physical `_ReferenceNN` mapping for this field should be treated as
# schema-specific and revalidated against the live 1C catalog map.
DEFAULT_ONEC_OPERATION_SQL = """
SELECT
    event_type,
    doc_ref,
    doc_number,
    doc_datetime,
    product_ref,
    product_name,
    store_ref,
    store_name,
    employee_ref,
    employee_name,
    price_type,
    quantity,
    amount
FROM (
    SELECT
        'sale' AS event_type,
        sale._IDRRef AS doc_ref,
        sale._Number AS doc_number,
        sale._Date_Time AS doc_datetime,
        sale_line._Fld4974RRef AS product_ref,
        product._Description AS product_name,
        sale_line._Fld4983RRef AS store_ref,
        store_ref._Description AS store_name,
        sale._Fld4942RRef AS employee_ref,
        sale_actor._Description AS employee_name,
        price_type._Description AS price_type,
        sale_line._Fld4971 AS quantity,
        sale_line._Fld4982 AS amount
    FROM _Document203 AS sale
    JOIN _Document203_VT4966 AS sale_line
        ON sale_line._Document203_IDRRef = sale._IDRRef
    LEFT JOIN _Reference62 AS product
        ON product._IDRRef = sale_line._Fld4974RRef
    LEFT JOIN _Reference80 AS store_ref
        ON store_ref._IDRRef = sale_line._Fld4983RRef
    LEFT JOIN _Reference54 AS sale_actor
        ON sale_actor._IDRRef = sale._Fld4942RRef
    LEFT JOIN _Reference87 AS price_type
        ON price_type._IDRRef = sale._Fld4943RRef
    WHERE sale._Marked = 0x00
      AND sale._Posted = 0x01
      AND sale._Date_Time >= :window_start
      AND sale._Date_Time < :window_end
      AND sale_line._Fld4974RRef <> 0x00000000000000000000000000000000
      AND sale_line._Fld4983RRef <> 0x00000000000000000000000000000000
      AND sale_line._Fld4971 > 0

    UNION ALL

    SELECT
        'return' AS event_type,
        ret._IDRRef AS doc_ref,
        ret._Number AS doc_number,
        ret._Date_Time AS doc_datetime,
        ret_line._Fld1700RRef AS product_ref,
        product._Description AS product_name,
        ret_line._Fld1716RRef AS store_ref,
        store_ref._Description AS store_name,
        ret._Fld1682RRef AS employee_ref,
        return_actor._Description AS employee_name,
        CAST(NULL AS nvarchar(255)) AS price_type,
        ret_line._Fld1701 AS quantity,
        ret_line._Fld1707 AS amount
    FROM _Document109 AS ret
    JOIN _Document109_VT1698 AS ret_line
        ON ret_line._Document109_IDRRef = ret._IDRRef
    LEFT JOIN _Reference62 AS product
        ON product._IDRRef = ret_line._Fld1700RRef
    LEFT JOIN _Reference80 AS store_ref
        ON store_ref._IDRRef = ret_line._Fld1716RRef
    LEFT JOIN _Reference54 AS return_actor
        ON return_actor._IDRRef = ret._Fld1682RRef
    WHERE ret._Marked = 0x00
      AND ret._Posted = 0x01
      AND ret._Date_Time >= :window_start
      AND ret._Date_Time < :window_end
      AND ret_line._Fld1700RRef <> 0x00000000000000000000000000000000
      AND ret_line._Fld1716RRef <> 0x00000000000000000000000000000000
      AND ret_line._Fld1701 > 0
) AS operations
ORDER BY doc_datetime, doc_ref
"""

EVENT_SALE = "sale"
EVENT_RETURN = "return"
REPORT_HEADERS = [
    "Магазин",
    "Номенклатура",
    "Менеджер",
    "Реализация (розница)",
    "Возврат",
    "Реализация (не розница)",
    "Тип цен",
    "Кол-во",
    "Сумма",
]
ALERT_BATCH_PENDING = "pending"
ALERT_BATCH_DELIVERED = "delivered"
ALERT_BATCH_FAILED = "failed"


def _clean_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.hex().upper()
    value = str(value).strip()
    return value or None


def _to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _normalize_event_type(value: Any) -> str:
    normalized = (_clean_string(value) or "").lower()
    if normalized in {EVENT_SALE, "retail_sale", "non_retail_sale"}:
        return EVENT_SALE
    if normalized in {EVENT_RETURN, "customer_return"}:
        return EVENT_RETURN
    if "возврат" in normalized or "return" in normalized:
        return EVENT_RETURN
    if "реал" in normalized or "sale" in normalized:
        return EVENT_SALE
    raise ValueError(f"unsupported event_type: {value!r}")


def parse_retail_price_types(raw: str | None) -> set[str]:
    if not raw:
        return {"розница"}
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def is_retail_price_type(price_type: str | None, retail_price_types: set[str]) -> bool:
    if not price_type:
        return False
    return price_type.strip().lower() in retail_price_types


def _format_document(doc_datetime: datetime, doc_number: str) -> str:
    return f"{doc_datetime:%d.%m.%Y %H:%M:%S} №{doc_number}"


def _quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _quantize_qty(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _build_fingerprint(
    *,
    first_sale_doc_ref: str,
    return_doc_ref: str,
    second_sale_doc_ref: str,
    product_ref: str,
    store_ref: str,
) -> str:
    raw = "|".join(
        [first_sale_doc_ref, return_doc_ref, second_sale_doc_ref, product_ref, store_ref]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class OperationEvent:
    event_type: str
    doc_ref: str
    doc_number: str
    doc_datetime: datetime
    product_ref: str
    product_name: str | None
    store_ref: str
    store_name: str | None
    employee_ref: str | None
    employee_name: str | None
    price_type: str | None
    quantity: Decimal
    amount: Decimal

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> OperationEvent:
        return cls(
            event_type=_normalize_event_type(row.get("event_type")),
            doc_ref=_clean_string(row.get("doc_ref")) or "",
            doc_number=_clean_string(row.get("doc_number")) or "",
            doc_datetime=row["doc_datetime"],
            product_ref=_clean_string(row.get("product_ref")) or "",
            product_name=_clean_string(row.get("product_name")),
            store_ref=_clean_string(row.get("store_ref")) or "",
            store_name=_clean_string(row.get("store_name")),
            employee_ref=_clean_string(row.get("employee_ref")),
            employee_name=_clean_string(row.get("employee_name")),
            price_type=_clean_string(row.get("price_type")),
            quantity=_to_decimal(row.get("quantity")),
            amount=_to_decimal(row.get("amount")),
        )


@dataclass(slots=True)
class DetectedReturnSchemeIncident:
    product_ref: str
    product_name: str | None
    store_ref: str
    store_name: str | None
    manager_ref: str | None
    manager_name: str | None
    first_sale_event: OperationEvent
    return_event: OperationEvent
    second_sale_event: OperationEvent
    second_price_type: str | None
    matched_qty: Decimal
    amount: Decimal

    @property
    def fingerprint(self) -> str:
        return _build_fingerprint(
            first_sale_doc_ref=self.first_sale_event.doc_ref,
            return_doc_ref=self.return_event.doc_ref,
            second_sale_doc_ref=self.second_sale_event.doc_ref,
            product_ref=self.product_ref,
            store_ref=self.store_ref,
        )

    def to_report_row(self) -> list[Any]:
        return [
            self.store_name or self.store_ref,
            self.product_name or self.product_ref,
            self.manager_name or self.manager_ref or "",
            _format_document(self.first_sale_event.doc_datetime, self.first_sale_event.doc_number),
            _format_document(self.return_event.doc_datetime, self.return_event.doc_number),
            _format_document(
                self.second_sale_event.doc_datetime, self.second_sale_event.doc_number
            ),
            self.second_price_type or "",
            float(self.matched_qty),
            float(self.amount),
        ]


@dataclass(slots=True)
class _SaleLot:
    event: OperationEvent
    remaining_qty: Decimal


@dataclass(slots=True)
class _ReturnLot:
    first_sale_event: OperationEvent
    return_event: OperationEvent
    remaining_qty: Decimal


class OneCReturnSchemeExtractor:
    def __init__(self, onec_engine, operations_sql: str = DEFAULT_ONEC_OPERATION_SQL):
        self.onec_engine = onec_engine
        self.operations_sql = operations_sql

    def fetch_operation_events(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> list[OperationEvent]:
        query = text(self.operations_sql)
        try:
            with self.onec_engine.connect() as conn:
                rows = conn.execute(
                    query, {"window_start": window_start, "window_end": window_end}
                ).mappings()
                return [OperationEvent.from_mapping(dict(row)) for row in rows]
        except Exception as exc:
            raise RuntimeError(
                "Не удалось получить операции возвратной схемы из 1С. "
                "По умолчанию extractor читает прямой SQL по `_Document203/_Document109` "
                "для наблюдаемой схемы УТ 10.3."
            ) from exc


def detect_return_scheme_incidents(
    events: Sequence[OperationEvent],
    *,
    retail_price_types: set[str],
    window_days: int,
) -> list[DetectedReturnSchemeIncident]:
    incidents: list[DetectedReturnSchemeIncident] = []
    window = timedelta(days=window_days)
    grouped: dict[tuple[str, str], list[OperationEvent]] = {}
    for event in events:
        if not event.product_ref or not event.store_ref:
            continue
        grouped.setdefault((event.product_ref, event.store_ref), []).append(event)

    for group_events in grouped.values():
        group_events = sorted(group_events, key=lambda item: (item.doc_datetime, item.doc_ref))
        sale_lots: list[_SaleLot] = []
        return_lots: list[_ReturnLot] = []

        for event in group_events:
            if event.quantity <= 0:
                continue

            if event.event_type == EVENT_SALE and is_retail_price_type(
                event.price_type, retail_price_types
            ):
                sale_lots.append(_SaleLot(event=event, remaining_qty=event.quantity))
                continue

            if event.event_type == EVENT_RETURN:
                remaining = event.quantity
                for sale_lot in sale_lots:
                    if remaining <= 0:
                        break
                    if sale_lot.remaining_qty <= 0:
                        continue
                    if event.doc_datetime < sale_lot.event.doc_datetime:
                        continue
                    if event.doc_datetime - sale_lot.event.doc_datetime > window:
                        continue
                    matched_qty = min(sale_lot.remaining_qty, remaining)
                    if matched_qty <= 0:
                        continue
                    return_lots.append(
                        _ReturnLot(
                            first_sale_event=sale_lot.event,
                            return_event=event,
                            remaining_qty=matched_qty,
                        )
                    )
                    sale_lot.remaining_qty -= matched_qty
                    remaining -= matched_qty
                sale_lots = [lot for lot in sale_lots if lot.remaining_qty > 0]
                continue

            if event.event_type != EVENT_SALE:
                continue
            if not event.price_type or is_retail_price_type(event.price_type, retail_price_types):
                continue

            remaining = event.quantity
            for return_lot in return_lots:
                if remaining <= 0:
                    break
                if return_lot.remaining_qty <= 0:
                    continue
                if event.doc_datetime < return_lot.return_event.doc_datetime:
                    continue
                if event.doc_datetime - return_lot.first_sale_event.doc_datetime > window:
                    continue
                matched_qty = min(return_lot.remaining_qty, remaining)
                if matched_qty <= 0:
                    continue
                amount = Decimal("0")
                if event.quantity > 0:
                    amount = event.amount * matched_qty / event.quantity
                incidents.append(
                    DetectedReturnSchemeIncident(
                        product_ref=event.product_ref,
                        product_name=event.product_name,
                        store_ref=event.store_ref,
                        store_name=event.store_name,
                        manager_ref=event.employee_ref,
                        manager_name=event.employee_name,
                        first_sale_event=return_lot.first_sale_event,
                        return_event=return_lot.return_event,
                        second_sale_event=event,
                        second_price_type=event.price_type,
                        matched_qty=_quantize_qty(matched_qty),
                        amount=_quantize_amount(amount),
                    )
                )
                return_lot.remaining_qty -= matched_qty
                remaining -= matched_qty
            return_lots = [lot for lot in return_lots if lot.remaining_qty > 0]

    return incidents


def upsert_return_scheme_incidents(
    session: Session,
    incidents: Sequence[DetectedReturnSchemeIncident],
    *,
    detected_at: datetime,
) -> dict[str, list[ReturnSchemeIncident]]:
    if not incidents:
        return {"new": [], "pending_notification": [], "existing": []}

    fingerprints = [incident.fingerprint for incident in incidents]
    existing_rows = session.execute(
        select(ReturnSchemeIncident).where(ReturnSchemeIncident.fingerprint.in_(fingerprints))
    ).scalars()
    existing_by_fingerprint = {row.fingerprint: row for row in existing_rows}

    created: list[ReturnSchemeIncident] = []
    existing: list[ReturnSchemeIncident] = []
    pending_notification: list[ReturnSchemeIncident] = []

    for incident in incidents:
        row = existing_by_fingerprint.get(incident.fingerprint)
        if row is None:
            row = ReturnSchemeIncident(
                fingerprint=incident.fingerprint,
                product_ref=incident.product_ref,
                product_name=incident.product_name,
                store_ref=incident.store_ref,
                store_name=incident.store_name,
                manager_ref=incident.manager_ref,
                manager_name=incident.manager_name,
                first_sale_doc_ref=incident.first_sale_event.doc_ref,
                first_sale_doc_number=incident.first_sale_event.doc_number,
                first_sale_doc_datetime=incident.first_sale_event.doc_datetime,
                return_doc_ref=incident.return_event.doc_ref,
                return_doc_number=incident.return_event.doc_number,
                return_doc_datetime=incident.return_event.doc_datetime,
                second_sale_doc_ref=incident.second_sale_event.doc_ref,
                second_sale_doc_number=incident.second_sale_event.doc_number,
                second_sale_doc_datetime=incident.second_sale_event.doc_datetime,
                second_price_type=incident.second_price_type,
                matched_qty=incident.matched_qty,
                amount=incident.amount,
                first_detected_at=detected_at,
                last_seen_at=detected_at,
            )
            session.add(row)
            created.append(row)
            pending_notification.append(row)
            existing_by_fingerprint[incident.fingerprint] = row
            continue

        row.product_name = incident.product_name
        row.store_name = incident.store_name
        row.manager_ref = incident.manager_ref
        row.manager_name = incident.manager_name
        row.second_price_type = incident.second_price_type
        row.matched_qty = incident.matched_qty
        row.amount = incident.amount
        row.last_seen_at = detected_at
        existing.append(row)
        if row.notified_at is None:
            pending_notification.append(row)

    session.flush()
    return {"new": created, "pending_notification": pending_notification, "existing": existing}


def create_return_scheme_alert_batch(
    session: Session,
    *,
    incidents: Sequence[ReturnSchemeIncident],
    generated_at: datetime,
    window_start: datetime,
    window_end: datetime,
    report_path: Path,
    new_incidents_count: int,
) -> ReturnSchemeAlertBatch | None:
    unbatched_incidents = [
        incident
        for incident in incidents
        if incident.notified_at is None and incident.alert_batch_id is None
    ]
    if not unbatched_incidents:
        return None

    batch = ReturnSchemeAlertBatch(
        generated_at=generated_at,
        window_start=window_start,
        window_end=window_end,
        new_incidents_count=new_incidents_count,
        notification_incidents_count=len(unbatched_incidents),
        report_path=str(Path(report_path).resolve()),
        status=ALERT_BATCH_PENDING,
    )
    session.add(batch)
    session.flush()

    for incident in unbatched_incidents:
        incident.alert_batch_id = batch.id

    session.flush()
    return batch


def get_pending_return_scheme_alert_batches(
    session: Session,
) -> list[ReturnSchemeAlertBatch]:
    return (
        session.execute(
            select(ReturnSchemeAlertBatch)
            .where(ReturnSchemeAlertBatch.status == ALERT_BATCH_PENDING)
            .order_by(ReturnSchemeAlertBatch.generated_at.asc(), ReturnSchemeAlertBatch.id.asc())
        )
        .scalars()
        .all()
    )


def _count_recent_store_product_incidents(
    session: Session,
    *,
    store_ref: str,
    product_ref: str,
    since: datetime,
) -> int:
    return int(
        session.scalar(
            select(func.count(ReturnSchemeIncident.id)).where(
                ReturnSchemeIncident.store_ref == store_ref,
                ReturnSchemeIncident.product_ref == product_ref,
                ReturnSchemeIncident.second_sale_doc_datetime >= since,
            )
        )
        or 0
    )


def _count_recent_employee_incidents(
    session: Session,
    *,
    manager_ref: str | None,
    since: datetime,
) -> int:
    if not manager_ref:
        return 0
    return int(
        session.scalar(
            select(func.count(ReturnSchemeIncident.id)).where(
                ReturnSchemeIncident.manager_ref == manager_ref,
                ReturnSchemeIncident.second_sale_doc_datetime >= since,
            )
        )
        or 0
    )


def serialize_return_scheme_alert_batch(
    session: Session,
    batch: ReturnSchemeAlertBatch,
    *,
    repeat_window_days: int = 7,
) -> dict[str, Any]:
    repeat_window_start = batch.generated_at - timedelta(days=repeat_window_days)
    incidents = sorted(
        batch.incidents,
        key=lambda item: (item.second_sale_doc_datetime, item.id),
    )
    incident_payloads = []
    for incident in incidents:
        incident_payloads.append(
            {
                "id": incident.id,
                "store_ref": incident.store_ref,
                "store_name": incident.store_name,
                "product_ref": incident.product_ref,
                "product_name": incident.product_name,
                "manager_ref": incident.manager_ref,
                "manager_name": incident.manager_name,
                "second_price_type": incident.second_price_type,
                "matched_qty": float(incident.matched_qty),
                "amount": float(incident.amount),
                "first_sale_doc_number": incident.first_sale_doc_number,
                "first_sale_doc_datetime": incident.first_sale_doc_datetime.isoformat(),
                "return_doc_number": incident.return_doc_number,
                "return_doc_datetime": incident.return_doc_datetime.isoformat(),
                "second_sale_doc_number": incident.second_sale_doc_number,
                "second_sale_doc_datetime": incident.second_sale_doc_datetime.isoformat(),
                "repeat_store_product_7d_count": _count_recent_store_product_incidents(
                    session,
                    store_ref=incident.store_ref,
                    product_ref=incident.product_ref,
                    since=repeat_window_start,
                ),
                "repeat_employee_7d_count": _count_recent_employee_incidents(
                    session,
                    manager_ref=incident.manager_ref,
                    since=repeat_window_start,
                ),
            }
        )

    return {
        "id": batch.id,
        "generated_at": batch.generated_at.isoformat(),
        "window_start": batch.window_start.isoformat(),
        "window_end": batch.window_end.isoformat(),
        "new_incidents_count": batch.new_incidents_count,
        "notification_incidents_count": batch.notification_incidents_count,
        "report_path": batch.report_path,
        "status": batch.status,
        "incident_ids": [incident.id for incident in incidents],
        "incidents": incident_payloads,
        "summary": {
            "message": build_return_scheme_telegram_message(
                {
                    "generated_at": batch.generated_at.isoformat(),
                    "window_start": batch.window_start.isoformat(),
                    "window_end": batch.window_end.isoformat(),
                    "new_incidents": batch.new_incidents_count,
                    "notification_incidents": batch.notification_incidents_count,
                }
            )
        },
    }


def get_return_scheme_alert_batch(
    session: Session,
    batch_id: int,
) -> ReturnSchemeAlertBatch | None:
    return session.get(ReturnSchemeAlertBatch, batch_id)


def acknowledge_return_scheme_alert_batch(
    session: Session,
    batch_id: int,
    *,
    delivered_at: datetime | None = None,
) -> ReturnSchemeAlertBatch:
    batch = session.get(ReturnSchemeAlertBatch, batch_id)
    if batch is None:
        raise ValueError(f"return scheme alert batch {batch_id} not found")

    delivered_at = delivered_at or datetime.now()
    if batch.status != ALERT_BATCH_DELIVERED:
        batch.status = ALERT_BATCH_DELIVERED
        batch.delivered_at = delivered_at
        batch.delivery_error = None
        mark_return_scheme_incidents_notified(
            session,
            [incident.id for incident in batch.incidents],
            notified_at=delivered_at,
        )
        session.flush()
    return batch


def mark_return_scheme_alert_batch_failed(
    session: Session,
    batch_id: int,
    *,
    delivery_error: str,
) -> ReturnSchemeAlertBatch:
    batch = session.get(ReturnSchemeAlertBatch, batch_id)
    if batch is None:
        raise ValueError(f"return scheme alert batch {batch_id} not found")
    batch.status = ALERT_BATCH_FAILED
    batch.delivery_error = delivery_error
    session.flush()
    return batch


def mark_return_scheme_incidents_notified(
    session: Session,
    incident_ids: Iterable[int],
    *,
    notified_at: datetime | None = None,
) -> None:
    ids = [incident_id for incident_id in incident_ids if incident_id is not None]
    if not ids:
        return
    notified_at = notified_at or datetime.now()
    session.execute(
        update(ReturnSchemeIncident)
        .where(ReturnSchemeIncident.id.in_(ids))
        .values(notified_at=notified_at)
    )


def mark_return_scheme_incidents_notified_by_ids(
    incident_ids: Iterable[int],
    *,
    notified_at: datetime | None = None,
) -> None:
    with SqlAlchemyUnitOfWork() as unit_of_work:
        assert unit_of_work.session is not None
        mark_return_scheme_incidents_notified(
            unit_of_work.session,
            incident_ids,
            notified_at=notified_at,
        )


def acknowledge_return_scheme_alert_batch_by_id(
    batch_id: int,
    *,
    delivered_at: datetime | None = None,
) -> ReturnSchemeAlertBatch:
    with SqlAlchemyUnitOfWork() as unit_of_work:
        assert unit_of_work.session is not None
        return acknowledge_return_scheme_alert_batch(
            unit_of_work.session,
            batch_id,
            delivered_at=delivered_at,
        )


def export_return_scheme_report_xlsx(
    incidents: Sequence[ReturnSchemeIncident | DetectedReturnSchemeIncident],
    output_path: Path,
) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "return_scheme"
    sheet.append(REPORT_HEADERS)

    for incident in incidents:
        if isinstance(incident, DetectedReturnSchemeIncident):
            row = incident.to_report_row()
        else:
            row = [
                incident.store_name or incident.store_ref,
                incident.product_name or incident.product_ref,
                incident.manager_name or incident.manager_ref or "",
                _format_document(incident.first_sale_doc_datetime, incident.first_sale_doc_number),
                _format_document(incident.return_doc_datetime, incident.return_doc_number),
                _format_document(
                    incident.second_sale_doc_datetime, incident.second_sale_doc_number
                ),
                incident.second_price_type or "",
                float(incident.matched_qty),
                float(incident.amount),
            ]
        sheet.append(row)

    widths = [22, 38, 24, 28, 28, 30, 18, 12, 14]
    for idx, width in enumerate(widths, start=1):
        sheet.column_dimensions[chr(64 + idx)].width = width

    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)
    return output_path


def build_return_scheme_output_path(
    *,
    output_dir: str,
    generated_at: datetime,
) -> Path:
    base_dir = Path(output_dir)
    dated_dir = base_dir / generated_at.strftime("%Y-%m-%d")
    filename = f"return-scheme-{generated_at.strftime('%Y%m%d-%H%M%S')}.xlsx"
    return dated_dir / filename


def build_return_scheme_telegram_message(payload: dict[str, Any]) -> str:
    generated_at = payload.get("generated_at") or datetime.now().isoformat()
    return "\n".join(
        [
            "⚠️ Контроль схемы Розница -> Возврат -> Не розница",
            f"Сформировано: {generated_at}",
            f"Окно: {payload.get('window_start')} -> {payload.get('window_end')}",
            f"Получено операций: {payload.get('fetched_events', 0)}",
            f"Новых инцидентов: {payload.get('new_incidents', 0)}",
            f"К отправке: {payload.get('notification_incidents', 0)}",
        ]
    )


def send_return_scheme_telegram_report(
    *,
    token: str,
    chat_id: str,
    message: str,
    report_path: Path,
    transport: httpx.BaseTransport | None = None,
) -> None:
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    with report_path.open("rb") as fh, httpx.Client(timeout=20.0, transport=transport) as client:
        response = client.post(
            url,
            data={"chat_id": chat_id, "caption": message},
            files={"document": (report_path.name, fh)},
        )
        response.raise_for_status()
