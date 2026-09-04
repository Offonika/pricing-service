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
    onec_order_number: str | None = None
    raw_delivery: str | None = None
    duplicate_deal_ids: tuple[int, ...] = ()
    crm_assembled: bool = False
    crm_payment_confirmed: bool = False
    site_canceled: bool | None = None
    site_status: str | None = None
    site_paid: bool | None = None
    onec_order_count: int = 0
    onec_inactive_marked_order_count: int = 0
    rtu_count: int = 0
    assembled_rtu_count: int = 0
    issued_rtu_count: int = 0
    returned_rtu_count: int = 0
    posted_sale_amount: Decimal | None = None
    returned_amount: Decimal | None = None
    payment_amount: Decimal | None = None
    debt_amount: Decimal | None = None
    line_coverage_status: str | None = None
    expected_item_quantity: Decimal | None = None
    assembled_item_quantity: Decimal | None = None
    missing_item_count: int = 0
    excess_item_count: int = 0
    onec_payment_confirmed: bool = False
    latest_rtu_at: datetime | None = None
    latest_assembled_at: datetime | None = None
    latest_issued_at: datetime | None = None
    latest_return_at: datetime | None = None
    dismantling_started_at: datetime | None = None
    dismantling_started_source: str | None = None
    onec_evidence_available: bool = True
    historical: bool = False

    @property
    def payment_confirmed(self) -> bool:
        return bool(
            self.crm_payment_confirmed or self.onec_payment_confirmed or self.site_paid is True
        )

    @property
    def assembled(self) -> bool:
        if self.line_coverage_status:
            return self.line_coverage_status == "complete" and bool(
                self.crm_assembled or self.assembled_rtu_count > 0
            )
        return self.crm_assembled or self.assembled_rtu_count > 0

    @property
    def onec_order_inactive_marked(self) -> bool:
        return self.onec_order_count == 1 and self.onec_inactive_marked_order_count == 1

    @property
    def partial_rtu_assembly(self) -> bool:
        if self.line_coverage_status in {"partial", "conflict"}:
            return True
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
    if stage not in {"EXECUTING", "DISMANTLING"}:
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
    if stage == "DISMANTLING":
        return _decide_dismantling_outcome(snapshot, delivery_class=delivery_class)

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

    if snapshot.site_canceled and (
        not snapshot.payment_confirmed
        and not snapshot.assembled
        and snapshot.rtu_count == 0
        and snapshot.onec_order_inactive_marked
    ):
        return _update(
            "LOSE",
            "canceled_before_fulfillment",
            "execution_canceled_before_fulfillment",
        )

    if snapshot.site_canceled:
        return _manual("canceled_without_confirmed_return", "execution_canceled_unresolved")

    if snapshot.issued_rtu_count > 0 or snapshot.assembled_rtu_count > 0 or snapshot.crm_assembled:
        coverage_decision = _validate_fulfillment_coverage(snapshot)
        if coverage_decision is not None:
            return coverage_decision

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


def _decide_dismantling_outcome(
    snapshot: ExecutionEvidenceSnapshot,
    *,
    delivery_class: str,
) -> ExecutionDecision:
    started_at = snapshot.dismantling_started_at
    if started_at is None:
        return _manual(
            "dismantling_start_time_unconfirmed",
            "execution_dismantling_time_missing",
            "weak",
        )

    if snapshot.has_return:
        if not snapshot.full_return:
            return _manual(
                "dismantling_partial_or_unquantified_return",
                "execution_dismantling_partial_return",
            )
        if snapshot.latest_return_at is None:
            return _manual(
                "dismantling_return_chronology_missing",
                "execution_dismantling_return_time_missing",
            )
        if not _is_after(snapshot.latest_return_at, started_at):
            return _manual(
                "dismantling_return_precedes_start",
                "execution_dismantling_return_chronology_conflict",
            )
        if snapshot.issued_rtu_count > 0 and snapshot.latest_issued_at is None:
            return _manual(
                "dismantling_issue_return_chronology_missing",
                "execution_dismantling_issue_return_time_missing",
            )
        if snapshot.latest_issued_at is not None and not _is_after(
            snapshot.latest_return_at,
            snapshot.latest_issued_at,
        ):
            return _manual(
                "dismantling_return_not_after_issue",
                "execution_dismantling_issue_return_conflict",
            )
        return _update(
            "LOSE",
            "dismantling_full_return_confirmed",
            "execution_dismantling_full_return",
        )

    if snapshot.site_canceled:
        if (
            snapshot.payment_confirmed
            or snapshot.assembled
            or snapshot.rtu_count > 0
            or not snapshot.onec_order_inactive_marked
        ):
            return _manual(
                "dismantling_canceled_with_fulfillment_evidence",
                "execution_dismantling_cancel_conflict",
            )
        return _update(
            "LOSE",
            "dismantling_canceled_before_fulfillment",
            "execution_dismantling_canceled",
        )

    if snapshot.issued_rtu_count > 0:
        coverage_decision = _validate_fulfillment_coverage(
            snapshot,
            partial_is_review=True,
        )
        if coverage_decision is not None:
            return coverage_decision
        if snapshot.partial_rtu_issue:
            return _manual(
                "dismantling_partial_rtu_issue",
                "execution_dismantling_partial_issue",
            )
        if delivery_class != fulfillment.DELIVERY_CLASS_PICKUP:
            return _manual(
                "dismantling_issue_not_internal_pickup",
                "execution_dismantling_non_pickup_issue",
            )
        if snapshot.rtu_count <= 0 or snapshot.issued_rtu_count != snapshot.rtu_count:
            return _manual(
                "dismantling_issue_not_full",
                "execution_dismantling_issue_incomplete",
            )
        if snapshot.line_coverage_status != "complete":
            return _manual(
                "dismantling_item_coverage_unconfirmed",
                "execution_dismantling_coverage_missing",
                "weak",
            )
        if snapshot.latest_issued_at is None:
            return _manual(
                "dismantling_issue_chronology_missing",
                "execution_dismantling_issue_time_missing",
            )
        if not _is_after(snapshot.latest_issued_at, started_at):
            return _manual(
                "dismantling_issue_precedes_start",
                "execution_dismantling_issue_chronology_conflict",
            )
        return _update(
            "WON",
            "dismantling_full_pickup_issued",
            "execution_dismantling_pickup_issued",
        )

    if snapshot.rtu_count > 0 and not snapshot.assembled:
        return _manual(
            "dismantling_rtu_without_assembly",
            "execution_dismantling_rtu_without_assembly",
        )
    return _noop(
        "dismantling_waiting_for_outcome_evidence",
        "execution_dismantling_waiting_evidence",
        "medium",
    )


