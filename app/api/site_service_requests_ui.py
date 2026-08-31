from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.core.config import Settings, get_settings
from app.models.site_service_requests import SiteServiceRequestFile
from app.schemas.site_service_requests import (
    SiteServiceRequestConversationMutationResponse,
    SiteServiceRequestConversationResponse,
    SiteServiceRequestInternalNoteRequest,
    SiteServiceRequestUiSessionRequest,
    SiteServiceRequestUiSessionResponse,
    SiteServiceRequestUiUser,
)
from app.services.site_service_request_conversations import (
    build_site_service_request_conversation,
    create_site_service_request_internal_note,
    create_site_service_request_ui_reply,
    get_site_service_request_case_for_ui,
    retry_site_service_request_ui_command,
)
from app.services.site_service_requests import (
    SiteServiceRequestConfigurationError,
    SiteServiceRequestConflictError,
    SiteServiceRequestNotFoundError,
    SiteServiceRequestPayloadError,
    build_site_service_request_cipher,
)
from app.services.site_service_requests_ui_auth import (
    SiteServiceRequestUiSession,
    authenticate_site_service_request_ui_user,
    create_site_service_request_ui_session_token,
    require_site_service_request_ui_session,
    validate_site_service_request_ui_launch,
)

router = APIRouter()
page_router = APIRouter()

_INDEX_PATHS = (
    Path(__file__).resolve().parents[2] / "ui" / "dist" / "index.html",
    Path("/var/www/pricing-service/index.html"),
)


def _trusted_bitrix_download_url(value: str, *, webhook: str) -> str:
    expected = urllib.parse.urlsplit(f"{webhook.rstrip('/')}/")
    actual = urllib.parse.urlsplit(value)
    if (
        actual.scheme != "https"
        or actual.hostname != expected.hostname
        or actual.port != expected.port
    ):
        raise ValueError("untrusted Bitrix download URL")
    return value


class _BitrixSameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, webhook: str) -> None:
        super().__init__()
        self._webhook = webhook

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _trusted_bitrix_download_url(newurl, webhook=self._webhook)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_index() -> str:
    for path in _INDEX_PATHS:
        if path.exists():
            return (
                path.read_text(encoding="utf-8")
                .replace('src="./assets/', 'src="/assets/')
                .replace('href="./assets/', 'href="/assets/')
                .replace('href="./vite.svg"', 'href="/vite.svg"')
            )
    return "<!doctype html><html><body>Site service requests UI is not built</body></html>"


async def _launch_payload(request: Request) -> dict[str, object | None]:
    query = dict(request.query_params)
    form: dict[str, list[str]] = {}
    if request.method == "POST":
        raw_body = await request.body()
        if len(raw_body) > 64 * 1024:
            raise HTTPException(status_code=413, detail="bitrix_launch_payload_too_large")
        try:
            form = urllib.parse.parse_qs(raw_body.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="bitrix_launch_payload_invalid") from exc

    def first(key: str) -> str | None:
        value = form.get(key, [None])[0]
        return str(value).strip() if value not in (None, "") else None

    def query_first(key: str) -> str | None:
        value = query.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    raw_options = (
        first("PLACEMENT_OPTIONS")
        or first("options")
        or query_first("PLACEMENT_OPTIONS")
        or query_first("options")
    )
    try:
        options = json.loads(raw_options) if raw_options else {}
    except json.JSONDecodeError:
        options = {}
    if not isinstance(options, dict):
        options = {}
    return {
        "access_token": first("AUTH_ID") or first("access_token"),
        "domain": query_first("DOMAIN") or first("DOMAIN") or query_first("domain"),
        "member_id": first("member_id") or query_first("member_id"),
        "placement": first("PLACEMENT") or query_first("PLACEMENT"),
        "placement_options": options,
    }


