from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.services.customer_returns import CustomerReturnServiceRequestLink
from app.services.site_order_fulfillment import BitrixChatClient, BitrixChatError

CRM_DEAL_FIELD = "ufCrm36Crmdeal"
ORDER_REF_FIELD = "ufCrm36Orderrefs"


class CustomerReturnServiceRequestUnavailable(RuntimeError):
    pass


class CustomerReturnServiceRequestNotFound(RuntimeError):
    pass


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("id") or value.get("ID") or value.get("value")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _integer(value: Any) -> int | None:
    text = _clean(value)
    if text is None:
        return None
    try:
        result = int(text)
    except ValueError:
        return None
    return result if result > 0 else None


def _item_field_name(value: str) -> str:
    normalized = str(value).strip()
    if not normalized.upper().startswith("UF_"):
        return normalized
    parts = [part for part in normalized.lower().split("_") if part]
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def _site_ticket_field(settings: Settings) -> str | None:
    value = settings.site_service_requests_bitrix_field_map.get("site_ticket_id")
    return _item_field_name(value) if value else None


def _client(
    settings: Settings,
    client: BitrixChatClient | None,
) -> BitrixChatClient:
    if client is not None:
        return client
    webhook = (
        settings.site_service_requests_bitrix_webhook_url
        or settings.customer_return_bitrix_webhook_url
    )
    if not _clean(webhook):
        raise CustomerReturnServiceRequestUnavailable(
            "Bitrix24 service request search is not configured"
        )
    return BitrixChatClient(str(webhook), timeout=15.0)


