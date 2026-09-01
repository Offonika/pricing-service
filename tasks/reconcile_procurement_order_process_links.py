from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.infrastructure.db.session import session_scope
from app.services.procurement_order_process_link import (
    ProcurementProcessCardSnapshot,
    reconcile_procurement_order_process_links,
)
from app.services.procurement_order_registry import date_value
from scripts.ensure_procurement_bitrix_process import (
    DEFAULT_ENV_FILE,
    DEFAULT_MAPPING_PATH,
    load_env,
)
from scripts.import_onec_supplier_order_to_procurement import (
    BitrixRestApi,
    crm_item_rest_field_name,
    field_name,
    load_mapping,
)
from scripts.sync_open_cargo_supplier_orders_to_bitrix import (
    list_existing_procurement_items,
    list_procurement_stage_names,
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconcile pricing-service orders with the canonical Bitrix procurement process."
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--mapping-path", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--webhook-url")
    parser.add_argument("--apply", action="store_true", help="Persist DB links and audit events.")
    return parser.parse_args(argv)


def snapshots_from_items(
    items: list[dict[str, Any]], mapping: dict[str, Any], stage_names: dict[str, str]
) -> list[ProcurementProcessCardSnapshot]:
    entity_type_id = int((mapping.get("process") or {}).get("entity_type_id") or 0)
    ref_field = crm_item_rest_field_name(field_name(mapping, "onec_document_ref"))
    number_field = crm_item_rest_field_name(field_name(mapping, "onec_source_number"))
    date_field = crm_item_rest_field_name(field_name(mapping, "onec_source_date"))
    snapshots: list[ProcurementProcessCardSnapshot] = []
    for item in items:
        item_id = _clean(item.get("id"))
        onec_ref = _clean(item.get(ref_field))
        if not item_id or not onec_ref:
            continue
        category_id = int(item["categoryId"]) if item.get("categoryId") else None
        stage_id = _clean(item.get("stageId"))
        snapshots.append(
            ProcurementProcessCardSnapshot(
                item_id=item_id,
                onec_ref=onec_ref,
                onec_number=_clean(item.get(number_field)),
                onec_date=date_value(item.get(date_field)),
                category_id=category_id,
                stage_id=stage_id,
                stage_name=stage_names.get(stage_id, ""),
                entity_type_id=entity_type_id,
            )
        )
    return snapshots


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env = load_env(args.env_file)
    webhook = _clean(args.webhook_url) or _clean(
        env.get("PROCUREMENT_BITRIX_WEBHOOK_URL")
        or env.get("BITRIX_BOX_WEBHOOK_BASE")
        or env.get("BITRIX24_BOX_WEBHOOK_URL")
    )
    database_url = _clean(env.get("DATABASE_URL"))
    if not webhook:
        raise SystemExit(f"Bitrix webhook is not configured in {args.env_file}")
    if not database_url:
        raise SystemExit(f"DATABASE_URL is not configured in {args.env_file}")

    mapping = load_mapping(args.mapping_path)
    entity_type_id = int((mapping.get("process") or {}).get("entity_type_id") or 0)
    if entity_type_id != 1056:
        raise SystemExit(f"Expected canonical entityTypeId 1056, got {entity_type_id}")
    api = BitrixRestApi(webhook)
    items = list_existing_procurement_items(api, mapping)
    stage_names = list_procurement_stage_names(api, mapping)
    snapshots = snapshots_from_items(items, mapping, stage_names)
    with session_scope(read_only=not args.apply, database_url=database_url) as db:
        summary = reconcile_procurement_order_process_links(db, snapshots)

    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry-run",
                "entity_type_id": entity_type_id,
                "bitrix_cards": len(snapshots),
                "summary": summary,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if summary["broken"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
