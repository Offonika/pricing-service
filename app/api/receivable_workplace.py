from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Security
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, security
from app.core.config import get_settings
from app.infrastructure.db.engines import DatabaseNotConfiguredError, get_onec_engine
from app.schemas.management import (
    CounterpartyFolderRecommendationItem,
    CounterpartyFolderRecommendationResponse,
)
from app.schemas.receivable_workplace import (
    ReceivableWorkplaceActionRequest,
    ReceivableWorkplaceActionResponse,
    ReceivableWorkplaceMetaResponse,
    ReceivableWorkplaceResponse,
)
from app.services.bitrix_receivables_auth import verify_receivables_session_token
from app.services.counterparty_folder_recommendations import (
    STATUS_MOVE_RECOMMENDED,
    STATUS_NEEDS_REVIEW,
    STATUS_NO_OVERDUE,
    STATUS_OK,
    build_counterparty_folder_recommendations,
)
from app.services.receivable_workplace import (
    WorkplaceSortBy,
    WorkplaceSortDir,
    apply_receivable_workplace_action,
    build_receivable_workplace,
    build_receivable_workplace_meta,
)
from app.services.receivable_workplace_cache import (
    load_buyer_counterparty_refs,
    load_cached_folder_recommendation_report,
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


@page_router.get("/receivables/workplace", response_class=HTMLResponse, include_in_schema=False)
@page_router.get("/receivables/workplace/", response_class=HTMLResponse, include_in_schema=False)
@page_router.get(
    "/receivables/workplace/{path:path}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def receivable_workplace_page() -> HTMLResponse:
    return HTMLResponse(_read_index())


@dataclass(frozen=True)
class ReceivableWorkplaceAuthContext:
    actor: str
    source: Literal["internal", "bitrix"]
    access_level: Literal["full", "department"]
    department_refs: frozenset[str] | None = None

    @property
    def allowed_department_refs(self) -> frozenset[str] | None:
        if self.access_level == "full":
            return None
        return self.department_refs or frozenset()


def _management_internal_token() -> str | None:
    settings = get_settings()
    return (
        settings.management_internal_api_token
        or settings.counterparty_duplicate_internal_api_token
        or settings.return_scheme_internal_api_token
    )


def require_receivable_workplace_access(
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> ReceivableWorkplaceAuthContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="unauthorized")

    token = credentials.credentials
    internal_token = _management_internal_token()
    if internal_token and token == internal_token:
        return ReceivableWorkplaceAuthContext(
            actor="internal:management",
            source="internal",
            access_level="full",
            department_refs=None,
        )

    try:
        session = verify_receivables_session_token(token)
    except HTTPException as exc:
        if exc.status_code == 500:
            raise
        raise HTTPException(status_code=401, detail="unauthorized") from exc
    return ReceivableWorkplaceAuthContext(
        actor=session.actor,
        source="bitrix",
        access_level=session.access_level,
        department_refs=session.department_refs,
    )


def _build_onec_engine():
    try:
        return get_onec_engine()
    except DatabaseNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail="1C source is unavailable") from exc


def _department_allowed(
    department_ref: str | None,
    *,
    allowed_department_refs: frozenset[str] | None,
) -> bool:
    if allowed_department_refs is None:
        return True
    if not department_ref:
        return False
    allowed = {str(value).strip().casefold() for value in allowed_department_refs}
    return str(department_ref).strip().casefold() in allowed


def _filter_folder_report_for_access(
    report: dict,
    *,
    allowed_department_refs: frozenset[str] | None,
) -> dict:
    if allowed_department_refs is None:
        return report
    payload = [
        item
        for item in report["payload"]
        if _department_allowed(
            item.get("debt_department_ref"),
            allowed_department_refs=allowed_department_refs,
        )
        or _department_allowed(
            item.get("snapshot_department_ref"),
            allowed_department_refs=allowed_department_refs,
        )
    ]
    summary = dict(report["summary"])
    summary.update(
        {
            "total_count": len(payload),
            "move_recommended_count": sum(
                1 for item in payload if item.get("status") == STATUS_MOVE_RECOMMENDED
            ),
            "ok_count": sum(1 for item in payload if item.get("status") == STATUS_OK),
            "no_overdue_count": sum(
                1 for item in payload if item.get("status") == STATUS_NO_OVERDUE
            ),
            "needs_review_count": sum(
                1 for item in payload if item.get("status") == STATUS_NEEDS_REVIEW
            ),
            "total_open_debt": sum(
                (Decimal(str(item.get("current_balance") or "0")) for item in payload),
                Decimal("0"),
            ),
            "move_recommended_amount": sum(
                (
                    Decimal(str(item.get("current_balance") or "0"))
                    for item in payload
                    if item.get("status") == STATUS_MOVE_RECOMMENDED
                ),
                Decimal("0"),
            ),
        }
    )
    return {**report, "summary": summary, "payload": payload}


def _folder_candidate_limit(limit: int | None) -> int:
    requested_limit = limit or 100
    return min(max(requested_limit * 3, 100), 500)


@router.get("/workplace/meta", response_model=ReceivableWorkplaceMetaResponse)
def get_receivable_workplace_meta(
    date_value: date | None = Query(default=None, alias="date"),
    db: Session = Depends(get_db),
    access: ReceivableWorkplaceAuthContext = Depends(require_receivable_workplace_access),
) -> ReceivableWorkplaceMetaResponse:
    return build_receivable_workplace_meta(
        db,
        snapshot_date=date_value,
        allowed_department_refs=access.allowed_department_refs,
    )


@router.get("/workplace", response_model=ReceivableWorkplaceResponse)
def get_receivable_workplace(
    date_value: date = Query(alias="date"),
    department_ref: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    sort_by: WorkplaceSortBy = Query(default="balance"),
    sort_dir: WorkplaceSortDir = Query(default="desc"),
    db: Session = Depends(get_db),
    access: ReceivableWorkplaceAuthContext = Depends(require_receivable_workplace_access),
) -> ReceivableWorkplaceResponse:
    return build_receivable_workplace(
        db,
        snapshot_date=date_value,
        department_ref=department_ref,
        status=status,
        limit=limit,
        sort_by=sort_by,
        sort_dir=sort_dir,
        allowed_department_refs=access.allowed_department_refs,
    )


@router.get(
    "/workplace/folder-recommendations",
    response_model=CounterpartyFolderRecommendationResponse,
)
def get_receivable_workplace_folder_recommendations(
    date_value: date = Query(alias="date"),
    status: str | None = Query(
        default=None,
        pattern=(
            f"^({STATUS_MOVE_RECOMMENDED}|{STATUS_OK}|"
            f"{STATUS_NO_OVERDUE}|{STATUS_NEEDS_REVIEW})$"
        ),
    ),
    limit: int | None = Query(default=None, ge=1, le=10000),
    db: Session = Depends(get_db),
    access: ReceivableWorkplaceAuthContext = Depends(require_receivable_workplace_access),
) -> CounterpartyFolderRecommendationResponse:
    report = load_cached_folder_recommendation_report(
        db,
        snapshot_date=date_value,
        status=status,
        limit=limit,
        allowed_department_refs=access.allowed_department_refs,
    )
    if report is None:
        onec_engine = _build_onec_engine()
        try:
            buyer_refs = load_buyer_counterparty_refs(db, snapshot_date=date_value)
            report = build_counterparty_folder_recommendations(
                db,
                onec_engine=onec_engine,
                snapshot_date=date_value,
                limit=limit,
                status=status,
                candidate_limit=_folder_candidate_limit(limit),
                snapshot_department_refs=access.allowed_department_refs,
                counterparty_refs=buyer_refs,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except SQLAlchemyError as error:
            raise HTTPException(status_code=503, detail="1C source is unavailable") from error
        finally:
            onec_engine.dispose()

        report = _filter_folder_report_for_access(
            report,
            allowed_department_refs=access.allowed_department_refs,
        )
        report["source_status"] = "fallback_live"
    payload = [
        CounterpartyFolderRecommendationItem.model_validate(item) for item in report["payload"]
    ]
    source_snapshot_count = int(report["summary"].get("source_snapshot_count") or 0)
    summary = dict(report["summary"])
    if report.get("computed_at") is not None:
        summary["computed_at"] = report["computed_at"]
    summary["source_status"] = report.get("source_status") or "ready"
    response_source_status = (
        str(report.get("source_status") or "ready") if source_snapshot_count else "empty"
    )
    return CounterpartyFolderRecommendationResponse(
        as_of=report["snapshot_date"],
        freshness_status="fresh" if source_snapshot_count else "missing",
        source_status=response_source_status,
        report_revision=report["report_revision"],
        summary=summary,
        payload=payload,
    )


@router.patch(
    "/workplace/{counterparty_ref}",
    response_model=ReceivableWorkplaceActionResponse,
)
def update_receivable_workplace_item(
    counterparty_ref: str,
    payload: ReceivableWorkplaceActionRequest,
    date_value: date = Query(alias="date"),
    db: Session = Depends(get_db),
    access: ReceivableWorkplaceAuthContext = Depends(require_receivable_workplace_access),
) -> ReceivableWorkplaceActionResponse:
    try:
        result = apply_receivable_workplace_action(
            db,
            snapshot_date=date_value,
            counterparty_ref=counterparty_ref,
            payload=payload,
            allowed_department_refs=access.allowed_department_refs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="receivable workplace item not found")
    db.commit()
    return result
