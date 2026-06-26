from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ReceivableCase, ReceivableWorkEvent, ReceivableWorkItem, StaffMember
from app.schemas.receivable_workplace import (
    ReceivableWorkplaceActionRequest,
    ReceivableWorkplaceActionResponse,
    ReceivableWorkplaceDocument,
    ReceivableWorkplaceItem,
    ReceivableWorkplaceResponse,
    ReceivableWorkplaceStaffOption,
    ReceivableWorkplaceStatusOption,
    ReceivableWorkplaceSummary,
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
from app.services.receivables import CASE_BUYERS

DEFAULT_CREDIT_DEPTH_DAYS = 7
WORKPLACE_EVENT_MANAGER_UPDATE = "manager_update"
WORKPLACE_PAYLOAD_KEYS = {
    "contacted_staff_ref",
    "contacted_staff_name",
    "payment_postponed",
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


def _payload_dict(item: ReceivableWorkItem | None) -> dict[str, Any]:
    if item is None or not isinstance(item.payload, dict):
        return {}
    return dict(item.payload)


def _effective_due_date(case: ReceivableCase) -> tuple[datetime | None, bool]:
    if case.due_date is not None:
        return case.due_date, False
    if case.planned_payment_date is not None:
        return case.planned_payment_date, False
    if case.credit_depth_days and case.credit_depth_days > 0 and case.origin_document_date:
        return case.origin_document_date + timedelta(days=case.credit_depth_days), False
    if case.origin_document_date is not None:
        return case.origin_document_date + timedelta(days=DEFAULT_CREDIT_DEPTH_DAYS), True
    return None, False


def _effective_overdue_days(case: ReceivableCase, *, as_of: date) -> tuple[int | None, bool]:
    due_date, needs_default = _effective_due_date(case)
    if due_date is None:
        return None, needs_default
    return max((as_of - due_date.date()).days, 0), needs_default


def _document_due_date(
    *,
    case: ReceivableCase,
    document_date: datetime | None,
    needs_default_credit_depth: bool,
) -> datetime | None:
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
) -> list[ReceivableWorkplaceDocument]:
    raw_documents = list(case.chain_documents or [])
    if not raw_documents and (case.origin_document_ref or case.origin_document_number):
        raw_documents.append(
            {
                "document_ref": case.origin_document_ref,
                "document_number": case.origin_document_number,
                "document_date": case.origin_document_date,
                "amount_delta": case.current_balance,
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
                amount=_money(raw.get("amount_delta")),
                manager_name=raw.get("manager_name") or case.origin_manager_name,
                due_date=due_date,
                overdue_days=overdue_days,
                is_overdue=bool(overdue_days and overdue_days > 0),
            )
        )
    return documents


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
    return (
        session.execute(
            select(StaffMember)
            .where(StaffMember.employment_status != "fired")
            .order_by(StaffMember.department_name, StaffMember.store_name, StaffMember.full_name)
        )
        .scalars()
        .all()
    )


def _staff_matches_department(
    staff: StaffMember,
    *,
    department_ref: str | None,
    department_name: str | None,
) -> bool:
    if department_ref and department_ref in {staff.department_ref, staff.store_ref}:
        return True
    normalized = _normalize_name(department_name)
    return bool(
        normalized
        and normalized
        in {_normalize_name(staff.department_name), _normalize_name(staff.store_name)}
    )


def _staff_options_for_case(
    *,
    case: ReceivableCase,
    staff_members: list[StaffMember],
) -> list[ReceivableWorkplaceStaffOption]:
    options: list[ReceivableWorkplaceStaffOption] = []
    seen: set[str] = set()
    for staff in staff_members:
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
                department_name=staff.department_name or staff.store_name,
            )
        )
    if case.current_manager_ref and case.current_manager_ref not in seen:
        options.insert(
            0,
            ReceivableWorkplaceStaffOption(
                staff_ref=case.current_manager_ref,
                staff_name=case.current_manager_name or case.current_manager_ref,
                department_ref=case.department_ref,
                department_name=case.department_name,
            ),
        )
    return options


