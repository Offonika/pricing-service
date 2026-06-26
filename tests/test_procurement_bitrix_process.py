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


def test_procurement_type_flags_enable_stage_amounts(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_call(_webhook_base: str, method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, deepcopy(params)))
        return {"result": {"type": {"id": 8, "isLinkWithProductsEnabled": "Y"}}}

    monkeypatch.setattr(bitrix_setup, "bitrix_call", fake_call)
    monkeypatch.setattr(
        bitrix_setup,
        "find_type_by_title",
        lambda _webhook_base, _title: {"id": 8, "isLinkWithProductsEnabled": "Y"},
    )

    result = procurement_setup.ensure_procurement_type_flags(
        "https://example.test/rest/1/token",
        process_type={"id": 8, "isLinkWithProductsEnabled": "N"},
        title="Закупка/Заказ",
    )

    assert result["isLinkWithProductsEnabled"] == "Y"
    assert calls == [
        (
            "crm.type.update",
            {"id": 8, "fields": {"isLinkWithProductsEnabled": True}},
        )
    ]


def test_onec_procurement_contour_values_are_normalized() -> None:
    assert procurement_setup.normalize_onec_procurement_contour("") == "ordinary"
    assert procurement_setup.normalize_onec_procurement_contour("", currency="RMB") == "cargo"
    assert procurement_setup.normalize_onec_procurement_contour("", currency="USD") == "cargo"
    assert procurement_setup.normalize_onec_procurement_contour("", currency="RUB") == "ordinary"
    assert procurement_setup.normalize_onec_procurement_contour("", currency="руб.") == "ordinary"
    assert (
        procurement_setup.normalize_onec_procurement_contour("", is_open_supplier_order=True)
        == "cargo"
    )
    assert procurement_setup.normalize_onec_procurement_contour(None) == "ordinary"
    assert (
        procurement_setup.normalize_onec_procurement_contour(None, is_open_supplier_order=True)
        == "cargo"
    )
    assert procurement_setup.normalize_onec_procurement_contour("Обычный") == "ordinary"
    assert procurement_setup.normalize_onec_procurement_contour("Cargo") == "cargo"
    assert procurement_setup.normalize_onec_procurement_contour("Карго") == "cargo"
    assert procurement_setup.normalize_onec_procurement_contour("ВЭД импорт") == "ved_import"
    assert procurement_setup.normalize_onec_procurement_contour("ВЭДИмпорт") == "ved_import"
    assert procurement_setup.normalize_onec_procurement_contour("ved_import") == "ved_import"


def test_unknown_onec_procurement_contour_blocks_import() -> None:
    try:
        procurement_setup.normalize_onec_procurement_contour("Белый импорт")
    except ValueError as error:
        assert "Unsupported procurement contour" in str(error)
    else:
        raise AssertionError("unknown contour value must fail closed")


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


def test_procurement_has_supplier_resolution_control_fields() -> None:
    specs = {item["logical_key"]: item for item in procurement_setup.CUSTOM_FIELD_SPECS}

    assert specs["supplier_onec_ref"]["title"] == "1С ref поставщика"
    assert specs["supplier_resolution_status"]["type"] == "enumeration"
    assert [item["xml_id"] for item in specs["supplier_resolution_status"]["enum"]] == [
        "resolved_existing",
        "created_from_onec",
        "manual_review",
        "blocked_duplicate",
    ]
    assert specs["supplier_resolution_basis"]["title"] == "Как найден поставщик"
    assert specs["supplier_conflicts"]["type"] == "text"


def test_crm_supplier_sync_fields_cover_companies_and_contacts() -> None:
    rows = procurement_setup.CRM_SYNC_FIELD_SPECS
    by_entity = {
        entity: {item["field_name"] for item in rows if item["entity"] == entity}
        for entity in ("company", "contact")
    }

    assert "UF_CRM_MM_ONEC_SUPPLIER_REF" in by_entity["company"]
    assert "UF_CRM_MM_ONEC_SUPPLIER_CODE" in by_entity["company"]
    assert "UF_CRM_MM_ONEC_SUPPLIER_UPDATED_AT" in by_entity["company"]
    assert "UF_CRM_MM_SUPPLIER_REG_NO" in by_entity["company"]
    assert "UF_CRM_MM_ONEC_CONTACT_REF" in by_entity["contact"]
    assert "UF_CRM_MM_ONEC_CONTACT_CODE" in by_entity["contact"]
    assert "UF_CRM_MM_ONEC_CONTACT_UPDATED_AT" in by_entity["contact"]


def test_ved_supplier_passport_has_crm_company_link() -> None:
    specs = {
        item["logical_key"]: item for item in procurement_setup.VED_SUPPLIER_PASSPORT_FIELD_SPECS
    }
    user_type_id, settings = procurement_setup.field_config_for_spec(specs["crm_company"])

    assert specs["crm_company"]["title"] == "CRM company"
    assert user_type_id == "crm"
    assert settings["COMPANY"] == "Y"


