from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Iterable, Sequence

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.receivable_balance_snapshot import ReceivableBalanceSnapshot
from app.models.receivable_ledger_event import ReceivableLedgerEvent
from app.services.receivable_document_structure import (
    DOCUMENT_STRUCTURE_CLOSED,
    DOCUMENT_STRUCTURE_CONFIRMED_OPEN,
    DOCUMENT_STRUCTURE_NOT_FOUND,
    ReceivableDocumentStructureCheck,
    fetch_receivable_document_structure_checks,
)
from app.services.receivable_statement_debt import (
    ReceivableStatementEvent,
    resolve_open_debt_documents_by_statement,
)
from app.services.receivables import _build_ref_filter_clause, _hex_ref_expr, _with_nolock

STATUS_MOVE_RECOMMENDED = "move_recommended"
STATUS_OK = "ok"
STATUS_NO_OVERDUE = "no_overdue"
STATUS_NEEDS_REVIEW = "needs_review"
DEFAULT_PAYMENT_TERM_DAYS = 7
MIN_RECOMMENDATION_BALANCE = Decimal("500.00")
PAYMENT_TERM_SOURCE_FALLBACK = "fallback_7_days_read_only"
STRUCTURE_CANDIDATE_OLDEST_SALES_PER_COUNTERPARTY = 20
STRUCTURE_CANDIDATE_AFTER_ORIGIN_SALES_PER_COUNTERPARTY = 40
STRUCTURE_CANDIDATE_LATEST_SALES_PER_COUNTERPARTY = 60

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
REVIEW_REASON_FOLDER_MISMATCH_PAYMENT_TERM_MISSING = "folder_mismatch_payment_term_missing"
REVIEW_REASON_SPB_CROSS_FOLDER = "spb_cross_folder_manual_review"
REVIEW_REASON_EXCLUDED_EMPLOYEE_FOLDER = "excluded_employee_folder"
REVIEW_REASON_EXCLUDED_WHOLESALE = "excluded_wholesale_counterparty"
REVIEW_REASON_EXCLUDED_SITE_PAYMENT_ON_PICKUP = "excluded_site_payment_on_pickup"
REVIEW_REASON_BELOW_MIN_BALANCE = "below_min_balance_threshold"
REVIEW_REASON_EXCLUDED_CHINA_SUPPLIER_GROUP = "excluded_china_supplier_group"
REVIEW_REASON_OPEN_STRUCTURE_DOCUMENT_NOT_FOUND = (
    "open_structure_document_not_found"
)
REVIEW_REASON_ORIGIN_DOCUMENT_NEEDS_ORDER_PAYMENT_CHECK = (
    "origin_document_needs_order_payment_check"
)
REVIEW_REASON_ORIGIN_DOCUMENT_STRUCTURE_UNCONFIRMED = (
    "origin_document_structure_unconfirmed"
)
REVIEW_REASON_ORIGIN_DOCUMENT_STRUCTURE_CONFIRMED = (
    "origin_document_structure_confirmed_manual_review"
)
REVIEW_REASON_ORIGIN_DOCUMENT_CLOSED_BY_STRUCTURE = (
    "origin_document_closed_by_structure"
)


@dataclass(frozen=True)
class CounterpartyFolderRow:
    counterparty_ref: str
    counterparty_code: str | None
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
    document_author_ref: str | None
    document_author_name: str | None


@dataclass(frozen=True)
class EffectivePaymentTerm:
    credit_depth_days: int | None
    due_date: datetime | None
    overdue_days: int | None
    is_overdue: bool
    payment_term_source: str | None


@dataclass(frozen=True)
class LedgerSaleEventRow:
    counterparty_ref: str
    document_ref: str
    document_number: str | None
    document_date: datetime
    manager_ref: str | None
    manager_name: str | None
    amount_delta: Decimal


def _normalize_ref(value: Any) -> str:
    return str(value or "").strip()


def _ref_key(value: Any) -> str:
    return _normalize_ref(value).casefold()


def _refs_equal(left: str | None, right: str | None) -> bool:
    return bool(left and right and _ref_key(left) == _ref_key(right))


def _text_key(value: Any) -> str:
    return str(value or "").strip().casefold().replace("ё", "е")


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
                    cp._Code AS counterparty_code,
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
                    counterparty_code=_normalize_ref(row.get("counterparty_code")) or None,
                    counterparty_name=_normalize_ref(row.get("counterparty_name")) or None,
                    current_folder_ref=_normalize_ref(row.get("current_folder_ref")) or None,
                    current_folder_name=_normalize_ref(row.get("current_folder_name")) or None,
                )

    return rows_by_ref


