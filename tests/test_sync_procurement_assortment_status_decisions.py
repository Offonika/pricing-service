from __future__ import annotations

from copy import deepcopy
from typing import Any

from scripts.sync_procurement_assortment_status_decisions import (
    collect_decisions,
    list_procurement_items,
    merge_manual_overrides,
    override_from_bitrix_item,
    validate_mapping,
)


class FakeBitrixApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        self.calls.append((method, deepcopy(params)))
        if method != "crm.item.list":
            raise AssertionError(method)
        if params.get("start") == 50:
            return {"result": {"items": [{"id": "2"}]}}
        return {"result": {"items": [{"id": "1"}]}, "next": 50}


def _mapping() -> dict[str, Any]:
    return {
        "process": {"entity_type_id": 1056},
        "field_map": {
            "auto_order_sku_code": "UF_CRM_8_AUTOORDERSKUCODE",
            "auto_order_sku_name": "UF_CRM_8_AUTOORDERSKUNAME",
            "assortment_status_decision": "UF_CRM_8_ASSORTMENTSTATUSDECISION",
            "assortment_status_reason": "UF_CRM_8_ASSORTMENTSTATUSREASON",
            "assortment_status_approved_by": "UF_CRM_8_ASSORTMENTSTATUSAPPROVEDBY",
            "assortment_status_changed_at": "UF_CRM_8_ASSORTMENTSTATUSCHANGEDAT",
            "assortment_commercial_marks": "UF_CRM_8_ASSORTMENTCOMMERCIALMARKS",
        },
        "enum_map": {
            "assortment_status_decision": {
                "no_change": "480",
                "matrix": "481",
                "working": "482",
                "on_demand": "483",
                "replace_candidate": "484",
                "nonliquid": "485",
                "do_not_order": "486",
            }
        },
    }


def _item(**overrides: Any) -> dict[str, Any]:
    item = {
        "id": "7001",
        "title": "Автозаказ витрины",
        "updatedTime": "2026-07-06T12:30:00+03:00",
        "ufCrm8Autoorderskucode": "РБ000075803",
        "ufCrm8Autoorderskuname": "Дисплей F5ENERGY",
        "ufCrm8Assortmentstatusdecision": "481",
        "ufCrm8Assortmentstatusreason": "Собственная марка, заказано 1000 шт.",
        "ufCrm8Assortmentstatusapprovedby": "Омар",
        "ufCrm8Assortmentstatuschangedat": "2026-07-06T09:15:00+03:00",
        "ufCrm8Assortmentcommercialmarks": "own_brand, rare_market_item",
    }
    item.update(overrides)
    return item


def test_matrix_bitrix_decision_builds_manual_status_override() -> None:
    mapping = _mapping()

    override, blockers = override_from_bitrix_item(_item(), mapping=mapping)

    assert blockers == []
    assert override == {
        "nomenclature_code": "РБ000075803",
        "approval_rule": "bitrix_assortment_status_decision",
        "approval_rule_ru": "ручное решение в Bitrix Закупка/Заказ",
        "approval_source": "bitrix_procurement_order:7001",
        "manual_approved_by": "Омар",
        "manual_changed_at": "2026-07-06",
        "manual_reason": "Собственная марка, заказано 1000 шт.",
        "source_bitrix_item_id": "7001",
        "source_bitrix_title": "Автозаказ витрины",
        "source_bitrix_updated_at": "2026-07-06T12:30:00+03:00",
        "sync_blockers": [],
        "source_bitrix_sku_name": "Дисплей F5ENERGY",
        "commercial_marks": ["own_brand", "rare_market_item"],
        "manual_status": "matrix",
    }
    validate_mapping(mapping)


def test_working_bitrix_decision_builds_working_confirmation_override() -> None:
    override, blockers = override_from_bitrix_item(
        _item(ufCrm8Assortmentstatusdecision="482"),
        mapping=_mapping(),
    )

    assert blockers == []
    assert override is not None
    assert override["working_confirmed_by_folder_responsible"] is True
    assert "manual_status" not in override


def test_collect_decisions_skips_no_change_and_reports_bad_rows() -> None:
    decisions, skipped = collect_decisions(
        [
            _item(id="1", ufCrm8Assortmentstatusdecision="480"),
            _item(id="2", ufCrm8Autoorderskucode=""),
            _item(id="3", ufCrm8Assortmentstatusdecision="484"),
        ],
        mapping=_mapping(),
    )

    assert [item["source_bitrix_item_id"] for item in decisions] == ["3"]
    assert skipped == [{"item_id": "2", "blockers": ["nomenclature_code_required"]}]
    assert decisions[0]["manual_status"] == "replace_candidate"


def test_merge_manual_overrides_upserts_bitrix_sources_after_existing_items() -> None:
    payload = {
        "_description": "existing",
        "items": [
            {"nomenclature_code": "OLD", "manual_status": "do_not_order"},
            {
                "nomenclature_code": "РБ000075803",
                "approval_source": "bitrix_procurement_order:7001",
                "manual_status": "on_demand",
            },
        ],
    }
    decision = {
        "nomenclature_code": "РБ000075803",
        "approval_source": "bitrix_procurement_order:7001",
        "manual_status": "matrix",
    }

    merged, rows = merge_manual_overrides(payload, [decision])

    assert [item["nomenclature_code"] for item in merged["items"]] == ["OLD", "РБ000075803"]
    assert merged["items"][1]["manual_status"] == "matrix"
    assert rows[0]["action"] == "updated"


def test_list_procurement_items_reads_paginated_bitrix_result() -> None:
    api = FakeBitrixApi()

    rows = list_procurement_items(api, mapping=_mapping())

    assert [row["id"] for row in rows] == ["1", "2"]
    assert api.calls[0][1]["entityTypeId"] == 1056
    assert api.calls[1][1]["start"] == 50
