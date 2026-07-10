from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.api.procurement_labels import _bitrix_launch_payload, _inject_launch_payload, _read_index
from app.schemas.procurement_assortment_decisions import (
    ProcurementAssortmentDecision,
    ProcurementAssortmentDecisionSyncResponse,
    ProcurementAssortmentDecisionUpdateRequest,
    ProcurementAssortmentDecisionUpdateResponse,
)
from app.services.bitrix_procurement_labels_auth import (
    ProcurementLabelsSession,
    verify_procurement_labels_session,
)
from app.services.procurement_assortment_decisions import (
    fetch_decision,
    sync_decision_to_manual_overrides,
    update_decision,
)

router = APIRouter()
page_router = APIRouter()


@page_router.api_route(
    "/bitrix/procurement-assortment",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
@page_router.api_route(
    "/bitrix/procurement-assortment/",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
@page_router.api_route(
    "/bitrix/procurement-assortment/{path:path}",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def bitrix_procurement_assortment_page(request: Request) -> HTMLResponse:
    payload = await _bitrix_launch_payload(request)
    html = _inject_launch_payload(_read_index(), payload)
    return HTMLResponse(html)


def _service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(status_code=500, detail="Bitrix procurement mapping is incomplete")
    return HTTPException(
        status_code=502, detail="Bitrix procurement decision service is unavailable"
    )


@router.get(
    "/procurement-assortment/orders/{item_id}/decision",
    response_model=ProcurementAssortmentDecision,
)
def get_procurement_assortment_decision(
    item_id: str,
    _session: ProcurementLabelsSession = Depends(verify_procurement_labels_session),
) -> ProcurementAssortmentDecision:
    try:
        return ProcurementAssortmentDecision.model_validate(fetch_decision(item_id))
    except (KeyError, RuntimeError, ValueError) as exc:
        raise _service_error(exc) from exc


@router.post(
    "/procurement-assortment/orders/{item_id}/decision",
    response_model=ProcurementAssortmentDecisionUpdateResponse,
)
def save_procurement_assortment_decision(
    item_id: str,
    payload: ProcurementAssortmentDecisionUpdateRequest,
    _session: ProcurementLabelsSession = Depends(verify_procurement_labels_session),
) -> ProcurementAssortmentDecisionUpdateResponse:
    try:
        decision = update_decision(item_id, payload.model_dump())
    except (KeyError, RuntimeError, ValueError) as exc:
        raise _service_error(exc) from exc
    return ProcurementAssortmentDecisionUpdateResponse(
        decision=ProcurementAssortmentDecision.model_validate(decision),
        updated=True,
    )


@router.post(
    "/procurement-assortment/orders/{item_id}/decision/sync",
    response_model=ProcurementAssortmentDecisionSyncResponse,
)
def sync_procurement_assortment_decision(
    item_id: str,
    _session: ProcurementLabelsSession = Depends(verify_procurement_labels_session),
) -> ProcurementAssortmentDecisionSyncResponse:
    try:
        result = sync_decision_to_manual_overrides(item_id)
    except (KeyError, RuntimeError, ValueError) as exc:
        raise _service_error(exc) from exc
    return ProcurementAssortmentDecisionSyncResponse.model_validate(result)
