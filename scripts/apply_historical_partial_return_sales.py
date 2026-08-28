#!/usr/bin/env python3
"""Safely close strict historical return scenarios.

Dry-run is the default. ``--apply`` persists an execution event and uses the
existing stage outbox, including live Bitrix readback and timeline audit.
``--full-refunds`` requires a complete goods return and an exact linked money
refund before moving an order to ``LOSE``.
Pickup return modes require an explicit bounded order list and distinguish a
payment received before the customer return from a payment created afterwards.
``--stale-execution`` never replays an old 1C decision directly: it requires a
compatible later pickup event and repeats CRM, 1C, inventory and event readback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import or_, select, text

from app.core.config import get_settings
from app.infrastructure.db import session_scope
from app.infrastructure.db.engines import build_engine
from app.models import (
    BitrixChatMessage,
    LogisticsManualReview,
    SiteOrderExecutionCase,
    SiteOrderExecutionEvent,
    SiteOrderStageOutbox,
)
from app.services import pickup_history
from app.services import site_order_execution_reconciliation as execution_reconciliation
from app.services import site_order_fulfillment as fulfillment
from app.services import site_order_stage_outbox as stage_outbox
from infra.cron import order_fulfillment_sync as sync

PARTIAL_RETURN_EVENT_TYPE = "execution_historical_partial_return_sale"
PARTIAL_RETURN_REASON = "partial_return_with_retained_goods"
PARTIAL_RETURN_TARGET_STAGE = "WON"
FULL_REFUND_EVENT_TYPE = "execution_historical_full_goods_money_refund"
FULL_REFUND_REASON = "full_goods_and_money_refund"
FULL_REFUND_TARGET_STAGE = "LOSE"
PICKUP_PAID_RETURN_EVENT_TYPE = "execution_historical_pickup_paid_then_returned"
PICKUP_PAID_RETURN_REASON = "pickup_paid_at_receipt_then_customer_return"
PICKUP_PAID_RETURN_TARGET_STAGE = "WON"
PICKUP_RETURNED_LATE_PAYMENT_EVENT_TYPE = (
    "execution_historical_pickup_returned_without_prior_payment"
)
PICKUP_RETURNED_LATE_PAYMENT_REASON = "pickup_returned_without_prior_payment"
PICKUP_RETURNED_LATE_PAYMENT_TARGET_STAGE = "DISMANTLING"
PICKUP_ISSUED_RETURN_EVENT_TYPE = "execution_historical_pickup_issued_then_returned"
PICKUP_ISSUED_RETURN_REASON = "pickup_partial_issue_confirms_sale"
PICKUP_ISSUED_RETURN_TARGET_STAGE = "WON"
STALE_EXECUTION_WON_EVENT_TYPE = "execution_historical_stale_composite_won"
STALE_EXECUTION_LOSE_EVENT_TYPE = "execution_historical_stale_composite_lose"
STALE_EXECUTION_SOURCE = "reconciliation"
STALE_EXECUTION_CHAT_SOURCES = {fulfillment.SOURCE_BITRIX_CHAT, "manual"}
STALE_EXECUTION_EVENT_TYPES = {
    STALE_EXECUTION_WON_EVENT_TYPE,
    STALE_EXECUTION_LOSE_EVENT_TYPE,
}
BATCH_SIZE = 20
QTY_TOLERANCE = Decimal("0.0001")
MONEY_TOLERANCE = Decimal("0.05")


@dataclass(frozen=True, slots=True)
class Candidate:
    order_number: str
    deal_id: int


@dataclass(frozen=True, slots=True)
class RetainedGoodsEvidence:
    order_number: str
    retained_line_count: int
    retained_quantity: Decimal
    retained_amount: Decimal
    fingerprint: str


@dataclass(frozen=True, slots=True)
class MoneyRefundEvidence:
    order_number: str
    payment_amount: Decimal
    refund_amount: Decimal
    ambiguous_refund_amount: Decimal
    classification: str


@dataclass(frozen=True, slots=True)
class FullRefundEvidence:
    order_number: str
    rtu_count: int
    returned_rtu_count: int
    posted_sale_amount: Decimal
    returned_goods_amount: Decimal
    payment_amount: Decimal
    refund_amount: Decimal
    latest_return_at: datetime
    fingerprint: str


@dataclass(frozen=True, slots=True)
class PaymentMovement:
    paid_at: datetime
    amount: Decimal
    source: str


@dataclass(frozen=True, slots=True)
class PickupReturnEvidence:
    order_number: str
    rtu_count: int
    returned_rtu_count: int
    posted_sale_amount: Decimal
    returned_goods_amount: Decimal
    latest_rtu_at: datetime
    latest_return_at: datetime
    payment_before_return_amount: Decimal
    payment_after_return_amount: Decimal
    qualifying_payment_at: datetime | None
    fingerprint: str


@dataclass(frozen=True, slots=True)
class IssuedRtuMovement:
    rtu_number: str
    sale_amount: Decimal
    issued: bool
    scanned_at: datetime | None
    returned_at: datetime | None


@dataclass(frozen=True, slots=True)
class PickupIssuedReturnEvidence:
    order_number: str
    rtu_count: int
    issued_rtu_count: int
    qualifying_issued_rtu_count: int
    retained_issued_rtu_count: int
    returned_after_issue_rtu_count: int
    qualifying_rtu_numbers: tuple[str, ...]
    latest_qualifying_issue_at: datetime
    fingerprint: str


@dataclass(frozen=True, slots=True)
class StaleExecutionCompositeEvidence:
    order_number: str
    deal_id: int
    onec_event_at: datetime
    onec_decision_reason: str
    onec_target_stage: str
    chat_event_id: int
    chat_event_type: str
    chat_event_at: datetime
    chat_event_source: str
    chat_event_confidence: str
    target_stage: str
    composite_reason: str
    fingerprint: str


def _classify_money_refund(
    *,
    payment_amount: Decimal,
    refund_amount: Decimal,
    ambiguous_refund_amount: Decimal,
) -> str:
    if payment_amount > MONEY_TOLERANCE and refund_amount >= payment_amount - MONEY_TOLERANCE:
        return "full_refund"
    if refund_amount > MONEY_TOLERANCE:
        return "partial_refund"
    if ambiguous_refund_amount > MONEY_TOLERANCE:
        return "ambiguous_refund"
    return "no_refund"


def _header_candidates() -> list[Candidate]:
    statement = text("""
        WITH latest AS (
            SELECT DISTINCT ON (review.source_external_id)
                review.source_external_id AS order_number,
                case_row.bitrix_deal_id,
                (
                    case_row.payload::jsonb
                    #>> '{execution_reconciliation,snapshot,posted_sale_amount}'
                )::numeric AS sale_amount,
                (
                    case_row.payload::jsonb
                    #>> '{execution_reconciliation,snapshot,returned_amount}'
                )::numeric AS return_amount
            FROM logistics_manual_review AS review
            JOIN site_order_execution_case AS case_row
              ON case_row.site_order_number = review.source_external_id
            WHERE review.review_type = 'site_order_execution_conflict'
              AND review.status = 'open'
              AND review.reason = 'paid_and_returned'
            ORDER BY review.source_external_id, review.created_at DESC
        )
        SELECT order_number, bitrix_deal_id
        FROM latest
        WHERE return_amount < sale_amount - 0.05
          AND bitrix_deal_id IS NOT NULL
        ORDER BY order_number
        """)
    with session_scope(read_only=True) as session:
        return [
            Candidate(order_number=str(row.order_number), deal_id=int(row.bitrix_deal_id))
            for row in session.execute(statement)
        ]


def _full_return_candidates() -> list[Candidate]:
    statement = text("""
        WITH latest AS (
            SELECT DISTINCT ON (review.source_external_id)
                review.source_external_id AS order_number,
                case_row.bitrix_deal_id,
                (
                    case_row.payload::jsonb
                    #>> '{execution_reconciliation,snapshot,posted_sale_amount}'
                )::numeric AS sale_amount,
                (
                    case_row.payload::jsonb
                    #>> '{execution_reconciliation,snapshot,returned_amount}'
                )::numeric AS return_amount
            FROM logistics_manual_review AS review
            JOIN site_order_execution_case AS case_row
              ON case_row.site_order_number = review.source_external_id
            WHERE review.review_type = 'site_order_execution_conflict'
              AND review.status = 'open'
              AND review.reason = 'paid_and_returned'
            ORDER BY review.source_external_id, review.created_at DESC
        )
        SELECT order_number, bitrix_deal_id
        FROM latest
        WHERE return_amount >= sale_amount - 0.05
          AND bitrix_deal_id IS NOT NULL
        ORDER BY order_number
        """)
    with session_scope(read_only=True) as session:
        return [
            Candidate(order_number=str(row.order_number), deal_id=int(row.bitrix_deal_id))
            for row in session.execute(statement)
        ]


def _issued_return_candidates() -> list[Candidate]:
    statement = text("""
        SELECT DISTINCT ON (review.source_external_id)
            review.source_external_id AS order_number,
            case_row.bitrix_deal_id
        FROM logistics_manual_review AS review
        JOIN site_order_execution_case AS case_row
          ON case_row.site_order_number = review.source_external_id
        WHERE review.review_type = 'site_order_execution_conflict'
          AND review.status = 'open'
          AND review.reason = 'issued_and_returned'
          AND case_row.bitrix_deal_id IS NOT NULL
        ORDER BY review.source_external_id, review.created_at DESC
        """)
    with session_scope(read_only=True) as session:
        return [
            Candidate(order_number=str(row.order_number), deal_id=int(row.bitrix_deal_id))
            for row in session.execute(statement)
        ]


def _money_refund_evidence(order_numbers: list[str]) -> dict[str, MoneyRefundEvidence]:
    if not order_numbers:
        return {}
    params = {f"order_{index}": value for index, value in enumerate(order_numbers)}
    placeholders = ", ".join(f":order_{index}" for index in range(len(order_numbers)))
    statement = text(f"""
        WITH orders AS (
            SELECT _IDRRef AS order_ref, LTRIM(RTRIM(_Fld2425)) AS order_number
            FROM dbo._Document132 WITH (NOLOCK)
            WHERE LTRIM(RTRIM(_Fld2425)) IN ({placeholders})
        ),
        sales AS (
            SELECT orders.order_number, sale._IDRRef AS sale_ref
            FROM orders
            JOIN dbo._Document203 AS sale WITH (NOLOCK)
              ON sale._Fld4939_TYPE = 0x08
             AND sale._Fld4939_RTRef = 0x00000084
             AND sale._Fld4939_RRRef = orders.order_ref
            WHERE sale._Posted = 0x01 AND sale._Marked <> 0x01
        ),
        return_map AS (
            SELECT sales.order_number, return_doc._IDRRef AS return_ref
            FROM sales
            JOIN dbo._Document109 AS return_doc WITH (NOLOCK)
              ON return_doc._Fld1684_TYPE = 0x08
             AND return_doc._Fld1684_RTRef = 0x000000CB
             AND return_doc._Fld1684_RRRef = sales.sale_ref
             AND return_doc._Posted = 0x01
             AND return_doc._Marked = 0x00
            UNION
            SELECT sales.order_number, return_doc._IDRRef
            FROM sales
            JOIN dbo._Document109_VT1698 AS return_line WITH (NOLOCK)
              ON return_line._Fld1712_TYPE = 0x08
             AND return_line._Fld1712_RTRef = 0x000000CB
             AND return_line._Fld1712_RRRef = sales.sale_ref
            JOIN dbo._Document109 AS return_doc WITH (NOLOCK)
              ON return_doc._IDRRef = return_line._Document109_IDRRef
             AND return_doc._Posted = 0x01
             AND return_doc._Marked = 0x00
        ),
        target_returns AS (
            SELECT DISTINCT return_ref FROM return_map
        ),
        all_return_orders AS (
            SELECT
                target_returns.return_ref,
                LTRIM(RTRIM(source_order._Fld2425)) AS order_number
            FROM target_returns
            JOIN dbo._Document109 AS return_doc WITH (NOLOCK)
              ON return_doc._IDRRef = target_returns.return_ref
            JOIN dbo._Document203 AS source_sale WITH (NOLOCK)
              ON return_doc._Fld1684_TYPE = 0x08
             AND return_doc._Fld1684_RTRef = 0x000000CB
             AND source_sale._IDRRef = return_doc._Fld1684_RRRef
            JOIN dbo._Document132 AS source_order WITH (NOLOCK)
              ON source_sale._Fld4939_TYPE = 0x08
             AND source_sale._Fld4939_RTRef = 0x00000084
             AND source_order._IDRRef = source_sale._Fld4939_RRRef
            UNION
            SELECT
                target_returns.return_ref,
                LTRIM(RTRIM(source_order._Fld2425))
            FROM target_returns
            JOIN dbo._Document109_VT1698 AS return_line WITH (NOLOCK)
              ON return_line._Document109_IDRRef = target_returns.return_ref
             AND return_line._Fld1712_TYPE = 0x08
             AND return_line._Fld1712_RTRef = 0x000000CB
            JOIN dbo._Document203 AS source_sale WITH (NOLOCK)
              ON source_sale._IDRRef = return_line._Fld1712_RRRef
            JOIN dbo._Document132 AS source_order WITH (NOLOCK)
              ON source_sale._Fld4939_TYPE = 0x08
             AND source_sale._Fld4939_RTRef = 0x00000084
             AND source_order._IDRRef = source_sale._Fld4939_RRRef
        ),
        return_counts AS (
            SELECT return_ref, COUNT(DISTINCT order_number) AS order_count
            FROM all_return_orders
            GROUP BY return_ref
        ),
        movements AS (
            SELECT orders.order_number, N'payment' AS kind, card._IDRRef AS document_ref,
                   CAST(card._Fld3414 AS decimal(18, 2)) AS amount, 1 AS exact_link
            FROM orders
            JOIN dbo._Document169 AS card WITH (NOLOCK)
              ON card._Fld3417_TYPE = 0x08
             AND card._Fld3417_RTRef = 0x00000084
             AND card._Fld3417_RRRef = orders.order_ref
            JOIN dbo._Enum278 AS operation WITH (NOLOCK)
              ON operation._IDRRef = card._Fld3412RRef AND operation._EnumOrder = 0
            WHERE card._Posted = 0x01 AND card._Marked = 0x00
            UNION ALL
            SELECT sales.order_number, N'payment', card._IDRRef,
                   CAST(card._Fld3414 AS decimal(18, 2)), 1
            FROM sales
            JOIN dbo._Document169 AS card WITH (NOLOCK)
              ON card._Fld3417_TYPE = 0x08
             AND card._Fld3417_RTRef = 0x000000CB
             AND card._Fld3417_RRRef = sales.sale_ref
            JOIN dbo._Enum278 AS operation WITH (NOLOCK)
              ON operation._IDRRef = card._Fld3412RRef AND operation._EnumOrder = 0
            WHERE card._Posted = 0x01 AND card._Marked = 0x00
            UNION ALL
            SELECT orders.order_number, N'payment', cash_in._IDRRef,
                   CAST(cash_in._Fld4688 AS decimal(18, 2)), 1
            FROM orders
            JOIN dbo._Document196 AS cash_in WITH (NOLOCK)
              ON cash_in._Fld4697_TYPE = 0x08
             AND cash_in._Fld4697_RTRef = 0x00000084
             AND cash_in._Fld4697_RRRef = orders.order_ref
            WHERE cash_in._Posted = 0x01 AND cash_in._Marked = 0x00
            UNION ALL
            SELECT sales.order_number, N'payment', cash_in._IDRRef,
                   CAST(cash_in._Fld4688 AS decimal(18, 2)), 1
            FROM sales
            JOIN dbo._Document196 AS cash_in WITH (NOLOCK)
              ON cash_in._Fld4697_TYPE = 0x08
             AND cash_in._Fld4697_RTRef = 0x000000CB
             AND cash_in._Fld4697_RRRef = sales.sale_ref
            WHERE cash_in._Posted = 0x01 AND cash_in._Marked = 0x00
            UNION ALL
            SELECT return_map.order_number, N'refund', card._IDRRef,
                   CAST(card._Fld3414 AS decimal(18, 2)),
                   CASE WHEN return_counts.order_count = 1 THEN 1 ELSE 0 END
            FROM return_map
            JOIN return_counts ON return_counts.return_ref = return_map.return_ref
            JOIN dbo._Document169 AS card WITH (NOLOCK)
              ON card._Fld3417_TYPE = 0x08
             AND card._Fld3417_RTRef = 0x0000006D
             AND card._Fld3417_RRRef = return_map.return_ref
            JOIN dbo._Enum278 AS operation WITH (NOLOCK)
              ON operation._IDRRef = card._Fld3412RRef AND operation._EnumOrder = 1
            WHERE card._Posted = 0x01 AND card._Marked = 0x00
            UNION ALL
            SELECT return_map.order_number, N'refund', cash_out._IDRRef,
                   CAST(cash_out._Fld4852 AS decimal(18, 2)),
                   CASE WHEN return_counts.order_count = 1 THEN 1 ELSE 0 END
            FROM return_map
            JOIN return_counts ON return_counts.return_ref = return_map.return_ref
            JOIN dbo._Document201 AS cash_out WITH (NOLOCK)
              ON cash_out._Fld4862_TYPE = 0x08
             AND cash_out._Fld4862_RTRef = 0x0000006D
             AND cash_out._Fld4862_RRRef = return_map.return_ref
            WHERE cash_out._Posted = 0x01 AND cash_out._Marked = 0x00
        )
        SELECT order_number, kind,
               master.dbo.fn_varbintohexstr(document_ref) AS document_ref,
               amount, exact_link
        FROM movements
        """)
    engine = build_engine(get_settings().onec_database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(statement, params).mappings()]
    finally:
        engine.dispose()
    unique = {
        (str(row["order_number"]), str(row["kind"]), str(row["document_ref"])): row for row in rows
    }
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in unique.values():
        grouped[str(row["order_number"])].append(row)
    result: dict[str, MoneyRefundEvidence] = {}
    for order_number in order_numbers:
        payment = sum(
            (
                Decimal(str(row["amount"] or 0))
                for row in grouped[order_number]
                if row["kind"] == "payment"
            ),
            Decimal("0"),
        )
        refund = sum(
            (
                Decimal(str(row["amount"] or 0))
                for row in grouped[order_number]
                if row["kind"] == "refund" and int(row["exact_link"] or 0) == 1
            ),
            Decimal("0"),
        )
        ambiguous = sum(
            (
                Decimal(str(row["amount"] or 0))
                for row in grouped[order_number]
                if row["kind"] == "refund" and int(row["exact_link"] or 0) != 1
            ),
            Decimal("0"),
        )
        classification = _classify_money_refund(
            payment_amount=payment,
            refund_amount=refund,
            ambiguous_refund_amount=ambiguous,
        )
        result[order_number] = MoneyRefundEvidence(
            order_number=order_number,
            payment_amount=payment,
            refund_amount=refund,
            ambiguous_refund_amount=ambiguous,
            classification=classification,
        )
    return result


def _pickup_payment_movements(order_numbers: list[str]) -> dict[str, list[PaymentMovement]]:
    if not order_numbers:
        return {}
    params = {f"order_{index}": value for index, value in enumerate(order_numbers)}
    placeholders = ", ".join(f":order_{index}" for index in range(len(order_numbers)))
    statement = text(f"""
        WITH orders AS (
            SELECT _IDRRef AS order_ref, LTRIM(RTRIM(_Fld2425)) AS order_number
            FROM dbo._Document132 WITH (NOLOCK)
            WHERE LTRIM(RTRIM(_Fld2425)) IN ({placeholders})
        ),
        sales AS (
            SELECT orders.order_number, sale._IDRRef AS sale_ref
            FROM orders
            JOIN dbo._Document203 AS sale WITH (NOLOCK)
              ON sale._Fld4939_TYPE = 0x08
             AND sale._Fld4939_RTRef = 0x00000084
             AND sale._Fld4939_RRRef = orders.order_ref
            WHERE sale._Posted = 0x01 AND sale._Marked <> 0x01
        ),
        movements AS (
            SELECT orders.order_number, card._IDRRef AS document_ref,
                   card._Date_Time AS paid_at,
                   CAST(card._Fld3414 AS decimal(18, 2)) AS amount,
                   N'acquiring_order' AS source
            FROM orders
            JOIN dbo._Document169 AS card WITH (NOLOCK)
              ON card._Fld3417_TYPE = 0x08
             AND card._Fld3417_RTRef = 0x00000084
             AND card._Fld3417_RRRef = orders.order_ref
            JOIN dbo._Enum278 AS operation WITH (NOLOCK)
              ON operation._IDRRef = card._Fld3412RRef AND operation._EnumOrder = 0
            WHERE card._Posted = 0x01 AND card._Marked = 0x00
            UNION ALL
            SELECT sales.order_number, card._IDRRef, card._Date_Time,
                   CAST(card._Fld3414 AS decimal(18, 2)), N'acquiring_sale'
            FROM sales
            JOIN dbo._Document169 AS card WITH (NOLOCK)
              ON card._Fld3417_TYPE = 0x08
             AND card._Fld3417_RTRef = 0x000000CB
             AND card._Fld3417_RRRef = sales.sale_ref
            JOIN dbo._Enum278 AS operation WITH (NOLOCK)
              ON operation._IDRRef = card._Fld3412RRef AND operation._EnumOrder = 0
            WHERE card._Posted = 0x01 AND card._Marked = 0x00
            UNION ALL
            SELECT orders.order_number, cash_in._IDRRef, cash_in._Date_Time,
                   CAST(cash_in._Fld4688 AS decimal(18, 2)), N'cash_order'
            FROM orders
            JOIN dbo._Document196 AS cash_in WITH (NOLOCK)
              ON cash_in._Fld4697_TYPE = 0x08
             AND cash_in._Fld4697_RTRef = 0x00000084
             AND cash_in._Fld4697_RRRef = orders.order_ref
            WHERE cash_in._Posted = 0x01 AND cash_in._Marked = 0x00
            UNION ALL
            SELECT sales.order_number, cash_in._IDRRef, cash_in._Date_Time,
                   CAST(cash_in._Fld4688 AS decimal(18, 2)), N'cash_sale'
            FROM sales
            JOIN dbo._Document196 AS cash_in WITH (NOLOCK)
              ON cash_in._Fld4697_TYPE = 0x08
             AND cash_in._Fld4697_RTRef = 0x000000CB
             AND cash_in._Fld4697_RRRef = sales.sale_ref
            WHERE cash_in._Posted = 0x01 AND cash_in._Marked = 0x00
        )
        SELECT order_number,
               master.dbo.fn_varbintohexstr(document_ref) AS document_ref,
               paid_at, amount, source
        FROM movements
        """)
    engine = build_engine(get_settings().onec_database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(statement, params).mappings()]
    finally:
        engine.dispose()
    unique = {(str(row["order_number"]), str(row["document_ref"])): row for row in rows}
    grouped: dict[str, list[PaymentMovement]] = defaultdict(list)
    for row in unique.values():
        paid_at = row.get("paid_at")
        if not isinstance(paid_at, datetime):
            continue
        grouped[str(row["order_number"])].append(
            PaymentMovement(
                paid_at=paid_at,
                amount=Decimal(str(row.get("amount") or 0)),
                source=str(row.get("source") or "unknown"),
            )
        )
    return {order: sorted(items, key=lambda item: item.paid_at) for order, items in grouped.items()}


def _pickup_payment_sequence(
    movements: list[PaymentMovement],
    *,
    latest_rtu_at: datetime,
    latest_return_at: datetime,
    posted_sale_amount: Decimal,
) -> tuple[Decimal, Decimal, datetime | None]:
    before_return = [item for item in movements if latest_rtu_at <= item.paid_at < latest_return_at]
    after_return = [item for item in movements if item.paid_at >= latest_return_at]
    before_amount = sum((item.amount for item in before_return), Decimal("0"))
    after_amount = sum((item.amount for item in after_return), Decimal("0"))
    qualifying_at = (
        max(item.paid_at for item in before_return)
        if before_amount >= posted_sale_amount - MONEY_TOLERANCE
        else None
    )
    return before_amount, after_amount, qualifying_at


def _pickup_issued_rtu_movements(
    order_numbers: list[str],
) -> dict[str, list[IssuedRtuMovement]]:
    if not order_numbers:
        return {}
    params = {f"order_{index}": value for index, value in enumerate(order_numbers)}
    placeholders = ", ".join(f":order_{index}" for index in range(len(order_numbers)))
    statement = text(f"""
        SELECT
            LTRIM(RTRIM(ord._Fld2425)) AS order_number,
            LTRIM(RTRIM(rtu._Number)) AS rtu_number,
            CAST(rtu._Fld4948 AS decimal(18, 2)) AS sale_amount,
            CASE WHEN EXISTS (
                SELECT 1
                FROM dbo._InfoRg9448 AS event WITH (NOLOCK)
                WHERE event._Fld9449_RRRef = rtu._IDRRef
                  AND event._Fld9449_TYPE = 0x08
                  AND event._Fld9449_RTRef = 0x000000CB
                  AND event._Fld9454 = N'Распечатан'
            ) AND EXISTS (
                SELECT 1
                FROM dbo._InfoRg9448 AS event WITH (NOLOCK)
                WHERE event._Fld9449_RRRef = rtu._IDRRef
                  AND event._Fld9449_TYPE = 0x08
                  AND event._Fld9449_RTRef = 0x000000CB
                  AND event._Fld9454 = N'Отсканирован'
            ) THEN 1 ELSE 0 END AS issued,
            (
                SELECT MAX(event._Fld9450)
                FROM dbo._InfoRg9448 AS event WITH (NOLOCK)
                WHERE event._Fld9449_RRRef = rtu._IDRRef
                  AND event._Fld9449_TYPE = 0x08
                  AND event._Fld9449_RTRef = 0x000000CB
                  AND event._Fld9454 = N'Отсканирован'
            ) AS scanned_at,
            (
                SELECT MAX(return_doc._Date_Time)
                FROM dbo._Document109 AS return_doc WITH (NOLOCK)
                WHERE return_doc._Posted = 0x01
                  AND return_doc._Marked = 0x00
                  AND (
                      (
                          return_doc._Fld1684_TYPE = 0x08
                          AND return_doc._Fld1684_RTRef = 0x000000CB
                          AND return_doc._Fld1684_RRRef = rtu._IDRRef
                      )
                      OR EXISTS (
                          SELECT 1
                          FROM dbo._Document109_VT1698 AS return_line WITH (NOLOCK)
                          WHERE return_line._Document109_IDRRef = return_doc._IDRRef
                            AND return_line._Fld1712_TYPE = 0x08
                            AND return_line._Fld1712_RTRef = 0x000000CB
                            AND return_line._Fld1712_RRRef = rtu._IDRRef
                      )
                  )
            ) AS returned_at
        FROM dbo._Document203 AS rtu WITH (NOLOCK)
        JOIN dbo._Document132 AS ord WITH (NOLOCK)
          ON ord._IDRRef = rtu._Fld4939_RRRef
        WHERE rtu._Posted = 0x01
          AND rtu._Marked <> 0x01
          AND LTRIM(RTRIM(ord._Fld2425)) IN ({placeholders})
        ORDER BY order_number, rtu._Date_Time, rtu_number
        """)
    engine = build_engine(get_settings().onec_database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(statement, params).mappings()]
    finally:
        engine.dispose()
    grouped: dict[str, list[IssuedRtuMovement]] = defaultdict(list)
    for row in rows:
        grouped[str(row["order_number"])].append(
            IssuedRtuMovement(
                rtu_number=str(row.get("rtu_number") or ""),
                sale_amount=Decimal(str(row.get("sale_amount") or 0)),
                issued=bool(row.get("issued")),
                scanned_at=(
                    row.get("scanned_at") if isinstance(row.get("scanned_at"), datetime) else None
                ),
                returned_at=(
                    row.get("returned_at") if isinstance(row.get("returned_at"), datetime) else None
                ),
            )
        )
    return dict(grouped)


def _qualifying_issued_rtu_rows(
    rows: list[IssuedRtuMovement],
) -> list[IssuedRtuMovement]:
    return [
        row
        for row in rows
        if row.issued
        and row.scanned_at is not None
        and (row.returned_at is None or row.returned_at > row.scanned_at)
    ]


def _line_evidence(order_numbers: list[str]) -> dict[str, RetainedGoodsEvidence]:
    if not order_numbers:
        return {}
    params = {f"order_{index}": value for index, value in enumerate(order_numbers)}
    placeholders = ", ".join(f":order_{index}" for index in range(len(order_numbers)))
    statement = text(f"""
        WITH orders AS (
            SELECT
                _IDRRef AS order_ref,
                LTRIM(RTRIM(_Fld2425)) AS order_number
            FROM dbo._Document132 WITH (NOLOCK)
            WHERE LTRIM(RTRIM(_Fld2425)) IN ({placeholders})
        ),
        sales AS (
            SELECT orders.order_number, sale._IDRRef AS sale_ref
            FROM orders
            JOIN dbo._Document203 AS sale WITH (NOLOCK)
              ON sale._Fld4939_TYPE = 0x08
             AND sale._Fld4939_RTRef = 0x00000084
             AND sale._Fld4939_RRRef = orders.order_ref
            WHERE sale._Posted = 0x01
              AND sale._Marked <> 0x01
        ),
        sale_lines AS (
            SELECT
                sales.order_number,
                line._Fld4974RRef AS product_ref,
                SUM(CAST(line._Fld4971 AS decimal(18, 4))) AS sale_quantity,
                SUM(
                    CAST(line._Fld4971 AS decimal(18, 4))
                    * CAST(line._Fld4982 AS decimal(18, 2))
                ) AS sale_amount
            FROM sales
            JOIN dbo._Document203_VT4966 AS line WITH (NOLOCK)
              ON line._Document203_IDRRef = sales.sale_ref
            GROUP BY sales.order_number, line._Fld4974RRef
        ),
        return_source AS (
            SELECT
                sales.order_number,
                return_doc._IDRRef AS return_ref,
                return_line._LineNo1699 AS line_no,
                return_line._Fld1700RRef AS product_ref,
                return_line._Fld1701 AS return_quantity,
                return_line._Fld1707 AS return_amount
            FROM sales
            JOIN dbo._Document109 AS return_doc WITH (NOLOCK)
              ON return_doc._Fld1684_TYPE = 0x08
             AND return_doc._Fld1684_RTRef = 0x000000CB
             AND return_doc._Fld1684_RRRef = sales.sale_ref
             AND return_doc._Posted = 0x01
             AND return_doc._Marked = 0x00
            JOIN dbo._Document109_VT1698 AS return_line WITH (NOLOCK)
              ON return_line._Document109_IDRRef = return_doc._IDRRef
            UNION
            SELECT
                sales.order_number,
                return_doc._IDRRef,
                return_line._LineNo1699,
                return_line._Fld1700RRef,
                return_line._Fld1701,
                return_line._Fld1707
            FROM sales
            JOIN dbo._Document109_VT1698 AS return_line WITH (NOLOCK)
              ON return_line._Fld1712_TYPE = 0x08
             AND return_line._Fld1712_RTRef = 0x000000CB
             AND return_line._Fld1712_RRRef = sales.sale_ref
            JOIN dbo._Document109 AS return_doc WITH (NOLOCK)
              ON return_doc._IDRRef = return_line._Document109_IDRRef
             AND return_doc._Posted = 0x01
             AND return_doc._Marked = 0x00
        ),
        return_lines AS (
            SELECT
                order_number,
                product_ref,
                SUM(ABS(CAST(return_quantity AS decimal(18, 4)))) AS return_quantity,
                SUM(ABS(CAST(return_amount AS decimal(18, 2)))) AS return_amount
            FROM return_source
            GROUP BY order_number, product_ref
        )
        SELECT
            sale_lines.order_number,
            master.dbo.fn_varbintohexstr(sale_lines.product_ref) AS product_ref,
            sale_lines.sale_quantity,
            COALESCE(return_lines.return_quantity, 0) AS return_quantity,
            sale_lines.sale_amount,
            COALESCE(return_lines.return_amount, 0) AS return_amount
        FROM sale_lines
        LEFT JOIN return_lines
          ON return_lines.order_number = sale_lines.order_number
         AND return_lines.product_ref = sale_lines.product_ref
        ORDER BY sale_lines.order_number, product_ref
        """)
    engine = build_engine(get_settings().onec_database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(statement, params).mappings()]
    finally:
        engine.dispose()

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["order_number"])].append(row)

    result: dict[str, RetainedGoodsEvidence] = {}
    for order_number in order_numbers:
        retained: list[dict[str, str]] = []
        total_quantity = Decimal("0")
        total_amount = Decimal("0")
        conflict = False
        for row in grouped.get(order_number, []):
            sale_quantity = Decimal(str(row.get("sale_quantity") or 0))
            return_quantity = Decimal(str(row.get("return_quantity") or 0))
            sale_amount = Decimal(str(row.get("sale_amount") or 0))
            return_amount = Decimal(str(row.get("return_amount") or 0))
            if return_quantity > sale_quantity + QTY_TOLERANCE:
                conflict = True
                break
            net_quantity = sale_quantity - return_quantity
            net_amount = sale_amount - return_amount
            if net_quantity <= QTY_TOLERANCE or net_amount <= MONEY_TOLERANCE:
                continue
            total_quantity += net_quantity
            total_amount += net_amount
            retained.append(
                {
                    "product_ref": str(row.get("product_ref") or ""),
                    "quantity": str(net_quantity),
                    "amount": str(net_amount),
                }
            )
        if conflict or not retained:
            continue
        canonical = json.dumps(retained, ensure_ascii=False, sort_keys=True)
        result[order_number] = RetainedGoodsEvidence(
            order_number=order_number,
            retained_line_count=len(retained),
            retained_quantity=total_quantity,
            retained_amount=total_amount,
            fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
    return result


def _live_deals(
    client: fulfillment.BitrixChatClient, order_numbers: list[str]
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for start in range(0, len(order_numbers), 40):
        batch = order_numbers[start : start + 40]
        offset: int | None = 0
        while offset is not None:
            response = client.call(
                "crm.deal.list",
                {
                    "filter": {f"@{fulfillment.CRM_ORDER_NUMBER_FIELD}": batch},
                    "select": [
                        "ID",
                        "STAGE_ID",
                        fulfillment.CRM_ORDER_NUMBER_FIELD,
                        fulfillment.CRM_DELIVERY_FIELD,
                    ],
                    "order": {"ID": "ASC"},
                    "start": offset,
                },
            )
            for raw in response.get("result") or []:
                order_number = str(raw.get(fulfillment.CRM_ORDER_NUMBER_FIELD) or "").strip()
                if order_number:
                    result[order_number].append(raw)
            next_value = response.get("next")
            offset = int(next_value) if next_value is not None else None
    return result


def _ready_batch(
    candidates: list[Candidate],
    evidence: dict[str, RetainedGoodsEvidence],
    *,
    client: fulfillment.BitrixChatClient,
) -> tuple[list[tuple[Candidate, RetainedGoodsEvidence]], Counter[str]]:
    live = _live_deals(client, [item.order_number for item in candidates])
    reasons: Counter[str] = Counter()
    ready: list[tuple[Candidate, RetainedGoodsEvidence]] = []
    with session_scope(read_only=True) as session:
        for candidate in candidates:
            item_evidence = evidence.get(candidate.order_number)
            if item_evidence is None:
                reasons["no_positive_retained_goods"] += 1
                continue
            deals = live.get(candidate.order_number, [])
            if len(deals) != 1:
                reasons["deal_not_unique"] += 1
                continue
            deal = deals[0]
            if int(deal["ID"]) != candidate.deal_id:
                reasons["deal_changed"] += 1
                continue
            stage = str(deal.get("STAGE_ID") or "").strip()
            if stage == PARTIAL_RETURN_TARGET_STAGE:
                reasons["already_won"] += 1
                continue
            if stage != "EXECUTING":
                reasons[f"unexpected_stage:{stage or '-'}"] += 1
                continue
            warehouses = pickup_history._current_inventory_warehouse_ids(  # noqa: SLF001
                session,
                site_order_number=candidate.order_number,
            )
            if warehouses:
                reasons["current_inventory"] += 1
                continue
            ready.append((candidate, item_evidence))
    return ready, reasons


def _enqueue(ready: list[tuple[Candidate, RetainedGoodsEvidence]]) -> list[int]:
    outbox_ids: list[int] = []
    now = datetime.now()
    with session_scope() as session:
        for candidate, evidence in ready:
            case_row = session.scalar(
                select(SiteOrderExecutionCase).where(
                    SiteOrderExecutionCase.site_order_number == candidate.order_number
                )
            )
            if case_row is None:
                continue
            source_ref = f"historical-partial-sale:{evidence.fingerprint}"
            event = fulfillment.upsert_execution_event(
                session,
                site_order_number=candidate.order_number,
                event_type=PARTIAL_RETURN_EVENT_TYPE,
                event_at=now,
                source="onec",
                source_ref=source_ref,
                confidence="strong",
                raw_message_id=None,
                payload={
                    "pipeline": "execution_reconciliation",
                    "historical": True,
                    "decision": {
                        "action": "update_stage",
                        "reason": PARTIAL_RETURN_REASON,
                        "target_stage": PARTIAL_RETURN_TARGET_STAGE,
                    },
                    "evidence_fingerprint": evidence.fingerprint,
                    "retained_goods": {
                        "line_count": evidence.retained_line_count,
                        "quantity": str(evidence.retained_quantity),
                        "amount": str(evidence.retained_amount),
                    },
                },
            )
            if event is None:
                existing = session.scalar(
                    select(SiteOrderStageOutbox).where(
                        SiteOrderStageOutbox.idempotency_key
                        == (
                            f"execution-stage|{candidate.order_number}|"
                            f"{evidence.fingerprint}|{PARTIAL_RETURN_TARGET_STAGE}"
                        )
                    )
                )
                if existing is not None and existing.status in {
                    stage_outbox.STATUS_PENDING,
                    stage_outbox.STATUS_RETRY,
                }:
                    outbox_ids.append(existing.id)
                continue
            case_row.bitrix_deal_id = candidate.deal_id
            case_row.current_derived_status = PARTIAL_RETURN_EVENT_TYPE
            case_row.current_crm_stage = "EXECUTING"
            case_row.confidence = "strong"
            case_row.last_evidence_event_id = event.id
            case_row.updated_at = now
            outbox = SiteOrderStageOutbox(
                case_id=case_row.id,
                event_id=event.id,
                idempotency_key=(
                    f"execution-stage|{candidate.order_number}|{evidence.fingerprint}|"
                    f"{PARTIAL_RETURN_TARGET_STAGE}"
                ),
                site_order_number=candidate.order_number,
                bitrix_deal_id=candidate.deal_id,
                source_event_type=PARTIAL_RETURN_EVENT_TYPE,
                target_stage=PARTIAL_RETURN_TARGET_STAGE,
                payload={
                    "pipeline": "execution_reconciliation",
                    "historical": True,
                    "decision": {
                        "action": "update_stage",
                        "reason": PARTIAL_RETURN_REASON,
                        "target_stage": PARTIAL_RETURN_TARGET_STAGE,
                    },
                    "evidence_fingerprint": evidence.fingerprint,
                    "retained_goods": {
                        "line_count": evidence.retained_line_count,
                        "quantity": str(evidence.retained_quantity),
                        "amount": str(evidence.retained_amount),
                    },
                },
            )
            session.add(outbox)
            session.flush()
            outbox_ids.append(outbox.id)
        session.commit()
    return outbox_ids


def _apply_outbox(
    outbox_ids: list[int],
    *,
    client: fulfillment.BitrixChatClient,
    target_stage: str,
) -> list[stage_outbox.StageOutboxResult]:
    if not outbox_ids:
        return []
    with session_scope(read_only=True) as session:
        rows = [
            {
                "id": row.id,
                "site_order_number": row.site_order_number,
                "bitrix_deal_id": row.bitrix_deal_id,
                "timeline_comment": stage_outbox._timeline_comment(row),  # noqa: SLF001
            }
            for row in session.scalars(
                select(SiteOrderStageOutbox)
                .where(SiteOrderStageOutbox.id.in_(outbox_ids))
                .order_by(SiteOrderStageOutbox.id.asc())
            ).all()
        ]
    live_before = _live_deals(client, [str(row["site_order_number"]) for row in rows])
    commands: dict[str, str] = {}
    for index, row in enumerate(rows):
        order_number = str(row["site_order_number"])
        deal_id = int(row["bitrix_deal_id"] or 0)
        deals = live_before.get(order_number, [])
        if len(deals) != 1 or int(deals[0]["ID"]) != deal_id:
            continue
        if str(deals[0].get("STAGE_ID") or "").strip() != "EXECUTING":
            continue
        commands[f"update_{index}"] = "crm.deal.update?" + urlencode(
            {"id": deal_id, "fields[STAGE_ID]": target_stage}
        )
        commands[f"timeline_{index}"] = "crm.timeline.comment.add?" + urlencode(
            {
                "fields[ENTITY_TYPE]": "deal",
                "fields[ENTITY_ID]": deal_id,
                "fields[COMMENT]": str(row["timeline_comment"]),
            }
        )
    if commands:
        client.call("batch", {"halt": 0, "cmd": commands})

    live_after = _live_deals(client, [str(row["site_order_number"]) for row in rows])
    now = datetime.now()
    results: list[stage_outbox.StageOutboxResult] = []
    with session_scope() as session:
        persisted = {
            row.id: row
            for row in session.scalars(
                select(SiteOrderStageOutbox).where(SiteOrderStageOutbox.id.in_(outbox_ids))
            ).all()
        }
        for source_row in rows:
            row = persisted[int(source_row["id"])]
            deals = live_after.get(row.site_order_number, [])
            live_stage = str(deals[0].get("STAGE_ID") or "").strip() if len(deals) == 1 else None
            applied = (
                len(deals) == 1
                and int(deals[0]["ID"]) == int(row.bitrix_deal_id or 0)
                and live_stage == target_stage
            )
            if applied:
                row.status = stage_outbox.STATUS_APPLIED
                row.applied_at = row.applied_at or now
                row.timeline_written_at = row.timeline_written_at or now
                row.next_attempt_at = None
                row.last_error = None
                result_name = "applied"
            else:
                row.status = stage_outbox.STATUS_RETRY
                row.next_attempt_at = now
                row.last_error = f"batch_readback_mismatch:{live_stage or '-'}"
                result_name = "retry"
            row.last_live_stage = live_stage
            row.updated_at = now
            results.append(
                stage_outbox.StageOutboxResult(
                    outbox_id=row.id,
                    site_order_number=row.site_order_number,
                    target_stage=row.target_stage,
                    result=result_name,
                    bitrix_deal_id=row.bitrix_deal_id,
                    live_stage=live_stage,
                    reason=None if applied else row.last_error,
                    applied=applied,
                )
            )
        session.commit()
    return results


def _recover_pending(
    *,
    client: fulfillment.BitrixChatClient,
    event_type: str,
    target_stage: str,
    resolved_reason: str,
) -> int:
    with session_scope(read_only=True) as session:
        outbox_ids = list(
            session.scalars(
                select(SiteOrderStageOutbox.id)
                .where(
                    SiteOrderStageOutbox.source_event_type == event_type,
                    SiteOrderStageOutbox.status.in_(
                        [stage_outbox.STATUS_PENDING, stage_outbox.STATUS_RETRY]
                    ),
                )
                .order_by(SiteOrderStageOutbox.id.asc())
                .limit(BATCH_SIZE)
            ).all()
        )
    results = _apply_outbox(outbox_ids, client=client, target_stage=target_stage)
    _finalize_applied(
        results,
        target_stage=target_stage,
        resolved_reason=resolved_reason,
    )
    print(
        json.dumps(
            {
                "mode": "recover",
                "pending": len(outbox_ids),
                "applied": sum(1 for item in results if item.applied),
                "result_counts": dict(Counter(item.result for item in results)),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _finalize_applied(
    results: list[stage_outbox.StageOutboxResult],
    *,
    target_stage: str,
    resolved_reason: str,
) -> None:
    applied_orders = {item.site_order_number for item in results if item.applied}
    if not applied_orders:
        return
    now = datetime.now()
    with session_scope() as session:
        cases = session.scalars(
            select(SiteOrderExecutionCase).where(
                SiteOrderExecutionCase.site_order_number.in_(applied_orders)
            )
        ).all()
        for case_row in cases:
            case_row.current_crm_stage = target_stage
            case_row.updated_at = now
        reviews = session.scalars(
            select(LogisticsManualReview).where(
                LogisticsManualReview.review_type == "site_order_execution_conflict",
                LogisticsManualReview.source_external_id.in_(applied_orders),
                LogisticsManualReview.status == "open",
                LogisticsManualReview.reason == "paid_and_returned",
            )
        ).all()
        for review in reviews:
            review.status = "resolved"
            review.resolved_at = now
            review.updated_at = now
            review.payload = {
                **(review.payload if isinstance(review.payload, dict) else {}),
                "resolved_reason": resolved_reason,
            }
        session.commit()


def _has_confirmed_delivery_evidence(session: Any, *, case_id: int) -> bool:
    event_id = session.scalar(
        select(SiteOrderExecutionEvent.id)
        .where(
            SiteOrderExecutionEvent.case_id == case_id,
            or_(
                SiteOrderExecutionEvent.event_type.in_(
                    {
                        fulfillment.EVENT_PICKUP_RECEIVED,
                        fulfillment.EVENT_COURIER_DELIVERED_PAID,
                    }
                ),
                SiteOrderExecutionEvent.event_type.like("%pickup_issued"),
            ),
        )
        .limit(1)
    )
    if event_id is not None:
        return True
    won_outbox_id = session.scalar(
        select(SiteOrderStageOutbox.id)
        .where(
            SiteOrderStageOutbox.case_id == case_id,
            SiteOrderStageOutbox.target_stage == "WON",
            SiteOrderStageOutbox.status == stage_outbox.STATUS_APPLIED,
        )
        .limit(1)
    )
    return won_outbox_id is not None


def _full_refund_ready_batch(
    candidates: list[Candidate],
    *,
    money_evidence: dict[str, MoneyRefundEvidence],
    rtu_signals: dict[str, dict[str, Any]],
    client: fulfillment.BitrixChatClient,
) -> tuple[list[tuple[Candidate, FullRefundEvidence]], Counter[str]]:
    live = _live_deals(client, [item.order_number for item in candidates])
    reasons: Counter[str] = Counter()
    ready: list[tuple[Candidate, FullRefundEvidence]] = []
    with session_scope(read_only=True) as session:
        for candidate in candidates:
            refund = money_evidence.get(candidate.order_number)
            if refund is None:
                reasons["money_evidence_missing"] += 1
                continue
            if refund.classification != "full_refund":
                reasons[refund.classification] += 1
                continue

            signal = rtu_signals.get(candidate.order_number)
            if signal is None:
                reasons["rtu_evidence_missing"] += 1
                continue
            rtu_count = int(signal.get("rtu_count") or 0)
            returned_rtu_count = int(signal.get("returned_rtu_count") or 0)
            issued_rtu_count = int(signal.get("issued_rtu_count") or 0)
            posted_sale_amount = Decimal(str(signal.get("posted_sale_amount") or 0))
            returned_goods_amount = Decimal(str(signal.get("returned_amount") or 0))
            latest_return_at = signal.get("latest_return_at")
            if issued_rtu_count > 0:
                reasons["issued_in_onec"] += 1
                continue
            if rtu_count <= 0 or returned_rtu_count != rtu_count:
                reasons["not_all_rtu_returned"] += 1
                continue
            if posted_sale_amount <= MONEY_TOLERANCE:
                reasons["sale_amount_missing"] += 1
                continue
            if returned_goods_amount < posted_sale_amount - MONEY_TOLERANCE:
                reasons["goods_return_incomplete"] += 1
                continue
            if not isinstance(latest_return_at, datetime):
                reasons["return_chronology_missing"] += 1
                continue

            deals = live.get(candidate.order_number, [])
            if len(deals) != 1:
                reasons["deal_not_unique"] += 1
                continue
            deal = deals[0]
            if int(deal["ID"]) != candidate.deal_id:
                reasons["deal_changed"] += 1
                continue
            stage = str(deal.get("STAGE_ID") or "").strip()
            if stage == FULL_REFUND_TARGET_STAGE:
                reasons["already_lose"] += 1
                continue
            if stage != "EXECUTING":
                reasons[f"unexpected_stage:{stage or '-'}"] += 1
                continue

            case_row = session.scalar(
                select(SiteOrderExecutionCase).where(
                    SiteOrderExecutionCase.site_order_number == candidate.order_number
                )
            )
            if case_row is None:
                reasons["execution_case_missing"] += 1
                continue
            if _has_confirmed_delivery_evidence(session, case_id=case_row.id):
                reasons["confirmed_delivery_evidence"] += 1
                continue
            warehouses = pickup_history._current_inventory_warehouse_ids(  # noqa: SLF001
                session,
                site_order_number=candidate.order_number,
            )
            if warehouses:
                reasons["current_inventory"] += 1
                continue

            canonical = json.dumps(
                {
                    "order_number": candidate.order_number,
                    "rtu_count": rtu_count,
                    "returned_rtu_count": returned_rtu_count,
                    "posted_sale_amount": str(posted_sale_amount),
                    "returned_goods_amount": str(returned_goods_amount),
                    "payment_amount": str(refund.payment_amount),
                    "refund_amount": str(refund.refund_amount),
                    "latest_return_at": latest_return_at.isoformat(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            ready.append(
                (
                    candidate,
                    FullRefundEvidence(
                        order_number=candidate.order_number,
                        rtu_count=rtu_count,
                        returned_rtu_count=returned_rtu_count,
                        posted_sale_amount=posted_sale_amount,
                        returned_goods_amount=returned_goods_amount,
                        payment_amount=refund.payment_amount,
                        refund_amount=refund.refund_amount,
                        latest_return_at=latest_return_at,
                        fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    ),
                )
            )
    return ready, reasons


def _enqueue_full_refunds(ready: list[tuple[Candidate, FullRefundEvidence]]) -> list[int]:
    outbox_ids: list[int] = []
    now = datetime.now()
    with session_scope() as session:
        for candidate, evidence in ready:
            case_row = session.scalar(
                select(SiteOrderExecutionCase).where(
                    SiteOrderExecutionCase.site_order_number == candidate.order_number
                )
            )
            if case_row is None:
                continue
            source_ref = f"historical-full-refund:{evidence.fingerprint}"
            payload = {
                "pipeline": "execution_reconciliation",
                "historical": True,
                "decision": {
                    "action": "update_stage",
                    "reason": FULL_REFUND_REASON,
                    "target_stage": FULL_REFUND_TARGET_STAGE,
                },
                "evidence_fingerprint": evidence.fingerprint,
                "full_goods_return": {
                    "rtu_count": evidence.rtu_count,
                    "returned_rtu_count": evidence.returned_rtu_count,
                    "posted_sale_amount": str(evidence.posted_sale_amount),
                    "returned_amount": str(evidence.returned_goods_amount),
                    "latest_return_at": evidence.latest_return_at.isoformat(),
                },
                "money_refund": {
                    "payment_amount": str(evidence.payment_amount),
                    "refund_amount": str(evidence.refund_amount),
                    "link": "exact_single_order_return",
                },
            }
            event = fulfillment.upsert_execution_event(
                session,
                site_order_number=candidate.order_number,
                event_type=FULL_REFUND_EVENT_TYPE,
                event_at=evidence.latest_return_at,
                source="onec",
                source_ref=source_ref,
                confidence="strong",
                raw_message_id=None,
                payload=payload,
            )
            if event is None:
                existing = session.scalar(
                    select(SiteOrderStageOutbox).where(
                        SiteOrderStageOutbox.idempotency_key
                        == (
                            f"execution-stage|{candidate.order_number}|"
                            f"{evidence.fingerprint}|{FULL_REFUND_TARGET_STAGE}"
                        )
                    )
                )
                if existing is not None and existing.status in {
                    stage_outbox.STATUS_PENDING,
                    stage_outbox.STATUS_RETRY,
                }:
                    outbox_ids.append(existing.id)
                continue
            case_row.bitrix_deal_id = candidate.deal_id
            case_row.current_derived_status = FULL_REFUND_EVENT_TYPE
            case_row.current_crm_stage = "EXECUTING"
            case_row.confidence = "strong"
            case_row.last_evidence_event_id = event.id
            case_row.updated_at = now
            outbox = SiteOrderStageOutbox(
                case_id=case_row.id,
                event_id=event.id,
                idempotency_key=(
                    f"execution-stage|{candidate.order_number}|{evidence.fingerprint}|"
                    f"{FULL_REFUND_TARGET_STAGE}"
                ),
                site_order_number=candidate.order_number,
                bitrix_deal_id=candidate.deal_id,
                source_event_type=FULL_REFUND_EVENT_TYPE,
                target_stage=FULL_REFUND_TARGET_STAGE,
                payload=payload,
            )
            session.add(outbox)
            session.flush()
            outbox_ids.append(outbox.id)
        session.commit()
    return outbox_ids


def _pickup_return_ready_batch(
    candidates: list[Candidate],
    *,
    payment_movements: dict[str, list[PaymentMovement]],
    rtu_signals: dict[str, dict[str, Any]],
    client: fulfillment.BitrixChatClient,
    target_stage: str,
) -> tuple[list[tuple[Candidate, PickupReturnEvidence]], Counter[str]]:
    if target_stage not in {
        PICKUP_PAID_RETURN_TARGET_STAGE,
        PICKUP_RETURNED_LATE_PAYMENT_TARGET_STAGE,
    }:
        raise ValueError(f"unsupported pickup return target stage: {target_stage}")
    live = _live_deals(client, [item.order_number for item in candidates])
    reasons: Counter[str] = Counter()
    ready: list[tuple[Candidate, PickupReturnEvidence]] = []
    with session_scope(read_only=True) as session:
        for candidate in candidates:
            signal = rtu_signals.get(candidate.order_number)
            if signal is None:
                reasons["rtu_evidence_missing"] += 1
                continue
            rtu_count = int(signal.get("rtu_count") or 0)
            returned_rtu_count = int(signal.get("returned_rtu_count") or 0)
            posted_sale_amount = Decimal(str(signal.get("posted_sale_amount") or 0))
            returned_goods_amount = Decimal(str(signal.get("returned_amount") or 0))
            latest_rtu_at = signal.get("latest_rtu_date")
            latest_return_at = signal.get("latest_return_at")
            if rtu_count <= 0 or returned_rtu_count != rtu_count:
                reasons["not_all_rtu_returned"] += 1
                continue
            if posted_sale_amount <= MONEY_TOLERANCE:
                reasons["sale_amount_missing"] += 1
                continue
            if returned_goods_amount < posted_sale_amount - MONEY_TOLERANCE:
                reasons["goods_return_incomplete"] += 1
                continue
            if not isinstance(latest_rtu_at, datetime) or not isinstance(
                latest_return_at, datetime
            ):
                reasons["return_chronology_missing"] += 1
                continue
            if latest_return_at <= latest_rtu_at:
                reasons["return_not_after_rtu"] += 1
                continue

            before_amount, after_amount, qualifying_at = _pickup_payment_sequence(
                payment_movements.get(candidate.order_number, []),
                latest_rtu_at=latest_rtu_at,
                latest_return_at=latest_return_at,
                posted_sale_amount=posted_sale_amount,
            )
            if target_stage == PICKUP_PAID_RETURN_TARGET_STAGE:
                if qualifying_at is None:
                    reasons["payment_before_return_not_full"] += 1
                    continue
            elif qualifying_at is not None:
                reasons["confirmed_payment_before_return"] += 1
                continue
            elif after_amount <= MONEY_TOLERANCE:
                reasons["payment_after_return_missing"] += 1
                continue

            deals = live.get(candidate.order_number, [])
            if len(deals) != 1:
                reasons["deal_not_unique"] += 1
                continue
            deal = deals[0]
            if int(deal["ID"]) != candidate.deal_id:
                reasons["deal_changed"] += 1
                continue
            stage = str(deal.get("STAGE_ID") or "").strip()
            if stage == target_stage:
                reasons[f"already_{target_stage.lower()}"] += 1
                continue
            if stage != "EXECUTING":
                reasons[f"unexpected_stage:{stage or '-'}"] += 1
                continue
            delivery = str(deal.get(fulfillment.CRM_DELIVERY_FIELD) or "").strip()
            if fulfillment.classify_delivery_method(delivery) != fulfillment.DELIVERY_CLASS_PICKUP:
                reasons["not_pickup_delivery"] += 1
                continue

            case_row = session.scalar(
                select(SiteOrderExecutionCase).where(
                    SiteOrderExecutionCase.site_order_number == candidate.order_number
                )
            )
            if case_row is None:
                reasons["execution_case_missing"] += 1
                continue
            warehouses = pickup_history._current_inventory_warehouse_ids(  # noqa: SLF001
                session,
                site_order_number=candidate.order_number,
            )
            if warehouses:
                reasons["current_inventory"] += 1
                continue

            canonical = json.dumps(
                {
                    "order_number": candidate.order_number,
                    "target_stage": target_stage,
                    "rtu_count": rtu_count,
                    "returned_rtu_count": returned_rtu_count,
                    "posted_sale_amount": str(posted_sale_amount),
                    "returned_goods_amount": str(returned_goods_amount),
                    "latest_rtu_at": latest_rtu_at.isoformat(),
                    "latest_return_at": latest_return_at.isoformat(),
                    "payment_before_return_amount": str(before_amount),
                    "payment_after_return_amount": str(after_amount),
                    "qualifying_payment_at": (
                        qualifying_at.isoformat() if qualifying_at is not None else None
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            ready.append(
                (
                    candidate,
                    PickupReturnEvidence(
                        order_number=candidate.order_number,
                        rtu_count=rtu_count,
                        returned_rtu_count=returned_rtu_count,
                        posted_sale_amount=posted_sale_amount,
                        returned_goods_amount=returned_goods_amount,
                        latest_rtu_at=latest_rtu_at,
                        latest_return_at=latest_return_at,
                        payment_before_return_amount=before_amount,
                        payment_after_return_amount=after_amount,
                        qualifying_payment_at=qualifying_at,
                        fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    ),
                )
            )
    return ready, reasons


def _enqueue_pickup_returns(
    ready: list[tuple[Candidate, PickupReturnEvidence]],
    *,
    event_type: str,
    reason: str,
    target_stage: str,
) -> list[int]:
    outbox_ids: list[int] = []
    now = datetime.now()
    with session_scope() as session:
        for candidate, evidence in ready:
            case_row = session.scalar(
                select(SiteOrderExecutionCase).where(
                    SiteOrderExecutionCase.site_order_number == candidate.order_number
                )
            )
            if case_row is None:
                continue
            source_ref = f"historical-pickup-return:{reason}:{evidence.fingerprint}"
            payload = {
                "pipeline": "execution_reconciliation",
                "historical": True,
                "explicit_user_approved_batch": True,
                "decision": {
                    "action": "update_stage",
                    "reason": reason,
                    "target_stage": target_stage,
                },
                "evidence_fingerprint": evidence.fingerprint,
                "pickup_return_chronology": {
                    "rtu_count": evidence.rtu_count,
                    "returned_rtu_count": evidence.returned_rtu_count,
                    "posted_sale_amount": str(evidence.posted_sale_amount),
                    "returned_goods_amount": str(evidence.returned_goods_amount),
                    "latest_rtu_at": evidence.latest_rtu_at.isoformat(),
                    "latest_return_at": evidence.latest_return_at.isoformat(),
                    "payment_before_return_amount": str(evidence.payment_before_return_amount),
                    "payment_after_return_amount": str(evidence.payment_after_return_amount),
                    "qualifying_payment_at": (
                        evidence.qualifying_payment_at.isoformat()
                        if evidence.qualifying_payment_at is not None
                        else None
                    ),
                },
            }
            event_at = evidence.qualifying_payment_at or evidence.latest_return_at
            event = fulfillment.upsert_execution_event(
                session,
                site_order_number=candidate.order_number,
                event_type=event_type,
                event_at=event_at,
                source="onec",
                source_ref=source_ref,
                confidence="strong",
                raw_message_id=None,
                payload=payload,
            )
            if event is None:
                existing = session.scalar(
                    select(SiteOrderStageOutbox).where(
                        SiteOrderStageOutbox.idempotency_key
                        == (
                            f"execution-stage|{candidate.order_number}|"
                            f"{evidence.fingerprint}|{target_stage}"
                        )
                    )
                )
                if existing is not None and existing.status in {
                    stage_outbox.STATUS_PENDING,
                    stage_outbox.STATUS_RETRY,
                }:
                    outbox_ids.append(existing.id)
                continue
            case_row.bitrix_deal_id = candidate.deal_id
            case_row.current_derived_status = event_type
            case_row.current_crm_stage = "EXECUTING"
            case_row.confidence = "strong"
            case_row.last_evidence_event_id = event.id
            case_row.updated_at = now
            outbox = SiteOrderStageOutbox(
                case_id=case_row.id,
                event_id=event.id,
                idempotency_key=(
                    f"execution-stage|{candidate.order_number}|{evidence.fingerprint}|"
                    f"{target_stage}"
                ),
                site_order_number=candidate.order_number,
                bitrix_deal_id=candidate.deal_id,
                source_event_type=event_type,
                target_stage=target_stage,
                payload=payload,
            )
            session.add(outbox)
            session.flush()
            outbox_ids.append(outbox.id)
        session.commit()
    return outbox_ids


def _record_case_stages(
    results: list[stage_outbox.StageOutboxResult], *, target_stage: str
) -> None:
    applied_orders = {item.site_order_number for item in results if item.applied}
    if not applied_orders:
        return
    now = datetime.now()
    with session_scope() as session:
        cases = session.scalars(
            select(SiteOrderExecutionCase).where(
                SiteOrderExecutionCase.site_order_number.in_(applied_orders)
            )
        ).all()
        for case_row in cases:
            case_row.current_crm_stage = target_stage
            case_row.updated_at = now
        session.commit()


def _pickup_issued_return_ready_batch(
    candidates: list[Candidate],
    *,
    movements: dict[str, list[IssuedRtuMovement]],
    client: fulfillment.BitrixChatClient,
) -> tuple[list[tuple[Candidate, PickupIssuedReturnEvidence]], Counter[str]]:
    live = _live_deals(client, [item.order_number for item in candidates])
    reasons: Counter[str] = Counter()
    ready: list[tuple[Candidate, PickupIssuedReturnEvidence]] = []
    with session_scope(read_only=True) as session:
        for candidate in candidates:
            rows = movements.get(candidate.order_number, [])
            if not rows:
                reasons["rtu_evidence_missing"] += 1
                continue
            issued_rows = [row for row in rows if row.issued and row.scanned_at is not None]
            qualifying_rows = _qualifying_issued_rtu_rows(rows)
            if not qualifying_rows:
                reasons["no_issued_rtu_before_return"] += 1
                continue

            deals = live.get(candidate.order_number, [])
            if len(deals) != 1:
                reasons["deal_not_unique"] += 1
                continue
            deal = deals[0]
            if int(deal["ID"]) != candidate.deal_id:
                reasons["deal_changed"] += 1
                continue
            stage = str(deal.get("STAGE_ID") or "").strip()
            if stage == PICKUP_ISSUED_RETURN_TARGET_STAGE:
                reasons["already_won"] += 1
                continue
            if stage != "EXECUTING":
                reasons[f"unexpected_stage:{stage or '-'}"] += 1
                continue
            delivery = str(deal.get(fulfillment.CRM_DELIVERY_FIELD) or "").strip()
            if fulfillment.classify_delivery_method(delivery) != fulfillment.DELIVERY_CLASS_PICKUP:
                reasons["not_pickup_delivery"] += 1
                continue

            case_row = session.scalar(
                select(SiteOrderExecutionCase).where(
                    SiteOrderExecutionCase.site_order_number == candidate.order_number
                )
            )
            if case_row is None:
                reasons["execution_case_missing"] += 1
                continue
            warehouses = pickup_history._current_inventory_warehouse_ids(  # noqa: SLF001
                session,
                site_order_number=candidate.order_number,
            )
            if warehouses:
                reasons["current_inventory"] += 1
                continue

            qualifying_numbers = tuple(sorted(row.rtu_number for row in qualifying_rows))
            latest_issue_at = max(
                row.scanned_at for row in qualifying_rows if row.scanned_at is not None
            )
            canonical = json.dumps(
                {
                    "order_number": candidate.order_number,
                    "rtu_count": len(rows),
                    "issued_rtu_count": len(issued_rows),
                    "qualifying_rtu_numbers": qualifying_numbers,
                    "qualifying_rows": [
                        {
                            "rtu_number": row.rtu_number,
                            "sale_amount": str(row.sale_amount),
                            "scanned_at": (
                                row.scanned_at.isoformat() if row.scanned_at is not None else None
                            ),
                            "returned_at": (
                                row.returned_at.isoformat() if row.returned_at is not None else None
                            ),
                        }
                        for row in qualifying_rows
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            ready.append(
                (
                    candidate,
                    PickupIssuedReturnEvidence(
                        order_number=candidate.order_number,
                        rtu_count=len(rows),
                        issued_rtu_count=len(issued_rows),
                        qualifying_issued_rtu_count=len(qualifying_rows),
                        retained_issued_rtu_count=sum(
                            1 for row in qualifying_rows if row.returned_at is None
                        ),
                        returned_after_issue_rtu_count=sum(
                            1 for row in qualifying_rows if row.returned_at is not None
                        ),
                        qualifying_rtu_numbers=qualifying_numbers,
                        latest_qualifying_issue_at=latest_issue_at,
                        fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    ),
                )
            )
    return ready, reasons


def _enqueue_pickup_issued_returns(
    ready: list[tuple[Candidate, PickupIssuedReturnEvidence]],
) -> list[int]:
    outbox_ids: list[int] = []
    now = datetime.now()
    with session_scope() as session:
        for candidate, evidence in ready:
            case_row = session.scalar(
                select(SiteOrderExecutionCase).where(
                    SiteOrderExecutionCase.site_order_number == candidate.order_number
                )
            )
            if case_row is None:
                continue
            source_ref = f"historical-pickup-issued-return:{evidence.fingerprint}"
            payload = {
                "pipeline": "execution_reconciliation",
                "historical": True,
                "explicit_user_approved_batch": True,
                "decision": {
                    "action": "update_stage",
                    "reason": PICKUP_ISSUED_RETURN_REASON,
                    "target_stage": PICKUP_ISSUED_RETURN_TARGET_STAGE,
                },
                "evidence_fingerprint": evidence.fingerprint,
                "pickup_issue_evidence": {
                    "rtu_count": evidence.rtu_count,
                    "issued_rtu_count": evidence.issued_rtu_count,
                    "qualifying_issued_rtu_count": evidence.qualifying_issued_rtu_count,
                    "retained_issued_rtu_count": evidence.retained_issued_rtu_count,
                    "returned_after_issue_rtu_count": (evidence.returned_after_issue_rtu_count),
                    "qualifying_rtu_numbers": list(evidence.qualifying_rtu_numbers),
                    "latest_qualifying_issue_at": (evidence.latest_qualifying_issue_at.isoformat()),
                },
            }
            event = fulfillment.upsert_execution_event(
                session,
                site_order_number=candidate.order_number,
                event_type=PICKUP_ISSUED_RETURN_EVENT_TYPE,
                event_at=evidence.latest_qualifying_issue_at,
                source="onec",
                source_ref=source_ref,
                confidence="strong",
                raw_message_id=None,
                payload=payload,
            )
            if event is None:
                existing = session.scalar(
                    select(SiteOrderStageOutbox).where(
                        SiteOrderStageOutbox.idempotency_key
                        == (
                            f"execution-stage|{candidate.order_number}|"
                            f"{evidence.fingerprint}|{PICKUP_ISSUED_RETURN_TARGET_STAGE}"
                        )
                    )
                )
                if existing is not None and existing.status in {
                    stage_outbox.STATUS_PENDING,
                    stage_outbox.STATUS_RETRY,
                }:
                    outbox_ids.append(existing.id)
                continue
            case_row.bitrix_deal_id = candidate.deal_id
            case_row.current_derived_status = PICKUP_ISSUED_RETURN_EVENT_TYPE
            case_row.current_crm_stage = "EXECUTING"
            case_row.confidence = "strong"
            case_row.last_evidence_event_id = event.id
            case_row.updated_at = now
            outbox = SiteOrderStageOutbox(
                case_id=case_row.id,
                event_id=event.id,
                idempotency_key=(
                    f"execution-stage|{candidate.order_number}|{evidence.fingerprint}|"
                    f"{PICKUP_ISSUED_RETURN_TARGET_STAGE}"
                ),
                site_order_number=candidate.order_number,
                bitrix_deal_id=candidate.deal_id,
                source_event_type=PICKUP_ISSUED_RETURN_EVENT_TYPE,
                target_stage=PICKUP_ISSUED_RETURN_TARGET_STAGE,
                payload=payload,
            )
            session.add(outbox)
            session.flush()
            outbox_ids.append(outbox.id)
        session.commit()
    return outbox_ids


def _select_candidates(
    all_candidates: list[Candidate],
    *,
    batch_number: int | None,
    order_numbers: list[str] | None,
    candidate_kind: str = "full-return",
) -> tuple[list[Candidate], int]:
    if batch_number is not None and order_numbers:
        raise SystemExit("--batch and --orders cannot be used together")
    if order_numbers:
        by_order = {item.order_number: item for item in all_candidates}
        missing = [value for value in order_numbers if value not in by_order]
        if missing:
            raise SystemExit(
                f"orders are not open {candidate_kind} candidates: {', '.join(missing)}"
            )
        return [by_order[value] for value in dict.fromkeys(order_numbers)], 0
    if batch_number is None:
        return all_candidates, 0
    if batch_number < 1:
        raise SystemExit("batch number must be positive")
    start = (batch_number - 1) * BATCH_SIZE
    candidates = all_candidates[start : start + BATCH_SIZE]
    if not candidates:
        raise SystemExit(f"batch {batch_number} is empty")
    return candidates, batch_number - 1


def _run_full_refunds(
    *,
    apply: bool,
    batch_number: int | None,
    order_numbers: list[str] | None,
    recover_pending: bool,
    client: fulfillment.BitrixChatClient,
) -> int:
    if recover_pending:
        return _recover_pending(
            client=client,
            event_type=FULL_REFUND_EVENT_TYPE,
            target_stage=FULL_REFUND_TARGET_STAGE,
            resolved_reason=FULL_REFUND_REASON,
        )
    if apply and batch_number is None and not order_numbers:
        raise SystemExit("full-refund apply requires one bounded --batch or --orders selection")
    all_candidates = _full_return_candidates()
    candidates, batch_offset = _select_candidates(
        all_candidates,
        batch_number=batch_number,
        order_numbers=order_numbers,
    )
    if apply and len(candidates) > BATCH_SIZE:
        raise SystemExit(f"full-refund apply is capped at {BATCH_SIZE} orders")

    totals: Counter[str] = Counter()
    classifications: Counter[str] = Counter()
    all_results: list[stage_outbox.StageOutboxResult] = []
    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start : start + BATCH_SIZE]
        batch_orders = [item.order_number for item in batch]
        money_evidence = _money_refund_evidence(batch_orders)
        classifications.update(item.classification for item in money_evidence.values())
        rtu_signals = sync.query_rtu_signal_by_orders(batch_orders)
        ready, blocked = _full_refund_ready_batch(
            batch,
            money_evidence=money_evidence,
            rtu_signals=rtu_signals,
            client=client,
        )
        totals.update(blocked)
        totals["ready"] += len(ready)
        batch_results: list[stage_outbox.StageOutboxResult] = []
        if apply and ready:
            outbox_ids = _enqueue_full_refunds(ready)
            batch_results = _apply_outbox(
                outbox_ids,
                client=client,
                target_stage=FULL_REFUND_TARGET_STAGE,
            )
            _finalize_applied(
                batch_results,
                target_stage=FULL_REFUND_TARGET_STAGE,
                resolved_reason=FULL_REFUND_REASON,
            )
            all_results.extend(batch_results)
        print(
            json.dumps(
                {
                    "batch": batch_offset + start // BATCH_SIZE + 1,
                    "scanned": len(batch),
                    "money_classifications": dict(
                        Counter(
                            money_evidence[item.order_number].classification
                            for item in batch
                            if item.order_number in money_evidence
                        )
                    ),
                    "ready": len(ready),
                    "ready_orders": [item.order_number for item, _ in ready],
                    "blocked": dict(blocked),
                    "applied": sum(1 for item in batch_results if item.applied),
                    "results": dict(Counter(item.result for item in batch_results)),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    print(
        json.dumps(
            {
                "mode": "full-refund-apply" if apply else "full-refund-dry-run",
                "header_candidates": len(all_candidates),
                "scanned_candidates": len(candidates),
                "money_classifications": dict(classifications),
                "totals": dict(totals),
                "applied": sum(1 for item in all_results if item.applied),
                "result_counts": dict(Counter(item.result for item in all_results)),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _run_pickup_return_mode(
    *,
    apply: bool,
    order_numbers: list[str] | None,
    recover_pending: bool,
    client: fulfillment.BitrixChatClient,
    paid_before_return: bool,
) -> int:
    if paid_before_return:
        event_type = PICKUP_PAID_RETURN_EVENT_TYPE
        reason = PICKUP_PAID_RETURN_REASON
        target_stage = PICKUP_PAID_RETURN_TARGET_STAGE
        mode_name = "pickup-paid-then-returned"
    else:
        event_type = PICKUP_RETURNED_LATE_PAYMENT_EVENT_TYPE
        reason = PICKUP_RETURNED_LATE_PAYMENT_REASON
        target_stage = PICKUP_RETURNED_LATE_PAYMENT_TARGET_STAGE
        mode_name = "pickup-returned-without-prior-payment"

    if recover_pending:
        with session_scope(read_only=True) as session:
            outbox_ids = list(
                session.scalars(
                    select(SiteOrderStageOutbox.id)
                    .where(
                        SiteOrderStageOutbox.source_event_type == event_type,
                        SiteOrderStageOutbox.status.in_(
                            [stage_outbox.STATUS_PENDING, stage_outbox.STATUS_RETRY]
                        ),
                    )
                    .order_by(SiteOrderStageOutbox.id.asc())
                    .limit(BATCH_SIZE)
                ).all()
            )
        results = _apply_outbox(outbox_ids, client=client, target_stage=target_stage)
        _record_case_stages(results, target_stage=target_stage)
        print(
            json.dumps(
                {
                    "mode": f"{mode_name}-recover",
                    "pending": len(outbox_ids),
                    "applied": sum(1 for item in results if item.applied),
                    "result_counts": dict(Counter(item.result for item in results)),
                },
                ensure_ascii=False,
            )
        )
        return 0

    if not order_numbers:
        raise SystemExit(f"{mode_name} requires an explicit --orders selection")
    all_candidates = _full_return_candidates()
    candidates, _ = _select_candidates(
        all_candidates,
        batch_number=None,
        order_numbers=order_numbers,
        candidate_kind="full-return",
    )
    if len(candidates) > BATCH_SIZE:
        raise SystemExit(f"{mode_name} is capped at {BATCH_SIZE} explicit orders")

    selected_orders = [item.order_number for item in candidates]
    payment_movements = _pickup_payment_movements(selected_orders)
    rtu_signals = sync.query_rtu_signal_by_orders(selected_orders)
    ready, blocked = _pickup_return_ready_batch(
        candidates,
        payment_movements=payment_movements,
        rtu_signals=rtu_signals,
        client=client,
        target_stage=target_stage,
    )
    results: list[stage_outbox.StageOutboxResult] = []
    if apply and ready:
        outbox_ids = _enqueue_pickup_returns(
            ready,
            event_type=event_type,
            reason=reason,
            target_stage=target_stage,
        )
        results = _apply_outbox(outbox_ids, client=client, target_stage=target_stage)
        _record_case_stages(results, target_stage=target_stage)
    print(
        json.dumps(
            {
                "mode": f"{mode_name}-apply" if apply else f"{mode_name}-dry-run",
                "scanned": len(candidates),
                "ready": len(ready),
                "ready_orders": [item.order_number for item, _ in ready],
                "ready_evidence": [
                    {
                        "order_number": item.order_number,
                        "sale_amount": str(evidence.posted_sale_amount),
                        "returned_amount": str(evidence.returned_goods_amount),
                        "latest_rtu_at": evidence.latest_rtu_at.isoformat(),
                        "latest_return_at": evidence.latest_return_at.isoformat(),
                        "payment_before_return_amount": str(evidence.payment_before_return_amount),
                        "payment_after_return_amount": str(evidence.payment_after_return_amount),
                    }
                    for item, evidence in ready
                ],
                "blocked": dict(blocked),
                "applied": sum(1 for item in results if item.applied),
                "result_counts": dict(Counter(item.result for item in results)),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _run_pickup_issued_returns(
    *,
    apply: bool,
    order_numbers: list[str] | None,
    recover_pending: bool,
    client: fulfillment.BitrixChatClient,
) -> int:
    if recover_pending:
        with session_scope(read_only=True) as session:
            outbox_ids = list(
                session.scalars(
                    select(SiteOrderStageOutbox.id)
                    .where(
                        SiteOrderStageOutbox.source_event_type == PICKUP_ISSUED_RETURN_EVENT_TYPE,
                        SiteOrderStageOutbox.status.in_(
                            [stage_outbox.STATUS_PENDING, stage_outbox.STATUS_RETRY]
                        ),
                    )
                    .order_by(SiteOrderStageOutbox.id.asc())
                    .limit(BATCH_SIZE)
                ).all()
            )
        results = _apply_outbox(
            outbox_ids,
            client=client,
            target_stage=PICKUP_ISSUED_RETURN_TARGET_STAGE,
        )
        _record_case_stages(results, target_stage=PICKUP_ISSUED_RETURN_TARGET_STAGE)
        print(
            json.dumps(
                {
                    "mode": "pickup-issued-then-returned-recover",
                    "pending": len(outbox_ids),
                    "applied": sum(1 for item in results if item.applied),
                    "result_counts": dict(Counter(item.result for item in results)),
                },
                ensure_ascii=False,
            )
        )
        return 0

    if not order_numbers:
        raise SystemExit("pickup-issued-then-returned requires an explicit --orders selection")
    candidates, _ = _select_candidates(
        _issued_return_candidates(),
        batch_number=None,
        order_numbers=order_numbers,
        candidate_kind="issued-return",
    )
    if len(candidates) > BATCH_SIZE:
        raise SystemExit(f"pickup-issued-then-returned is capped at {BATCH_SIZE} explicit orders")
    selected_orders = [item.order_number for item in candidates]
    movements = _pickup_issued_rtu_movements(selected_orders)
    ready, blocked = _pickup_issued_return_ready_batch(
        candidates,
        movements=movements,
        client=client,
    )
    results: list[stage_outbox.StageOutboxResult] = []
    if apply and ready:
        outbox_ids = _enqueue_pickup_issued_returns(ready)
        results = _apply_outbox(
            outbox_ids,
            client=client,
            target_stage=PICKUP_ISSUED_RETURN_TARGET_STAGE,
        )
        _record_case_stages(results, target_stage=PICKUP_ISSUED_RETURN_TARGET_STAGE)
    print(
        json.dumps(
            {
                "mode": (
                    "pickup-issued-then-returned-apply"
                    if apply
                    else "pickup-issued-then-returned-dry-run"
                ),
                "scanned": len(candidates),
                "ready": len(ready),
                "ready_orders": [item.order_number for item, _ in ready],
                "ready_evidence": [
                    {
                        "order_number": item.order_number,
                        "rtu_count": evidence.rtu_count,
                        "issued_rtu_count": evidence.issued_rtu_count,
                        "qualifying_issued_rtu_count": (evidence.qualifying_issued_rtu_count),
                        "retained_issued_rtu_count": evidence.retained_issued_rtu_count,
                        "returned_after_issue_rtu_count": (evidence.returned_after_issue_rtu_count),
                    }
                    for item, evidence in ready
                ],
                "blocked": dict(blocked),
                "applied": sum(1 for item in results if item.applied),
                "result_counts": dict(Counter(item.result for item in results)),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _snapshot_evidence_at(
    snapshot: execution_reconciliation.ExecutionEvidenceSnapshot,
) -> datetime | None:
    return max(
        (
            value
            for value in (
                snapshot.latest_rtu_at,
                snapshot.latest_assembled_at,
                snapshot.latest_issued_at,
                snapshot.latest_return_at,
            )
            if value is not None
        ),
        default=None,
    )


def _classify_stale_execution_composite(
    *,
    snapshot: execution_reconciliation.ExecutionEvidenceSnapshot,
    decision: execution_reconciliation.ExecutionDecision,
    chat_event_type: str,
    chat_event_at: datetime | None,
    chat_event_source: str,
    chat_event_confidence: str,
) -> tuple[str | None, str]:
    """Return a strict target for compatible 1C and later chat evidence."""

    if not snapshot.historical:
        return None, "onec_evidence_not_historical"
    onec_event_at = _snapshot_evidence_at(snapshot)
    if onec_event_at is None:
        return None, "onec_evidence_time_missing"
    if chat_event_at is None:
        return None, "chat_event_time_missing"
    if chat_event_at <= onec_event_at:
        return None, "chat_event_not_later"
    if chat_event_source not in STALE_EXECUTION_CHAT_SOURCES:
        return None, "latest_event_not_chat_confirmation"
    if decision.action != execution_reconciliation.ACTION_UPDATE_STAGE:
        return None, f"onec_decision_not_stage_update:{decision.reason}"

    if chat_event_type == fulfillment.EVENT_PICKUP_RECEIVED:
        if chat_event_confidence != "strong":
            return None, "pickup_received_not_strong"
        if decision.target_stage == "WON" and decision.reason == "pickup_printed_and_scanned":
            return "WON", "onec_issued_and_later_pickup_received"
        if (
            decision.target_stage == "FINAL_INVOICE"
            and decision.reason == "assembled_without_return"
        ):
            return "WON", "onec_assembled_and_later_pickup_received"
        return None, f"pickup_received_conflicts_with_onec:{decision.target_stage or '-'}"

    if chat_event_type in {
        fulfillment.EVENT_PICKUP_DISMANTLING,
        fulfillment.EVENT_PICKUP_UNCLAIMED,
    }:
        if decision.target_stage == "LOSE" and decision.reason == "full_unpaid_return":
            return "LOSE", "onec_full_unpaid_return_and_later_nonreceipt"
        return None, f"pickup_nonreceipt_conflicts_with_onec:{decision.target_stage or '-'}"

    return None, f"unsupported_latest_chat_event:{chat_event_type or '-'}"


def _fetch_stale_execution_deals(
    client: fulfillment.BitrixChatClient,
    order_numbers: list[str],
) -> dict[str, list[fulfillment.BitrixDealSnapshot]]:
    result: dict[str, list[fulfillment.BitrixDealSnapshot]] = defaultdict(list)
    for start in range(0, len(order_numbers), 40):
        batch = order_numbers[start : start + 40]
        offset: int | None = 0
        while offset is not None:
            response = client.call(
                "crm.deal.list",
                {
                    "filter": {f"@{fulfillment.CRM_ORDER_NUMBER_FIELD}": batch},
                    "select": [
                        *fulfillment.CRM_REVIEW_SELECT_FIELDS,
                        sync.CRM_ASSEMBLED_FIELD,
                    ],
                    "order": {"ID": "ASC"},
                    "start": offset,
                },
            )
            rows = response.get("result") or []
            if not isinstance(rows, list):
                raise RuntimeError("crm.deal.list returned invalid result")
            for raw in rows:
                deal = fulfillment.bitrix_deal_from_payload(raw)
                if deal is None:
                    continue
                order_number = fulfillment._clean_string(  # noqa: SLF001
                    (deal.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
                )
                if order_number:
                    result[order_number].append(deal)
            next_value = response.get("next")
            offset = int(next_value) if next_value is not None else None
    return result


def _latest_execution_event(
    session: Any,
    *,
    case_id: int,
) -> SiteOrderExecutionEvent | None:
    return session.scalar(
        select(SiteOrderExecutionEvent)
        .where(SiteOrderExecutionEvent.case_id == case_id)
        .order_by(
            SiteOrderExecutionEvent.event_at.desc().nullslast(),
            SiteOrderExecutionEvent.id.desc(),
        )
        .limit(1)
    )


def _stale_execution_ready_batch(
    order_numbers: list[str],
    *,
    client: fulfillment.BitrixChatClient,
) -> tuple[list[StaleExecutionCompositeEvidence], Counter[str]]:
    live = _fetch_stale_execution_deals(client, order_numbers)
    blocked: Counter[str] = Counter()
    unique_deals: list[fulfillment.BitrixDealSnapshot] = []
    for order_number in order_numbers:
        deals = live.get(order_number, [])
        if len(deals) != 1:
            blocked["deal_not_unique"] += 1
            continue
        deal = deals[0]
        stage = fulfillment._clean_string(deal.stage_id)  # noqa: SLF001
        if stage != "EXECUTING":
            blocked[f"unexpected_stage:{stage or '-'}"] += 1
            continue
        unique_deals.append(deal)

    if not unique_deals:
        return [], blocked

    selected_orders = [
        fulfillment._clean_string(  # noqa: SLF001
            (deal.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
        )
        for deal in unique_deals
    ]
    settings = get_settings()
    if not settings.onec_database_url:
        blocked["onec_not_configured"] += len(unique_deals)
        return [], blocked

    rtu_signals = sync.query_rtu_signal_by_orders(selected_orders)
    order_states = sync.query_onec_order_states_by_orders(selected_orders)
    for order_number, order_state in order_states.items():
        rtu_signals.setdefault(order_number, {}).update(order_state)
    settlements = sync.fetch_onec_order_settlements(selected_orders, strict=True)
    snapshots = sync.build_execution_snapshots(
        unique_deals,
        order_statuses={},
        onec_settlements=settlements,
        rtu_signals=rtu_signals,
        cutover_at=settings.order_fulfillment_execution_cutover_at,
        onec_evidence_available=True,
    )

    ready: list[StaleExecutionCompositeEvidence] = []
    with session_scope(read_only=True) as session:
        for snapshot in snapshots:
            case_row = session.scalar(
                select(SiteOrderExecutionCase).where(
                    SiteOrderExecutionCase.site_order_number == snapshot.site_order_number
                )
            )
            if case_row is None:
                blocked["execution_case_missing"] += 1
                continue
            if case_row.bitrix_deal_id not in (None, snapshot.bitrix_deal_id):
                blocked["case_deal_changed"] += 1
                continue
            event = _latest_execution_event(session, case_id=case_row.id)
            if event is None:
                blocked["latest_event_missing"] += 1
                continue
            if event.source == fulfillment.SOURCE_BITRIX_CHAT and event.raw_message_id:
                message = session.get(BitrixChatMessage, event.raw_message_id)
                if message is not None and message.parse_status == "edited_manual_review":
                    blocked["latest_chat_message_edited"] += 1
                    continue
            warehouses = pickup_history._current_inventory_warehouse_ids(  # noqa: SLF001
                session,
                site_order_number=snapshot.site_order_number,
            )
            if warehouses:
                blocked["current_inventory"] += 1
                continue
            decision = execution_reconciliation.decide_execution_stage(snapshot)
            target_stage, composite_reason = _classify_stale_execution_composite(
                snapshot=snapshot,
                decision=decision,
                chat_event_type=event.event_type,
                chat_event_at=event.event_at,
                chat_event_source=event.source,
                chat_event_confidence=event.confidence,
            )
            if target_stage is None:
                blocked[composite_reason] += 1
                continue
            onec_event_at = _snapshot_evidence_at(snapshot)
            if onec_event_at is None or event.event_at is None:
                blocked["composite_time_missing"] += 1
                continue
            fingerprint_payload = {
                "snapshot": execution_reconciliation.snapshot_fingerprint(snapshot),
                "onec_decision_reason": decision.reason,
                "onec_target_stage": decision.target_stage,
                "chat_event_id": event.id,
                "chat_event_type": event.event_type,
                "chat_event_at": event.event_at.isoformat(),
                "chat_event_source": event.source,
                "chat_event_confidence": event.confidence,
                "target_stage": target_stage,
                "composite_reason": composite_reason,
            }
            fingerprint = hashlib.sha256(
                json.dumps(
                    fingerprint_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            ready.append(
                StaleExecutionCompositeEvidence(
                    order_number=snapshot.site_order_number,
                    deal_id=snapshot.bitrix_deal_id,
                    onec_event_at=onec_event_at,
                    onec_decision_reason=decision.reason,
                    onec_target_stage=decision.target_stage or "",
                    chat_event_id=event.id,
                    chat_event_type=event.event_type,
                    chat_event_at=event.event_at,
                    chat_event_source=event.source,
                    chat_event_confidence=event.confidence,
                    target_stage=target_stage,
                    composite_reason=composite_reason,
                    fingerprint=fingerprint,
                )
            )
    return ready, blocked


def _enqueue_stale_execution_composites(
    ready: list[StaleExecutionCompositeEvidence],
) -> list[int]:
    outbox_ids: list[int] = []
    now = datetime.now()
    with session_scope() as session:
        for evidence in ready:
            case_row = session.scalar(
                select(SiteOrderExecutionCase)
                .where(SiteOrderExecutionCase.site_order_number == evidence.order_number)
                .with_for_update()
            )
            if case_row is None:
                continue
            if case_row.bitrix_deal_id not in (None, evidence.deal_id):
                continue
            latest_event = _latest_execution_event(session, case_id=case_row.id)
            if latest_event is None or latest_event.id != evidence.chat_event_id:
                continue

            event_type = (
                STALE_EXECUTION_WON_EVENT_TYPE
                if evidence.target_stage == "WON"
                else STALE_EXECUTION_LOSE_EVENT_TYPE
            )
            idempotency_key = (
                f"execution-stage|{evidence.order_number}|{evidence.fingerprint}|"
                f"{evidence.target_stage}"
            )
            payload = {
                "pipeline": execution_reconciliation.EXECUTION_PIPELINE,
                "historical": True,
                "composite_reconciliation": True,
                "sms_allowed": False,
                "evidence_fingerprint": evidence.fingerprint,
                "decision": {
                    "action": execution_reconciliation.ACTION_UPDATE_STAGE,
                    "reason": evidence.composite_reason,
                    "target_stage": evidence.target_stage,
                },
                "onec": {
                    "evidence_at": evidence.onec_event_at.isoformat(),
                    "decision_reason": evidence.onec_decision_reason,
                    "target_stage": evidence.onec_target_stage,
                },
                "chat": {
                    "event_id": evidence.chat_event_id,
                    "event_type": evidence.chat_event_type,
                    "event_at": evidence.chat_event_at.isoformat(),
                    "source": evidence.chat_event_source,
                    "confidence": evidence.chat_event_confidence,
                },
            }
            event = fulfillment.upsert_execution_event(
                session,
                site_order_number=evidence.order_number,
                event_type=event_type,
                event_at=evidence.chat_event_at,
                source=STALE_EXECUTION_SOURCE,
                source_ref=f"historical-stale-composite:{evidence.fingerprint}",
                confidence="strong",
                raw_message_id=None,
                payload=payload,
            )
            if event is None:
                existing = session.scalar(
                    select(SiteOrderStageOutbox).where(
                        SiteOrderStageOutbox.idempotency_key == idempotency_key
                    )
                )
                if existing is not None and existing.status in {
                    stage_outbox.STATUS_PENDING,
                    stage_outbox.STATUS_RETRY,
                }:
                    outbox_ids.append(existing.id)
                continue

            case_row.bitrix_deal_id = evidence.deal_id
            case_row.current_derived_status = event_type
            case_row.current_crm_stage = "EXECUTING"
            case_row.confidence = "strong"
            case_row.last_evidence_event_id = event.id
            case_row.updated_at = now
            outbox = SiteOrderStageOutbox(
                case_id=case_row.id,
                event_id=event.id,
                idempotency_key=idempotency_key,
                site_order_number=evidence.order_number,
                bitrix_deal_id=evidence.deal_id,
                source_event_type=event_type,
                target_stage=evidence.target_stage,
                payload=payload,
            )
            session.add(outbox)
            session.flush()
            outbox_ids.append(outbox.id)
        session.commit()
    return outbox_ids


def _recover_stale_execution_pending(
    *,
    order_numbers: list[str],
    client: fulfillment.BitrixChatClient,
) -> list[stage_outbox.StageOutboxResult]:
    with session_scope(read_only=True) as session:
        rows = session.scalars(
            select(SiteOrderStageOutbox)
            .where(
                SiteOrderStageOutbox.site_order_number.in_(order_numbers),
                SiteOrderStageOutbox.source_event_type.in_(STALE_EXECUTION_EVENT_TYPES),
                SiteOrderStageOutbox.status.in_(
                    [stage_outbox.STATUS_PENDING, stage_outbox.STATUS_RETRY]
                ),
            )
            .order_by(SiteOrderStageOutbox.id.asc())
        ).all()
    results: list[stage_outbox.StageOutboxResult] = []
    for target_stage in ("WON", "LOSE"):
        ids = [row.id for row in rows if row.target_stage == target_stage]
        target_results = _apply_outbox(ids, client=client, target_stage=target_stage)
        _record_case_stages(target_results, target_stage=target_stage)
        results.extend(target_results)
    return results


def _run_stale_execution(
    *,
    apply: bool,
    order_numbers: list[str] | None,
    recover_pending: bool,
    client: fulfillment.BitrixChatClient,
) -> int:
    if not order_numbers:
        raise SystemExit("stale-execution requires an explicit --orders selection")
    order_numbers = list(dict.fromkeys(order_numbers))
    if len(order_numbers) > BATCH_SIZE:
        raise SystemExit(f"stale-execution is capped at {BATCH_SIZE} explicit orders")
    if recover_pending:
        results = _recover_stale_execution_pending(
            order_numbers=order_numbers,
            client=client,
        )
        print(
            json.dumps(
                {
                    "mode": "stale-execution-recover",
                    "selected": len(order_numbers),
                    "applied": sum(1 for item in results if item.applied),
                    "result_counts": dict(Counter(item.result for item in results)),
                },
                ensure_ascii=False,
            )
        )
        return 0

    ready, blocked = _stale_execution_ready_batch(order_numbers, client=client)
    results: list[stage_outbox.StageOutboxResult] = []
    if apply and ready:
        outbox_ids = _enqueue_stale_execution_composites(ready)
        with session_scope(read_only=True) as session:
            outbox_targets = {
                row.id: row.target_stage
                for row in session.scalars(
                    select(SiteOrderStageOutbox).where(SiteOrderStageOutbox.id.in_(outbox_ids))
                ).all()
            }
        for target_stage in ("WON", "LOSE"):
            target_ids = [
                outbox_id
                for outbox_id in outbox_ids
                if outbox_targets.get(outbox_id) == target_stage
            ]
            target_results = _apply_outbox(
                target_ids,
                client=client,
                target_stage=target_stage,
            )
            _record_case_stages(target_results, target_stage=target_stage)
            results.extend(target_results)
    print(
        json.dumps(
            {
                "mode": "stale-execution-apply" if apply else "stale-execution-dry-run",
                "selected": len(order_numbers),
                "ready": len(ready),
                "ready_orders": [item.order_number for item in ready],
                "ready_targets": dict(Counter(item.target_stage for item in ready)),
                "blocked": dict(blocked),
                "applied": sum(1 for item in results if item.applied),
                "result_counts": dict(Counter(item.result for item in results)),
            },
            ensure_ascii=False,
        )
    )
    return 0


def run(
    *,
    apply: bool,
    batch_number: int | None = None,
    recover_pending: bool = False,
    full_refunds: bool = False,
    pickup_paid_then_returned: bool = False,
    pickup_returned_without_prior_payment: bool = False,
    pickup_issued_then_returned: bool = False,
    stale_execution: bool = False,
    order_numbers: list[str] | None = None,
) -> int:
    settings = get_settings()
    if recover_pending and not apply:
        raise SystemExit("--recover-pending requires --apply")
    if apply and not (
        settings.order_fulfillment_execution_master_enabled
        and settings.order_fulfillment_execution_stage_apply_enabled
        and settings.order_fulfillment_execution_historical_apply_enabled
    ):
        raise SystemExit("historical execution apply flags are not enabled")
    webhook_url = sync.resolve_bitrix_webhook_url()
    if not webhook_url:
        raise SystemExit("Bitrix webhook is not configured")
    client = fulfillment.BitrixChatClient(webhook_url)
    selected_modes = sum(
        int(value)
        for value in (
            full_refunds,
            pickup_paid_then_returned,
            pickup_returned_without_prior_payment,
            pickup_issued_then_returned,
            stale_execution,
        )
    )
    if selected_modes > 1:
        raise SystemExit("return reconciliation modes are mutually exclusive")
    if batch_number is not None and (
        pickup_paid_then_returned
        or pickup_returned_without_prior_payment
        or pickup_issued_then_returned
        or stale_execution
    ):
        raise SystemExit("explicit historical modes require --orders and do not accept --batch")
    if stale_execution:
        return _run_stale_execution(
            apply=apply,
            order_numbers=order_numbers,
            recover_pending=recover_pending,
            client=client,
        )
    if pickup_issued_then_returned:
        return _run_pickup_issued_returns(
            apply=apply,
            order_numbers=order_numbers,
            recover_pending=recover_pending,
            client=client,
        )
    if pickup_paid_then_returned or pickup_returned_without_prior_payment:
        return _run_pickup_return_mode(
            apply=apply,
            order_numbers=order_numbers,
            recover_pending=recover_pending,
            client=client,
            paid_before_return=pickup_paid_then_returned,
        )
    if full_refunds:
        return _run_full_refunds(
            apply=apply,
            batch_number=batch_number,
            order_numbers=order_numbers,
            recover_pending=recover_pending,
            client=client,
        )
    if recover_pending:
        return _recover_pending(
            client=client,
            event_type=PARTIAL_RETURN_EVENT_TYPE,
            target_stage=PARTIAL_RETURN_TARGET_STAGE,
            resolved_reason=PARTIAL_RETURN_REASON,
        )
    all_candidates = _header_candidates()
    candidates, batch_offset = _select_candidates(
        all_candidates,
        batch_number=batch_number,
        order_numbers=order_numbers,
        candidate_kind="partial-return",
    )
    if apply and len(candidates) > BATCH_SIZE and order_numbers:
        raise SystemExit(f"partial-return explicit apply is capped at {BATCH_SIZE} orders")
    totals: Counter[str] = Counter()
    all_results: list[stage_outbox.StageOutboxResult] = []
    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start : start + BATCH_SIZE]
        evidence = _line_evidence([item.order_number for item in batch])
        ready, blocked = _ready_batch(batch, evidence, client=client)
        totals.update(blocked)
        totals["ready"] += len(ready)
        batch_results: list[stage_outbox.StageOutboxResult] = []
        if apply and ready:
            outbox_ids = _enqueue(ready)
            batch_results = _apply_outbox(
                outbox_ids,
                client=client,
                target_stage=PARTIAL_RETURN_TARGET_STAGE,
            )
            _finalize_applied(
                batch_results,
                target_stage=PARTIAL_RETURN_TARGET_STAGE,
                resolved_reason=PARTIAL_RETURN_REASON,
            )
            all_results.extend(batch_results)
        print(
            json.dumps(
                {
                    "batch": batch_offset + start // BATCH_SIZE + 1,
                    "scanned": len(batch),
                    "ready": len(ready),
                    "blocked": dict(blocked),
                    "applied": sum(1 for item in batch_results if item.applied),
                    "results": Counter(item.result for item in batch_results),
                },
                ensure_ascii=False,
                default=dict,
            ),
            flush=True,
        )
    print(
        json.dumps(
            {
                "mode": "apply" if apply else "dry-run",
                "header_candidates": len(all_candidates),
                "scanned_candidates": len(candidates),
                "totals": dict(totals),
                "applied": sum(1 for item in all_results if item.applied),
                "result_counts": dict(Counter(item.result for item in all_results)),
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch", type=int, default=None, help="Run one 1-based batch only")
    parser.add_argument("--recover-pending", action="store_true")
    parser.add_argument(
        "--full-refunds",
        action="store_true",
        help="Analyze/apply exact full goods and money refunds to LOSE",
    )
    parser.add_argument(
        "--pickup-paid-then-returned",
        action="store_true",
        help="Apply explicit pickup orders paid after RTU and before a customer return to WON",
    )
    parser.add_argument(
        "--pickup-returned-without-prior-payment",
        action="store_true",
        help="Apply explicit pickups returned before payment to DISMANTLING",
    )
    parser.add_argument(
        "--pickup-issued-then-returned",
        action="store_true",
        help="Apply explicit historical pickups with at least one qualifying issued RTU to WON",
    )
    parser.add_argument(
        "--stale-execution",
        action="store_true",
        help=(
            "Reconcile explicit stale 1C execution decisions with a compatible "
            "later pickup chat event"
        ),
    )
    parser.add_argument(
        "--orders",
        default=None,
        help="Comma-separated stable order selection",
    )
    args = parser.parse_args()
    order_numbers = (
        [value.strip() for value in args.orders.split(",") if value.strip()]
        if args.orders
        else None
    )
    return run(
        apply=args.apply,
        batch_number=args.batch,
        recover_pending=args.recover_pending,
        full_refunds=args.full_refunds,
        pickup_paid_then_returned=args.pickup_paid_then_returned,
        pickup_returned_without_prior_payment=args.pickup_returned_without_prior_payment,
        pickup_issued_then_returned=args.pickup_issued_then_returned,
        stale_execution=args.stale_execution,
        order_numbers=order_numbers,
    )


if __name__ == "__main__":
    raise SystemExit(main())
