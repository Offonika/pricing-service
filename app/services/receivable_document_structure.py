from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Sequence

from sqlalchemy import text

from app.services.receivables import _build_ref_filter_clause, _hex_ref_expr, _with_nolock

DOCUMENT_STRUCTURE_CONFIRMED_OPEN = "confirmed_open"
DOCUMENT_STRUCTURE_CLOSED = "closed_by_structure"
DOCUMENT_STRUCTURE_AMBIGUOUS = "ambiguous"
DOCUMENT_STRUCTURE_NOT_FOUND = "not_found"


@dataclass(frozen=True)
class ReceivableDocumentStructureCheck:
    document_ref: str
    status: str
    open_amount: Decimal | None
    sale_amount: Decimal | None
    closing_amount: Decimal | None
    sale_number: str | None
    sale_date: datetime | None
    order_ref: str | None
    order_number: str | None
    order_date: datetime | None
    linked_documents: tuple[dict[str, Any], ...]


def _normalize_ref(value: Any) -> str:
    return str(value or "").strip()


def _ref_key(value: Any) -> str:
    return _normalize_ref(value).casefold()


def _chunked(values: Sequence[str], size: int = 500) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield list(values[index : index + size])


def _to_decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _clean_string(value: Any) -> str | None:
    text_value = str(value or "").strip()
    return text_value or None


def _tref_literal(value: str, *, dialect_name: str) -> str:
    if dialect_name == "mssql":
        return value
    return f"'{value}'"


def fetch_receivable_document_structure_checks(
    onec_engine,
    *,
    document_refs: Sequence[str],
    snapshot_date: date,
) -> dict[str, ReceivableDocumentStructureCheck]:
    refs = sorted({_normalize_ref(value) for value in document_refs if _normalize_ref(value)})
    if not refs:
        return {}

    sale_rows = _fetch_sale_rows(onec_engine, refs=refs)
    sale_amounts = _fetch_sale_amounts(onec_engine, refs=refs)
    payment_rows = _fetch_payment_rows(
        onec_engine,
        sale_rows=sale_rows,
        snapshot_date=snapshot_date,
    )
    settlement_rows = _fetch_settlement_rows(
        onec_engine,
        sale_rows=sale_rows,
        snapshot_date=snapshot_date,
    )

    checks: dict[str, ReceivableDocumentStructureCheck] = {}
    for ref in refs:
        key = _ref_key(ref)
        sale_row = sale_rows.get(key)
        if sale_row is None:
            checks[key] = ReceivableDocumentStructureCheck(
                document_ref=ref,
                status=DOCUMENT_STRUCTURE_NOT_FOUND,
                open_amount=None,
                sale_amount=None,
                closing_amount=None,
                sale_number=None,
                sale_date=None,
                order_ref=None,
                order_number=None,
                order_date=None,
                linked_documents=(),
            )
            continue

        sale_amount = sale_amounts.get(key)
        linked_documents = [
            dict(row)
            for row in sorted(
                [*payment_rows.get(key, ()), *settlement_rows.get(key, ())],
                key=lambda item: (
                    str(item.get("document_date") or ""),
                    str(item.get("document_number") or ""),
                ),
            )
        ]
        closing_amount = sum(
            (_to_decimal(row.get("amount")) for row in linked_documents),
            Decimal("0.00"),
        )
        open_amount: Decimal | None = None
        if sale_amount is not None:
            open_amount = (sale_amount + closing_amount).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )

        if sale_amount is None:
            status = DOCUMENT_STRUCTURE_AMBIGUOUS
        elif open_amount is not None and open_amount > Decimal("0.00"):
            status = DOCUMENT_STRUCTURE_CONFIRMED_OPEN
        else:
            status = DOCUMENT_STRUCTURE_CLOSED

        checks[key] = ReceivableDocumentStructureCheck(
            document_ref=ref,
            status=status,
            open_amount=open_amount,
            sale_amount=sale_amount,
            closing_amount=closing_amount,
            sale_number=sale_row.get("sale_number"),
            sale_date=sale_row.get("sale_date"),
            order_ref=sale_row.get("order_ref"),
            order_number=sale_row.get("order_number"),
            order_date=sale_row.get("order_date"),
            linked_documents=tuple(linked_documents),
        )

    return checks


