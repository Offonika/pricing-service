from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from app.core.config import get_settings
from app.schemas.procurement_labels import (
    ProcurementCertificationDocsGenerateResponse,
    ProcurementLabelApproveResponse,
    ProcurementLabelGenerateRequest,
    ProcurementLabelGenerateResponse,
    ProcurementLabelOrderPreview,
    ProcurementLabelsSessionRequest,
    ProcurementLabelsSessionResponse,
    ProcurementLabelsUser,
)
from app.services.bitrix_procurement_labels_auth import (
    ProcurementLabelsSession,
    create_procurement_labels_session_token,
    ensure_bitrix_launch_allowed,
    ensure_bitrix_user_allowed,
    load_bitrix_current_user,
    verify_procurement_labels_session,
)
from app.services.procurement_labels import (
    approve_zip,
    build_preview,
    generate_certification_docs_zip,
    generate_zip,
    send_zip_to_factory,
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
    return "<!doctype html><html><body>Procurement labels UI is not built</body></html>"


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


def _json_form_value(values: dict[str, list[str]], key: str) -> dict[str, object]:
    raw = _first(values, key)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


async def _bitrix_launch_payload(request: Request) -> dict[str, object | None]:
    query = dict(request.query_params)
    form_values: dict[str, list[str]] = {}
    if request.method == "POST":
        body = await request.body()
        form_values = urllib.parse.parse_qs(body.decode("utf-8", errors="ignore"))

    access_token = _first(form_values, "AUTH_ID") or _first(form_values, "access_token")
    member_id = _first(form_values, "member_id")
    domain = query.get("DOMAIN") or _first(form_values, "DOMAIN") or query.get("domain")
    placement = _first(form_values, "PLACEMENT") or query.get("PLACEMENT")
    placement_options = _json_form_value(form_values, "PLACEMENT_OPTIONS")
    if not placement_options:
        placement_options = _json_form_value(form_values, "options")
    if "itemId" in query and "ID" not in placement_options:
        placement_options["ID"] = query["itemId"]
    return {
        "access_token": access_token,
        "domain": domain,
        "member_id": member_id,
        "placement": placement,
        "placement_options": placement_options,
    }


def _inject_launch_payload(index_html: str, payload: dict[str, object | None]) -> str:
    raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    script = f"<script>window.__MM_BITRIX_LAUNCH__={raw_json};</script>"
    if "</head>" in index_html:
        return index_html.replace("</head>", f"{script}</head>", 1)
    return f"{script}{index_html}"


@page_router.api_route(
    "/bitrix/procurement-labels",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
@page_router.api_route(
    "/bitrix/procurement-labels/",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
@page_router.api_route(
    "/bitrix/procurement-labels/{path:path}",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def bitrix_procurement_labels_page(request: Request) -> HTMLResponse:
    payload = await _bitrix_launch_payload(request)
    html = _inject_launch_payload(_read_index(), payload)
    return HTMLResponse(html)


@router.post("/procurement-labels/session", response_model=ProcurementLabelsSessionResponse)
def create_bitrix_procurement_labels_session(
    payload: ProcurementLabelsSessionRequest,
) -> ProcurementLabelsSessionResponse:
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
    token, expires_at = create_procurement_labels_session_token(
        domain=domain,
        member_id=member_id,
        user_id=user_id,
        user_name=user.name,
        settings=settings,
    )
    return ProcurementLabelsSessionResponse(
        session_token=token,
        expires_at=expires_at,
        expires_in=settings.procurement_labels_bitrix_session_ttl_seconds,
        user=ProcurementLabelsUser(user_id=user_id, name=user.name),
    )


@router.get(
    "/procurement-labels/orders/{item_id}/preview",
    response_model=ProcurementLabelOrderPreview,
)
def preview_procurement_labels(
    item_id: str,
    _session: ProcurementLabelsSession = Depends(verify_procurement_labels_session),
) -> ProcurementLabelOrderPreview:
    return build_preview(item_id)


@router.post(
    "/procurement-labels/orders/{item_id}/generate",
    response_model=ProcurementLabelGenerateResponse,
)
def generate_procurement_labels(
    item_id: str,
    payload: ProcurementLabelGenerateRequest,
    request: Request,
    _session: ProcurementLabelsSession = Depends(verify_procurement_labels_session),
) -> ProcurementLabelGenerateResponse:
    base_url = str(request.base_url).rstrip("/")
    return generate_zip(item_id, base_url=base_url, dry_run=payload.dry_run)


@router.post(
    "/procurement-labels/orders/{item_id}/certification-docs/generate",
    response_model=ProcurementCertificationDocsGenerateResponse,
)
def generate_procurement_certification_docs(
    item_id: str,
    payload: ProcurementLabelGenerateRequest,
    request: Request,
    _session: ProcurementLabelsSession = Depends(verify_procurement_labels_session),
) -> ProcurementCertificationDocsGenerateResponse:
    base_url = str(request.base_url).rstrip("/")
    return generate_certification_docs_zip(item_id, base_url=base_url, dry_run=payload.dry_run)


@router.post(
    "/procurement-labels/orders/{item_id}/approve",
    response_model=ProcurementLabelApproveResponse,
)
def approve_procurement_labels(
    item_id: str,
    _session: ProcurementLabelsSession = Depends(verify_procurement_labels_session),
) -> ProcurementLabelApproveResponse:
    version, zip_url = approve_zip(item_id)
    return ProcurementLabelApproveResponse(
        item_id=item_id,
        status="approved",
        artifact_version=version,
        zip_url=zip_url,
    )


@router.post(
    "/procurement-labels/orders/{item_id}/send-to-factory",
    response_model=ProcurementLabelApproveResponse,
)
def send_procurement_labels_to_factory(
    item_id: str,
    _session: ProcurementLabelsSession = Depends(verify_procurement_labels_session),
) -> ProcurementLabelApproveResponse:
    version, zip_url = send_zip_to_factory(item_id)
    return ProcurementLabelApproveResponse(
        item_id=item_id,
        status="sent_to_factory",
        artifact_version=version,
        zip_url=zip_url,
    )


@router.get("/procurement-labels/artifacts/{filename}", include_in_schema=False)
def download_procurement_label_artifact(
    filename: str,
    _session: ProcurementLabelsSession = Depends(verify_procurement_labels_session),
) -> FileResponse:
    if "/" in filename or "\\" in filename or not filename.endswith(".zip"):
        raise HTTPException(status_code=404, detail="artifact not found")
    artifact_dir = Path(get_settings().procurement_labels_artifact_dir)
    candidates = sorted(artifact_dir.glob(filename))
    if not candidates:
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(
        candidates[-1],
        media_type="application/zip",
        filename=candidates[-1].name,
    )
