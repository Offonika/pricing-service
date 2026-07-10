#!/usr/bin/env python3
"""Dry-run/apply display auto-order recommendations into Bitrix order-formation queues."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.procurement_supplier_crm import clean_string  # noqa: E402
from scripts.ensure_procurement_bitrix_process import (  # noqa: E402
    DEFAULT_ENV_FILE,
    DEFAULT_MAPPING_PATH,
    load_env,
)
from scripts.import_onec_supplier_order_to_procurement import (  # noqa: E402
    BitrixRestApi,
    bitrix_values_match,
    changed_rest_fields,
    crm_item_rest_field_name,
    crm_item_rest_fields,
    get_procurement_item,
    load_mapping,
)

DEFAULT_INPUT_CSV = (
    REPO_ROOT
    / "reports/assortment_lifecycle"
    / datetime.now().date().isoformat()
    / "display-auto-order-dry-run.csv"
)
DEFAULT_SUMMARY_JSON = (
    REPO_ROOT
    / "reports/assortment_lifecycle"
    / datetime.now().date().isoformat()
    / "display-auto-order-dry-run-summary.json"
)
DEFAULT_RESULT_PATH = REPO_ROOT / "build/bitrix/display_auto_order_candidates_result.json"
SOURCE_NAME = "display_auto_order_dry_run"
LEGACY_PROCUREMENT_PROCESS_TITLE = "Закупка/Заказ"
LEGACY_PROCUREMENT_ENTITY_TYPE_ID = 1056
TERMINAL_STAGE_KEYS = {
    "done",
    "closed",
    "cancelled",
    "canceled",
    "declined",
    "rejected",
    "exception",
    "blocked",
}
AUTO_ORDER_FIELD_KEYS = [
    "auto_order_source",
    "auto_order_run_id",
    "auto_order_sku_code",
    "auto_order_sku_name",
    "auto_order_decision",
    "auto_order_recommended_qty",
    "auto_order_raw_qty",
    "auto_order_target_stock_qty",
    "auto_order_free_stock_qty",
    "auto_order_incoming_qty",
    "auto_order_reason",
    "auto_order_warnings",
    "auto_order_blockers",
    "auto_order_calculated_at",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--webhook-url")
    parser.add_argument("--mapping-path", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--input-csv", type=Path, default=DEFAULT_INPUT_CSV)
    parser.add_argument("--summary-json", type=Path, default=DEFAULT_SUMMARY_JSON)
    parser.add_argument("--result-path", type=Path, default=DEFAULT_RESULT_PATH)
    parser.add_argument("--assigned-by-id", default="")
    parser.add_argument("--apply", action="store_true", help="Write Bitrix changes.")
    return parser.parse_args(argv)


def load_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return payload if isinstance(payload, dict) else {}


def load_order_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as csv_file:
        rows = [dict(row) for row in csv.DictReader(csv_file)]
    return [
        row
        for row in rows
        if clean_string(row.get("dry_run_decision")) == "order"
        and decimal_value(row.get("recommended_order_qty")) > 0
    ]


def decimal_value(value: Any) -> Decimal:
    raw = clean_string(value).replace(" ", "").replace(",", ".")
    if not raw:
        return Decimal("0")
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def bitrix_number(value: Any) -> str:
    number = decimal_value(value)
    return format(number.normalize(), "f") if number else "0"


def enum_value(mapping: dict[str, Any], logical_key: str, xml_id: str) -> str:
    return (
        clean_string(((mapping.get("enum_map") or {}).get(logical_key) or {}).get(xml_id)) or xml_id
    )


def required_mapping_value(mapping: dict[str, Any], logical_key: str) -> str:
    value = clean_string((mapping.get("field_map") or {}).get(logical_key))
    if not value:
        raise KeyError(f"Bitrix procurement mapping has no field for {logical_key!r}")
    return value


def validate_mapping(mapping: dict[str, Any]) -> None:
    missing = [
        key
        for key in [
            "procurement_contour",
            "pilot_batch_id",
            *AUTO_ORDER_FIELD_KEYS,
        ]
        if not clean_string((mapping.get("field_map") or {}).get(key))
    ]
    if missing:
        raise KeyError("Bitrix procurement mapping is missing fields: " + ", ".join(missing))
    if not clean_string(
        ((mapping.get("enum_map") or {}).get("procurement_contour") or {}).get("ordinary")
    ):
        raise KeyError("Bitrix procurement mapping is missing ordinary contour enum")
    if not clean_string(
        ((mapping.get("enum_map") or {}).get("auto_order_decision") or {}).get("order")
    ):
        raise KeyError("Bitrix procurement mapping is missing auto_order_decision.order enum")


def is_legacy_procurement_mapping(mapping: dict[str, Any]) -> bool:
    process = mapping.get("process") or {}
    title = clean_string(process.get("title"))
    code = clean_string(process.get("code"))
    try:
        entity_type_id = int(process.get("entity_type_id") or 0)
    except (TypeError, ValueError):
        entity_type_id = 0
    return (
        title == LEGACY_PROCUREMENT_PROCESS_TITLE
        or code == "procurement_order"
        or entity_type_id == LEGACY_PROCUREMENT_ENTITY_TYPE_ID
    )


def guard_legacy_procurement_apply(mapping: dict[str, Any]) -> None:
    if not is_legacy_procurement_mapping(mapping):
        return
    raise RuntimeError(
        "Display auto-order Bitrix apply is blocked for legacy process "
        "`Закупка/Заказ`: pre-1C demand must be written only to the separate "
        "`Формирование заказа` process after its mapping is published."
    )


def auto_order_batch_id(row: dict[str, str], *, run_id: str) -> str:
    code = clean_string(row.get("nomenclature_code"))
    if not code:
        raise ValueError("display auto-order row has no nomenclature_code")
    run = clean_string(run_id) or "no-run"
    return f"DISPLAY-AUTO-{run}-{code}"


def auto_order_title(row: dict[str, str]) -> str:
    code = clean_string(row.get("nomenclature_code")) or "без кода"
    qty = bitrix_number(row.get("recommended_order_qty"))
    name = clean_string(row.get("name"))
    return f"Автозаказ витрины · {code} · {qty} шт. · {name}"[:255]


def build_candidate_fields(
    row: dict[str, str],
    *,
    mapping: dict[str, Any],
    run_id: str,
    assigned_by_id: str = "",
    calculated_at: str = "",
) -> tuple[str, dict[str, Any]]:
    category = (mapping.get("category_map") or {}).get("ordinary") or {}
    stage = ((mapping.get("stage_map") or {}).get("ordinary") or {}).get("need")
    category_id = int(category.get("id") or 0)
    if not category_id or not stage:
        raise KeyError("Bitrix procurement mapping has no ordinary/need category-stage")

    batch_id = auto_order_batch_id(row, run_id=run_id)
    field_map = mapping.get("field_map") or {}
    fields: dict[str, Any] = {
        "TITLE": auto_order_title(row),
        "categoryId": category_id,
        "stageId": stage,
        field_map["procurement_contour"]: enum_value(mapping, "procurement_contour", "ordinary"),
        field_map["pilot_batch_id"]: batch_id,
        field_map["auto_order_source"]: SOURCE_NAME,
        field_map["auto_order_run_id"]: (
            int(run_id) if str(run_id).isdigit() else clean_string(run_id)
        ),
        field_map["auto_order_sku_code"]: clean_string(row.get("nomenclature_code")),
        field_map["auto_order_sku_name"]: clean_string(row.get("name")),
        field_map["auto_order_decision"]: enum_value(mapping, "auto_order_decision", "order"),
        field_map["auto_order_recommended_qty"]: bitrix_number(row.get("recommended_order_qty")),
        field_map["auto_order_raw_qty"]: bitrix_number(row.get("recommended_order_qty_raw")),
        field_map["auto_order_target_stock_qty"]: bitrix_number(row.get("target_stock_qty")),
        field_map["auto_order_free_stock_qty"]: bitrix_number(row.get("free_stock_qty")),
        field_map["auto_order_incoming_qty"]: bitrix_number(row.get("incoming_qty")),
        field_map["auto_order_reason"]: clean_string(row.get("reason_ru")),
        field_map["auto_order_warnings"]: clean_string(row.get("warnings")),
        field_map["auto_order_blockers"]: clean_string(row.get("blockers")),
    }
    if calculated_at:
        fields[field_map["auto_order_calculated_at"]] = calculated_at
    if assigned_by_id:
        fields["ASSIGNED_BY_ID"] = assigned_by_id
    return batch_id, {key: value for key, value in fields.items() if value not in ("", None)}


def terminal_stage_ids(mapping: dict[str, Any]) -> set[str]:
    stage_ids: set[str] = set()
    for stage_map in (mapping.get("stage_map") or {}).values():
        if not isinstance(stage_map, dict):
            continue
        for stage_key, stage_id in stage_map.items():
            if clean_string(stage_key).casefold() in TERMINAL_STAGE_KEYS:
                stage_ids.add(clean_string(stage_id))
    return {stage_id for stage_id in stage_ids if stage_id}


def existing_candidate_item(
    api: Any,
    *,
    mapping: dict[str, Any],
    filters: dict[str, Any],
    select_fields: Sequence[str],
    duplicate_label: str,
    open_only: bool = False,
) -> dict[str, Any]:
    entity_type_id = int((mapping.get("process") or {}).get("entity_type_id") or 0)
    payload = api.call(
        "crm.item.list",
        {
            "entityTypeId": entity_type_id,
            "filter": filters,
            "select": ["id", "stageId", *select_fields],
        },
    )
    result = payload.get("result") if isinstance(payload, dict) else payload
    items = result.get("items") if isinstance(result, dict) else []
    if not isinstance(items, list):
        return {}
    if open_only:
        closed_stages = terminal_stage_ids(mapping)
        items = [
            item
            for item in items
            if isinstance(item, dict)
            and clean_string(item.get("stageId")) not in closed_stages
            and clean_string(item.get("id"))
        ]
    else:
        items = [item for item in items if isinstance(item, dict) and clean_string(item.get("id"))]
    if len(items) > 1:
        raise RuntimeError(f"Found multiple Bitrix auto-order candidates for {duplicate_label}")
    return dict(items[0]) if items else {}


def existing_candidate_by_batch(
    api: Any, *, mapping: dict[str, Any], batch_id: str
) -> dict[str, Any]:
    batch_field = crm_item_rest_field_name(required_mapping_value(mapping, "pilot_batch_id"))
    return existing_candidate_item(
        api,
        mapping=mapping,
        filters={f"={batch_field}": batch_id},
        select_fields=[batch_field],
        duplicate_label=f"batch {batch_id}",
    )


def existing_open_candidate_by_sku(
    api: Any,
    *,
    mapping: dict[str, Any],
    sku_code: str,
) -> dict[str, Any]:
    source_field = crm_item_rest_field_name(required_mapping_value(mapping, "auto_order_source"))
    sku_field = crm_item_rest_field_name(required_mapping_value(mapping, "auto_order_sku_code"))
    batch_field = crm_item_rest_field_name(required_mapping_value(mapping, "pilot_batch_id"))
    return existing_candidate_item(
        api,
        mapping=mapping,
        filters={f"={source_field}": SOURCE_NAME, f"={sku_field}": sku_code},
        select_fields=[source_field, sku_field, batch_field],
        duplicate_label=f"open SKU {sku_code}",
        open_only=True,
    )


def remove_workflow_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in fields.items()
        if key not in {"categoryId", "stageId", "CATEGORY_ID", "STAGE_ID"}
    }


def upsert_candidate(
    api: Any,
    row: dict[str, str],
    *,
    mapping: dict[str, Any],
    run_id: str,
    assigned_by_id: str,
    apply: bool,
    calculated_at: str = "",
) -> dict[str, Any]:
    if apply:
        guard_legacy_procurement_apply(mapping)
    entity_type_id = int((mapping.get("process") or {}).get("entity_type_id") or 0)
    batch_id, fields = build_candidate_fields(
        row,
        mapping=mapping,
        run_id=run_id,
        assigned_by_id=assigned_by_id,
        calculated_at=calculated_at,
    )
    rest_fields = crm_item_rest_fields(fields)
    matched_by = "none"
    existing_item = existing_candidate_by_batch(api, mapping=mapping, batch_id=batch_id)
    if existing_item:
        matched_by = "batch_id"
    else:
        existing_item = existing_open_candidate_by_sku(
            api,
            mapping=mapping,
            sku_code=clean_string(row.get("nomenclature_code")),
        )
        if existing_item:
            matched_by = "sku_open_card"
    item_id = clean_string(existing_item.get("id"))
    current_item = (
        get_procurement_item(api, entity_type_id=entity_type_id, item_id=item_id) if item_id else {}
    )
    if item_id:
        rest_fields = remove_workflow_fields(rest_fields)
    changed_fields = changed_rest_fields(current_item, rest_fields) if item_id else rest_fields

    if not apply:
        action = (
            "dry_run_create"
            if not item_id
            else "dry_run_noop" if not changed_fields else "dry_run_update"
        )
    elif item_id and not changed_fields:
        action = "noop"
    elif item_id:
        api.call(
            "crm.item.update",
            {"entityTypeId": entity_type_id, "id": item_id, "fields": changed_fields},
        )
        action = "updated"
    else:
        created = api.call("crm.item.add", {"entityTypeId": entity_type_id, "fields": rest_fields})
        result = created.get("result") if isinstance(created, dict) else created
        item = result.get("item") if isinstance(result, dict) else {}
        item_id = clean_string(item.get("id"))
        action = "created"

    return {
        "source": SOURCE_NAME,
        "source_number": clean_string(row.get("nomenclature_code")),
        "batch_id": batch_id,
        "matched_by": matched_by,
        "action": action,
        "item_id": item_id,
        "recommended_order_qty": bitrix_number(row.get("recommended_order_qty")),
        "changed_field_count": len(changed_fields),
    }


def bitrix_values_match_smoke() -> None:
    if not bitrix_values_match("5", "5.0"):
        raise AssertionError("Bitrix numeric comparison helper is not available")


def calculated_at_from_file(path: Path) -> str:
    if not path.exists():
        return ""
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    webhook_base = (args.webhook_url or "").strip()
    env = load_env(args.env_file)
    if not webhook_base:
        webhook_base = (
            env.get("PROCUREMENT_BITRIX_WEBHOOK_URL")
            or env.get("BITRIX_BOX_WEBHOOK_BASE")
            or env.get("BITRIX24_BOX_WEBHOOK_URL")
            or ""
        ).strip()
    if not webhook_base:
        raise SystemExit(
            f"Bitrix webhook is not configured. Set PROCUREMENT_BITRIX_WEBHOOK_URL "
            f"or BITRIX_BOX_WEBHOOK_BASE in {args.env_file}"
        )

    mapping = load_mapping(args.mapping_path)
    validate_mapping(mapping)
    summary = load_summary(args.summary_json)
    run_id = clean_string(summary.get("classification_run_id"))
    calculated_at = clean_string(summary.get("generated_at")) or calculated_at_from_file(
        args.summary_json
    )
    rows = load_order_rows(args.input_csv)
    api = BitrixRestApi(webhook_base)
    bitrix_values_match_smoke()
    result_rows = [
        upsert_candidate(
            api,
            row,
            mapping=mapping,
            run_id=run_id,
            assigned_by_id=args.assigned_by_id,
            apply=args.apply,
            calculated_at=calculated_at,
        )
        for row in rows
    ]
    result = {
        "mode": "apply" if args.apply else "dry-run",
        "source": SOURCE_NAME,
        "input_csv": str(args.input_csv),
        "summary_json": str(args.summary_json),
        "rows": result_rows,
    }
    args.result_path.parent.mkdir(parents=True, exist_ok=True)
    args.result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps({"mode": result["mode"], "rows": len(result_rows)}, ensure_ascii=False, indent=2)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
