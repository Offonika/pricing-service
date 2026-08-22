from typing import Annotated, Generator

from fastapi import Depends, Header, HTTPException, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.infrastructure.db import (
    SqlAlchemyUnitOfWork,
    get_application_engine,
    get_application_session_factory,
)
from app.services.site_service_requests_auth import (
    SiteServiceRequestAuthError,
    VerifiedSiteRequest,
    verify_site_request,
)

security = HTTPBearer(auto_error=False)


def _require_bearer_token(
    credentials: HTTPAuthorizationCredentials | None,
    expected: str | None,
    *,
    missing_detail: str = "internal token not configured",
) -> str:
    if not expected:
        raise HTTPException(status_code=401, detail=missing_detail)
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="unauthorized")
    if credentials.credentials != expected:
        raise HTTPException(status_code=401, detail="unauthorized")
    return credentials.credentials


# Compatibility alias retained for API/tests that import the historic symbol.
get_engine = get_application_engine


def get_db() -> Generator[Session, None, None]:
    session = get_application_session_factory()()
    try:
        yield session
    finally:
        session.close()


def get_uow() -> Generator[SqlAlchemyUnitOfWork, None, None]:
    with SqlAlchemyUnitOfWork() as unit_of_work:
        yield unit_of_work


def get_site_service_request_settings() -> Settings:
    return get_settings()


async def require_site_service_request_signature(
    request: Request,
    timestamp_header: Annotated[
        str,
        Header(alias="X-MM-Site-Timestamp"),
    ],
    nonce_header: Annotated[
        str,
        Header(alias="X-MM-Site-Nonce"),
    ],
    content_sha256_header: Annotated[
        str,
        Header(alias="X-MM-Site-Content-SHA256"),
    ],
    signature_header: Annotated[
        str,
        Header(alias="X-MM-Site-Signature"),
    ],
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_site_service_request_settings),
) -> VerifiedSiteRequest:
    try:
        body_limit = _site_service_request_body_limit(request, settings=settings)
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="request_size_invalid") from exc
            if declared_length < 0:
                raise HTTPException(status_code=422, detail="request_size_invalid")
            if declared_length > body_limit:
                raise HTTPException(status_code=413, detail="request_body_too_large")
        body = await request.body()
        if len(body) > body_limit:
            raise HTTPException(status_code=413, detail="request_body_too_large")
        verified = verify_site_request(
            db,
            method=request.method,
            path=request.url.path,
            body=body,
            timestamp_header=timestamp_header,
            nonce_header=nonce_header,
            content_sha256_header=content_sha256_header,
            signature_header=signature_header,
            settings=settings,
        )
        db.commit()
        return verified
    except SiteServiceRequestAuthError as exc:
        db.rollback()
        if exc.code == "auth_not_configured":
            raise HTTPException(
                status_code=503,
                detail="site_service_requests_auth_not_configured",
            ) from exc
        if exc.code == "nonce_replay":
            raise HTTPException(status_code=409, detail="nonce_replay") from exc
        raise HTTPException(status_code=401, detail="unauthorized") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="site_service_requests_auth_unavailable",
        ) from exc


def _site_service_request_body_limit(request: Request, *, settings: Settings) -> int:
    return _site_service_request_body_limit_for_path(
        method=request.method,
        path=request.url.path,
        settings=settings,
    )


def _site_service_request_body_limit_for_path(
    *,
    method: str,
    path: str,
    settings: Settings,
) -> int:
    if method.upper() == "PUT" and "/files/" in path:
        return settings.site_service_requests_max_file_bytes
    if method.upper() == "POST" and path.endswith("/events"):
        return settings.site_service_requests_max_event_body_bytes
    if method.upper() == "POST" and path.endswith("/ack"):
        return settings.site_service_requests_max_ack_body_bytes
    return settings.site_service_requests_max_ack_body_bytes


