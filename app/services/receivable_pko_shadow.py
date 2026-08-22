from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Sequence
from uuid import uuid4

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.models import ReceivableCase, ReceivableOpenDebtCache, ReceivablePkoShadowResult
from app.services.counterparty_folder_recommendations import (
    SaleDocumentDepartmentRow,
    fetch_counterparty_ledger_statement_events,
    fetch_sale_document_departments,
)
from app.services.receivable_document_structure import (
    DOCUMENT_STRUCTURE_CONFIRMED_OPEN,
    ReceivableDocumentStructureCheck,
    fetch_receivable_document_structure_checks,
)
from app.services.receivable_statement_debt import ReceivableStatementEvent
from app.services.receivables import (
    CASE_BUYERS,
    _build_ref_filter_clause,
    _hex_ref_expr,
    _with_nolock,
)

PKO_SHADOW_ALGORITHM_VERSION = "pko-shadow-v1"
PKO_SHADOW_MATCHED = "matched"
PKO_SHADOW_DATA_QUALITY = "data_quality"
PKO_SHADOW_NO_CANDIDATE = "no_candidate"
PKO_SHADOW_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class ReceivableReturnSaleLink:
    return_document_ref: str
    status: str
    sale_document_ref: str | None = None
    basis_ref: str | None = None
    basis_kind: str | None = None


@dataclass(frozen=True)
class ReceivablePkoShadowDocument:
    document_ref: str
    document_number: str | None
    document_date: datetime
    gross_amount: Decimal
    open_amount: Decimal
    manager_ref: str | None
    manager_name: str | None
    selection_rule: str


@dataclass(frozen=True)
class ReceivablePkoShadowResolution:
    status: str
    reason: str
    current_balance: Decimal
    selected_open_amount: Decimal
    delta: Decimal
    base_payment_ref: str | None = None
    base_payment_number: str | None = None
    base_payment_date: datetime | None = None
    base_balance_after: Decimal | None = None
    documents: tuple[ReceivablePkoShadowDocument, ...] = ()
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class _OpenDocument:
    event: ReceivableStatementEvent
    gross_amount: Decimal
    open_amount: Decimal
    selection_rule: str


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _ref(value: Any) -> str:
    return str(value or "").strip()


def _ref_key(value: Any) -> str:
    return _ref(value).casefold()


def _event_sort_key(
    event: ReceivableStatementEvent,
    index: int,
) -> tuple[datetime, int, str, int]:
    return (
        event.document_date,
        event.line_no if event.line_no is not None else 999_999,
        event.document_ref,
        index,
    )


def _sorted_events(
    events: Sequence[ReceivableStatementEvent],
) -> list[ReceivableStatementEvent]:
    return [
        event
        for index, event in sorted(
            enumerate(events),
            key=lambda item: _event_sort_key(item[1], item[0]),
        )
    ]


def _return_links_by_ref(
    links: dict[str, ReceivableReturnSaleLink] | None,
) -> dict[str, ReceivableReturnSaleLink]:
    return {_ref_key(key): value for key, value in (links or {}).items()}


def _structure_checks_by_ref(
    checks: dict[str, ReceivableDocumentStructureCheck] | None,
) -> dict[str, ReceivableDocumentStructureCheck]:
    return {_ref_key(key): value for key, value in (checks or {}).items()}


def _unconfirmed_returns(
    events: Sequence[ReceivableStatementEvent],
    *,
    links_by_ref: dict[str, ReceivableReturnSaleLink],
    sale_refs: set[str],
) -> list[dict[str, Any]]:
    unresolved: list[dict[str, Any]] = []
    for event in events:
        if event.event_type != "return" or _money(event.amount_delta) >= Decimal("0.00"):
            continue
        link = links_by_ref.get(_ref_key(event.document_ref))
        if (
            link is None
            or link.status != "confirmed"
            or _ref_key(link.sale_document_ref) not in sale_refs
        ):
            unresolved.append(
                {
                    "document_ref": event.document_ref,
                    "document_number": event.document_number,
                    "status": link.status if link is not None else "not_found",
                    "basis_ref": link.basis_ref if link is not None else None,
                    "basis_kind": link.basis_kind if link is not None else None,
                }
            )
    return unresolved


