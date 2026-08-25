from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session, aliased

from app.core.config import Settings
from app.models.site_order_fulfillment import (
    PickupInventoryItem,
    PickupInventorySubmission,
    SiteOrderExecutionCase,
)
from app.services import pickup_inventory
from app.services import site_order_fulfillment as fulfillment
from app.services import site_order_fulfillment_bot as bot

QUEUE_WON = "won_candidate"
QUEUE_PRESENT = "pickup_waiting"
QUEUE_LOSE = "lose_candidate"
QUEUE_MANUAL = "manual_review"
MAX_APPROVED_BATCH_SIZE = 20


@dataclass(frozen=True, slots=True)
class HistoricalPickupAssessment:
    site_order_number: str
    bitrix_deal_id: int | None
    current_stage: str | None
    queue: str
    target_stage: str | None
    reason: str
    warehouse_ids: tuple[int, ...] = ()
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def assess_historical_pickup_cases(
    session: Session,
    *,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], bot.OneCPickupValidation],
    limit: int = 500,
) -> list[HistoricalPickupAssessment]:
    bounded_limit = max(1, min(limit, 5000))
    cases = list(
        session.scalars(
            select(SiteOrderExecutionCase)
            .order_by(SiteOrderExecutionCase.updated_at.asc(), SiteOrderExecutionCase.id.asc())
            .limit(bounded_limit)
        ).all()
    )
    case_by_order = {case.site_order_number: case for case in cases}
    live_deals_by_order: dict[str, list[fulfillment.BitrixDealSnapshot]] = {}
    list_by_stages = getattr(client, "list_deals_by_stages", None)
    if callable(list_by_stages):
        try:
            live_deals = list_by_stages(
                {
                    "EXECUTING",
                    "FINAL_INVOICE",
                    fulfillment.CRM_STAGE_PICKUP_WAITING,
                    "DISMANTLING",
                    "WON",
                    "LOSE",
                },
                limit=bounded_limit,
            )
        except Exception:
            live_deals = []
        for deal in live_deals:
            order_number = fulfillment._clean_string(  # noqa: SLF001
                (deal.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
            )
            if not order_number:
                continue
            live_deals_by_order.setdefault(order_number, []).append(deal)
            if order_number not in case_by_order and len(case_by_order) < bounded_limit:
                placeholder = _historical_placeholder_case(order_number, deal=deal)
                cases.append(placeholder)
                case_by_order[order_number] = placeholder
    inventory_orders = session.scalars(
        select(PickupInventoryItem.site_order_number)
        .distinct()
        .order_by(PickupInventoryItem.site_order_number.asc())
        .limit(bounded_limit)
    ).all()
    for order_number in inventory_orders:
        if order_number not in case_by_order and len(case_by_order) < bounded_limit:
            deal_hint = (live_deals_by_order.get(order_number) or [None])[0]
            placeholder = _historical_placeholder_case(order_number, deal=deal_hint)
            cases.append(placeholder)
            case_by_order[order_number] = placeholder
    return [
        _assess_case(
            session,
            case=case,
            client=client,
            settings=settings,
            onec_validator=onec_validator,
            deal_hint=live_deals_by_order.get(case.site_order_number),
        )
        for case in cases
        if not bot._case_is_after_cutover(case, settings=settings)  # noqa: SLF001
    ]


def approved_batch_id(rows: list[HistoricalPickupAssessment]) -> str:
    canonical = [
        {
            "order": row.site_order_number,
            "deal": row.bitrix_deal_id,
            "before": row.current_stage,
            "target": row.target_stage,
            "queue": row.queue,
            "reason": row.reason,
            "warehouses": list(row.warehouse_ids),
            "evidence": list(row.evidence),
        }
        for row in rows
    ]
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return f"pickup-history-{digest}"


def reassess_historical_order(
    session: Session,
    *,
    site_order_number: str,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], bot.OneCPickupValidation],
) -> HistoricalPickupAssessment:
    case = session.scalar(
        select(SiteOrderExecutionCase).where(
            SiteOrderExecutionCase.site_order_number == site_order_number
        )
    )
    if case is None:
        try:
            deals = client.list_deals_by_site_order(site_order_number)
        except Exception:
            deals = []
        deal = deals[0] if len(deals) == 1 else None
        case = _historical_placeholder_case(site_order_number, deal=deal)
        return _assess_case(
            session,
            case=case,
            client=client,
            settings=settings,
            onec_validator=onec_validator,
            deal_hint=deals,
        )
    return _assess_case(
        session,
        case=case,
        client=client,
        settings=settings,
        onec_validator=onec_validator,
    )