def _call(
    client: BitrixChatClient,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    try:
        return client.call(method, params)
    except BitrixChatError as exc:
        raise CustomerReturnServiceRequestUnavailable(
            "Bitrix24 service requests are temporarily unavailable"
        ) from exc


def _list_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    result = response.get("result")
    items = result.get("items") if isinstance(result, dict) else None
    if not isinstance(items, list):
        raise CustomerReturnServiceRequestUnavailable(
            "Bitrix24 returned an invalid service request response"
        )
    return [item for item in items if isinstance(item, dict)]


def _get_item(response: dict[str, Any]) -> dict[str, Any] | None:
    result = response.get("result")
    item = result.get("item") if isinstance(result, dict) else None
    return item if isinstance(item, dict) else None


def _directory(client: BitrixChatClient, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    response = _call(client, "user.get", {"ID": sorted(user_ids)})
    rows = response.get("result")
    if not isinstance(rows, list):
        raise CustomerReturnServiceRequestUnavailable("Bitrix24 returned an invalid user response")
    result: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        user_id = _integer(row.get("ID") or row.get("id"))
        parts = [
            _clean(row.get("LAST_NAME") or row.get("lastName")),
            _clean(row.get("NAME") or row.get("name")),
            _clean(row.get("SECOND_NAME") or row.get("secondName")),
        ]
        name = " ".join(part for part in parts if part)
        if user_id is not None and name:
            result[user_id] = name
    return result


def _stage_directory(
    client: BitrixChatClient,
    *,
    entity_type_id: int,
    category_ids: set[int],
) -> dict[tuple[int, str], tuple[str, bool]]:
    result: dict[tuple[int, str], tuple[str, bool]] = {}
    for category_id in category_ids:
        response = _call(
            client,
            "crm.status.list",
            {"filter": {"ENTITY_ID": f"DYNAMIC_{entity_type_id}_STAGE_{category_id}"}},
        )
        rows = response.get("result")
        if not isinstance(rows, list):
            raise CustomerReturnServiceRequestUnavailable(
                "Bitrix24 returned an invalid service request stage response"
            )
        for row in rows:
            if not isinstance(row, dict):
                continue
            stage_id = _clean(row.get("STATUS_ID") or row.get("statusId"))
            name = _clean(row.get("NAME") or row.get("name"))
            semantics = (_clean(row.get("SEMANTICS") or row.get("semantics")) or "").upper()
            if stage_id and name:
                result[(category_id, stage_id)] = (name, semantics in {"S", "F"})
    return result


def _enrich(
    client: BitrixChatClient,
    rows: list[dict[str, Any]],
    *,
    settings: Settings,
) -> list[CustomerReturnServiceRequestLink]:
    user_ids = {
        user_id
        for row in rows
        if (user_id := _integer(row.get("assignedById") or row.get("ASSIGNED_BY_ID")))
    }
    names = _directory(client, user_ids)
    category_ids = {
        _integer(row.get("categoryId") or row.get("CATEGORY_ID"))
        or settings.site_service_requests_bitrix_working_category_id
        for row in rows
    }
    stages = _stage_directory(
        client,
        entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
        category_ids=category_ids,
    )
    ticket_field = _site_ticket_field(settings)
    result: list[CustomerReturnServiceRequestLink] = []
    for row in rows:
        item_id = _integer(row.get("id") or row.get("ID"))
        if item_id is None:
            continue
        category_id = (
            _integer(row.get("categoryId") or row.get("CATEGORY_ID"))
            or settings.site_service_requests_bitrix_working_category_id
        )
        stage_id = _clean(row.get("stageId") or row.get("STAGE_ID"))
        stage_name, closed = stages.get((category_id, stage_id or ""), (None, False))
        responsible_id = _integer(row.get("assignedById") or row.get("ASSIGNED_BY_ID"))
        result.append(
            CustomerReturnServiceRequestLink(
                item_id=item_id,
                title=_clean(row.get("title") or row.get("TITLE"))
                or f"Сервисное обращение #{item_id}",
                stage_id=stage_id,
                stage_name=stage_name,
                closed=closed,
                category_id=category_id,
                deal_id=_integer(row.get(CRM_DEAL_FIELD) or row.get("UF_CRM_36_CRMDEAL")),
                order_ref=_clean(row.get(ORDER_REF_FIELD) or row.get("UF_CRM_36_ORDERREFS")),
                responsible_user_id=responsible_id,
                responsible_name=names.get(responsible_id) if responsible_id else None,
                site_ticket_id=(
                    _clean(row.get(ticket_field)) if ticket_field is not None else None
                ),
            )
        )
    return result


def _select_fields(settings: Settings) -> list[str]:
    fields = [
        "id",
        "title",
        "stageId",
        "categoryId",
        "assignedById",
        CRM_DEAL_FIELD,
        ORDER_REF_FIELD,
    ]
    ticket_field = _site_ticket_field(settings)
    if ticket_field:
        fields.append(ticket_field)
    return fields


def search_customer_return_service_requests(
    *,
    settings: Settings,
    search: str | None = None,
    deal_id: int | None = None,
    limit: int = 20,
    client: BitrixChatClient | None = None,
) -> list[CustomerReturnServiceRequestLink]:
    query = (search or "").strip()
    if deal_id is None and len(query) < 2:
        return []
    crm = _client(settings, client)
    entity_type_id = settings.site_service_requests_bitrix_entity_type_id
    candidates: dict[int, dict[str, Any]] = {}

    if query.isdigit():
        response = _call(
            crm,
            "crm.item.get",
            {"entityTypeId": entity_type_id, "id": int(query)},
        )
        item = _get_item(response)
        item_id = _integer(item.get("id") or item.get("ID")) if item else None
        if item is not None and item_id is not None:
            candidates[item_id] = item

    filters: list[dict[str, Any]] = []
    if deal_id is not None:
        filters.append({CRM_DEAL_FIELD: deal_id})
    if len(query) >= 2:
        filters.append({"%title": query})
        ticket_field = _site_ticket_field(settings)
        if ticket_field:
            filters.append({f"%{ticket_field}": query})
    for item_filter in filters:
        response = _call(
            crm,
            "crm.item.list",
            {
                "entityTypeId": entity_type_id,
                "filter": item_filter,
                "select": _select_fields(settings),
                "order": {"id": "DESC"},
                "start": 0,
            },
        )
        for item in _list_items(response):
            item_id = _integer(item.get("id") or item.get("ID"))
            if item_id is not None:
                candidates[item_id] = item

    rows = list(candidates.values())
    if deal_id is not None:
        rows = [
            row
            for row in rows
            if _integer(row.get(CRM_DEAL_FIELD) or row.get("UF_CRM_36_CRMDEAL")) == deal_id
        ]
    rows.sort(key=lambda row: -(_integer(row.get("id") or row.get("ID")) or 0))
    return _enrich(crm, rows[:limit], settings=settings)


def get_customer_return_service_request(
    *,
    settings: Settings,
    item_id: int,
    client: BitrixChatClient | None = None,
) -> CustomerReturnServiceRequestLink:
    crm = _client(settings, client)
    response = _call(
        crm,
        "crm.item.get",
        {
            "entityTypeId": settings.site_service_requests_bitrix_entity_type_id,
            "id": item_id,
        },
    )
    item = _get_item(response)
    actual_id = _integer(item.get("id") or item.get("ID")) if item else None
    if item is None or actual_id != item_id:
        raise CustomerReturnServiceRequestNotFound(
            f"Bitrix24 service request {item_id} was not found"
        )
    enriched = _enrich(crm, [item], settings=settings)
    if not enriched:
        raise CustomerReturnServiceRequestNotFound(
            f"Bitrix24 service request {item_id} was not found"
        )
    return enriched[0]
