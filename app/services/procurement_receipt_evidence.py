"""Exact order/SKU receipt evidence from UT supplier-order register movements."""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from app.core.config import get_settings
from app.infrastructure.db.engines import build_onec_engine
from app.services.procurement_supply_scenarios import decimal, facts_hash


def receipt_reference_list(values: list[str]) -> str:
    if not values or any(re.fullmatch(r"0x[0-9a-f]{32}", value) is None for value in values):
        raise ValueError("Expected canonical 16-byte 1C GUIDs")
    return ", ".join(values)


def receipt_review_hash(
    evidence: dict[str, Any], *, ordered_quantity: Any, open_quantity: Any
) -> str:
    return facts_hash(
        {
            "evidence": evidence,
            "open_quantity": str(decimal(open_quantity).normalize()),
            "ordered_quantity": str(decimal(ordered_quantity).normalize()),
        }
    )


def load_receipt_evidence(database_url: str, snapshots: list[dict[str, Any]]) -> None:
    refs = sorted({str(s.get("onec_ref") or "").lower() for s in snapshots} - {""})
    facts: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    statement = text("""
        SELECT 'receipt' AS source_kind, LOWER(CONVERT(varchar(34), movement._Fld7149RRef, 1)) AS order_ref,
            LOWER(CONVERT(varchar(34), movement._Fld7151RRef, 1)) AS item_ref,
            LOWER(CONVERT(varchar(34), receipt._IDRRef, 1)) AS receipt_ref,
            RTRIM(receipt._Number) AS receipt_number, receipt._Date_Time AS receipt_at,
            SUM(CASE WHEN movement._RecordKind = 1 THEN movement._Fld7156
                     ELSE -movement._Fld7156 END) AS quantity
        FROM dbo._AccumRg7147 AS movement
        JOIN dbo._Document194 AS receipt ON receipt._IDRRef = movement._RecorderRRef
        WHERE movement._Active = 0x01 AND receipt._Posted = 0x01 AND receipt._Marked = 0x00
            AND movement._RecorderTRef = 0x000000C2
            AND movement._Fld7149RRef IN :refs
            AND movement._Fld7151RRef IN :item_refs
        GROUP BY movement._Fld7149RRef, movement._Fld7151RRef,
            receipt._IDRRef, receipt._Number, receipt._Date_Time
    """)
    engine = None
    try:
        settings = get_settings()
        engine = build_onec_engine(
            database_url,
            query_timeout_seconds=settings.onec_query_timeout_seconds,
            login_timeout_seconds=settings.onec_login_timeout_seconds,
        )
        with engine.connect() as connection:
            for start in range(0, len(refs), 300):
                batch = refs[start : start + 300]
                item_refs = sorted(
                    {
                        str(line.get("item_ref_hex") or "").lower()
                        for snapshot in snapshots
                        if str(snapshot.get("onec_ref") or "").lower() in batch
                        for line in snapshot.get("lines") or []
                    }
                    - {""}
                )
                for item_start in range(0, len(item_refs), 300):
                    # SQL Server's binary keys use validated hex literals. The live TDS
                    # connection rejects these RPC conversions; literal seeks are read back.
                    query = statement.text
                    for key, values in (
                        ("refs", batch),
                        ("item_refs", item_refs[item_start : item_start + 300]),
                    ):
                        query = query.replace(
                            f"IN :{key}", f"IN ({receipt_reference_list(values)})"
                        )
                    # The storage IDs and metadata names are verified against Config/DBNames.
                    branches = [query]
                    for kind, document, type_ref in (
                        ("return", "110", "0000006E"),
                        ("adjustment", "163", "000000A3"),
                    ):
                        branch = (
                            query.replace("'receipt' AS source_kind", f"'{kind}' AS source_kind")
                            .replace("dbo._Document194", f"dbo._Document{document}")
                            .replace("0x000000C2", f"0x{type_ref}")
                        )
                        if kind == "return":
                            # A supplier return adds back an obligation (RecordKind=0).
                            # Report returned units as positive, independently of receipts.
                            branch = branch.replace("_RecordKind = 1", "_RecordKind = 0")
                        branches.append(branch)
                    for row in connection.execute(text(" UNION ALL ".join(branches))).mappings():
                        facts[row["order_ref"]][row.get("source_kind", "receipt")].append(
                            {
                                **dict(row),
                                "quantity": str(row["quantity"]),
                                "receipt_at": row["receipt_at"].isoformat(),
                            }
                        )
    except Exception as exc:
        # Preserve existing confirmed values at the registry boundary; no guessed zeroes.
        for snapshot in snapshots:
            snapshot["receipt_evidence"] = {
                "status": "unavailable",
                "error_type": type(exc).__name__,
            }
        return
    finally:
        if engine is not None:
            engine.dispose()
    for snapshot in snapshots:
        evidence = facts.get(str(snapshot.get("onec_ref") or "").lower(), {})
        attach_receipt_evidence(
            snapshot,
            evidence.get("receipt", []),
            returns=evidence.get("return", []),
            adjustments=evidence.get("adjustment", []),
        )


