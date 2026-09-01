from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.config import get_settings
from app.schemas.bitrix_matching import (
    BitrixMatchingSessionRequest,
    BitrixMatchingSessionResponse,
    BitrixMatchingUser,
)
from app.services.bitrix_matching_auth import (
    create_matching_session_token,
    ensure_bitrix_launch_allowed,
    ensure_bitrix_user_allowed,
    load_bitrix_current_user,
)

router = APIRouter()
page_router = APIRouter()

# Сборка активного релиза имеет приоритет: легаси-каталог /var/www не обновляется
# при выкладке и иначе месяцами подменяет свежий ui/dist старым билдом.
_INDEX_PATHS = (
    Path(__file__).resolve().parents[2] / "ui" / "dist" / "index.html",
    Path("/var/www/pricing-service/index.html"),
)


def _read_matching_index() -> str:
    for path in _INDEX_PATHS:
        if path.exists():
            return _inline_matching_assets(path.read_text(encoding="utf-8"), index_path=path)
    return "<!doctype html><html><body>Matching UI is not built</body></html>"


def _inline_matching_assets(index_html: str, *, index_path: Path) -> str:
    assets_root = index_path.parent

    def inline_script(match: re.Match[str]) -> str:
        src = match.group("src")
        if not src.startswith("./assets/"):
            return match.group(0)
        asset_path = assets_root / src.removeprefix("./")
        if not asset_path.exists():
            return match.group(0)
        script = asset_path.read_text(encoding="utf-8").replace("</script", "<\\/script")
        return f'<script type="module">{script}</script>'

    def inline_stylesheet(match: re.Match[str]) -> str:
        href = match.group("href")
        if not href.startswith("./assets/"):
            return match.group(0)
        asset_path = assets_root / href.removeprefix("./")
        if not asset_path.exists():
            return match.group(0)
        stylesheet = asset_path.read_text(encoding="utf-8").replace("</style", "<\\/style")
        return f"<style>{stylesheet}</style>"

    html = re.sub(
        r'<script(?P<attrs>[^>]*?)\ssrc="(?P<src>\./assets/[^"]+\.js)"(?P<tail>[^>]*)></script>',
        inline_script,
        index_html,
    )
    return re.sub(
        r'<link(?P<attrs>[^>]*?)\shref="(?P<href>\./assets/[^"]+\.css)"(?P<tail>[^>]*)>',
        inline_stylesheet,
        html,
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
    "/bitrix/matching",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
@page_router.api_route(
    "/bitrix/matching/",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
@page_router.api_route(
    "/bitrix/matching/{path:path}",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def bitrix_matching_page(request: Request) -> HTMLResponse:
    payload = await _bitrix_launch_payload(request)
    html = _inject_launch_payload(_read_matching_index(), payload)
    return HTMLResponse(html)


@router.post("/bitrix/matching/session", response_model=BitrixMatchingSessionResponse)
def create_bitrix_matching_session(
    payload: BitrixMatchingSessionRequest,
) -> BitrixMatchingSessionResponse:
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
    user_id = ensure_bitrix_user_allowed(user.user_id, settings=settings)
    token, expires_at = create_matching_session_token(
        domain=domain,
        member_id=member_id,
        user_id=user_id,
        user_name=user.name,
        settings=settings,
    )
    return BitrixMatchingSessionResponse(
        session_token=token,
        expires_at=expires_at,
        expires_in=settings.matching_bitrix_session_ttl_seconds,
        user=BitrixMatchingUser(user_id=user_id, name=user.name),
    )