class SiteServiceRequestBodyLimitMiddleware:
    """Reject oversized site requests while ASGI chunks are still arriving."""

    def __init__(self, app, *, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send) -> None:
        path = str(scope.get("path") or "")
        if scope.get("type") != "http" or not path.startswith(
            "/api/internal/site-service-requests/"
        ):
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "GET")
        limit = _site_service_request_body_limit_for_path(
            method=method,
            path=path,
            settings=self.settings,
        )
        headers = {
            key.lower(): value
            for key, value in scope.get("headers", [])
            if isinstance(key, bytes) and isinstance(value, bytes)
        }
        raw_content_length = headers.get(b"content-length")
        if raw_content_length is not None:
            try:
                declared_length = int(raw_content_length)
            except ValueError:
                await JSONResponse(
                    status_code=422,
                    content={"detail": "request_size_invalid"},
                )(scope, receive, send)
                return
            if declared_length < 0:
                await JSONResponse(
                    status_code=422,
                    content={"detail": "request_size_invalid"},
                )(scope, receive, send)
                return
            if declared_length > limit:
                await JSONResponse(
                    status_code=413,
                    content={"detail": "request_body_too_large"},
                )(scope, receive, send)
                return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    raise _SiteServiceRequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _SiteServiceRequestBodyTooLarge:
            await JSONResponse(
                status_code=413,
                content={"detail": "request_body_too_large"},
            )(scope, receive, send)


class _SiteServiceRequestBodyTooLarge(Exception):
    pass


def require_management_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    expected = (
        settings.management_internal_api_token
        or settings.counterparty_duplicate_internal_api_token
        or settings.return_scheme_internal_api_token
    )
    return _require_bearer_token(credentials, expected)


def require_orchestration_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    return _require_bearer_token(
        credentials,
        settings.orchestration_internal_api_token,
        missing_detail="orchestration internal token not configured",
    )


def require_sms_journal_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    return _require_bearer_token(
        credentials,
        settings.sms_journal_internal_api_token,
        missing_detail="SMS journal internal token not configured",
    )


def require_weekly_kpi_ingest_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    return _require_bearer_token(
        credentials,
        settings.weekly_kpi_ingest_internal_api_token,
        missing_detail="weekly KPI ingest token not configured",
    )


def require_logistics_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    return _require_bearer_token(
        credentials,
        settings.logistics_internal_api_token,
        missing_detail="logistics internal token not configured",
    )


def require_expertise_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    return _require_bearer_token(
        credentials,
        settings.expertise_internal_api_token,
        missing_detail="expertise internal token not configured",
    )


def require_site_defect_archive_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    expected = (
        settings.site_defect_archive_internal_api_token
        or settings.expertise_internal_api_token
        or settings.management_internal_api_token
    )
    return _require_bearer_token(
        credentials,
        expected,
        missing_detail="site defect archive internal token not configured",
    )


def require_card_balance_reconciliation_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    expected = (
        settings.card_balance_reconciliation_internal_api_token
        or settings.management_internal_api_token
    )
    return _require_bearer_token(
        credentials,
        expected,
        missing_detail="card balance reconciliation internal token not configured",
    )


def require_bank_payments_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    expected = settings.bank_payments_internal_api_token or settings.management_internal_api_token
    return _require_bearer_token(
        credentials,
        expected,
        missing_detail="bank payments internal token not configured",
    )


def require_order_fulfillment_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    expected = (
        settings.order_fulfillment_internal_api_token
        or settings.logistics_internal_api_token
        or settings.management_internal_api_token
    )
    return _require_bearer_token(
        credentials,
        expected,
        missing_detail="order fulfillment internal token not configured",
    )


def require_order_payment_control_internal_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    settings = get_settings()
    expected = (
        settings.order_payment_control_internal_api_token
        or settings.order_fulfillment_internal_api_token
        or settings.management_internal_api_token
    )
    return _require_bearer_token(
        credentials,
        expected,
        missing_detail="order payment control internal token not configured",
    )
