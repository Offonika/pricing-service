from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Sequence

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.receivable_balance_snapshot import ReceivableBalanceSnapshot
from app.services.receivables import _build_ref_filter_clause, _hex_ref_expr, _with_nolock

STATUS_MOVE_RECOMMENDED = "move_recommended"
STATUS_OK = "ok"
STATUS_NO_OVERDUE = "no_overdue"
STATUS_NEEDS_REVIEW = "needs_review"

STATUS_SORT_ORDER = {
    STATUS_MOVE_RECOMMENDED: 0,
    STATUS_NEEDS_REVIEW: 1,
    STATUS_OK: 2,
    STATUS_NO_OVERDUE: 3,
}

REVIEW_REASON_MISSING_DOCUMENT = "missing_origin_document"
REVIEW_REASON_DOCUMENT_NOT_FOUND = "origin_document_not_found"
REVIEW_REASON_DOCUMENT_DEPARTMENT_MISSING = "origin_document_department_missing"
REVIEW_REASON_DEPARTMENT_FOLDER_MISSING = "department_folder_missing"
REVIEW_REASON_CURRENT_FOLDER_MISSING = "current_counterparty_folder_missing"


@dataclass(frozen=True)
class CounterpartyFolderRow:
    counterparty_ref: str
    counterparty_name: str | None
    current_folder_ref: str | None
    current_folder_name: str | None


@dataclass(frozen=True)
class SaleDocumentDepartmentRow:
    document_ref: str
    document_department_ref: str | None
    document_department_name: str | None
    recommended_folder_ref: str | None
    recommended_folder_name: str | None


def _normalize_ref(value: Any) -> str:
    return str(value or "").strip()


def _ref_key(value: Any) -> str:
    return _normalize_ref(value).casefold()


def _refs_equal(left: str | None, right: str | None) -> bool:
    return bool(left and right and _ref_key(left) == _ref_key(right))


def _chunked(values: Sequence[str], size: int = 500) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield list(values[index : index + size])


def fetch_counterparty_current_folders(
    onec_engine,
    *,
    counterparty_refs: Sequence[str],
) -> dict[str, CounterpartyFolderRow]:
    refs = sorted({_normalize_ref(value) for value in counterparty_refs if _normalize_ref(value)})
    if not refs:
        return {}

    dialect_name = onec_engine.dialect.name
    nolock = _with_nolock(dialect_name=dialect_name)
    counterparty_ref_expr = _hex_ref_expr("cp._IDRRef", dialect_name=dialect_name)
    current_folder_ref_expr = _hex_ref_expr("folder._IDRRef", dialect_name=dialect_name)
    rows_by_ref: dict[str, CounterpartyFolderRow] = {}

    with onec_engine.connect() as conn:
        for chunk in _chunked(refs):
            where_clause, params = _build_ref_filter_clause(
                dialect_name=dialect_name,
                refs=chunk,
                column_name="cp._IDRRef",
                prefix="counterparty_ref",
            )
            stmt = text(f"""
                SELECT
                    {counterparty_ref_expr} AS counterparty_ref,
                    cp._Description AS counterparty_name,
                    {current_folder_ref_expr} AS current_folder_ref,
                    folder._Description AS current_folder_name
                FROM _Reference54 AS cp {nolock}
                LEFT JOIN _Reference54 AS folder {nolock}
                    ON folder._IDRRef = cp._ParentIDRRef
                WHERE {where_clause}
            """)
            for row in conn.execute(stmt, params).mappings():
                counterparty_ref = _normalize_ref(row.get("counterparty_ref"))
                if not counterparty_ref:
                    continue
                rows_by_ref[_ref_key(counterparty_ref)] = CounterpartyFolderRow(
                    counterparty_ref=counterparty_ref,
                    counterparty_name=_normalize_ref(row.get("counterparty_name")) or None,
                    current_folder_ref=_normalize_ref(row.get("current_folder_ref")) or None,
                    current_folder_name=_normalize_ref(row.get("current_folder_name")) or None,
                )

    return rows_by_ref


