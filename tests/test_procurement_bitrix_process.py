from __future__ import annotations

from copy import deepcopy

import scripts.ensure_expertise_bitrix_process as bitrix_setup
import scripts.ensure_procurement_bitrix_process as procurement_setup


def test_procurement_contour_field_is_required_enumeration() -> None:
    specs = {item["logical_key"]: item for item in procurement_setup.CUSTOM_FIELD_SPECS}

    contour = specs["procurement_contour"]

    assert contour["title"] == "Контур закупки"
    assert contour["type"] == "enumeration"
    assert contour["required"] is True
    assert [item["xml_id"] for item in contour["enum"]] == [
        "ordinary",
        "cargo",
        "ved_import",
    ]
    assert [item["value"] for item in contour["enum"]] == [
        "Обычный",
        "Cargo",
        "ВЭД импорт",
    ]


def test_procurement_participant_fields_use_crm_companies() -> None:
    specs = {item["logical_key"]: item for item in procurement_setup.CUSTOM_FIELD_SPECS}

    assert "goods_supplier" not in specs
    assert "logistics_operator" not in specs
    assert "payment_agent" not in specs

    for logical_key in ["supplier_company", "broker_company", "payment_agent_company"]:
        spec = specs[logical_key]
        user_type_id, settings = procurement_setup.field_config_for_spec(spec)

        assert spec["type"] == "crm_company"
        assert user_type_id == "crm"
        assert settings["COMPANY"] == "Y"
        assert settings["CONTACT"] == "N"


def test_procurement_categories_cover_all_contours() -> None:
    categories = {item["logical_key"]: item for item in procurement_setup.CATEGORY_SPECS}

    assert list(categories) == ["ordinary", "cargo", "ved_import"]
    assert categories["ordinary"]["name"] == "Обычный"
    assert categories["cargo"]["name"] == "Cargo"
    assert categories["ved_import"]["name"] == "ВЭД импорт"
    assert categories["ved_import"]["reuse_default"] is True


def test_ved_import_stage_model_contains_docs_customs_receiving() -> None:
    category = next(
        item for item in procurement_setup.CATEGORY_SPECS if item["logical_key"] == "ved_import"
    )
    stages = {item["logical_key"]: item for item in procurement_setup.desired_stage_specs(category)}

    assert list(stages) == [
        "need",
        "docs_collection",
        "docs_checked",
        "supplier_order",
        "payment_agent",
        "logistics_customs",
        "customs_clearance",
        "receiving",
        "closed",
        "blocked",
    ]
    assert stages["closed"]["semantics"] == "S"
    assert stages["blocked"]["semantics"] == "F"


def test_ved_import_category_reuses_default_category(monkeypatch) -> None:
    categories = [
        {"id": 11, "name": "Общая воронка", "sort": 500, "isDefault": "Y"},
        {"id": 12, "name": "Cargo", "sort": 200, "isDefault": "N"},
    ]
    updates: list[dict] = []

    def fake_bitrix_call(webhook_base: str, method: str, params=None):
        if method == "crm.category.list":
            return {"result": {"categories": deepcopy(categories)}}
        assert method == "crm.category.update"
        updates.append(params)
        for item in categories:
            if int(item["id"]) == int(params["id"]):
                item.update(params["fields"])
                return {"result": {"category": deepcopy(item)}}
        raise AssertionError(f"unknown category id {params['id']}")

    monkeypatch.setattr(bitrix_setup, "bitrix_call", fake_bitrix_call)
    category_spec = next(
        item for item in procurement_setup.CATEGORY_SPECS if item["logical_key"] == "ved_import"
    )

    category = procurement_setup.ensure_category_for_spec(
        "https://bitrix.example/rest/1/token",
        entity_type_id=1056,
        category_spec=category_spec,
    )

    assert int(category["id"]) == 11
    assert category["name"] == "ВЭД импорт"
    assert category["sort"] == 300
    assert updates == [
        {
            "entityTypeId": 1056,
            "id": 11,
            "fields": {"name": "ВЭД импорт", "sort": 300},
        }
    ]


def test_enum_options_for_update_preserves_existing_ids() -> None:
    spec = {
        "enum": [
            {"xml_id": "ordinary", "value": "Обычный", "default": True},
            {"xml_id": "cargo", "value": "Cargo"},
            {"xml_id": "ved_import", "value": "ВЭД импорт"},
        ]
    }
    current = {
        "enum": [
            {"id": "364", "value": "Обычный", "xmlId": "existing_ordinary"},
            {"id": "365", "value": "Cargo", "xmlId": "existing_cargo"},
            {"id": "366", "value": "ВЭД импорт", "xmlId": "existing_ved"},
        ]
    }

    rows = procurement_setup.enum_options_for_update(spec, current)

    assert [(row["id"], row["value"], row["xmlId"]) for row in rows] == [
        ("364", "Обычный", "existing_ordinary"),
        ("365", "Cargo", "existing_cargo"),
        ("366", "ВЭД импорт", "existing_ved"),
    ]
    assert [(row["def"], row["sort"]) for row in rows] == [
        ("Y", 200),
        ("N", 300),
        ("N", 400),
    ]

def test_existing_enum_field_update_omits_user_type_id(monkeypatch) -> None:
    procurement_setup.configure_generic_setup()
    entity_id = "CRM_8"
    enum_field = {
        "id": "865",
        "fieldName": "UF_CRM_8_PROCUREMENTCONTOUR",
        "xmlId": "UF_CRM_PROCUREMENT_PROCUREMENT_CONTOUR",
        "userTypeId": "enumeration",
        "enum": [
            {"id": "367", "value": "Обычный", "xmlId": "existing_ordinary"},
            {"id": "368", "value": "Cargo", "xmlId": "existing_cargo"},
            {"id": "369", "value": "ВЭД импорт", "xmlId": "existing_ved"},
        ],
    }
    string_fields = []
    for spec in procurement_setup.CUSTOM_FIELD_SPECS:
        if spec["logical_key"] == "procurement_contour":
            continue
        string_fields.append(
            {
                "id": str(1000 + len(string_fields)),
                "fieldName": bitrix_setup._field_name_for_spec(entity_id, spec),
                "xmlId": procurement_setup.field_xml_id_for_spec(spec),
                "userTypeId": "string",
            }
        )
    fields = [enum_field, *string_fields]
    calls: list[dict] = []

    def fake_list_userfields(webhook_base: str, *, entity_id: str):
        return deepcopy(fields)

    def fake_bitrix_call(webhook_base: str, method: str, params=None):
        if method == "userfieldconfig.get":
            field_id = str(params["id"])
            field = next(item for item in fields if str(item["id"]) == field_id)
            return {"result": {"field": deepcopy(field)}}
        assert method == "userfieldconfig.update"
        calls.append(params)
        return {"result": {"field": {}}}

    monkeypatch.setattr(bitrix_setup, "list_userfields", fake_list_userfields)
    monkeypatch.setattr(bitrix_setup, "bitrix_call", fake_bitrix_call)

    procurement_setup.ensure_procurement_custom_fields(
        "https://bitrix.example/rest/1/token",
        process_type={"id": 8},
    )

    contour_update = next(item for item in calls if str(item["id"]) == "865")
    assert "userTypeId" not in contour_update["field"]
    assert not any("enum" in item["field"] for item in calls)
