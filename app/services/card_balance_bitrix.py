from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import date, datetime
from decimal import Decimal
from email.message import Message
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models import CardBalanceReconciliation
from app.services import card_balance_reconciliation as reconciliation_service
from app.services.card_balance_ocr import CardBalanceOCRClient, ocr_is_available
from app.services.card_balance_onec import clean_string, decimal_or_none

STAGE_WAITING_SCREENSHOT = "waiting_screenshot"
STAGE_SCREENSHOT_RECEIVED = "screenshot_received"
STAGE_RECOGNITION = "recognition"
STAGE_MATCHED = "matched"
STAGE_MISMATCH = "mismatch"
STAGE_MANUAL_REVIEW = "manual_review"
STAGE_CLOSED_FINCONTROL = "closed_fincontrol"
STAGE_OVERDUE = "overdue"
STAGE_CANCELLED = "cancelled"

STATUS_TO_STAGE_KEY = {
    reconciliation_service.STATUS_MATCHED: STAGE_MATCHED,
    reconciliation_service.STATUS_MISMATCH: STAGE_MISMATCH,
    reconciliation_service.STATUS_LOW_CONFIDENCE: STAGE_MANUAL_REVIEW,
    reconciliation_service.STATUS_STALE_SCREENSHOT: STAGE_OVERDUE,
    reconciliation_service.STATUS_MISSING_SCREENSHOT: STAGE_WAITING_SCREENSHOT,
    reconciliation_service.STATUS_MISSING_ONEC_BALANCE: STAGE_MANUAL_REVIEW,
    reconciliation_service.STATUS_UNMAPPED_CARD: STAGE_MANUAL_REVIEW,
    reconciliation_service.STATUS_AMBIGUOUS_MAPPING: STAGE_MANUAL_REVIEW,
    reconciliation_service.STATUS_CLOSED_FINCONTROL: STAGE_CLOSED_FINCONTROL,
    reconciliation_service.STATUS_CANCELLED: STAGE_CANCELLED,
}

READ_ITEM_FIELDS = [
    "id",
    "title",
    "stageId",
    "createdTime",
    "updatedTime",
    "movedTime",
]

BUILTIN_FIELD_MAP = {
    "assigned_by": "ASSIGNED_BY_ID",
}


