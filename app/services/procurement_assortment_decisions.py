from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.services.procurement_labels import ProcurementLabelsBitrixClient, clean_string

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANUAL_OVERRIDES_PATH = REPO_ROOT / "config/assortment/display-manual-overrides.json"

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

DECISION_LABELS = {
    "no_change": "Без изменения",
    "matrix": "Матричный",
    "working": "Рабочий",
    "on_demand": "Под заказ",
    "replace_candidate": "Кандидат на замену",
    "nonliquid": "Неликвид",
    "do_not_order": "Не закупать",
}


def resolve_repo_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else REPO_ROOT / path


def load_mapping(path: str | Path) -> dict[str, Any]:
    mapping_path = resolve_repo_path(path)
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def crm_item_rest_field_name(field: str) -> str:
    raw = clean_string(field)
    if not raw:
        return ""
    builtins = {
        "ID": "id",
        "TITLE": "title",
        "STAGE_ID": "stageId",
        "CATEGORY_ID": "categoryId",
        "ASSIGNED_BY_ID": "assignedById",
    }
    upper = raw.upper()
    if upper in builtins:
        return builtins[upper]
    if upper.startswith("UF_CRM_"):
        parts = [part for part in raw.split("_")[2:] if part]
        return "ufCrm" + "".join(part[:1].upper() + part[1:].lower() for part in parts)
    return raw


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


