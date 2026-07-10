from __future__ import annotations

from scripts.ensure_order_formation_bitrix_process import (
    CUSTOM_FIELD_SPECS,
    STAGE_SPECS,
    build_dry_run_mapping,
)


def test_order_formation_process_has_required_stages_and_products() -> None:
    mapping = build_dry_run_mapping()

    assert mapping["process"]["title"] == "Формирование заказа"
    assert mapping["process"]["products_enabled"] is True
    assert list(mapping["stage_map"]) == [item[0] for item in STAGE_SPECS]
    assert [item[2] for item in STAGE_SPECS] == [
        "Черновик сформирован",
        "На проверке",
        "Согласовано к 1С",
        "Передача в 1С",
        "Передано в 1С",
        "Отложено / отменено",
        "Ошибка передачи",
    ]


def test_order_formation_mapping_is_separate_and_covers_connector_fields() -> None:
    mapping = build_dry_run_mapping()
    field_keys = {item[0] for item in CUSTOM_FIELD_SPECS}

    assert mapping["process"]["code"] == "procurement_order_formation"
    assert mapping["process"]["owner_type"] == "T0"
    assert mapping["catalog"]["xml_id"] == "XML_ID"
    assert {
        "stable_key",
        "version",
        "supplier_ref",
        "contract_ref",
        "warehouse_ref",
        "connector_status",
        "onec_document_ref",
        "onec_document_number",
    }.issubset(field_keys)
