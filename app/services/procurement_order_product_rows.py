from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.procurement_order_formation import (
    ProcurementOrderFormation,
    ProcurementOrderFormationEvent,
)
from app.services.bitrix_order_formation import bitrix_call
from app.services.procurement_order_formation import PROCUREMENT_PROCESS_ENTITY_TYPE_ID

PRODUCT_ROW_OWNER_TYPE = f"T{PROCUREMENT_PROCESS_ENTITY_TYPE_ID}"
PRODUCT_ROW_BATCH_SIZE = 50
PRODUCT_ROW_METHODS = {
    "crm.productrow.list",
    "crm.productrow.add",
    "crm.productrow.update",
    "crm.productrow.delete",
}
MANAGED_FIELDS = (
    "PRODUCT_ID",
    "PRODUCT_NAME",
    "PRICE",
    "QUANTITY",
    "CURRENCY_ID",
    "SORT",
)


class ProcurementProductRowsSyncError(RuntimeError):
    """A product-row mirror could not be made exact and needs a retry."""


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _row_value(row: Mapping[str, Any], field: str) -> Any:
    return row.get(field, row.get(field.lower()))


def _decimal_text(value: Any) -> str:
    try:
        number = Decimal(str(value or "0").replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise ProcurementProductRowsSyncError(f"Некорректное числовое значение {value!r}") from exc
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _safe_error_message(error: BaseException | str) -> str:
    message = _clean(error) or "Неизвестная ошибка синхронизации товаров"
    return re.sub(r"https?://\S+", "[url]", message, flags=re.IGNORECASE)[:1000]


def build_procurement_product_rows(order: ProcurementOrderFormation) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    missing: list[int] = []
    mismatched: list[int] = []
    for line in order.lines:
        if line.removed:
            continue
        product_id = _clean(line.bitrix_product_id)
        if not product_id:
            missing.append(line.line_number)
            continue
        source_guid = re.sub(r"[^0-9a-f]", "", _clean(line.nomenclature_ref).casefold())
        catalog_guid = re.sub(r"[^0-9a-f]", "", _clean(line.bitrix_product_xml_id).casefold())
        if not source_guid or source_guid != catalog_guid:
            mismatched.append(line.line_number)
            continue
        rows.append(
            {
                "PRODUCT_ID": int(product_id),
                "PRODUCT_NAME": _clean(line.nomenclature_name),
                "PRICE": _decimal_text(line.purchase_price),
                "QUANTITY": _decimal_text(line.final_quantity),
                "CURRENCY_ID": _clean(line.currency or order.currency).upper(),
                "SORT": int(line.line_number) * 10,
            }
        )
    if missing or mismatched:
        details: list[str] = []
        if missing:
            details.append("нет товара Bitrix в строках " + ", ".join(map(str, missing[:20])))
        if mismatched:
            details.append("XML_ID не совпадает в строках " + ", ".join(map(str, mismatched[:20])))
        raise ProcurementProductRowsSyncError("; ".join(details))
    return rows


def procurement_product_rows_checksum(rows: Sequence[Mapping[str, Any]]) -> str:
    normalized = [_managed_row(row) for row in rows]
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _managed_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "PRODUCT_ID": int(_row_value(row, "PRODUCT_ID") or 0),
        "PRODUCT_NAME": _clean(_row_value(row, "PRODUCT_NAME")),
        "PRICE": _decimal_text(_row_value(row, "PRICE")),
        "QUANTITY": _decimal_text(_row_value(row, "QUANTITY")),
        "CURRENCY_ID": _clean(_row_value(row, "CURRENCY_ID")).upper(),
        "SORT": int(_row_value(row, "SORT") or 0),
    }


def _product_row_key(row: Mapping[str, Any]) -> tuple[int, int]:
    managed = _managed_row(row)
    return managed["PRODUCT_ID"], managed["SORT"]


def _list_product_rows(
    *, item_id: str, settings: Settings, webhook_base: str = ""
) -> list[dict[str, Any]]:
    payload = bitrix_call(
        "crm.productrow.list",
        {
            "filter": {"OWNER_TYPE": PRODUCT_ROW_OWNER_TYPE, "OWNER_ID": int(item_id)},
            "select": ["ID", "OWNER_TYPE", "OWNER_ID", *MANAGED_FIELDS],
        },
        settings=settings,
        webhook_base=webhook_base,
    )
    result = payload.get("result")
    if result is None:
        return []
    if not isinstance(result, list):
        raise ProcurementProductRowsSyncError("Bitrix вернул некорректный список товаров")
    return [dict(row) for row in result if isinstance(row, Mapping)]