def _fetch_sale_rows(onec_engine, *, refs: Sequence[str]) -> dict[str, dict[str, Any]]:
    dialect_name = onec_engine.dialect.name
    nolock = _with_nolock(dialect_name=dialect_name)
    sale_ref_expr = _hex_ref_expr("sale._IDRRef", dialect_name=dialect_name)
    order_ref_expr = _hex_ref_expr("order_doc._IDRRef", dialect_name=dialect_name)
    order_tref = _tref_literal("0x00000084", dialect_name=dialect_name)
    rows: dict[str, dict[str, Any]] = {}

    with onec_engine.connect() as conn:
        for chunk in _chunked(refs):
            where_clause, params = _build_ref_filter_clause(
                dialect_name=dialect_name,
                refs=chunk,
                column_name="sale._IDRRef",
                prefix="sale_ref",
            )
            stmt = text(f"""
                SELECT
                    {sale_ref_expr} AS document_ref,
                    sale._Number AS sale_number,
                    sale._Date_Time AS sale_date,
                    {order_ref_expr} AS order_ref,
                    order_doc._Number AS order_number,
                    order_doc._Date_Time AS order_date
                FROM _Document203 AS sale {nolock}
                LEFT JOIN _Document132 AS order_doc {nolock}
                    ON sale._Fld4939_RTRef = {order_tref}
                   AND order_doc._IDRRef = sale._Fld4939_RRRef
                WHERE {where_clause}
            """)
            for row in conn.execute(stmt, params).mappings():
                document_ref = _normalize_ref(row.get("document_ref"))
                if not document_ref:
                    continue
                rows[_ref_key(document_ref)] = {
                    "document_ref": document_ref,
                    "sale_number": _clean_string(row.get("sale_number")),
                    "sale_date": row.get("sale_date"),
                    "order_ref": _clean_string(row.get("order_ref")),
                    "order_number": _clean_string(row.get("order_number")),
                    "order_date": row.get("order_date"),
                }
    return rows


def _fetch_sale_amounts(onec_engine, *, refs: Sequence[str]) -> dict[str, Decimal]:
    dialect_name = onec_engine.dialect.name
    nolock = _with_nolock(dialect_name=dialect_name)
    sale_ref_expr = _hex_ref_expr("r._RecorderRRef", dialect_name=dialect_name)
    sale_tref = _tref_literal("0x000000CB", dialect_name=dialect_name)
    rows: dict[str, Decimal] = {}

    with onec_engine.connect() as conn:
        for chunk in _chunked(refs):
            where_clause, params = _build_ref_filter_clause(
                dialect_name=dialect_name,
                refs=chunk,
                column_name="r._RecorderRRef",
                prefix="sale_amount_ref",
            )
            stmt = text(f"""
                SELECT
                    {sale_ref_expr} AS document_ref,
                    SUM(CAST(r._Fld7562 AS decimal(18, 2))) AS amount
                FROM _AccumRg7550 AS r {nolock}
                WHERE r._Active = 0x01
                  AND r._RecorderTRef = {sale_tref}
                  AND {where_clause}
                GROUP BY r._RecorderRRef
            """)
            for row in conn.execute(stmt, params).mappings():
                document_ref = _normalize_ref(row.get("document_ref"))
                if document_ref:
                    rows[_ref_key(document_ref)] = _to_decimal(row.get("amount"))
    return rows


def _basis_refs_by_sale(sale_rows: dict[str, dict[str, Any]]) -> dict[str, set[str]]:
    refs_by_sale: dict[str, set[str]] = {}
    for sale_key, row in sale_rows.items():
        refs = {_normalize_ref(row.get("document_ref"))}
        order_ref = _normalize_ref(row.get("order_ref"))
        if order_ref:
            refs.add(order_ref)
        refs_by_sale[sale_key] = {value for value in refs if value}
    return refs_by_sale


def _build_basis_to_sale_keys(sale_rows: dict[str, dict[str, Any]]) -> dict[str, set[str]]:
    sale_keys_by_basis: dict[str, set[str]] = defaultdict(set)
    for sale_key, refs in _basis_refs_by_sale(sale_rows).items():
        for ref in refs:
            sale_keys_by_basis[_ref_key(ref)].add(sale_key)
    return sale_keys_by_basis


