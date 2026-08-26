from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    LogisticsManualReview,
    SiteOrderExecutionCase,
    SiteOrderExecutionEvent,
    SiteOrderStageOutbox,
)
from app.services import site_order_fulfillment as fulfillment

ACTION_UPDATE_STAGE = "update_stage"
ACTION_MANUAL_REVIEW = "manual_review"
ACTION_NOOP = "noop"

SOURCE_ONEC = "onec"
EXECUTION_EVENT_PREFIX = "execution_"
EXECUTION_PIPELINE = "execution_reconciliation"

MONEY_TOLERANCE = Decimal("0.05")


@dataclass(frozen=True, slots=True)
class ExecutionEvidenceSnapshot:
    site_order_number: str
    bitrix_deal_id: int
    current_stage: str | None
    delivery_class: str | None
    raw_delivery: str | None = None
    duplicate_deal_ids: tuple[int, ...] = ()
    crm_assembled: bool = False
    crm_payment_confirmed: bool = False
    site_canceled: bool | None = None
    site_status: str | None = None
    site_paid: bool | None = None
    rtu_count: int = 0
    assembled_rtu_count: int = 0
    issued_rtu_count: int = 0
    returned_rtu_count: int = 0
    posted_sale_amount: Decimal | None = None
    returned_amount: Decimal | None = None
    onec_payment_confirmed: bool = False
    latest_rtu_at: datetime | None = None
    latest_assembled_at: datetime | None = None
    latest_issued_at: datetime | None = None
    latest_return_at: datetime | None = None
    onec_evidence_available: bool = True
    historical: bool = False

    @property
    def payment_confirmed(self) -> bool:
        return bool(
            self.crm_payment_confirmed or self.onec_payment_confirmed or self.site_paid is True
        )

    @property
    def assembled(self) -> bool:
        return self.crm_assembled or self.assembled_rtu_count > 0

    @property
    def partial_rtu_assembly(self) -> bool:
        return self.rtu_count > 0 and 0 < self.assembled_rtu_count < self.rtu_count

    @property
    def partial_rtu_issue(self) -> bool:
        return self.rtu_count > 0 and 0 < self.issued_rtu_count < self.rtu_count

    @property
    def has_return(self) -> bool:
        return self.returned_rtu_count > 0 or bool(
            self.returned_amount is not None and self.returned_amount > MONEY_TOLERANCE
        )

    @property
    def full_return(self) -> bool:
        if not self.has_return:
            return False
        if self.posted_sale_amount is None or self.posted_sale_amount <= MONEY_TOLERANCE:
            return False
        if self.returned_amount is None:
            return False
        return self.returned_amount >= self.posted_sale_amount - MONEY_TOLERANCE


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    action: str
    reason: str
    event_type: str
    target_stage: str | None = None
    confidence: str = "strong"


@dataclass(frozen=True, slots=True)
class PersistExecutionDecisionResult:
    site_order_number: str
    event_id: int | None
    outbox_id: int | None
    result: str


