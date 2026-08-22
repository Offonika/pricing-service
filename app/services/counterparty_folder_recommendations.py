from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.receivable_balance_snapshot import ReceivableBalanceSnapshot
from app.models.receivable_ledger_event import ReceivableLedgerEvent
from app.services.receivable_canonical_debt_origin import (
    CANONICAL_DEBT_SELECTION_RULE,
    CANONICAL_DEBT_STATUS_MATCHED,
    CanonicalOpenDebtDocument,
    fetch_canonical_open_debt_documents,
)
from app.services.receivable_department_aliases import (
    receivable_department_alias_key,
    receivable_department_display_name,
    receivable_department_names_equivalent,
)
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
QUEUE_ACTIONABLE = "actionable"
QUEUE_BUSINESS_REVIEW = "business_review"
QUEUE_DATA_QUALITY = "data_quality"
QUEUE_EXCLUDED = "excluded"
QUEUE_ALL = "all"
DEFAULT_PAYMENT_TERM_DAYS = 7
MIN_RECOMMENDATION_BALANCE = Decimal("500.00")
PAYMENT_TERM_SOURCE_FALLBACK = "fallback_7_days_read_only"
STRUCTURE_CANDIDATE_OLDEST_SALES_PER_COUNTERPARTY = 20
STRUCTURE_CANDIDATE_AFTER_ORIGIN_SALES_PER_COUNTERPARTY = 40
STRUCTURE_CANDIDATE_LATEST_SALES_PER_COUNTERPARTY = 60
OPEN_DEBT_SOURCE_MAX_LAG_DAYS = 7
OPEN_DEBT_SOURCE_EVENT_TYPES = ("sale", "payment", "return", "settlement")

STATUS_SORT_ORDER = {
    STATUS_MOVE_RECOMMENDED: 0,
    STATUS_NEEDS_REVIEW: 1,
    STATUS_OK: 2,
    STATUS_NO_OVERDUE: 3,
}

QUEUE_SORT_ORDER = {
    QUEUE_ACTIONABLE: 0,
    QUEUE_BUSINESS_REVIEW: 1,
    QUEUE_DATA_QUALITY: 2,
    QUEUE_EXCLUDED: 3,
}

REVIEW_REASON_MISSING_DOCUMENT = "missing_origin_document"
REVIEW_REASON_DOCUMENT_NOT_FOUND = "origin_document_not_found"
REVIEW_REASON_DOCUMENT_DEPARTMENT_MISSING = "origin_document_department_missing"
REVIEW_REASON_DEPARTMENT_FOLDER_MISSING = "department_folder_missing"
REVIEW_REASON_CURRENT_FOLDER_MISSING = "current_counterparty_folder_missing"
REVIEW_REASON_FOLDER_MISMATCH_PAYMENT_TERM_MISSING = "folder_mismatch_payment_term_missing"
REVIEW_REASON_SPB_CROSS_FOLDER = "spb_cross_folder_manual_review"
REVIEW_REASON_MULTIPLE_OPEN_DEBT_FOLDERS = "multiple_open_debt_folders"
REVIEW_REASON_EXCLUDED_EMPLOYEE_FOLDER = "excluded_employee_folder"
REVIEW_REASON_EXCLUDED_WHOLESALE = "excluded_wholesale_counterparty"
REVIEW_REASON_EXCLUDED_SUPPLIER_FOLDER = "excluded_supplier_folder"
REVIEW_REASON_EXCLUDED_SERVICE_COUNTERPARTY = "excluded_service_counterparty"
REVIEW_REASON_EXCLUDED_SITE_PAYMENT_ON_PICKUP = "excluded_site_payment_on_pickup"
REVIEW_REASON_EXCLUDED_MAKLAB_SPB_PROSVET = "excluded_maklab_spb_prosvet"
REVIEW_REASON_BELOW_MIN_BALANCE = "below_min_balance_threshold"
REVIEW_REASON_EXCLUDED_CHINA_SUPPLIER_GROUP = "excluded_china_supplier_group"
REVIEW_REASON_OPEN_STRUCTURE_DOCUMENT_NOT_FOUND = "open_structure_document_not_found"
REVIEW_REASON_ORIGIN_DOCUMENT_NEEDS_ORDER_PAYMENT_CHECK = (
    "origin_document_needs_order_payment_check"
)
REVIEW_REASON_ORIGIN_DOCUMENT_STRUCTURE_UNCONFIRMED = "origin_document_structure_unconfirmed"
REVIEW_REASON_ORIGIN_DOCUMENT_STRUCTURE_CONFIRMED = (
    "origin_document_structure_confirmed_manual_review"
)
REVIEW_REASON_ORIGIN_DOCUMENT_CLOSED_BY_STRUCTURE = "origin_document_closed_by_structure"
REVIEW_REASON_DOCUMENT_COMMENT_HISTORY_REQUIRED = "document_comment_history_required"
REVIEW_REASON_OPEN_DEBT_SOURCE_STALE = "open_debt_source_stale"
REVIEW_REASON_OPEN_DEBT_AMOUNT_MISMATCH = "open_debt_document_amount_mismatch"
REVIEW_REASON_OPEN_DEBT_STATEMENT_MISSING = "open_debt_statement_missing"
REVIEW_REASON_OPEN_DEBT_STRUCTURE_UNCONFIRMED = "open_debt_structure_unconfirmed"
REVIEW_REASON_OPEN_DEBT_TOTAL_BELOW_BALANCE = "open_debt_document_total_below_balance"
REVIEW_REASON_OPEN_DEBT_TOTAL_ABOVE_BALANCE = "open_debt_document_total_above_balance"

OPEN_DEBT_DIAGNOSTIC_MATCHED = "matched"
OPEN_DEBT_DIAGNOSTIC_STATEMENT_MISSING = "statement_missing"
OPEN_DEBT_DIAGNOSTIC_STRUCTURE_UNCONFIRMED = "structure_unconfirmed"
OPEN_DEBT_DIAGNOSTIC_TOTAL_BELOW_BALANCE = "document_total_below_balance"
OPEN_DEBT_DIAGNOSTIC_TOTAL_ABOVE_BALANCE = "document_total_above_balance"

EXCLUDED_SERVICE_COUNTERPARTY_CODES = frozenset({"рб034645"})


@dataclass(frozen=True)
class OpenDebtSourceFreshness:
    source_status: str
    source_max_document_date: datetime | None
    source_lag_days: int | None


def open_debt_documents_match_balance(
    documents: Sequence[dict[str, Any]],
    *,
    current_balance: Decimal,
) -> bool:
    if not documents:
        return Decimal(current_balance).quantize(Decimal("0.01")) == Decimal("0.00")
    balance = Decimal(current_balance).quantize(Decimal("0.01"))
    return abs(open_debt_document_total(documents) - balance) <= Decimal("0.01")


