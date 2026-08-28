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
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_logistics_internal_token
from app.core.config import get_settings
from app.models import LogisticsDraft, LogisticsUser
from app.schemas.logistics import (
    LogisticsConfirmResponse,
    LogisticsDraftResponse,
    LogisticsMonitorResponse,
    LogisticsPhotoInput,
    LogisticsUserProfile,
)
from app.services import logistics as logistics_service

router = APIRouter()
page_router = APIRouter()

COOKIE_NAME = "mm_logistics_session"
# Сборка активного релиза имеет приоритет: легаси-каталог /var/www не обновляется
# при выкладке и иначе месяцами подменяет свежий ui/dist старым билдом.
_INDEX_PATHS = (
    Path(__file__).resolve().parents[2] / "ui" / "dist" / "index.html",
    Path("/var/www/pricing-service/index.html"),
)


class LogisticsWebSessionRequest(BaseModel):
    actor_user_id: int


class LogisticsWebDraftCreateRequest(BaseModel):
    warehouse_id: int
    driver_id: int | None = None
    route_run_id: int | None = None
    comment: str | None = Field(default=None, max_length=1000)


class LogisticsWebDraftScanRequest(BaseModel):
    barcode: str | None = Field(default=None, max_length=255)
    lookup_code: str | None = Field(default=None, max_length=255)


class LogisticsWebDraftConfirmRequest(BaseModel):
    comment: str | None = Field(default=None, max_length=1000)
    idempotency_key: str | None = Field(default=None, max_length=255)
    photos: list[LogisticsPhotoInput] = Field(default_factory=list, max_length=20)


class LogisticsWebDraftCancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


def _read_index() -> str:
    for path in _INDEX_PATHS:
        if path.exists():
            return path.read_text(encoding="utf-8").replace(
                'href="./vite.svg"',
                'href="/vite.svg"',
            )
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
        "bitrix_user_id": user.bitrix_user_id,
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


def _require_web_role(actor: LogisticsUser, expected: str) -> None:
    if actor.role not in {expected, "admin"}:
        raise HTTPException(status_code=403, detail="operation is not allowed for logistics role")


def _require_web_draft_type(db: Session, draft_id: int, expected_type: str) -> None:
    actual_type = db.scalar(select(LogisticsDraft.draft_type).where(LogisticsDraft.id == draft_id))
    if actual_type is None:
        raise HTTPException(status_code=404, detail="draft not found")
    if actual_type != expected_type:
        raise HTTPException(status_code=409, detail="draft type does not match endpoint")


def _web_monitor_warehouse_id(actor: LogisticsUser, requested: int | None) -> int | None:
    if actor.role in {"logist", "admin"}:
        return requested
    if actor.default_warehouse_id is None:
        raise HTTPException(status_code=422, detail="default logistics warehouse is not configured")
    if requested not in (None, actor.default_warehouse_id):
        raise HTTPException(status_code=403, detail="warehouse is not allowed for user")
    return actor.default_warehouse_id


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


@router.get("/draft/open", response_model=LogisticsDraftResponse | None)
def web_open_draft(
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(require_logistics_web_actor),
):
    return logistics_service.get_open_draft_for_actor(db, actor_user_id=actor.id)


@router.get("/monitor", response_model=list[LogisticsMonitorResponse])
def web_monitor(
    status: str | None = None,
    warehouse_id: int | None = None,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(require_logistics_web_actor),
):
    return logistics_service.list_monitor(
        db,
        status=status,
        warehouse_id=_web_monitor_warehouse_id(actor, warehouse_id),
    )


@router.post("/handoffs/draft", response_model=LogisticsDraftResponse)
def web_create_handoff_draft(
    payload: LogisticsWebDraftCreateRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(require_logistics_web_actor),
):
    _require_web_role(actor, "sender")
    return logistics_service.create_draft(
        db,
        draft_type=logistics_service.DRAFT_TYPE_HANDOFF,
        actor_user_id=actor.id,
        warehouse_id=payload.warehouse_id,
        driver_id=payload.driver_id,
        route_run_id=payload.route_run_id,
        default_dropoff_warehouse_id=None,
        comment=payload.comment,
    )


