from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import (
    ReceivableCase,
    ReceivableFolderRecommendationCache,
    ReceivableWorkEvent,
    ReceivableWorkItem,
    StaffMember,
)
from app.models.receivable_balance_snapshot import ReceivableBalanceSnapshot
from app.schemas.receivable_workplace import (
    ReceivableWorkplaceActionRequest,
    ReceivableWorkplaceActionResponse,
    ReceivableWorkplaceDepartmentOption,
    ReceivableWorkplaceDocument,
    ReceivableWorkplaceItem,
    ReceivableWorkplaceMetaResponse,
    ReceivableWorkplaceResponse,
    ReceivableWorkplaceStaffOption,
    ReceivableWorkplaceStatusOption,
    ReceivableWorkplaceSummary,
)
from app.services.counterparty_folder_recommendations import (
    build_open_debt_documents_by_counterparty,
)
from app.services.receivable_department_aliases import (
    expand_receivable_department_refs,
    receivable_department_names_equivalent,
)
from app.services.receivable_staff_contact_rules import (
    FallbackStaffMember,
    load_receivable_staff_contact_rules,
)
from app.services.receivable_workflow import (
    STATUS_CLOSED,
    STATUS_NO_PHONE,
    debt_age_days,
    debt_key_for_case,
    needs_call_on_date,
    stable_key_for_counterparty,
    utcnow,
)
from app.services.receivable_workplace_cache import (
    latest_receivable_snapshot_date,
    load_cached_open_debt_documents,
    receivable_department_options,
    workplace_cache_status,
)
from app.services.receivables import CASE_BUYERS

logger = logging.getLogger(__name__)

DEFAULT_CREDIT_DEPTH_DAYS = 7
WORKPLACE_EVENT_MANAGER_UPDATE = "manager_update"
WORKPLACE_PAYLOAD_KEYS = {
    "contacted_staff_ref",
    "contacted_staff_name",
    "payment_postponed",
    "payment_postponed_count",
    "workplace_last_action_at",
}

STATUS_OPTIONS = [
    ReceivableWorkplaceStatusOption(value="no_answer", label="Не берет трубку"),
    ReceivableWorkplaceStatusOption(value="waiting_payment", label="Ждем оплату"),
    ReceivableWorkplaceStatusOption(value="call_back", label="Перезвонить"),
    ReceivableWorkplaceStatusOption(value="intervention_required", label="Требуется вмешательство"),
    ReceivableWorkplaceStatusOption(value="remind", label="Напомнить"),
    ReceivableWorkplaceStatusOption(value="paid", label="Оплачено"),
    ReceivableWorkplaceStatusOption(value="transfer", label="Перемещение", scope="pyatigorsk"),
    ReceivableWorkplaceStatusOption(
        value="on_card_route", label="На карте/в маршрутке", scope="pyatigorsk"
    ),
]
STATUS_VALUES = {item.value for item in STATUS_OPTIONS}
STAFF_INACTIVE_STATUS_MARKERS = (
    "fired",
    "dismissed",
    "terminated",
    "inactive",
    "уволен",
    "уволена",
    "увольнение",
)


def _money(value: Any) -> Decimal:
    return Decimal(str(value or "0")).quantize(Decimal("0.01"))


def _normalize_name(value: str | None) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _ref_key(value: str | None) -> str:
    return str(value or "").strip().casefold()


def _department_allowed(
    department_ref: str | None,
    *,
    allowed_department_refs: set[str] | frozenset[str] | None,
) -> bool:
    if allowed_department_refs is None:
        return True
    if not department_ref:
        return False
    allowed = {_ref_key(value) for value in allowed_department_refs}
    return _ref_key(department_ref) in allowed


def _date_to_datetime(value: date | None) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(value, time.min)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _parse_decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return _money(value)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _payload_dict(item: ReceivableWorkItem | None) -> dict[str, Any]:
    if item is None or not isinstance(item.payload, dict):
        return {}
    return dict(item.payload)


def _action_idempotency_key(counterparty_ref: str, action_id: str | None) -> str | None:
    normalized = str(action_id or "").strip()
    if not normalized:
        return None
    return f"receivable-workplace:{stable_key_for_counterparty(counterparty_ref)}:{normalized}"


def _selected_debt_document_date(open_debt_documents: list[dict[str, Any]]) -> datetime | None:
    documents = _sort_open_debt_documents(open_debt_documents)
    if not documents:
        return None
    return _parse_datetime(documents[0].get("document_date"))