def _return_amounts_by_sale(
    events: Sequence[ReceivableStatementEvent],
    *,
    links_by_ref: dict[str, ReceivableReturnSaleLink],
    through_index: int | None = None,
) -> dict[str, Decimal]:
    amounts: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for index, event in enumerate(events):
        if through_index is not None and index > through_index:
            break
        if event.event_type != "return" or _money(event.amount_delta) >= Decimal("0.00"):
            continue
        link = links_by_ref.get(_ref_key(event.document_ref))
        if link is None or link.status != "confirmed" or not link.sale_document_ref:
            continue
        key = _ref_key(link.sale_document_ref)
        amounts[key] = _money(amounts[key] + abs(_money(event.amount_delta)))
    return dict(amounts)


def _documents_payload(
    documents: Sequence[_OpenDocument],
) -> tuple[ReceivablePkoShadowDocument, ...]:
    return tuple(
        ReceivablePkoShadowDocument(
            document_ref=document.event.document_ref,
            document_number=document.event.document_number,
            document_date=document.event.document_date,
            gross_amount=_money(document.gross_amount),
            open_amount=_money(document.open_amount),
            manager_ref=document.event.manager_ref,
            manager_name=document.event.manager_name,
            selection_rule=document.selection_rule,
        )
        for document in sorted(
            (item for item in documents if item.open_amount > Decimal("0.00")),
            key=lambda item: (item.event.document_date, item.event.document_ref),
        )
    )