@page_router.api_route(
    "/bitrix/site-service-requests/{path:path}",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
@page_router.api_route(
    "/bitrix/site-service-requests",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def site_service_requests_ui_page(request: Request) -> HTMLResponse:
    payload = await _launch_payload(request)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    script = f'<script id="mm-bitrix-launch">window.__MM_BITRIX_LAUNCH__={raw};</script>'
    return HTMLResponse(
        _read_index().replace("</head>", f"{script}</head>", 1),
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/session", response_model=SiteServiceRequestUiSessionResponse)
def create_ui_session(
    payload: SiteServiceRequestUiSessionRequest,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> SiteServiceRequestUiSessionResponse:
    response.headers["Cache-Control"] = "no-store"
    domain, member_id = validate_site_service_request_ui_launch(
        domain=payload.domain,
        member_id=payload.member_id,
        placement=payload.placement,
        settings=settings,
    )
    user_id, user_name, is_admin = authenticate_site_service_request_ui_user(
        domain=domain,
        access_token=payload.access_token,
        item_id=payload.item_id,
        settings=settings,
    )
    token, expires_at = create_site_service_request_ui_session_token(
        domain=domain,
        member_id=member_id,
        user_id=user_id,
        user_name=user_name,
        is_admin=is_admin,
        item_id=payload.item_id,
        settings=settings,
    )
    return SiteServiceRequestUiSessionResponse(
        sessionToken=token,
        expiresAt=expires_at,
        itemId=payload.item_id,
        user=SiteServiceRequestUiUser(id=user_id, name=user_name, isAdmin=is_admin),
    )


def _require_item(session: SiteServiceRequestUiSession, item_id: int) -> None:
    if session.item_id != item_id:
        raise HTTPException(status_code=403, detail="ui_session_item_mismatch")


def _require_ui_writes(settings: Settings) -> None:
    if not settings.site_service_requests_ui_replies_enabled:
        raise HTTPException(status_code=503, detail="ui_replies_disabled")


@router.get(
    "/items/{item_id}/conversation",
    response_model=SiteServiceRequestConversationResponse,
)
def conversation(
    item_id: int,
    response: Response,
    before_id: int | None = Query(default=None, alias="beforeId", gt=0),
    ui_session: SiteServiceRequestUiSession = Depends(require_site_service_request_ui_session),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> SiteServiceRequestConversationResponse:
    response.headers["Cache-Control"] = "private, no-store"
    _require_item(ui_session, item_id)
    try:
        data = build_site_service_request_conversation(
            db,
            item_id=item_id,
            cipher=build_site_service_request_cipher(settings),
            before_id=before_id,
            site_base_url=settings.site_service_requests_site_base_url,
        )
        data["canReply"] = bool(
            data["canReply"]
            and settings.site_service_requests_ui_replies_enabled
            and settings.site_service_requests_outbound_replies_enabled
        )
    except SiteServiceRequestNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc
    except (SiteServiceRequestConfigurationError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=503, detail="conversation_encryption_unavailable") from exc
    return SiteServiceRequestConversationResponse.model_validate(data)


def _safe_filename(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or "\x00" in normalized
        or len(normalized) > 255
    ):
        raise HTTPException(status_code=422, detail="reply_file_name_invalid")
    return normalized


@router.post(
    "/items/{item_id}/replies",
    response_model=SiteServiceRequestConversationMutationResponse,
)
async def create_reply(
    item_id: int,
    client_request_id: Annotated[
        str,
        Form(
            alias="clientRequestId",
            min_length=8,
            max_length=64,
            pattern=r"^[A-Za-z0-9_-]+$",
        ),
    ],
    text: Annotated[str, Form(min_length=1, max_length=200_000)],
    files: Annotated[list[UploadFile] | None, File()] = None,
    ui_session: SiteServiceRequestUiSession = Depends(require_site_service_request_ui_session),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> SiteServiceRequestConversationMutationResponse:
    _require_item(ui_session, item_id)
    _require_ui_writes(settings)
    if not settings.site_service_requests_outbound_replies_enabled:
        raise HTTPException(status_code=503, detail="outbound_replies_disabled")
    uploads = files or []
    if len(uploads) > settings.site_service_requests_ui_max_files_per_reply:
        raise HTTPException(status_code=422, detail="too_many_reply_files")
    loaded_files: list[tuple[str, str, bytes]] = []
    loaded_total = 0
    for upload in uploads:
        body = await upload.read(settings.site_service_requests_ui_max_file_bytes + 1)
        if len(body) > settings.site_service_requests_ui_max_file_bytes:
            raise HTTPException(status_code=422, detail="reply_file_too_large")
        loaded_total += len(body)
        if loaded_total > settings.site_service_requests_ui_max_total_file_bytes:
            raise HTTPException(status_code=422, detail="reply_files_total_too_large")
        loaded_files.append(
            (
                _safe_filename(upload.filename or ""),
                (upload.content_type or "application/octet-stream")[:255],
                body,
            )
        )
    try:
        command, duplicate = create_site_service_request_ui_reply(
            db,
            item_id=item_id,
            client_request_id=client_request_id,
            text=text,
            files=loaded_files,
            actor_user_id=ui_session.user_id,
            actor_name=ui_session.user_name,
            cipher=build_site_service_request_cipher(settings),
            max_files=settings.site_service_requests_ui_max_files_per_reply,
            max_file_bytes=settings.site_service_requests_ui_max_file_bytes,
            max_total_file_bytes=settings.site_service_requests_ui_max_total_file_bytes,
            attachments_enabled=settings.site_service_requests_command_attachments_enabled,
        )
        db.commit()
    except SiteServiceRequestPayloadError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=exc.code) from exc
    except SiteServiceRequestConfigurationError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="reply_encryption_unavailable") from exc
    except SiteServiceRequestConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=exc.code) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="reply_idempotency_conflict") from exc
    return SiteServiceRequestConversationMutationResponse(
        id=f"command:{command.id}", duplicate=duplicate, status=command.status
    )


@router.post(
    "/items/{item_id}/notes",
    response_model=SiteServiceRequestConversationMutationResponse,
)
def create_note(
    item_id: int,
    payload: SiteServiceRequestInternalNoteRequest,
    ui_session: SiteServiceRequestUiSession = Depends(require_site_service_request_ui_session),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> SiteServiceRequestConversationMutationResponse:
    _require_item(ui_session, item_id)
    _require_ui_writes(settings)
    try:
        note, duplicate = create_site_service_request_internal_note(
            db,
            item_id=item_id,
            client_request_id=payload.client_request_id,
            text=payload.text,
            actor_user_id=ui_session.user_id,
            actor_name=ui_session.user_name,
            cipher=build_site_service_request_cipher(settings),
        )
        db.commit()
    except SiteServiceRequestPayloadError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=exc.code) from exc
    except SiteServiceRequestConfigurationError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="note_encryption_unavailable") from exc
    except SiteServiceRequestConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=exc.code) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="note_idempotency_conflict") from exc
    return SiteServiceRequestConversationMutationResponse(
        id=f"message:{note.id}", duplicate=duplicate, status="note"
    )