def test_ensure_crm_sync_userfields_is_idempotent(monkeypatch) -> None:
    existing = {
        "company": [{"FIELD_NAME": "UF_CRM_MM_ONEC_SUPPLIER_REF"}],
        "contact": [],
    }
    calls: list[tuple[str, dict]] = []

    def fake_bitrix_call(webhook_base: str, method: str, params=None):
        calls.append((method, params or {}))
        if method == "crm.company.userfield.list":
            return {"result": deepcopy(existing["company"])}
        if method == "crm.contact.userfield.list":
            return {"result": deepcopy(existing["contact"])}
        if method == "crm.company.userfield.add":
            existing["company"].append({"FIELD_NAME": "UF_CRM_" + params["fields"]["FIELD_NAME"]})
            return {"result": 101}
        if method == "crm.contact.userfield.add":
            existing["contact"].append({"FIELD_NAME": "UF_CRM_" + params["fields"]["FIELD_NAME"]})
            return {"result": 201}
        raise AssertionError(method)

    monkeypatch.setattr(bitrix_setup, "bitrix_call", fake_bitrix_call)

    rows = procurement_setup.ensure_crm_sync_userfields("https://bitrix.example/rest/1/token")

    assert rows[0]["field_name"] == "UF_CRM_MM_ONEC_SUPPLIER_REF"
    assert rows[0]["action"] == "exists"
    add_methods = [method for method, _params in calls if method.endswith(".add")]
    assert "crm.company.userfield.add" in add_methods
    assert "crm.contact.userfield.add" in add_methods


def test_procurement_has_cargo_date_fields_from_onec_order() -> None:
    specs = {item["logical_key"]: item for item in procurement_setup.CUSTOM_FIELD_SPECS}

    assert specs["supplier_dispatch_date"]["title"] == "Отправка поставщиком"
    assert specs["supplier_dispatch_date"]["type"] == "datetime"
    assert specs["cargo_dropoff_date"]["title"] == "Сдача в карго"
    assert specs["cargo_dropoff_date"]["type"] == "datetime"
    assert specs["expected_receipt_date"]["title"] == "Поступление"
    assert specs["expected_receipt_date"]["type"] == "datetime"


def test_procurement_categories_cover_all_contours() -> None:
    categories = {item["logical_key"]: item for item in procurement_setup.CATEGORY_SPECS}

    assert list(categories) == ["ordinary", "cargo", "ved_import"]
    assert categories["ordinary"]["name"] == "Обычный"
    assert categories["cargo"]["name"] == "Cargo"
    assert categories["ved_import"]["name"] == "ВЭД импорт"
    assert categories["ved_import"]["reuse_default"] is True


def test_cargo_stage_model_contains_as_is_payment_and_logistics_steps() -> None:
    category = next(
        item for item in procurement_setup.CATEGORY_SPECS if item["logical_key"] == "cargo"
    )
    stages = {item["logical_key"]: item for item in procurement_setup.desired_stage_specs(category)}

    assert list(stages) == [
        "need",
        "supplier_terms",
        "prices_confirmed",
        "payment_request",
        "payment_confirmed",
        "supplier_order",
        "supplier_dispatch",
        "cargo_dropoff",
        "in_transit",
        "receiving",
        "closed",
        "exception",
    ]
    assert stages["closed"]["semantics"] == "S"
    assert stages["exception"]["semantics"] == "F"


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


def test_enum_map_for_spec_uses_desired_keys_even_when_bitrix_xml_ids_differ() -> None:
    spec = {
        "enum": [
            {"xml_id": "ordinary", "value": "Обычный", "default": True},
            {"xml_id": "cargo", "value": "Cargo"},
            {"xml_id": "ved_import", "value": "ВЭД импорт"},
        ]
    }
    current = {
        "enum": [
            {"id": "367", "value": "Обычный", "xmlId": "1848e4ed61fb6f143a87b9e007a45550"},
            {"id": "368", "value": "Cargo", "xmlId": "9eaf00d6333266b659d33a8102d02af3"},
            {"id": "369", "value": "ВЭД импорт", "xmlId": "c6f41dbf669d4db0b850ed62a342402f"},
        ]
    }

    assert procurement_setup.enum_map_for_spec(spec, current) == {
        "ordinary": "367",
        "cargo": "368",
        "ved_import": "369",
    }


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
        current = {
            "id": str(1000 + len(string_fields)),
            "fieldName": bitrix_setup._field_name_for_spec(entity_id, spec),
            "xmlId": procurement_setup.field_xml_id_for_spec(spec),
            "userTypeId": "enumeration" if spec["type"] == "enumeration" else "string",
        }
        if spec["type"] == "enumeration":
            current["enum"] = [
                {"id": str(2000 + index), "value": option["value"], "xmlId": option["xml_id"]}
                for index, option in enumerate(spec["enum"], start=1)
            ]
        string_fields.append(current)
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


