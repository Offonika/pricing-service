from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Sequence

STATEMENT_RULE_DIRECT_PAYMENT = "statement_direct_payment_match"
STATEMENT_RULE_MULTI_SALE_PAYMENT = "statement_multi_sale_payment_match"
STATEMENT_RULE_BOTTOM_UP_BALANCE = "statement_bottom_up_balance_cutoff"
STATEMENT_RULE_UNMATCHED_OPEN = "statement_unmatched_open_sale"
STATEMENT_RULE_STRUCTURE_OPEN = "statement_structure_confirmed_open"
STATEMENT_RULE_RETURN_PKO_RKO_MATCH = "statement_return_pko_rko_match"
STATEMENT_RULE_RETURN_RKO_WITHOUT_PKO_REVIEW = "statement_return_rko_without_pko_review"

DIRECT_PAYMENT_TOLERANCE = Decimal("50.00")
MULTI_SALE_PAYMENT_TOLERANCE = Decimal("100.00")
SAFE_BALANCE_TOLERANCE = Decimal("2000.00")
NEARBY_PAYMENT_ROW_WINDOW = 7
MULTI_SALE_ROW_WINDOW = 7


@dataclass(frozen=True)
class ReceivableStatementEvent:
    counterparty_ref: str
    event_type: str
    document_ref: str
    document_number: str | None
    document_date: datetime
    amount_delta: Decimal
    manager_ref: str | None = None
    manager_name: str | None = None
    line_no: int | None = None
    source_layer: str | None = None
    contract_ref: str | None = None
    contract_name: str | None = None
    contract_kind_ref: str | None = None
    contract_kind_name: str | None = None
    settlement_document_ref: str | None = None
    settlement_document_name: str | None = None


@dataclass(frozen=True)
class ReceivableStatementOpenDocument:
    document_ref: str
    document_number: str | None
    document_date: datetime
    open_amount: Decimal
    gross_amount: Decimal
    closing_amount: Decimal
    return_amount: Decimal
    manager_ref: str | None
    manager_name: str | None
    statement_balance_after: Decimal | None
    statement_segment_start_row: int | None
    statement_segment_end_row: int | None
    statement_selection_rule: str
    statement_match_details: tuple[dict[str, Any], ...] = ()
    contract_ref: str | None = None
    contract_name: str | None = None
    contract_kind_ref: str | None = None
    contract_kind_name: str | None = None
    settlement_document_ref: str | None = None
    settlement_document_name: str | None = None


@dataclass
class _ClosingLayer:
    event: ReceivableStatementEvent
    amount: Decimal
    row_index: int
    used: bool = False
    blocks_safe_balance: bool = False


@dataclass
class _SaleLayer:
    event: ReceivableStatementEvent
    row_index: int
    gross_amount: Decimal
    open_amount: Decimal
    structure_closing_amount: Decimal = Decimal("0.00")
    return_amount: Decimal = Decimal("0.00")
    statement_balance_after: Decimal | None = None
    statement_segment_start_row: int | None = None
    statement_segment_end_row: int | None = None
    closed: bool = False
    selection_rule: str = STATEMENT_RULE_UNMATCHED_OPEN
    match_details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def match_amount(self) -> Decimal:
        value = self.open_amount - self.return_amount
        return _money(value if value > Decimal("0.00") else Decimal("0.00"))


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _event_sort_key(event: ReceivableStatementEvent, index: int) -> tuple[datetime, int, str, int]:
    line_no = event.line_no if event.line_no is not None else 999_999
    return (event.document_date, line_no, event.document_ref, index)


def _distance(left: int, right: int) -> int:
    return abs(left - right)


