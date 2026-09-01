from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.core.config import get_settings
from app.infrastructure.db.session import get_application_session_factory
from app.models.procurement_order_formation import (
    ProcurementOrderFormation,
    ProcurementOrderFormationEvent,
    ProcurementOrderFormationLine,
)
from app.services.bitrix_order_formation import resolve_catalog_products_by_xml_ids
from app.services.procurement_order_formation import normalize_guid
from app.services.procurement_order_registry import (
    decimal_value,
    lifecycle_status_for_snapshot,
    normalize_onec_ref,
    upsert_onec_order_snapshot,
)
from scripts.ensure_procurement_bitrix_process import (
    DEFAULT_ENV_FILE,
    DEFAULT_MAPPING_PATH,
    load_env,
)
from scripts.import_onec_supplier_order_to_procurement import load_mapping
from scripts.sync_open_cargo_supplier_orders_to_bitrix import (
    bitrix_webhook,
    fetch_open_supplier_orders,
    fetch_supplier_orders_by_refs,
    parse_contour_keys,
    run_bitrix_import,
    write_json,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULT_PATH = REPO_ROOT / "build/bitrix/procurement_order_registry_sync.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synchronize the unified order registry from 1C")
    parser.add_argument("--contours", default="ordinary,cargo,ved_import")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--source-only",
        action="store_true",
        help="Validate and report the 1C read-only source without opening the application DB.",
    )
    parser.add_argument("--sync-bitrix", action="store_true")
    parser.add_argument("--assigned-by-id", default="130750")
    parser.add_argument("--finance-user-id", default="")
    parser.add_argument("--webhook-url", default="")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--mapping-path", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--result-path", type=Path, default=DEFAULT_RESULT_PATH)
    return parser.parse_args(argv)


def _known_refs() -> list[str]:
    session = get_application_session_factory()()
    try:
        values = [
            str(value)
            for value in session.scalars(
                select(ProcurementOrderFormation.onec_document_ref).where(
                    ProcurementOrderFormation.onec_document_ref.is_not(None),
                    ProcurementOrderFormation.lifecycle_status.not_in(("received", "cancelled")),
                )
            ).all()
            if str(value or "").strip()
        ]
        return [value for value in values if re.fullmatch(r"0x[0-9a-fA-F]{32}", value.strip())]
    finally:
        session.close()


def _catalog_product_ids_for_snapshots(session, snapshots: list[dict[str, Any]]) -> dict[str, str]:
    refs = {
        normalize_onec_ref(line.get("item_ref_hex") or line.get("nomenclature_ref"))
        for snapshot in snapshots
        for line in snapshot.get("lines") or []
        if str(line.get("item_ref_hex") or line.get("nomenclature_ref") or "").strip()
    }
    known = {
        normalize_onec_ref(nomenclature_ref): str(product_id)
        for nomenclature_ref, product_id in session.execute(
            select(
                ProcurementOrderFormationLine.nomenclature_ref,
                ProcurementOrderFormationLine.bitrix_product_id,
            ).where(
                ProcurementOrderFormationLine.nomenclature_ref.in_(refs),
                ProcurementOrderFormationLine.bitrix_product_id.is_not(None),
            )
        ).all()
        if str(product_id or "").strip()
    }
    unresolved_refs = refs - known.keys()
    if not unresolved_refs:
        return known

    resolved = resolve_catalog_products_by_xml_ids(sorted(unresolved_refs))
    known.update(
        {
            raw_ref: product.product_id
            for raw_ref in unresolved_refs
            if (product := resolved.get(normalize_guid(raw_ref))) is not None and product.product_id
        }
    )
    return known


