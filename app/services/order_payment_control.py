from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine

MONEY_QUANTUM = Decimal("0.01")

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


ONEC_ORDER_PAYMENT_SQL = text("""
    SELECT TOP (5)
        d._IDRRef AS order_ref,
        d._Number AS document_number,
        d._Fld2415 AS document_amount,
        d._Marked AS marked,
        d._Posted AS posted,
        d._Version AS revision
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
            )
            for row in rows
        ]


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


def check_order_payment(
    engine: Engine,
    *,
    site_order_number: str,
    site_amount: Decimal,
    payment_amount: Decimal,
    require_posted: bool = False,
    closure_blocks_payment: bool = True,
    closure_allowed_reasons: list[str] | None = None,
) -> OrderPaymentDecision:
    normalized_site_amount = normalize_money(site_amount)
    normalized_payment_amount = normalize_money(payment_amount)
    checked_at = datetime.now(timezone.utc)
    check_id = uuid4().hex

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
    if require_posted and not snapshot.posted:
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
    return _decision_from_snapshot(
        snapshot,
        check_id=check_id,
        allowed=True,
        reason="amount_match",
        site_order_number=site_order_number,
        site_amount=normalized_site_amount,
        payment_amount=normalized_payment_amount,
        checked_at=checked_at,
    )


def _decision_from_snapshot(
    snapshot: OneCOrderPaymentSnapshot,
    **kwargs: Any,
) -> OrderPaymentDecision:
    return _decision(
        onec_amount=snapshot.amount,
        onec_document_number=snapshot.document_number or None,
        onec_revision=snapshot.revision or None,
        onec_posted=snapshot.posted,
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
    )
