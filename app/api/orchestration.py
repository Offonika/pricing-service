"""Authenticated internal API for durable job and delivery orchestration."""

from __future__ import annotations

from typing import Annotated, Any, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_orchestration_internal_token
from app.domains.management.orchestration import (
    OrchestrationConflictError,
    OrchestrationNotFoundError,
    OrchestrationService,
)
from app.infrastructure.orchestration import SqlAlchemyOrchestrationRepository
from app.schemas.orchestration import (
    DeliveryClaimRequest,
    DeliveryFinishRequest,
    DeliveryResponse,
    OrchestrationHealthResponse,
    RunClaimRequest,
    RunFinishRequest,
    RunHeartbeatRequest,
    RunResponse,
)

router = APIRouter(
    prefix="/api/management/internal/orchestration",
    tags=["management-orchestration"],
    dependencies=[Depends(require_orchestration_internal_token)],
)

IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=255),
]


def _service(db: Session) -> OrchestrationService:
    return OrchestrationService(SqlAlchemyOrchestrationRepository(db))


def _write(db: Session, command: Callable[[], Any]) -> Any:
    try:
        response = command()
        db.commit()
        return response
    except OrchestrationNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrchestrationConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="concurrent orchestration claim conflict; retry with the same idempotency key",
        ) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="orchestration storage unavailable") from exc


@router.post("/runs/claim", response_model=RunResponse)
def claim_run(
    payload: RunClaimRequest,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
) -> RunResponse:
    response = _write(
        db,
        lambda: _service(db).claim_run(
            idempotency_key=idempotency_key,
            **payload.model_dump(),
        ),
    )
    return RunResponse.model_validate(response)


@router.post("/runs/{run_id}/heartbeat", response_model=RunResponse)
def heartbeat_run(
    run_id: UUID,
    payload: RunHeartbeatRequest,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
) -> RunResponse:
    response = _write(
        db,
        lambda: _service(db).heartbeat_run(
            idempotency_key=idempotency_key,
            run_id=run_id,
            **payload.model_dump(),
        ),
    )
    return RunResponse.model_validate(response)


@router.post("/runs/{run_id}/finish", response_model=RunResponse)
def finish_run(
    run_id: UUID,
    payload: RunFinishRequest,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
) -> RunResponse:
    response = _write(
        db,
        lambda: _service(db).finish_run(
            idempotency_key=idempotency_key,
            run_id=run_id,
            **payload.model_dump(),
        ),
    )
    return RunResponse.model_validate(response)


@router.post("/delivery-intents/claim", response_model=DeliveryResponse)
def claim_delivery(
    payload: DeliveryClaimRequest,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
) -> DeliveryResponse:
    response = _write(
        db,
        lambda: _service(db).claim_delivery(
            idempotency_key=idempotency_key,
            **payload.model_dump(),
        ),
    )
    return DeliveryResponse.model_validate(response)


@router.post("/delivery-intents/{intent_id}/finish", response_model=DeliveryResponse)
def finish_delivery(
    intent_id: UUID,
    payload: DeliveryFinishRequest,
    idempotency_key: IdempotencyKey,
    db: Session = Depends(get_db),
) -> DeliveryResponse:
    response = _write(
        db,
        lambda: _service(db).finish_delivery(
            idempotency_key=idempotency_key,
            intent_id=intent_id,
            **payload.model_dump(),
        ),
    )
    return DeliveryResponse.model_validate(response)


@router.get("/health", response_model=OrchestrationHealthResponse)
def get_orchestration_health(
    db: Session = Depends(get_db),
) -> OrchestrationHealthResponse:
    try:
        return OrchestrationHealthResponse.model_validate(_service(db).health())
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="orchestration storage unavailable") from exc