def _sort_open_debt_documents(
    open_debt_documents: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    def key(document: dict[str, Any]) -> tuple[datetime, str]:
        document_date = _parse_datetime(document.get("document_date")) or datetime.max
        return document_date, str(
            document.get("document_ref") or document.get("document_number") or ""
        )

    return sorted((dict(item) for item in open_debt_documents), key=key)


def _effective_due_date(
    case: ReceivableCase,
    *,
    debt_document_date: datetime | None = None,
) -> tuple[datetime | None, bool]:
    if case.planned_payment_date is not None:
        return case.planned_payment_date, False
    if debt_document_date is not None:
        if case.credit_depth_days and case.credit_depth_days > 0:
            return debt_document_date + timedelta(days=case.credit_depth_days), False
        return debt_document_date + timedelta(days=DEFAULT_CREDIT_DEPTH_DAYS), True
    origin_date = case.origin_document_date
    if case.due_date is not None:
        return case.due_date, False
    if case.credit_depth_days and case.credit_depth_days > 0 and origin_date:
        return origin_date + timedelta(days=case.credit_depth_days), False
    if origin_date is not None:
        return origin_date + timedelta(days=DEFAULT_CREDIT_DEPTH_DAYS), True
    return None, False


def _effective_overdue_days(
    case: ReceivableCase,
    *,
    as_of: date,
    debt_document_date: datetime | None = None,
) -> tuple[int | None, bool]:
    due_date, needs_default = _effective_due_date(
        case,
        debt_document_date=debt_document_date,
    )
    if due_date is None:
        return None, needs_default
    return max((as_of - due_date.date()).days, 0), needs_default


def _document_due_date(
    *,
    case: ReceivableCase,
    document_date: datetime | None,
    needs_default_credit_depth: bool,
) -> datetime | None:
    if case.planned_payment_date is not None:
        return case.planned_payment_date
    if document_date is None:
        return case.due_date
    if case.credit_depth_days and case.credit_depth_days > 0:
        return document_date + timedelta(days=case.credit_depth_days)
    if needs_default_credit_depth:
        return document_date + timedelta(days=DEFAULT_CREDIT_DEPTH_DAYS)
    return case.due_date


def _build_documents(
    case: ReceivableCase,
    *,
    as_of: date,
    needs_default_credit_depth: bool,
    open_debt_documents: list[dict[str, Any]] | None = None,
) -> list[ReceivableWorkplaceDocument]:
    source_documents = case.chain_documents if open_debt_documents is None else open_debt_documents
    raw_documents = list(source_documents or [])
    raw_documents = _sort_open_debt_documents(raw_documents)
    if (
        open_debt_documents is None
        and not raw_documents
        and (case.origin_document_ref or case.origin_document_number)
    ):
        raw_documents.append(
            {
                "document_ref": case.origin_document_ref,
                "document_number": case.origin_document_number,
                "document_date": case.origin_document_date,
                "open_amount": case.current_balance,
            }
        )

    documents: list[ReceivableWorkplaceDocument] = []
    for raw in raw_documents:
        document_date = _parse_datetime(raw.get("document_date"))
        due_date = _document_due_date(
            case=case,
            document_date=document_date,
            needs_default_credit_depth=needs_default_credit_depth,
        )
        overdue_days = None if due_date is None else max((as_of - due_date.date()).days, 0)
        documents.append(
            ReceivableWorkplaceDocument(
                document_ref=raw.get("document_ref"),
                document_number=raw.get("document_number"),
                document_date=document_date,
                amount=_money(
                    raw.get("open_amount")
                    or raw.get("amount_delta")
                    or raw.get("sale_amount")
                    or case.current_balance
                ),
                gross_amount=_parse_decimal_or_none(
                    raw.get("gross_amount") or raw.get("sale_amount")
                ),
                open_amount=_parse_decimal_or_none(raw.get("open_amount")),
                closing_amount=_parse_decimal_or_none(raw.get("closing_amount")),
                return_amount=_parse_decimal_or_none(raw.get("return_amount")),
                manager_name=(
                    raw.get("document_responsible_name")
                    or raw.get("manager_name")
                    or case.origin_manager_name
                ),
                due_date=due_date,
                overdue_days=overdue_days,
                is_overdue=bool(overdue_days and overdue_days > 0),
                selection_rule=raw.get("statement_selection_rule")
                or raw.get("document_structure_status"),
                statement_balance_after=_parse_decimal_or_none(raw.get("statement_balance_after")),
                match_details=list(raw.get("statement_match_details") or []),
                document_structure_status=raw.get("document_structure_status"),
            )
        )
    return documents


def _payment_postponed_count(payload: dict[str, Any]) -> int:
    raw_count = payload.get("payment_postponed_count")
    try:
        count = int(raw_count or 0)
    except (TypeError, ValueError):
        count = 0
    if count <= 0 and payload.get("payment_postponed"):
        return 1
    return max(count, 0)


def _criticality(effective_overdue_days: int | None) -> str:
    days = effective_overdue_days or 0
    if days > 90:
        return "salary_risk"
    if days > 30:
        return "critical"
    if days > 0:
        return "warning"
    return "normal"


def _needs_call_today(
    *,
    item: ReceivableWorkItem | None,
    effective_overdue_days: int | None,
    as_of: date,
) -> bool:
    if item is not None and item.status in {STATUS_CLOSED, "paid"}:
        return False
    if item is not None and item.next_action_date is not None:
        return item.next_action_date.date() <= as_of
    if item is not None and item.needs_call_today:
        return True
    return bool(effective_overdue_days and effective_overdue_days > 0)


def _load_staff_options(session: Session) -> list[StaffMember]:
    rows = (
        session.execute(
            select(StaffMember).order_by(
                StaffMember.department_name,
                StaffMember.store_name,
                StaffMember.full_name,
            )
        )
        .scalars()
        .all()
    )
    return [staff for staff in rows if _staff_available_for_contact_source(staff)]


def _staff_employment_active(staff: StaffMember) -> bool:
    status = _normalize_name(staff.employment_status)
    if not status:
        return False
    return not any(marker in status for marker in STAFF_INACTIVE_STATUS_MARKERS)


def _staff_contact_excluded(staff: StaffMember) -> bool:
    name = _normalize_name(staff.full_name)
    return any(
        marker in name for marker in load_receivable_staff_contact_rules().exclude_name_markers
    )


def _staff_available_for_contact_source(staff: StaffMember) -> bool:
    return _staff_employment_active(staff) and not _staff_contact_excluded(staff)


def _staff_department_override(staff: StaffMember) -> str | None:
    name = _normalize_name(staff.full_name)
    for override in load_receivable_staff_contact_rules().department_overrides:
        if override.name_marker in name:
            return override.department_name
    return None


def _staff_department_refs_for_matching(staff: StaffMember) -> tuple[str | None, ...]:
    if _staff_department_override(staff):
        return ()
    return (staff.department_ref, staff.store_ref)


def _staff_department_names_for_matching(staff: StaffMember) -> tuple[str | None, ...]:
    override = _staff_department_override(staff)
    if override:
        return (override,)
    return (staff.department_name, staff.store_name)


def _staff_option_department_name(staff: StaffMember) -> str | None:
    return _staff_department_override(staff) or staff.department_name or staff.store_name


def _staff_is_courier(staff: StaffMember) -> bool:
    values = (
        _normalize_name(staff.full_name),
        _normalize_name(staff.role_code),
        _normalize_name(staff.role_name),
        _normalize_name(staff.department_name),
        _normalize_name(staff.store_name),
    )
    courier_markers = ("курьер", "courier", "kurer")
    return any(marker in value for value in values for marker in courier_markers)


def _staff_role_allowed_for_contact(staff: StaffMember) -> bool:
    if not _staff_available_for_contact_source(staff):
        return False
    if _staff_is_courier(staff):
        return False
    values = (
        _normalize_name(staff.role_code),
        _normalize_name(staff.role_name),
    )
    if not any(values):
        return True
    allowed_markers = (
        "менедж",
        "manager",
        "товаровед",
        "merchand",
        "руковод",
        "head",
        "директор",
        "director",
        "завед",
        "управля",
        "старш",
    )
    return any(marker in value for value in values for marker in allowed_markers)


def _staff_matches_department(
    staff: StaffMember,
    *,
    department_ref: str | None,
    department_name: str | None,
) -> bool:
    case_refs = expand_receivable_department_refs([department_ref], names=[department_name])
    staff_refs = expand_receivable_department_refs(
        _staff_department_refs_for_matching(staff),
        names=_staff_department_names_for_matching(staff),
    )
    if case_refs and staff_refs and case_refs.intersection(staff_refs):
        return True
    return any(
        receivable_department_names_equivalent(department_name, value)
        for value in _staff_department_names_for_matching(staff)
    )


def _fallback_staff_matches_department(
    staff: FallbackStaffMember,
    *,
    department_ref: str | None,
    department_name: str | None,
) -> bool:
    case_refs = expand_receivable_department_refs([department_ref], names=[department_name])
    staff_refs = expand_receivable_department_refs(
        [staff.department_ref],
        names=[staff.department_name],
    )
    if case_refs and staff_refs and case_refs.intersection(staff_refs):
        return True
    return receivable_department_names_equivalent(department_name, staff.department_name)


def _find_fallback_staff(
    *,
    case: ReceivableCase,
    staff_ref: str | None,
) -> FallbackStaffMember | None:
    if not staff_ref:
        return None
    return next(
        (
            staff
            for staff in load_receivable_staff_contact_rules().fallback_staff
            if staff.staff_ref == staff_ref
            and _fallback_staff_matches_department(
                staff,
                department_ref=case.department_ref,
                department_name=case.department_name,
            )
        ),
        None,
    )


def _staff_options_for_case(
    *,
    case: ReceivableCase,
    staff_members: list[StaffMember],
) -> list[ReceivableWorkplaceStaffOption]:
    options: list[ReceivableWorkplaceStaffOption] = []
    seen: set[str] = set()
    for staff in staff_members:
        if not _staff_role_allowed_for_contact(staff):
            continue
        if not _staff_matches_department(
            staff,
            department_ref=case.department_ref,
            department_name=case.department_name,
        ):
            continue
        seen.add(staff.external_ref)
        options.append(
            ReceivableWorkplaceStaffOption(
                staff_ref=staff.external_ref,
                staff_name=staff.full_name,
                department_ref=staff.department_ref or staff.store_ref,
                department_name=_staff_option_department_name(staff),
            )
        )
    current_manager = next(
        (staff for staff in staff_members if staff.external_ref == case.current_manager_ref),
        None,
    )
    if (
        case.current_manager_ref
        and case.current_manager_ref not in seen
        and current_manager is not None
        and _staff_role_allowed_for_contact(current_manager)
        and _staff_matches_department(
            current_manager,
            department_ref=case.department_ref,
            department_name=case.department_name,
        )
    ):
        options.insert(
            0,
            ReceivableWorkplaceStaffOption(
                staff_ref=case.current_manager_ref,
                staff_name=case.current_manager_name or case.current_manager_ref,
                department_ref=case.department_ref,
                department_name=case.department_name,
            ),
        )
    existing_names = {_normalize_name(option.staff_name) for option in options}
    for staff in load_receivable_staff_contact_rules().fallback_staff:
        if staff.staff_ref in seen or _normalize_name(staff.full_name) in existing_names:
            continue
        if not _fallback_staff_matches_department(
            staff,
            department_ref=case.department_ref,
            department_name=case.department_name,
        ):
            continue
        seen.add(staff.staff_ref)
        existing_names.add(_normalize_name(staff.full_name))
        options.append(
            ReceivableWorkplaceStaffOption(
                staff_ref=staff.staff_ref,
                staff_name=staff.full_name,
                department_ref=staff.department_ref,
                department_name=staff.department_name,
            )
        )
    return options


def _document_display_balance(document: ReceivableWorkplaceDocument) -> Decimal:
    return _money(document.open_amount if document.open_amount is not None else document.amount)


def _item_current_balance(
    case: ReceivableCase,
    *,
    documents: list[ReceivableWorkplaceDocument],
    has_open_debt_documents: bool,
) -> Decimal:
    if has_open_debt_documents and documents:
        return _money(
            sum((_document_display_balance(document) for document in documents), Decimal("0"))
        )
    return _money(case.current_balance)


def _item_overdue_amount(
    case: ReceivableCase,
    *,
    documents: list[ReceivableWorkplaceDocument],
    has_open_debt_documents: bool,
    effective_overdue_days: int | None,
) -> Decimal:
    if has_open_debt_documents and documents:
        return _money(
            sum(
                (
                    _document_display_balance(document)
                    for document in documents
                    if document.is_overdue
                ),
                Decimal("0"),
            )
        )
    return _money(case.current_balance if effective_overdue_days else 0)


def _build_item(
    case: ReceivableCase,
    *,
    item: ReceivableWorkItem | None,
    as_of: date,
    staff_members: list[StaffMember],
    open_debt_documents: list[dict[str, Any]] | None = None,
    counterparty_code: str | None = None,
) -> ReceivableWorkplaceItem:
    has_open_debt_source = open_debt_documents is not None
    open_debt_documents = _sort_open_debt_documents(open_debt_documents or [])
    primary_open_document = open_debt_documents[0] if open_debt_documents else {}
    debt_document_date = _selected_debt_document_date(open_debt_documents)
    if has_open_debt_source and not open_debt_documents:
        effective_due_date = None
        effective_overdue_days = None
        needs_default_credit_depth = False
    else:
        effective_due_date, needs_default_credit_depth = _effective_due_date(
            case,
            debt_document_date=debt_document_date,
        )
        effective_overdue_days, _ = _effective_overdue_days(
            case,
            as_of=as_of,
            debt_document_date=debt_document_date,
        )
    documents = _build_documents(
        case,
        as_of=as_of,
        needs_default_credit_depth=needs_default_credit_depth,
        open_debt_documents=open_debt_documents if has_open_debt_source else None,
    )
    current_balance = _item_current_balance(
        case,
        documents=documents,
        has_open_debt_documents=has_open_debt_source,
    )
    overdue_amount = _item_overdue_amount(
        case,
        documents=documents,
        has_open_debt_documents=has_open_debt_source,
        effective_overdue_days=effective_overdue_days,
    )
    item_payload = _payload_dict(item)
    phone = item.phone if item is not None else None
    phone_status = item.phone_status if item is not None else ("present" if phone else "missing")
    needs_call = _needs_call_today(
        item=item,
        effective_overdue_days=effective_overdue_days,
        as_of=as_of,
    )
    status = item.status if item is not None else (STATUS_NO_PHONE if not phone else "new_debt")
    responsible_ref = (
        primary_open_document.get("document_responsible_ref")
        or primary_open_document.get("manager_ref")
        or case.origin_manager_ref
        or case.current_manager_ref
    )
    responsible_name = (
        primary_open_document.get("document_responsible_name")
        or primary_open_document.get("manager_name")
        or case.origin_manager_name
        or case.current_manager_name
    )
    return ReceivableWorkplaceItem(
        snapshot_date=case.snapshot_date,
        stable_key=stable_key_for_counterparty(case.counterparty_ref),
        counterparty_ref=case.counterparty_ref,
        counterparty_code=case.counterparty_code
        or counterparty_code
        or item_payload.get("counterparty_code"),
        counterparty_name=case.counterparty_name,
        bitrix_detail_url=item.bitrix_detail_url if item is not None else None,
        department_ref=case.department_ref,
        department_name=case.department_name,
        responsible_ref=responsible_ref,
        responsible_name=responsible_name,
        phone=phone,
        phone_status=phone_status,
        current_balance=current_balance,
        overdue_amount=overdue_amount,
        effective_due_date=effective_due_date,
        effective_overdue_days=effective_overdue_days,
        oldest_overdue_date=debt_document_date or case.origin_document_date,
        invoice_count=len(documents),
        overdue_invoice_count=sum(1 for document in documents if document.is_overdue),
        promised_payment_date=item.promised_payment_date if item is not None else None,
        last_contact_at=item.last_contact_at if item is not None else None,
        contacted_staff_ref=item_payload.get("contacted_staff_ref"),
        contacted_staff_name=item_payload.get("contacted_staff_name"),
        status=status,
        next_action_date=item.next_action_date if item is not None else None,
        payment_postponed=False,
        payment_postponed_count=_payment_postponed_count(item_payload),
        comment=item.last_contact_comment if item is not None else None,
        needs_call_today=needs_call,
        no_phone_marker=phone_status == "missing",
        needs_credit_depth_default=needs_default_credit_depth,
        criticality=_criticality(effective_overdue_days),
        documents=documents,
        staff_options=_staff_options_for_case(case=case, staff_members=staff_members),
    )


def _load_cases(session: Session, *, snapshot_date: date) -> list[ReceivableCase]:
    return (
        session.execute(
            select(ReceivableCase)
            .where(
                ReceivableCase.snapshot_date == snapshot_date,
                ReceivableCase.segment == CASE_BUYERS,
                ReceivableCase.current_balance > 0,
            )
            .order_by(ReceivableCase.current_balance.desc(), ReceivableCase.counterparty_ref)
        )
        .scalars()
        .all()
    )


def _load_work_items(
    session: Session,
    *,
    counterparty_refs: list[str],
) -> dict[str, ReceivableWorkItem]:
    if not counterparty_refs:
        return {}
    stable_keys = [stable_key_for_counterparty(ref) for ref in counterparty_refs]
    items = (
        session.execute(
            select(ReceivableWorkItem).where(ReceivableWorkItem.stable_key.in_(stable_keys))
        )
        .scalars()
        .all()
    )
    return {item.counterparty_ref: item for item in items}


def _load_counterparty_codes_from_folder_cache(
    session: Session,
    *,
    snapshot_date: date,
    counterparty_refs: list[str],
) -> dict[str, str]:
    if not counterparty_refs:
        return {}
    row = session.scalar(
        select(ReceivableFolderRecommendationCache).where(
            ReceivableFolderRecommendationCache.snapshot_date == snapshot_date,
            ReceivableFolderRecommendationCache.status_scope == "all",
        )
    )
    if row is None:
        return {}
    wanted = {_ref_key(value) for value in counterparty_refs if value}
    result: dict[str, str] = {}
    for item in row.payload or []:
        if not isinstance(item, dict):
            continue
        key = _ref_key(item.get("counterparty_ref"))
        code = str(item.get("counterparty_code") or "").strip()
        if key in wanted and code:
            result[key] = code
    return result


def _status_for_case(case: ReceivableCase, item: ReceivableWorkItem | None) -> str:
    if item is not None:
        return item.status
    return STATUS_NO_PHONE if not (item.phone if item is not None else None) else "new_debt"


def _case_sort_key(
    case: ReceivableCase,
) -> tuple[Decimal, int, str]:
    overdue_days = case.overdue_days
    if overdue_days is None:
        overdue_days = _effective_overdue_days(case, as_of=case.snapshot_date)[0] or 0
    return (
        _money(case.current_balance),
        int(overdue_days or 0),
        str(case.counterparty_name or ""),
    )


def _load_balance_snapshots(
    session: Session,
    *,
    snapshot_date: date,
    counterparty_refs: list[str],
) -> dict[str, ReceivableBalanceSnapshot]:
    if not counterparty_refs:
        return {}
    rows = (
        session.execute(
            select(ReceivableBalanceSnapshot).where(
                ReceivableBalanceSnapshot.snapshot_date == snapshot_date,
                ReceivableBalanceSnapshot.counterparty_ref.in_(counterparty_refs),
            )
        )
        .scalars()
        .all()
    )
    return {row.counterparty_ref: row for row in rows}


def _load_open_debt_documents(
    session: Session,
    *,
    snapshot_date: date,
    cases: list[ReceivableCase],
) -> dict[str, list[dict[str, Any]]]:
    if not cases:
        return {}
    snapshot_by_counterparty = _load_balance_snapshots(
        session,
        snapshot_date=snapshot_date,
        counterparty_refs=[case.counterparty_ref for case in cases],
    )
    snapshots = [
        snapshot
        for case in cases
        if (snapshot := snapshot_by_counterparty.get(case.counterparty_ref))
    ]
    if not snapshots:
        return {}
    try:
        return build_open_debt_documents_by_counterparty(
            session,
            snapshots=snapshots,
            snapshot_date=snapshot_date,
            include_onec_enrichment=False,
        )
    except (SQLAlchemyError, TimeoutError, OSError, ValueError) as exc:
        logger.warning(
            "receivable_workplace_open_debt_enrichment_failed",
            extra={"snapshot_date": snapshot_date.isoformat(), "error_type": type(exc).__name__},
        )
        return {}


def _summary(items: list[ReceivableWorkplaceItem]) -> ReceivableWorkplaceSummary:
    return ReceivableWorkplaceSummary(
        row_count=len(items),
        total_receivable=sum((item.current_balance for item in items), Decimal("0.00")),
        total_overdue=sum((item.overdue_amount for item in items), Decimal("0.00")),
        overdue_over_30_amount=sum(
            (item.current_balance for item in items if (item.effective_overdue_days or 0) > 30),
            Decimal("0.00"),
        ),
        overdue_over_90_amount=sum(
            (item.current_balance for item in items if (item.effective_overdue_days or 0) > 90),
            Decimal("0.00"),
        ),
        need_call_today_amount=sum(
            (item.current_balance for item in items if item.needs_call_today),
            Decimal("0.00"),
        ),
        no_phone_count=sum(1 for item in items if item.no_phone_marker),
        credit_depth_default_count=sum(1 for item in items if item.needs_credit_depth_default),
    )


def build_receivable_workplace_meta(
    session: Session,
    *,
    snapshot_date: date | None = None,
    allowed_department_refs: set[str] | frozenset[str] | None = None,
) -> ReceivableWorkplaceMetaResponse:
    resolved_date = snapshot_date or latest_receivable_snapshot_date(
        session,
        allowed_department_refs=allowed_department_refs,
    )
    return ReceivableWorkplaceMetaResponse(
        latest_snapshot_date=resolved_date,
        department_options=[
            ReceivableWorkplaceDepartmentOption(
                department_ref=item.department_ref,
                department_name=item.department_name,
            )
            for item in receivable_department_options(
                session,
                snapshot_date=resolved_date,
                allowed_department_refs=allowed_department_refs,
            )
        ],
        cache_status=workplace_cache_status(session, snapshot_date=resolved_date),
    )


def build_receivable_workplace(
    session: Session,
    *,
    snapshot_date: date,
    department_ref: str | None = None,
    status: str | None = None,
    limit: int = 500,
    allowed_department_refs: set[str] | frozenset[str] | None = None,
) -> ReceivableWorkplaceResponse:
    cases = _load_cases(session, snapshot_date=snapshot_date)
    if allowed_department_refs is not None:
        cases = [
            case
            for case in cases
            if _department_allowed(
                case.department_ref,
                allowed_department_refs=allowed_department_refs,
            )
        ]
    department_options = [
        ReceivableWorkplaceDepartmentOption(
            department_ref=item.department_ref,
            department_name=item.department_name,
        )
        for item in receivable_department_options(
            session,
            snapshot_date=snapshot_date,
            allowed_department_refs=allowed_department_refs,
        )
    ]
    if department_ref:
        if not _department_allowed(
            department_ref,
            allowed_department_refs=allowed_department_refs,
        ):
            cases = []
        else:
            cases = [case for case in cases if case.department_ref == department_ref]
    work_items = _load_work_items(
        session,
        counterparty_refs=[case.counterparty_ref for case in cases],
    )
    counterparty_codes = _load_counterparty_codes_from_folder_cache(
        session,
        snapshot_date=snapshot_date,
        counterparty_refs=[case.counterparty_ref for case in cases],
    )
    if status:
        cases = [
            case
            for case in cases
            if _status_for_case(case, work_items.get(case.counterparty_ref)) == status
        ]
    cases.sort(key=_case_sort_key, reverse=True)
    open_debt_cache = load_cached_open_debt_documents(
        session,
        snapshot_date=snapshot_date,
        counterparty_refs=[case.counterparty_ref for case in cases],
    )
    open_debt_source_status = open_debt_cache.source_status
    open_debt_documents_by_counterparty = dict(open_debt_cache.documents_by_counterparty)
    missing_cases = [
        case
        for case in cases
        if _ref_key(case.counterparty_ref) not in open_debt_documents_by_counterparty
    ]
    if missing_cases:
        open_debt_documents_by_counterparty.update(
            _load_open_debt_documents(
                session,
                snapshot_date=snapshot_date,
                cases=missing_cases,
            )
        )
        open_debt_source_status = (
            "fallback_live"
            if open_debt_cache.source_status == "cache_missing"
            else "cache_partial_fallback"
        )
    staff_members = _load_staff_options(session)
    items = [
        _build_item(
            case,
            item=work_items.get(case.counterparty_ref),
            as_of=snapshot_date,
            staff_members=staff_members,
            open_debt_documents=open_debt_documents_by_counterparty.get(
                _ref_key(case.counterparty_ref)
            ),
            counterparty_code=counterparty_codes.get(_ref_key(case.counterparty_ref)),
        )
        for case in cases
    ]
    items = [item for item in items if (item.effective_overdue_days or 0) > 0]
    items.sort(
        key=lambda item: (
            item.current_balance,
            item.effective_overdue_days or 0,
            item.counterparty_name or "",
        ),
        reverse=True,
    )
    visible_items = items[:limit]
    cache_status = workplace_cache_status(session, snapshot_date=snapshot_date)
    cache_status["open_debt"]["source_status"] = open_debt_source_status
    cache_status["open_debt"]["cached_count"] = open_debt_cache.cached_counterparty_count
    cache_status["open_debt"]["computed_at"] = open_debt_cache.computed_at
    return ReceivableWorkplaceResponse(
        as_of=snapshot_date,
        freshness_status="fresh" if items else "missing",
        source_status=open_debt_source_status if cases else "empty",
        summary=_summary(items),
        total_count=len(items),
        visible_count=len(visible_items),
        summary_scope="filtered_total",
        department_options=department_options,
        cache_status=cache_status,
        status_options=STATUS_OPTIONS,
        payload=visible_items,
    )


def _get_case_or_none(
    session: Session,
    *,
    snapshot_date: date,
    counterparty_ref: str,
) -> ReceivableCase | None:
    return session.scalar(
        select(ReceivableCase).where(
            ReceivableCase.snapshot_date == snapshot_date,
            ReceivableCase.segment == CASE_BUYERS,
            ReceivableCase.counterparty_ref == counterparty_ref,
        )
    )


def _find_staff(
    staff_members: list[StaffMember],
    *,
    staff_ref: str | None,
) -> StaffMember | None:
    if not staff_ref:
        return None
    return next((item for item in staff_members if item.external_ref == staff_ref), None)


def _refresh_work_item_from_case(
    *,
    item: ReceivableWorkItem,
    case: ReceivableCase,
    as_of: date,
) -> None:
    payload = _payload_dict(item)
    preserved = {key: payload.get(key) for key in WORKPLACE_PAYLOAD_KEYS if key in payload}
    item.counterparty_ref = case.counterparty_ref
    item.counterparty_name = case.counterparty_name
    item.current_debt_key = debt_key_for_case(case)
    item.current_balance = case.current_balance
    item.origin_document_ref = case.origin_document_ref
    item.origin_document_number = case.origin_document_number
    item.origin_document_date = case.origin_document_date
    item.due_date = _effective_due_date(case)[0]
    item.overdue_days = _effective_overdue_days(case, as_of=as_of)[0]
    item.age_days = debt_age_days(case, as_of=as_of)
    item.origin_manager_ref = case.origin_manager_ref
    item.origin_manager_name = case.origin_manager_name
    item.current_manager_ref = case.current_manager_ref
    item.current_manager_name = case.current_manager_name
    item.department_ref = case.department_ref
    item.department_name = case.department_name
    item.needs_call_today = needs_call_on_date(case, as_of=as_of)
    item.chain_documents = case.chain_documents or []
    item.payload = {
        "snapshot_date": case.snapshot_date.isoformat(),
        "segment": case.segment,
        "aged_bucket": case.aged_bucket,
        "activity_segment": case.activity_segment,
        "payment_term_source": case.payment_term_source,
        "shipment_ban": case.shipment_ban,
        **preserved,
    }


def _get_or_create_work_item(
    session: Session,
    *,
    case: ReceivableCase,
    as_of: date,
) -> ReceivableWorkItem:
    stable_key = stable_key_for_counterparty(case.counterparty_ref)
    item = session.scalar(
        select(ReceivableWorkItem).where(ReceivableWorkItem.stable_key == stable_key)
    )
    if item is None:
        item = ReceivableWorkItem(
            stable_key=stable_key,
            counterparty_ref=case.counterparty_ref,
            counterparty_name=case.counterparty_name,
            status="new_debt",
            current_balance=case.current_balance,
        )
        session.add(item)
        session.flush()
    _refresh_work_item_from_case(item=item, case=case, as_of=as_of)
    return item


def apply_receivable_workplace_action(
    session: Session,
    *,
    snapshot_date: date,
    counterparty_ref: str,
    payload: ReceivableWorkplaceActionRequest,
    allowed_department_refs: set[str] | frozenset[str] | None = None,
) -> ReceivableWorkplaceActionResponse | None:
    case = _get_case_or_none(
        session,
        snapshot_date=snapshot_date,
        counterparty_ref=counterparty_ref,
    )
    if case is None:
        return None
    if not _department_allowed(
        case.department_ref,
        allowed_department_refs=allowed_department_refs,
    ):
        raise PermissionError("receivable workplace item is outside allowed departments")
    if payload.status is not None and payload.status not in STATUS_VALUES:
        raise ValueError(f"Unsupported receivable workplace status: {payload.status}")

    item = _get_or_create_work_item(session, case=case, as_of=snapshot_date)
    staff_members = _load_staff_options(session)
    staff = _find_staff(staff_members, staff_ref=payload.contacted_staff_ref)
    fallback_staff = _find_fallback_staff(case=case, staff_ref=payload.contacted_staff_ref)
    if staff is not None and not _staff_role_allowed_for_contact(staff):
        raise ValueError("Only manager, merchandiser or department head can be selected")
    if payload.contacted_staff_ref and staff is None and fallback_staff is None:
        raise ValueError("Selected staff member is not available for this department")
    payload_dict = _payload_dict(item)
    fields_set = payload.model_fields_set
    action_key = _action_idempotency_key(counterparty_ref, payload.action_id)
    if action_key:
        existing_event = session.scalar(
            select(ReceivableWorkEvent).where(ReceivableWorkEvent.idempotency_key == action_key)
        )
        if existing_event is not None:
            counterparty_code = _load_counterparty_codes_from_folder_cache(
                session,
                snapshot_date=snapshot_date,
                counterparty_refs=[case.counterparty_ref],
            ).get(_ref_key(case.counterparty_ref))
            response_item = _build_item(
                case,
                item=item,
                as_of=snapshot_date,
                staff_members=staff_members,
                open_debt_documents=load_cached_open_debt_documents(
                    session,
                    snapshot_date=snapshot_date,
                    counterparty_refs=[case.counterparty_ref],
                ).documents_by_counterparty.get(_ref_key(case.counterparty_ref)),
                counterparty_code=counterparty_code,
            )
            return ReceivableWorkplaceActionResponse(
                item=response_item,
                event={
                    "event_type": existing_event.event_type,
                    "event_at": existing_event.event_at.isoformat(),
                    "source": existing_event.source,
                    "idempotent": True,
                },
                cache_status=workplace_cache_status(session, snapshot_date=snapshot_date),
            )

    previous_status = item.status
    previous_contacted_staff_ref = payload_dict.get("contacted_staff_ref")
    previous_contacted_staff_name = payload_dict.get("contacted_staff_name")
    previous_promised_payment_date = item.promised_payment_date
    previous_last_contact_at = item.last_contact_at
    previous_comment = item.last_contact_comment

    if payload.status is not None:
        item.status = payload.status
    if "promised_payment_date" in fields_set:
        item.promised_payment_date = _date_to_datetime(payload.promised_payment_date)
    if "last_contact_at" in fields_set:
        item.last_contact_at = _date_to_datetime(payload.last_contact_at)
    if "next_action_date" in fields_set:
        item.next_action_date = _date_to_datetime(payload.next_action_date)
        item.needs_call_today = (
            item.next_action_date.date() <= snapshot_date
            if item.next_action_date is not None
            else bool(item.overdue_days and item.overdue_days > 0)
        )
    if "payment_postponed" in fields_set:
        postponed_added = bool(payload.payment_postponed)
        if postponed_added:
            payload_dict["payment_postponed_count"] = _payment_postponed_count(payload_dict) + 1
        payload_dict["payment_postponed"] = False
    if "comment" in fields_set:
        comment = payload.comment.strip() if payload.comment else ""
        item.last_contact_comment = comment or None
    if "contacted_staff_ref" in fields_set or "contacted_staff_name" in fields_set:
        payload_dict["contacted_staff_ref"] = payload.contacted_staff_ref
        payload_dict["contacted_staff_name"] = (
            staff.full_name
            if staff is not None
            else (
                fallback_staff.full_name
                if fallback_staff is not None
                else payload.contacted_staff_name
            )
        )

    now = utcnow()
    contact_changed = any(
        (
            payload.status is not None and payload.status != previous_status,
            "contacted_staff_ref" in fields_set
            and payload_dict.get("contacted_staff_ref") != previous_contacted_staff_ref,
            "contacted_staff_name" in fields_set
            and payload_dict.get("contacted_staff_name") != previous_contacted_staff_name,
            "promised_payment_date" in fields_set
            and item.promised_payment_date != previous_promised_payment_date,
            "last_contact_at" in fields_set and item.last_contact_at != previous_last_contact_at,
            bool(payload.payment_postponed),
            "comment" in fields_set
            and (item.last_contact_comment or "") != (previous_comment or "")
            and bool(item.last_contact_comment),
        )
    )
    if contact_changed and "last_contact_at" not in fields_set:
        item.last_contact_at = now
    item.last_manager_update_at = now
    payload_dict["workplace_last_action_at"] = now.isoformat()
    item.payload = payload_dict
    event = ReceivableWorkEvent(
        work_item=item,
        event_type=WORKPLACE_EVENT_MANAGER_UPDATE,
        event_at=now,
        source="web_workplace",
        comment=item.last_contact_comment,
        payload={
            "status": item.status,
            "contacted_staff_ref": payload_dict.get("contacted_staff_ref"),
            "contacted_staff_name": payload_dict.get("contacted_staff_name"),
            "promised_payment_date": (
                item.promised_payment_date.date().isoformat()
                if item.promised_payment_date
                else None
            ),
            "next_action_date": (
                item.next_action_date.date().isoformat() if item.next_action_date else None
            ),
            "last_contact_at": (
                item.last_contact_at.date().isoformat() if item.last_contact_at else None
            ),
            "payment_postponed": False,
            "payment_postponed_added": bool(payload.payment_postponed),
            "payment_postponed_count": payload_dict.get("payment_postponed_count", 0),
            "action_id": payload.action_id,
        },
        idempotency_key=action_key,
    )
    session.add(event)
    session.flush()

    cached_open_debt = load_cached_open_debt_documents(
        session,
        snapshot_date=snapshot_date,
        counterparty_refs=[case.counterparty_ref],
    )
    response_item = _build_item(
        case,
        item=item,
        as_of=snapshot_date,
        staff_members=staff_members,
        open_debt_documents=cached_open_debt.documents_by_counterparty.get(
            _ref_key(case.counterparty_ref)
        ),
        counterparty_code=_load_counterparty_codes_from_folder_cache(
            session,
            snapshot_date=snapshot_date,
            counterparty_refs=[case.counterparty_ref],
        ).get(_ref_key(case.counterparty_ref)),
    )
    return ReceivableWorkplaceActionResponse(
        item=response_item,
        event={
            "event_type": event.event_type,
            "event_at": event.event_at.isoformat(),
            "source": event.source,
        },
        cache_status=workplace_cache_status(session, snapshot_date=snapshot_date),
    )