def fetch_sale_document_departments(
    onec_engine,
    *,
    document_refs: Sequence[str],
) -> dict[str, SaleDocumentDepartmentRow]:
    refs = sorted({_normalize_ref(value) for value in document_refs if _normalize_ref(value)})
    if not refs:
        return {}

    dialect_name = onec_engine.dialect.name
    nolock = _with_nolock(dialect_name=dialect_name)
    document_ref_expr = _hex_ref_expr("sale._IDRRef", dialect_name=dialect_name)
    department_ref_expr = _hex_ref_expr("department._IDRRef", dialect_name=dialect_name)
    folder_ref_expr = _hex_ref_expr("department_folder._IDRRef", dialect_name=dialect_name)
    rows_by_ref: dict[str, SaleDocumentDepartmentRow] = {}

    with onec_engine.connect() as conn:
        for chunk in _chunked(refs):
            where_clause, params = _build_ref_filter_clause(
                dialect_name=dialect_name,
                refs=chunk,
                column_name="sale._IDRRef",
                prefix="document_ref",
            )
            stmt = text(f"""
                SELECT
                    {document_ref_expr} AS document_ref,
                    {department_ref_expr} AS document_department_ref,
                    department._Description AS document_department_name,
                    {folder_ref_expr} AS recommended_folder_ref,
                    department_folder._Description AS recommended_folder_name
                FROM _Document203 AS sale {nolock}
                LEFT JOIN _Reference68 AS department {nolock}
                    ON department._IDRRef = sale._Fld4937RRef
                LEFT JOIN _Reference54 AS department_folder {nolock}
                    ON department_folder._IDRRef = department._Fld8927RRef
                WHERE {where_clause}
            """)
            for row in conn.execute(stmt, params).mappings():
                document_ref = _normalize_ref(row.get("document_ref"))
                if not document_ref:
                    continue
                rows_by_ref[_ref_key(document_ref)] = SaleDocumentDepartmentRow(
                    document_ref=document_ref,
                    document_department_ref=(
                        _normalize_ref(row.get("document_department_ref")) or None
                    ),
                    document_department_name=(
                        _normalize_ref(row.get("document_department_name")) or None
                    ),
                    recommended_folder_ref=_normalize_ref(row.get("recommended_folder_ref"))
                    or None,
                    recommended_folder_name=_normalize_ref(row.get("recommended_folder_name"))
                    or None,
                )

    return rows_by_ref


def _review_reason(
    *,
    snapshot: ReceivableBalanceSnapshot,
    document_row: SaleDocumentDepartmentRow | None,
    folder_row: CounterpartyFolderRow | None,
) -> str | None:
    if not snapshot.origin_document_ref:
        return REVIEW_REASON_MISSING_DOCUMENT
    if document_row is None:
        return REVIEW_REASON_DOCUMENT_NOT_FOUND
    if not document_row.document_department_ref:
        return REVIEW_REASON_DOCUMENT_DEPARTMENT_MISSING
    if not document_row.recommended_folder_ref:
        return REVIEW_REASON_DEPARTMENT_FOLDER_MISSING
    if folder_row is None or not folder_row.current_folder_ref:
        return REVIEW_REASON_CURRENT_FOLDER_MISSING
    return None


def _build_item(
    snapshot: ReceivableBalanceSnapshot,
    *,
    folder_row: CounterpartyFolderRow | None,
    document_row: SaleDocumentDepartmentRow | None,
) -> dict[str, Any]:
    status = STATUS_NO_OVERDUE
    review_reason: str | None = None
    if snapshot.is_overdue:
        review_reason = _review_reason(
            snapshot=snapshot,
            document_row=document_row,
            folder_row=folder_row,
        )
        if review_reason is not None:
            status = STATUS_NEEDS_REVIEW
        elif _refs_equal(folder_row.current_folder_ref, document_row.recommended_folder_ref):
            status = STATUS_OK
        else:
            status = STATUS_MOVE_RECOMMENDED

    return {
        "snapshot_date": snapshot.snapshot_date,
        "counterparty_ref": snapshot.counterparty_ref,
        "counterparty_name": snapshot.counterparty_name,
        "current_balance": snapshot.current_balance,
        "current_folder_ref": folder_row.current_folder_ref if folder_row else None,
        "current_folder_name": folder_row.current_folder_name if folder_row else None,
        "recommended_folder_ref": (document_row.recommended_folder_ref if document_row else None),
        "recommended_folder_name": (document_row.recommended_folder_name if document_row else None),
        "debt_department_ref": (document_row.document_department_ref if document_row else None),
        "debt_department_name": (document_row.document_department_name if document_row else None),
        "origin_document_ref": snapshot.origin_document_ref,
        "origin_document_number": snapshot.origin_document_number,
        "origin_document_date": snapshot.origin_document_date,
        "origin_manager_ref": snapshot.origin_manager_ref,
        "origin_manager_name": snapshot.origin_manager_name,
        "current_manager_ref": snapshot.current_manager_ref,
        "current_manager_name": snapshot.current_manager_name,
        "planned_payment_date": snapshot.planned_payment_date,
        "credit_depth_days": snapshot.credit_depth_days,
        "payment_term_source": snapshot.payment_term_source,
        "due_date": snapshot.due_date,
        "overdue_days": snapshot.overdue_days,
        "is_overdue": snapshot.is_overdue,
        "status": status,
        "review_reason": review_reason,
    }


