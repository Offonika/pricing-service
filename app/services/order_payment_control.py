from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine

MONEY_QUANTUM = Decimal("0.01")
RESERVATION_TOLERANCE = Decimal("0.001")
ZERO_REF = bytes(16)

# Причины закрытия заказа, которые не считаются отменой для клиента.
DEFAULT_CLOSURE_ALLOWED_REASONS = ["Исполнение заказа", "Частичное исполнение заказа"]


@dataclass(frozen=True)
class OneCOrderPaymentSnapshot:
    document_number: str
    amount: Decimal | None
    marked: bool
    posted: bool
    revision: str
    order_ref: bytes | None = None
    warehouse_ref: bytes | None = None


@dataclass(frozen=True)
class OneCOrderLine:
    line_number: int
    product_ref: bytes
    characteristic_ref: bytes
    series_ref: bytes
    placement_ref: bytes
    line_unit_ref: bytes
    storage_unit_ref: bytes
    quantity: Decimal
    coefficient: Decimal
    storage_coefficient: Decimal


@dataclass(frozen=True)
class OneCOrderReserve:
    warehouse_ref: bytes
    product_ref: bytes
    characteristic_ref: bytes
    series_ref: bytes
    quantity: Decimal


@dataclass(frozen=True)
class OneCOrderClosure:
    document_number: str
    closed_at: datetime | None
    reason: str


@dataclass(frozen=True)
class OrderPaymentDecision:
    check_id: str
    allowed: bool
    reason: str
    site_order_number: str
    site_amount: Decimal
    payment_amount: Decimal
    onec_amount: Decimal | None
    onec_document_number: str | None
    onec_revision: str | None
    checked_at: datetime
    onec_posted: bool | None = None
    onec_closure_document: str | None = None
    onec_closure_reason: str | None = None
    reservation_state: str = "MISMATCH"
    reservation_quantity_match: bool = False
    source_warehouse_xml_id: str | None = None
    reservation_confirmed_at: datetime | None = None
    confirmed_ready_at: datetime | None = None


ONEC_ORDER_PAYMENT_SQL = text("""
    SELECT TOP (5)
        d._IDRRef AS order_ref,
        d._Number AS document_number,
        d._Fld2415 AS document_amount,
        d._Marked AS marked,
        d._Posted AS posted,
        d._Version AS revision,
        d._Fld2413_RRRef AS warehouse_ref
    FROM dbo._Document132 AS d
    WHERE LTRIM(RTRIM(d._Fld2425)) = :site_order_number
    ORDER BY d._Marked ASC, d._Date_Time DESC
    """)

# Закрытие заказов покупателей (_Document135) с табличной частью Заказы
# (_Document135_VT2569) и справочником причин закрытия (_Reference71).
# Ссылка подставляется валидированным hex-литералом: драйвер pytds не умеет
# биндить varbinary-параметр, а CONVERT из nvarchar эта версия SQL Server
# на таком запросе не выполняет.
ONEC_ORDER_CLOSURE_SQL_TEMPLATE = """
    SELECT TOP (5)
        h._Number AS closure_number,
        h._Date_Time AS closure_date,
        r._Description AS closure_reason
    FROM dbo._Document135_VT2569 AS v
    INNER JOIN dbo._Document135 AS h ON h._IDRRef = v._Document135_IDRRef
    LEFT JOIN dbo._Reference71 AS r ON r._IDRRef = v._Fld2572RRef
    WHERE v._Fld2571_RRRef = 0x{order_ref_hex}
      AND h._Marked = 0x00
      AND h._Posted = 0x01
    ORDER BY h._Date_Time DESC
    """
ORDER_REF_HEX_PATTERN = re.compile(r"\A[0-9a-f]{32}\Z")

ONEC_ORDER_LINES_SQL_TEMPLATE = """
    SELECT
        v._LineNo2428 AS line_number,
        v._Fld2434RRef AS product_ref,
        v._Fld2430RRef AS characteristic_ref,
        v._Fld2447RRef AS series_ref,
        v._Fld2437_RRRef AS placement_ref,
        v._Fld2429RRef AS line_unit_ref,
        p._Fld843RRef AS storage_unit_ref,
        v._Fld2431 AS quantity,
        v._Fld2433 AS coefficient,
        storage_unit._Fld550 AS storage_coefficient
    FROM dbo._Document132_VT2427 AS v
    LEFT JOIN dbo._Reference62 AS p ON p._IDRRef = v._Fld2434RRef
    LEFT JOIN dbo._Reference41 AS storage_unit ON storage_unit._IDRRef = p._Fld843RRef
    WHERE v._Document132_IDRRef = 0x{order_ref_hex}
      AND v._Fld2431 > 0
    ORDER BY v._LineNo2428
    """