def fetch_counterparty_refs_from_onec_group(
    onec_engine,
    *,
    group_name: str,
) -> set[str]:
    dialect_name = onec_engine.dialect.name
    nolock = _with_nolock(dialect_name=dialect_name)
    counterparty_ref_expr = _hex_ref_expr("tree._IDRRef", dialect_name=dialect_name)
    if dialect_name == "mssql":
        root_predicate = "c._ParentIDRRef = 0x00000000000000000000000000000000"
        group_match = "UPPER(LTRIM(RTRIM(COALESCE(c._Description, N'')))) = UPPER(:group_name)"
        child_group_match = (
            "UPPER(LTRIM(RTRIM(COALESCE(child._Description, N'')))) = UPPER(:group_name)"
        )
        item_predicate = "tree._Folder = 0x01"
        recursive_keyword = ""
    else:
        root_predicate = "(c._ParentIDRRef IS NULL OR c._ParentIDRRef = '')"
        group_match = "UPPER(TRIM(COALESCE(c._Description, ''))) = UPPER(:group_name)"
        child_group_match = (
            "UPPER(TRIM(COALESCE(child._Description, ''))) = UPPER(:group_name)"
        )
        item_predicate = "tree._Folder = 1"
        recursive_keyword = "RECURSIVE"

    stmt = text(f"""
        WITH {recursive_keyword} tree AS (
            SELECT
                c._IDRRef,
                c._ParentIDRRef,
                c._Description,
                c._Folder,
                CAST(
                    CASE
                        WHEN {group_match} THEN 1
                        ELSE 0
                    END AS int
                ) AS is_group_branch
            FROM _Reference54 AS c {nolock}
            WHERE {root_predicate}

            UNION ALL

            SELECT
                child._IDRRef,
                child._ParentIDRRef,
                child._Description,
                child._Folder,
                CAST(
                    CASE
                        WHEN parent.is_group_branch = 1 THEN 1
                        WHEN {child_group_match} THEN 1
                        ELSE 0
                    END AS int
                ) AS is_group_branch
            FROM _Reference54 AS child {nolock}
            JOIN tree AS parent
                ON child._ParentIDRRef = parent._IDRRef
        )
        SELECT DISTINCT
            {counterparty_ref_expr} AS counterparty_ref
        FROM tree
        WHERE {item_predicate}
          AND is_group_branch = 1
    """)
    refs: set[str] = set()
    with onec_engine.connect() as conn:
        for row in conn.execute(stmt, {"group_name": group_name}).mappings():
            counterparty_ref = _normalize_ref(row.get("counterparty_ref"))
            if counterparty_ref:
                refs.add(counterparty_ref)
    return refs


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
    author_ref_expr = _hex_ref_expr("author._IDRRef", dialect_name=dialect_name)
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
                    department_folder._Description AS recommended_folder_name,
                    {author_ref_expr} AS document_author_ref,
                    author._Description AS document_author_name
                FROM _Document203 AS sale {nolock}
                LEFT JOIN _Reference68 AS department {nolock}
                    ON department._IDRRef = sale._Fld4937RRef
                LEFT JOIN _Reference54 AS department_folder {nolock}
                    ON department_folder._IDRRef = department._Fld8927RRef
                LEFT JOIN _Reference54 AS author {nolock}
                    ON author._IDRRef = sale._Fld4942RRef
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
                    document_author_ref=_normalize_ref(row.get("document_author_ref")) or None,
                    document_author_name=_normalize_ref(row.get("document_author_name"))
                    or None,
                )

    return rows_by_ref


