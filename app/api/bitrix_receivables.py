from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.api.receivable_workplace import (
    ReceivableWorkplaceAuthContext,
    require_receivable_workplace_access,
)
from app.core.config import get_settings
from app.schemas.bitrix_receivables import (
    BitrixReceivablesSessionRequest,
    BitrixReceivablesSessionResponse,
    BitrixReceivablesUser,
)
from app.services.bitrix_receivables_auth import (
    create_receivables_session_token,
    diagnose_receivables_access,
    ensure_bitrix_launch_allowed,
    load_bitrix_current_user,
    resolve_receivables_access,
)

router = APIRouter()
page_router = APIRouter()

_INDEX_PATHS = (
    Path(__file__).resolve().parents[2] / "ui" / "dist" / "index.html",
    Path("/var/www/pricing-service/index.html"),
)


def _read_index() -> str:
    for path in _INDEX_PATHS:
        if path.exists():
            return _rewrite_index_asset_paths(path.read_text(encoding="utf-8"))
    return "<!doctype html><html><body>Receivables workplace UI is not built</body></html>"


def _rewrite_index_asset_paths(index_html: str) -> str:
    return (
        index_html.replace('src="./assets/', 'src="/assets/')
        .replace('href="./assets/', 'href="/assets/')
        .replace('href="./vite.svg"', 'href="/vite.svg"')
    )


def _first(values: dict[str, list[str]], key: str) -> str | None:
    items = values.get(key)
    if not items:
        return None
    value = str(items[0] or "").strip()
    return value or None


async def _bitrix_launch_payload(request: Request) -> dict[str, str | None]:
    query = dict(request.query_params)
    form_values: dict[str, list[str]] = {}
    if request.method == "POST":
        body = await request.body()
        form_values = urllib.parse.parse_qs(body.decode("utf-8", errors="ignore"))

    access_token = _first(form_values, "AUTH_ID") or _first(form_values, "access_token")
    member_id = _first(form_values, "member_id")
    domain = query.get("DOMAIN") or _first(form_values, "DOMAIN") or query.get("domain")
    return {
        "access_token": access_token,
        "domain": domain,
        "member_id": member_id,
    }


def _inject_launch_payload(index_html: str, payload: dict[str, str | None]) -> str:
    raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    script = f"<script>window.__MM_BITRIX_LAUNCH__={raw_json};</script>"
    if "</head>" in index_html:
        return index_html.replace("</head>", f"{script}</head>", 1)
    return f"{script}{index_html}"


@page_router.api_route(
    "/bitrix/receivables",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
@page_router.api_route(
    "/bitrix/receivables/",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
@page_router.api_route(
    "/bitrix/receivables/{path:path}",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def bitrix_receivables_page(request: Request) -> HTMLResponse:
    payload = await _bitrix_launch_payload(request)
    html = _inject_launch_payload(_read_index(), payload)
    return HTMLResponse(html)


@router.post("/bitrix/receivables/session", response_model=BitrixReceivablesSessionResponse)
def create_bitrix_receivables_session(
    payload: BitrixReceivablesSessionRequest,
    db: Session = Depends(get_db),
) -> BitrixReceivablesSessionResponse:
    settings = get_settings()
    domain, member_id = ensure_bitrix_launch_allowed(
        domain=payload.domain,
        member_id=payload.member_id,
        settings=settings,
    )
    user = load_bitrix_current_user(
        domain=domain,
        access_token=payload.access_token,
        settings=settings,
    )
    access = resolve_receivables_access(db, bitrix_user_id=user.user_id, settings=settings)
    token, expires_at = create_receivables_session_token(
        domain=domain,
        member_id=member_id,
        user_id=user.user_id,
        user_name=user.name,
        access=access,
        settings=settings,
    )
    return BitrixReceivablesSessionResponse(
        session_token=token,
        expires_at=expires_at,
        expires_in=settings.receivable_workplace_bitrix_session_ttl_seconds,
        user=BitrixReceivablesUser(user_id=user.user_id, name=user.name),
        access_level=access.access_level,
        department_refs=sorted(access.department_refs),
    )


@router.get("/bitrix/receivables/access-diagnostics")
def get_bitrix_receivables_access_diagnostics(
    user_id: str = Query(min_length=1),
    db: Session = Depends(get_db),
    access: ReceivableWorkplaceAuthContext = Depends(require_receivable_workplace_access),
) -> dict:
    if access.access_level != "full":
        raise HTTPException(status_code=403, detail="diagnostics require full receivables access")
    return diagnose_receivables_access(db, bitrix_user_id=user_id, settings=get_settings())