ONEC_ORDER_RESERVES_SQL_TEMPLATE = """
    SELECT
        r._Fld7654RRef AS warehouse_ref,
        r._Fld7655RRef AS product_ref,
        r._Fld7656RRef AS characteristic_ref,
        r._Fld7658RRef AS series_ref,
        SUM(r._Fld7659) AS reserve_quantity
    FROM dbo._AccumRgT7662 AS r
    WHERE r._Fld7657_RTRef = 0x00000084
      AND r._Fld7657_RRRef = 0x{order_ref_hex}
    GROUP BY
        r._Fld7654RRef,
        r._Fld7655RRef,
        r._Fld7656RRef,
        r._Fld7658RRef
    """

CONFIRMED_READY_AT_SQL = text("""
    SELECT q.assembly_due_at
    FROM order_assembly_queue_item AS q
    INNER JOIN order_assembly_queue_sync_state AS s
        ON s.source = 'bitrix_deal'
    WHERE q.order_number = :site_order_number
      AND q.crm_stage = 'EXECUTING'
      AND q.assembly_due_at IS NOT NULL
      AND q.synced_at >= :fresh_after
      AND s.last_success_at >= :fresh_after
    ORDER BY q.deal_id
    LIMIT 2
    """)


def normalize_money(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _binary_flag(value: Any) -> bool:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return any(bytes(value))
    return bool(value)


def _revision(value: Any) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value or "").strip()


def fetch_onec_order_payment_snapshots(
    engine: Engine,
    *,
    site_order_number: str,
) -> list[OneCOrderPaymentSnapshot]:
    with engine.connect() as connection:
        rows = connection.execute(
            ONEC_ORDER_PAYMENT_SQL,
            {"site_order_number": site_order_number},
        ).mappings()
        return [
            OneCOrderPaymentSnapshot(
                document_number=str(row.get("document_number") or "").strip(),
                amount=(
                    normalize_money(row["document_amount"])
                    if row.get("document_amount") is not None
                    else None
                ),
                marked=_binary_flag(row.get("marked")),
                posted=_binary_flag(row.get("posted")),
                revision=_revision(row.get("revision")),
                order_ref=(bytes(row["order_ref"]) if row.get("order_ref") is not None else None),
                warehouse_ref=(
                    bytes(row["warehouse_ref"]) if row.get("warehouse_ref") is not None else None
                ),
            )
            for row in rows
        ]


def _statement_for_order_ref(template: str, order_ref: bytes) -> Any:
    order_ref_hex = bytes(order_ref).hex()
    if not ORDER_REF_HEX_PATTERN.match(order_ref_hex):
        raise ValueError("unexpected 1C order reference")
    return text(template.format(order_ref_hex=order_ref_hex))


def _ref(value: Any) -> bytes:
    if value is None:
        return ZERO_REF
    result = bytes(value)
    if len(result) != 16:
        raise ValueError("unexpected 1C reference length")
    return result


def fetch_onec_order_lines(
    engine: Engine,
    *,
    order_ref: bytes,
) -> list[OneCOrderLine]:
    statement = _statement_for_order_ref(ONEC_ORDER_LINES_SQL_TEMPLATE, order_ref)
    with engine.connect() as connection:
        rows = connection.execute(statement).mappings()
        return [
            OneCOrderLine(
                line_number=int(row.get("line_number") or 0),
                product_ref=_ref(row.get("product_ref")),
                characteristic_ref=_ref(row.get("characteristic_ref")),
                series_ref=_ref(row.get("series_ref")),
                placement_ref=_ref(row.get("placement_ref")),
                line_unit_ref=_ref(row.get("line_unit_ref")),
                storage_unit_ref=_ref(row.get("storage_unit_ref")),
                quantity=Decimal(str(row.get("quantity") or 0)),
                coefficient=Decimal(str(row.get("coefficient") or 0)),
                storage_coefficient=Decimal(str(row.get("storage_coefficient") or 0)),
            )
            for row in rows
        ]


def fetch_onec_order_reserves(
    engine: Engine,
    *,
    order_ref: bytes,
) -> list[OneCOrderReserve]:
    statement = _statement_for_order_ref(ONEC_ORDER_RESERVES_SQL_TEMPLATE, order_ref)
    with engine.connect() as connection:
        rows = connection.execute(statement).mappings()
        return [
            OneCOrderReserve(
                warehouse_ref=_ref(row.get("warehouse_ref")),
                product_ref=_ref(row.get("product_ref")),
                characteristic_ref=_ref(row.get("characteristic_ref")),
                series_ref=_ref(row.get("series_ref")),
                quantity=Decimal(str(row.get("reserve_quantity") or 0)),
            )
            for row in rows
        ]