def enqueue_approved_batch(
    session: Session,
    *,
    rows: list[HistoricalPickupAssessment],
    approved_id: str,
    settings: Settings,
    now: datetime | None = None,
) -> dict[str, int | str]:
    if not rows:
        raise ValueError("historical_batch_empty")
    if len(rows) > MAX_APPROVED_BATCH_SIZE:
        raise ValueError("historical_batch_too_large")
    expected_id = approved_batch_id(rows)
    if approved_id != expected_id:
        raise ValueError("historical_batch_approval_mismatch")
    if not settings.order_fulfillment_pickup_stage_apply_enabled:
        raise ValueError("pickup_stage_apply_disabled")
    queued = 0
    skipped = 0
    now = bot._naive_utc(now or bot.utcnow())  # noqa: SLF001
    for row in rows:
        if (
            row.queue not in {QUEUE_WON, QUEUE_PRESENT, QUEUE_LOSE}
            or row.target_stage is None
            or row.bitrix_deal_id is None
        ):
            skipped += 1
            continue
        event_type = {
            QUEUE_WON: fulfillment.EVENT_PICKUP_RECEIVED,
            QUEUE_PRESENT: fulfillment.EVENT_PICKUP_STORED,
            QUEUE_LOSE: fulfillment.EVENT_PICKUP_DISMANTLED,
        }[row.queue]
        source_key = f"historical:{expected_id}:{row.site_order_number}:{row.target_stage}"
        stage_row = bot.enqueue_outbox(
            session,
            operation=bot.OP_UPDATE_CRM_STAGE,
            idempotency_key=f"{source_key}:crm-stage",
            target_type="deal",
            target_id=str(row.bitrix_deal_id),
            payload={
                "site_order_number": row.site_order_number,
                "before_stage": row.current_stage,
                "target_stage": row.target_stage,
                "feature_guard": "historical_reconciliation",
                "historical_queue": row.queue,
                "historical_reason": row.reason,
                "historical_warehouse_ids": list(row.warehouse_ids),
            },
            now=now,
        )
        event_row = bot.enqueue_outbox(
            session,
            depends_on=stage_row,
            operation=bot.OP_FINALIZE_CASE_EVENT,
            idempotency_key=f"{source_key}:event",
            target_type="deal",
            target_id=str(row.bitrix_deal_id),
            payload={
                "site_order_number": row.site_order_number,
                "event_type": event_type,
                "event_at": now.isoformat(),
                "source": "historical_reconciliation",
                "source_ref": source_key,
                "confidence": "strong",
                "warehouse_id": row.warehouse_ids[0] if len(row.warehouse_ids) == 1 else None,
                "evidence": {
                    "batch_id": expected_id,
                    "reason": row.reason,
                    "evidence": list(row.evidence),
                },
            },
            now=now,
        )
        bot.enqueue_outbox(
            session,
            depends_on=event_row,
            operation=bot.OP_UPDATE_CRM_FIELDS,
            idempotency_key=f"{source_key}:crm-fields",
            target_type="deal",
            target_id=str(row.bitrix_deal_id),
            payload={
                "site_order_number": row.site_order_number,
                "fields": {
                    bot.CRM_PICKUP_DERIVED_STATUS_FIELD: event_type,
                    bot.CRM_PICKUP_LAST_EVIDENCE_FIELD: row.reason,
                },
            },
            now=now,
        )
        queued += 1
    session.commit()
    return {"batch_id": expected_id, "queued": queued, "skipped": skipped}


