from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from scripts.sync_display_auto_order_candidates_to_bitrix import (
    build_candidate_fields,
    guard_legacy_procurement_apply,
    load_order_rows,
    upsert_candidate,
    validate_mapping,
)


class FakeBitrixApi:
    def __init__(self, *, items: dict[str, dict[str, Any]] | None = None) -> None:
        self.items = items or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.next_item_id = 3000

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        self.calls.append((method, deepcopy(params)))
        if method == "crm.item.list":
            filters = params.get("filter") or {}
            rows = [
                deepcopy(item)
                for item in self.items.values()
                if all(
                    str(item.get(field.removeprefix("=")) or "") == str(expected)
                    for field, expected in filters.items()
                )
            ]
            return {"result": {"items": rows}}
        if method == "crm.item.get":
            return {"result": {"item": deepcopy(self.items.get(str(params["id"]), {}))}}
        if method == "crm.item.add":
            item_id = str(self.next_item_id)
            self.next_item_id += 1
            self.items[item_id] = {"id": item_id, **deepcopy(params["fields"])}
            return {"result": {"item": {"id": item_id}}}
        if method == "crm.item.update":
            self.items[str(params["id"])].update(deepcopy(params["fields"]))
            return {"result": {"item": deepcopy(self.items[str(params["id"])])}}
        raise AssertionError(method)


def _mapping() -> dict[str, Any]:
    return {
        "process": {
            "title": "Формирование заказа",
            "code": "order_formation",
            "entity_type_id": 2050,
        },
        "category_map": {"ordinary": {"id": 1}},
        "stage_map": {"ordinary": {"need": "DT2050_1:NEW", "closed": "DT2050_1:SUCCESS"}},
        "field_map": {
            "procurement_contour": "UF_CONTOUR",
            "pilot_batch_id": "UF_BATCH",
            "auto_order_source": "UF_AUTO_SOURCE",
            "auto_order_run_id": "UF_AUTO_RUN",
            "auto_order_sku_code": "UF_AUTO_SKU",
            "auto_order_sku_name": "UF_AUTO_SKU_NAME",
            "auto_order_decision": "UF_AUTO_DECISION",
            "auto_order_recommended_qty": "UF_AUTO_QTY",
            "auto_order_raw_qty": "UF_AUTO_RAW_QTY",
            "auto_order_target_stock_qty": "UF_AUTO_TARGET",
            "auto_order_free_stock_qty": "UF_AUTO_FREE",
            "auto_order_incoming_qty": "UF_AUTO_INCOMING",
            "auto_order_reason": "UF_AUTO_REASON",
            "auto_order_warnings": "UF_AUTO_WARNINGS",
            "auto_order_blockers": "UF_AUTO_BLOCKERS",
            "auto_order_calculated_at": "UF_AUTO_CALCULATED_AT",
        },
        "enum_map": {
            "procurement_contour": {"ordinary": "464"},
            "auto_order_decision": {"order": "500"},
        },
    }


def _row() -> dict[str, str]:
    return {
        "nomenclature_code": "РБ000041438",
        "name": "Дисплей Samsung A12 Medium",
        "recommended_order_qty": "5",
        "recommended_order_qty_raw": "7",
        "target_stock_qty": "14",
        "free_stock_qty": "7",
        "incoming_qty": "0",
        "dry_run_decision": "order",
        "reason_ru": "Рекомендуем 5 шт.",
        "warnings": "order_qty_capped",
        "blockers": "",
    }