def open_debt_document_total(documents: Sequence[dict[str, Any]]) -> Decimal:
    return sum(
        (Decimal(str(document.get("open_amount") or "0")) for document in documents),
        Decimal("0.00"),
    ).quantize(Decimal("0.01"))


def classify_open_debt_documents(
    documents: Sequence[dict[str, Any]],
    *,
    current_balance: Decimal,
    statement_sale_count: int,
) -> str:
    balance = Decimal(current_balance).quantize(Decimal("0.01"))
    if not documents:
        return (
            OPEN_DEBT_DIAGNOSTIC_STRUCTURE_UNCONFIRMED
            if statement_sale_count
            else OPEN_DEBT_DIAGNOSTIC_STATEMENT_MISSING
        )
    total = open_debt_document_total(documents)
    if abs(total - balance) <= Decimal("0.01"):
        return OPEN_DEBT_DIAGNOSTIC_MATCHED
    if any(
        str(document.get("document_structure_status") or "") != DOCUMENT_STRUCTURE_CONFIRMED_OPEN
        for document in documents
    ):
        return OPEN_DEBT_DIAGNOSTIC_STRUCTURE_UNCONFIRMED
    return (
        OPEN_DEBT_DIAGNOSTIC_TOTAL_BELOW_BALANCE
        if total < balance
        else OPEN_DEBT_DIAGNOSTIC_TOTAL_ABOVE_BALANCE
    )


def open_debt_review_reason(diagnostic: str) -> str:
    return {
        OPEN_DEBT_DIAGNOSTIC_STATEMENT_MISSING: REVIEW_REASON_OPEN_DEBT_STATEMENT_MISSING,
        OPEN_DEBT_DIAGNOSTIC_STRUCTURE_UNCONFIRMED: (REVIEW_REASON_OPEN_DEBT_STRUCTURE_UNCONFIRMED),
        OPEN_DEBT_DIAGNOSTIC_TOTAL_BELOW_BALANCE: (REVIEW_REASON_OPEN_DEBT_TOTAL_BELOW_BALANCE),
        OPEN_DEBT_DIAGNOSTIC_TOTAL_ABOVE_BALANCE: (REVIEW_REASON_OPEN_DEBT_TOTAL_ABOVE_BALANCE),
    }.get(diagnostic, REVIEW_REASON_OPEN_DEBT_AMOUNT_MISMATCH)