@router.post("/handoffs/draft/{draft_id}/scan", response_model=LogisticsDraftResponse)
def web_scan_handoff(
    draft_id: int,
    payload: LogisticsWebDraftScanRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(require_logistics_web_actor),
):
    _require_web_role(actor, "sender")
    _require_web_draft_type(db, draft_id, logistics_service.DRAFT_TYPE_HANDOFF)
    return logistics_service.add_scan_to_draft(
        db,
        draft_id=draft_id,
        actor_user_id=actor.id,
        barcode=payload.barcode,
        lookup_code=payload.lookup_code,
        dropoff_warehouse_id=None,
    )


@router.post(
    "/handoffs/draft/{draft_id}/items/{item_id}/remove",
    response_model=LogisticsDraftResponse,
)
def web_remove_handoff_item(
    draft_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(require_logistics_web_actor),
):
    _require_web_role(actor, "sender")
    _require_web_draft_type(db, draft_id, logistics_service.DRAFT_TYPE_HANDOFF)
    return logistics_service.remove_scan_from_draft(
        db,
        draft_id=draft_id,
        item_id=item_id,
        actor_user_id=actor.id,
    )


@router.post("/handoffs/draft/{draft_id}/cancel", response_model=LogisticsDraftResponse)
def web_cancel_handoff(
    draft_id: int,
    payload: LogisticsWebDraftCancelRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(require_logistics_web_actor),
):
    _require_web_role(actor, "sender")
    _require_web_draft_type(db, draft_id, logistics_service.DRAFT_TYPE_HANDOFF)
    return logistics_service.cancel_draft(
        db,
        draft_id=draft_id,
        actor_user_id=actor.id,
        reason=payload.reason,
    )


@router.post("/handoffs/draft/{draft_id}/confirm", response_model=LogisticsConfirmResponse)
def web_confirm_handoff(
    draft_id: int,
    payload: LogisticsWebDraftConfirmRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(require_logistics_web_actor),
):
    _require_web_role(actor, "sender")
    _require_web_draft_type(db, draft_id, logistics_service.DRAFT_TYPE_HANDOFF)
    return logistics_service.confirm_draft(
        db,
        draft_id=draft_id,
        actor_user_id=actor.id,
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
        photos=[photo.model_dump() for photo in payload.photos],
        source_channel="web_fallback",
    )


@router.post("/receipts/draft", response_model=LogisticsDraftResponse)
def web_create_receipt_draft(
    payload: LogisticsWebDraftCreateRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(require_logistics_web_actor),
):
    _require_web_role(actor, "receiver")
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
    _require_web_role(actor, "receiver")
    _require_web_draft_type(db, draft_id, logistics_service.DRAFT_TYPE_RECEIPT)
    return logistics_service.add_scan_to_draft(
        db,
        draft_id=draft_id,
        actor_user_id=actor.id,
        barcode=payload.barcode,
        lookup_code=payload.lookup_code,
    )


@router.post(
    "/receipts/draft/{draft_id}/items/{item_id}/remove",
    response_model=LogisticsDraftResponse,
)
def web_remove_receipt_item(
    draft_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(require_logistics_web_actor),
):
    _require_web_role(actor, "receiver")
    _require_web_draft_type(db, draft_id, logistics_service.DRAFT_TYPE_RECEIPT)
    return logistics_service.remove_scan_from_draft(
        db,
        draft_id=draft_id,
        item_id=item_id,
        actor_user_id=actor.id,
    )


@router.post("/receipts/draft/{draft_id}/cancel", response_model=LogisticsDraftResponse)
def web_cancel_receipt(
    draft_id: int,
    payload: LogisticsWebDraftCancelRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(require_logistics_web_actor),
):
    _require_web_role(actor, "receiver")
    _require_web_draft_type(db, draft_id, logistics_service.DRAFT_TYPE_RECEIPT)
    return logistics_service.cancel_draft(
        db,
        draft_id=draft_id,
        actor_user_id=actor.id,
        reason=payload.reason,
    )


@router.post("/receipts/draft/{draft_id}/confirm", response_model=LogisticsConfirmResponse)
def web_confirm_receipt(
    draft_id: int,
    payload: LogisticsWebDraftConfirmRequest,
    db: Session = Depends(get_db),
    actor: LogisticsUser = Depends(require_logistics_web_actor),
):
    _require_web_role(actor, "receiver")
    _require_web_draft_type(db, draft_id, logistics_service.DRAFT_TYPE_RECEIPT)
    return logistics_service.confirm_draft(
        db,
        draft_id=draft_id,
        actor_user_id=actor.id,
        comment=payload.comment,
        idempotency_key=payload.idempotency_key,
        photos=[photo.model_dump() for photo in payload.photos],
        source_channel="web_fallback",
    )
