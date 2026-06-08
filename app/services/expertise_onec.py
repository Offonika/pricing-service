from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.core.config import Settings

DECISION_LABEL_TO_CODE = {
    "Принято": "approved",
    "approved": "approved",
    "Отказано": "rejected",
    "rejected": "rejected",
}


def _clean_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _optional_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"unsupported datetime value: {value!r}")


def _optional_number(value: Any) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, Decimal):
        normalized = float(value)
        return int(normalized) if normalized.is_integer() else normalized
    normalized = str(value).strip()
    if not normalized:
        return None
    parsed = Decimal(normalized)
    as_float = float(parsed)
    return int(as_float) if as_float.is_integer() else as_float


def normalize_onec_decision_code(value: Any) -> str | None:
    normalized = _clean_string(value)
    if normalized is None:
        return None
    return DECISION_LABEL_TO_CODE.get(normalized)


def load_expertise_onec_sql(settings: Settings) -> str:
    if settings.expertise_onec_sql:
        return settings.expertise_onec_sql
    if settings.expertise_onec_sql_file:
        return Path(settings.expertise_onec_sql_file).read_text(encoding="utf-8")
    raise RuntimeError(
        "Expertise 1C SQL is not configured. " "Set EXPERTISE_ONEC_SQL or EXPERTISE_ONEC_SQL_FILE."
    )


def _build_item_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {}
    field_mapping = {
        "item_line_no": "line_no",
        "item_nomenclature_ref": "nomenclature_ref",
        "item_nomenclature_name": "nomenclature_name",
        "item_quantity": "quantity",
        "item_price": "price",
        "item_amount": "amount",
        "item_quality_ref": "quality_ref",
        "item_quality_name": "quality_name",
        "item_return_reason_ref": "return_reason_ref",
        "item_return_reason_name": "return_reason_name",
        "item_linked_customer_order_ref": "linked_customer_order_ref",
        "item_linked_customer_order_number": "linked_customer_order_number",
        "item_decision_label": "decision_label",
        "item_decision_ref": "decision_ref",
    }
    for source_key, target_key in field_mapping.items():
        if source_key not in row:
            continue
        value = row.get(source_key)
        if target_key in {"quantity", "price", "amount", "line_no"}:
            normalized = _optional_number(value)
        else:
            normalized = _clean_string(value)
        if normalized is not None:
            item[target_key] = normalized
    decision_label = item.get("decision_label") or _clean_string(row.get("decision_label"))
    if decision_label is not None:
        item["decision_label"] = decision_label
        decision_code = normalize_onec_decision_code(decision_label)
        if decision_code is not None:
            item["decision_code"] = decision_code
    return item


def build_expertise_sync_payloads(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}

    for raw_row in rows:
        row = dict(raw_row)
        external_id = _clean_string(row.get("external_id"))
        if not external_id:
            continue

        case_payload = grouped.get(external_id)
        if case_payload is None:
            payload: dict[str, Any] = {"source": "1c", "items": []}
            for source_key, target_key in (
                ("posted", "posted"),
                ("organization_ref", "organization_ref"),
                ("organization_name", "organization_name"),
                ("base_document_ref", "base_document_ref"),
                ("base_document_number", "base_document_number"),
                ("base_document_number", "linked_sale_number"),
                ("manager_comment", "manager_comment"),
                ("quality_comment", "quality_comment"),
                ("store_ref", "store_ref"),
                ("counterparty_ref", "counterparty_ref"),
                ("responsible_ref", "responsible_ref"),
                ("contract_ref", "contract_ref"),
                ("warehouse_ref", "warehouse_ref"),
            ):
                value = row.get(source_key)
                normalized = value if isinstance(value, bool) else _clean_string(value)
                if normalized is not None:
                    payload[target_key] = normalized

            case_payload = {
                "external_id": external_id,
                "onec_expertise_ref": _clean_string(row.get("onec_expertise_ref")) or external_id,
                "onec_expertise_number": _clean_string(row.get("onec_expertise_number")) or "",
                "created_at_source": _optional_datetime(row.get("created_at_source")),
                "organization_ref": _clean_string(row.get("organization_ref")),
                "contract_ref": _clean_string(row.get("contract_ref")),
                "linked_sale_ref": _clean_string(
                    row.get("linked_sale_ref") or row.get("base_document_ref")
                ),
                "linked_sale_number": _clean_string(
                    row.get("linked_sale_number") or row.get("base_document_number")
                ),
                "store_external_id": _clean_string(row.get("store_external_id")),
                "store_name": _clean_string(row.get("store_name")),
                "customer_name": _clean_string(row.get("customer_name")),
                "customer_phone": _clean_string(row.get("customer_phone")),
                "owner_user_external_id": _clean_string(row.get("owner_user_external_id")) or "",
                "linked_customer_order_ref": _clean_string(row.get("linked_customer_order_ref")),
                "linked_customer_order_number": _clean_string(
                    row.get("linked_customer_order_number")
                ),
                "problem_summary": _clean_string(row.get("manager_comment")),
                "decision_label": _clean_string(row.get("decision_label")),
                "decision_comment": _clean_string(row.get("quality_comment")),
                "decision_code": normalize_onec_decision_code(row.get("decision_label")),
                "payload": payload,
            }
            grouped[external_id] = case_payload

        item_payload = _build_item_payload(row)
        item_line_no = item_payload.get("line_no")
        items = case_payload["payload"]["items"]
        if item_payload and not any(item.get("line_no") == item_line_no for item in items):
            items.append(item_payload)

        if case_payload["linked_customer_order_ref"] is None:
            case_payload["linked_customer_order_ref"] = _clean_string(
                row.get("item_linked_customer_order_ref")
            )
        if case_payload["linked_customer_order_number"] is None:
            case_payload["linked_customer_order_number"] = _clean_string(
                row.get("item_linked_customer_order_number")
            )
        if case_payload["decision_code"] is None:
            case_payload["decision_code"] = normalize_onec_decision_code(
                row.get("item_decision_label")
            )
        if case_payload["decision_label"] is None:
            case_payload["decision_label"] = _clean_string(row.get("item_decision_label"))

    result: list[dict[str, Any]] = []
    for case_payload in grouped.values():
        if not case_payload["owner_user_external_id"]:
            continue
        if not case_payload["onec_expertise_number"] or case_payload["created_at_source"] is None:
            continue
        result.append(case_payload)

    result.sort(
        key=lambda item: (
            item["created_at_source"] or datetime.min,
            item["onec_expertise_number"],
            item["external_id"],
        )
    )
    return result


class OneCExpertiseExtractor:
    def __init__(self, onec_engine, *, sql: str | None = None):
        self.onec_engine = onec_engine
        self.sql = sql

    def fetch_case_payloads(self) -> list[dict[str, Any]]:
        if not self.sql:
            raise RuntimeError(
                "Expertise extractor SQL is not configured. "
                "Pass sql explicitly or configure EXPERTISE_ONEC_SQL / EXPERTISE_ONEC_SQL_FILE."
            )
        try:
            with self.onec_engine.connect() as conn:
                rows = conn.execute(text(self.sql)).mappings().all()
        finally:
            self.onec_engine.dispose()
        return build_expertise_sync_payloads(rows)