def _persist_snapshots(snapshots: list[dict[str, Any]], *, apply: bool) -> list[dict[str, Any]]:
    session = get_application_session_factory()()
    try:
        catalog_product_ids = _catalog_product_ids_for_snapshots(session, snapshots)
        synced_at = datetime.now(UTC).replace(tzinfo=None)
        rows = []
        for snapshot in snapshots:
            result = upsert_onec_order_snapshot(
                session,
                snapshot,
                synced_at=synced_at,
                catalog_product_ids=catalog_product_ids,
            )
            rows.append(
                {
                    "action": result.action,
                    "order_id": result.order_id,
                    "onec_ref": result.onec_ref,
                    "lifecycle_status": result.lifecycle_status,
                    "conflict": result.conflict,
                    "source_number": str(snapshot.get("number") or ""),
                }
            )
        if apply:
            session.commit()
        else:
            session.rollback()
        return rows
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def _store_bitrix_links(result_rows: list[dict[str, Any]]) -> None:
    by_number = {
        str(row.get("source_number") or "").strip(): row
        for row in result_rows
        if row.get("item_id") and str(row.get("source_number") or "").strip()
    }
    if not by_number:
        return
    session = get_application_session_factory()()
    try:
        orders = list(
            session.scalars(
                select(ProcurementOrderFormation).where(
                    ProcurementOrderFormation.onec_document_number.in_(list(by_number))
                )
            ).all()
        )
        for order in orders:
            row = by_number.get(str(order.onec_document_number or "").strip())
            if not row:
                continue
            order.bitrix_item_id = str(row["item_id"])
            if row.get("entity_type_id"):
                order.bitrix_entity_type_id = int(row["entity_type_id"])
            if row.get("category_id") is not None:
                order.bitrix_category_id = int(row["category_id"])
            if row.get("stage_id"):
                order.bitrix_stage_id = str(row["stage_id"])
            if row.get("item_url"):
                order.bitrix_item_url = str(row["item_url"])
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def _store_missing_conflicts(refs: list[str]) -> None:
    if not refs:
        return
    session = get_application_session_factory()()
    try:
        orders = list(
            session.scalars(
                select(ProcurementOrderFormation).where(
                    func.lower(ProcurementOrderFormation.onec_document_ref).in_(refs)
                )
            ).all()
        )
        synced_at = datetime.now(UTC).replace(tzinfo=None)
        for order in orders:
            message = "Документ не найден по сохранённому GUID 1С; статус не изменён"
            order.last_onec_sync_at = synced_at
            if order.sync_conflict == message:
                continue
            order.sync_conflict = message
            session.add(
                ProcurementOrderFormationEvent(
                    order_id=order.id,
                    entity_type="order",
                    entity_id=str(order.id),
                    event_type="onec_order_reconciliation_required",
                    actor="system:onec-procurement-registry",
                    before={"lifecycle_status": order.lifecycle_status},
                    after={"lifecycle_status": order.lifecycle_status, "sync_conflict": message},
                    payload={"onec_ref": order.onec_document_ref},
                )
            )
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    if not settings.onec_database_url:
        raise RuntimeError("ONEC_DATABASE_URL is not configured")
    contours = parse_contour_keys(args.contours)
    known_refs = [] if args.source_only else _known_refs()
    open_orders = fetch_open_supplier_orders(
        settings.onec_database_url,
        limit=args.limit,
        date_from="",
        date_to="",
        contours=contours,
        filter_contours_in_sql=True,
        fail_on_query_limit=True,
    )
    refreshed = fetch_supplier_orders_by_refs(
        settings.onec_database_url,
        refs=known_refs,
        contours=contours,
    )
    snapshots_by_ref = {
        str(item.get("onec_ref") or "").strip().lower(): item
        for item in [*open_orders, *refreshed]
        if str(item.get("onec_ref") or "").strip()
    }
    snapshots = list(snapshots_by_ref.values())
    missing_refs = sorted(set(value.lower() for value in known_refs) - set(snapshots_by_ref))
    for snapshot in snapshots:
        snapshot["lifecycle_status"] = lifecycle_status_for_snapshot(snapshot)
        snapshot["received_qty"] = max(
            decimal_value(snapshot.get("ordered_qty")) - decimal_value(snapshot.get("open_qty")),
            0,
        )
        contour_key = str(snapshot.get("procurement_contour_key") or "ordinary")
        lifecycle = str(snapshot["lifecycle_status"])
        if lifecycle == "received":
            snapshot["procurement_stage_key"] = "closed"
        elif lifecycle == "partially_received":
            snapshot["procurement_stage_key"] = "receiving"
        elif lifecycle == "cancelled":
            snapshot["procurement_stage_key"] = {
                "ordinary": "cancelled",
                "cargo": "exception",
                "ved_import": "blocked",
            }.get(contour_key, "supplier_order")
        elif lifecycle == "in_transit":
            snapshot["procurement_stage_key"] = {
                "ordinary": "waiting_delivery",
                "cargo": "in_transit",
                "ved_import": "logistics_customs",
            }.get(contour_key, "supplier_order")
    registry_rows = (
        [] if args.source_only else _persist_snapshots(snapshots, apply=bool(args.apply))
    )
    lifecycle_by_ref = {
        str(row.get("onec_ref") or "").lower(): str(row.get("lifecycle_status") or "")
        for row in registry_rows
        if row.get("lifecycle_status")
    }
    for snapshot in snapshots:
        resolved_lifecycle = lifecycle_by_ref.get(str(snapshot.get("onec_ref") or "").lower())
        if resolved_lifecycle:
            snapshot["lifecycle_status"] = resolved_lifecycle
            contour_key = str(snapshot.get("procurement_contour_key") or "ordinary")
            if resolved_lifecycle == "received":
                snapshot["procurement_stage_key"] = "closed"
            elif resolved_lifecycle == "partially_received":
                snapshot["procurement_stage_key"] = "receiving"
            elif resolved_lifecycle == "cancelled":
                snapshot["procurement_stage_key"] = {
                    "ordinary": "cancelled",
                    "cargo": "exception",
                    "ved_import": "blocked",
                }.get(contour_key, "supplier_order")
            elif resolved_lifecycle == "in_transit":
                snapshot["procurement_stage_key"] = {
                    "ordinary": "waiting_delivery",
                    "cargo": "in_transit",
                    "ved_import": "logistics_customs",
                }.get(contour_key, "supplier_order")
    if args.apply and not args.source_only:
        _store_missing_conflicts(missing_refs)

    bitrix_rows: list[dict[str, Any]] = []
    if args.sync_bitrix and not args.source_only:
        webhook = bitrix_webhook(args, load_env(args.env_file))
        if not webhook:
            raise RuntimeError("PROCUREMENT_BITRIX_WEBHOOK_URL is not configured")
        bitrix_rows = run_bitrix_import(
            snapshots,
            webhook_base=webhook,
            mapping=load_mapping(args.mapping_path),
            apply=bool(args.apply),
            assigned_by_id=str(args.assigned_by_id),
            finance_user_id=str(args.finance_user_id),
        )
        if args.apply:
            _store_bitrix_links(bitrix_rows)

    return {
        "mode": "apply" if args.apply else "dry-run",
        "summary": {
            "snapshots": len(snapshots),
            "open_orders": len(open_orders),
            "known_ref_refreshes": len(refreshed),
            "missing_known_refs": len(missing_refs),
            "registry_actions": dict(Counter(row["action"] for row in registry_rows)),
            "bitrix_actions": dict(
                Counter(
                    str(row.get("would_action") or row.get("action") or "") for row in bitrix_rows
                )
            ),
            "bitrix_blocked": sum(
                1 for row in bitrix_rows if str(row.get("action") or "") == "blocked"
            ),
        },
        "missing_refs": missing_refs,
        "registry_rows": registry_rows,
        "bitrix_rows": bitrix_rows,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run(args)
    write_json(args.result_path, result)
    print(
        json.dumps(
            {"mode": result["mode"], "summary": result["summary"]}, ensure_ascii=False, indent=2
        )
    )
    conflicts = int(result["summary"]["registry_actions"].get("conflict", 0))
    blocked = int(result["summary"].get("bitrix_blocked", 0))
    missing = int(result["summary"].get("missing_known_refs", 0))
    return 2 if conflicts or blocked or missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