def fetch_confirmed_ready_at(
    engine: Engine,
    *,
    site_order_number: str,
    checked_at: datetime,
    max_age: timedelta = timedelta(minutes=10),
) -> datetime | None:
    """Read the exact CRM-owned due time only from one fresh queue row."""
    fresh_after = checked_at - max_age
    with engine.connect() as connection:
        values = list(
            connection.execute(
                CONFIRMED_READY_AT_SQL,
                {
                    "site_order_number": site_order_number,
                    "fresh_after": fresh_after,
                },
            ).scalars()
        )
    if len(values) != 1 or values[0] is None:
        return None
    value = values[0]
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def onec_ref_from_guid(value: str | UUID) -> bytes:
    guid = UUID(str(value))
    first, second, third, fourth, fifth = str(guid).split("-")
    return bytes.fromhex(fourth + fifth + third + second + first)


def onec_guid_from_ref(value: bytes) -> str:
    raw = _ref(value).hex()
    return str(UUID(f"{raw[24:32]}-{raw[20:24]}-{raw[16:20]}-{raw[0:4]}-{raw[4:16]}"))


def fetch_onec_order_closures(
    engine: Engine,
    *,
    order_ref: bytes,
) -> list[OneCOrderClosure]:
    order_ref_hex = bytes(order_ref).hex()
    if not ORDER_REF_HEX_PATTERN.match(order_ref_hex):
        raise ValueError("unexpected 1C order reference")
    statement = text(ONEC_ORDER_CLOSURE_SQL_TEMPLATE.format(order_ref_hex=order_ref_hex))
    with engine.connect() as connection:
        rows = connection.execute(statement).mappings()
        return [
            OneCOrderClosure(
                document_number=str(row.get("closure_number") or "").strip(),
                closed_at=row.get("closure_date"),
                reason=str(row.get("closure_reason") or "").strip(),
            )
            for row in rows
        ]


def _normalize_reason(value: str) -> str:
    return " ".join(value.split()).casefold()


def blocking_closure(
    closures: list[OneCOrderClosure],
    *,
    allowed_reasons: list[str],
) -> OneCOrderClosure | None:
    """Первое закрытие, которое означает отмену интернет-заказа.

    Закрытие по причинам исполнения (полного или частичного) отменой не считается,
    остальные причины, включая незаполненную, блокируют оплату.
    """
    allowed = {_normalize_reason(reason) for reason in allowed_reasons}
    for closure in closures:
        if _normalize_reason(closure.reason) not in allowed:
            return closure
    return None


ReservationKey = tuple[bytes, bytes, bytes, bytes]


def evaluate_reservation(
    lines: list[OneCOrderLine],
    reserves: list[OneCOrderReserve],
    *,
    expected_warehouse_ref: bytes,
) -> tuple[str, bool, str]:
    """Compare normalized order lines with the current 1C reserve totals."""
    if not lines:
        return "MISMATCH", False, "onec_lines_missing"

    expected: dict[ReservationKey, Decimal] = {}
    for line in lines:
        if line.placement_ref != expected_warehouse_ref:
            return "MISMATCH", False, "onec_line_placement_mismatch"
        if (
            line.product_ref == ZERO_REF
            or line.line_unit_ref == ZERO_REF
            or line.storage_unit_ref == ZERO_REF
            or line.quantity <= 0
            or line.coefficient <= 0
            or line.storage_coefficient <= 0
        ):
            return "MISMATCH", False, "onec_reservation_mismatch"
        normalized_quantity = line.quantity * line.coefficient / line.storage_coefficient
        key = (
            expected_warehouse_ref,
            line.product_ref,
            line.characteristic_ref,
            line.series_ref,
        )
        expected[key] = expected.get(key, Decimal(0)) + normalized_quantity

    actual: dict[ReservationKey, Decimal] = {}
    for reserve in reserves:
        key = (
            reserve.warehouse_ref,
            reserve.product_ref,
            reserve.characteristic_ref,
            reserve.series_ref,
        )
        actual[key] = actual.get(key, Decimal(0)) + reserve.quantity

    positive_actual = {
        key: quantity for key, quantity in actual.items() if quantity > RESERVATION_TOLERANCE
    }
    if any(quantity < -RESERVATION_TOLERANCE for quantity in actual.values()):
        return "MISMATCH", False, "onec_reservation_mismatch"
    if not positive_actual:
        return "NONE", False, "onec_reservation_none"
    if set(positive_actual) - set(expected):
        return "MISMATCH", False, "onec_reservation_mismatch"

    has_deficit = False
    for key, expected_quantity in expected.items():
        actual_quantity = actual.get(key, Decimal(0))
        difference = actual_quantity - expected_quantity
        if abs(difference) <= RESERVATION_TOLERANCE:
            continue
        if difference > RESERVATION_TOLERANCE:
            return "MISMATCH", False, "onec_reservation_mismatch"
        has_deficit = True

    if has_deficit:
        return "PARTIAL", False, "onec_reservation_partial"
    return "FULL", True, "amount_and_full_reservation_match"


