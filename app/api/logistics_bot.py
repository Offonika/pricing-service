from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.api.dependencies import get_db, require_logistics_internal_token
from app.core.config import get_settings
from app.models import LogisticsBotSession, LogisticsDraft, LogisticsDraftItem
from app.telegram.logistics_bot import get_logistics_telegram_bot

router = APIRouter()


@router.get("/health")
def logistics_bot_health():
    settings = get_settings()
    return {
        "status": "ok" if settings.logistics_bot_token else "not_configured",
        "webhook_secret_configured": bool(settings.logistics_bot_webhook_secret),
    }


@router.post("/webhook")
def logistics_bot_webhook(
    payload: dict,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    settings = get_settings()
    expected_secret = settings.logistics_bot_webhook_secret
    if expected_secret:
        if x_telegram_bot_api_secret_token != expected_secret:
            raise HTTPException(status_code=401, detail="unauthorized")

    bot = get_logistics_telegram_bot()
    bot.handle_update(payload)
    return {"status": "ok"}


@router.get("/webhook/info")
def logistics_bot_webhook_info(_: str = Depends(require_logistics_internal_token)):
    bot = get_logistics_telegram_bot()
    return bot.bot_api.get_webhook_info()


@router.post("/webhook/register")
def logistics_bot_webhook_register(_: str = Depends(require_logistics_internal_token)):
    settings = get_settings()
    if not settings.logistics_bot_webhook_url:
        raise HTTPException(status_code=422, detail="logistics bot webhook url is not configured")
    bot = get_logistics_telegram_bot()
    return bot.bot_api.set_webhook(
        url=settings.logistics_bot_webhook_url,
        secret_token=settings.logistics_bot_webhook_secret,
    )


@router.post("/webhook/delete")
def logistics_bot_webhook_delete(_: str = Depends(require_logistics_internal_token)):
    bot = get_logistics_telegram_bot()
    return bot.bot_api.delete_webhook(drop_pending_updates=False)


@router.get("/sessions")
def logistics_bot_sessions(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    draft_type: str | None = Query(default=None),
    actor_user_name: str | None = Query(default=None),
    has_errors: bool | None = Query(default=None),
    db: Session = Depends(get_db),
    _: str = Depends(require_logistics_internal_token),
):
    rows = (
        db.scalars(
            select(LogisticsBotSession)
            .options(
                joinedload(LogisticsBotSession.actor_user),
                joinedload(LogisticsBotSession.photos),
                joinedload(LogisticsBotSession.draft)
                .joinedload(LogisticsDraft.items)
                .joinedload(LogisticsDraftItem.transfer),
            )
            .order_by(LogisticsBotSession.id.desc())
        )
        .unique()
        .all()
    )
    if draft_type is not None:
        rows = [row for row in rows if row.draft_type == draft_type]
    if actor_user_name is not None:
        actor_user_name_normalized = actor_user_name.strip().lower()
        rows = [
            row
            for row in rows
            if row.actor_user is not None
            and (
                actor_user_name_normalized in row.actor_user.full_name.lower()
                or (
                    row.actor_user.username is not None
                    and actor_user_name_normalized in row.actor_user.username.lower()
                )
            )
        ]
    if has_errors is not None:
        rows = [row for row in rows if (row.scan_error_count > 0) is has_errors]
    total = len(rows)
    rows = rows[offset : offset + limit]
    payload = []
    for row in rows:
        payload.append(
            {
                "id": row.id,
                "chat_id": row.chat_id,
                "telegram_user_id": row.telegram_user_id,
                "actor_user_id": row.actor_user_id,
                "actor_user_name": row.actor_user.full_name if row.actor_user is not None else None,
                "draft_id": row.draft_id,
                "draft_type": row.draft_type,
                "status_message_id": row.status_message_id,
                "scan_error_count": row.scan_error_count,
                "recent_errors": row.recent_errors or [],
                "photo_count": len(row.photos),
                "item_count": len(row.draft.items) if row.draft is not None else 0,
                "items": [
                    {
                        "transfer_id": item.transfer_id,
                        "document_number": item.transfer.document_number,
                        "barcode": item.barcode,
                    }
                    for item in (row.draft.items if row.draft is not None else [])
                ],
            }
        )
    return {
        "items": payload,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/sessions/{session_id}/close")
def logistics_bot_session_close(
    session_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(require_logistics_internal_token),
):
    row = db.get(LogisticsBotSession, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="bot session not found")
    db.delete(row)
    db.commit()
    return {"status": "closed", "session_id": session_id}
