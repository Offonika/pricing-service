from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    ReceivableBalanceSnapshot,
    ReceivableCase,
    ReceivableFolderRecommendationCache,
    ReceivableOpenDebtCache,
)
from app.services.counterparty_folder_recommendations import (
    OPEN_DEBT_DIAGNOSTIC_MATCHED,
    QUEUE_ACTIONABLE,
    QUEUE_ALL,
    QUEUE_BUSINESS_REVIEW,
    QUEUE_DATA_QUALITY,
    QUEUE_EXCLUDED,
    STATUS_MOVE_RECOMMENDED,
    STATUS_NEEDS_REVIEW,
    STATUS_NO_OVERDUE,
    STATUS_OK,
    build_open_debt_documents_by_counterparty,
    classify_open_debt_documents,
    enrich_folder_recommendation_item,
    evaluate_open_debt_source_freshness,
)
from app.services.receivables import CASE_BUYERS


@dataclass(frozen=True)
class CachedOpenDebtDocuments:
    documents_by_counterparty: dict[str, list[dict[str, Any]]]
    source_status: str
    computed_at: datetime | None = None
    cached_counterparty_count: int = 0
    missing_counterparty_count: int = 0
    source_max_document_date: datetime | None = None
    source_lag_days: int | None = None
    hidden_counterparty_refs: frozenset[str] = frozenset()
    document_mismatch_counterparty_refs: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ReceivableDepartmentOption:
    department_ref: str
    department_name: str


def _ref_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


def _money(value: Any) -> Decimal:
    return Decimal(str(value or "0")).quantize(Decimal("0.01"))


def latest_receivable_snapshot_date(
    session: Session,
    *,
    allowed_department_refs: set[str] | frozenset[str] | None = None,
) -> date | None:
    if allowed_department_refs is None:
        return session.scalar(
            select(func.max(ReceivableCase.snapshot_date)).where(
                ReceivableCase.segment == CASE_BUYERS,
                ReceivableCase.current_balance > 0,
            )
        )
    allowed = {_ref_key(value) for value in allowed_department_refs if value}
    if not allowed:
        return None
    dates = (
        session.execute(
            select(ReceivableCase.snapshot_date).where(
                ReceivableCase.segment == CASE_BUYERS,
                ReceivableCase.current_balance > 0,
                ReceivableCase.department_ref.is_not(None),
            )
        )
        .scalars()
        .all()
    )
    if not dates:
        return None
    refs_by_date = session.execute(
        select(ReceivableCase.snapshot_date, ReceivableCase.department_ref).where(
            ReceivableCase.segment == CASE_BUYERS,
            ReceivableCase.current_balance > 0,
        )
    ).all()
    allowed_dates = [
        snapshot_date
        for snapshot_date, department_ref in refs_by_date
        if _ref_key(department_ref) in allowed
    ]
    return max(allowed_dates) if allowed_dates else None


def receivable_department_options(
    session: Session,
    *,
    snapshot_date: date | None,
    allowed_department_refs: set[str] | frozenset[str] | None = None,
) -> list[ReceivableDepartmentOption]:
    if snapshot_date is None:
        return []
    rows = session.execute(
        select(ReceivableCase.department_ref, ReceivableCase.department_name).where(
            ReceivableCase.snapshot_date == snapshot_date,
            ReceivableCase.segment == CASE_BUYERS,
            ReceivableCase.current_balance > 0,
            ReceivableCase.department_ref.is_not(None),
        )
    ).all()
    allowed = None
    if allowed_department_refs is not None:
        allowed = {_ref_key(value) for value in allowed_department_refs if value}
    by_ref: dict[str, str] = {}
    for department_ref, department_name in rows:
        ref = str(department_ref or "").strip()
        if not ref:
            continue
        if allowed is not None and _ref_key(ref) not in allowed:
            continue
        by_ref.setdefault(ref, str(department_name or ref).strip() or ref)
    return [
        ReceivableDepartmentOption(department_ref=ref, department_name=name)
        for ref, name in sorted(by_ref.items(), key=lambda item: item[1])
    ]