def evaluate_open_debt_source_freshness(
    session: Session,
    *,
    snapshot_date: date,
    max_lag_days: int = OPEN_DEBT_SOURCE_MAX_LAG_DAYS,
) -> OpenDebtSourceFreshness:
    snapshot_end = datetime.combine(snapshot_date + timedelta(days=1), time.min)
    source_max_document_date = session.scalar(
        select(func.max(ReceivableLedgerEvent.external_document_date)).where(
            ReceivableLedgerEvent.event_type.in_(OPEN_DEBT_SOURCE_EVENT_TYPES),
            ReceivableLedgerEvent.external_document_date < snapshot_end,
        )
    )
    if source_max_document_date is None:
        return OpenDebtSourceFreshness(
            source_status="source_stale",
            source_max_document_date=None,
            source_lag_days=None,
        )
    source_lag_days = max((snapshot_date - source_max_document_date.date()).days, 0)
    return OpenDebtSourceFreshness(
        source_status=("cache_ready" if source_lag_days <= max_lag_days else "source_stale"),
        source_max_document_date=source_max_document_date,
        source_lag_days=source_lag_days,
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
    document_responsible_ref: str | None
    document_responsible_name: str | None
    document_author_ref: str | None
    document_author_name: str | None
    responsible_department_ref: str | None = None
    responsible_department_name: str | None = None
    responsible_folder_ref: str | None = None
    responsible_folder_name: str | None = None


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


def _folder_alias_key(value: Any) -> str | None:
    return receivable_department_alias_key(value)


def _folder_display_name(value: str | None) -> str | None:
    return receivable_department_display_name(value)


def _folder_names_equivalent(left: str | None, right: str | None) -> bool:
    return receivable_department_names_equivalent(left, right)


def _folders_equivalent(
    folder_row: CounterpartyFolderRow | None,
    document_row: SaleDocumentDepartmentRow | None,
) -> bool:
    recommended_folder_ref, recommended_folder_name, _ = _effective_recommended_folder(document_row)
    return _folder_pair_equivalent(
        folder_row,
        recommended_folder_ref=recommended_folder_ref,
        recommended_folder_name=recommended_folder_name,
    )


def _folder_pair_equivalent(
    folder_row: CounterpartyFolderRow | None,
    *,
    recommended_folder_ref: str | None,
    recommended_folder_name: str | None,
) -> bool:
    if folder_row is None:
        return False
    return _refs_equal(
        folder_row.current_folder_ref,
        recommended_folder_ref,
    ) or _folder_names_equivalent(
        folder_row.current_folder_name,
        recommended_folder_name,
    )


def _effective_recommended_folder(
    document_row: SaleDocumentDepartmentRow | None,
) -> tuple[str | None, str | None, str | None]:
    if document_row is None:
        return None, None, None
    if _is_usable_responsible_folder(
        document_row.responsible_folder_ref,
        document_row.responsible_folder_name,
    ):
        return (
            document_row.responsible_folder_ref,
            document_row.responsible_folder_name,
            "responsible_department",
        )
    return (
        document_row.recommended_folder_ref,
        document_row.recommended_folder_name,
        "document_department",
    )


def _is_usable_responsible_folder(folder_ref: str | None, folder_name: str | None) -> bool:
    if not folder_ref and not folder_name:
        return False
    folder_key = _text_key(folder_name)
    return "уволен" not in folder_key and "курьер" not in folder_key


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("T", " "))
        except ValueError:
            return None
    return None


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
        child_group_match = "UPPER(TRIM(COALESCE(child._Description, ''))) = UPPER(:group_name)"
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
    responsible_ref_expr = _hex_ref_expr("responsible._IDRRef", dialect_name=dialect_name)
    responsible_department_ref_expr = _hex_ref_expr(
        "responsible_department._IDRRef",
        dialect_name=dialect_name,
    )
    responsible_person_department_ref_expr = _hex_ref_expr(
        "responsible_person_department._IDRRef",
        dialect_name=dialect_name,
    )
    responsible_folder_ref_expr = _hex_ref_expr(
        "responsible_folder._IDRRef",
        dialect_name=dialect_name,
    )
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
                    {responsible_ref_expr} AS document_responsible_ref,
                    responsible._Description AS document_responsible_name,
                    COALESCE(
                        {responsible_department_ref_expr},
                        {responsible_person_department_ref_expr}
                    ) AS responsible_department_ref,
                    COALESCE(
                        responsible_department._Description,
                        responsible_person_department._Description
                    ) AS responsible_department_name,
                    {responsible_folder_ref_expr} AS responsible_folder_ref,
                    COALESCE(
                        responsible_folder._Description,
                        responsible_person_department._Description
                    ) AS responsible_folder_name,
                    {author_ref_expr} AS document_author_ref,
                    author._Description AS document_author_name
                FROM _Document203 AS sale {nolock}
                LEFT JOIN _Reference68 AS department {nolock}
                    ON department._IDRRef = sale._Fld4937RRef
                LEFT JOIN _Reference54 AS department_folder {nolock}
                    ON department_folder._IDRRef = department._Fld8927RRef
                LEFT JOIN _Reference69 AS responsible {nolock}
                    ON responsible._IDRRef = sale._Fld4950RRef
                LEFT JOIN _Reference68 AS responsible_department {nolock}
                    ON responsible_department._IDRRef = responsible._Fld9524RRef
                LEFT JOIN _Reference54 AS responsible_folder {nolock}
                    ON responsible_folder._IDRRef = responsible_department._Fld8927RRef
                LEFT JOIN _Reference94 AS responsible_person {nolock}
                    ON responsible_person._IDRRef = responsible._Fld915RRef
                LEFT JOIN _Reference94 AS responsible_person_department {nolock}
                    ON responsible_person_department._IDRRef = responsible_person._ParentIDRRef
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
                    document_responsible_ref=(
                        _normalize_ref(row.get("document_responsible_ref")) or None
                    ),
                    document_responsible_name=(
                        _normalize_ref(row.get("document_responsible_name")) or None
                    ),
                    responsible_department_ref=(
                        _normalize_ref(row.get("responsible_department_ref")) or None
                    ),
                    responsible_department_name=(
                        _normalize_ref(row.get("responsible_department_name")) or None
                    ),
                    responsible_folder_ref=_normalize_ref(row.get("responsible_folder_ref"))
                    or None,
                    responsible_folder_name=_normalize_ref(row.get("responsible_folder_name"))
                    or None,
                    document_author_ref=_normalize_ref(row.get("document_author_ref")) or None,
                    document_author_name=_normalize_ref(row.get("document_author_name")) or None,
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
    include_opening_balance: bool = False,
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
                        (
                            "sale",
                            "payment",
                            "return",
                            "settlement",
                            "debt_adjustment",
                            *(("opening_balance",) if include_opening_balance else ()),
                        )
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
    recommended_folder_ref, recommended_folder_name, _ = _effective_recommended_folder(document_row)
    if not document_row.document_department_ref and not document_row.responsible_department_ref:
        return REVIEW_REASON_DOCUMENT_DEPARTMENT_MISSING
    if not recommended_folder_ref and not recommended_folder_name:
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
    has_selected_debt_document = debt_document_date is not None
    effective_origin_date = debt_document_date or snapshot.origin_document_date
    if (
        effective_origin_date is not None
        and credit_depth_days
        and (has_selected_debt_document or due_date is None)
    ):
        due_date = effective_origin_date + timedelta(days=credit_depth_days)

    if _has_missing_payment_term(snapshot) and effective_origin_date is not None:
        credit_depth_days = DEFAULT_PAYMENT_TERM_DAYS
        due_date = effective_origin_date + timedelta(days=DEFAULT_PAYMENT_TERM_DAYS)
        source = PAYMENT_TERM_SOURCE_FALLBACK

    overdue_days = (
        snapshot.overdue_days
        if snapshot.overdue_days is not None
        and source != PAYMENT_TERM_SOURCE_FALLBACK
        and not has_selected_debt_document
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


def _is_maklab_spb_prosvet(*, counterparty_code: str | None, counterparty_name: str | None) -> bool:
    return _text_key(counterparty_code) == "рб028196" or (
        "маклаб" in _text_key(counterparty_name) and "просвет" in _text_key(counterparty_name)
    )


def _is_site_folder(current_folder_name: str | None) -> bool:
    return _folder_alias_key(current_folder_name) == "online_store"


def _is_supplier_context(
    *,
    current_folder_name: str | None,
    recommended_folder_name: str | None,
) -> bool:
    return any(
        "поставщик" in _text_key(value) for value in (current_folder_name, recommended_folder_name)
    )


def _counterparty_exception_reason(
    *,
    snapshot: ReceivableBalanceSnapshot,
    folder_row: CounterpartyFolderRow | None,
    recommended_folder_name: str | None = None,
) -> str | None:
    counterparty_code = snapshot.counterparty_code or (
        folder_row.counterparty_code if folder_row else None
    )
    if _text_key(counterparty_code) in EXCLUDED_SERVICE_COUNTERPARTY_CODES:
        return REVIEW_REASON_EXCLUDED_SERVICE_COUNTERPARTY
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
    if _is_supplier_context(
        current_folder_name=folder_row.current_folder_name if folder_row else None,
        recommended_folder_name=recommended_folder_name,
    ):
        return REVIEW_REASON_EXCLUDED_SUPPLIER_FOLDER
    if _is_site_payment_on_pickup(snapshot.counterparty_name):
        return REVIEW_REASON_EXCLUDED_SITE_PAYMENT_ON_PICKUP
    if _is_maklab_spb_prosvet(
        counterparty_code=folder_row.counterparty_code if folder_row else None,
        counterparty_name=snapshot.counterparty_name,
    ):
        return REVIEW_REASON_EXCLUDED_MAKLAB_SPB_PROSVET
    return None


def _is_spb_cross_folder(
    *,
    current_folder_name: str | None,
    recommended_folder_name: str | None,
) -> bool:
    current = _text_key(current_folder_name)
    recommended = _text_key(recommended_folder_name)
    if _folder_names_equivalent(current_folder_name, recommended_folder_name):
        return False
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
        REVIEW_REASON_ORIGIN_DOCUMENT_STRUCTURE_UNCONFIRMED,
        REVIEW_REASON_ORIGIN_DOCUMENT_STRUCTURE_CONFIRMED,
        REVIEW_REASON_ORIGIN_DOCUMENT_CLOSED_BY_STRUCTURE,
        REVIEW_REASON_OPEN_STRUCTURE_DOCUMENT_NOT_FOUND,
        REVIEW_REASON_DOCUMENT_COMMENT_HISTORY_REQUIRED,
    }


