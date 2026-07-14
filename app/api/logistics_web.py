from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_logistics_internal_token
from app.core.config import get_settings
from app.models import LogisticsUser
from app.schemas.logistics import (
    LogisticsConfirmResponse,
    LogisticsDraftResponse,
    LogisticsMonitorResponse,
    LogisticsUserProfile,
)
from app.services import logistics as logistics_service

router = APIRouter()
page_router = APIRouter()

COOKIE_NAME = "mm_logistics_session"
_INDEX_PATHS = (
    Path("/var/www/pricing-service/index.html"),
    Path(__file__).resolve().parents[2] / "ui" / "dist" / "index.html",
)


class LogisticsWebSessionRequest(BaseModel):
    actor_user_id: int


class LogisticsWebDraftCreateRequest(BaseModel):
    warehouse_id: int
    driver_id: int | None = None
    route_run_id: int | None = None
    default_dropoff_warehouse_id: int | None = None
    comment: str | None = None


class LogisticsWebDraftScanRequest(BaseModel):
    barcode: str | None = None
    lookup_code: str | None = None
    dropoff_warehouse_id: int | None = None


class LogisticsWebDraftConfirmRequest(BaseModel):
    comment: str | None = None
    idempotency_key: str | None = None
    photos: list[dict[str, str | None]] = Field(default_factory=list)


def _read_index() -> str:
    for path in _INDEX_PATHS:
        if path.exists():
            return path.read_text(encoding="utf-8")
    return "<!doctype html><html><body>Logistics fallback UI is not built</body></html>"


@page_router.get("/logistics/fallback", response_class=HTMLResponse, include_in_schema=False)
@page_router.get("/logistics/fallback/", response_class=HTMLResponse, include_in_schema=False)
@page_router.get(
    "/logistics/fallback/{path:path}", response_class=HTMLResponse, include_in_schema=False
)
def logistics_fallback_page() -> HTMLResponse:
    return HTMLResponse(_read_index())


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def _session_secret() -> str:
    settings = get_settings()
    if not settings.logistics_web_session_secret:
        raise HTTPException(status_code=401, detail="logistics web session is not configured")
    return settings.logistics_web_session_secret


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()