def list_procurement_product_rows(
    *, item_id: str, settings: Settings | None = None, webhook_base: str = ""
) -> list[dict[str, Any]]:
    return _list_product_rows(
        item_id=item_id,
        settings=settings or get_settings(),
        webhook_base=webhook_base,
    )


def _flatten(prefix: str, value: Any) -> list[tuple[str, str]]:
    if isinstance(value, Mapping):
        result: list[tuple[str, str]] = []
        for key, child in value.items():
            result.extend(_flatten(f"{prefix}[{key}]", child))
        return result
    if isinstance(value, list):
        result = []
        for index, child in enumerate(value):
            result.extend(_flatten(f"{prefix}[{index}]", child))
        return result
    return [(prefix, "" if value is None else str(value))]


def _batch_command(method: str, params: Mapping[str, Any]) -> str:
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        pairs.extend(_flatten(str(key), value))
    return method + "?" + urllib.parse.urlencode(pairs)


def _run_batches(
    commands: Sequence[tuple[str, str]], *, settings: Settings, webhook_base: str = ""
) -> None:
    for start in range(0, len(commands), PRODUCT_ROW_BATCH_SIZE):
        chunk = commands[start : start + PRODUCT_ROW_BATCH_SIZE]
        payload = bitrix_call(
            "batch",
            {"halt": 1, "cmd": dict(chunk)},
            settings=settings,
            webhook_base=webhook_base,
        )
        batch = payload.get("result") or {}
        errors = batch.get("result_error") or {}
        if errors:
            key = next(iter(errors))
            raise ProcurementProductRowsSyncError(
                f"Bitrix не применил операцию {key}: {errors[key]}"
            )


