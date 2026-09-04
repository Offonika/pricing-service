from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SiteOrderExecutionCase
from app.services import site_order_fulfillment as fulfillment

ExecutionSignal = Literal["assembled", "issued"]
SiteCrmSignal = Literal["in_delivery", "delivered", "cdek_address_mismatch"]


@dataclass(frozen=True, slots=True)
class OnecExecutionFact:
    signal: ExecutionSignal
    event_at: datetime
    site_order_number: str
    onec_order_number: str | None = None
    rtu_external_id: str | None = None
    rtu_number: str | None = None
    rtu_date: datetime | None = None
    is_posted: bool = True
    document_amount: Decimal | None = None


@dataclass(frozen=True, slots=True)
class OnecExecutionIngestResult:
    event_id: int | None
    duplicate: bool
    source_ref: str


@dataclass(frozen=True, slots=True)
class SiteCrmSignalFact:
    signal: SiteCrmSignal
    event_at: datetime
    site_order_number: str
    bitrix_deal_id: int
    source_revision: str
    current_stage: str | None = None


def canonical_onec_execution_source_ref(fact: OnecExecutionFact) -> str:
    """Build the same stable fact identity for direct and reconciler delivery."""

    document_key = _clean(fact.rtu_number) or _clean(fact.rtu_external_id)
    return "|".join(
        (
            "onec-execution-v1",
            fact.signal,
            _clean(fact.site_order_number),
            document_key or "-",
            _naive(fact.event_at).isoformat(timespec="seconds"),
        )
    )


def ingest_onec_execution_fact(
    session: Session,
    fact: OnecExecutionFact,
) -> OnecExecutionIngestResult:
    site_order_number = _clean(fact.site_order_number)
    if not site_order_number:
        raise ValueError("site_order_number is required")
    if fact.signal not in {"assembled", "issued"}:
        raise ValueError("unsupported execution signal")

    source_ref = canonical_onec_execution_source_ref(fact)
    event_type = (
        "execution_pickup_issued_raw" if fact.signal == "issued" else "execution_assembled_raw"
    )
    persisted = fulfillment.upsert_execution_event(
        session,
        site_order_number=site_order_number,
        event_type=event_type,
        event_at=fact.event_at,
        source="onec",
        source_ref=source_ref,
        confidence="strong",
        raw_message_id=None,
        payload={
            "pipeline": "execution_reconciliation",
            "rtu_external_id": _clean(fact.rtu_external_id) or None,
            "rtu_number": _clean(fact.rtu_number) or None,
            "rtu_date": fact.rtu_date.isoformat() if fact.rtu_date is not None else None,
            "onec_order_number": _clean(fact.onec_order_number) or None,
            "is_posted": fact.is_posted,
            "document_amount": (
                format(fact.document_amount, "f") if fact.document_amount is not None else None
            ),
        },
    )
    case = session.scalar(
        select(SiteOrderExecutionCase).where(
            SiteOrderExecutionCase.site_order_number == site_order_number
        )
    )
    if case is None:
        raise RuntimeError("execution_case_not_created")
    if fact.onec_order_number:
        case.onec_order_external_id = _clean(fact.onec_order_number) or None
    if fact.rtu_external_id:
        case.rtu_external_id = _clean(fact.rtu_external_id) or None
    case.updated_at = datetime.now()
    session.flush()
    return OnecExecutionIngestResult(
        event_id=persisted.id if persisted is not None else None,
        duplicate=persisted is None,
        source_ref=source_ref,
    )


def canonical_site_crm_signal_source_ref(fact: SiteCrmSignalFact) -> str:
    return "|".join(
        (
            "site-crm-signal-v1",
            fact.signal,
            str(fact.bitrix_deal_id),
            _clean(fact.site_order_number),
            _clean(fact.source_revision),
        )
    )


def ingest_site_crm_signal_fact(
    session: Session,
    fact: SiteCrmSignalFact,
) -> OnecExecutionIngestResult:
    site_order_number = _clean(fact.site_order_number)
    if not site_order_number:
        raise ValueError("site_order_number is required")
    event_types = {
        "in_delivery": fulfillment.EVENT_SITE_CARRIER_IN_DELIVERY,
        "delivered": fulfillment.EVENT_SITE_CARRIER_DELIVERED,
        "cdek_address_mismatch": fulfillment.EVENT_SITE_CDEK_ADDRESS_MISMATCH,
    }
    event_type = event_types.get(fact.signal)
    if event_type is None:
        raise ValueError("unsupported site CRM signal")
    source_ref = canonical_site_crm_signal_source_ref(fact)
    persisted = fulfillment.upsert_execution_event(
        session,
        site_order_number=site_order_number,
        event_type=event_type,
        event_at=fact.event_at,
        source=fulfillment.SOURCE_SITE_CRM,
        source_ref=source_ref,
        confidence="strong",
        raw_message_id=None,
        payload={
            "pipeline": "site_crm_signal",
            "bitrix_deal_id": fact.bitrix_deal_id,
            "source_revision": _clean(fact.source_revision),
            "current_stage": _clean(fact.current_stage) or None,
        },
        bitrix_deal_id=fact.bitrix_deal_id,
    )
    case = session.scalar(
        select(SiteOrderExecutionCase).where(
            SiteOrderExecutionCase.site_order_number == site_order_number
        )
    )
    if case is None:
        raise RuntimeError("execution_case_not_created")
    if fact.current_stage:
        case.current_crm_stage = _clean(fact.current_stage) or case.current_crm_stage
    current_payload = case.payload if isinstance(case.payload, dict) else {}
    case.payload = {
        **current_payload,
        "site_crm_signal": {
            "signal": fact.signal,
            "source_revision": _clean(fact.source_revision),
            "event_at": _naive(fact.event_at).isoformat(),
            "review_required": fact.signal == "cdek_address_mismatch",
            "review_reason": (
                "cdek_result_address_differs_from_order_address"
                if fact.signal == "cdek_address_mismatch"
                else None
            ),
        },
    }
    case.updated_at = datetime.now()
    session.flush()
    return OnecExecutionIngestResult(
        event_id=persisted.id if persisted is not None else None,
        duplicate=persisted is None,
        source_ref=source_ref,
    )


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value