def _base_filter(settings: Settings, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    base: dict[str, Any] = {}
    if settings.card_balance_bitrix_category_id:
        base["categoryId"] = settings.card_balance_bitrix_category_id
    if extra:
        base.update(extra)
    return base


def bitrix_call(
    webhook_url: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: int = 60,
) -> dict[str, Any]:
    body = urllib.parse.urlencode(_flatten_params(params or {})).encode("utf-8")
    request = urllib.request.Request(
        webhook_url.rstrip("/") + f"/{method}.json",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        parsed = json.loads(response.read().decode("utf-8"))
    if parsed.get("error"):
        raise RuntimeError(
            f"Bitrix API {method}: {parsed['error']} {parsed.get('error_description', '')}".strip()
        )
    return parsed


def _flatten_params(params: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        pairs.extend(_flatten_param(key, value))
    return pairs


def _flatten_param(prefix: str, value: Any) -> list[tuple[str, str]]:
    if isinstance(value, dict):
        result: list[tuple[str, str]] = []
        for child_key, child_value in value.items():
            result.extend(_flatten_param(f"{prefix}[{child_key}]", child_value))
        return result
    if isinstance(value, list):
        result = []
        for child_value in value:
            result.extend(_flatten_param(f"{prefix}[]", child_value))
        return result
    if value is None:
        return [(prefix, "")]
    if isinstance(value, bool):
        return [(prefix, "Y" if value else "N")]
    return [(prefix, str(value))]


def rest_field_name(field_name: str) -> str:
    if not field_name.upper().startswith("UF_"):
        return field_name
    parts = field_name.lower().split("_")
    head, *tail = parts
    return head + "".join(part.capitalize() for part in tail)


def _rest_field_name_with_separator(field_name: str) -> str:
    if not field_name.upper().startswith("UF_"):
        return field_name
    tail = field_name[3:]
    if "_" not in tail:
        return "uf" + tail.capitalize()
    first, remainder = tail.split("_", 1)
    return "uf" + first.capitalize() + "_" + remainder


def _field_read_candidates(field_name: str) -> list[str]:
    candidates = [
        field_name,
        rest_field_name(field_name),
        _rest_field_name_with_separator(field_name),
    ]
    if field_name.upper().startswith("UF_"):
        candidates.append(field_name.lower())
    # keep order, remove duplicates
    return list(dict.fromkeys(candidates))


def _field_select_candidates(field_name: str) -> list[str]:
    if not field_name.upper().startswith("UF_"):
        return [field_name]
    candidates = [
        rest_field_name(field_name),
        _rest_field_name_with_separator(field_name),
        field_name,
    ]
    return list(dict.fromkeys(candidates))


CARD_LAST4_DYNAMIC_FIELD_RE = re.compile(r"^UF_CRM_\d+_CARDLAST4$", re.IGNORECASE)


def _field_write_name(field_name: str, *, logical_key: str | None = None) -> str:
    # Dynamic UF_CRM_<type_id>_CARDLAST4 accepts write payload in `ufCrm_<type_id>_...` form.
    if logical_key == "card_last4" and CARD_LAST4_DYNAMIC_FIELD_RE.match(field_name):
        return _rest_field_name_with_separator(field_name)
    if field_name.upper().startswith("UF_"):
        return rest_field_name(field_name)
    return field_name


def _field(settings: Settings, logical_key: str) -> str | None:
    return settings.card_balance_bitrix_field_map.get(logical_key) or BUILTIN_FIELD_MAP.get(
        logical_key
    )


def _default_assigned_by_id(settings: Settings) -> int | None:
    webhook = clean_string(settings.card_balance_bitrix_webhook_url)
    if not webhook:
        return None
    match = re.search(r"/rest/(\d+)/", webhook)
    if not match:
        return None
    value = clean_string(match.group(1))
    if not value or not value.isdigit():
        return None
    return int(value)


def _item_get(item: dict[str, Any], settings: Settings, logical_key: str) -> Any:
    field = _field(settings, logical_key)
    if not field:
        return None
    for key in _field_read_candidates(field):
        if key in item:
            return item.get(key)
    return None


def _item_file_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list) and value:
        return _item_file_id(value[0])
    if isinstance(value, dict):
        return clean_string(value.get("id") or value.get("ID") or value.get("fileId"))
    return clean_string(value)


def _item_file_meta(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, list):
        for item in value:
            resolved = _item_file_meta(item)
            if resolved:
                return resolved
        return None
    if isinstance(value, dict):
        return value
    return None


def _item_employee_user_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        for item in value:
            resolved = _item_employee_user_id(item)
            if resolved:
                return resolved
        return None
    if isinstance(value, dict):
        return clean_string(
            value.get("ID") or value.get("id") or value.get("VALUE") or value.get("value")
        )
    if isinstance(value, (int, float)):
        return str(int(value))
    return clean_string(value)


def decode_bitrix_item(item: dict[str, Any], *, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    business_date = _date_value(_item_get(item, settings, "business_date"))
    employee_user_id = _item_employee_user_id(_item_get(item, settings, "employee_user"))
    employee_name = clean_string(_item_get(item, settings, "employee_name"))
    payload = {
        "external_id": f"bitrix:{item.get('id')}",
        "business_date": business_date.isoformat() if business_date else None,
        "employee_id": employee_user_id or clean_string(_item_get(item, settings, "employee_id")),
        "employee_name": employee_name,
        "employee_last_name": clean_string(_item_get(item, settings, "employee_last_name"))
        or _last_name(employee_name),
        "card_last4": clean_string(_item_get(item, settings, "card_last4")),
        "onec_cashbox_code": clean_string(_item_get(item, settings, "onec_cashbox_code")),
        "onec_cashbox_name": clean_string(_item_get(item, settings, "onec_cashbox_name")),
        "bitrix_item_id": clean_string(item.get("id")),
        "bitrix_stage_id": clean_string(item.get("stageId")),
        "screenshot_file_id": _item_file_id(_item_get(item, settings, "screenshot_file")),
        "manual_balance": decimal_or_none(_item_get(item, settings, "manual_balance")),
        "recognized_balance": decimal_or_none(_item_get(item, settings, "recognized_balance")),
        "recognition_confidence": decimal_or_none(
            _item_get(item, settings, "recognition_confidence")
        ),
        "resolution_comment": clean_string(_item_get(item, settings, "resolution_comment")),
        "reviewer_id": clean_string(_item_get(item, settings, "reviewer_id")),
        "due_at": _datetime_str(_item_get(item, settings, "due_at")),
        "source_channel": "bitrix",
        "raw_bitrix_item": item,
    }
    if payload["business_date"] is None:
        payload["business_date"] = date.today().isoformat()
    return payload


def build_bitrix_update_fields(
    row: CardBalanceReconciliation,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    fields: dict[str, Any] = {}
    stage_id = _stage_id_for_row(row, settings=settings)
    if stage_id:
        fields["stageId"] = stage_id

    values = {
        "employee_user": row.employee_id,
        "employee_id": row.employee_id,
        "card_last4": row.card_last4,
        "employee_name": row.employee_name,
        "employee_last_name": row.employee_last_name,
        "recognized_balance": row.recognized_balance,
        "onec_balance": row.onec_balance,
        "diff_amount": row.diff_amount,
        "status": row.status,
        "recognition_confidence": row.recognition_confidence,
        "onec_cashbox_code": row.onec_cashbox_code,
        "onec_cashbox_name": row.onec_cashbox_name,
    }
    for logical_key, value in values.items():
        field = _field(settings, logical_key)
        if field:
            fields[_field_write_name(field, logical_key=logical_key)] = _format_field_value(
                logical_key, value
            )
    assigned_field = _field(settings, "assigned_by")
    if assigned_field:
        assigned_value = clean_string(row.employee_id)
        if assigned_value and assigned_value.isdigit():
            fields[_field_write_name(assigned_field, logical_key="assigned_by")] = int(
                assigned_value
            )
        else:
            fallback = _default_assigned_by_id(settings)
            if fallback is not None:
                fields[_field_write_name(assigned_field, logical_key="assigned_by")] = fallback
    return fields


def _stage_id_for_row(
    row: CardBalanceReconciliation,
    *,
    settings: Settings | None = None,
) -> str | None:
    settings = settings or get_settings()
    if (
        row.status == reconciliation_service.STATUS_LOW_CONFIDENCE
        and clean_string(row.screenshot_file_id)
        and row.manual_balance is None
        and row.recognized_balance is None
    ):
        recognition_stage = settings.card_balance_bitrix_stage_map.get(STAGE_RECOGNITION)
        if recognition_stage:
            return recognition_stage
    stage_key = STATUS_TO_STAGE_KEY.get(row.status)
    if not stage_key:
        return None
    return settings.card_balance_bitrix_stage_map.get(stage_key)


def _format_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _format_field_value(logical_key: str, value: Any) -> Any:
    if logical_key == "employee_user":
        normalized = clean_string(value)
        if normalized and normalized.isdigit():
            return int(normalized)
        return ""
    return _format_value(value)


def list_bitrix_items(
    *,
    settings: Settings | None = None,
    limit: int = 50,
    filters: dict[str, Any] | None = None,
    order: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    if not settings.card_balance_bitrix_webhook_url:
        raise RuntimeError("CARD_BALANCE_BITRIX_WEBHOOK_URL is not configured")
    if not settings.card_balance_bitrix_entity_type_id:
        raise RuntimeError("CARD_BALANCE_BITRIX_ENTITY_TYPE_ID is not configured")
    collected: list[dict[str, Any]] = []
    start: int | None = 0
    while len(collected) < limit:
        params: dict[str, Any] = {
            "entityTypeId": settings.card_balance_bitrix_entity_type_id,
            "select": READ_ITEM_FIELDS
            + list(
                dict.fromkeys(
                    key
                    for value in settings.card_balance_bitrix_field_map.values()
                    for key in _field_select_candidates(value)
                )
            ),
            "order": order or {"updatedTime": "DESC"},
            "filter": _base_filter(settings, filters),
        }
        if start is not None:
            params["start"] = start
        response = bitrix_call(
            settings.card_balance_bitrix_webhook_url,
            "crm.item.list",
            params,
        )
        items = (response.get("result") or {}).get("items") or []
        collected.extend(items)
        next_start = response.get("next")
        if next_start is None or not items:
            break
        start = int(next_start)
    return collected[:limit]


def get_bitrix_item(
    item_id: str | int,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    response = bitrix_call(
        settings.card_balance_bitrix_webhook_url or "",
        "crm.item.get",
        {
            "entityTypeId": settings.card_balance_bitrix_entity_type_id,
            "id": item_id,
        },
    )
    result = response.get("result") or {}
    item = result.get("item")
    if not isinstance(item, dict):
        raise RuntimeError(f"Bitrix item {item_id} not found")
    return item


def get_bitrix_item_screenshot_download_url(
    item: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> str | None:
    settings = settings or get_settings()
    meta = _item_file_meta(_item_get(item, settings, "screenshot_file"))
    if not meta:
        return None
    return clean_string(meta.get("urlMachine") or meta.get("url"))


def download_bitrix_item_screenshot(
    item: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> tuple[bytes, str]:
    settings = settings or get_settings()
    url = get_bitrix_item_screenshot_download_url(item, settings=settings)
    if not url:
        item_id = clean_string(item.get("id"))
        if not item_id:
            raise RuntimeError("Bitrix screenshot URL is missing")
        full_item = get_bitrix_item(item_id, settings=settings)
        url = get_bitrix_item_screenshot_download_url(full_item, settings=settings)
        if not url:
            raise RuntimeError(f"Bitrix screenshot URL is missing for item {item_id}")
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=60) as response:
        content = response.read()
        if len(content) > settings.card_balance_ocr_max_image_bytes:
            raise RuntimeError(
                f"Bitrix screenshot exceeds max size {settings.card_balance_ocr_max_image_bytes} bytes"
            )
        mime_type = _response_mime_type(response.headers) or "image/png"
    return content, mime_type


def list_bitrix_items_by_business_date(
    business_date: date,
    *,
    settings: Settings | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    business_date_field = _field(settings, "business_date")
    filters: dict[str, Any] = {}
    if business_date_field:
        filters[rest_field_name(business_date_field)] = business_date.isoformat()
    return list_bitrix_items(
        settings=settings,
        limit=limit,
        filters=filters,
        order={"id": "DESC"},
    )


def list_existing_cashbox_codes_for_business_date(
    business_date: date,
    *,
    settings: Settings | None = None,
    limit: int = 1000,
) -> set[str]:
    settings = settings or get_settings()
    items = list_bitrix_items_by_business_date(
        business_date,
        settings=settings,
        limit=limit,
    )
    codes: set[str] = set()
    for item in items:
        code = clean_string(_item_get(item, settings, "onec_cashbox_code"))
        if code:
            codes.add(code)
    return codes


def build_bitrix_daily_item_fields(
    *,
    business_date: date,
    onec_cashbox_code: str,
    onec_cashbox_name: str,
    card_last4: str | None = None,
    employee_id: str | None = None,
    employee_name: str | None = None,
    employee_last_name: str | None = None,
    assigned_by_id: str | int | None = None,
    status: str = reconciliation_service.STATUS_MISSING_SCREENSHOT,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    title = f"{business_date.strftime('%d.%m.%Y')} {onec_cashbox_name}"
    fields: dict[str, Any] = {}
    if settings.card_balance_bitrix_category_id:
        fields["categoryId"] = settings.card_balance_bitrix_category_id
    waiting_stage = settings.card_balance_bitrix_stage_map.get(STAGE_WAITING_SCREENSHOT)
    if waiting_stage:
        fields["stageId"] = waiting_stage
    values: dict[str, Any] = {
        "title": title,
        "business_date": business_date.isoformat(),
        "employee_user": employee_id,
        "employee_id": employee_id,
        "employee_name": employee_name,
        "employee_last_name": employee_last_name,
        "card_last4": card_last4,
        "onec_cashbox_code": onec_cashbox_code,
        "onec_cashbox_name": onec_cashbox_name,
        "status": status,
    }
    for logical_key, value in values.items():
        field = _field(settings, logical_key)
        if not field:
            continue
        fields[_field_write_name(field, logical_key=logical_key)] = _format_field_value(
            logical_key, value
        )
    assigned_field = _field(settings, "assigned_by")
    if assigned_field and assigned_by_id is not None:
        assigned_str = clean_string(assigned_by_id)
        if assigned_str and assigned_str.isdigit():
            fields[_field_write_name(assigned_field, logical_key="assigned_by")] = int(assigned_str)
    return fields


def create_bitrix_item(
    *,
    fields: dict[str, Any],
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    if not settings.card_balance_bitrix_webhook_url:
        raise RuntimeError("CARD_BALANCE_BITRIX_WEBHOOK_URL is not configured")
    if not settings.card_balance_bitrix_entity_type_id:
        raise RuntimeError("CARD_BALANCE_BITRIX_ENTITY_TYPE_ID is not configured")
    response = bitrix_call(
        settings.card_balance_bitrix_webhook_url,
        "crm.item.add",
        {
            "entityTypeId": settings.card_balance_bitrix_entity_type_id,
            "fields": fields,
        },
    )
    result = response.get("result") or {}
    item = result.get("item")
    if isinstance(item, dict):
        return item
    item_id = result.get("id") or result.get("itemId") or item
    if item_id is not None:
        return {"id": str(item_id)}
    raise RuntimeError("Bitrix API crm.item.add returned empty item")


def sync_bitrix_item(
    session: Session,
    *,
    item: dict[str, Any],
    decoded_payload: dict[str, Any] | None = None,
    onec_balances: dict[str, Decimal] | None = None,
    apply_ocr: bool = True,
    settings: Settings | None = None,
) -> CardBalanceReconciliation:
    settings = settings or get_settings()
    payload = decoded_payload or decode_bitrix_item(item, settings=settings)
    if apply_ocr:
        payload = _apply_ocr_to_payload(item=item, payload=payload, settings=settings)
    onec_balance = None
    cashbox_code = clean_string(payload.get("onec_cashbox_code"))
    if onec_balances is not None and cashbox_code:
        onec_balance = onec_balances.get(cashbox_code)
    row = reconciliation_service.upsert_reconciliation_from_payload(
        session,
        payload=payload,
        onec_balance=onec_balance,
        settings=settings,
    )
    if settings.card_balance_bitrix_webhook_url and settings.card_balance_bitrix_entity_type_id:
        update_and_mark_bitrix_item(session, row, settings=settings)
    return row


def _apply_ocr_to_payload(
    *,
    item: dict[str, Any],
    payload: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    if not ocr_is_available(settings):
        return payload
    if not clean_string(payload.get("screenshot_file_id")):
        return payload
    if decimal_or_none(payload.get("manual_balance")) is not None:
        return payload
    if decimal_or_none(payload.get("recognized_balance")) is not None:
        return payload
    item_id = clean_string(payload.get("bitrix_item_id") or item.get("id")) or "unknown"
    enriched = dict(payload)
    try:
        image_bytes, mime_type = download_bitrix_item_screenshot(item, settings=settings)
        result = CardBalanceOCRClient(settings=settings).extract_balance(
            image_bytes=image_bytes,
            mime_type=mime_type,
            item_title=clean_string(item.get("title")),
        )
    except Exception as exc:
        enriched["ocr_error"] = _truncate_error(str(exc))
        return enriched
    enriched["recognition_confidence"] = result.confidence
    enriched["ocr_evidence"] = result.evidence
    enriched["ocr_raw_response"] = result.raw_response_text
    if result.recognized_balance is not None:
        enriched["recognized_balance"] = result.recognized_balance
    else:
        enriched["ocr_error"] = (
            clean_string(enriched.get("ocr_error"))
            or f"OCR did not extract confident balance for item {item_id}"
        )
    return enriched


def update_and_mark_bitrix_item(
    session: Session,
    row: CardBalanceReconciliation,
    *,
    settings: Settings | None = None,
) -> None:
    try:
        update_bitrix_item(row, settings=settings)
    except Exception as exc:
        row.bitrix_last_error = _truncate_error(str(exc))
        reconciliation_service.append_event(
            session,
            row,
            event_type="bitrix_sync_error",
            source="bitrix",
            comment=row.bitrix_last_error,
            meta={"status": row.status, "bitrix_item_id": row.bitrix_item_id},
            idempotency_key=_bitrix_sync_event_key(row, "error"),
        )
        session.commit()
        raise
    row.bitrix_last_sync_at = reconciliation_service.utcnow().replace(tzinfo=None)
    row.bitrix_last_error = None
    reconciliation_service.append_event(
        session,
        row,
        event_type="bitrix_sync_success",
        source="bitrix",
        meta={"status": row.status, "bitrix_item_id": row.bitrix_item_id},
        idempotency_key=_bitrix_sync_event_key(row, "success"),
    )
    session.commit()
    session.refresh(row)


def update_bitrix_item(row: CardBalanceReconciliation, *, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if not row.bitrix_item_id:
        return
    fields = build_bitrix_update_fields(row, settings=settings)
    if not fields:
        return
    bitrix_call(
        settings.card_balance_bitrix_webhook_url or "",
        "crm.item.update",
        {
            "entityTypeId": settings.card_balance_bitrix_entity_type_id,
            "id": row.bitrix_item_id,
            "fields": fields,
        },
    )


def _bitrix_sync_event_key(row: CardBalanceReconciliation, suffix: str) -> str:
    return f"{row.external_id}:bitrix:{suffix}:{row.status}:{row.business_date.isoformat()}"


def _truncate_error(value: str) -> str:
    return value[:1000]


def _last_name(full_name: str | None) -> str | None:
    normalized = clean_string(full_name)
    if not normalized:
        return None
    return normalized.split()[-1]


def _date_value(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _datetime_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time()).isoformat()
    return str(value)


def _response_mime_type(headers: Message) -> str | None:
    content_type = headers.get_content_type()
    if not content_type:
        return None
    return clean_string(content_type)
