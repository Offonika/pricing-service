from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import require_order_payment_control_internal_token
from app.core.config import get_settings
from app.infrastructure.db.engines import (
    DatabaseNotConfiguredError,
    get_application_engine,
    get_onec_engine,
)
from app.schemas.order_payment_control import (
    OrderPaymentCheckRequest,
    OrderPaymentCheckResponse,
)
from app.services import order_payment_control as payment_control

logger = logging.getLogger("app.order_payment_control")
router = APIRouter(dependencies=[Depends(require_order_payment_control_internal_token)])


def _confirmed_ready_at(site_order_number: str, checked_at: datetime) -> datetime | None:
    try:
        return payment_control.fetch_confirmed_ready_at(
            get_application_engine(),
            site_order_number=site_order_number,
            checked_at=checked_at,
        )
    except SQLAlchemyError:
        # Точный срок — nullable-данные CRM. Его недоступность не может ослаблять
        # проверку резерва и не должна превращать корректный FULL в 503.
        logger.warning(
            "confirmed order readiness is unavailable",
            extra={"site_order_number": site_order_number},
        )
        return None


@router.post("/check", response_model=OrderPaymentCheckResponse)
def check_order_payment(payload: OrderPaymentCheckRequest) -> OrderPaymentCheckResponse:
    settings = get_settings()
    try:
        decision = payment_control.check_order_payment(
            get_onec_engine(),
            site_order_number=payload.site_order_number,
            site_amount=payload.site_amount,
            payment_amount=payload.payment_amount,
            source_warehouse_xml_id=payload.source_warehouse_xml_id,
            closure_blocks_payment=settings.order_payment_control_closure_blocks_payment,
            closure_allowed_reasons=settings.order_payment_control_closure_allowed_reasons,
            confirmed_ready_at_resolver=_confirmed_ready_at,
        )
    except DatabaseNotConfiguredError as exc:
        logger.warning(
            "order payment check denied: 1C source is not configured",
            extra={
                "site_order_number": payload.site_order_number,
                "stage": payload.stage,
            },
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "onec_unavailable", "message": "1C source is unavailable"},
        ) from exc
    except SQLAlchemyError as exc:
        logger.warning(
            "order payment check denied: 1C query failed",
            extra={
                "site_order_number": payload.site_order_number,
                "stage": payload.stage,
            },
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "onec_unavailable", "message": "1C source is unavailable"},
        ) from exc

    logger.info(
        "order payment check completed",
        extra={
            "check_id": decision.check_id,
            "site_order_number": decision.site_order_number,
            "payment_id": payload.payment_id,
            "region_xml_id": str(payload.region_xml_id),
            "expected_source_warehouse_xml_id": str(payload.source_warehouse_xml_id),
            "availability_snapshot_id": payload.availability_snapshot_id,
            "stage": payload.stage,
            "allowed": decision.allowed,
            "reason": decision.reason,
            "site_amount": str(decision.site_amount),
            "payment_amount": str(decision.payment_amount),
            "onec_amount": str(decision.onec_amount) if decision.onec_amount is not None else None,
            "onec_revision": decision.onec_revision,
            "onec_posted": decision.onec_posted,
            "onec_closure_document": decision.onec_closure_document,
            "onec_closure_reason": decision.onec_closure_reason,
            "reservation_state": decision.reservation_state,
            "reservation_quantity_match": decision.reservation_quantity_match,
            "actual_source_warehouse_xml_id": decision.source_warehouse_xml_id,
        },
    )
    return OrderPaymentCheckResponse(**asdict(decision))