def _assess_case(
    session: Session,
    *,
    case: SiteOrderExecutionCase,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], bot.OneCPickupValidation],
    deal_hint: list[fulfillment.BitrixDealSnapshot] | None = None,
) -> HistoricalPickupAssessment:
    del settings  # cutover filtering is performed by the caller.
    if deal_hint is not None:
        deals = deal_hint
    else:
        try:
            deals = client.list_deals_by_site_order(case.site_order_number)
        except Exception:
            return _manual(case, "crm_readback_failed")
    if len(deals) != 1:
        return _manual(case, "deal_not_unique")
    deal = deals[0]
    if case.bitrix_deal_id is not None and deal.deal_id != case.bitrix_deal_id:
        return _manual(case, "deal_changed")
    if not fulfillment._is_internal_pickup_deal(deal):  # noqa: SLF001
        return _manual(case, "delivery_mismatch", deal_id=deal.deal_id, stage=deal.stage_id)
    onec = onec_validator(case.site_order_number)
    if not onec.available:
        return _manual(case, "onec_unavailable", deal_id=deal.deal_id, stage=deal.stage_id)

    current_warehouses = _current_inventory_warehouse_ids(
        session,
        site_order_number=case.site_order_number,
    )
    disappearance = _latest_uncontested_disappearance(
        session,
        case=case,
    )
    evidence = tuple(
        value
        for value, present in (
            ("onec_issued", onec.issued_confirmed),
            ("onec_return", onec.return_confirmed),
            ("onec_payment", onec.payment_confirmed),
            ("confirmed_inventory_disappearance", disappearance is not None),
            ("current_inventory", bool(current_warehouses)),
        )
        if present
    )

    if len(current_warehouses) > 1:
        return _manual(
            case,
            "present_at_multiple_points",
            deal_id=deal.deal_id,
            stage=deal.stage_id,
            warehouses=current_warehouses,
            evidence=evidence,
        )
    if current_warehouses and (
        onec.issued_confirmed or onec.return_confirmed or disappearance is not None
    ):
        return _manual(
            case,
            "current_inventory_conflicts_with_closure",
            deal_id=deal.deal_id,
            stage=deal.stage_id,
            warehouses=current_warehouses,
            evidence=evidence,
        )
    if onec.issued_confirmed and onec.return_confirmed:
        return _manual(
            case,
            "onec_issue_return_conflict",
            deal_id=deal.deal_id,
            stage=deal.stage_id,
            evidence=evidence,
        )
    if current_warehouses:
        warehouse_id = current_warehouses[0]
        if case.pickup_point_warehouse_id not in {None, warehouse_id}:
            return _manual(
                case,
                "current_inventory_other_point",
                deal_id=deal.deal_id,
                stage=deal.stage_id,
                warehouses=current_warehouses,
                evidence=evidence,
            )
        if deal.stage_id not in {
            "EXECUTING",
            "FINAL_INVOICE",
            fulfillment.CRM_STAGE_PICKUP_WAITING,
        }:
            return _manual(
                case,
                "pickup_waiting_stage_not_allowed",
                deal_id=deal.deal_id,
                stage=deal.stage_id,
                warehouses=current_warehouses,
                evidence=evidence,
            )
        return HistoricalPickupAssessment(
            site_order_number=case.site_order_number,
            bitrix_deal_id=deal.deal_id,
            current_stage=deal.stage_id,
            queue=QUEUE_PRESENT,
            target_stage=fulfillment.CRM_STAGE_PICKUP_WAITING,
            reason="confirmed_current_inventory",
            warehouse_ids=current_warehouses,
            evidence=evidence,
        )
    if onec.return_confirmed:
        if onec.payment_confirmed or onec.issued_confirmed or onec.debt_conflict:
            return _manual(
                case,
                "return_payment_conflict",
                deal_id=deal.deal_id,
                stage=deal.stage_id,
                evidence=evidence,
            )
        if deal.stage_id not in {fulfillment.CRM_STAGE_PICKUP_WAITING, "DISMANTLING", "LOSE"}:
            return _manual(
                case,
                "lose_stage_not_allowed",
                deal_id=deal.deal_id,
                stage=deal.stage_id,
                evidence=evidence,
            )
        return HistoricalPickupAssessment(
            site_order_number=case.site_order_number,
            bitrix_deal_id=deal.deal_id,
            current_stage=deal.stage_id,
            queue=QUEUE_LOSE,
            target_stage="LOSE",
            reason="confirmed_onec_return_without_payment",
            evidence=evidence,
        )
    if onec.issued_confirmed or (onec.assembled and disappearance is not None):
        if deal.stage_id not in {fulfillment.CRM_STAGE_PICKUP_WAITING, "WON"}:
            return _manual(
                case,
                "won_stage_not_allowed",
                deal_id=deal.deal_id,
                stage=deal.stage_id,
                evidence=evidence,
            )
        return HistoricalPickupAssessment(
            site_order_number=case.site_order_number,
            bitrix_deal_id=deal.deal_id,
            current_stage=deal.stage_id,
            queue=QUEUE_WON,
            target_stage="WON",
            reason=(
                "confirmed_onec_issue"
                if onec.issued_confirmed
                else "confirmed_inventory_disappearance_with_rtu"
            ),
            warehouse_ids=(disappearance.warehouse_id,) if disappearance is not None else (),
            evidence=evidence,
        )
    return _manual(
        case,
        "lost_or_insufficient_evidence",
        deal_id=deal.deal_id,
        stage=deal.stage_id,
        evidence=evidence,
    )