def _build_report_revision(snapshot_date: date, items: Sequence[dict[str, Any]]) -> str:
    revision_payload = [
        {
            "counterparty_ref": item["counterparty_ref"],
            "current_folder_ref": item.get("current_folder_ref"),
            "recommended_folder_ref": item.get("recommended_folder_ref"),
            "origin_document_ref": item.get("origin_document_ref"),
            "status": item.get("status"),
        }
        for item in items
    ]
    raw = json.dumps(
        {"date": snapshot_date.isoformat(), "items": revision_payload},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def build_counterparty_folder_recommendations(
    session: Session,
    *,
    onec_engine,
    snapshot_date: date,
    limit: int | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    allowed_statuses = {
        STATUS_MOVE_RECOMMENDED,
        STATUS_OK,
        STATUS_NO_OVERDUE,
        STATUS_NEEDS_REVIEW,
    }
    if status is not None and status not in allowed_statuses:
        raise ValueError(f"unsupported status: {status}")

    snapshots = (
        session.execute(
            select(ReceivableBalanceSnapshot)
            .where(
                ReceivableBalanceSnapshot.snapshot_date == snapshot_date,
                ReceivableBalanceSnapshot.current_balance > Decimal("0"),
            )
            .order_by(
                ReceivableBalanceSnapshot.counterparty_ref,
                ReceivableBalanceSnapshot.origin_document_date,
            )
        )
        .scalars()
        .all()
    )

    counterparty_refs = [snapshot.counterparty_ref for snapshot in snapshots]
    document_refs = [
        snapshot.origin_document_ref
        for snapshot in snapshots
        if snapshot.origin_document_ref is not None
    ]
    current_folders = fetch_counterparty_current_folders(
        onec_engine,
        counterparty_refs=counterparty_refs,
    )
    document_departments = fetch_sale_document_departments(
        onec_engine,
        document_refs=document_refs,
    )

    items = [
        _build_item(
            snapshot,
            folder_row=current_folders.get(_ref_key(snapshot.counterparty_ref)),
            document_row=document_departments.get(_ref_key(snapshot.origin_document_ref)),
        )
        for snapshot in snapshots
    ]
    if status is not None:
        items = [item for item in items if item["status"] == status]

    items.sort(
        key=lambda item: (
            STATUS_SORT_ORDER.get(str(item["status"]), 99),
            -(int(item.get("overdue_days") or 0)),
            -(item.get("current_balance") or Decimal("0")),
            str(item.get("counterparty_name") or ""),
        )
    )
    if limit is not None:
        items = items[:limit]

    status_counts = Counter(item["status"] for item in items)
    total_open_debt = sum((item["current_balance"] for item in items), Decimal("0"))
    move_amount = sum(
        (item["current_balance"] for item in items if item["status"] == STATUS_MOVE_RECOMMENDED),
        Decimal("0"),
    )

    return {
        "snapshot_date": snapshot_date,
        "report_revision": _build_report_revision(snapshot_date, items),
        "summary": {
            "total_count": len(items),
            "source_snapshot_count": len(snapshots),
            "move_recommended_count": status_counts[STATUS_MOVE_RECOMMENDED],
            "ok_count": status_counts[STATUS_OK],
            "no_overdue_count": status_counts[STATUS_NO_OVERDUE],
            "needs_review_count": status_counts[STATUS_NEEDS_REVIEW],
            "total_open_debt": total_open_debt,
            "move_recommended_amount": move_amount,
        },
        "payload": items,
    }