@router.post(
    "/items/{item_id}/replies/{command_id}/retry",
    response_model=SiteServiceRequestConversationMutationResponse,
)
def retry_reply(
    item_id: int,
    command_id: int,
    ui_session: SiteServiceRequestUiSession = Depends(require_site_service_request_ui_session),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> SiteServiceRequestConversationMutationResponse:
    _require_item(ui_session, item_id)
    _require_ui_writes(settings)
    if not settings.site_service_requests_outbound_replies_enabled:
        raise HTTPException(status_code=503, detail="outbound_replies_disabled")
    try:
        command = retry_site_service_request_ui_command(db, item_id=item_id, command_id=command_id)
        db.commit()
    except SiteServiceRequestNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=exc.code) from exc
    except SiteServiceRequestConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=exc.code) from exc
    return SiteServiceRequestConversationMutationResponse(
        id=f"command:{command.id}", duplicate=False, status=command.status
    )


@router.get("/items/{item_id}/attachments/{file_id}", include_in_schema=False)
def download_attachment(
    item_id: int,
    file_id: int,
    ui_session: SiteServiceRequestUiSession = Depends(require_site_service_request_ui_session),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> Response:
    _require_item(ui_session, item_id)
    case = get_site_service_request_case_for_ui(db, item_id=item_id)
    file = db.get(SiteServiceRequestFile, file_id)
    if (
        file is None
        or file.case_id != case.id
        or file.status != "uploaded"
        or not file.bitrix_object_id
    ):
        raise HTTPException(status_code=404, detail="conversation_file_not_found")
    webhook = str(settings.site_service_requests_bitrix_webhook_url or "").rstrip("/")
    if not webhook:
        raise HTTPException(status_code=503, detail="bitrix_file_api_unavailable")
    try:
        metadata_request = urllib.request.Request(
            f"{webhook}/disk.file.get.json",
            data=urllib.parse.urlencode({"id": file.bitrix_object_id}).encode(),
            method="POST",
        )
        with urllib.request.urlopen(metadata_request, timeout=15) as response:
            raw_metadata = response.read(256 * 1024 + 1)
        if len(raw_metadata) > 256 * 1024:
            raise ValueError
        metadata = json.loads(raw_metadata.decode("utf-8"))
        result = metadata.get("result") if isinstance(metadata, dict) else None
        download_url = (
            result.get("DOWNLOAD_URL") or result.get("downloadUrl")
            if isinstance(result, dict)
            else None
        )
        if not isinstance(download_url, str) or not download_url:
            raise ValueError
        download_url = _trusted_bitrix_download_url(download_url, webhook=webhook)
        opener = urllib.request.build_opener(_BitrixSameOriginRedirectHandler(webhook=webhook))
        with opener.open(download_url, timeout=30) as response:
            body = response.read(settings.site_service_requests_max_file_bytes + 1)
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="bitrix_file_download_failed") from exc
    if len(body) > settings.site_service_requests_max_file_bytes:
        raise HTTPException(status_code=413, detail="conversation_file_too_large")
    return Response(
        content=body,
        media_type=file.mime_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(file.safe_filename)}",
            "Cache-Control": "private, no-store",
        },
    )