def decide_execution_stage(snapshot: ExecutionEvidenceSnapshot) -> ExecutionDecision:
    order_number = snapshot.site_order_number.strip()
    stage = (snapshot.current_stage or "").strip().upper()
    delivery_class = (snapshot.delivery_class or "").strip().lower()
    duplicate_ids = tuple(sorted(set(snapshot.duplicate_deal_ids)))

    if not order_number:
        return _manual("missing_order_number", "execution_missing_order", "weak")
    if (
        stage in fulfillment.TERMINAL_CRM_STAGES
        or stage.endswith(":WON")
        or stage.endswith(":LOSE")
    ):
        return _noop("terminal_crm_stage", "execution_terminal_stage")
    if stage != "EXECUTING":
        return _noop(f"outside_executing:{stage or '-'}", "execution_outside_stage")
    if len(duplicate_ids) > 1:
        return _manual("multiple_bitrix_deals", "execution_duplicate_deals")
    if delivery_class not in {
        fulfillment.DELIVERY_CLASS_PICKUP,
        fulfillment.DELIVERY_CLASS_COURIER,
        fulfillment.DELIVERY_CLASS_CARRIER,
    }:
        return _manual("delivery_method_unknown", "execution_delivery_conflict")
    if not snapshot.onec_evidence_available:
        return _manual("onec_evidence_unavailable", "execution_onec_unavailable", "weak")
    if any(
        count < 0 or count > snapshot.rtu_count
        for count in (
            snapshot.assembled_rtu_count,
            snapshot.issued_rtu_count,
            snapshot.returned_rtu_count,
        )
    ):
        return _manual("rtu_evidence_count_mismatch", "execution_rtu_count_conflict")

    if snapshot.has_return:
        if snapshot.issued_rtu_count > 0:
            return _manual("issued_and_returned", "execution_issued_return_conflict")
        if snapshot.payment_confirmed:
            return _manual("paid_and_returned", "execution_paid_return_conflict")
        if not snapshot.full_return:
            return _manual("partial_or_unquantified_return", "execution_partial_return")
        if snapshot.latest_return_at is None:
            return _manual("return_chronology_missing", "execution_return_time_missing")
        return _update("LOSE", "full_unpaid_return", "execution_full_return")

    if snapshot.site_canceled:
        return _manual("canceled_without_confirmed_return", "execution_canceled_unresolved")

    if snapshot.issued_rtu_count > 0:
        if snapshot.partial_rtu_issue:
            return _manual("partial_rtu_issue", "execution_partial_issue")
        if delivery_class != fulfillment.DELIVERY_CLASS_PICKUP:
            return _manual(
                "issued_rtu_not_pickup_handoff",
                "execution_non_pickup_issue_conflict",
            )
        return _update("WON", "pickup_printed_and_scanned", "execution_pickup_issued")

    if snapshot.assembled:
        if snapshot.partial_rtu_assembly:
            return _manual("partial_rtu_assembly", "execution_partial_assembly")
        if delivery_class == fulfillment.DELIVERY_CLASS_CARRIER and not snapshot.payment_confirmed:
            return _update(
                "PREPAYMENT_INVOICE",
                "carrier_assembled_payment_missing",
                "execution_carrier_waiting_payment",
            )
        return _update("FINAL_INVOICE", "assembled_without_return", "execution_assembled")

    if snapshot.rtu_count > 0:
        return _manual("rtu_without_assembled", "execution_rtu_without_assembled")
    return _noop("waiting_for_assembly_evidence", "execution_waiting_evidence", "medium")