def fetch_counterparty_ledger_sale_events(
    session: Session,
    *,
    counterparty_refs: Sequence[str],
    snapshot_date: date,
) -> dict[str, list[LedgerSaleEventRow]]:
    refs = sorted({_normalize_ref(value) for value in counterparty_refs if _normalize_ref(value)})
    if not refs:
        return {}

    snapshot_end = datetime.combine(snapshot_date + timedelta(days=1), time.min)
    rows_by_counterparty: dict[str, list[LedgerSaleEventRow]] = {}
    for chunk in _chunked(refs):
        rows = (
            session.execute(
                select(
                    ReceivableLedgerEvent.counterparty_ref,
                    ReceivableLedgerEvent.external_document_ref,
                    ReceivableLedgerEvent.external_document_number,
                    ReceivableLedgerEvent.external_document_date,
                    ReceivableLedgerEvent.manager_ref,
                    ReceivableLedgerEvent.manager_name,
                    ReceivableLedgerEvent.amount_delta,
                )
                .where(
                    ReceivableLedgerEvent.counterparty_ref.in_(chunk),
                    ReceivableLedgerEvent.event_type == "sale",
                    ReceivableLedgerEvent.amount_delta > Decimal("0"),
                    ReceivableLedgerEvent.external_document_date < snapshot_end,
                )
                .order_by(
                    ReceivableLedgerEvent.counterparty_ref,
                    ReceivableLedgerEvent.external_document_date,
                    ReceivableLedgerEvent.id,
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            counterparty_ref = _normalize_ref(row.get("counterparty_ref"))
            document_ref = _normalize_ref(row.get("external_document_ref"))
            document_date = row.get("external_document_date")
            if not counterparty_ref or not document_ref or not isinstance(document_date, datetime):
                continue
            rows_by_counterparty.setdefault(_ref_key(counterparty_ref), []).append(
                LedgerSaleEventRow(
                    counterparty_ref=counterparty_ref,
                    document_ref=document_ref,
                    document_number=_normalize_ref(row.get("external_document_number")) or None,
                    document_date=document_date,
                    manager_ref=_normalize_ref(row.get("manager_ref")) or None,
                    manager_name=_normalize_ref(row.get("manager_name")) or None,
                    amount_delta=Decimal(row.get("amount_delta") or 0),
                )
            )
    return rows_by_counterparty


def fetch_counterparty_ledger_statement_events(
    session: Session,
    *,
    counterparty_refs: Sequence[str],
    snapshot_date: date,
) -> dict[str, list[ReceivableStatementEvent]]:
    refs = sorted({_normalize_ref(value) for value in counterparty_refs if _normalize_ref(value)})
    if not refs:
        return {}

    snapshot_end = datetime.combine(snapshot_date + timedelta(days=1), time.min)
    rows_by_counterparty: dict[str, list[ReceivableStatementEvent]] = {}
    for chunk in _chunked(refs):
        rows = (
            session.execute(
                select(
                    ReceivableLedgerEvent.counterparty_ref,
                    ReceivableLedgerEvent.event_type,
                    ReceivableLedgerEvent.external_document_ref,
                    ReceivableLedgerEvent.external_document_number,
                    ReceivableLedgerEvent.external_document_date,
                    ReceivableLedgerEvent.manager_ref,
                    ReceivableLedgerEvent.manager_name,
                    ReceivableLedgerEvent.source_layer,
                    ReceivableLedgerEvent.line_no,
                    ReceivableLedgerEvent.amount_delta,
                )
                .where(
                    ReceivableLedgerEvent.counterparty_ref.in_(chunk),
                    ReceivableLedgerEvent.event_type.in_(
                        ("sale", "payment", "return", "settlement", "debt_adjustment")
                    ),
                    ReceivableLedgerEvent.external_document_date < snapshot_end,
                )
                .order_by(
                    ReceivableLedgerEvent.counterparty_ref,
                    ReceivableLedgerEvent.external_document_date,
                    ReceivableLedgerEvent.line_no,
                    ReceivableLedgerEvent.id,
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            counterparty_ref = _normalize_ref(row.get("counterparty_ref"))
            document_ref = _normalize_ref(row.get("external_document_ref"))
            document_date = row.get("external_document_date")
            if not counterparty_ref or not document_ref or not isinstance(document_date, datetime):
                continue
            rows_by_counterparty.setdefault(_ref_key(counterparty_ref), []).append(
                ReceivableStatementEvent(
                    counterparty_ref=counterparty_ref,
                    event_type=_normalize_ref(row.get("event_type")),
                    document_ref=document_ref,
                    document_number=_normalize_ref(row.get("external_document_number")) or None,
                    document_date=document_date,
                    amount_delta=Decimal(row.get("amount_delta") or 0),
                    manager_ref=_normalize_ref(row.get("manager_ref")) or None,
                    manager_name=_normalize_ref(row.get("manager_name")) or None,
                    line_no=row.get("line_no"),
                    source_layer=_normalize_ref(row.get("source_layer")) or None,
                )
            )
    return rows_by_counterparty


def _review_reason(
    *,
    debt_document_ref: str | None,
    document_row: SaleDocumentDepartmentRow | None,
    folder_row: CounterpartyFolderRow | None,
) -> str | None:
    if not debt_document_ref:
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


def _has_missing_payment_term(snapshot: ReceivableBalanceSnapshot) -> bool:
    return (
        snapshot.planned_payment_date is None
        and snapshot.credit_depth_days is None
        and snapshot.due_date is None
    )


def _compute_overdue_days(snapshot_date: date, due_date: datetime | None) -> int | None:
    if due_date is None:
        return None
    days = (snapshot_date - due_date.date()).days
    return days if days > 0 else 0


def _effective_payment_term(
    snapshot: ReceivableBalanceSnapshot,
    *,
    debt_document_date: datetime | None = None,
) -> EffectivePaymentTerm:
    due_date = snapshot.due_date or snapshot.planned_payment_date
    credit_depth_days = snapshot.credit_depth_days
    source = snapshot.payment_term_source
    effective_origin_date = debt_document_date or snapshot.origin_document_date
    if due_date is None and effective_origin_date is not None and credit_depth_days:
        due_date = effective_origin_date + timedelta(days=credit_depth_days)

    if _has_missing_payment_term(snapshot) and effective_origin_date is not None:
        credit_depth_days = DEFAULT_PAYMENT_TERM_DAYS
        due_date = effective_origin_date + timedelta(days=DEFAULT_PAYMENT_TERM_DAYS)
        source = PAYMENT_TERM_SOURCE_FALLBACK

    overdue_days = (
        snapshot.overdue_days
        if snapshot.overdue_days is not None and source != PAYMENT_TERM_SOURCE_FALLBACK
        else _compute_overdue_days(snapshot.snapshot_date, due_date)
    )
    is_overdue = bool(overdue_days is not None and overdue_days > 0)
    return EffectivePaymentTerm(
        credit_depth_days=credit_depth_days,
        due_date=due_date,
        overdue_days=overdue_days,
        is_overdue=is_overdue,
        payment_term_source=source,
    )


def _is_employee_context(
    *,
    counterparty_name: str | None,
    current_folder_name: str | None,
    recommended_folder_name: str | None,
) -> bool:
    values = (
        _text_key(counterparty_name),
        _text_key(current_folder_name),
        _text_key(recommended_folder_name),
    )
    return any("сотрудник" in value for value in values)


def _is_wholesale_context(
    *,
    counterparty_name: str | None,
    current_folder_name: str | None,
    recommended_folder_name: str | None,
) -> bool:
    values = (
        _text_key(counterparty_name),
        _text_key(current_folder_name),
        _text_key(recommended_folder_name),
    )
    return any("оптов" in value for value in values)


def _is_site_payment_on_pickup(counterparty_name: str | None) -> bool:
    return "выдача без оплаты" in _text_key(counterparty_name)


def _is_site_folder(current_folder_name: str | None) -> bool:
    return _text_key(current_folder_name) in {"08. сайт", "сайт"}


def _counterparty_exception_reason(
    *,
    snapshot: ReceivableBalanceSnapshot,
    folder_row: CounterpartyFolderRow | None,
    recommended_folder_name: str | None = None,
) -> str | None:
    if _is_employee_context(
        counterparty_name=snapshot.counterparty_name,
        current_folder_name=folder_row.current_folder_name if folder_row else None,
        recommended_folder_name=recommended_folder_name,
    ):
        return REVIEW_REASON_EXCLUDED_EMPLOYEE_FOLDER
    if _is_wholesale_context(
        counterparty_name=snapshot.counterparty_name,
        current_folder_name=folder_row.current_folder_name if folder_row else None,
        recommended_folder_name=recommended_folder_name,
    ):
        return REVIEW_REASON_EXCLUDED_WHOLESALE
    if _is_site_payment_on_pickup(snapshot.counterparty_name):
        return REVIEW_REASON_EXCLUDED_SITE_PAYMENT_ON_PICKUP
    return None


def _is_spb_cross_folder(
    *,
    current_folder_name: str | None,
    recommended_folder_name: str | None,
) -> bool:
    current = _text_key(current_folder_name)
    recommended = _text_key(recommended_folder_name)
    return bool(
        current
        and recommended
        and current != recommended
        and "спб" in current
        and "спб" in recommended
    )


def _is_manual_review_exception(reason: str | None) -> bool:
    return reason in {
        REVIEW_REASON_SPB_CROSS_FOLDER,
        REVIEW_REASON_ORIGIN_DOCUMENT_NEEDS_ORDER_PAYMENT_CHECK,
        REVIEW_REASON_ORIGIN_DOCUMENT_STRUCTURE_UNCONFIRMED,
        REVIEW_REASON_ORIGIN_DOCUMENT_STRUCTURE_CONFIRMED,
        REVIEW_REASON_ORIGIN_DOCUMENT_CLOSED_BY_STRUCTURE,
        REVIEW_REASON_OPEN_STRUCTURE_DOCUMENT_NOT_FOUND,
    }


def _is_excluded_reason(reason: str | None) -> bool:
    return reason in {
        REVIEW_REASON_EXCLUDED_CHINA_SUPPLIER_GROUP,
        REVIEW_REASON_EXCLUDED_EMPLOYEE_FOLDER,
        REVIEW_REASON_EXCLUDED_WHOLESALE,
        REVIEW_REASON_EXCLUDED_SITE_PAYMENT_ON_PICKUP,
    }


def _structure_review_reason(
    structure_check: ReceivableDocumentStructureCheck | None,
) -> str:
    if structure_check is None:
        return REVIEW_REASON_ORIGIN_DOCUMENT_STRUCTURE_UNCONFIRMED
    if structure_check.status == DOCUMENT_STRUCTURE_CONFIRMED_OPEN:
        return REVIEW_REASON_ORIGIN_DOCUMENT_STRUCTURE_CONFIRMED
    if structure_check.status == DOCUMENT_STRUCTURE_CLOSED:
        return REVIEW_REASON_ORIGIN_DOCUMENT_CLOSED_BY_STRUCTURE
    if structure_check.status == DOCUMENT_STRUCTURE_NOT_FOUND:
        return REVIEW_REASON_DOCUMENT_NOT_FOUND
    return REVIEW_REASON_ORIGIN_DOCUMENT_STRUCTURE_UNCONFIRMED


def _folder_mismatch_exception_reason(
    *,
    snapshot: ReceivableBalanceSnapshot,
    folder_row: CounterpartyFolderRow,
    document_row: SaleDocumentDepartmentRow,
) -> str | None:
    counterparty_exception = _counterparty_exception_reason(
        snapshot=snapshot,
        folder_row=folder_row,
        recommended_folder_name=document_row.recommended_folder_name,
    )
    if counterparty_exception:
        return counterparty_exception
    if _is_site_folder(folder_row.current_folder_name):
        return REVIEW_REASON_ORIGIN_DOCUMENT_NEEDS_ORDER_PAYMENT_CHECK
    if _is_spb_cross_folder(
        current_folder_name=folder_row.current_folder_name,
        recommended_folder_name=document_row.recommended_folder_name,
    ):
        return REVIEW_REASON_SPB_CROSS_FOLDER
    return None


def _open_debt_documents_for_snapshot(
    *,
    sale_events: Sequence[LedgerSaleEventRow],
    document_departments: dict[str, SaleDocumentDepartmentRow],
    document_structure_checks: dict[str, ReceivableDocumentStructureCheck],
) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for event in sorted(sale_events, key=lambda row: (row.document_date, row.document_ref)):
        document_key = _ref_key(event.document_ref)
        if document_key in seen_refs:
            continue
        seen_refs.add(document_key)
        structure_check = document_structure_checks.get(document_key)
        if (
            structure_check is None
            or structure_check.status != DOCUMENT_STRUCTURE_CONFIRMED_OPEN
            or structure_check.open_amount is None
            or structure_check.open_amount <= Decimal("0")
        ):
            continue
        document_row = document_departments.get(document_key)
        documents.append(
            {
                "document_ref": event.document_ref,
                "document_number": structure_check.sale_number or event.document_number,
                "document_date": structure_check.sale_date or event.document_date,
                "open_amount": structure_check.open_amount,
                "sale_amount": structure_check.sale_amount,
                "closing_amount": structure_check.closing_amount,
                "manager_ref": event.manager_ref,
                "manager_name": event.manager_name,
                "debt_department_ref": (
                    document_row.document_department_ref if document_row else None
                ),
                "debt_department_name": (
                    document_row.document_department_name if document_row else None
                ),
                "recommended_folder_ref": (
                    document_row.recommended_folder_ref if document_row else None
                ),
                "recommended_folder_name": (
                    document_row.recommended_folder_name if document_row else None
                ),
                "document_structure_status": structure_check.status,
                "document_structure_order_ref": structure_check.order_ref,
                "document_structure_order_number": structure_check.order_number,
                "document_structure_order_date": structure_check.order_date,
                "document_structure_linked_documents": list(structure_check.linked_documents),
            }
        )
    return documents


def _open_debt_documents_from_statement(
    *,
    statement_events: Sequence[ReceivableStatementEvent],
    current_balance: Decimal,
    document_departments: dict[str, SaleDocumentDepartmentRow],
    document_structure_checks: dict[str, ReceivableDocumentStructureCheck],
) -> list[dict[str, Any]]:
    statement_documents = resolve_open_debt_documents_by_statement(
        statement_events,
        current_balance=current_balance,
        structure_checks=document_structure_checks,
        ref_key=_ref_key,
    )
    documents: list[dict[str, Any]] = []
    for document in statement_documents:
        document_key = _ref_key(document.document_ref)
        document_row = document_departments.get(document_key)
        structure_check = document_structure_checks.get(document_key)
        documents.append(
            {
                "document_ref": document.document_ref,
                "document_number": (
                    structure_check.sale_number
                    if structure_check and structure_check.sale_number
                    else document.document_number
                ),
                "document_date": (
                    structure_check.sale_date
                    if structure_check and structure_check.sale_date
                    else document.document_date
                ),
                "open_amount": document.open_amount,
                "sale_amount": (
                    structure_check.sale_amount
                    if structure_check and structure_check.sale_amount is not None
                    else document.gross_amount
                ),
                "closing_amount": (
                    structure_check.closing_amount
                    if structure_check and structure_check.closing_amount is not None
                    else -document.closing_amount
                ),
                "return_amount": document.return_amount,
                "manager_ref": document.manager_ref,
                "manager_name": document.manager_name,
                "document_author_ref": (
                    document_row.document_author_ref if document_row else None
                ),
                "document_author_name": (
                    document_row.document_author_name if document_row else None
                ),
                "debt_department_ref": (
                    document_row.document_department_ref if document_row else None
                ),
                "debt_department_name": (
                    document_row.document_department_name if document_row else None
                ),
                "recommended_folder_ref": (
                    document_row.recommended_folder_ref if document_row else None
                ),
                "recommended_folder_name": (
                    document_row.recommended_folder_name if document_row else None
                ),
                "document_structure_status": (
                    structure_check.status if structure_check else None
                ),
                "document_structure_order_ref": (
                    structure_check.order_ref if structure_check else None
                ),
                "document_structure_order_number": (
                    structure_check.order_number if structure_check else None
                ),
                "document_structure_order_date": (
                    structure_check.order_date if structure_check else None
                ),
                "document_structure_linked_documents": (
                    list(structure_check.linked_documents) if structure_check else []
                ),
                "statement_selection_rule": document.statement_selection_rule,
                "statement_match_details": list(document.statement_match_details),
            }
        )
    return documents


def _candidate_sale_events_for_structure(
    snapshot: ReceivableBalanceSnapshot,
    sale_events: Sequence[LedgerSaleEventRow],
) -> list[LedgerSaleEventRow]:
    events = sorted(sale_events, key=lambda row: (row.document_date, row.document_ref))
    if not events:
        return []

    selected_by_ref: dict[str, LedgerSaleEventRow] = {}

    def add(items: Sequence[LedgerSaleEventRow]) -> None:
        for item in items:
            selected_by_ref.setdefault(_ref_key(item.document_ref), item)

    add(events[:STRUCTURE_CANDIDATE_OLDEST_SALES_PER_COUNTERPARTY])
    add(events[-STRUCTURE_CANDIDATE_LATEST_SALES_PER_COUNTERPARTY:])
    if snapshot.origin_document_ref:
        origin_key = _ref_key(snapshot.origin_document_ref)
        add([item for item in events if _ref_key(item.document_ref) == origin_key])
    if snapshot.origin_document_date is not None:
        add(
            [
                item
                for item in events
                if item.document_date >= snapshot.origin_document_date
            ][:STRUCTURE_CANDIDATE_AFTER_ORIGIN_SALES_PER_COUNTERPARTY]
        )

    return sorted(selected_by_ref.values(), key=lambda row: (row.document_date, row.document_ref))


def _build_item(
    snapshot: ReceivableBalanceSnapshot,
    *,
    folder_row: CounterpartyFolderRow | None,
    document_row: SaleDocumentDepartmentRow | None,
    structure_check: ReceivableDocumentStructureCheck | None = None,
    open_debt_documents: Sequence[dict[str, Any]] = (),
    is_excluded_china_supplier: bool = False,
) -> dict[str, Any]:
    status = STATUS_NO_OVERDUE
    review_reason: str | None = None
    primary_open_document = open_debt_documents[0] if open_debt_documents else None
    debt_document_ref = (
        _normalize_ref(primary_open_document.get("document_ref"))
        if primary_open_document
        else None
    )
    debt_document_number = (
        _normalize_ref(primary_open_document.get("document_number"))
        if primary_open_document
        else None
    )
    debt_document_date = (
        primary_open_document.get("document_date") if primary_open_document else None
    )
    if not isinstance(debt_document_date, datetime):
        debt_document_date = None
    debt_document_author_ref = (
        _normalize_ref(primary_open_document.get("document_author_ref"))
        if primary_open_document
        else None
    )
    debt_document_author_name = (
        _normalize_ref(primary_open_document.get("document_author_name"))
        if primary_open_document
        else None
    )
    term = _effective_payment_term(snapshot, debt_document_date=debt_document_date)
    counterparty_exception = _counterparty_exception_reason(
        snapshot=snapshot,
        folder_row=folder_row,
        recommended_folder_name=(
            _normalize_ref(primary_open_document.get("recommended_folder_name"))
            if primary_open_document
            else None
        ),
    )
    if is_excluded_china_supplier:
        review_reason = REVIEW_REASON_EXCLUDED_CHINA_SUPPLIER_GROUP
    elif counterparty_exception:
        review_reason = counterparty_exception
    elif not open_debt_documents:
        if term.is_overdue:
            status = STATUS_NEEDS_REVIEW
            review_reason = REVIEW_REASON_OPEN_STRUCTURE_DOCUMENT_NOT_FOUND
        else:
            review_reason = REVIEW_REASON_OPEN_STRUCTURE_DOCUMENT_NOT_FOUND
    else:
        review_reason = _review_reason(
            debt_document_ref=debt_document_ref,
            document_row=document_row,
            folder_row=folder_row,
        )
    if review_reason is None:
        folders_match = _refs_equal(
            folder_row.current_folder_ref, document_row.recommended_folder_ref
        )
        exception_reason = None
        if not folders_match:
            exception_reason = _folder_mismatch_exception_reason(
                snapshot=snapshot,
                folder_row=folder_row,
                document_row=document_row,
            )

        if term.is_overdue and folders_match:
            status = STATUS_OK
        elif _is_manual_review_exception(exception_reason) and term.is_overdue:
            status = STATUS_NEEDS_REVIEW
            review_reason = exception_reason
        elif exception_reason:
            status = STATUS_NO_OVERDUE
            review_reason = exception_reason
        elif term.is_overdue:
            status = STATUS_NEEDS_REVIEW
            review_reason = _structure_review_reason(structure_check)
        elif not folders_match and _has_missing_payment_term(snapshot):
            review_reason = REVIEW_REASON_FOLDER_MISMATCH_PAYMENT_TERM_MISSING
    elif term.is_overdue:
        if _is_excluded_reason(review_reason):
            status = STATUS_NO_OVERDUE
        elif review_reason == REVIEW_REASON_OPEN_STRUCTURE_DOCUMENT_NOT_FOUND:
            status = STATUS_NEEDS_REVIEW
        else:
            exception_reason = _counterparty_exception_reason(
                snapshot=snapshot,
                folder_row=folder_row,
            )
            if exception_reason:
                status = STATUS_NO_OVERDUE
                review_reason = exception_reason
            else:
                status = STATUS_NEEDS_REVIEW
    else:
        review_reason = None

    return {
        "snapshot_date": snapshot.snapshot_date,
        "counterparty_ref": snapshot.counterparty_ref,
        "counterparty_code": folder_row.counterparty_code if folder_row else None,
        "counterparty_name": snapshot.counterparty_name,
        "current_balance": snapshot.current_balance,
        "current_folder_ref": folder_row.current_folder_ref if folder_row else None,
        "current_folder_name": folder_row.current_folder_name if folder_row else None,
        "recommended_folder_ref": (document_row.recommended_folder_ref if document_row else None),
        "recommended_folder_name": (document_row.recommended_folder_name if document_row else None),
        "debt_department_ref": (document_row.document_department_ref if document_row else None),
        "debt_department_name": (document_row.document_department_name if document_row else None),
        "debt_document_ref": debt_document_ref,
        "debt_document_number": debt_document_number,
        "debt_document_date": debt_document_date,
        "debt_document_author_ref": debt_document_author_ref,
        "debt_document_author_name": debt_document_author_name,
        "open_debt_documents": list(open_debt_documents),
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
        "due_date": term.due_date,
        "overdue_days": term.overdue_days,
        "is_overdue": term.is_overdue,
        "effective_credit_depth_days": term.credit_depth_days,
        "effective_payment_term_source": term.payment_term_source,
        "effective_due_date": term.due_date,
        "effective_overdue_days": term.overdue_days,
        "status": status,
        "review_reason": review_reason,
        "document_structure_status": structure_check.status if structure_check else None,
        "document_structure_open_amount": structure_check.open_amount
        if structure_check
        else None,
        "document_structure_sale_amount": structure_check.sale_amount
        if structure_check
        else None,
        "document_structure_closing_amount": structure_check.closing_amount
        if structure_check
        else None,
        "document_structure_order_ref": structure_check.order_ref if structure_check else None,
        "document_structure_order_number": (
            structure_check.order_number if structure_check else None
        ),
        "document_structure_order_date": structure_check.order_date
        if structure_check
        else None,
        "document_structure_linked_documents": list(structure_check.linked_documents)
        if structure_check
        else [],
    }


def _is_actionable_status(status: str | None) -> bool:
    return status in {STATUS_MOVE_RECOMMENDED, STATUS_NEEDS_REVIEW}


def _apply_report_suppression(item: dict[str, Any]) -> dict[str, Any]:
    if (
        _is_actionable_status(str(item.get("status")))
        and Decimal(item.get("current_balance") or 0) < MIN_RECOMMENDATION_BALANCE
    ):
        item = dict(item)
        item["status"] = STATUS_NO_OVERDUE
        item["review_reason"] = REVIEW_REASON_BELOW_MIN_BALANCE
        item["suppressed_from_daily_report"] = True
        item["suppression_reason"] = REVIEW_REASON_BELOW_MIN_BALANCE
    return item


def _needs_structure_lookup_for_status(
    snapshot: ReceivableBalanceSnapshot,
    *,
    status: str | None,
) -> bool:
    if status in {STATUS_MOVE_RECOMMENDED, STATUS_NEEDS_REVIEW}:
        if Decimal(snapshot.current_balance or 0) < MIN_RECOMMENDATION_BALANCE:
            return False
        return _effective_payment_term(snapshot).is_overdue
    return True


def _build_report_revision(snapshot_date: date, items: Sequence[dict[str, Any]]) -> str:
    revision_payload = [
        {
            "counterparty_ref": item["counterparty_ref"],
            "current_folder_ref": item.get("current_folder_ref"),
            "recommended_folder_ref": item.get("recommended_folder_ref"),
            "debt_document_ref": item.get("debt_document_ref"),
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
    structure_candidate_refs = [
        snapshot.counterparty_ref
        for snapshot in snapshots
        if _needs_structure_lookup_for_status(snapshot, status=status)
    ]
    current_folders = fetch_counterparty_current_folders(
        onec_engine,
        counterparty_refs=counterparty_refs,
    )
    statement_events_by_counterparty = fetch_counterparty_ledger_statement_events(
        session,
        counterparty_refs=structure_candidate_refs,
        snapshot_date=snapshot_date,
    )
    document_departments: dict[str, SaleDocumentDepartmentRow] = {}
    document_structure_checks: dict[str, ReceivableDocumentStructureCheck] = {}
    origin_document_refs = sorted(
        {
            _normalize_ref(snapshot.origin_document_ref)
            for snapshot in snapshots
            if _needs_structure_lookup_for_status(snapshot, status=status)
            and _normalize_ref(snapshot.origin_document_ref)
        }
    )
    if origin_document_refs:
        document_departments.update(
            fetch_sale_document_departments(
                onec_engine,
                document_refs=origin_document_refs,
            )
        )
        document_structure_checks.update(
            fetch_receivable_document_structure_checks(
                onec_engine,
                document_refs=origin_document_refs,
                snapshot_date=snapshot_date,
            )
        )
    open_debt_documents_by_counterparty: dict[str, list[dict[str, Any]]] = {}
    for _ in range(4):
        missing_open_document_refs: set[str] = set()
        open_debt_documents_by_counterparty = {}
        for snapshot in snapshots:
            if not _needs_structure_lookup_for_status(snapshot, status=status):
                continue
            counterparty_key = _ref_key(snapshot.counterparty_ref)
            open_debt_documents = _open_debt_documents_from_statement(
                statement_events=statement_events_by_counterparty.get(counterparty_key, ()),
                current_balance=snapshot.current_balance,
                document_departments=document_departments,
                document_structure_checks=document_structure_checks,
            )
            open_debt_documents_by_counterparty[counterparty_key] = open_debt_documents
            for document in open_debt_documents:
                document_ref = _normalize_ref(document.get("document_ref"))
                document_key = _ref_key(document_ref)
                if document_ref and (
                    document_key not in document_departments
                    or document_key not in document_structure_checks
                ):
                    missing_open_document_refs.add(document_ref)
        if not missing_open_document_refs:
            break
        refs_to_fetch = sorted(missing_open_document_refs)
        document_departments.update(
            fetch_sale_document_departments(
                onec_engine,
                document_refs=refs_to_fetch,
            )
        )
        document_structure_checks.update(
            fetch_receivable_document_structure_checks(
                onec_engine,
                document_refs=refs_to_fetch,
                snapshot_date=snapshot_date,
            )
        )
    china_supplier_refs = {
        _ref_key(value)
        for value in fetch_counterparty_refs_from_onec_group(
            onec_engine,
            group_name="Поставщики Китай",
        )
    }

    items = []
    for snapshot in snapshots:
        counterparty_key = _ref_key(snapshot.counterparty_ref)
        open_debt_documents = open_debt_documents_by_counterparty.get(counterparty_key, [])
        primary_document_ref = (
            _normalize_ref(open_debt_documents[0].get("document_ref"))
            if open_debt_documents
            else None
        )
        items.append(
            _apply_report_suppression(
                _build_item(
                    snapshot,
                    folder_row=current_folders.get(counterparty_key),
                    document_row=document_departments.get(_ref_key(primary_document_ref)),
                    structure_check=document_structure_checks.get(_ref_key(primary_document_ref)),
                    open_debt_documents=open_debt_documents,
                    is_excluded_china_supplier=counterparty_key in china_supplier_refs,
                )
            )
        )
    below_min_balance_count = sum(
        1 for item in items if item.get("suppression_reason") == REVIEW_REASON_BELOW_MIN_BALANCE
    )
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
    review_reason_counts = Counter(
        item["review_reason"]
        for item in items
        if item["status"] == STATUS_NEEDS_REVIEW and item.get("review_reason")
    )
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
            "below_min_balance_count": below_min_balance_count,
            "min_recommendation_balance": MIN_RECOMMENDATION_BALANCE,
            "review_reason_counts": dict(sorted(review_reason_counts.items())),
            "total_open_debt": total_open_debt,
            "move_recommended_amount": move_amount,
        },
        "payload": items,
    }
