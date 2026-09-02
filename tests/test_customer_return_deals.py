from __future__ import annotations

from typing import Any

import pytest

from app.services.customer_return_deals import (
    CustomerReturnDealUnavailable,
    get_customer_return_deal,
    search_customer_return_deals,
)


class FakeBitrixClient:
    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        if method == "crm.deal.get":
            return {"result": _deal(3507, title="Возврат 3507", order_ref="241094")}
        if method == "crm.deal.list":
            crm_filter = params["filter"]
            if "%TITLE" in crm_filter:
                return {
                    "result": [
                        _deal(44, title="Возврат 3507", order_ref="241099", closed=True),
                        _deal(3507, title="Возврат 3507", order_ref="241094"),
                    ]
                }
            return {"result": [_deal(3507, title="Возврат 3507", order_ref="241094")]}
        if method == "crm.contact.list":
            return {"result": [{"ID": "7", "NAME": "Иван", "LAST_NAME": "Петров"}]}
        if method == "crm.company.list":
            return {"result": []}
        if method == "user.get":
            return {"result": [{"ID": "9", "NAME": "Анна", "LAST_NAME": "Смирнова"}]}
        if method == "crm.status.list":
            return {"result": [{"STATUS_ID": "NEW", "NAME": "Новая"}]}
        raise AssertionError(f"unexpected Bitrix method {method}")


def _deal(
    deal_id: int,
    *,
    title: str,
    order_ref: str,
    closed: bool = False,
) -> dict[str, str]:
    return {
        "ID": str(deal_id),
        "TITLE": title,
        "UF_CRM_1772784329053": order_ref,
        "STAGE_ID": "NEW",
        "CATEGORY_ID": "0",
        "CLOSED": "Y" if closed else "N",
        "DATE_CREATE": "2026-09-01T10:00:00+03:00",
        "CONTACT_ID": "7",
        "COMPANY_ID": "",
        "ASSIGNED_BY_ID": "9",
    }


def test_search_customer_return_deals_ranks_exact_id_and_keeps_closed() -> None:
    deals = search_customer_return_deals(
        webhook_url=None,
        search="3507",
        limit=20,
        client=FakeBitrixClient(),
    )

    assert [deal.deal_id for deal in deals] == [3507, 44]
    assert deals[0].order_ref == "241094"
    assert deals[0].stage_name == "Новая"
    assert deals[0].contact_name == "Петров Иван"
    assert deals[0].responsible_name == "Смирнова Анна"
    assert deals[1].closed is True


def test_get_customer_return_deal_returns_trusted_snapshot() -> None:
    deal = get_customer_return_deal(
        webhook_url=None,
        deal_id=3507,
        client=FakeBitrixClient(),
    )

    assert deal.deal_id == 3507
    assert deal.title == "Возврат 3507"
    assert deal.order_ref == "241094"


def test_search_requires_configured_webhook_without_injected_client() -> None:
    with pytest.raises(CustomerReturnDealUnavailable, match="not configured"):
        search_customer_return_deals(webhook_url=None, search="3507")