def test_mapping_links_onec_contour_to_bitrix_category_stage_and_enum() -> None:
    mapping = procurement_setup.build_mapping(
        process_type={
            "id": 8,
            "entityTypeId": 1056,
            "title": "Закупка/Заказ",
            "code": "procurement_order",
        },
        categories={
            "ordinary": {"id": 52, "name": "Обычный", "sort": 100},
            "cargo": {"id": 53, "name": "Cargo", "sort": 200},
            "ved_import": {"id": 12, "name": "ВЭД импорт", "sort": 300},
        },
        category_stages={
            "ordinary": {"need": {"STATUS_ID": "DT1056_52:NEED"}},
            "cargo": {"need": {"STATUS_ID": "DT1056_53:NEED"}},
            "ved_import": {"need": {"STATUS_ID": "DT1056_12:NEED"}},
        },
        custom_fields=[
            {
                "logical_key": "procurement_contour",
                "field_name": "UF_CRM_8_PROCUREMENTCONTOUR",
                "enum_map": {"ordinary": "367", "cargo": "368", "ved_import": "369"},
            }
        ],
        details_paths={},
    )

    contract = mapping["onec_contour_contract"]

    assert contract["onec_document"] == "ЗаказПоставщику"
    assert contract["onec_requisite"] == "КонтурЗакупки"
    assert (
        contract["blank_value_policy"]
        == "foreign_currency_cargo_rub_ordinary_otherwise_open_supplier_order"
    )
    assert contract["blank_foreign_currency_policy"] == "cargo"
    assert contract["blank_rub_currency_policy"] == "ordinary"
    assert contract["blank_open_supplier_order_policy"] == "cargo"
    assert contract["unknown_value_policy"] == "block_import"
    assert contract["onec_date_fields"] == {
        "supplier_dispatch_date": "Отправка постав.",
        "cargo_dropoff_date": "Сдача в карго",
        "expected_receipt_date": "Поступление",
    }
    assert contract["contours"]["ved_import"] == {
        "onec_requisite": "КонтурЗакупки",
        "onec_values": ["ВЭД импорт", "ВЭДИмпорт"],
        "bitrix_value": "ВЭД импорт",
        "bitrix_enum_xml_id": "ved_import",
        "bitrix_enum_id": "369",
        "category_key": "ved_import",
        "category_id": 12,
        "initial_stage_key": "need",
        "initial_stage_id": "DT1056_12:NEED",
    }
    assert "PROCUREMENT_BITRIX_ONEC_CONTOUR_CONTRACT" in mapping["env"]
    assert mapping["crm_supplier_sync_contract"]["company_is_master_profile"] is True
    assert (
        mapping["crm_supplier_sync_contract"]["multiple_match_policy"]
        == "manual_review_no_duplicate"
    )
    assert mapping["crm_supplier_sync_contract"]["procurement_field_map"] == {
        "supplier_company": None,
        "supplier_onec_ref": None,
        "supplier_resolution_status": None,
        "supplier_resolution_basis": None,
        "supplier_conflicts": None,
        "blocker_comment": None,
    }


def test_bitrix_payload_for_onec_contour_value() -> None:
    mapping = {
        "category_map": {
            "cargo": {"id": 53, "name": "Cargo"},
            "ved_import": {"id": 12, "name": "ВЭД импорт"},
        },
        "stage_map": {
            "cargo": {"need": "DT1056_53:NEW"},
            "ved_import": {"need": "DT1056_12:NEED"},
        },
        "field_map": {
            "procurement_contour": "UF_CRM_8_PROCUREMENTCONTOUR",
        },
        "enum_map": {
            "procurement_contour": {"ordinary": "367", "cargo": "368", "ved_import": "369"},
        },
    }

    payload = procurement_setup.bitrix_contour_payload_for_onec_value(
        "ВЭДИмпорт",
        mapping=mapping,
    )

    assert payload == {
        "logical_key": "ved_import",
        "onec_requisite": "КонтурЗакупки",
        "category_id": 12,
        "stage_id": "DT1056_12:NEED",
        "enum_id": "369",
        "fields": {
            "categoryId": 12,
            "stageId": "DT1056_12:NEED",
            "UF_CRM_8_PROCUREMENTCONTOUR": "369",
        },
    }

    cargo_payload = procurement_setup.bitrix_contour_payload_for_onec_value(
        "",
        mapping=mapping,
        is_open_supplier_order=True,
    )

    assert cargo_payload["logical_key"] == "cargo"
    assert cargo_payload["fields"]["categoryId"] == 53
    assert cargo_payload["fields"]["stageId"] == "DT1056_53:NEW"
    assert cargo_payload["fields"]["UF_CRM_8_PROCUREMENTCONTOUR"] == "368"