def _resolution(
    *,
    status: str,
    reason: str,
    current_balance: Decimal,
    documents: Sequence[_OpenDocument] = (),
    payment: ReceivableStatementEvent | None = None,
    base_balance_after: Decimal | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> ReceivablePkoShadowResolution:
    payload = _documents_payload(documents)
    selected = _money(sum((item.open_amount for item in payload), Decimal("0.00")))
    current = _money(current_balance)
    return ReceivablePkoShadowResolution(
        status=status,
        reason=reason,
        current_balance=current,
        selected_open_amount=selected,
        delta=_money(selected - current),
        base_payment_ref=payment.document_ref if payment is not None else None,
        base_payment_number=payment.document_number if payment is not None else None,
        base_payment_date=payment.document_date if payment is not None else None,
        base_balance_after=_money(base_balance_after) if base_balance_after is not None else None,
        documents=payload,
        diagnostics=diagnostics or {},
    )


def _resolve_structure_first(
    events: Sequence[ReceivableStatementEvent],
    *,
    current_balance: Decimal,
    structure_checks: dict[str, ReceivableDocumentStructureCheck],
    return_amounts: dict[str, Decimal],
) -> ReceivablePkoShadowResolution | None:
    sales = {
        _ref_key(event.document_ref): event
        for event in events
        if event.event_type == "sale" and _money(event.amount_delta) > Decimal("0.00")
    }
    confirmed: list[_OpenDocument] = []
    for sale_key, event in sales.items():
        check = structure_checks.get(sale_key)
        if (
            check is None
            or check.status != DOCUMENT_STRUCTURE_CONFIRMED_OPEN
            or check.open_amount is None
        ):
            continue
        linked_refs = {
            _ref_key(item.get("document_ref"))
            for item in check.linked_documents
            if isinstance(item, dict)
        }
        linked_return_amount = sum(
            (
                abs(_money(item.get("amount")))
                for item in check.linked_documents
                if isinstance(item, dict)
                and str(item.get("document_type") or "").casefold().startswith("возврат")
            ),
            Decimal("0.00"),
        )
        exact_return_amount = return_amounts.get(sale_key, Decimal("0.00"))
        # The current production structure checker does not include returns. The
        # linked-ref guard keeps this shadow calculation safe if it gains them later.
        if linked_refs:
            exact_return_amount = max(
                Decimal("0.00"),
                exact_return_amount - linked_return_amount,
            )
        open_amount = _money(max(Decimal("0.00"), _money(check.open_amount) - exact_return_amount))
        if open_amount <= Decimal("0.00"):
            continue
        confirmed.append(
            _OpenDocument(
                event=event,
                gross_amount=_money(check.sale_amount or event.amount_delta),
                open_amount=open_amount,
                selection_rule="pko_shadow_structure_confirmed",
            )
        )
    if not confirmed:
        return None

    selected = _money(sum((item.open_amount for item in confirmed), Decimal("0.00")))
    status = (
        PKO_SHADOW_MATCHED
        if abs(selected - _money(current_balance)) <= PKO_SHADOW_TOLERANCE
        else PKO_SHADOW_DATA_QUALITY
    )
    reason = "structure_confirmed" if status == PKO_SHADOW_MATCHED else "structure_total_mismatch"
    payments = [
        (index, event)
        for index, event in enumerate(events)
        if event.event_type == "payment" and _money(event.amount_delta) < Decimal("0.00")
    ]
    payment_index, payment = payments[-1] if payments else (None, None)
    base_balance_after = None
    if payment_index is not None:
        base_balance_after = _money(
            sum(
                (_money(event.amount_delta) for event in events[: payment_index + 1]),
                Decimal("0.00"),
            )
        )
    return _resolution(
        status=status,
        reason=reason,
        current_balance=current_balance,
        documents=confirmed,
        payment=payment,
        base_balance_after=base_balance_after,
        diagnostics={
            "source": "document_structure",
            "confirmed_document_count": len(confirmed),
            "tolerance": str(PKO_SHADOW_TOLERANCE),
        },
    )


def _apply_credit_to_document(document: _OpenDocument, credit: Decimal) -> Decimal:
    applied = min(document.open_amount, credit)
    document.open_amount = _money(document.open_amount - applied)
    return _money(credit - applied)


def _simulate_from_payment(
    events: Sequence[ReceivableStatementEvent],
    *,
    payment_index: int,
    current_balance: Decimal,
    links_by_ref: dict[str, ReceivableReturnSaleLink],
    balances_after: Sequence[Decimal],
) -> ReceivablePkoShadowResolution:
    payment = events[payment_index]
    base_balance = _money(balances_after[payment_index])
    returns_before = _return_amounts_by_sale(
        events,
        links_by_ref=links_by_ref,
        through_index=payment_index,
    )
    preceding_sales = [
        event
        for event in events[:payment_index]
        if event.event_type == "sale" and _money(event.amount_delta) > Decimal("0.00")
    ]
    open_documents: list[_OpenDocument] = []
    remaining = max(Decimal("0.00"), base_balance)
    for sale in reversed(preceding_sales):
        capacity = _money(
            max(
                Decimal("0.00"),
                _money(sale.amount_delta)
                - returns_before.get(_ref_key(sale.document_ref), Decimal("0.00")),
            )
        )
        if capacity <= Decimal("0.00") or remaining <= Decimal("0.00"):
            continue
        selected = min(capacity, remaining)
        open_documents.append(
            _OpenDocument(
                event=sale,
                gross_amount=_money(sale.amount_delta),
                open_amount=_money(selected),
                selection_rule="pko_shadow_boundary_balance",
            )
        )
        remaining = _money(remaining - selected)
    if remaining > PKO_SHADOW_TOLERANCE:
        return _resolution(
            status=PKO_SHADOW_DATA_QUALITY,
            reason="base_balance_exceeds_known_sales",
            current_balance=current_balance,
            documents=open_documents,
            payment=payment,
            base_balance_after=base_balance,
            diagnostics={"unattributed_base_balance": str(remaining)},
        )

    open_documents.reverse()
    credit = _money(abs(min(Decimal("0.00"), base_balance)))
    sale_events = {
        _ref_key(event.document_ref): event
        for event in events
        if event.event_type == "sale" and _money(event.amount_delta) > Decimal("0.00")
    }
    for event in events[payment_index + 1 :]:
        amount = _money(event.amount_delta)
        if event.event_type == "sale" and amount > Decimal("0.00"):
            document = _OpenDocument(
                event=event,
                gross_amount=amount,
                open_amount=amount,
                selection_rule="pko_shadow_after_base_payment",
            )
            if credit > Decimal("0.00"):
                credit = _apply_credit_to_document(document, credit)
            if document.open_amount > Decimal("0.00"):
                open_documents.append(document)
            continue

        if event.event_type == "return" and amount < Decimal("0.00"):
            link = links_by_ref.get(_ref_key(event.document_ref))
            if link is None or link.status != "confirmed" or not link.sale_document_ref:
                return _resolution(
                    status=PKO_SHADOW_DATA_QUALITY,
                    reason="unconfirmed_return_link",
                    current_balance=current_balance,
                    documents=open_documents,
                    payment=payment,
                    base_balance_after=base_balance,
                )
            sale_key = _ref_key(link.sale_document_ref)
            if sale_key not in sale_events:
                return _resolution(
                    status=PKO_SHADOW_DATA_QUALITY,
                    reason="return_sale_missing_from_statement",
                    current_balance=current_balance,
                    documents=open_documents,
                    payment=payment,
                    base_balance_after=base_balance,
                )
            return_credit = abs(amount)
            target = next(
                (
                    item
                    for item in open_documents
                    if _ref_key(item.event.document_ref) == sale_key
                    and item.open_amount > Decimal("0.00")
                ),
                None,
            )
            if target is not None:
                return_credit = _apply_credit_to_document(target, return_credit)
            credit = _money(credit + return_credit)
            continue

        if event.event_type in {"payment", "settlement", "debt_adjustment"}:
            if amount > Decimal("0.00"):
                return _resolution(
                    status=PKO_SHADOW_DATA_QUALITY,
                    reason="positive_unattributed_adjustment",
                    current_balance=current_balance,
                    documents=open_documents,
                    payment=payment,
                    base_balance_after=base_balance,
                    diagnostics={"document_ref": event.document_ref, "amount": str(amount)},
                )
            closing_credit = abs(amount)
            for document in open_documents:
                if closing_credit <= Decimal("0.00"):
                    break
                closing_credit = _apply_credit_to_document(document, closing_credit)
            credit = _money(credit + closing_credit)
            continue

        if event.event_type == "opening_balance" and amount != Decimal("0.00"):
            return _resolution(
                status=PKO_SHADOW_DATA_QUALITY,
                reason="opening_balance_after_base_payment",
                current_balance=current_balance,
                documents=open_documents,
                payment=payment,
                base_balance_after=base_balance,
            )

    selected = _money(sum((item.open_amount for item in open_documents), Decimal("0.00")) - credit)
    delta = _money(selected - _money(current_balance))
    status = PKO_SHADOW_MATCHED if abs(delta) <= PKO_SHADOW_TOLERANCE else PKO_SHADOW_DATA_QUALITY
    reason = "pko_cycle_matched" if status == PKO_SHADOW_MATCHED else "final_balance_mismatch"
    result = _resolution(
        status=status,
        reason=reason,
        current_balance=current_balance,
        documents=open_documents,
        payment=payment,
        base_balance_after=base_balance,
        diagnostics={
            "remaining_credit": str(credit),
            "calculated_net_open_amount": str(selected),
            "tolerance": str(PKO_SHADOW_TOLERANCE),
        },
    )
    if credit <= Decimal("0.00"):
        return result
    # Credit without later debt must not be hidden by the document-only payload.
    return ReceivablePkoShadowResolution(
        **{
            **result.__dict__,
            "selected_open_amount": selected,
            "delta": delta,
        }
    )


def resolve_receivable_pko_shadow(
    events: Sequence[ReceivableStatementEvent],
    *,
    current_balance: Decimal,
    return_links: dict[str, ReceivableReturnSaleLink] | None = None,
    structure_checks: dict[str, ReceivableDocumentStructureCheck] | None = None,
) -> ReceivablePkoShadowResolution:
    """Resolve open RTUs without changing the production statement resolver."""

    sorted_events = _sorted_events(events)
    current = _money(current_balance)
    sale_refs = {
        _ref_key(event.document_ref)
        for event in sorted_events
        if event.event_type == "sale" and _money(event.amount_delta) > Decimal("0.00")
    }
    links_by_ref = _return_links_by_ref(return_links)
    unresolved_returns = _unconfirmed_returns(
        sorted_events,
        links_by_ref=links_by_ref,
        sale_refs=sale_refs,
    )
    if unresolved_returns:
        return _resolution(
            status=PKO_SHADOW_DATA_QUALITY,
            reason="unconfirmed_return_link",
            current_balance=current,
            diagnostics={"unconfirmed_returns": unresolved_returns},
        )

    return_amounts = _return_amounts_by_sale(sorted_events, links_by_ref=links_by_ref)
    structure_result = _resolve_structure_first(
        sorted_events,
        current_balance=current,
        structure_checks=_structure_checks_by_ref(structure_checks),
        return_amounts=return_amounts,
    )
    if structure_result is not None:
        return structure_result

    balances_after: list[Decimal] = []
    running_balance = Decimal("0.00")
    for event in sorted_events:
        running_balance = _money(running_balance + _money(event.amount_delta))
        balances_after.append(running_balance)

    payment_indices = [
        index
        for index, event in enumerate(sorted_events)
        if event.event_type == "payment" and _money(event.amount_delta) < Decimal("0.00")
    ]
    if not payment_indices:
        return _resolution(
            status=PKO_SHADOW_NO_CANDIDATE,
            reason="no_pko_in_statement",
            current_balance=current,
            diagnostics={"statement_event_count": len(sorted_events)},
        )

    last_failure: ReceivablePkoShadowResolution | None = None
    for payment_index in reversed(payment_indices):
        candidate = _simulate_from_payment(
            sorted_events,
            payment_index=payment_index,
            current_balance=current,
            links_by_ref=links_by_ref,
            balances_after=balances_after,
        )
        if candidate.status == PKO_SHADOW_MATCHED:
            return candidate
        last_failure = candidate
    assert last_failure is not None
    return last_failure


def _chunked(values: Sequence[str], size: int = 500):
    for index in range(0, len(values), size):
        yield list(values[index : index + size])


def fetch_receivable_return_sale_links(
    onec_engine,
    *,
    return_document_refs: Sequence[str],
    sale_document_refs: Sequence[str],
) -> dict[str, ReceivableReturnSaleLink]:
    """Read exact return bases from 1C; ambiguous order bases remain unconfirmed."""

    return_refs = sorted({_ref(value) for value in return_document_refs if _ref(value)})
    sale_refs = sorted({_ref(value) for value in sale_document_refs if _ref(value)})
    if not return_refs:
        return {}

    dialect_name = onec_engine.dialect.name
    nolock = _with_nolock(dialect_name=dialect_name)
    return_ref_expr = _hex_ref_expr("ret._IDRRef", dialect_name=dialect_name)
    basis_ref_expr = _hex_ref_expr("ret._Fld1684_RRRef", dialect_name=dialect_name)
    basis_tref_expr = (
        "master.dbo.fn_varbintohexstr(ret._Fld1684_RTRef)"
        if dialect_name == "mssql"
        else "ret._Fld1684_RTRef"
    )
    sale_ref_expr = _hex_ref_expr("sale._IDRRef", dialect_name=dialect_name)
    order_ref_expr = _hex_ref_expr("sale._Fld4939_RRRef", dialect_name=dialect_name)
    sale_tref = "0x000000CB" if dialect_name == "mssql" else "'0x000000CB'"
    order_tref = "0x00000084" if dialect_name == "mssql" else "'0x00000084'"
    raw_rows: dict[str, dict[str, Any]] = {}
    sales_by_order: dict[str, list[str]] = defaultdict(list)

    with onec_engine.connect() as conn:
        for chunk_index, chunk in enumerate(_chunked(return_refs)):
            where_clause, params = _build_ref_filter_clause(
                dialect_name=dialect_name,
                refs=chunk,
                column_name="ret._IDRRef",
                prefix=f"shadow_return_{chunk_index}",
            )
            stmt = text(f"""
                SELECT
                    {return_ref_expr} AS return_document_ref,
                    {basis_ref_expr} AS basis_ref,
                    {basis_tref_expr} AS basis_tref
                FROM _Document109 AS ret {nolock}
                WHERE ret._Marked = 0x00
                  AND ret._Posted = 0x01
                  AND {where_clause}
            """)
            for row in conn.execute(stmt, params).mappings():
                return_ref = _ref(row.get("return_document_ref"))
                if return_ref:
                    raw_rows[_ref_key(return_ref)] = dict(row)

        for chunk_index, chunk in enumerate(_chunked(sale_refs)):
            where_clause, params = _build_ref_filter_clause(
                dialect_name=dialect_name,
                refs=chunk,
                column_name="sale._IDRRef",
                prefix=f"shadow_sale_{chunk_index}",
            )
            stmt = text(f"""
                SELECT
                    {sale_ref_expr} AS sale_document_ref,
                    {order_ref_expr} AS order_ref
                FROM _Document203 AS sale {nolock}
                WHERE sale._Fld4939_RTRef = {order_tref}
                  AND {where_clause}
            """)
            for row in conn.execute(stmt, params).mappings():
                order_ref = _ref(row.get("order_ref"))
                sale_ref = _ref(row.get("sale_document_ref"))
                if order_ref and sale_ref:
                    sales_by_order[_ref_key(order_ref)].append(sale_ref)

    links: dict[str, ReceivableReturnSaleLink] = {}
    for return_ref in return_refs:
        row = raw_rows.get(_ref_key(return_ref))
        if row is None:
            links[_ref_key(return_ref)] = ReceivableReturnSaleLink(
                return_document_ref=return_ref,
                status="not_found",
            )
            continue
        basis_ref = _ref(row.get("basis_ref")) or None
        basis_tref = _ref(row.get("basis_tref")).casefold()
        if basis_tref == sale_tref.casefold() and basis_ref:
            links[_ref_key(return_ref)] = ReceivableReturnSaleLink(
                return_document_ref=return_ref,
                status="confirmed",
                sale_document_ref=basis_ref,
                basis_ref=basis_ref,
                basis_kind="sale",
            )
            continue
        if basis_tref == order_tref.casefold() and basis_ref:
            candidates = sorted(set(sales_by_order.get(_ref_key(basis_ref), ())))
            links[_ref_key(return_ref)] = ReceivableReturnSaleLink(
                return_document_ref=return_ref,
                status="confirmed" if len(candidates) == 1 else "ambiguous",
                sale_document_ref=candidates[0] if len(candidates) == 1 else None,
                basis_ref=basis_ref,
                basis_kind="order",
            )
            continue
        links[_ref_key(return_ref)] = ReceivableReturnSaleLink(
            return_document_ref=return_ref,
            status="unsupported_basis",
            basis_ref=basis_ref,
        )
    return links


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


def _current_origin(
    case: ReceivableCase,
    current_documents: Sequence[dict[str, Any]],
) -> tuple[str | None, str | None, datetime | None]:
    documents = sorted(
        current_documents,
        key=lambda item: (
            str(item.get("document_date") or "9999-12-31"),
            str(item.get("document_ref") or ""),
        ),
    )
    if documents:
        item = documents[0]
        raw_date = item.get("document_date")
        document_date = None
        if isinstance(raw_date, datetime):
            document_date = raw_date
        elif isinstance(raw_date, str):
            try:
                document_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            except ValueError:
                document_date = None
        return (
            _ref(item.get("document_ref")) or None,
            _ref(item.get("document_number")) or None,
            document_date,
        )
    return case.origin_document_ref, case.origin_document_number, case.origin_document_date


def _responsible_for_candidate(
    candidate: ReceivablePkoShadowDocument | None,
    document_rows: dict[str, SaleDocumentDepartmentRow],
) -> tuple[str | None, str | None]:
    if candidate is None:
        return None, None
    row = document_rows.get(_ref_key(candidate.document_ref))
    if row is not None:
        return (
            row.document_responsible_ref or candidate.manager_ref,
            row.document_responsible_name or candidate.manager_name,
        )
    return candidate.manager_ref, candidate.manager_name


def rebuild_receivable_pko_shadow(
    session: Session,
    *,
    onec_engine,
    snapshot_date: date,
    algorithm_version: str = PKO_SHADOW_ALGORITHM_VERSION,
) -> dict[str, Any]:
    """Build a complete shadow snapshot in the caller's single transaction."""

    cases = (
        session.execute(
            select(ReceivableCase)
            .where(
                ReceivableCase.snapshot_date == snapshot_date,
                ReceivableCase.segment == CASE_BUYERS,
                ReceivableCase.current_balance > Decimal("0.00"),
            )
            .order_by(ReceivableCase.counterparty_ref)
        )
        .scalars()
        .all()
    )
    counterparty_refs = [case.counterparty_ref for case in cases]
    events_by_counterparty = fetch_counterparty_ledger_statement_events(
        session,
        counterparty_refs=counterparty_refs,
        snapshot_date=snapshot_date,
        include_opening_balance=True,
    )
    all_events = [event for events in events_by_counterparty.values() for event in events]
    sale_refs = [event.document_ref for event in all_events if event.event_type == "sale"]
    return_refs = [event.document_ref for event in all_events if event.event_type == "return"]
    structure_checks = fetch_receivable_document_structure_checks(
        onec_engine,
        document_refs=sale_refs,
        snapshot_date=snapshot_date,
    )
    return_links = fetch_receivable_return_sale_links(
        onec_engine,
        return_document_refs=return_refs,
        sale_document_refs=sale_refs,
    )
    document_rows = fetch_sale_document_departments(
        onec_engine,
        document_refs=sale_refs,
    )
    current_cache_rows = (
        session.execute(
            select(ReceivableOpenDebtCache).where(
                ReceivableOpenDebtCache.snapshot_date == snapshot_date,
                ReceivableOpenDebtCache.counterparty_ref.in_(counterparty_refs),
            )
        )
        .scalars()
        .all()
        if counterparty_refs
        else []
    )
    current_cache = {_ref_key(row.counterparty_ref): row for row in current_cache_rows}

    run_id = uuid4().hex
    computed_at = datetime.now(UTC).replace(tzinfo=None)
    rows: list[ReceivablePkoShadowResult] = []
    status_counts: dict[str, int] = defaultdict(int)
    for case in cases:
        key = _ref_key(case.counterparty_ref)
        events = events_by_counterparty.get(key, ())
        event_sale_refs = {
            _ref_key(event.document_ref) for event in events if event.event_type == "sale"
        }
        event_return_refs = {
            _ref_key(event.document_ref) for event in events if event.event_type == "return"
        }
        resolution = resolve_receivable_pko_shadow(
            events,
            current_balance=_money(case.current_balance),
            return_links={
                ref: link for ref, link in return_links.items() if ref in event_return_refs
            },
            structure_checks={
                ref: check for ref, check in structure_checks.items() if ref in event_sale_refs
            },
        )
        current_documents = (
            list(current_cache.get(key).documents or []) if key in current_cache else []
        )
        current_ref, current_number, current_date = _current_origin(case, current_documents)
        candidate = resolution.documents[0] if resolution.documents else None
        responsible_ref, responsible_name = _responsible_for_candidate(candidate, document_rows)
        rows.append(
            ReceivablePkoShadowResult(
                snapshot_date=snapshot_date,
                algorithm_version=algorithm_version,
                run_id=run_id,
                counterparty_ref=case.counterparty_ref,
                counterparty_code=case.counterparty_code,
                counterparty_name=case.counterparty_name,
                department_ref=case.department_ref,
                department_name=case.department_name,
                current_balance=resolution.current_balance,
                base_payment_ref=resolution.base_payment_ref,
                base_payment_number=resolution.base_payment_number,
                base_payment_date=resolution.base_payment_date,
                base_balance_after=resolution.base_balance_after,
                current_origin_document_ref=current_ref,
                current_origin_document_number=current_number,
                current_origin_document_date=current_date,
                candidate_origin_document_ref=candidate.document_ref if candidate else None,
                candidate_origin_document_number=candidate.document_number if candidate else None,
                candidate_origin_document_date=candidate.document_date if candidate else None,
                candidate_responsible_ref=responsible_ref,
                candidate_responsible_name=responsible_name,
                candidate_origin_open_amount=candidate.open_amount if candidate else None,
                selected_open_amount=resolution.selected_open_amount,
                delta=resolution.delta,
                status=resolution.status,
                reason=resolution.reason,
                current_documents=_json_safe(current_documents),
                candidate_documents=_json_safe(
                    [document.__dict__ for document in resolution.documents]
                ),
                diagnostics=_json_safe(resolution.diagnostics),
                computed_at=computed_at,
            )
        )
        status_counts[resolution.status] += 1

    session.execute(
        delete(ReceivablePkoShadowResult).where(
            ReceivablePkoShadowResult.snapshot_date == snapshot_date,
            ReceivablePkoShadowResult.algorithm_version == algorithm_version,
        )
    )
    session.add_all(rows)
    session.flush()
    return {
        "snapshot_date": snapshot_date,
        "algorithm_version": algorithm_version,
        "run_id": run_id,
        "computed_at": computed_at,
        "row_count": len(rows),
        "status_counts": dict(status_counts),
    }


def load_receivable_pko_shadow(
    session: Session,
    *,
    snapshot_date: date,
    algorithm_version: str = PKO_SHADOW_ALGORITHM_VERSION,
) -> tuple[list[ReceivablePkoShadowResult], dict[str, Any]]:
    rows = (
        session.execute(
            select(ReceivablePkoShadowResult)
            .where(
                ReceivablePkoShadowResult.snapshot_date == snapshot_date,
                ReceivablePkoShadowResult.algorithm_version == algorithm_version,
            )
            .order_by(
                ReceivablePkoShadowResult.status,
                func.abs(ReceivablePkoShadowResult.delta).desc(),
                ReceivablePkoShadowResult.counterparty_name,
            )
        )
        .scalars()
        .all()
    )
    summary = {
        "row_count": len(rows),
        "matched_count": sum(1 for row in rows if row.status == PKO_SHADOW_MATCHED),
        "data_quality_count": sum(1 for row in rows if row.status == PKO_SHADOW_DATA_QUALITY),
        "no_candidate_count": sum(1 for row in rows if row.status == PKO_SHADOW_NO_CANDIDATE),
    }
    return rows, summary