def _is_excluded_reason(reason: str | None) -> bool:
    return reason in {
        REVIEW_REASON_EXCLUDED_CHINA_SUPPLIER_GROUP,
        REVIEW_REASON_EXCLUDED_EMPLOYEE_FOLDER,
        REVIEW_REASON_EXCLUDED_WHOLESALE,
        REVIEW_REASON_EXCLUDED_SUPPLIER_FOLDER,
        REVIEW_REASON_EXCLUDED_SERVICE_COUNTERPARTY,
        REVIEW_REASON_EXCLUDED_SITE_PAYMENT_ON_PICKUP,
        REVIEW_REASON_EXCLUDED_MAKLAB_SPB_PROSVET,
        REVIEW_REASON_ORIGIN_DOCUMENT_NEEDS_ORDER_PAYMENT_CHECK,
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
    recommended_folder_name: str | None,
) -> str | None:
    counterparty_exception = _counterparty_exception_reason(
        snapshot=snapshot,
        folder_row=folder_row,
        recommended_folder_name=recommended_folder_name,
    )
    if counterparty_exception:
        return counterparty_exception
    if _is_site_folder(folder_row.current_folder_name):
        return REVIEW_REASON_ORIGIN_DOCUMENT_NEEDS_ORDER_PAYMENT_CHECK
    if _is_spb_cross_folder(
        current_folder_name=folder_row.current_folder_name,
        recommended_folder_name=recommended_folder_name,
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
        recommended_folder_ref, recommended_folder_name, recommended_folder_source = (
            _effective_recommended_folder(document_row)
        )
        documents.append(
            {
                "document_ref": event.document_ref,
                "document_number": structure_check.sale_number or event.document_number,
                "document_date": _coerce_datetime(structure_check.sale_date) or event.document_date,
                "open_amount": structure_check.open_amount,
                "sale_amount": structure_check.sale_amount,
                "closing_amount": structure_check.closing_amount,
                "manager_ref": event.manager_ref,
                "manager_name": event.manager_name,
                "document_responsible_ref": (
                    document_row.document_responsible_ref if document_row else None
                ),
                "document_responsible_name": (
                    document_row.document_responsible_name if document_row else None
                ),
                "document_author_ref": (document_row.document_author_ref if document_row else None),
                "document_author_name": (
                    document_row.document_author_name if document_row else None
                ),
                "debt_department_ref": (
                    document_row.document_department_ref if document_row else None
                ),
                "debt_department_name": (
                    document_row.document_department_name if document_row else None
                ),
                "document_department_recommended_folder_ref": (
                    document_row.recommended_folder_ref if document_row else None
                ),
                "document_department_recommended_folder_name": (
                    document_row.recommended_folder_name if document_row else None
                ),
                "document_responsible_department_ref": (
                    document_row.responsible_department_ref if document_row else None
                ),
                "document_responsible_department_name": (
                    document_row.responsible_department_name if document_row else None
                ),
                "document_responsible_folder_ref": (
                    document_row.responsible_folder_ref if document_row else None
                ),
                "document_responsible_folder_name": (
                    document_row.responsible_folder_name if document_row else None
                ),
                "recommended_folder_ref": recommended_folder_ref,
                "recommended_folder_name": recommended_folder_name,
                "recommended_folder_source": recommended_folder_source,
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
        recommended_folder_ref, recommended_folder_name, recommended_folder_source = (
            _effective_recommended_folder(document_row)
        )
        documents.append(
            {
                "document_ref": document.document_ref,
                "document_number": (
                    structure_check.sale_number
                    if structure_check and structure_check.sale_number
                    else document.document_number
                ),
                "document_date": (
                    _coerce_datetime(structure_check.sale_date)
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
                "document_responsible_ref": (
                    document_row.document_responsible_ref if document_row else None
                ),
                "document_responsible_name": (
                    document_row.document_responsible_name if document_row else None
                ),
                "document_author_ref": (document_row.document_author_ref if document_row else None),
                "document_author_name": (
                    document_row.document_author_name if document_row else None
                ),
                "debt_department_ref": (
                    document_row.document_department_ref if document_row else None
                ),
                "debt_department_name": (
                    document_row.document_department_name if document_row else None
                ),
                "document_department_recommended_folder_ref": (
                    document_row.recommended_folder_ref if document_row else None
                ),
                "document_department_recommended_folder_name": (
                    document_row.recommended_folder_name if document_row else None
                ),
                "document_responsible_department_ref": (
                    document_row.responsible_department_ref if document_row else None
                ),
                "document_responsible_department_name": (
                    document_row.responsible_department_name if document_row else None
                ),
                "document_responsible_folder_ref": (
                    document_row.responsible_folder_ref if document_row else None
                ),
                "document_responsible_folder_name": (
                    document_row.responsible_folder_name if document_row else None
                ),
                "recommended_folder_ref": recommended_folder_ref,
                "recommended_folder_name": recommended_folder_name,
                "recommended_folder_source": recommended_folder_source,
                "document_structure_status": (structure_check.status if structure_check else None),
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
                "statement_balance_after": document.statement_balance_after,
                "statement_segment_start_row": document.statement_segment_start_row,
                "statement_segment_end_row": document.statement_segment_end_row,
                "statement_match_details": list(document.statement_match_details),
            }
        )
    return documents


def _open_debt_documents_from_canonical_origin(
    documents: Sequence[CanonicalOpenDebtDocument],
    *,
    document_departments: dict[str, SaleDocumentDepartmentRow],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for document in documents:
        document_key = _ref_key(document.document_ref)
        document_row = document_departments.get(document_key)
        recommended_folder_ref, recommended_folder_name, recommended_folder_source = (
            _effective_recommended_folder(document_row)
        )
        result.append(
            {
                "document_ref": document.document_ref,
                "document_number": document.document_number,
                "document_date": document.document_date,
                "open_amount": document.open_amount,
                "sale_amount": document.gross_amount,
                "closing_amount": document.closing_amount,
                "return_amount": Decimal("0.00"),
                "manager_ref": None,
                "manager_name": None,
                "document_responsible_ref": (
                    document_row.document_responsible_ref if document_row else None
                ),
                "document_responsible_name": (
                    document_row.document_responsible_name if document_row else None
                ),
                "document_author_ref": (document_row.document_author_ref if document_row else None),
                "document_author_name": (
                    document_row.document_author_name if document_row else None
                ),
                "debt_department_ref": (
                    document_row.document_department_ref if document_row else None
                ),
                "debt_department_name": (
                    document_row.document_department_name if document_row else None
                ),
                "document_department_recommended_folder_ref": (
                    document_row.recommended_folder_ref if document_row else None
                ),
                "document_department_recommended_folder_name": (
                    document_row.recommended_folder_name if document_row else None
                ),
                "document_responsible_department_ref": (
                    document_row.responsible_department_ref if document_row else None
                ),
                "document_responsible_department_name": (
                    document_row.responsible_department_name if document_row else None
                ),
                "document_responsible_folder_ref": (
                    document_row.responsible_folder_ref if document_row else None
                ),
                "document_responsible_folder_name": (
                    document_row.responsible_folder_name if document_row else None
                ),
                "recommended_folder_ref": recommended_folder_ref,
                "recommended_folder_name": recommended_folder_name,
                "recommended_folder_source": recommended_folder_source,
                "document_structure_status": DOCUMENT_STRUCTURE_CONFIRMED_OPEN,
                "document_structure_order_ref": None,
                "document_structure_order_number": None,
                "document_structure_order_date": None,
                "document_structure_linked_documents": [],
                "statement_selection_rule": CANONICAL_DEBT_SELECTION_RULE,
                "statement_balance_after": document.open_amount,
                "statement_segment_start_row": None,
                "statement_segment_end_row": None,
                "statement_match_details": [
                    {
                        "rule": CANONICAL_DEBT_SELECTION_RULE,
                        "gross_amount": document.gross_amount,
                        "open_amount": document.open_amount,
                    }
                ],
            }
        )
    return result


def _candidate_sale_events_for_structure(
    snapshot: ReceivableBalanceSnapshot,
    sale_events: Sequence[ReceivableStatementEvent],
) -> list[ReceivableStatementEvent]:
    events = sorted(
        (
            event
            for event in sale_events
            if event.event_type == "sale" and Decimal(event.amount_delta) > Decimal("0")
        ),
        key=lambda row: (row.document_date, row.document_ref),
    )
    if not events:
        return []

    selected_by_ref: dict[str, ReceivableStatementEvent] = {}

    def add(items: Sequence[ReceivableStatementEvent]) -> None:
        for item in items:
            selected_by_ref.setdefault(_ref_key(item.document_ref), item)

    add(events[:STRUCTURE_CANDIDATE_OLDEST_SALES_PER_COUNTERPARTY])
    add(events[-STRUCTURE_CANDIDATE_LATEST_SALES_PER_COUNTERPARTY:])
    if snapshot.origin_document_ref:
        origin_key = _ref_key(snapshot.origin_document_ref)
        add([item for item in events if _ref_key(item.document_ref) == origin_key])
    if snapshot.origin_document_date is not None:
        add(
            [item for item in events if item.document_date >= snapshot.origin_document_date][
                :STRUCTURE_CANDIDATE_AFTER_ORIGIN_SALES_PER_COUNTERPARTY
            ]
        )

    return sorted(selected_by_ref.values(), key=lambda row: (row.document_date, row.document_ref))


def build_open_debt_documents_by_counterparty(
    session: Session,
    *,
    onec_engine=None,
    snapshots: Sequence[ReceivableBalanceSnapshot],
    snapshot_date: date,
    status: str | None = None,
    include_onec_enrichment: bool = True,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    structure_candidate_refs = [
        snapshot.counterparty_ref
        for snapshot in snapshots
        if _needs_structure_lookup_for_status(snapshot, status=status)
    ]
    statement_events_by_counterparty = fetch_counterparty_ledger_statement_events(
        session,
        counterparty_refs=structure_candidate_refs,
        snapshot_date=snapshot_date,
    )
    if diagnostics is not None:
        diagnostics["statement_sale_counts"] = {
            key: sum(
                1
                for event in events
                if event.event_type == "sale" and Decimal(event.amount_delta) > Decimal("0")
            )
            for key, events in statement_events_by_counterparty.items()
        }
    should_enrich_from_onec = include_onec_enrichment and onec_engine is not None
    if should_enrich_from_onec and onec_engine.dialect.name == "mssql":
        canonical_batch = fetch_canonical_open_debt_documents(
            onec_engine,
            counterparty_balances={
                snapshot.counterparty_ref: snapshot.current_balance
                for snapshot in snapshots
                if _needs_structure_lookup_for_status(snapshot, status=status)
            },
            snapshot_date=snapshot_date,
        )
        if canonical_batch.supported:
            selected_document_refs = sorted(
                {
                    document.document_ref
                    for documents in canonical_batch.documents_by_counterparty.values()
                    for document in documents
                }
            )
            canonical_document_departments = (
                fetch_sale_document_departments(
                    onec_engine,
                    document_refs=selected_document_refs,
                )
                if selected_document_refs
                else {}
            )
            if diagnostics is not None:
                diagnostics["canonical_opening_period"] = canonical_batch.opening_period
                diagnostics["canonical_origin_statuses"] = {
                    key: resolution.status
                    for key, resolution in canonical_batch.resolutions_by_counterparty.items()
                }
                diagnostics["canonical_matched_count"] = sum(
                    resolution.status == CANONICAL_DEBT_STATUS_MATCHED
                    for resolution in canonical_batch.resolutions_by_counterparty.values()
                )
            return {
                _ref_key(snapshot.counterparty_ref): _open_debt_documents_from_canonical_origin(
                    canonical_batch.documents_by_counterparty.get(
                        _ref_key(snapshot.counterparty_ref),
                        (),
                    ),
                    document_departments=canonical_document_departments,
                )
                for snapshot in snapshots
                if _needs_structure_lookup_for_status(snapshot, status=status)
            }
    document_departments: dict[str, SaleDocumentDepartmentRow] = {}
    document_structure_checks: dict[str, ReceivableDocumentStructureCheck] = {}
    structure_document_refs: set[str] = set()
    for snapshot in snapshots:
        if not _needs_structure_lookup_for_status(snapshot, status=status):
            continue
        origin_document_ref = _normalize_ref(snapshot.origin_document_ref)
        if origin_document_ref:
            structure_document_refs.add(origin_document_ref)
        counterparty_key = _ref_key(snapshot.counterparty_ref)
        structure_document_refs.update(
            event.document_ref
            for event in _candidate_sale_events_for_structure(
                snapshot,
                statement_events_by_counterparty.get(counterparty_key, ()),
            )
        )
    initial_structure_refs = sorted(structure_document_refs)
    if should_enrich_from_onec and initial_structure_refs:
        document_departments.update(
            fetch_sale_document_departments(
                onec_engine,
                document_refs=initial_structure_refs,
            )
        )
        document_structure_checks.update(
            fetch_receivable_document_structure_checks(
                onec_engine,
                document_refs=initial_structure_refs,
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
        if not missing_open_document_refs or not should_enrich_from_onec:
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
    return open_debt_documents_by_counterparty


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
        _normalize_ref(primary_open_document.get("document_ref")) if primary_open_document else None
    )
    debt_document_number = (
        _normalize_ref(primary_open_document.get("document_number"))
        if primary_open_document
        else None
    )
    debt_document_date = (
        primary_open_document.get("document_date") if primary_open_document else None
    )
    debt_document_date = _coerce_datetime(debt_document_date)
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
    debt_document_responsible_ref = (
        _normalize_ref(primary_open_document.get("document_responsible_ref"))
        if primary_open_document
        else None
    )
    debt_document_responsible_name = (
        _normalize_ref(primary_open_document.get("document_responsible_name"))
        if primary_open_document
        else None
    )
    statement_selection_rule = (
        _normalize_ref(primary_open_document.get("statement_selection_rule"))
        if primary_open_document
        else None
    )
    statement_balance_after = (
        primary_open_document.get("statement_balance_after") if primary_open_document else None
    )
    statement_segment_start_row = (
        primary_open_document.get("statement_segment_start_row") if primary_open_document else None
    )
    statement_segment_end_row = (
        primary_open_document.get("statement_segment_end_row") if primary_open_document else None
    )
    term = _effective_payment_term(snapshot, debt_document_date=debt_document_date)
    recommended_folder_ref, recommended_folder_name, recommended_folder_source = (
        _effective_recommended_folder(document_row)
    )
    if not (recommended_folder_ref or recommended_folder_name) and primary_open_document:
        recommended_folder_ref = (
            _normalize_ref(primary_open_document.get("recommended_folder_ref")) or None
        )
        recommended_folder_name = (
            _normalize_ref(primary_open_document.get("recommended_folder_name")) or None
        )
        recommended_folder_source = (
            _normalize_ref(primary_open_document.get("recommended_folder_source")) or None
        )
    counterparty_exception = _counterparty_exception_reason(
        snapshot=snapshot,
        folder_row=folder_row,
        recommended_folder_name=recommended_folder_name,
    )
    exclusion_reason = (
        REVIEW_REASON_EXCLUDED_CHINA_SUPPLIER_GROUP
        if is_excluded_china_supplier
        else counterparty_exception
    )
    open_document_folder_keys = {
        _ref_key(document.get("recommended_folder_ref") or document.get("recommended_folder_name"))
        for document in open_debt_documents
        if document.get("recommended_folder_ref") or document.get("recommended_folder_name")
    }
    business_review_reason = None
    if len(open_document_folder_keys) > 1:
        business_review_reason = REVIEW_REASON_MULTIPLE_OPEN_DEBT_FOLDERS
        recommended_folder_ref = None
        recommended_folder_name = None
        recommended_folder_source = None
    elif folder_row and _is_spb_cross_folder(
        current_folder_name=folder_row.current_folder_name,
        recommended_folder_name=recommended_folder_name,
    ):
        business_review_reason = REVIEW_REASON_SPB_CROSS_FOLDER
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
        folders_match = _folder_pair_equivalent(
            folder_row,
            recommended_folder_ref=recommended_folder_ref,
            recommended_folder_name=recommended_folder_name,
        )
        exception_reason = None
        if not folders_match:
            exception_reason = _folder_mismatch_exception_reason(
                snapshot=snapshot,
                folder_row=folder_row,
                recommended_folder_name=recommended_folder_name,
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
        "counterparty_code": snapshot.counterparty_code
        or (folder_row.counterparty_code if folder_row else None),
        "counterparty_name": snapshot.counterparty_name,
        "snapshot_department_ref": snapshot.department_ref,
        "snapshot_department_name": snapshot.department_name,
        "current_balance": snapshot.current_balance,
        "current_folder_ref": folder_row.current_folder_ref if folder_row else None,
        "current_folder_name": folder_row.current_folder_name if folder_row else None,
        "current_folder_display_name": (
            _folder_display_name(folder_row.current_folder_name) if folder_row else None
        ),
        "recommended_folder_ref": recommended_folder_ref,
        "recommended_folder_name": recommended_folder_name,
        "recommended_folder_display_name": (
            _folder_display_name(recommended_folder_name) if recommended_folder_name else None
        ),
        "recommended_folder_source": recommended_folder_source,
        "debt_department_ref": (document_row.document_department_ref if document_row else None),
        "debt_department_name": (document_row.document_department_name if document_row else None),
        "debt_department_display_name": (
            _folder_display_name(document_row.document_department_name) if document_row else None
        ),
        "debt_document_responsible_department_ref": (
            document_row.responsible_department_ref if document_row else None
        ),
        "debt_document_responsible_department_name": (
            document_row.responsible_department_name if document_row else None
        ),
        "debt_document_responsible_folder_ref": (
            document_row.responsible_folder_ref if document_row else None
        ),
        "debt_document_responsible_folder_name": (
            document_row.responsible_folder_name if document_row else None
        ),
        "snapshot_department_display_name": _folder_display_name(snapshot.department_name),
        "debt_document_ref": debt_document_ref,
        "debt_document_number": debt_document_number,
        "debt_document_date": debt_document_date,
        "debt_document_responsible_ref": debt_document_responsible_ref,
        "debt_document_responsible_name": debt_document_responsible_name,
        "debt_document_author_ref": debt_document_author_ref,
        "debt_document_author_name": debt_document_author_name,
        "open_debt_documents": list(open_debt_documents),
        "statement_balance_after": statement_balance_after,
        "statement_segment_start_row": statement_segment_start_row,
        "statement_segment_end_row": statement_segment_end_row,
        "statement_selection_rule": statement_selection_rule,
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
        "exclusion_reason": exclusion_reason,
        "business_review_reason": business_review_reason,
        "document_structure_status": structure_check.status if structure_check else None,
        "document_structure_open_amount": structure_check.open_amount if structure_check else None,
        "document_structure_sale_amount": structure_check.sale_amount if structure_check else None,
        "document_structure_closing_amount": (
            structure_check.closing_amount if structure_check else None
        ),
        "document_structure_order_ref": structure_check.order_ref if structure_check else None,
        "document_structure_order_number": (
            structure_check.order_number if structure_check else None
        ),
        "document_structure_order_date": structure_check.order_date if structure_check else None,
        "document_structure_linked_documents": (
            list(structure_check.linked_documents) if structure_check else []
        ),
    }


def _is_actionable_status(status: str | None) -> bool:
    return status in {STATUS_MOVE_RECOMMENDED, STATUS_NEEDS_REVIEW}


def _folder_identity(item: dict[str, Any], prefix: str) -> str:
    return _ref_key(
        item.get(f"{prefix}_folder_ref")
        or item.get(f"{prefix}_folder_name")
        or item.get(f"{prefix}_folder_display_name")
    )


def classify_folder_recommendation_queue(
    item: dict[str, Any], *, source_status: str = "cache_ready"
) -> str:
    review_reason = str(item.get("review_reason") or "")
    exclusion_reason = str(item.get("exclusion_reason") or "")
    business_review_reason = str(item.get("business_review_reason") or "")
    if (
        _is_excluded_reason(exclusion_reason)
        or _is_excluded_reason(review_reason)
        or review_reason == REVIEW_REASON_BELOW_MIN_BALANCE
    ):
        return QUEUE_EXCLUDED
    if source_status != "cache_ready" or review_reason == REVIEW_REASON_OPEN_DEBT_SOURCE_STALE:
        return QUEUE_DATA_QUALITY
    if business_review_reason or review_reason in {
        REVIEW_REASON_SPB_CROSS_FOLDER,
        REVIEW_REASON_MULTIPLE_OPEN_DEBT_FOLDERS,
        REVIEW_REASON_DOCUMENT_COMMENT_HISTORY_REQUIRED,
    }:
        return QUEUE_BUSINESS_REVIEW
    if str(item.get("status") or "") in {STATUS_OK, STATUS_NO_OVERDUE}:
        return QUEUE_EXCLUDED
    current_folder = _folder_identity(item, "current")
    recommended_folder = _folder_identity(item, "recommended")
    if (
        review_reason == REVIEW_REASON_ORIGIN_DOCUMENT_STRUCTURE_CONFIRMED
        and bool(item.get("is_overdue"))
        and Decimal(str(item.get("current_balance") or "0")) >= MIN_RECOMMENDATION_BALANCE
        and current_folder
        and recommended_folder
        and current_folder != recommended_folder
    ):
        return QUEUE_ACTIONABLE
    return QUEUE_DATA_QUALITY


def folder_recommendation_signal_key(item: dict[str, Any]) -> str:
    raw = json.dumps(
        {
            "counterparty_ref": _ref_key(item.get("counterparty_ref")),
            "current_folder": _folder_identity(item, "current"),
            "recommended_folder": _folder_identity(item, "recommended"),
            "document_ref": _ref_key(
                item.get("debt_document_ref") or item.get("origin_document_ref")
            ),
            "review_reason": str(item.get("review_reason") or ""),
            "exclusion_reason": str(item.get("exclusion_reason") or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def enrich_folder_recommendation_item(
    item: dict[str, Any], *, source_status: str = "cache_ready"
) -> dict[str, Any]:
    enriched = dict(item)
    queue = classify_folder_recommendation_queue(enriched, source_status=source_status)
    enriched["signal_key"] = folder_recommendation_signal_key(enriched)
    enriched["queue"] = queue
    enriched["action_required"] = queue == QUEUE_ACTIONABLE
    return enriched


def _apply_document_mismatch_guard(
    item: dict[str, Any],
    *,
    diagnostic: str,
) -> dict[str, Any]:
    guarded = dict(item)
    excluded = _is_excluded_reason(guarded.get("exclusion_reason")) or _is_excluded_reason(
        guarded.get("review_reason")
    )
    mismatch_reason = open_debt_review_reason(diagnostic)
    guarded.update(
        {
            "open_debt_source_status": "document_mismatch",
            "document_mismatch_reason": mismatch_reason,
            "status": STATUS_NO_OVERDUE if excluded else STATUS_NEEDS_REVIEW,
            "review_reason": mismatch_reason,
            "business_review_reason": None,
            "recommended_folder_ref": None,
            "recommended_folder_name": None,
            "recommended_folder_display_name": None,
            "recommended_folder_source": None,
            "debt_document_ref": None,
            "debt_document_number": None,
            "debt_document_date": None,
            "debt_document_responsible_ref": None,
            "debt_document_responsible_name": None,
            "debt_document_author_ref": None,
            "debt_document_author_name": None,
            "open_debt_documents": [],
            "statement_balance_after": None,
            "statement_segment_start_row": None,
            "statement_segment_end_row": None,
            "statement_selection_rule": None,
            "origin_document_ref": None,
            "origin_document_number": None,
            "origin_document_date": None,
            "due_date": None,
            "overdue_days": None,
            "is_overdue": False,
            "effective_credit_depth_days": None,
            "effective_payment_term_source": None,
            "effective_due_date": None,
            "effective_overdue_days": None,
        }
    )
    return guarded


def _apply_report_suppression(item: dict[str, Any]) -> dict[str, Any]:
    if Decimal(item.get("current_balance") or 0) < MIN_RECOMMENDATION_BALANCE:
        item = dict(item)
        item["suppressed_from_daily_report"] = True
        item["suppression_reason"] = REVIEW_REASON_BELOW_MIN_BALANCE
        if _is_actionable_status(str(item.get("status"))):
            item["status"] = STATUS_NO_OVERDUE
            if item.get("open_debt_source_status") != "document_mismatch":
                item["review_reason"] = REVIEW_REASON_BELOW_MIN_BALANCE
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


def _snapshot_candidate_sort_key(
    snapshot: ReceivableBalanceSnapshot,
) -> tuple[int, int, Decimal, str]:
    term = _effective_payment_term(snapshot)
    balance = Decimal(snapshot.current_balance or 0)
    actionable_rank = 0 if term.is_overdue and balance >= MIN_RECOMMENDATION_BALANCE else 1
    return (
        actionable_rank,
        -(term.overdue_days or 0),
        -balance,
        str(snapshot.counterparty_name or snapshot.counterparty_ref or ""),
    )


def _build_report_revision(snapshot_date: date, items: Sequence[dict[str, Any]]) -> str:
    revision_payload = [
        {
            "counterparty_ref": item["counterparty_ref"],
            "current_folder_ref": item.get("current_folder_ref"),
            "recommended_folder_ref": item.get("recommended_folder_ref"),
            "debt_document_ref": item.get("debt_document_ref"),
            "status": item.get("status"),
            "review_reason": item.get("review_reason"),
            "queue": item.get("queue"),
            "exclusion_reason": item.get("exclusion_reason"),
            "business_review_reason": item.get("business_review_reason"),
            "open_debt_source_status": item.get("open_debt_source_status"),
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
    queue: str = QUEUE_ALL,
    candidate_limit: int | None = None,
    snapshot_department_refs: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    allowed_statuses = {
        STATUS_MOVE_RECOMMENDED,
        STATUS_OK,
        STATUS_NO_OVERDUE,
        STATUS_NEEDS_REVIEW,
    }
    if status is not None and status not in allowed_statuses:
        raise ValueError(f"unsupported status: {status}")
    allowed_queues = {
        QUEUE_ACTIONABLE,
        QUEUE_BUSINESS_REVIEW,
        QUEUE_DATA_QUALITY,
        QUEUE_EXCLUDED,
        QUEUE_ALL,
    }
    if queue not in allowed_queues:
        raise ValueError(f"unsupported queue: {queue}")

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
    source_snapshot_count = len(snapshots)
    if snapshot_department_refs is not None:
        allowed_departments = {_ref_key(value) for value in snapshot_department_refs if value}
        snapshots = [
            snapshot
            for snapshot in snapshots
            if _ref_key(snapshot.department_ref) in allowed_departments
        ]
        source_snapshot_count = len(snapshots)
    if candidate_limit is not None and candidate_limit > 0:
        snapshots = sorted(snapshots, key=_snapshot_candidate_sort_key)[:candidate_limit]

    counterparty_refs = [snapshot.counterparty_ref for snapshot in snapshots]
    source_freshness = evaluate_open_debt_source_freshness(
        session,
        snapshot_date=snapshot_date,
    )
    current_folders = fetch_counterparty_current_folders(
        onec_engine,
        counterparty_refs=counterparty_refs,
    )
    open_debt_diagnostics: dict[str, Any] = {}
    open_debt_documents_by_counterparty = (
        build_open_debt_documents_by_counterparty(
            session,
            onec_engine=onec_engine,
            snapshots=snapshots,
            snapshot_date=snapshot_date,
            status=status,
            diagnostics=open_debt_diagnostics,
        )
        if source_freshness.source_status == "cache_ready"
        else {}
    )
    document_refs = sorted(
        {
            document_ref
            for documents in open_debt_documents_by_counterparty.values()
            for document in documents
            for document_ref in (_normalize_ref(document.get("document_ref")),)
            if document_ref
        }
    )
    document_departments = (
        fetch_sale_document_departments(onec_engine, document_refs=document_refs)
        if document_refs
        else {}
    )
    document_structure_checks = (
        fetch_receivable_document_structure_checks(
            onec_engine,
            document_refs=document_refs,
            snapshot_date=snapshot_date,
        )
        if document_refs
        else {}
    )
    china_supplier_refs = {
        _ref_key(value)
        for value in fetch_counterparty_refs_from_onec_group(
            onec_engine,
            group_name="Поставщики Китай",
        )
    }

    items = []
    statement_sale_counts = open_debt_diagnostics.get("statement_sale_counts") or {}
    for snapshot in snapshots:
        counterparty_key = _ref_key(snapshot.counterparty_ref)
        open_debt_documents = open_debt_documents_by_counterparty.get(counterparty_key, [])
        document_diagnostic = classify_open_debt_documents(
            open_debt_documents,
            current_balance=snapshot.current_balance,
            statement_sale_count=int(statement_sale_counts.get(counterparty_key) or 0),
        )
        document_amount_mismatch = (
            source_freshness.source_status == "cache_ready"
            and document_diagnostic != OPEN_DEBT_DIAGNOSTIC_MATCHED
        )
        primary_document_ref = (
            _normalize_ref(open_debt_documents[0].get("document_ref"))
            if open_debt_documents
            else None
        )
        item = _build_item(
            snapshot,
            folder_row=current_folders.get(counterparty_key),
            document_row=document_departments.get(_ref_key(primary_document_ref)),
            structure_check=document_structure_checks.get(_ref_key(primary_document_ref)),
            open_debt_documents=open_debt_documents,
            is_excluded_china_supplier=counterparty_key in china_supplier_refs,
        )
        if document_amount_mismatch:
            item = _apply_document_mismatch_guard(
                item,
                diagnostic=document_diagnostic,
            )
        if source_freshness.source_status == "source_stale":
            item = dict(item)
            excluded = _is_excluded_reason(item.get("exclusion_reason"))
            item.update(
                {
                    "status": STATUS_NO_OVERDUE if excluded else STATUS_NEEDS_REVIEW,
                    "review_reason": REVIEW_REASON_OPEN_DEBT_SOURCE_STALE,
                    "recommended_folder_ref": None,
                    "recommended_folder_name": None,
                    "recommended_folder_display_name": None,
                    "recommended_folder_source": None,
                    "debt_document_ref": None,
                    "debt_document_number": None,
                    "debt_document_date": None,
                    "open_debt_documents": [],
                }
            )
        item = _apply_report_suppression(item)
        items.append(
            enrich_folder_recommendation_item(
                item,
                source_status=source_freshness.source_status,
            )
        )
    below_min_balance_count = sum(
        1 for item in items if item.get("suppression_reason") == REVIEW_REASON_BELOW_MIN_BALANCE
    )
    if status is not None:
        items = [item for item in items if item["status"] == status]
    queue_counts_all = Counter(str(item.get("queue") or "") for item in items)
    if queue != QUEUE_ALL:
        items = [item for item in items if item.get("queue") == queue]

    items.sort(
        key=lambda item: (
            QUEUE_SORT_ORDER.get(str(item.get("queue")), 99),
            STATUS_SORT_ORDER.get(str(item["status"]), 99),
            -(int(item.get("overdue_days") or 0)),
            -(item.get("current_balance") or Decimal("0")),
            str(item.get("counterparty_name") or ""),
        )
    )
    if limit is not None:
        items = items[:limit]

    status_counts = Counter(item["status"] for item in items)
    document_mismatch_count = sum(
        item.get("open_debt_source_status") == "document_mismatch" for item in items
    )
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
        "source_status": source_freshness.source_status,
        "source_max_document_date": source_freshness.source_max_document_date,
        "source_lag_days": source_freshness.source_lag_days,
        "report_revision": _build_report_revision(snapshot_date, items),
        "summary": {
            "total_count": len(items),
            "source_snapshot_count": source_snapshot_count,
            "move_recommended_count": status_counts[STATUS_MOVE_RECOMMENDED],
            "ok_count": status_counts[STATUS_OK],
            "no_overdue_count": status_counts[STATUS_NO_OVERDUE],
            "needs_review_count": status_counts[STATUS_NEEDS_REVIEW],
            "candidate_snapshot_count": len(snapshots),
            "below_min_balance_count": below_min_balance_count,
            "document_mismatch_count": document_mismatch_count,
            "queue_counts": dict(sorted(queue_counts_all.items())),
            "actionable_count": queue_counts_all[QUEUE_ACTIONABLE],
            "business_review_count": queue_counts_all[QUEUE_BUSINESS_REVIEW],
            "data_quality_count": queue_counts_all[QUEUE_DATA_QUALITY],
            "excluded_count": queue_counts_all[QUEUE_EXCLUDED],
            "min_recommendation_balance": MIN_RECOMMENDATION_BALANCE,
            "review_reason_counts": dict(sorted(review_reason_counts.items())),
            "total_open_debt": total_open_debt,
            "move_recommended_amount": move_amount,
        },
        "payload": items,
    }