def entity_type_id_from_mapping(mapping: dict[str, Any], settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    return int(
        (mapping.get("process") or {}).get("entity_type_id")
        or settings.procurement_labels_entity_type_id
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


def decision_enum_id(mapping: dict[str, Any], xml_id: str) -> str:
    normalized = clean_string(xml_id)
    enum_map = (mapping.get("enum_map") or {}).get("assortment_status_decision") or {}
    enum_id = clean_string(enum_map.get(normalized))
    if not enum_id:
        allowed = ", ".join(sorted(enum_map)) or "empty enum map"
        raise ValueError(
            f"Unsupported assortment status decision: {normalized}. Allowed: {allowed}"
        )
    return enum_id


def decision_label(xml_id: str) -> str:
    return DECISION_LABELS.get(xml_id, xml_id)


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


def text_list_value(values: list[str]) -> str:
    return ", ".join(clean_string(item) for item in values if clean_string(item))


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
        "manual_reason": reason or f"Ручное решение в Bitrix: {decision_label(decision)}.",
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


def load_manual_overrides(path: str | Path) -> dict[str, Any]:
    manual_path = resolve_repo_path(path)
    if not manual_path.exists():
        return {
            "_description": "Ручные решения для пилота дисплеев.",
            "items": [],
        }
    payload = json.loads(manual_path.read_text(encoding="utf-8-sig"))
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


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    target = resolve_repo_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_webhook_url(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    webhook_url = (
        settings.procurement_labels_bitrix_webhook_url
        or settings.procurement_bitrix_webhook_url
        or settings.bitrix_box_webhook_base
        or ""
    ).strip()
    if not webhook_url:
        raise RuntimeError("Bitrix procurement webhook is not configured")
    return webhook_url


def load_configured_mapping(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    mapping = load_mapping(settings.procurement_labels_mapping_path)
    validate_mapping(mapping)
    return mapping


def build_update_fields(mapping: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if "status_decision" in payload:
        fields[rest_field(mapping, "assortment_status_decision")] = decision_enum_id(
            mapping,
            clean_string(payload.get("status_decision")) or "no_change",
        )
    optional_text_fields = {
        "status_reason": "assortment_status_reason",
        "status_approved_by": "assortment_status_approved_by",
        "status_changed_at": "assortment_status_changed_at",
    }
    for payload_key, mapping_key in optional_text_fields.items():
        if payload_key in payload:
            fields[rest_field(mapping, mapping_key)] = clean_string(payload.get(payload_key))
    if "commercial_marks" in payload:
        marks = payload.get("commercial_marks")
        fields[rest_field(mapping, "assortment_commercial_marks")] = (
            text_list_value(marks) if isinstance(marks, list) else clean_string(marks)
        )
    return fields


def build_decision_payload(
    item: dict[str, Any],
    *,
    mapping: dict[str, Any],
    entity_type_id: int,
) -> dict[str, Any]:
    decision = decision_xml_id(mapping, field_value(item, mapping, "assortment_status_decision"))
    if not decision:
        decision = "no_change"
    override, blockers = override_from_bitrix_item(item, mapping=mapping)
    return {
        "item_id": clean_string(item.get("id")),
        "entity_type_id": entity_type_id,
        "title": clean_string(item.get("title")),
        "sku_code": clean_string(field_value(item, mapping, "auto_order_sku_code")),
        "sku_name": clean_string(field_value(item, mapping, "auto_order_sku_name")),
        "status_decision": decision,
        "status_decision_label": decision_label(decision),
        "status_reason": clean_string(field_value(item, mapping, "assortment_status_reason")),
        "status_approved_by": clean_string(
            field_value(item, mapping, "assortment_status_approved_by")
        ),
        "status_changed_at": date_text(field_value(item, mapping, "assortment_status_changed_at")),
        "commercial_marks": text_list(field_value(item, mapping, "assortment_commercial_marks")),
        "sync_blockers": blockers,
        "manual_override_preview": override,
    }


def fetch_decision(
    item_id: str,
    *,
    settings: Settings | None = None,
    client: ProcurementLabelsBitrixClient | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    mapping = load_configured_mapping(settings)
    entity_type_id = entity_type_id_from_mapping(mapping, settings)
    bitrix = client or ProcurementLabelsBitrixClient(
        resolve_webhook_url(settings),
        timeout=float(settings.procurement_labels_bitrix_rest_timeout_seconds),
    )
    item = bitrix.get_item(entity_type_id=entity_type_id, item_id=item_id)
    return build_decision_payload(item, mapping=mapping, entity_type_id=entity_type_id)


def update_decision(
    item_id: str,
    payload: dict[str, Any],
    *,
    settings: Settings | None = None,
    client: ProcurementLabelsBitrixClient | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    mapping = load_configured_mapping(settings)
    entity_type_id = entity_type_id_from_mapping(mapping, settings)
    bitrix = client or ProcurementLabelsBitrixClient(
        resolve_webhook_url(settings),
        timeout=float(settings.procurement_labels_bitrix_rest_timeout_seconds),
    )
    fields = build_update_fields(mapping, payload)
    bitrix.update_item(entity_type_id=entity_type_id, item_id=item_id, fields=fields)
    item = bitrix.get_item(entity_type_id=entity_type_id, item_id=item_id)
    return build_decision_payload(item, mapping=mapping, entity_type_id=entity_type_id)


def sync_decision_to_manual_overrides(
    item_id: str,
    *,
    settings: Settings | None = None,
    client: ProcurementLabelsBitrixClient | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    mapping = load_configured_mapping(settings)
    entity_type_id = entity_type_id_from_mapping(mapping, settings)
    bitrix = client or ProcurementLabelsBitrixClient(
        resolve_webhook_url(settings),
        timeout=float(settings.procurement_labels_bitrix_rest_timeout_seconds),
    )
    item = bitrix.get_item(entity_type_id=entity_type_id, item_id=item_id)
    decision = build_decision_payload(item, mapping=mapping, entity_type_id=entity_type_id)
    override, blockers = override_from_bitrix_item(item, mapping=mapping)
    if override is None:
        if not blockers:
            blockers = ["status_decision_required"]
        return {
            "decision": decision,
            "synced": False,
            "merge_action": "",
            "manual_overrides_path": str(
                resolve_repo_path(settings.procurement_assortment_manual_overrides_path)
            ),
            "blockers": blockers,
        }
    if blockers:
        return {
            "decision": decision,
            "synced": False,
            "merge_action": "",
            "manual_overrides_path": str(
                resolve_repo_path(settings.procurement_assortment_manual_overrides_path)
            ),
            "blockers": blockers,
        }

    manual_path = resolve_repo_path(settings.procurement_assortment_manual_overrides_path)
    merged, rows = merge_manual_overrides(load_manual_overrides(manual_path), [override])
    write_json(manual_path, merged)
    return {
        "decision": build_decision_payload(item, mapping=mapping, entity_type_id=entity_type_id),
        "synced": True,
        "merge_action": clean_string(rows[0].get("action")) if rows else "",
        "manual_overrides_path": str(manual_path),
        "blockers": [],
    }