def _text_key(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _is_close(left: Decimal, right: Decimal, tolerance: Decimal) -> bool:
    return abs(_money(left) - _money(right)) <= tolerance


def _event_text(event: ReceivableStatementEvent) -> str:
    return _text_key(f"{event.event_type} {event.document_number or ''} {event.document_ref or ''}")


def _is_rko_like(event: ReceivableStatementEvent) -> bool:
    text = _event_text(event)
    return event.event_type == "rko" or "рко" in text or "расходн" in text


def _is_pko_like(event: ReceivableStatementEvent) -> bool:
    text = _event_text(event)
    return event.event_type == "payment" or "пко" in text or "приходн" in text


def _closing_detail(closing: _ClosingLayer, *, rule: str) -> dict[str, Any]:
    return {
        "rule": rule,
        "document_type": _closing_type_label(closing.event.event_type),
        "document_ref": closing.event.document_ref,
        "document_number": closing.event.document_number,
        "document_date": closing.event.document_date,
        "amount": _money(-closing.amount),
    }


def _return_detail(return_layer: _ClosingLayer, *, rule: str) -> dict[str, Any]:
    return {
        "rule": rule,
        "document_type": "Возврат товаров от покупателя",
        "document_ref": return_layer.event.document_ref,
        "document_number": return_layer.event.document_number,
        "document_date": return_layer.event.document_date,
        "amount": _money(-return_layer.amount),
    }


def _closing_type_label(event_type: str) -> str:
    return {
        "payment": "Приходный кассовый ордер",
        "settlement": "Документ урегулирования",
        "debt_adjustment": "Корректировка долга",
    }.get(event_type, event_type)


def _apply_structure_checks(
    sale_layers: list[_SaleLayer],
    structure_checks: dict[str, Any],
    *,
    ref_key,
) -> None:
    for sale in sale_layers:
        check = structure_checks.get(ref_key(sale.event.document_ref))
        if check is None:
            continue
        open_amount = getattr(check, "open_amount", None)
        closing_amount = getattr(check, "closing_amount", None)
        status = str(getattr(check, "status", "") or "")
        if closing_amount is not None:
            sale.structure_closing_amount = _money(abs(_money(closing_amount)))
        if open_amount is not None:
            sale.open_amount = _money(open_amount)
        if status == "closed_by_structure" or sale.open_amount <= Decimal("0.00"):
            sale.closed = True
            sale.open_amount = Decimal("0.00")
            sale.selection_rule = "statement_structure_closed"
        elif status == "confirmed_open":
            sale.selection_rule = STATEMENT_RULE_STRUCTURE_OPEN
            for item in getattr(check, "linked_documents", ()) or ():
                if not isinstance(item, dict):
                    continue
                detail = dict(item)
                detail.setdefault("rule", STATEMENT_RULE_STRUCTURE_OPEN)
                sale.match_details.append(detail)


def _structure_linked_document_refs(
    structure_checks: dict[str, Any],
    *,
    ref_key,
) -> set[str]:
    refs: set[str] = set()
    for check in structure_checks.values():
        for item in getattr(check, "linked_documents", ()) or ():
            if not isinstance(item, dict):
                continue
            document_ref = str(item.get("document_ref") or "").strip()
            if document_ref:
                refs.add(ref_key(document_ref))
    return refs


def _mark_structure_linked_closings_used(
    closing_layers: list[_ClosingLayer],
    *,
    linked_document_refs: set[str],
    ref_key,
) -> None:
    if not linked_document_refs:
        return
    for closing in closing_layers:
        if ref_key(closing.event.document_ref) in linked_document_refs:
            closing.used = True


def _apply_nearby_returns(
    sale_layers: list[_SaleLayer],
    return_layers: list[_ClosingLayer],
    payment_layers: list[_ClosingLayer],
) -> None:
    for ret in return_layers:
        candidates = [
            sale
            for sale in sale_layers
            if not sale.closed
            and sale.match_amount > Decimal("0.00")
            and _distance(sale.row_index, ret.row_index) <= NEARBY_PAYMENT_ROW_WINDOW
        ]
        if not candidates:
            continue
        sale = min(
            candidates, key=lambda item: (_distance(item.row_index, ret.row_index), item.row_index)
        )
        nearby_rko = next(
            (
                payment
                for payment in payment_layers
                if not payment.used
                and _is_rko_like(payment.event)
                and _distance(sale.row_index, payment.row_index) <= NEARBY_PAYMENT_ROW_WINDOW
                and _is_close(ret.amount, payment.amount, DIRECT_PAYMENT_TOLERANCE)
            ),
            None,
        )
        nearby_pko = next(
            (
                payment
                for payment in payment_layers
                if not payment.used
                and _is_pko_like(payment.event)
                and _distance(sale.row_index, payment.row_index) <= NEARBY_PAYMENT_ROW_WINDOW
                and _is_close(ret.amount, payment.amount, DIRECT_PAYMENT_TOLERANCE)
            ),
            None,
        )
        if nearby_rko is not None and nearby_pko is None:
            ret.used = True
            ret.blocks_safe_balance = True
            nearby_rko.used = True
            nearby_rko.blocks_safe_balance = True
            sale.match_details.append(
                _return_detail(ret, rule=STATEMENT_RULE_RETURN_RKO_WITHOUT_PKO_REVIEW)
            )
            sale.match_details.append(
                _closing_detail(nearby_rko, rule=STATEMENT_RULE_RETURN_RKO_WITHOUT_PKO_REVIEW)
            )
            continue
        if (
            nearby_rko is not None
            and nearby_pko is not None
            and _is_close(ret.amount, sale.gross_amount, DIRECT_PAYMENT_TOLERANCE)
        ):
            ret.used = True
            nearby_rko.used = True
            nearby_pko.used = True
            sale.return_amount = _money(sale.return_amount + ret.amount)
            sale.closed = True
            sale.open_amount = Decimal("0.00")
            sale.selection_rule = STATEMENT_RULE_RETURN_PKO_RKO_MATCH
            sale.match_details.append(_return_detail(ret, rule=STATEMENT_RULE_RETURN_PKO_RKO_MATCH))
            sale.match_details.append(
                _closing_detail(nearby_rko, rule=STATEMENT_RULE_RETURN_PKO_RKO_MATCH)
            )
            sale.match_details.append(
                _closing_detail(nearby_pko, rule=STATEMENT_RULE_RETURN_PKO_RKO_MATCH)
            )
            continue
        sale.return_amount = _money(sale.return_amount + ret.amount)
        sale.match_details.append(_return_detail(ret, rule="statement_nearby_return"))
        ret.used = True


def _apply_direct_payment_matches(
    sale_layers: list[_SaleLayer],
    payment_layers: list[_ClosingLayer],
) -> None:
    for sale in sale_layers:
        if sale.closed or sale.match_amount <= Decimal("0.00"):
            continue
        candidates = [
            payment
            for payment in payment_layers
            if not payment.used
            and not _is_rko_like(payment.event)
            and _distance(sale.row_index, payment.row_index) <= NEARBY_PAYMENT_ROW_WINDOW
            and _is_close(sale.match_amount, payment.amount, DIRECT_PAYMENT_TOLERANCE)
        ]
        if not candidates:
            continue
        payment = min(
            candidates, key=lambda item: (_distance(sale.row_index, item.row_index), item.row_index)
        )
        payment.used = True
        sale.closed = True
        sale.open_amount = Decimal("0.00")
        sale.selection_rule = STATEMENT_RULE_DIRECT_PAYMENT
        sale.match_details.append(_closing_detail(payment, rule=STATEMENT_RULE_DIRECT_PAYMENT))


def _sale_group(
    sale_layers: list[_SaleLayer],
    start_index: int,
    *,
    direction: int,
    count: int,
) -> list[_SaleLayer]:
    selected: list[_SaleLayer] = []
    for offset in range(count + 1):
        index = start_index + direction * offset
        if index < 0 or index >= len(sale_layers):
            return []
        sale = sale_layers[index]
        if sale.closed or sale.match_amount <= Decimal("0.00"):
            return []
        selected.append(sale)
    return selected


def _apply_multi_sale_payment_matches(
    sale_layers: list[_SaleLayer],
    payment_layers: list[_ClosingLayer],
) -> None:
    for payment in payment_layers:
        if payment.used or _is_rko_like(payment.event):
            continue
        candidate_indices = [
            index
            for index, sale in enumerate(sale_layers)
            if not sale.closed
            and sale.match_amount > Decimal("0.00")
            and _distance(sale.row_index, payment.row_index) <= MULTI_SALE_ROW_WINDOW
        ]
        candidate_index_set = set(candidate_indices)
        for sale_index in candidate_indices:
            group: list[_SaleLayer] = []
            group_amount = Decimal("0.00")
            for index in range(
                sale_index, min(len(sale_layers), sale_index + MULTI_SALE_ROW_WINDOW + 1)
            ):
                if index not in candidate_index_set:
                    break
                sale = sale_layers[index]
                group.append(sale)
                group_amount += sale.match_amount
                if len(group) < 2:
                    continue
                if (
                    min(_distance(item.row_index, payment.row_index) for item in group)
                    > NEARBY_PAYMENT_ROW_WINDOW
                ):
                    continue
                if not _is_close(group_amount, payment.amount, MULTI_SALE_PAYMENT_TOLERANCE):
                    continue
                payment.used = True
                detail = _closing_detail(payment, rule=STATEMENT_RULE_MULTI_SALE_PAYMENT)
                detail["matched_sale_count"] = len(group)
                for item in group:
                    item.closed = True
                    item.open_amount = Decimal("0.00")
                    item.selection_rule = STATEMENT_RULE_MULTI_SALE_PAYMENT
                    item.match_details.append(detail)
                break
            if payment.used:
                break


def _closing_belongs_to_following_sale(
    *,
    closing: _ClosingLayer,
    sale_layers: list[_SaleLayer],
) -> bool:
    following_sales = [
        sale
        for sale in sale_layers
        if sale.row_index > closing.row_index
        and _distance(sale.row_index, closing.row_index) <= MULTI_SALE_ROW_WINDOW
        and sale.match_amount > Decimal("0.00")
    ][:MULTI_SALE_ROW_WINDOW]
    if not following_sales:
        return False

    if _is_close(following_sales[0].match_amount, closing.amount, DIRECT_PAYMENT_TOLERANCE):
        return True

    accumulated = Decimal("0.00")
    for index, sale in enumerate(following_sales, start=1):
        accumulated += sale.match_amount
        if index > 1 and _is_close(accumulated, closing.amount, MULTI_SALE_PAYMENT_TOLERANCE):
            return True
    return False


def _last_safe_balance_row(
    balances_after: Sequence[Decimal],
    *,
    sale_layers: list[_SaleLayer],
    payment_layers: list[_ClosingLayer],
    return_layers: list[_ClosingLayer] | None = None,
) -> int | None:
    payment_by_row = {payment.row_index: payment for payment in payment_layers}
    blocked_rows = {
        layer.row_index
        for layer in [*payment_layers, *(return_layers or [])]
        if layer.blocks_safe_balance
    }
    last_safe_row: int | None = None
    for row_index, balance_after in enumerate(balances_after):
        if row_index == len(balances_after) - 1:
            continue
        if row_index in blocked_rows:
            continue
        if not Decimal("0.00") <= balance_after <= SAFE_BALANCE_TOLERANCE:
            continue
        payment = payment_by_row.get(row_index)
        if payment is None and balance_after > Decimal("0.00"):
            continue
        if payment is not None and _closing_belongs_to_following_sale(
            closing=payment,
            sale_layers=sale_layers,
        ):
            continue
        last_safe_row = row_index
    return last_safe_row


def _apply_statement_segment(
    sale_layers: list[_SaleLayer],
    balances_after: Sequence[Decimal],
    *,
    segment_start_row: int,
    segment_end_row: int | None,
) -> None:
    for sale in sale_layers:
        if sale.row_index < len(balances_after):
            sale.statement_balance_after = _money(balances_after[sale.row_index])
        sale.statement_segment_start_row = segment_start_row + 1
        sale.statement_segment_end_row = None if segment_end_row is None else segment_end_row + 1


def _select_open_sales_by_balance(
    open_sales: list[_SaleLayer],
    *,
    current_balance: Decimal,
) -> list[_SaleLayer]:
    total_open = _money(sum((sale.match_amount for sale in open_sales), Decimal("0.00")))
    if current_balance <= Decimal("0.00") or not open_sales:
        return []
    if total_open <= current_balance + MULTI_SALE_PAYMENT_TOLERANCE:
        return open_sales

    selected: list[_SaleLayer] = []
    accumulated = Decimal("0.00")
    for sale in reversed(open_sales):
        sale_amount = sale.match_amount
        if sale_amount <= Decimal("0.00"):
            continue
        remaining = current_balance - accumulated
        if remaining <= Decimal("0.00"):
            break
        if sale_amount > remaining:
            sale.open_amount = _money(remaining)
        else:
            sale.open_amount = sale_amount
        sale.selection_rule = STATEMENT_RULE_BOTTOM_UP_BALANCE
        sale.match_details.append(
            {
                "rule": STATEMENT_RULE_BOTTOM_UP_BALANCE,
                "current_balance": _money(current_balance),
                "accumulated_with_document": _money(accumulated + sale_amount),
            }
        )
        selected.append(sale)
        accumulated += sale_amount
        if accumulated >= current_balance:
            break
    return sorted(selected, key=lambda item: (item.event.document_date, item.event.document_ref))


def _settlement_group_key(event: ReceivableStatementEvent) -> tuple[str, str, str, str, str, str]:
    return (
        _text_key(event.contract_ref),
        _text_key(event.contract_name),
        _text_key(event.contract_kind_ref),
        _text_key(event.contract_kind_name),
        _text_key(event.settlement_document_ref),
        _text_key(event.settlement_document_name),
    )


def _has_settlement_grouping(
    grouped_events: dict[tuple[str, str, str, str, str, str], list[ReceivableStatementEvent]],
) -> bool:
    if len(grouped_events) <= 1:
        return False
    return any(any(key_part for key_part in key) for key in grouped_events)


def _group_balance(events: Sequence[ReceivableStatementEvent]) -> Decimal:
    return _money(sum((_money(event.amount_delta) for event in events), Decimal("0.00")))


def resolve_open_debt_documents_by_statement(
    events: Sequence[ReceivableStatementEvent],
    *,
    current_balance: Decimal,
    structure_checks: dict[str, Any] | None = None,
    ref_key=lambda value: str(value or "").strip().casefold(),
) -> list[ReceivableStatementOpenDocument]:
    grouped_events: dict[tuple[str, str, str, str, str, str], list[ReceivableStatementEvent]] = {}
    for event in events:
        grouped_events.setdefault(_settlement_group_key(event), []).append(event)
    if not _has_settlement_grouping(grouped_events):
        return _resolve_open_debt_documents_by_statement_group(
            events,
            current_balance=_money(current_balance),
            structure_checks=structure_checks,
            ref_key=ref_key,
        )

    documents: list[ReceivableStatementOpenDocument] = []
    for group_events in grouped_events.values():
        group_current_balance = _group_balance(group_events)
        if group_current_balance <= Decimal("0.00"):
            continue
        documents.extend(
            _resolve_open_debt_documents_by_statement_group(
                group_events,
                current_balance=group_current_balance,
                structure_checks=structure_checks,
                ref_key=ref_key,
            )
        )
    if documents:
        sorted_documents = sorted(
            documents, key=lambda item: (item.document_date, item.document_ref)
        )
        total_open = _money(
            sum((document.open_amount for document in sorted_documents), Decimal("0.00"))
        )
        if total_open <= _money(current_balance) + MULTI_SALE_PAYMENT_TOLERANCE:
            return sorted_documents
        return _resolve_open_debt_documents_by_statement_group(
            events,
            current_balance=_money(current_balance),
            structure_checks=structure_checks,
            ref_key=ref_key,
        )
    if _money(current_balance) <= Decimal("0.00"):
        return []
    return _resolve_open_debt_documents_by_statement_group(
        events,
        current_balance=_money(current_balance),
        structure_checks=structure_checks,
        ref_key=ref_key,
    )


def _resolve_open_debt_documents_by_statement_group(
    events: Sequence[ReceivableStatementEvent],
    *,
    current_balance: Decimal,
    structure_checks: dict[str, Any] | None = None,
    ref_key=lambda value: str(value or "").strip().casefold(),
) -> list[ReceivableStatementOpenDocument]:
    """Resolve open sales with rules close to 1C mutual-settlement statement checks."""

    sorted_events = [
        event
        for _, event in sorted(
            enumerate(events),
            key=lambda item: _event_sort_key(item[1], item[0]),
        )
    ]
    sale_layers: list[_SaleLayer] = []
    payment_layers: list[_ClosingLayer] = []
    return_layers: list[_ClosingLayer] = []
    for index, event in enumerate(sorted_events):
        event_type = event.event_type
        amount = _money(event.amount_delta)
        if event_type == "sale" and amount > Decimal("0.00"):
            sale_layers.append(
                _SaleLayer(
                    event=event,
                    row_index=index,
                    gross_amount=amount,
                    open_amount=amount,
                )
            )
        elif event_type in {"payment", "settlement", "debt_adjustment"} and amount < Decimal(
            "0.00"
        ):
            payment_layers.append(_ClosingLayer(event=event, amount=abs(amount), row_index=index))
        elif event_type == "return" and amount < Decimal("0.00"):
            return_layers.append(_ClosingLayer(event=event, amount=abs(amount), row_index=index))

    balances_after: list[Decimal] = []
    running_balance = Decimal("0.00")
    for event in sorted_events:
        running_balance = _money(running_balance + _money(event.amount_delta))
        balances_after.append(running_balance)

    sorted_event_ref_keys = {ref_key(event.document_ref) for event in sorted_events}
    resolved_structure_checks = {
        ref_key(document_ref): check
        for document_ref, check in (structure_checks or {}).items()
        if ref_key(document_ref) in sorted_event_ref_keys
    }
    _apply_structure_checks(sale_layers, resolved_structure_checks, ref_key=ref_key)
    structure_linked_document_refs = _structure_linked_document_refs(
        resolved_structure_checks,
        ref_key=ref_key,
    )
    _mark_structure_linked_closings_used(
        payment_layers,
        linked_document_refs=structure_linked_document_refs,
        ref_key=ref_key,
    )
    _mark_structure_linked_closings_used(
        return_layers,
        linked_document_refs=structure_linked_document_refs,
        ref_key=ref_key,
    )
    _apply_nearby_returns(sale_layers, return_layers, payment_layers)
    _apply_direct_payment_matches(sale_layers, payment_layers)
    _apply_multi_sale_payment_matches(sale_layers, payment_layers)

    last_safe_row = _last_safe_balance_row(
        balances_after,
        sale_layers=sale_layers,
        payment_layers=payment_layers,
        return_layers=return_layers,
    )
    segment_start_row = 0 if last_safe_row is None else last_safe_row + 1
    segment_end_row = len(sorted_events) - 1 if sorted_events else None
    _apply_statement_segment(
        sale_layers,
        balances_after,
        segment_start_row=segment_start_row,
        segment_end_row=segment_end_row,
    )

    open_sales = [
        sale
        for sale in sale_layers
        if not sale.closed
        and sale.match_amount > Decimal("0.00")
        and sale.row_index >= segment_start_row
    ]
    selected_sales = _select_open_sales_by_balance(
        open_sales,
        current_balance=_money(current_balance),
    )

    documents: list[ReceivableStatementOpenDocument] = []
    for sale in selected_sales:
        open_amount = sale.open_amount if sale.open_amount > Decimal("0.00") else sale.match_amount
        documents.append(
            ReceivableStatementOpenDocument(
                document_ref=sale.event.document_ref,
                document_number=sale.event.document_number,
                document_date=sale.event.document_date,
                open_amount=_money(open_amount),
                gross_amount=sale.gross_amount,
                closing_amount=_money(sale.structure_closing_amount),
                return_amount=_money(sale.return_amount),
                manager_ref=sale.event.manager_ref,
                manager_name=sale.event.manager_name,
                statement_balance_after=sale.statement_balance_after,
                statement_segment_start_row=sale.statement_segment_start_row,
                statement_segment_end_row=sale.statement_segment_end_row,
                statement_selection_rule=sale.selection_rule,
                statement_match_details=tuple(sale.match_details),
                contract_ref=sale.event.contract_ref,
                contract_name=sale.event.contract_name,
                contract_kind_ref=sale.event.contract_kind_ref,
                contract_kind_name=sale.event.contract_kind_name,
                settlement_document_ref=sale.event.settlement_document_ref,
                settlement_document_name=sale.event.settlement_document_name,
            )
        )
    return documents