def _diff_product_rows(
    current: Sequence[Mapping[str, Any]], desired: Sequence[Mapping[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    available: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in current:
        available[_product_row_key(row)].append(dict(row))

    additions: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    for row in desired:
        matches = available.get(_product_row_key(row)) or []
        if not matches:
            additions.append(dict(row))
            continue
        existing = matches.pop(0)
        if _managed_row(existing) != _managed_row(row):
            updates.append({"id": _clean(_row_value(existing, "ID")), "fields": dict(row)})

    deletions = [
        {"id": _clean(_row_value(row, "ID"))}
        for matches in available.values()
        for row in matches
        if _clean(_row_value(row, "ID"))
    ]
    return {"add": additions, "update": updates, "delete": deletions}


def _product_rows_state(order: ProcurementOrderFormation) -> dict[str, Any]:
    return {
        "state": order.bitrix_product_rows_sync_state,
        "checksum": order.bitrix_product_rows_checksum,
        "expected_count": order.bitrix_product_rows_expected_count,
        "synced_count": order.bitrix_product_rows_synced_count,
        "synced_at": order.bitrix_product_rows_synced_at,
        "error": order.bitrix_product_rows_error,
    }


def _audit(
    db: Session,
    *,
    order: ProcurementOrderFormation,
    event_type: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    actor: str,
) -> None:
    safe_before = json.loads(json.dumps(before, ensure_ascii=False, default=str))
    safe_after = json.loads(json.dumps(after, ensure_ascii=False, default=str))
    if safe_before == safe_after:
        return
    digest = hashlib.sha256(
        json.dumps(safe_after, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    idempotency_key = f"procurement-product-rows:{order.id}:{event_type}:{digest}"
    if db.scalar(
        select(ProcurementOrderFormationEvent).where(
            ProcurementOrderFormationEvent.idempotency_key == idempotency_key
        )
    ):
        return
    db.add(
        ProcurementOrderFormationEvent(
            order_id=order.id,
            entity_type="order",
            entity_id=str(order.id),
            event_type=event_type,
            actor=actor,
            idempotency_key=idempotency_key,
            before=safe_before,
            after=safe_after,
            payload={"entity_type_id": PROCUREMENT_PROCESS_ENTITY_TYPE_ID},
        )
    )


def sync_procurement_order_product_rows(
    db: Session,
    order: ProcurementOrderFormation,
    *,
    apply: bool,
    settings: Settings | None = None,
    webhook_base: str = "",
    actor: str = "system:procurement-product-rows",
) -> dict[str, Any]:
    settings = settings or get_settings()
    canonical_link = (
        order.bitrix_entity_type_id == PROCUREMENT_PROCESS_ENTITY_TYPE_ID
        and bool(_clean(order.bitrix_item_id))
        and not bool(_clean(order.bitrix_link_error))
    )
    if not canonical_link:
        result = {"state": "not_applicable", "order_id": order.id, "item_id": None}
        if apply:
            order.bitrix_product_rows_sync_state = "not_applicable"
        return result

    before = _product_rows_state(order)
    try:
        desired = build_procurement_product_rows(order)
        checksum = procurement_product_rows_checksum(desired)
        current = _list_product_rows(
            item_id=_clean(order.bitrix_item_id), settings=settings, webhook_base=webhook_base
        )
        diff = _diff_product_rows(current, desired)
        result = {
            "state": "pending" if any(diff.values()) else "synced",
            "order_id": order.id,
            "item_id": _clean(order.bitrix_item_id),
            "expected_count": len(desired),
            "current_count": len(current),
            "checksum": checksum,
            "add": len(diff["add"]),
            "update": len(diff["update"]),
            "delete": len(diff["delete"]),
        }
        if not apply:
            return result

        upserts: list[tuple[str, str]] = []
        for index, row in enumerate(diff["add"]):
            fields = {
                "OWNER_TYPE": PRODUCT_ROW_OWNER_TYPE,
                "OWNER_ID": int(_clean(order.bitrix_item_id)),
                **row,
            }
            upserts.append(
                (f"add_{index}", _batch_command("crm.productrow.add", {"fields": fields}))
            )
        for index, row in enumerate(diff["update"]):
            upserts.append(
                (
                    f"update_{index}",
                    _batch_command(
                        "crm.productrow.update", {"id": row["id"], "fields": row["fields"]}
                    ),
                )
            )
        _run_batches(upserts, settings=settings, webhook_base=webhook_base)
        deletes = [
            (
                f"delete_{index}",
                _batch_command("crm.productrow.delete", {"id": row["id"]}),
            )
            for index, row in enumerate(diff["delete"])
        ]
        _run_batches(deletes, settings=settings, webhook_base=webhook_base)

        readback = _list_product_rows(
            item_id=_clean(order.bitrix_item_id), settings=settings, webhook_base=webhook_base
        )
        if (
            len(readback) != len(desired)
            or procurement_product_rows_checksum([_managed_row(row) for row in readback])
            != checksum
        ):
            raise ProcurementProductRowsSyncError(
                "Readback товаров Bitrix не совпадает с каноническими строками 1С"
            )

        synced_at = datetime.now(UTC).replace(tzinfo=None)
        order.bitrix_product_rows_sync_state = "synced"
        order.bitrix_product_rows_checksum = checksum
        order.bitrix_product_rows_expected_count = len(desired)
        order.bitrix_product_rows_synced_count = len(readback)
        order.bitrix_product_rows_synced_at = synced_at
        order.bitrix_product_rows_error = None
        after = _product_rows_state(order)
        _audit(
            db,
            order=order,
            event_type="bitrix_product_rows_synced",
            before=before,
            after=after,
            actor=actor,
        )
        return {**result, "state": "synced", "synced_count": len(readback)}
    except Exception as exc:
        message = _safe_error_message(exc)
        if apply:
            expected_count = sum(1 for line in order.lines if not line.removed)
            order.bitrix_product_rows_sync_state = "error"
            order.bitrix_product_rows_expected_count = expected_count
            order.bitrix_product_rows_error = message
            after = _product_rows_state(order)
            _audit(
                db,
                order=order,
                event_type="bitrix_product_rows_sync_failed",
                before=before,
                after=after,
                actor=actor,
            )
        return {
            "state": "error",
            "order_id": order.id,
            "item_id": _clean(order.bitrix_item_id),
            "expected_count": sum(1 for line in order.lines if not line.removed),
            "error": message,
        }


def preflight_procurement_product_rows(
    *, item_id: str, settings: Settings | None = None, webhook_base: str = ""
) -> dict[str, Any]:
    settings = settings or get_settings()
    methods_payload = bitrix_call("methods", {}, settings=settings, webhook_base=webhook_base)
    methods = set(methods_payload.get("result") or [])
    missing_methods = sorted(PRODUCT_ROW_METHODS - methods)
    if missing_methods:
        raise ProcurementProductRowsSyncError(
            "Bitrix не предоставляет методы: " + ", ".join(missing_methods)
        )
    rows = _list_product_rows(item_id=item_id, settings=settings, webhook_base=webhook_base)
    return {
        "ok": True,
        "item_id": str(item_id),
        "owner_type": PRODUCT_ROW_OWNER_TYPE,
        "readable_rows": len(rows),
        "methods": sorted(PRODUCT_ROW_METHODS),
    }


def summarize_product_row_sync(results: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = defaultdict(int)
    for result in results:
        summary[_clean(result.get("state")) or "unknown"] += 1
    return dict(sorted(summary.items()))