def test_build_candidate_fields_uses_stable_batch_and_auto_order_fields() -> None:
    mapping = _mapping()

    batch_id, fields = build_candidate_fields(
        _row(),
        mapping=mapping,
        run_id="203",
        assigned_by_id="130750",
        calculated_at="2026-07-04T12:00:00+00:00",
    )

    assert batch_id == "DISPLAY-AUTO-203-РБ000041438"
    assert fields["TITLE"].startswith("Автозаказ витрины · РБ000041438 · 5 шт.")
    assert fields["categoryId"] == 1
    assert fields["stageId"] == "DT2050_1:NEW"
    assert fields["UF_CONTOUR"] == "464"
    assert fields["UF_AUTO_DECISION"] == "500"
    assert fields["UF_AUTO_QTY"] == "5"
    assert fields["UF_AUTO_RAW_QTY"] == "7"
    assert fields["UF_AUTO_WARNINGS"] == "order_qty_capped"
    assert fields["ASSIGNED_BY_ID"] == "130750"
    validate_mapping(mapping)


def test_upsert_candidate_creates_then_noops_existing_card() -> None:
    api = FakeBitrixApi()
    mapping = _mapping()

    created = upsert_candidate(
        api,
        _row(),
        mapping=mapping,
        run_id="203",
        assigned_by_id="130750",
        apply=True,
    )
    repeated = upsert_candidate(
        api,
        _row(),
        mapping=mapping,
        run_id="203",
        assigned_by_id="130750",
        apply=True,
    )

    assert created["action"] == "created"
    assert repeated["action"] == "noop"
    assert len([method for method, _params in api.calls if method == "crm.item.add"]) == 1
    assert len(api.items) == 1


def test_upsert_candidate_reuses_open_sku_card_from_previous_run_without_stage_reset() -> None:
    mapping = _mapping()
    api = FakeBitrixApi(
        items={
            "4000": {
                "id": "4000",
                "title": "Автозаказ витрины · старый расчет",
                "categoryId": 1,
                "stageId": "DT2050_1:SUPPLIER_REVIEW",
                "UF_BATCH": "DISPLAY-AUTO-202-РБ000041438",
                "UF_AUTO_SOURCE": "display_auto_order_dry_run",
                "UF_AUTO_SKU": "РБ000041438",
                "UF_AUTO_RUN": "202",
                "UF_AUTO_QTY": "4",
            }
        }
    )

    result = upsert_candidate(
        api,
        _row(),
        mapping=mapping,
        run_id="203",
        assigned_by_id="130757",
        apply=True,
    )

    assert result["action"] == "updated"
    assert result["item_id"] == "4000"
    assert result["matched_by"] == "sku_open_card"
    assert api.items["4000"]["stageId"] == "DT2050_1:SUPPLIER_REVIEW"
    assert api.items["4000"]["UF_BATCH"] == "DISPLAY-AUTO-203-РБ000041438"
    assert api.items["4000"]["UF_AUTO_QTY"] == "5"
    assert api.items["4000"]["assignedById"] == "130757"
    assert not any(method == "crm.item.add" for method, _params in api.calls)
    update_call = next(params for method, params in api.calls if method == "crm.item.update")
    assert "stageId" not in update_call["fields"]
    assert "categoryId" not in update_call["fields"]


def test_legacy_procurement_process_apply_is_blocked() -> None:
    mapping = {
        **_mapping(),
        "process": {
            "title": "Закупка/Заказ",
            "code": "procurement_order",
            "entity_type_id": 1056,
        },
    }

    with pytest.raises(RuntimeError, match="legacy process"):
        guard_legacy_procurement_apply(mapping)

    with pytest.raises(RuntimeError, match="Формирование заказа"):
        upsert_candidate(
            FakeBitrixApi(),
            _row(),
            mapping=mapping,
            run_id="203",
            assigned_by_id="130750",
            apply=True,
        )


def test_load_order_rows_keeps_only_positive_order_decisions(tmp_path) -> None:
    csv_path = tmp_path / "display-auto-order-dry-run.csv"
    csv_path.write_text(
        "\n".join(
            [
                "nomenclature_code,recommended_order_qty,dry_run_decision",
                "RB1,5,order",
                "RB2,0,order",
                "RB3,3,manual_review",
            ]
        ),
        encoding="utf-8",
    )

    rows = load_order_rows(csv_path)

    assert [row["nomenclature_code"] for row in rows] == ["RB1"]