def _fetch_payment_rows(
    onec_engine,
    *,
    sale_rows: dict[str, dict[str, Any]],
    snapshot_date: date,
) -> dict[str, list[dict[str, Any]]]:
    dialect_name = onec_engine.dialect.name
    nolock = _with_nolock(dialect_name=dialect_name)
    payment_ref_expr = _hex_ref_expr("pko._IDRRef", dialect_name=dialect_name)
    basis_ref_expr = _hex_ref_expr("pko._Fld4697_RRRef", dialect_name=dialect_name)
    sale_tref = _tref_literal("0x000000CB", dialect_name=dialect_name)
    order_tref = _tref_literal("0x00000084", dialect_name=dialect_name)
    basis_to_sale_keys = _build_basis_to_sale_keys(sale_rows)
    basis_refs = sorted(basis_to_sale_keys)
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not basis_refs:
        return rows

    with onec_engine.connect() as conn:
        for chunk in _chunked(basis_refs):
            where_clause, params = _build_ref_filter_clause(
                dialect_name=dialect_name,
                refs=chunk,
                column_name="pko._Fld4697_RRRef",
                prefix="payment_basis_ref",
            )
            params["snapshot_end"] = datetime.combine(snapshot_date + timedelta(days=1), time.min)
            stmt = text(f"""
                SELECT
                    {payment_ref_expr} AS document_ref,
                    pko._Number AS document_number,
                    pko._Date_Time AS document_date,
                    CAST(-pko._Fld4688 AS decimal(18, 2)) AS amount,
                    {basis_ref_expr} AS basis_ref,
                    pko._Fld4697_RTRef AS basis_tref
                FROM _Document196 AS pko {nolock}
                WHERE pko._Marked = 0x00
                  AND pko._Posted = 0x01
                  AND pko._Fld4697_RTRef IN ({sale_tref}, {order_tref})
                  AND pko._Date_Time < :snapshot_end
                  AND {where_clause}
            """)
            for row in conn.execute(stmt, params).mappings():
                basis_key = _ref_key(row.get("basis_ref"))
                sale_keys = basis_to_sale_keys.get(basis_key, set())
                for sale_key in sale_keys:
                    rows[sale_key].append(
                        {
                            "document_type": "Приходный кассовый ордер",
                            "document_ref": _clean_string(row.get("document_ref")),
                            "document_number": _clean_string(row.get("document_number")),
                            "document_date": row.get("document_date"),
                            "amount": _to_decimal(row.get("amount")),
                            "basis_ref": _clean_string(row.get("basis_ref")),
                            "basis_kind": (
                                "order"
                                if _normalize_ref(row.get("basis_tref")).lower()
                                == "0x00000084"
                                else "sale"
                            ),
                        }
                    )
    return rows


def _fetch_settlement_rows(
    onec_engine,
    *,
    sale_rows: dict[str, dict[str, Any]],
    snapshot_date: date,
) -> dict[str, list[dict[str, Any]]]:
    dialect_name = onec_engine.dialect.name
    nolock = _with_nolock(dialect_name=dialect_name)
    settlement_ref_expr = _hex_ref_expr("doc._IDRRef", dialect_name=dialect_name)
    basis_ref_expr = _hex_ref_expr("doc._Fld4862_RRRef", dialect_name=dialect_name)
    sale_tref = _tref_literal("0x000000CB", dialect_name=dialect_name)
    order_tref = _tref_literal("0x00000084", dialect_name=dialect_name)
    basis_to_sale_keys = _build_basis_to_sale_keys(sale_rows)
    basis_refs = sorted(basis_to_sale_keys)
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not basis_refs:
        return rows

    with onec_engine.connect() as conn:
        for chunk in _chunked(basis_refs):
            where_clause, params = _build_ref_filter_clause(
                dialect_name=dialect_name,
                refs=chunk,
                column_name="doc._Fld4862_RRRef",
                prefix="settlement_basis_ref",
            )
            params["snapshot_end"] = datetime.combine(snapshot_date + timedelta(days=1), time.min)
            stmt = text(f"""
                SELECT
                    {settlement_ref_expr} AS document_ref,
                    doc._Number AS document_number,
                    doc._Date_Time AS document_date,
                    CAST(-doc._Fld4852 AS decimal(18, 2)) AS amount,
                    {basis_ref_expr} AS basis_ref,
                    doc._Fld4862_RTRef AS basis_tref
                FROM _Document201 AS doc {nolock}
                WHERE doc._Marked = 0x00
                  AND doc._Posted = 0x01
                  AND doc._Fld4862_RTRef IN ({sale_tref}, {order_tref})
                  AND doc._Date_Time < :snapshot_end
                  AND {where_clause}
            """)
            for row in conn.execute(stmt, params).mappings():
                basis_key = _ref_key(row.get("basis_ref"))
                sale_keys = basis_to_sale_keys.get(basis_key, set())
                for sale_key in sale_keys:
                    rows[sale_key].append(
                        {
                            "document_type": "Документ урегулирования",
                            "document_ref": _clean_string(row.get("document_ref")),
                            "document_number": _clean_string(row.get("document_number")),
                            "document_date": row.get("document_date"),
                            "amount": _to_decimal(row.get("amount")),
                            "basis_ref": _clean_string(row.get("basis_ref")),
                            "basis_kind": (
                                "order"
                                if _normalize_ref(row.get("basis_tref")).lower()
                                == "0x00000084"
                                else "sale"
                            ),
                        }
                    )
    return rows