def _build_item(
    case: ReceivableCase,
    *,
    item: ReceivableWorkItem | None,
    as_of: date,
    staff_members: list[StaffMember],
) -> ReceivableWorkplaceItem:
    effective_due_date, needs_default_credit_depth = _effective_due_date(case)
    effective_overdue_days, _ = _effective_overdue_days(case, as_of=as_of)
    documents = _build_documents(
        case,
        as_of=as_of,
        needs_default_credit_depth=needs_default_credit_depth,
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
    return ReceivableWorkplaceItem(
        snapshot_date=case.snapshot_date,
        stable_key=stable_key_for_counterparty(case.counterparty_ref),
        counterparty_ref=case.counterparty_ref,
        counterparty_code=item_payload.get("counterparty_code"),
        counterparty_name=case.counterparty_name,
        department_ref=case.department_ref,
        department_name=case.department_name,
        responsible_ref=case.origin_manager_ref or case.current_manager_ref,
        responsible_name=case.origin_manager_name or case.current_manager_name,
        phone=phone,
        phone_status=phone_status,
        current_balance=_money(case.current_balance),
        overdue_amount=_money(case.current_balance if effective_overdue_days else 0),
        effective_due_date=effective_due_date,
        effective_overdue_days=effective_overdue_days,
        oldest_overdue_date=case.origin_document_date,
        invoice_count=len(documents),
        overdue_invoice_count=sum(1 for document in documents if document.is_overdue),
        promised_payment_date=item.promised_payment_date if item is not None else None,
        last_contact_at=item.last_manager_update_at if item is not None else None,
        contacted_staff_ref=item_payload.get("contacted_staff_ref"),
        contacted_staff_name=item_payload.get("contacted_staff_name"),
        status=status,
        next_action_date=item.next_action_date if item is not None else None,
        payment_postponed=bool(item_payload.get("payment_postponed")),
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
    work_items = _load_work_items(
        session,
        counterparty_refs=[case.counterparty_ref for case in cases],
    )
    staff_members = _load_staff_options(session)
    items = [
        _build_item(
            case,
            item=work_items.get(case.counterparty_ref),
            as_of=snapshot_date,
            staff_members=staff_members,
        )
        for case in cases
    ]
    items = [item for item in items if (item.effective_overdue_days or 0) > 0]
    if department_ref:
        if not _department_allowed(
            department_ref,
            allowed_department_refs=allowed_department_refs,
        ):
            items = []
        else:
            items = [item for item in items if item.department_ref == department_ref]
    if status:
        items = [item for item in items if item.status == status]
    items.sort(
        key=lambda item: (
            item.current_balance,
            item.effective_overdue_days or 0,
            item.counterparty_name or "",
        ),
        reverse=True,
    )
    limited_items = items[:limit]
    return ReceivableWorkplaceResponse(
        as_of=snapshot_date,
        freshness_status="fresh" if limited_items else "missing",
        source_status="ready" if cases else "empty",
        summary=_summary(limited_items),
        status_options=STATUS_OPTIONS,
        payload=limited_items,
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
    payload_dict = _payload_dict(item)
    fields_set = payload.model_fields_set

    if payload.status is not None:
        item.status = payload.status
    if "promised_payment_date" in fields_set:
        item.promised_payment_date = _date_to_datetime(payload.promised_payment_date)
    if "next_action_date" in fields_set:
        item.next_action_date = _date_to_datetime(payload.next_action_date)
        item.needs_call_today = (
            item.next_action_date.date() <= snapshot_date
            if item.next_action_date is not None
            else bool(item.overdue_days and item.overdue_days > 0)
        )
    if "payment_postponed" in fields_set:
        payload_dict["payment_postponed"] = payload.payment_postponed
    if "comment" in fields_set:
        comment = payload.comment.strip() if payload.comment else ""
        item.last_contact_comment = comment or None
    if "contacted_staff_ref" in fields_set or "contacted_staff_name" in fields_set:
        payload_dict["contacted_staff_ref"] = payload.contacted_staff_ref
        payload_dict["contacted_staff_name"] = (
            staff.full_name if staff is not None else payload.contacted_staff_name
        )

    now = utcnow()
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
            "payment_postponed": payload_dict.get("payment_postponed"),
        },
    )
    session.add(event)
    session.flush()

    response_item = _build_item(case, item=item, as_of=snapshot_date, staff_members=staff_members)
    return ReceivableWorkplaceActionResponse(
        item=response_item,
        event={
            "event_type": event.event_type,
            "event_at": event.event_at.isoformat(),
            "source": event.source,
        },
    )
