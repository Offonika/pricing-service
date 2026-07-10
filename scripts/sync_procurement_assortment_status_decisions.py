#!/usr/bin/env python3
"""Sync Bitrix procurement assortment-status decisions into manual overrides."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    crm_item_rest_field_name,
    load_mapping,
)

DEFAULT_MANUAL_OVERRIDES_PATH = REPO_ROOT / "config/assortment/display-manual-overrides.json"
DEFAULT_RESULT_PATH = REPO_ROOT / "build/bitrix/procurement_assortment_status_decisions_result.json"
SOURCE_RULE = "bitrix_assortment_status_decision"
SOURCE_RULE_RU = "ручное решение в Bitrix Закупка/Заказ"
MANUAL_STATUS_DECISIONS = {
    "matrix",
    "on_demand",
    "replace_candidate",
    "nonliquid",
    "do_not_order",
}
SUPPORTED_DECISIONS = {*MANUAL_STATUS_DECISIONS, "working"}
DECISION_FIELD_KEYS = [
    "auto_order_sku_code",
    "auto_order_sku_name",
    "assortment_status_decision",
    "assortment_status_reason",
    "assortment_status_approved_by",
    "assortment_status_changed_at",
    "assortment_commercial_marks",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--webhook-url")
    parser.add_argument("--mapping-path", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--manual-overrides-path", type=Path, default=DEFAULT_MANUAL_OVERRIDES_PATH)
    parser.add_argument("--result-path", type=Path, default=DEFAULT_RESULT_PATH)
    parser.add_argument("--apply", action="store_true", help="Write manual overrides JSON.")
    return parser.parse_args(argv)


def required_mapping_value(mapping: dict[str, Any], logical_key: str) -> str:
    value = clean_string((mapping.get("field_map") or {}).get(logical_key))
    if not value:
        raise KeyError(f"Bitrix procurement mapping has no field for {logical_key!r}")
    return value


def rest_field(mapping: dict[str, Any], logical_key: str) -> str:
    return crm_item_rest_field_name(required_mapping_value(mapping, logical_key))


def validate_mapping(mapping: dict[str, Any]) -> None:
    missing = [
        key
        for key in DECISION_FIELD_KEYS
        if not clean_string((mapping.get("field_map") or {}).get(key))
    ]
    if missing:
        raise KeyError("Bitrix procurement mapping is missing fields: " + ", ".join(missing))
    if not clean_string(
        ((mapping.get("enum_map") or {}).get("assortment_status_decision") or {}).get("matrix")
    ):
        raise KeyError(
            "Bitrix procurement mapping is missing assortment_status_decision.matrix enum"
        )


def decision_xml_id(mapping: dict[str, Any], value: Any) -> str:
    raw = clean_string(value)
    if not raw:
        return ""
    enum_map = (mapping.get("enum_map") or {}).get("assortment_status_decision") or {}
    for xml_id, enum_id in enum_map.items():
        if raw == clean_string(enum_id) or raw == clean_string(xml_id):
            return clean_string(xml_id)
    return raw


def decision_label(mapping: dict[str, Any], xml_id: str) -> str:
    labels = {
        "matrix": "Матричный",
        "working": "Рабочий",
        "on_demand": "Под заказ",
        "replace_candidate": "Кандидат на замену",
        "nonliquid": "Неликвид",
        "do_not_order": "Не закупать",
    }
    return labels.get(xml_id, xml_id)


def list_procurement_items(api: Any, *, mapping: dict[str, Any]) -> list[dict[str, Any]]:
    entity_type_id = int((mapping.get("process") or {}).get("entity_type_id") or 0)
    if not entity_type_id:
        raise KeyError("Bitrix procurement mapping has no process.entity_type_id")
    custom_select = [rest_field(mapping, key) for key in DECISION_FIELD_KEYS]
    select = ["id", "title", "updatedTime", "assignedById", *custom_select]
    rows: list[dict[str, Any]] = []
    start: int | None = 0
    while start is not None:
        params: dict[str, Any] = {"entityTypeId": entity_type_id, "select": select}
        if start:
            params["start"] = start
        payload = api.call("crm.item.list", params)
        result = payload.get("result") if isinstance(payload, dict) else payload
        items = result.get("items") if isinstance(result, dict) else []
        rows.extend(item for item in items if isinstance(item, dict))
        next_start = payload.get("next") if isinstance(payload, dict) else None
        start = int(next_start) if str(next_start or "").isdigit() else None
    return rows


def field_value(item: dict[str, Any], mapping: dict[str, Any], logical_key: str) -> Any:
    return item.get(rest_field(mapping, logical_key))


def date_text(value: Any) -> str:
    raw = clean_string(value)
    if not raw:
        return ""
    if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-":
        return raw[:10]
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw


def text_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [clean_string(item) for item in value if clean_string(item)]
    raw = clean_string(value).replace("\n", ",").replace(";", ",")
    return [part.strip() for part in raw.split(",") if part.strip()]


def override_from_bitrix_item(
    item: dict[str, Any],
    *,
    mapping: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    item_id = clean_string(item.get("id"))
    decision = decision_xml_id(mapping, field_value(item, mapping, "assortment_status_decision"))
    if decision in {"", "no_change"}:
        return None, []
    blockers: list[str] = []
    if decision not in SUPPORTED_DECISIONS:
        return None, [f"unsupported_decision:{decision}"]

    code = clean_string(field_value(item, mapping, "auto_order_sku_code"))
    if not code:
        return None, ["nomenclature_code_required"]

    reason = clean_string(field_value(item, mapping, "assortment_status_reason"))
    approved_by = clean_string(field_value(item, mapping, "assortment_status_approved_by"))
    changed_at = date_text(
        field_value(item, mapping, "assortment_status_changed_at") or item.get("updatedTime")
    )
    if not reason:
        blockers.append("manual_reason_required")
    if not approved_by:
        blockers.append("manual_approved_by_required")
    if not changed_at:
        blockers.append("manual_changed_at_required")

    override: dict[str, Any] = {
        "nomenclature_code": code,
        "approval_rule": SOURCE_RULE,
        "approval_rule_ru": SOURCE_RULE_RU,
        "approval_source": f"bitrix_procurement_order:{item_id}",
        "manual_approved_by": approved_by,
        "manual_changed_at": changed_at,
        "manual_reason": reason or f"Ручное решение в Bitrix: {decision_label(mapping, decision)}.",
        "source_bitrix_item_id": item_id,
        "source_bitrix_title": clean_string(item.get("title")),
        "source_bitrix_updated_at": clean_string(item.get("updatedTime")),
        "sync_blockers": blockers,
    }
    sku_name = clean_string(field_value(item, mapping, "auto_order_sku_name"))
    if sku_name:
        override["source_bitrix_sku_name"] = sku_name
    commercial_marks = text_list(field_value(item, mapping, "assortment_commercial_marks"))
    if commercial_marks:
        override["commercial_marks"] = commercial_marks
    if decision == "working":
        override["working_confirmed_by_folder_responsible"] = True
    else:
        override["manual_status"] = decision
    return override, blockers


def collect_decisions(
    items: list[dict[str, Any]],
    *,
    mapping: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    decisions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in items:
        override, blockers = override_from_bitrix_item(item, mapping=mapping)
        if override is None:
            if blockers:
                skipped.append({"item_id": clean_string(item.get("id")), "blockers": blockers})
            continue
        decisions.append(override)
    return decisions, skipped


def load_manual_overrides(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "_description": "Ручные решения для пилота дисплеев.",
            "items": [],
        }
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return payload if isinstance(payload, dict) else {"items": payload}


def merge_manual_overrides(
    payload: dict[str, Any],
    decisions: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    items = payload.get("items")
    if not isinstance(items, list):
        items = []
    existing = [item for item in items if isinstance(item, dict)]
    by_source = {
        clean_string(item.get("approval_source")): index
        for index, item in enumerate(existing)
        if clean_string(item.get("approval_source")).startswith("bitrix_procurement_order:")
    }
    merge_rows: list[dict[str, Any]] = []
    for decision in decisions:
        source = clean_string(decision.get("approval_source"))
        if source in by_source:
            existing[by_source[source]] = decision
            action = "updated"
        else:
            existing.append(decision)
            by_source[source] = len(existing) - 1
            action = "added"
        merge_rows.append(
            {
                "action": action,
                "approval_source": source,
                "nomenclature_code": decision.get("nomenclature_code"),
                "manual_status": decision.get("manual_status"),
                "working_confirmed_by_folder_responsible": bool(
                    decision.get("working_confirmed_by_folder_responsible")
                ),
                "sync_blockers": decision.get("sync_blockers") or [],
            }
        )
    merged = dict(payload)
    merged["_bitrix_assortment_status_synced_at"] = datetime.now(timezone.utc).isoformat()
    merged["_bitrix_assortment_status_source_rule"] = SOURCE_RULE
    merged["items"] = existing
    return merged, merge_rows


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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
    items = list_procurement_items(BitrixRestApi(webhook_base), mapping=mapping)
    decisions, skipped = collect_decisions(items, mapping=mapping)
    merge_rows: list[dict[str, Any]] = []
    if args.apply:
        overrides = load_manual_overrides(args.manual_overrides_path)
        merged, merge_rows = merge_manual_overrides(overrides, decisions)
        write_json(args.manual_overrides_path, merged)

    result = {
        "mode": "apply" if args.apply else "dry-run",
        "items_scanned": len(items),
        "decisions": decisions,
        "skipped": skipped,
        "merge_rows": merge_rows,
        "manual_overrides_path": str(args.manual_overrides_path),
    }
    write_json(args.result_path, result)
    print(
        json.dumps(
            {
                "mode": result["mode"],
                "items_scanned": len(items),
                "decisions": len(decisions),
                "skipped": len(skipped),
                "merged": len(merge_rows),
                "result_path": str(args.result_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
