from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.services.customer_return_service_requests import (
    CustomerReturnServiceRequestUnavailable,
    get_customer_return_service_request,
    search_customer_return_service_requests,
)
from app.services.site_order_fulfillment import BitrixChatError


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        site_service_requests_bitrix_webhook_url=None,
        site_service_requests_bitrix_entity_type_id=1134,
        site_service_requests_bitrix_working_category_id=55,
        site_service_requests_bitrix_field_map={"site_ticket_id": "UF_CRM_36_SITE_TICKET_ID"},
    )


def _request(
    item_id: int,
    *,
    title: str,
    stage_id: str = "DT1134_55:NEW",
    deal_id: int = 3507,
    order_ref: str = "241094",
    ticket_id: str = "7001",
) -> dict[str, Any]:
    return {
        "id": item_id,
        "title": title,
        "stageId": stage_id,
        "categoryId": 55,
        "assignedById": 88,
        "ufCrm36Crmdeal": deal_id,
        "ufCrm36Orderrefs": order_ref,
        "ufCrm36SiteTicketId": ticket_id,
    }


class FakeBitrixClient:
    def __init__(self) -> None:
        self.filters: list[dict[str, Any]] = []

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        if method == "crm.item.get":
            return {"result": {"item": _request(113401, title="Возврат по заказу 241094")}}
        if method == "crm.item.list":
            item_filter = params["filter"]
            self.filters.append(item_filter)
            if item_filter == {"ufCrm36Crmdeal": 3507}:
                return {
                    "result": {
                        "items": [
                            _request(113401, title="Возврат по заказу 241094"),
                            _request(
                                113402,
                                title="Закрытое обращение",
                                stage_id="DT1134_55:SUCCESS",
                                ticket_id="7002",
                            ),
                        ]
                    }
                }
            if "%title" in item_filter:
                return {"result": {"items": [_request(113401, title="Возврат по заказу 241094")]}}
            if "%ufCrm36SiteTicketId" in item_filter:
                return {
                    "result": {
                        "items": [
                            _request(
                                113402,
                                title="Закрытое обращение",
                                stage_id="DT1134_55:SUCCESS",
                                ticket_id="7002",
                            )
                        ]
                    }
                }
        if method == "user.get":
            return {"result": [{"ID": 88, "NAME": "Анна", "LAST_NAME": "Смирнова"}]}
        if method == "crm.status.list":
            return {
                "result": [
                    {
                        "STATUS_ID": "DT1134_55:NEW",
                        "NAME": "Новое",
                        "SEMANTICS": "P",
                    },
                    {
                        "STATUS_ID": "DT1134_55:SUCCESS",
                        "NAME": "Закрыто",
                        "SEMANTICS": "S",
                    },
                ]
            }
        raise AssertionError(f"unexpected Bitrix method {method}: {params}")


def test_search_service_requests_by_deal_keeps_active_and_closed() -> None:
    client = FakeBitrixClient()

    requests = search_customer_return_service_requests(
        settings=_settings(),
        deal_id=3507,
        client=client,
    )

    assert [item.item_id for item in requests] == [113402, 113401]
    assert requests[0].closed is True
    assert requests[0].stage_name == "Закрыто"
    assert requests[1].closed is False
    assert requests[1].deal_id == 3507
    assert requests[1].order_ref == "241094"
    assert requests[1].responsible_name == "Смирнова Анна"
    assert requests[1].site_ticket_id == "7001"


def test_search_service_requests_uses_id_title_and_ticket_filters() -> None:
    client = FakeBitrixClient()

    requests = search_customer_return_service_requests(
        settings=_settings(),
        search="113401",
        client=client,
    )

    assert {item.item_id for item in requests} == {113401, 113402}
    assert {next(iter(item_filter)) for item_filter in client.filters} == {
        "%title",
        "%ufCrm36SiteTicketId",
    }


def test_get_service_request_returns_trusted_snapshot() -> None:
    request = get_customer_return_service_request(
        settings=_settings(),
        item_id=113401,
        client=FakeBitrixClient(),
    )

    assert request.item_id == 113401
    assert request.title == "Возврат по заказу 241094"
    assert request.deal_id == 3507
    assert request.order_ref == "241094"
    assert request.responsible_name == "Смирнова Анна"


class FailingBitrixClient:
    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        raise BitrixChatError("Bitrix24 is unavailable")


def test_service_request_search_translates_bitrix_failure() -> None:
    with pytest.raises(CustomerReturnServiceRequestUnavailable, match="temporarily unavailable"):
        search_customer_return_service_requests(
            settings=_settings(),
            deal_id=3507,
            client=FailingBitrixClient(),
        )