def _validate_fulfillment_coverage(
    snapshot: ExecutionEvidenceSnapshot,
    *,
    partial_is_review: bool = False,
) -> ExecutionDecision | None:
    if snapshot.line_coverage_status == "complete":
        return None
    if snapshot.line_coverage_status == "conflict" or snapshot.excess_item_count > 0:
        return _manual(
            "assembly_line_quantity_conflict",
            "execution_assembly_quantity_conflict",
        )
    if snapshot.line_coverage_status == "partial" or snapshot.missing_item_count > 0:
        if partial_is_review:
            return _manual(
                "dismantling_partial_item_fulfillment",
                "execution_dismantling_partial_fulfillment",
            )
        return _noop(
            "waiting_for_full_order_item_assembly",
            "execution_waiting_for_full_item_assembly",
            "medium",
        )
    return _manual(
        "assembly_line_coverage_unavailable",
        "execution_assembly_coverage_unavailable",
        "weak",
    )


def _is_after(value: datetime, reference: datetime) -> bool:
    left = value.replace(tzinfo=None) if value.tzinfo is not None else value
    right = reference.replace(tzinfo=None) if reference.tzinfo is not None else reference
    return left > right


def snapshot_fingerprint(snapshot: ExecutionEvidenceSnapshot) -> str:
    payload = _json_ready(asdict(snapshot))
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def dismantling_started_at_by_order(
    session: Session,
    order_numbers: list[str],
) -> dict[str, tuple[datetime, str]]:
    normalized = [item.strip() for item in dict.fromkeys(order_numbers) if item.strip()]
    if not normalized:
        return {}
    rows = session.execute(
        select(
            SiteOrderExecutionCase.site_order_number,
            SiteOrderExecutionEvent.event_at,
            SiteOrderExecutionEvent.created_at,
        )
        .join(
            SiteOrderExecutionEvent,
            SiteOrderExecutionEvent.case_id == SiteOrderExecutionCase.id,
        )
        .where(
            SiteOrderExecutionCase.site_order_number.in_(normalized),
            SiteOrderExecutionEvent.event_type == fulfillment.EVENT_PICKUP_DISMANTLING,
            SiteOrderExecutionEvent.confidence == "strong",
        )
    ).all()
    result: dict[str, tuple[datetime, str]] = {}
    for order_number, event_at, created_at in rows:
        observed_at = event_at or created_at
        if not isinstance(observed_at, datetime):
            continue
        current = result.get(order_number)
        if current is None or _is_after(observed_at, current[0]):
            result[order_number] = (observed_at, "service_event")
    return result


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
    if snapshot.onec_order_number:
        case.onec_order_external_id = snapshot.onec_order_number
    case.current_crm_stage = snapshot.current_stage
    case.raw_delivery_method = snapshot.raw_delivery
    case.delivery_method = snapshot.delivery_class
    case.payment_status = "paid" if snapshot.payment_confirmed else "unconfirmed"
    projection = {
        "payment_amount": _json_ready(snapshot.payment_amount),
        "debt_amount": _json_ready(snapshot.debt_amount),
        "site_status": snapshot.site_status,
        "site_canceled": snapshot.site_canceled,
        "site_paid": snapshot.site_paid,
        "observed_at": datetime.now().isoformat(),
    }
    case.payload = {
        **(case.payload if isinstance(case.payload, dict) else {}),
        "execution_reconciliation": payload,
        "state_projection": projection,
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