def _historical_placeholder_case(
    site_order_number: str,
    *,
    deal: fulfillment.BitrixDealSnapshot | None,
) -> SiteOrderExecutionCase:
    return SiteOrderExecutionCase(
        site_order_number=site_order_number,
        bitrix_deal_id=deal.deal_id if deal is not None else None,
        delivery_method=deal.delivery if deal is not None else None,
        current_derived_status="manual_review",
        current_crm_stage=deal.stage_id if deal is not None else None,
        confidence="weak",
        payload={"historical_placeholder": True},
    )


def _manual(
    case: SiteOrderExecutionCase,
    reason: str,
    *,
    deal_id: int | None = None,
    stage: str | None = None,
    warehouses: tuple[int, ...] = (),
    evidence: tuple[str, ...] = (),
) -> HistoricalPickupAssessment:
    return HistoricalPickupAssessment(
        site_order_number=case.site_order_number,
        bitrix_deal_id=deal_id if deal_id is not None else case.bitrix_deal_id,
        current_stage=stage if stage is not None else case.current_crm_stage,
        queue=QUEUE_MANUAL,
        target_stage=None,
        reason=reason,
        warehouse_ids=warehouses,
        evidence=evidence,
    )


def _current_inventory_warehouse_ids(
    session: Session,
    *,
    site_order_number: str,
) -> tuple[int, ...]:
    newer = aliased(PickupInventorySubmission)
    rows = session.scalars(
        select(PickupInventorySubmission.warehouse_id)
        .join(
            PickupInventoryItem,
            PickupInventoryItem.submission_id == PickupInventorySubmission.id,
        )
        .where(
            PickupInventorySubmission.status == pickup_inventory.STATUS_CONFIRMED,
            PickupInventorySubmission.warehouse_id.is_not(None),
            PickupInventoryItem.site_order_number == site_order_number,
            ~exists(
                select(newer.id).where(
                    newer.warehouse_id == PickupInventorySubmission.warehouse_id,
                    newer.status == pickup_inventory.STATUS_CONFIRMED,
                    or_(
                        newer.submitted_at > PickupInventorySubmission.submitted_at,
                        and_(
                            newer.submitted_at == PickupInventorySubmission.submitted_at,
                            newer.id > PickupInventorySubmission.id,
                        ),
                    ),
                )
            ),
        )
        .distinct()
    ).all()
    return tuple(sorted(int(value) for value in rows if value is not None))


def _latest_uncontested_disappearance(
    session: Session,
    *,
    case: SiteOrderExecutionCase,
) -> pickup_inventory.InventoryDisappearance | None:
    submissions = session.scalars(
        select(PickupInventorySubmission)
        .where(PickupInventorySubmission.status == pickup_inventory.STATUS_CONFIRMED)
        .order_by(
            PickupInventorySubmission.submitted_at.desc(),
            PickupInventorySubmission.id.desc(),
        )
    ).all()
    for submission in submissions:
        for candidate in pickup_inventory.disappearance_candidates(
            session,
            current_submission=submission,
        ):
            if candidate.site_order_number != case.site_order_number:
                continue
            if case.pickup_point_warehouse_id not in {None, candidate.warehouse_id}:
                continue
            uncontested, _ = pickup_inventory.disappearance_is_uncontested(
                session,
                candidate=candidate,
            )
            if uncontested:
                return candidate
    return None
