from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ReceivableBalanceSnapshot,
    ReceivableCase,
    ReceivableFolderRecommendationCache,
    ReceivableOpenDebtCache,
)
from app.services.counterparty_folder_recommendations import (
    STATUS_MOVE_RECOMMENDED,
    STATUS_NEEDS_REVIEW,
    STATUS_NO_OVERDUE,
    STATUS_OK,
    build_open_debt_documents_by_counterparty,
)
from app.services.receivables import CASE_BUYERS


@dataclass(frozen=True)
class CachedOpenDebtDocuments:
    documents_by_counterparty: dict[str, list[dict[str, Any]]]
    source_status: str
    computed_at: datetime | None = None
    cached_counterparty_count: int = 0
    missing_counterparty_count: int = 0


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
        _ref_key(row.counterparty_ref): list(row.documents or []) for row in rows
    }
    missing_count = len(expected_keys - set(documents_by_counterparty))
    computed_at = max((row.computed_at for row in rows), default=None)
    if not rows:
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
    )


def rebuild_open_debt_cache(
    session: Session,
    *,
    snapshot_date: date,
    onec_engine=None,
    include_onec_enrichment: bool = False,
    department_refs: Sequence[str] | None = None,
) -> dict[str, Any]:
    conditions = [
        ReceivableBalanceSnapshot.snapshot_date == snapshot_date,
        ReceivableBalanceSnapshot.current_balance > Decimal("0"),
    ]
    if department_refs:
        conditions.append(ReceivableBalanceSnapshot.department_ref.in_(list(department_refs)))
    snapshots = (
        session.execute(select(ReceivableBalanceSnapshot).where(*conditions)).scalars().all()
    )
    documents_by_counterparty = build_open_debt_documents_by_counterparty(
        session,
        onec_engine=onec_engine,
        snapshots=snapshots,
        snapshot_date=snapshot_date,
        include_onec_enrichment=include_onec_enrichment,
    )
    now = datetime.utcnow()
    updated_count = 0
    for snapshot in snapshots:
        key = _ref_key(snapshot.counterparty_ref)
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
        row.source_status = "ready"
        row.documents = _json_safe(documents_by_counterparty.get(key, []))
        row.computed_at = now
        updated_count += 1
    return {
        "snapshot_date": snapshot_date,
        "source_snapshot_count": len(snapshots),
        "updated_count": updated_count,
        "department_refs": list(department_refs or []),
        "computed_at": now,
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
    payload: list[dict[str, Any]], *, source_snapshot_count: int = 0
) -> dict[str, Any]:
    status_counts = Counter(str(item.get("status") or "") for item in payload)
    return {
        "total_count": len(payload),
        "source_snapshot_count": source_snapshot_count,
        "move_recommended_count": status_counts[STATUS_MOVE_RECOMMENDED],
        "ok_count": status_counts[STATUS_OK],
        "no_overdue_count": status_counts[STATUS_NO_OVERDUE],
        "needs_review_count": status_counts[STATUS_NEEDS_REVIEW],
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
    row.source_status = "cached"
    row.computed_at = datetime.utcnow()
    return row


def load_cached_folder_recommendation_report(
    session: Session,
    *,
    snapshot_date: date,
    status: str | None = None,
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
    payload = _filter_folder_payload_for_access(
        list(row.payload or []),
        allowed_department_refs=allowed_department_refs,
    )
    if status:
        payload = [item for item in payload if item.get("status") == status]
    if limit is not None:
        payload = payload[:limit]
    source_snapshot_count = int((row.summary or {}).get("source_snapshot_count") or len(payload))
    return {
        "snapshot_date": row.snapshot_date,
        "report_revision": row.report_revision,
        "summary": _json_safe(
            _folder_summary(payload, source_snapshot_count=source_snapshot_count)
        ),
        "payload": payload,
        "computed_at": row.computed_at,
        "source_status": "cache_ready",
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
            "source_status": "cache_ready" if open_debt_count else "missing",
            "cached_count": int(open_debt_count or 0),
            "computed_at": open_debt_computed_at,
        },
        "folder_recommendations": {
            "source_status": "cache_ready" if folder_count else "missing",
            "cached_count": int(folder_count or 0),
            "computed_at": folder_computed_at,
        },
    }