def snapshot_fingerprint(snapshot: ExecutionEvidenceSnapshot) -> str:
    payload = _json_ready(asdict(snapshot))
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def persist_execution_decision(
    session: Session,
    *,
    snapshot: ExecutionEvidenceSnapshot,
    decision: ExecutionDecision,
) -> PersistExecutionDecisionResult:
    fingerprint = snapshot_fingerprint(snapshot)
    persisted_event_type = (
        f"execution_historical_{decision.event_type.removeprefix(EXECUTION_EVENT_PREFIX)}"
        if snapshot.historical
        else decision.event_type
    )
    source_ref = f"execution-snapshot:{snapshot.bitrix_deal_id}:{fingerprint}"
    payload = {
        "pipeline": EXECUTION_PIPELINE,
        "historical": snapshot.historical,
        "evidence_fingerprint": fingerprint,
        "decision": {
            "action": decision.action,
            "reason": decision.reason,
            "target_stage": decision.target_stage,
        },
        "snapshot": _json_ready(asdict(snapshot)),
    }
    evidence_at = max(
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
    event = fulfillment.upsert_execution_event(
        session,
        site_order_number=snapshot.site_order_number,
        event_type=persisted_event_type,
        event_at=evidence_at,
        source=SOURCE_ONEC,
        source_ref=source_ref,
        confidence=decision.confidence,
        raw_message_id=None,
        payload=payload,
    )
    case = session.scalar(
        select(SiteOrderExecutionCase).where(
            SiteOrderExecutionCase.site_order_number == snapshot.site_order_number
        )
    )
    if case is None:
        raise RuntimeError("execution_case_not_created")
    if event is None:
        return PersistExecutionDecisionResult(
            site_order_number=snapshot.site_order_number,
            event_id=None,
            outbox_id=None,
            result="duplicate_snapshot",
        )

    event_is_current = fulfillment._event_is_not_older_than_current(  # noqa: SLF001
        session,
        case=case,
        event=event,
    )
    if not event_is_current:
        return PersistExecutionDecisionResult(
            site_order_number=snapshot.site_order_number,
            event_id=event.id,
            outbox_id=None,
            result="stale_evidence",
        )

    if case.bitrix_deal_id in (None, snapshot.bitrix_deal_id):
        case.bitrix_deal_id = snapshot.bitrix_deal_id
    case.current_crm_stage = snapshot.current_stage
    case.raw_delivery_method = snapshot.raw_delivery
    case.delivery_method = snapshot.delivery_class
    case.payment_status = "paid" if snapshot.payment_confirmed else "unconfirmed"
    case.payload = {
        **(case.payload if isinstance(case.payload, dict) else {}),
        "execution_reconciliation": payload,
    }
    case.updated_at = datetime.now()
    case.current_derived_status = persisted_event_type
    case.confidence = decision.confidence
    case.last_evidence_event_id = event.id

    if decision.action != ACTION_MANUAL_REVIEW:
        for review in session.scalars(
            select(LogisticsManualReview).where(
                LogisticsManualReview.review_type == "site_order_execution_conflict",
                LogisticsManualReview.source_external_id == snapshot.site_order_number,
                LogisticsManualReview.status == "open",
            )
        ).all():
            review.status = "resolved"
            review.resolved_at = datetime.now()
            review.payload = {
                **(review.payload if isinstance(review.payload, dict) else {}),
                "resolved_by_event_id": event.id,
                "resolved_reason": "superseded_by_strict_evidence",
            }
            review.updated_at = datetime.now()

    if decision.action == ACTION_MANUAL_REVIEW:
        session.add(
            LogisticsManualReview(
                review_type="site_order_execution_conflict",
                source_document_type="site_order",
                source_external_id=snapshot.site_order_number,
                reason=decision.reason,
                payload={
                    "bitrix_deal_id": snapshot.bitrix_deal_id,
                    "event_id": event.id,
                    "evidence_fingerprint": fingerprint,
                },
            )
        )
        session.flush()

    if decision.action != ACTION_UPDATE_STAGE or decision.target_stage is None:
        return PersistExecutionDecisionResult(
            site_order_number=snapshot.site_order_number,
            event_id=event.id,
            outbox_id=None,
            result=decision.action,
        )

    outbox = SiteOrderStageOutbox(
        case_id=case.id,
        event_id=event.id,
        idempotency_key=(
            f"execution-stage|{snapshot.site_order_number}|{fingerprint}|{decision.target_stage}"
        ),
        site_order_number=snapshot.site_order_number,
        bitrix_deal_id=snapshot.bitrix_deal_id,
        source_event_type=persisted_event_type,
        target_stage=decision.target_stage,
        payload=payload,
    )
    session.add(outbox)
    session.flush()
    return PersistExecutionDecisionResult(
        site_order_number=snapshot.site_order_number,
        event_id=event.id,
        outbox_id=outbox.id,
        result="outbox_created",
    )


def execution_reconciliation_metrics(session: Session) -> dict[str, Any]:
    outbox_by_status = {
        str(status): int(count)
        for status, count in session.execute(
            select(SiteOrderStageOutbox.status, func.count(SiteOrderStageOutbox.id))
            .where(SiteOrderStageOutbox.source_event_type.like(f"{EXECUTION_EVENT_PREFIX}%"))
            .group_by(SiteOrderStageOutbox.status)
        ).all()
    }
    onec_event_count = int(
        session.scalar(
            select(func.count())
            .select_from(SiteOrderExecutionEvent)
            .where(SiteOrderExecutionEvent.source == SOURCE_ONEC)
        )
        or 0
    )
    latest_onec_event_at = session.scalar(
        select(func.max(SiteOrderExecutionEvent.created_at)).where(
            SiteOrderExecutionEvent.source == SOURCE_ONEC
        )
    )
    manual_review_count = int(
        session.scalar(
            select(func.count())
            .select_from(LogisticsManualReview)
            .where(
                LogisticsManualReview.review_type == "site_order_execution_conflict",
                LogisticsManualReview.status == "open",
            )
        )
        or 0
    )
    return {
        "execution_event_count": onec_event_count,
        "execution_latest_event_at": (
            latest_onec_event_at.isoformat() if isinstance(latest_onec_event_at, datetime) else None
        ),
        "execution_manual_review_count": manual_review_count,
        "execution_outbox_by_status": outbox_by_status,
        "execution_outbox_active": sum(
            outbox_by_status.get(status, 0) for status in ("pending", "retry")
        ),
    }


def _update(target_stage: str, reason: str, event_type: str) -> ExecutionDecision:
    return ExecutionDecision(
        action=ACTION_UPDATE_STAGE,
        target_stage=target_stage,
        reason=reason,
        event_type=event_type,
        confidence="strong",
    )


def _manual(reason: str, event_type: str, confidence: str = "strong") -> ExecutionDecision:
    return ExecutionDecision(
        action=ACTION_MANUAL_REVIEW,
        target_stage=None,
        reason=reason,
        event_type=event_type,
        confidence=confidence,
    )


def _noop(reason: str, event_type: str, confidence: str = "strong") -> ExecutionDecision:
    return ExecutionDecision(
        action=ACTION_NOOP,
        target_stage=None,
        reason=reason,
        event_type=event_type,
        confidence=confidence,
    )


def _json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    return value