def check_order_payment(
    engine: Engine,
    *,
    site_order_number: str,
    site_amount: Decimal,
    payment_amount: Decimal,
    source_warehouse_xml_id: str | UUID,
    closure_blocks_payment: bool = True,
    closure_allowed_reasons: list[str] | None = None,
    confirmed_ready_at_resolver: Callable[[str, datetime], datetime | None] | None = None,
) -> OrderPaymentDecision:
    normalized_site_amount = normalize_money(site_amount)
    normalized_payment_amount = normalize_money(payment_amount)
    checked_at = datetime.now(timezone.utc)
    check_id = uuid4().hex
    expected_warehouse_ref = onec_ref_from_guid(source_warehouse_xml_id)

    if normalized_site_amount != normalized_payment_amount:
        return _decision(
            check_id=check_id,
            allowed=False,
            reason="site_payment_mismatch",
            site_order_number=site_order_number,
            site_amount=normalized_site_amount,
            payment_amount=normalized_payment_amount,
            checked_at=checked_at,
        )

    snapshots = fetch_onec_order_payment_snapshots(
        engine,
        site_order_number=site_order_number,
    )
    active = [snapshot for snapshot in snapshots if not snapshot.marked]
    if not snapshots:
        return _decision(
            check_id=check_id,
            allowed=False,
            reason="onec_order_not_found",
            site_order_number=site_order_number,
            site_amount=normalized_site_amount,
            payment_amount=normalized_payment_amount,
            checked_at=checked_at,
        )
    if not active:
        return _decision_from_snapshot(
            snapshots[0],
            check_id=check_id,
            allowed=False,
            reason="onec_order_deleted",
            site_order_number=site_order_number,
            site_amount=normalized_site_amount,
            payment_amount=normalized_payment_amount,
            checked_at=checked_at,
        )
    if len(active) != 1:
        return _decision(
            check_id=check_id,
            allowed=False,
            reason="onec_order_ambiguous",
            site_order_number=site_order_number,
            site_amount=normalized_site_amount,
            payment_amount=normalized_payment_amount,
            checked_at=checked_at,
        )

    snapshot = active[0]
    if not snapshot.posted:
        return _decision_from_snapshot(
            snapshot,
            check_id=check_id,
            allowed=False,
            reason="onec_order_unposted",
            site_order_number=site_order_number,
            site_amount=normalized_site_amount,
            payment_amount=normalized_payment_amount,
            checked_at=checked_at,
        )
    if closure_blocks_payment and snapshot.order_ref is not None:
        closure = blocking_closure(
            fetch_onec_order_closures(engine, order_ref=snapshot.order_ref),
            allowed_reasons=(
                closure_allowed_reasons
                if closure_allowed_reasons is not None
                else DEFAULT_CLOSURE_ALLOWED_REASONS
            ),
        )
        if closure is not None:
            return _decision_from_snapshot(
                snapshot,
                check_id=check_id,
                allowed=False,
                reason="onec_order_closed",
                site_order_number=site_order_number,
                site_amount=normalized_site_amount,
                payment_amount=normalized_payment_amount,
                checked_at=checked_at,
                onec_closure_document=closure.document_number or None,
                onec_closure_reason=closure.reason or None,
            )
    if snapshot.amount is None or snapshot.amount <= 0:
        return _decision_from_snapshot(
            snapshot,
            check_id=check_id,
            allowed=False,
            reason="onec_amount_invalid",
            site_order_number=site_order_number,
            site_amount=normalized_site_amount,
            payment_amount=normalized_payment_amount,
            checked_at=checked_at,
        )
    if snapshot.amount != normalized_site_amount:
        return _decision_from_snapshot(
            snapshot,
            check_id=check_id,
            allowed=False,
            reason="onec_amount_mismatch",
            site_order_number=site_order_number,
            site_amount=normalized_site_amount,
            payment_amount=normalized_payment_amount,
            checked_at=checked_at,
        )

    if not snapshot.warehouse_ref or snapshot.warehouse_ref == ZERO_REF:
        return _decision_from_snapshot(
            snapshot,
            check_id=check_id,
            allowed=False,
            reason="onec_warehouse_missing",
            site_order_number=site_order_number,
            site_amount=normalized_site_amount,
            payment_amount=normalized_payment_amount,
            checked_at=checked_at,
        )
    if snapshot.warehouse_ref != expected_warehouse_ref:
        return _decision_from_snapshot(
            snapshot,
            check_id=check_id,
            allowed=False,
            reason="onec_warehouse_mismatch",
            site_order_number=site_order_number,
            site_amount=normalized_site_amount,
            payment_amount=normalized_payment_amount,
            checked_at=checked_at,
        )
    if snapshot.order_ref is None:
        return _decision_from_snapshot(
            snapshot,
            check_id=check_id,
            allowed=False,
            reason="onec_reservation_mismatch",
            site_order_number=site_order_number,
            site_amount=normalized_site_amount,
            payment_amount=normalized_payment_amount,
            checked_at=checked_at,
        )

    lines = fetch_onec_order_lines(engine, order_ref=snapshot.order_ref)
    reserves = fetch_onec_order_reserves(engine, order_ref=snapshot.order_ref)
    reservation_state, quantity_match, reservation_reason = evaluate_reservation(
        lines,
        reserves,
        expected_warehouse_ref=expected_warehouse_ref,
    )
    if reservation_state != "FULL":
        return _decision_from_snapshot(
            snapshot,
            check_id=check_id,
            allowed=False,
            reason=reservation_reason,
            site_order_number=site_order_number,
            site_amount=normalized_site_amount,
            payment_amount=normalized_payment_amount,
            checked_at=checked_at,
            reservation_state=reservation_state,
            reservation_quantity_match=quantity_match,
        )

    confirmed_ready_at = None
    if confirmed_ready_at_resolver is not None:
        confirmed_ready_at = confirmed_ready_at_resolver(site_order_number, checked_at)
    return _decision_from_snapshot(
        snapshot,
        check_id=check_id,
        allowed=True,
        reason="amount_and_full_reservation_match",
        site_order_number=site_order_number,
        site_amount=normalized_site_amount,
        payment_amount=normalized_payment_amount,
        checked_at=checked_at,
        reservation_state="FULL",
        reservation_quantity_match=True,
        reservation_confirmed_at=checked_at,
        confirmed_ready_at=confirmed_ready_at,
    )


