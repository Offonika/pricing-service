"""Read-only, batched UT price evidence. Storage mappings: procurement-price-sources.json."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import bindparam, text

from app.services.procurement_receipt_evidence import receipt_reference_list

EMPTY_REF = "0x" + "0" * 32


def valid_ref(value: Any) -> str | None:
    value = str(value or "").lower()
    return value if re.fullmatch(r"0x[0-9a-f]{32}", value) else None


def _hex(field: str) -> str:
    return f"LOWER(CONVERT(varchar(34), {field}, 1))"


def read_price_sources(engine, items: list[dict[str, Any]], *, as_of: date) -> dict[str, list]:
    """No fuzzy SKU matching, no price-type default currency, no dirty accounting reads."""
    result: dict[str, list] = {
        key: [] for key in ("products", "costs", "quotes", "orders", "receipts", "allocations")
    }
    until = datetime.combine(as_of + timedelta(days=1), time.min)
    codes = sorted({str(item.get("nomenclature_code") or "").strip() for item in items} - {""})
    refs = sorted({ref for item in items if (ref := valid_ref(item.get("nomenclature_ref")))})
    with engine.connect() as connection:
        # Exact code supports draft UUIDs; binary refs are independently checked by the builder.
        for start in range(0, max(len(codes), len(refs)), 200):
            batch_codes = codes[start : start + 200]
            batch_refs = refs[start : start + 200]
            ref_filter = (
                f"OR p._IDRRef IN ({receipt_reference_list(batch_refs)})" if batch_refs else ""
            )
            query = text(f"""
                SELECT {_hex('p._IDRRef')} AS item_ref, RTRIM(p._Code) AS code,
                    {_hex('p._Fld844RRef')} AS unit_ref, RTRIM(u._Description) AS unit_name,
                    p._Fld842 AS has_characteristics
                FROM dbo._Reference62 p
                LEFT JOIN dbo._Reference41 u ON u._IDRRef = p._Fld844RRef
                WHERE p._Marked = 0x00 AND (p._Code IN :codes {ref_filter})
            """).bindparams(bindparam("codes", expanding=True))
            result["products"].extend(
                dict(row) for row in connection.execute(query, {"codes": batch_codes}).mappings()
            )
        product_refs = sorted({row["item_ref"] for row in result["products"]})
        supplier_refs = sorted(
            {ref for item in items if (ref := valid_ref(item.get("supplier_ref")))}
        )
        order_refs = sorted({ref for item in items if (ref := valid_ref(item.get("order_ref")))})
        for start in range(0, len(product_refs), 200):
            sku_filter = receipt_reference_list(product_refs[start : start + 200])
            queries = {
                "costs": f"""
                    WITH costs AS (
                        SELECT {_hex('r._Fld6961RRef')} AS item_ref,
                            {_hex('r._Fld6962RRef')} AS characteristic_ref,
                            {_hex('r._Fld6965RRef')} AS unit_ref, RTRIM(u._Description) AS unit_name,
                            RTRIM(c._Code) AS currency, r._Fld6964 AS value, r._Period AS at,
                            {_hex('d._IDRRef')} AS document_ref, RTRIM(d._Number) AS document_number,
                            d._Date_Time AS document_at,
                            CASE WHEN d._Fld8791_RTRef = 0x000000C2 THEN {_hex('d._Fld8791_RRRef')} END AS receipt_ref,
                            DENSE_RANK() OVER (PARTITION BY r._Fld6961RRef, r._Fld6962RRef,
                                r._Fld6965RRef, r._Fld6963RRef, d._Fld8791_RTRef, d._Fld8791_RRRef
                                ORDER BY r._Period DESC) AS rank_no
                        FROM dbo._InfoRg6959 r
                        JOIN dbo._Document224 d ON d._IDRRef = r._RecorderRRef AND r._RecorderTRef = 0x000000E0
                        LEFT JOIN dbo._Reference20 c ON c._IDRRef = r._Fld6963RRef
                        LEFT JOIN dbo._Reference41 u ON u._IDRRef = r._Fld6965RRef
                        WHERE r._Fld6961RRef IN ({sku_filter}) AND r._Active = 0x01
                            AND r._Fld6960RRef = 0x8c7400241d5a8bc311dff8957b4abb78
                            AND d._Posted = 0x01 AND d._Marked = 0x00
                            AND r._Period < :until AND d._Date_Time < :until
                    ) SELECT * FROM costs WHERE rank_no = 1
                """,
            }
            if supplier_refs:
                queries["quotes"] = f"""
                    WITH quotes AS (
                        SELECT {_hex('r._Fld6974RRef')} AS item_ref, {_hex('t._OwnerIDRRef')} AS supplier_ref,
                            {_hex('r._Fld6975RRef')} AS characteristic_ref, {_hex('r._Fld6976RRef')} AS unit_ref,
                            RTRIM(u._Description) AS unit_name, RTRIM(c._Code) AS currency,
                            r._Fld6978 AS value, r._Period AS at,
                            {_hex('r._RecorderRRef')} AS document_ref, RTRIM(COALESCE(d._Number, receipt._Number, supplier_order._Number)) AS document_number,
                            CONVERT(varchar(10), r._RecorderTRef, 1) AS document_type,
                            DENSE_RANK() OVER (PARTITION BY r._Fld6974RRef, t._OwnerIDRRef,
                                r._Fld6975RRef, r._Fld6976RRef, r._Fld6977RRef, r._Fld6973RRef
                                ORDER BY r._Period DESC) AS rank_no
                        FROM dbo._InfoRg6972 r
                        JOIN dbo._Reference88 t ON t._IDRRef = r._Fld6973RRef
                        LEFT JOIN dbo._Reference20 c ON c._IDRRef = r._Fld6977RRef
                        LEFT JOIN dbo._Reference41 u ON u._IDRRef = r._Fld6976RRef
                        LEFT JOIN dbo._Document225 d ON d._IDRRef = r._RecorderRRef AND r._RecorderTRef = 0x000000E1
                        LEFT JOIN dbo._Document194 receipt ON receipt._IDRRef = r._RecorderRRef AND r._RecorderTRef = 0x000000C2
                        LEFT JOIN dbo._Document133 supplier_order ON supplier_order._IDRRef = r._RecorderRRef AND r._RecorderTRef = 0x00000085
                        WHERE r._Fld6974RRef IN ({sku_filter}) AND r._Active = 0x01
                            AND t._OwnerIDRRef IN ({receipt_reference_list(supplier_refs)}) AND r._Period < :until
                    ) SELECT * FROM quotes WHERE rank_no = 1
                """
            for key, query in queries.items():
                result[key].extend(
                    dict(row)
                    for row in connection.execute(text(query), {"until": until}).mappings()
                )
            for order_start in range(0, len(order_refs), 200):
                order_filter = receipt_reference_list(order_refs[order_start : order_start + 200])
                result["orders"].extend(
                    dict(row)
                    for row in connection.execute(
                        text(f"""
                    SELECT {_hex('d._IDRRef')} AS order_ref, {_hex('v._Fld2523RRef')} AS item_ref,
                        v._LineNo2516 AS line_number, v._Fld2529 AS value, RTRIM(c._Code) AS currency,
                        {_hex('d._Fld2498RRef')} AS supplier_ref,
                        {_hex('v._Fld2528RRef')} AS characteristic_ref,
                        {_hex('v._Fld2517RRef')} AS unit_ref, RTRIM(u._Description) AS unit_name,
                        d._Fld2501 AS exchange_rate, d._Fld2500 AS exchange_multiplicity,
                        RTRIM(sc._Code) AS settlement_currency,
                        {_hex('d._IDRRef')} AS document_ref, RTRIM(d._Number) AS document_number,
                        d._Date_Time AS at
                    FROM dbo._Document133 d
                    JOIN dbo._Document133_VT2515 v ON v._Document133_IDRRef = d._IDRRef
                    LEFT JOIN dbo._Reference20 c ON c._IDRRef = d._Fld2490RRef
                    LEFT JOIN dbo._Reference37 contract ON contract._IDRRef = d._Fld2494RRef
                    LEFT JOIN dbo._Reference20 sc ON sc._IDRRef = contract._Fld498RRef
                    LEFT JOIN dbo._Reference41 u ON u._IDRRef = v._Fld2517RRef
                    WHERE d._IDRRef IN ({order_filter}) AND v._Fld2523RRef IN ({sku_filter})
                        AND d._Posted = 0x01 AND d._Marked = 0x00 AND d._Date_Time < :until
                """),
                        {"until": until},
                    ).mappings()
                )
                result["receipts"].extend(
                    dict(row)
                    for row in connection.execute(
                        text(f"""
                    SELECT DISTINCT {_hex('m._Fld7149RRef')} AS order_ref,
                        {_hex('m._Fld7151RRef')} AS item_ref, {_hex('d._IDRRef')} AS receipt_ref,
                        RTRIM(d._Number) AS document_number, d._Date_Time AS at,
                        {_hex('v._Fld4520RRef')} AS characteristic_ref, {_hex('v._Fld4511RRef')} AS unit_ref,
                        RTRIM(u._Description) AS unit_name, v._Fld4515 AS value,
                        RTRIM(c._Code) AS currency, RTRIM(sc._Code) AS settlement_currency,
                        d._Fld4485 AS exchange_rate, d._Fld4484 AS exchange_multiplicity,
                        {_hex('d._IDRRef')} AS document_ref, {_hex('d._Fld4483RRef')} AS supplier_ref
                    FROM dbo._AccumRg7147 m
                    JOIN dbo._Document194 d ON d._IDRRef = m._RecorderRRef AND m._RecorderTRef = 0x000000C2
                    JOIN dbo._Document194_VT4507 v ON v._Document194_IDRRef = d._IDRRef AND v._Fld4509RRef = m._Fld7151RRef
                        AND v._Fld4525RRef = m._Fld7149RRef
                    LEFT JOIN dbo._Reference41 u ON u._IDRRef = v._Fld4511RRef
                    LEFT JOIN dbo._Reference20 c ON c._IDRRef = d._Fld4477RRef
                    LEFT JOIN dbo._Reference37 contract ON contract._IDRRef = d._Fld4480RRef
                    LEFT JOIN dbo._Reference20 sc ON sc._IDRRef = contract._Fld498RRef
                    WHERE m._Fld7149RRef IN ({order_filter}) AND m._Fld7151RRef IN ({sku_filter})
                        AND m._Active = 0x01 AND m._RecordKind = 1 AND m._Fld7156 > 0
                        AND d._Posted = 0x01 AND d._Marked = 0x00 AND d._Date_Time < :until
                """),
                        {"until": until},
                    ).mappings()
                )
        receipt_refs = sorted({row["receipt_ref"] for row in result["receipts"]})
        for start in range(0, len(receipt_refs), 200):
            receipt_filter = receipt_reference_list(receipt_refs[start : start + 200])
            for doc, table, sku, unit, characteristic, basis, final in (
                ("193", "4450", "4452", "4453", "4459", "4461", "8445"),
                ("9197", "9225", "9227", "9228", "9234", "9236", "9223"),
            ):
                result["allocations"].extend(
                    dict(row)
                    for row in connection.execute(
                        text(f"""
                    SELECT {_hex(f'v._Fld{sku}RRef')} AS item_ref, {_hex(f'v._Fld{unit}RRef')} AS unit_ref,
                        {_hex(f'v._Fld{characteristic}RRef')} AS characteristic_ref,
                        {_hex(f'v._Fld{basis}_RRRef')} AS receipt_ref,
                        {_hex('d._IDRRef')} AS document_ref, RTRIM(d._Number) AS document_number,
                        d._Date_Time AS at, d._Fld{final} AS final_allocation,
                        'Document{doc}' AS document_type
                    FROM dbo._Document{doc}_VT{table} v
                    JOIN dbo._Document{doc} d ON d._IDRRef = v._Document{doc}_IDRRef
                    WHERE v._Fld{basis}_RTRef = 0x000000C2 AND v._Fld{basis}_RRRef IN ({receipt_filter})
                        AND d._Posted = 0x01 AND d._Marked = 0x00 AND d._Date_Time < :until
                """),
                        {"until": until},
                    ).mappings()
                )
    return result