def _create_session_token(actor_user_id: int) -> tuple[str, int]:
    settings = get_settings()
    secret = _session_secret()
    expires_at = int(time.time()) + settings.logistics_web_session_ttl_seconds
    payload = _b64encode(
        json.dumps(
            {"actor_user_id": actor_user_id, "exp": expires_at},
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return f"{payload}.{_sign(payload, secret)}", expires_at


def _decode_session_token(token: str | None) -> dict[str, Any]:
    if not token or "." not in token:
        raise HTTPException(status_code=401, detail="unauthorized")
    payload, signature = token.rsplit(".", 1)
    expected = _sign(payload, _session_secret())
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        data = json.loads(_b64decode(payload))
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="unauthorized") from None
    if int(data.get("exp") or 0) < int(time.time()):
        raise HTTPException(status_code=401, detail="session expired")
    return data


def _profile(user: LogisticsUser) -> dict:
    return {
        "id": user.id,
        "external_id": user.external_id,
        "telegram_user_id": user.telegram_user_id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "default_warehouse_id": user.default_warehouse_id,
        "default_warehouse_name": (
            user.default_warehouse.name if user.default_warehouse is not None else None
        ),
    }


def require_logistics_web_actor(
    mm_logistics_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: Session = Depends(get_db),
) -> LogisticsUser:
    data = _decode_session_token(mm_logistics_session)
    user = db.get(LogisticsUser, int(data["actor_user_id"]))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


@router.post(
    "/session",
    response_model=LogisticsUserProfile,
    dependencies=[Depends(require_logistics_internal_token)],
)
def create_web_session(
    payload: LogisticsWebSessionRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    user = db.get(LogisticsUser, payload.actor_user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=404, detail="logistics user not found")
    token, _expires_at = _create_session_token(user.id)
    settings = get_settings()
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.logistics_web_session_ttl_seconds,
        httponly=True,
        secure=not settings.debug,
        samesite="lax",
    )
    return _profile(user)


@router.get("/profile", response_model=LogisticsUserProfile)
def web_profile(actor: LogisticsUser = Depends(require_logistics_web_actor)):
    return _profile(actor)


@router.get("/warehouses")
def web_warehouses(
    db: Session = Depends(get_db),
    _: LogisticsUser = Depends(require_logistics_web_actor),
):
    return logistics_service.list_warehouses(db)


@router.get("/drivers")
def web_drivers(
    db: Session = Depends(get_db),
    _: LogisticsUser = Depends(require_logistics_web_actor),
):
    return logistics_service.list_drivers(db)


@router.get("/monitor", response_model=list[LogisticsMonitorResponse])
def web_monitor(
    status: str | None = None,
    warehouse_id: int | None = None,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(require_logistics_web_actor),
):
    effective_warehouse_id = warehouse_id or actor.default_warehouse_id
    return logistics_service.list_monitor(
        db,
        status=status,
        warehouse_id=effective_warehouse_id,
    )


@router.post("/handoffs/draft", response_model=LogisticsDraftResponse)
def web_create_handoff_draft(
    payload: LogisticsWebDraftCreateRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(require_logistics_web_actor),
):
    return logistics_service.create_draft(
        db,
        draft_type=logistics_service.DRAFT_TYPE_HANDOFF,
        actor_user_id=actor.id,
        warehouse_id=payload.warehouse_id,
        driver_id=payload.driver_id,
        route_run_id=payload.route_run_id,
        default_dropoff_warehouse_id=payload.default_dropoff_warehouse_id,
        comment=payload.comment,
    )


@router.post("/handoffs/draft/{draft_id}/scan", response_model=LogisticsDraftResponse)
def web_scan_handoff(
    draft_id: int,
    payload: LogisticsWebDraftScanRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(require_logistics_web_actor),
):
    return logistics_service.add_scan_to_draft(
        db,
        draft_id=draft_id,
        actor_user_id=actor.id,
        barcode=payload.barcode,
        lookup_code=payload.lookup_code,
        dropoff_warehouse_id=payload.dropoff_warehouse_id,
    )


@router.post("/handoffs/draft/{draft_id}/confirm", response_model=LogisticsConfirmResponse)
def web_confirm_handoff(
    draft_id: int,
    payload: LogisticsWebDraftConfirmRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(require_logistics_web_actor),
):
    return logistics_service.confirm_draft(
        db,
        draft_id=draft_id,
        actor_user_id=actor.id,
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
        photos=payload.photos,
    )


@router.post("/receipts/draft", response_model=LogisticsDraftResponse)
def web_create_receipt_draft(
    payload: LogisticsWebDraftCreateRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(require_logistics_web_actor),
):
    return logistics_service.create_draft(
        db,
        draft_type=logistics_service.DRAFT_TYPE_RECEIPT,
        actor_user_id=actor.id,
        warehouse_id=payload.warehouse_id,
        route_run_id=payload.route_run_id,
        comment=payload.comment,
    )


@router.post("/receipts/draft/{draft_id}/scan", response_model=LogisticsDraftResponse)
def web_scan_receipt(
    draft_id: int,
    payload: LogisticsWebDraftScanRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(require_logistics_web_actor),
):
    return logistics_service.add_scan_to_draft(
        db,
        draft_id=draft_id,
        actor_user_id=actor.id,
        barcode=payload.barcode,
        lookup_code=payload.lookup_code,
    )


@router.post("/receipts/draft/{draft_id}/confirm", response_model=LogisticsConfirmResponse)
def web_confirm_receipt(
    draft_id: int,
    payload: LogisticsWebDraftConfirmRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(require_logistics_web_actor),
):
    return logistics_service.confirm_draft(
        db,
        draft_id=draft_id,
        actor_user_id=actor.id,
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
        photos=payload.photos,
    )