def load_cached_open_debt_documents(
    session: Session,
    *,
    snapshot_date: date,
    counterparty_refs: Sequence[str],
) -> CachedOpenDebtDocuments:
    expected_keys = {_ref_key(value) for value in counterparty_refs if value}
    if not expected_keys:
        return CachedOpenDebtDocuments(documents_by_counterparty={}, source_status="empty")
    rows = (
        session.execute(
            select(ReceivableOpenDebtCache).where(
                ReceivableOpenDebtCache.snapshot_date == snapshot_date,
                ReceivableOpenDebtCache.counterparty_ref.in_(list(counterparty_refs)),
            )
        )
        .scalars()
        .all()
    )
    documents_by_counterparty = {
        _ref_key(row.counterparty_ref): (
            list(row.documents or []) if row.source_status == "ready" else []
        )
        for row in rows
    }
    missing_count = len(expected_keys - set(documents_by_counterparty))
    computed_at = max((row.computed_at for row in rows), default=None)
    stale_rows = [row for row in rows if row.source_status == "source_stale"]
    hidden_counterparty_refs = frozenset(
        _ref_key(row.counterparty_ref)
        for row in rows
        if row.source_status in {"source_stale", "document_mismatch"}
    )
    document_mismatch_counterparty_refs = frozenset(
        _ref_key(row.counterparty_ref) for row in rows if row.source_status == "document_mismatch"
    )
    freshness = evaluate_open_debt_source_freshness(
        session,
        snapshot_date=snapshot_date,
    )
    if stale_rows:
        source_status = "source_stale"
    elif not rows:
        source_status = "cache_missing"
    elif missing_count:
        source_status = "cache_partial"
    else:
        source_status = "cache_ready"
    return CachedOpenDebtDocuments(
        documents_by_counterparty=documents_by_counterparty,
        source_status=source_status,
        computed_at=computed_at,
        cached_counterparty_count=len(rows),
        missing_counterparty_count=missing_count,
        source_max_document_date=freshness.source_max_document_date,
        source_lag_days=freshness.source_lag_days,
        hidden_counterparty_refs=hidden_counterparty_refs,
        document_mismatch_counterparty_refs=document_mismatch_counterparty_refs,
    )


def rebuild_open_debt_cache(
    session: Session,
    *,
    snapshot_date: date,
    onec_engine=None,
    include_onec_enrichment: bool = False,
) -> dict[str, Any]:
    snapshots = (
        session.execute(
            select(ReceivableBalanceSnapshot).where(
                ReceivableBalanceSnapshot.snapshot_date == snapshot_date,
                ReceivableBalanceSnapshot.current_balance > Decimal("0"),
            )
        )
        .scalars()
        .all()
    )
    freshness = evaluate_open_debt_source_freshness(
        session,
        snapshot_date=snapshot_date,
    )
    active_counterparty_refs = {snapshot.counterparty_ref for snapshot in snapshots}
    stale_cache_delete = delete(ReceivableOpenDebtCache).where(
        ReceivableOpenDebtCache.snapshot_date == snapshot_date
    )
    if active_counterparty_refs:
        stale_cache_delete = stale_cache_delete.where(
            ReceivableOpenDebtCache.counterparty_ref.not_in(active_counterparty_refs)
        )
    deleted_count = session.execute(stale_cache_delete).rowcount or 0
    open_debt_diagnostics: dict[str, Any] = {}
    documents_by_counterparty = (
        build_open_debt_documents_by_counterparty(
            session,
            onec_engine=onec_engine,
            snapshots=snapshots,
            snapshot_date=snapshot_date,
            include_onec_enrichment=include_onec_enrichment,
            diagnostics=open_debt_diagnostics,
        )
        if freshness.source_status == "cache_ready"
        else {}
    )
    now = datetime.utcnow()
    updated_count = 0
    diagnostic_counts: Counter[str] = Counter()
    statement_sale_counts = open_debt_diagnostics.get("statement_sale_counts") or {}
    for snapshot in snapshots:
        key = _ref_key(snapshot.counterparty_ref)
        documents = documents_by_counterparty.get(key, [])
        document_diagnostic = (
            classify_open_debt_documents(
                documents,
                current_balance=snapshot.current_balance,
                statement_sale_count=int(statement_sale_counts.get(key) or 0),
            )
            if freshness.source_status == "cache_ready"
            else freshness.source_status
        )
        diagnostic_counts[document_diagnostic] += 1
        document_amount_mismatch = document_diagnostic != OPEN_DEBT_DIAGNOSTIC_MATCHED
        row = session.scalar(
            select(ReceivableOpenDebtCache).where(
                ReceivableOpenDebtCache.snapshot_date == snapshot_date,
                ReceivableOpenDebtCache.counterparty_ref == snapshot.counterparty_ref,
            )
        )
        if row is None:
            row = ReceivableOpenDebtCache(
                snapshot_date=snapshot_date,
                counterparty_ref=snapshot.counterparty_ref,
                department_ref=snapshot.department_ref,
                documents=[],
            )
            session.add(row)
        row.department_ref = snapshot.department_ref
        row.source_status = (
            "source_stale"
            if freshness.source_status == "source_stale"
            else "document_mismatch" if document_amount_mismatch else "ready"
        )
        row.documents = _json_safe([] if document_amount_mismatch else documents)
        row.computed_at = now
        updated_count += 1
    return {
        "snapshot_date": snapshot_date,
        "source_snapshot_count": len(snapshots),
        "updated_count": updated_count,
        "deleted_count": deleted_count,
        "computed_at": now,
        "source_status": freshness.source_status,
        "source_max_document_date": freshness.source_max_document_date,
        "source_lag_days": freshness.source_lag_days,
        "document_diagnostic_counts": dict(sorted(diagnostic_counts.items())),
        "document_mismatch_count": sum(
            count
            for diagnostic, count in diagnostic_counts.items()
            if diagnostic != OPEN_DEBT_DIAGNOSTIC_MATCHED
        ),
        "revealed_document_mismatch_count": 0,
        "extra_cache_rows": 0,
    }


