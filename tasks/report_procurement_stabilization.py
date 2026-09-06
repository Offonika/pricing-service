"""Read-only receipt comparison and addressed procurement reconciliation list."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from sqlalchemy import text

from app.infrastructure.db.engines import build_engine, build_onec_engine
from app.services.procurement_manual_removal_recovery import plan_manual_removal_recovery
from app.services.procurement_order_metrics import _normalize_currency
from app.services.procurement_order_registry import lifecycle_status_for_snapshot
from app.services.procurement_receipt_evidence import (
    load_receipt_evidence,
    receipt_reference_list,
    receipt_review_hash,
)
from app.services.procurement_supply_scenarios import facts_hash, supply_date
from scripts.sync_open_cargo_supplier_orders_to_bitrix import (
    fetch_supplier_orders_by_refs,
    parse_contour_keys,
)


def compare_snapshots(
    previous: list[dict[str, Any]], snapshots: list[dict[str, Any]], *, now: datetime
) -> dict[str, Any]:
    by_ref = {str(item["onec_ref"]).lower(): item for item in snapshots}
    rows = []
    for order in previous:
        snapshot = by_ref.get(str(order["onec_document_ref"]).lower())
        reasons = []
        if snapshot is None:
            rows.append(
                {
                    **order,
                    "proposed_status": order["lifecycle_status"],
                    "reasons": ["source_missing"],
                    "review_required": True,
                }
            )
            continue
        eta = supply_date(snapshot.get("expected_receipt_date"))
        if eta is None:
            reasons.append("missing_eta")
        elif eta < now.date():
            reasons.append("overdue_eta")
        ordered_at = supply_date(snapshot.get("order_date") or snapshot.get("date"))
        if ordered_at and (now.date() - ordered_at).days > 90:
            reasons.append("aged_order")
        if order.get("bitrix_product_rows_sync_state") == "error":
            reasons.append("product_mapping")
        proposed = lifecycle_status_for_snapshot(
            snapshot, previous_status=order["lifecycle_status"]
        )
        evidence = snapshot.get("receipt_evidence") or {}
        if proposed == "reconciliation_required" or evidence.get("status") != "exact":
            reasons.append("receipt_reconciliation")
        rows.append(
            {
                **order,
                "proposed_status": proposed,
                "reasons": reasons,
                "status_changed": proposed != order["lifecycle_status"],
                "received_quantity_after": evidence.get("received_quantity"),
                "receipt_evidence": evidence,
                "receipt_review_hash": receipt_review_hash(
                    evidence,
                    ordered_quantity=snapshot.get("ordered_qty"),
                    open_quantity=snapshot.get("open_qty"),
                ),
                "snapshot_hash": facts_hash(snapshot),
                "review_required": bool(reasons) or proposed != order["lifecycle_status"],
            }
        )
    return {
        "mode": "compare_only",
        "generated_at": now.isoformat(),
        "input_hash": facts_hash({"previous": previous, "snapshots": snapshots}),
        "order_count": len(rows),
        "status_change_count": sum(bool(row.get("status_changed")) for row in rows),
        "by_reason": dict(Counter(reason for row in rows for reason in row["reasons"])),
        "orders": rows,
        "source_snapshots": snapshots,
    }


def contract_currency_comparison(
    database_url: str, projects: list[dict[str, Any]], lines: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    refs = sorted({str(project.get("contract_ref") or "").lower() for project in projects} - {""})
    currencies = {}
    error_type = None
    if refs:
        engine = build_onec_engine(
            database_url, query_timeout_seconds=300, login_timeout_seconds=30
        )
        try:
            with engine.connect() as connection:
                query = text(f"""
                    SELECT LOWER(CONVERT(varchar(34), contract._IDRRef, 1)) AS contract_ref,
                        RTRIM(currency._Code) AS currency
                    FROM dbo._Reference37 contract
                    JOIN dbo._Reference20 currency ON currency._IDRRef = contract._Fld498RRef
                    WHERE contract._Marked = 0x00 AND contract._IDRRef IN ({receipt_reference_list(refs)})
                """)
                currencies = {
                    row["contract_ref"]: _normalize_currency(row["currency"])
                    for row in connection.execute(query).mappings()
                }
        except Exception as exc:
            error_type = type(exc).__name__
        finally:
            engine.dispose()
    return [
        {
            **project,
            "contract_currency": currencies.get(str(project.get("contract_ref") or "").lower()),
            "source_error_type": error_type,
            "price_history_mismatch_line_ids": [
                line["id"]
                for line in lines
                if line["order_id"] == project["id"]
                and not line.get("removed")
                and (line.get("payload") or {}).get("price_change_status") == "currency_mismatch"
            ],
            "action": (
                "preserve_manual_currency"
                if project.get("manual_currency")
                else "compare_with_contract"
            ),
        }
        for project in projects
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = dotenv_values(args.env_file)
    engine = build_engine(config["DATABASE_URL"])
    try:
        with engine.connect() as connection, connection.begin():
            if engine.dialect.name == "postgresql":
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(text("SET LOCAL statement_timeout = '30s'"))
            previous = [dict(row) for row in connection.execute(text("""
                SELECT id, version, onec_document_ref, onec_document_number, lifecycle_status,
                    onec_received_quantity, bitrix_product_rows_sync_state,
                    bitrix_product_rows_error
                FROM procurement_order_formation
                WHERE origin = 'onec_import' AND onec_document_ref IS NOT NULL
                ORDER BY id
            """)).mappings()]
            lines = [dict(row) for row in connection.execute(text("""
                SELECT line.id, line.order_id, line.version, line.payload, line.removed
                FROM procurement_order_formation_line line
                JOIN procurement_order_formation orders ON orders.id = line.order_id
                WHERE orders.origin = 'generated' AND orders.status = 'draft'
            """)).mappings()]
            projects = [dict(row) for row in connection.execute(text("""
                SELECT id, version, supplier_name, contract_ref, currency,
                    payload::jsonb->>'manual_currency' AS manual_currency
                FROM procurement_order_formation
                WHERE origin = 'generated' AND status = 'draft'
            """)).mappings()]
            events = [dict(row) for row in connection.execute(text("""
                SELECT event.id, event.order_id, event.entity_id, event.entity_type,
                    event.event_type, event.actor, event.created_at, event.payload,
                    jsonb_build_object('lines', COALESCE((
                        SELECT jsonb_agg(item) FROM jsonb_array_elements(event.before::jsonb->'lines') item
                        WHERE item->>'id' = event.entity_id
                    ), '[]'::jsonb)) AS before,
                    jsonb_build_object('lines', COALESCE((
                        SELECT jsonb_agg(item) FROM jsonb_array_elements(event.after::jsonb->'lines') item
                        WHERE item->>'id' = event.entity_id
                    ), '[]'::jsonb)) AS after
                FROM procurement_order_formation_event event
                WHERE event.entity_type = 'order_line'
                ORDER BY event.created_at, event.id
            """)).mappings()]
    finally:
        engine.dispose()
    snapshots = fetch_supplier_orders_by_refs(
        config["ONEC_DATABASE_URL"],
        refs=[row["onec_document_ref"] for row in previous],
        contours=parse_contour_keys("ordinary,cargo,ved_import"),
    )
    load_receipt_evidence(config["ONEC_DATABASE_URL"], snapshots)
    report = compare_snapshots(previous, snapshots, now=datetime.now(UTC))
    report["manual_removal_recovery"] = plan_manual_removal_recovery(lines, events)
    report["price_currency_comparison"] = contract_currency_comparison(
        config["ONEC_DATABASE_URL"], projects, lines
    )
    report["manual_event_counts"] = dict(Counter(event["event_type"] for event in events))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("mode", "order_count", "status_change_count", "by_reason")
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
