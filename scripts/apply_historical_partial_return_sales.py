#!/usr/bin/env python3
"""Safely close strict historical partial-sale and full-refund orders.

Dry-run is the default. ``--apply`` persists an execution event and uses the
existing stage outbox, including live Bitrix readback and timeline audit.
``--full-refunds`` requires a complete goods return and an exact linked money
refund before moving an order to ``LOSE``.
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
    LogisticsManualReview,
    SiteOrderExecutionCase,
    SiteOrderExecutionEvent,
    SiteOrderStageOutbox,
)
from app.services import pickup_history
from app.services import site_order_fulfillment as fulfillment
from app.services import site_order_stage_outbox as stage_outbox
from infra.cron import order_fulfillment_sync as sync

PARTIAL_RETURN_EVENT_TYPE = "execution_historical_partial_return_sale"
PARTIAL_RETURN_REASON = "partial_return_with_retained_goods"
PARTIAL_RETURN_TARGET_STAGE = "WON"
FULL_REFUND_EVENT_TYPE = "execution_historical_full_goods_money_refund"
FULL_REFUND_REASON = "full_goods_and_money_refund"
FULL_REFUND_TARGET_STAGE = "LOSE"
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
                SUM(CAST(line._Fld4982 AS decimal(18, 2))) AS sale_amount
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
                    "select": ["ID", "STAGE_ID", fulfillment.CRM_ORDER_NUMBER_FIELD],
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


def _select_candidates(
    all_candidates: list[Candidate],
    *,
    batch_number: int | None,
    order_numbers: list[str] | None,
) -> tuple[list[Candidate], int]:
    if batch_number is not None and order_numbers:
        raise SystemExit("--batch and --orders cannot be used together")
    if order_numbers:
        by_order = {item.order_number: item for item in all_candidates}
        missing = [value for value in order_numbers if value not in by_order]
        if missing:
            raise SystemExit(f"orders are not open full-return candidates: {', '.join(missing)}")
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


def run(
    *,
    apply: bool,
    batch_number: int | None = None,
    recover_pending: bool = False,
    full_refunds: bool = False,
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
    if full_refunds:
        return _run_full_refunds(
            apply=apply,
            batch_number=batch_number,
            order_numbers=order_numbers,
            recover_pending=recover_pending,
            client=client,
        )
    if order_numbers:
        raise SystemExit("--orders requires --full-refunds")
    if recover_pending:
        return _recover_pending(
            client=client,
            event_type=PARTIAL_RETURN_EVENT_TYPE,
            target_stage=PARTIAL_RETURN_TARGET_STAGE,
            resolved_reason=PARTIAL_RETURN_REASON,
        )
    all_candidates = _header_candidates()
    candidates = all_candidates
    batch_offset = 0
    if batch_number is not None:
        if batch_number < 1:
            raise SystemExit("batch number must be positive")
        batch_offset = batch_number - 1
        start = batch_offset * BATCH_SIZE
        candidates = all_candidates[start : start + BATCH_SIZE]
        if not candidates:
            raise SystemExit(f"batch {batch_number} is empty")
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
        "--orders",
        default=None,
        help="Comma-separated stable order selection; requires --full-refunds",
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
        order_numbers=order_numbers,
    )


if __name__ == "__main__":
    raise SystemExit(main())