def attach_receipt_evidence(
    snapshot: dict[str, Any],
    movements: list[dict[str, Any]],
    *,
    returns: list[dict[str, Any]] | None = None,
    adjustments: list[dict[str, Any]] | None = None,
) -> None:
    quantities: dict[str, Decimal] = defaultdict(Decimal)
    for movement in movements:
        quantities[str(movement["item_ref"]).lower()] += decimal(movement["quantity"])
    ordered: dict[str, Decimal] = defaultdict(Decimal)
    counts: dict[str, int] = defaultdict(int)
    for line in snapshot.get("lines") or []:
        ref = str(line.get("item_ref_hex") or "").lower()
        ordered[ref] += decimal(line.get("quantity"))
        counts[ref] += 1
    complete = bool(ordered) and all(ref and quantities[ref] >= qty for ref, qty in ordered.items())
    for line in snapshot.get("lines") or []:
        ref = str(line.get("item_ref_hex") or "").lower()
        # Do not duplicate a SKU aggregate across repeated document lines.
        line["received_quantity"] = str(quantities[ref]) if ref and counts[ref] == 1 else None
        line["receipt_source_status"] = "exact" if ref and counts[ref] == 1 else "ambiguous_line"
    total = sum(quantities.values(), Decimal(0))
    attributable = bool(ordered) and all(ordered)
    snapshot["received_qty"] = str(total) if attributable else None
    snapshot["receipt_evidence"] = {
        "status": "exact" if attributable else "unconfirmed",
        "source": "onec:_AccumRg7147:posted_Document194",
        "received_quantity": str(total) if attributable else None,
        "fulfillment_complete": complete,
        "movements": sorted(movements, key=lambda row: (row["receipt_ref"], row["item_ref"])),
        "return_quantity": (
            str(sum((decimal(row["quantity"]) for row in returns), Decimal(0))) if returns else None
        ),
        "return_source_status": (
            "exact" if returns else "no_linked_facts" if returns is not None else "not_attributed"
        ),
        "return_movements": _obligation_movements(returns or [], kind="return"),
        # Positive adjustment adds an obligation, negative adjustment reduces it.
        "adjustment_quantity": (
            str(-sum((decimal(row["quantity"]) for row in adjustments), Decimal(0)))
            if adjustments
            else None
        ),
        "adjustment_source_status": (
            "exact"
            if adjustments
            else "no_linked_facts" if adjustments is not None else "not_attributed"
        ),
        "adjustment_movements": _obligation_movements(adjustments or [], kind="adjustment"),
    }


def _obligation_movements(rows: list[dict[str, Any]], *, kind: str) -> list[dict[str, Any]]:
    return [
        {
            "document_ref": row["receipt_ref"],
            "document_number": row.get("receipt_number"),
            "document_at": row.get("receipt_at"),
            "item_ref": row["item_ref"],
            "quantity": str(decimal(row["quantity"]) * (-1 if kind == "adjustment" else 1)),
            "kind": kind,
        }
        for row in sorted(rows, key=lambda row: (row["receipt_ref"], row["item_ref"]))
    ]
