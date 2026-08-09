from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine

MONEY_QUANTUM = Decimal("0.01")


@dataclass(frozen=True)
class OneCOrderPaymentSnapshot:
    document_number: str
    amount: Decimal | None
    marked: bool
    posted: bool
    revision: str


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


ONEC_ORDER_PAYMENT_SQL = text("""
    SELECT TOP (5)
        d._Number AS document_number,
        d._Fld2415 AS document_amount,
        d._Marked AS marked,
        d._Posted AS posted,
        d._Version AS revision
    FROM dbo._Document132 AS d
    WHERE LTRIM(RTRIM(d._Fld2425)) = :site_order_number
    ORDER BY d._Marked ASC, d._Date_Time DESC
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
            )
            for row in rows
        ]


def check_order_payment(
    engine: Engine,
    *,
    site_order_number: str,
    site_amount: Decimal,
    payment_amount: Decimal,
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
    )
