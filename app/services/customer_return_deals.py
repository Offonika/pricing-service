from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.customer_returns import CustomerReturnDealLink
from app.services.site_order_fulfillment import BitrixChatClient, BitrixChatError

CRM_ORDER_NUMBER_FIELD = "UF_CRM_1772784329053"
CRM_DEAL_SELECT_FIELDS = (
    "ID",
    "TITLE",
    "STAGE_ID",
    "CATEGORY_ID",
    "CLOSED",
    "DATE_CREATE",
    "CONTACT_ID",
    "COMPANY_ID",
    "ASSIGNED_BY_ID",
    CRM_ORDER_NUMBER_FIELD,
)


class CustomerReturnDealUnavailable(RuntimeError):
    pass


class CustomerReturnDealNotFound(RuntimeError):
    pass


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _integer(value: Any) -> int | None:
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _date(value: Any) -> datetime | None:
    text = _clean(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    result = response.get("result") or []
    if not isinstance(result, list):
        raise CustomerReturnDealUnavailable("Bitrix24 returned an invalid CRM response")
    return [row for row in result if isinstance(row, dict)]


def _client(webhook_url: str | None, client: BitrixChatClient | None) -> BitrixChatClient:
    if client is not None:
        return client
    if not _clean(webhook_url):
        raise CustomerReturnDealUnavailable("Bitrix24 deal search is not configured")
    return BitrixChatClient(str(webhook_url), timeout=15.0)


def _call(client: BitrixChatClient, method: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        return client.call(method, params)
    except BitrixChatError as exc:
        raise CustomerReturnDealUnavailable("Bitrix24 CRM is temporarily unavailable") from exc


def _display_name(row: dict[str, Any], *, title_field: str | None = None) -> str | None:
    if title_field:
        title = _clean(row.get(title_field))
        if title:
            return title
    parts = [_clean(row.get(key)) for key in ("LAST_NAME", "NAME", "SECOND_NAME")]
    return " ".join(part for part in parts if part) or None


def _directory(
    client: BitrixChatClient,
    method: str,
    ids: set[int],
    *,
    title_field: str | None = None,
) -> dict[int, str]:
    if not ids:
        return {}
    if method == "user.get":
        params: dict[str, Any] = {"ID": sorted(ids)}
    else:
        params = {
            "filter": {"ID": sorted(ids)},
            "select": ["ID", "TITLE", "NAME", "LAST_NAME", "SECOND_NAME"],
        }
    rows = _rows(_call(client, method, params))
    result: dict[int, str] = {}
    for row in rows:
        item_id = _integer(row.get("ID"))
        name = _display_name(row, title_field=title_field)
        if item_id is not None and name:
            result[item_id] = name
    return result


def _stage_directory(
    client: BitrixChatClient,
    rows: list[dict[str, Any]],
) -> dict[str, str]:
    categories = {_integer(row.get("CATEGORY_ID")) or 0 for row in rows}
    result: dict[str, str] = {}
    for category_id in categories:
        entity_id = "DEAL_STAGE" if category_id == 0 else f"DEAL_STAGE_{category_id}"
        response = _call(
            client,
            "crm.status.list",
            {"filter": {"ENTITY_ID": entity_id}, "order": {"SORT": "ASC"}},
        )
        for stage in _rows(response):
            stage_id = _clean(stage.get("STATUS_ID"))
            stage_name = _clean(stage.get("NAME"))
            if stage_id and stage_name:
                result[stage_id] = stage_name
                result[f"C{category_id}:{stage_id}"] = stage_name
    return result


def _enrich(
    client: BitrixChatClient,
    rows: list[dict[str, Any]],
) -> list[CustomerReturnDealLink]:
    contact_names = _directory(
        client,
        "crm.contact.list",
        {value for row in rows if (value := _integer(row.get("CONTACT_ID")))},
    )
    company_names = _directory(
        client,
        "crm.company.list",
        {value for row in rows if (value := _integer(row.get("COMPANY_ID")))},
        title_field="TITLE",
    )
    responsible_names = _directory(
        client,
        "user.get",
        {value for row in rows if (value := _integer(row.get("ASSIGNED_BY_ID")))},
    )
    stage_names = _stage_directory(client, rows)
    result: list[CustomerReturnDealLink] = []
    for row in rows:
        deal_id = _integer(row.get("ID"))
        if deal_id is None:
            continue
        contact_id = _integer(row.get("CONTACT_ID"))
        company_id = _integer(row.get("COMPANY_ID"))
        responsible_id = _integer(row.get("ASSIGNED_BY_ID"))
        stage_id = _clean(row.get("STAGE_ID"))
        result.append(
            CustomerReturnDealLink(
                deal_id=deal_id,
                title=_clean(row.get("TITLE")) or f"Сделка #{deal_id}",
                order_ref=_clean(row.get(CRM_ORDER_NUMBER_FIELD)),
                stage_id=stage_id,
                stage_name=stage_names.get(stage_id or ""),
                closed=str(row.get("CLOSED") or "N").upper() == "Y",
                created_at=_date(row.get("DATE_CREATE")),
                contact_id=contact_id,
                contact_name=contact_names.get(contact_id) if contact_id else None,
                company_id=company_id,
                company_name=company_names.get(company_id) if company_id else None,
                responsible_user_id=responsible_id,
                responsible_name=responsible_names.get(responsible_id) if responsible_id else None,
            )
        )
    return result


def _rank(row: dict[str, Any], search: str) -> tuple[int, float, int]:
    query = search.casefold()
    deal_id = _integer(row.get("ID")) or 0
    title = (_clean(row.get("TITLE")) or "").casefold()
    order_ref = (_clean(row.get(CRM_ORDER_NUMBER_FIELD)) or "").casefold()
    if search.isdigit() and deal_id == int(search):
        score = 0
    elif order_ref == query:
        score = 1
    elif title == query:
        score = 2
    elif order_ref.startswith(query) or title.startswith(query):
        score = 3
    else:
        score = 4
    created_at = _date(row.get("DATE_CREATE"))
    timestamp = created_at.timestamp() if created_at else 0.0
    return score, -timestamp, -deal_id


def search_customer_return_deals(
    *,
    webhook_url: str | None,
    search: str,
    limit: int = 20,
    client: BitrixChatClient | None = None,
) -> list[CustomerReturnDealLink]:
    query = search.strip()
    if len(query) < 2:
        return []
    crm = _client(webhook_url, client)
    candidates: dict[int, dict[str, Any]] = {}
    if query.isdigit():
        try:
            response = _call(crm, "crm.deal.get", {"id": int(query)})
        except CustomerReturnDealUnavailable:
            response = {}
        row = response.get("result")
        if isinstance(row, dict) and (deal_id := _integer(row.get("ID"))):
            candidates[deal_id] = row
    for crm_filter in ({"%TITLE": query}, {f"%{CRM_ORDER_NUMBER_FIELD}": query}):
        response = _call(
            crm,
            "crm.deal.list",
            {
                "filter": crm_filter,
                "select": list(CRM_DEAL_SELECT_FIELDS),
                "order": {"DATE_CREATE": "DESC", "ID": "DESC"},
                "start": 0,
            },
        )
        for row in _rows(response):
            if deal_id := _integer(row.get("ID")):
                candidates[deal_id] = row
    bounded = sorted(candidates.values(), key=lambda row: _rank(row, query))[:limit]
    return _enrich(crm, bounded)


def get_customer_return_deal(
    *,
    webhook_url: str | None,
    deal_id: int,
    client: BitrixChatClient | None = None,
) -> CustomerReturnDealLink:
    crm = _client(webhook_url, client)
    try:
        response = crm.call("crm.deal.get", {"id": deal_id})
    except BitrixChatError as exc:
        message = str(exc).casefold()
        if "not_found" in message or "not found" in message:
            raise CustomerReturnDealNotFound(f"Bitrix24 deal {deal_id} was not found") from exc
        raise CustomerReturnDealUnavailable("Bitrix24 CRM is temporarily unavailable") from exc
    row = response.get("result")
    if not isinstance(row, dict):
        raise CustomerReturnDealNotFound(f"Bitrix24 deal {deal_id} was not found")
    links = _enrich(crm, [row])
    if not links:
        raise CustomerReturnDealNotFound(f"Bitrix24 deal {deal_id} was not found")
    return links[0]