def _decision_from_snapshot(
    snapshot: OneCOrderPaymentSnapshot,
    **kwargs: Any,
) -> OrderPaymentDecision:
    source_warehouse_xml_id = None
    if snapshot.warehouse_ref and snapshot.warehouse_ref != ZERO_REF:
        source_warehouse_xml_id = onec_guid_from_ref(snapshot.warehouse_ref)
    return _decision(
        onec_amount=snapshot.amount,
        onec_document_number=snapshot.document_number or None,
        onec_revision=snapshot.revision or None,
        onec_posted=snapshot.posted,
        source_warehouse_xml_id=source_warehouse_xml_id,
        **kwargs,
    )


def _decision(
    *,
    check_id: str,
    allowed: bool,
    reason: str,
    site_order_number: str,
    site_amount: Decimal,
    payment_amount: Decimal,
    checked_at: datetime,
    onec_amount: Decimal | None = None,
    onec_document_number: str | None = None,
    onec_revision: str | None = None,
    onec_posted: bool | None = None,
    onec_closure_document: str | None = None,
    onec_closure_reason: str | None = None,
    reservation_state: str = "MISMATCH",
    reservation_quantity_match: bool = False,
    source_warehouse_xml_id: str | None = None,
    reservation_confirmed_at: datetime | None = None,
    confirmed_ready_at: datetime | None = None,
) -> OrderPaymentDecision:
    return OrderPaymentDecision(
        check_id=check_id,
        allowed=allowed,
        reason=reason,
        site_order_number=site_order_number,
        site_amount=site_amount,
        payment_amount=payment_amount,
        onec_amount=onec_amount,
        onec_document_number=onec_document_number,
        onec_revision=onec_revision,
        checked_at=checked_at,
        onec_posted=onec_posted,
        onec_closure_document=onec_closure_document,
        onec_closure_reason=onec_closure_reason,
        reservation_state=reservation_state,
        reservation_quantity_match=reservation_quantity_match,
        source_warehouse_xml_id=source_warehouse_xml_id,
        reservation_confirmed_at=reservation_confirmed_at,
        confirmed_ready_at=confirmed_ready_at,
    )