def _filter_folder_payload_for_access(
    payload: list[dict[str, Any]],
    *,
    allowed_department_refs: set[str] | frozenset[str] | None,
) -> list[dict[str, Any]]:
    if allowed_department_refs is None:
        return payload
    allowed = {_ref_key(value) for value in allowed_department_refs if value}
    return [
        item
        for item in payload
        if _ref_key(item.get("debt_department_ref")) in allowed
        or _ref_key(item.get("snapshot_department_ref")) in allowed
    ]


def _folder_summary(
    payload: list[dict[str, Any]],
    *,
    source_snapshot_count: int = 0,
    queue_population: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    status_counts = Counter(str(item.get("status") or "") for item in payload)
    queue_counts = Counter(str(item.get("queue") or "") for item in (queue_population or payload))
    return {
        "total_count": len(payload),
        "source_snapshot_count": source_snapshot_count,
        "move_recommended_count": status_counts[STATUS_MOVE_RECOMMENDED],
        "ok_count": status_counts[STATUS_OK],
        "no_overdue_count": status_counts[STATUS_NO_OVERDUE],
        "needs_review_count": status_counts[STATUS_NEEDS_REVIEW],
        "queue_counts": dict(sorted(queue_counts.items())),
        "actionable_count": queue_counts[QUEUE_ACTIONABLE],
        "business_review_count": queue_counts[QUEUE_BUSINESS_REVIEW],
        "data_quality_count": queue_counts[QUEUE_DATA_QUALITY],
        "excluded_count": queue_counts[QUEUE_EXCLUDED],
        "document_mismatch_count": sum(
            item.get("open_debt_source_status") == "document_mismatch" for item in payload
        ),
        "total_open_debt": sum(
            (_money(item.get("current_balance")) for item in payload),
            Decimal("0.00"),
        ),
        "move_recommended_amount": sum(
            (
                _money(item.get("current_balance"))
                for item in payload
                if item.get("status") == STATUS_MOVE_RECOMMENDED
            ),
            Decimal("0.00"),
        ),
    }


def cache_folder_recommendation_report(
    session: Session,
    *,
    report: dict[str, Any],
    status_scope: str = "all",
) -> ReceivableFolderRecommendationCache:
    snapshot_date = report["snapshot_date"]
    row = session.scalar(
        select(ReceivableFolderRecommendationCache).where(
            ReceivableFolderRecommendationCache.snapshot_date == snapshot_date,
            ReceivableFolderRecommendationCache.status_scope == status_scope,
        )
    )
    if row is None:
        row = ReceivableFolderRecommendationCache(
            snapshot_date=snapshot_date,
            status_scope=status_scope,
            report_revision=str(report.get("report_revision") or ""),
            summary={},
            payload=[],
        )
        session.add(row)
    row.report_revision = str(report.get("report_revision") or "")
    row.summary = _json_safe(dict(report.get("summary") or {}))
    row.payload = _json_safe(list(report.get("payload") or []))
    row.source_status = str(report.get("source_status") or "cache_ready")
    row.computed_at = datetime.utcnow()
    return row


def load_cached_folder_recommendation_report(
    session: Session,
    *,
    snapshot_date: date,
    status: str | None = None,
    queue: str = QUEUE_ALL,
    limit: int | None = None,
    allowed_department_refs: set[str] | frozenset[str] | None = None,
) -> dict[str, Any] | None:
    row = session.scalar(
        select(ReceivableFolderRecommendationCache).where(
            ReceivableFolderRecommendationCache.snapshot_date == snapshot_date,
            ReceivableFolderRecommendationCache.status_scope == "all",
        )
    )
    if row is None:
        return None
    allowed_queues = {
        QUEUE_ACTIONABLE,
        QUEUE_BUSINESS_REVIEW,
        QUEUE_DATA_QUALITY,
        QUEUE_EXCLUDED,
        QUEUE_ALL,
    }
    if queue not in allowed_queues:
        raise ValueError(f"unsupported queue: {queue}")
    source_status = "source_stale" if row.source_status == "source_stale" else "cache_ready"
    payload = _filter_folder_payload_for_access(
        [
            enrich_folder_recommendation_item(item, source_status=source_status)
            for item in list(row.payload or [])
        ],
        allowed_department_refs=allowed_department_refs,
    )
    if status:
        payload = [item for item in payload if item.get("status") == status]
    queue_population = list(payload)
    if queue != QUEUE_ALL:
        payload = [item for item in payload if item.get("queue") == queue]
    if limit is not None:
        payload = payload[:limit]
    source_snapshot_count = int((row.summary or {}).get("source_snapshot_count") or len(payload))
    return {
        "snapshot_date": row.snapshot_date,
        "report_revision": row.report_revision,
        "summary": _json_safe(
            _folder_summary(
                payload,
                source_snapshot_count=source_snapshot_count,
                queue_population=queue_population,
            )
        ),
        "payload": payload,
        "computed_at": row.computed_at,
        "source_status": source_status,
    }


def workplace_cache_status(
    session: Session,
    *,
    snapshot_date: date | None,
) -> dict[str, Any]:
    if snapshot_date is None:
        return {
            "open_debt": {"source_status": "missing", "cached_count": 0, "computed_at": None},
            "folder_recommendations": {
                "source_status": "missing",
                "cached_count": 0,
                "computed_at": None,
            },
        }
    open_debt_count = session.scalar(
        select(func.count(ReceivableOpenDebtCache.id)).where(
            ReceivableOpenDebtCache.snapshot_date == snapshot_date
        )
    )
    open_debt_computed_at = session.scalar(
        select(func.max(ReceivableOpenDebtCache.computed_at)).where(
            ReceivableOpenDebtCache.snapshot_date == snapshot_date
        )
    )
    open_debt_stale_count = session.scalar(
        select(func.count(ReceivableOpenDebtCache.id)).where(
            ReceivableOpenDebtCache.snapshot_date == snapshot_date,
            ReceivableOpenDebtCache.source_status == "source_stale",
        )
    )
    freshness = evaluate_open_debt_source_freshness(
        session,
        snapshot_date=snapshot_date,
    )
    folder_count = session.scalar(
        select(func.count(ReceivableFolderRecommendationCache.id)).where(
            ReceivableFolderRecommendationCache.snapshot_date == snapshot_date,
            ReceivableFolderRecommendationCache.status_scope == "all",
        )
    )
    folder_computed_at = session.scalar(
        select(func.max(ReceivableFolderRecommendationCache.computed_at)).where(
            ReceivableFolderRecommendationCache.snapshot_date == snapshot_date,
            ReceivableFolderRecommendationCache.status_scope == "all",
        )
    )
    return {
        "open_debt": {
            "source_status": (
                "source_stale"
                if open_debt_stale_count
                else "cache_ready" if open_debt_count else "missing"
            ),
            "cached_count": int(open_debt_count or 0),
            "computed_at": open_debt_computed_at,
            "source_max_document_date": freshness.source_max_document_date,
            "source_lag_days": freshness.source_lag_days,
        },
        "folder_recommendations": {
            "source_status": "cache_ready" if folder_count else "missing",
            "cached_count": int(folder_count or 0),
            "computed_at": folder_computed_at,
        },
    }
