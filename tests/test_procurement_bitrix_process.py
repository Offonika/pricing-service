from __future__ import annotations

import json
from copy import deepcopy

import scripts.ensure_expertise_bitrix_process as bitrix_setup
import scripts.ensure_procurement_bitrix_process as procurement_setup


def test_procurement_contour_field_is_required_enumeration() -> None:
    specs = {item["logical_key"]: item for item in procurement_setup.CUSTOM_FIELD_SPECS}

    contour = specs["procurement_contour"]

    assert contour["title"] == "Вид транспортной отправки"
    assert contour["type"] == "enumeration"
    assert contour["required"] is True
    assert [item["xml_id"] for item in contour["enum"]] == [
        "ordinary",
        "cargo",
        "ved_import",
    ]
    assert [item["value"] for item in contour["enum"]] == [
        "Обычный",
        "Карго",
        "ВЭД импорт",
    ]
    assert contour["enum"][1]["aliases"] == ["Cargo"]


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


def test_list_userfields_reads_paginated_bitrix_result(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_call(
        _webhook_base: str,
        method: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        assert method == "userfieldconfig.list"
        calls.append(deepcopy(params))
        if params.get("start") == 50:
            return {"result": {"fields": [{"fieldName": "UF_CRM_8_PAGE2"}]}}
        return {
            "result": {"fields": [{"fieldName": "UF_CRM_8_PAGE1"}]},
            "next": 50,
        }

    monkeypatch.setattr(bitrix_setup, "bitrix_call", fake_call)

    rows = bitrix_setup.list_userfields("https://example.test/rest/1/token", entity_id="CRM_8")

    assert [row["fieldName"] for row in rows] == ["UF_CRM_8_PAGE1", "UF_CRM_8_PAGE2"]
    assert calls == [
        {"moduleId": "crm", "filter": {"entityId": "CRM_8"}},
        {"moduleId": "crm", "filter": {"entityId": "CRM_8"}, "start": 50},
    ]


def test_onec_procurement_contour_values_are_normalized() -> None:
    assert procurement_setup.normalize_onec_procurement_contour("") == "ordinary"
    assert procurement_setup.normalize_onec_procurement_contour("", currency="RMB") == "cargo"
    assert procurement_setup.normalize_onec_procurement_contour("", currency="USD") == "cargo"
    assert procurement_setup.normalize_onec_procurement_contour("", currency="RUB") == "ordinary"
    assert procurement_setup.normalize_onec_procurement_contour("", currency="руб.") == "ordinary"
    assert (
        procurement_setup.normalize_onec_procurement_contour(
            "",
            currency="руб.",
            has_cargo_dropoff=True,
        )
        == "cargo"
    )
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
    assert (
        procurement_setup.normalize_onec_procurement_contour(
            "Обычный",
            currency="RMB",
            has_cargo_dropoff=True,
        )
        == "ordinary"
    )
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

    assert specs["supplier_dispatch_date"]["title"] == "Отправка поставщику на обсуждение"
    assert specs["supplier_dispatch_date"]["type"] == "datetime"
    assert specs["cargo_dropoff_date"]["title"] == "Сдача в карго"
    assert specs["cargo_dropoff_date"]["type"] == "datetime"
    assert specs["expected_receipt_date"]["title"] == "Поступление"
    assert specs["expected_receipt_date"]["type"] == "datetime"
    assert specs["payment_task_id"]["title"] == "Задача оплаты: ID"
    assert specs["payment_task_status"]["type"] == "enumeration"
    assert [item["xml_id"] for item in specs["payment_task_status"]["enum"]] == [
        "created",
        "done",
        "skipped",
        "error",
    ]
    assert specs["payment_request_created_at"]["type"] == "datetime"


def test_procurement_has_auto_order_decision_fields() -> None:
    specs = {item["logical_key"]: item for item in procurement_setup.CUSTOM_FIELD_SPECS}
    sections = {item["name"]: item for item in procurement_setup.DETAIL_SECTION_SPECS}

    assert specs["auto_order_source"]["title"] == "Автозаказ: источник"
    assert specs["auto_order_run_id"]["type"] == "integer"
    assert specs["auto_order_sku_code"]["searchable"] is True
    assert specs["auto_order_decision"]["type"] == "enumeration"
    assert [item["xml_id"] for item in specs["auto_order_decision"]["enum"]] == [
        "order",
        "manual_review",
        "do_not_order",
    ]
    assert specs["auto_order_recommended_qty"]["type"] == "double"
    assert specs["auto_order_reason"]["type"] == "text"
    assert sections["auto_order"]["elements"] == [
        "auto_order_source",
        "auto_order_run_id",
        "auto_order_sku_code",
        "auto_order_sku_name",
        "auto_order_decision",
        "auto_order_recommended_qty",
        "auto_order_raw_qty",
        "auto_order_target_stock_qty",
        "auto_order_free_stock_qty",
        "auto_order_incoming_qty",
        "auto_order_reason",
        "auto_order_warnings",
        "auto_order_blockers",
        "auto_order_calculated_at",
    ]


def test_procurement_has_assortment_status_decision_fields() -> None:
    specs = {item["logical_key"]: item for item in procurement_setup.CUSTOM_FIELD_SPECS}
    sections = {item["name"]: item for item in procurement_setup.DETAIL_SECTION_SPECS}

    assert specs["assortment_status_decision"]["title"] == "Статус ассортимента: решение"
    assert specs["assortment_status_decision"]["type"] == "enumeration"
    assert [item["xml_id"] for item in specs["assortment_status_decision"]["enum"]] == [
        "no_change",
        "matrix",
        "working",
        "on_demand",
        "replace_candidate",
        "nonliquid",
        "do_not_order",
    ]
    assert [item["value"] for item in specs["assortment_status_decision"]["enum"]] == [
        "Без изменения",
        "Матричный",
        "Рабочий",
        "Под заказ",
        "Кандидат на замену",
        "Неликвид",
        "Не закупать",
    ]
    assert specs["assortment_status_reason"]["type"] == "text"
    assert specs["assortment_status_approved_by"]["type"] == "string"
    assert specs["assortment_status_changed_at"]["type"] == "datetime"
    assert specs["assortment_commercial_marks"]["type"] == "text"
    assert sections["assortment_status"]["elements"] == [
        "assortment_status_decision",
        "assortment_status_reason",
        "assortment_status_approved_by",
        "assortment_status_changed_at",
        "assortment_commercial_marks",
    ]


def test_procurement_has_certification_docs_generation_fields() -> None:
    specs = {item["logical_key"]: item for item in procurement_setup.CUSTOM_FIELD_SPECS}
    sections = {item["name"]: item for item in procurement_setup.DETAIL_SECTION_SPECS}

    assert specs["certification_docs_status"]["title"] == "Сертификация: статус"
    assert specs["certification_docs_status"]["type"] == "enumeration"
    assert [item["xml_id"] for item in specs["certification_docs_status"]["enum"]] == [
        "draft",
        "needs_data",
        "blocked",
        "ready",
        "gtin_requested",
        "sent_to_certifier",
    ]
    assert specs["certification_docs_version"]["type"] == "integer"
    assert specs["certification_docs_zip_url"]["type"] == "url"
    assert specs["certification_docs_disk_file_id"]["type"] == "string"
    assert specs["certification_docs_errors"]["type"] == "text"
    assert specs["certification_docs_generated_at"]["type"] == "datetime"
    assert sections["certification_docs"]["title"] == "Сертификация / GTIN"
    assert sections["certification_docs"]["elements"] == [
        "certification_docs_status",
        "certification_docs_version",
        "certification_docs_zip_url",
        "certification_docs_disk_file_id",
        "certification_docs_errors",
        "certification_docs_generated_at",
    ]


def test_certificate_document_types_include_gost_r_and_gtin_package() -> None:
    values = {
        item["xml_id"]: item["value"] for item in procurement_setup.CERTIFICATE_DOCUMENT_TYPE_ENUM
    }

    assert values["declaration_gost_r"] == "ДС ГОСТ Р"
    assert values["declaration_master_register"] == "Мастер-таблица ДС"
    assert values["gtin_ean13"] == "GTIN/EAN-13"


def test_procurement_categories_cover_all_contours() -> None:
    categories = {item["logical_key"]: item for item in procurement_setup.CATEGORY_SPECS}

    assert list(categories) == ["ordinary", "cargo", "ved_import"]
    assert categories["ordinary"]["name"] == "Обычный"
    assert categories["cargo"]["name"] == "Карго"
    assert categories["cargo"]["aliases"] == ["Cargo"]
    assert categories["ved_import"]["name"] == "ВЭД импорт"
    assert categories["ved_import"]["reuse_default"] is True


def test_cargo_category_plan_reuses_old_cargo_name_as_alias() -> None:
    category_spec = next(
        item for item in procurement_setup.CATEGORY_SPECS if item["logical_key"] == "cargo"
    )

    plan = procurement_setup.category_plan(
        [{"id": 53, "name": "Cargo", "sort": 200, "isDefault": "N"}],
        category_spec=category_spec,
    )

    assert plan["action"] == "update"
    assert plan["matched_by"] == "alias"
    assert plan["existing"]["id"] == 53


def test_cargo_stage_model_contains_target_payment_and_logistics_steps() -> None:
    category = next(
        item for item in procurement_setup.CATEGORY_SPECS if item["logical_key"] == "cargo"
    )
    stages = {item["logical_key"]: item for item in procurement_setup.desired_stage_specs(category)}

    assert list(stages) == [
        "supplier_order",
        "ready_for_cargo",
        "cargo_dropoff",
        "payment_work",
        "in_transit",
        "own_delivery",
        "unpacking",
        "receiving",
        "onec_receipt",
        "barcode",
        "placement",
        "closed",
        "exception",
    ]
    assert stages["supplier_order"]["name"] == "Заказ поставщику"
    assert stages["supplier_order"]["code"] == "NEW"
    assert stages["ready_for_cargo"]["sort"] == 200
    assert stages["payment_work"]["name"] == "Заявка на оплату / оплата в работе"
    assert stages["closed"]["name"] == "Раскладка завершена"
    assert stages["closed"]["semantics"] == "S"
    assert stages["exception"]["semantics"] == "F"


def test_terminal_stages_are_moved_before_adding_later_process_steps(monkeypatch) -> None:
    category = next(
        item for item in procurement_setup.CATEGORY_SPECS if item["logical_key"] == "cargo"
    )
    updates: list[dict] = []

    monkeypatch.setattr(
        bitrix_setup,
        "list_stages",
        lambda *_args, **_kwargs: [
            {"ID": "420", "NAME": "Закрыто", "SORT": "990", "SEMANTICS": "S"},
            {"ID": "421", "NAME": "Проблема / отмена", "SORT": "1001", "SEMANTICS": "F"},
            {"ID": "424", "NAME": "Приемка на склад", "SORT": "1000", "SEMANTICS": None},
        ],
    )
    monkeypatch.setattr(
        bitrix_setup,
        "bitrix_call",
        lambda _webhook_base, method, params=None: updates.append(params or {}) or {"result": True},
    )

    procurement_setup.move_terminal_stages_after_process_steps(
        "https://bitrix.example/rest/1/token",
        entity_type_id=1056,
        category_id=53,
        stage_specs=procurement_setup.desired_stage_specs(category),
    )

    assert updates == [
        {"id": "420", "fields": {"NAME": "Раскладка завершена", "SORT": 1300, "SEMANTICS": "S"}},
        {"id": "421", "fields": {"NAME": "Проблема / отмена", "SORT": 1400, "SEMANTICS": "F"}},
    ]


def test_missing_process_stages_are_precreated_before_success(monkeypatch) -> None:
    category = next(
        item for item in procurement_setup.CATEGORY_SPECS if item["logical_key"] == "cargo"
    )
    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        bitrix_setup,
        "list_stages",
        lambda *_args, **_kwargs: [
            {"ID": "420", "STATUS_ID": "DT1056_53:SUCCESS", "SORT": "990", "SEMANTICS": "S"},
            {"ID": "424", "STATUS_ID": "DT1056_53:RECEIVING", "SORT": "1000", "SEMANTICS": None},
        ],
    )
    monkeypatch.setattr(
        bitrix_setup,
        "bitrix_call",
        lambda _webhook_base, method, params=None: calls.append((method, params or {}))
        or {"result": True},
    )

    procurement_setup.precreate_missing_process_stages_before_success(
        "https://bitrix.example/rest/1/token",
        entity_type_id=1056,
        category_id=53,
        stage_specs=procurement_setup.desired_stage_specs(category),
    )

    assert [method for method, _params in calls] == [
        "crm.status.add",
        "crm.status.add",
        "crm.status.add",
    ]
    added = [params["fields"] for _method, params in calls]
    assert [item["STATUS_ID"] for item in added] == [
        "DT1056_53:ONEC_RECEIPT",
        "DT1056_53:BARCODE",
        "DT1056_53:PLACEMENT",
    ]
    assert [item["SORT"] for item in added] == [987, 988, 989]


def test_obsolete_supplier_order_stage_is_merged_into_system_start(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_call(_webhook_base, method, params=None):
        calls.append((method, params or {}))
        if method == "crm.item.list":
            return {"result": {"items": [{"id": 10}, {"id": 11}]}}
        return {"result": True}

    monkeypatch.setattr(
        bitrix_setup,
        "list_stages",
        lambda *_args, **_kwargs: [
            {"ID": "418", "STATUS_ID": "DT1056_53:PREPARATION", "NAME": "Заказ отправлен"},
            {"ID": "429", "STATUS_ID": "DT1056_53:ORDER", "NAME": "Заказ поставщику"},
            {"ID": "430", "STATUS_ID": "DT1056_53:NEW", "NAME": "Потребность"},
        ],
    )
    monkeypatch.setattr(bitrix_setup, "bitrix_call", fake_call)

    actions = procurement_setup.merge_and_delete_obsolete_stages(
        "https://bitrix.example/rest/1/token",
        entity_type_id=1056,
        category_id=53,
        category_key="cargo",
    )

    assert actions == [
        {
            "old_stage_id": "DT1056_53:PREPARATION",
            "target_stage_id": "DT1056_53:NEW",
            "moved_items": 2,
            "reason": (
                "supplier_assembly moved to order_formation; "
                "procurement starts at supplier_order"
            ),
        },
        {
            "old_stage_id": "DT1056_53:ORDER",
            "target_stage_id": "DT1056_53:NEW",
            "moved_items": 2,
            "reason": "supplier_order merged into system start stage",
        },
    ]
    assert [method for method, _params in calls] == [
        "crm.item.list",
        "crm.item.update",
        "crm.item.update",
        "crm.status.delete",
        "crm.item.list",
        "crm.item.update",
        "crm.item.update",
        "crm.status.delete",
    ]
    assert calls[1][1]["fields"]["stageId"] == "DT1056_53:NEW"
    assert calls[3][1] == {"id": "418", "FORCED": "Y"}
    assert calls[7][1] == {"id": "429", "FORCED": "Y"}


def test_obsolete_auto_code_supplier_order_stage_is_merged_by_name(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_call(_webhook_base, method, params=None):
        calls.append((method, params or {}))
        if method == "crm.item.list":
            return {"result": {"items": [{"id": 134}]}}
        return {"result": True}

    monkeypatch.setattr(
        bitrix_setup,
        "list_stages",
        lambda *_args, **_kwargs: [
            {"ID": "126", "STATUS_ID": "DT1056_12:NEW", "NAME": "Потребность"},
            {"ID": "131", "STATUS_ID": "DT1056_12:UC_OW8QYB", "NAME": "Заказ поставщику"},
        ],
    )
    monkeypatch.setattr(bitrix_setup, "bitrix_call", fake_call)

    actions = procurement_setup.merge_and_delete_obsolete_stages(
        "https://bitrix.example/rest/1/token",
        entity_type_id=1056,
        category_id=12,
        category_key="ved_import",
    )

    assert actions == [
        {
            "old_stage_id": "DT1056_12:UC_OW8QYB",
            "target_stage_id": "DT1056_12:NEW",
            "moved_items": 1,
            "reason": "supplier_order merged into system start stage",
        }
    ]
    assert calls[1][1]["fields"]["stageId"] == "DT1056_12:NEW"
    assert calls[2][1] == {"id": "131", "FORCED": "Y"}


def test_obsolete_stage_merge_plan_lists_auto_code_supplier_order() -> None:
    rows = procurement_setup.obsolete_stage_merge_plan(
        [
            {"ID": "126", "STATUS_ID": "DT1056_12:NEW", "NAME": "Потребность"},
            {"ID": "131", "STATUS_ID": "DT1056_12:UC_OW8QYB", "NAME": "Заказ поставщику"},
        ],
        entity_type_id=1056,
        category_id=12,
        category_key="ved_import",
    )

    assert rows == [
        {
            "old_stage_id": "DT1056_12:UC_OW8QYB",
            "old_stage_name": "Заказ поставщику",
            "target_stage_id": "DT1056_12:NEW",
            "action": "merge_then_delete",
            "reason": "supplier_order merged into system start stage",
        }
    ]


def test_ved_import_stage_model_contains_docs_customs_receiving() -> None:
    category = next(
        item for item in procurement_setup.CATEGORY_SPECS if item["logical_key"] == "ved_import"
    )
    stages = {item["logical_key"]: item for item in procurement_setup.desired_stage_specs(category)}

    assert list(stages) == [
        "supplier_order",
        "docs_collection",
        "docs_checked",
        "payment_agent",
        "logistics_customs",
        "customs_clearance",
        "receiving",
        "closed",
        "blocked",
    ]
    assert stages["supplier_order"]["code"] == "NEW"
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
            {"xml_id": "cargo", "value": "Карго", "aliases": ["Cargo"]},
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
        ("365", "Карго", "existing_cargo"),
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
            {"xml_id": "cargo", "value": "Карго", "aliases": ["Cargo"]},
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
    contour_enum_update = next(item for item in calls if "enum" in item["field"])
    assert contour_enum_update["field"]["enum"][1]["id"] == "368"
    assert contour_enum_update["field"]["enum"][1]["value"] == "Карго"
    assert contour_enum_update["field"]["enum"][1]["xmlId"] == "existing_cargo"


def test_discover_process_fields_fetches_enum_options(monkeypatch) -> None:
    procurement_setup.configure_generic_setup()
    entity_id = "CRM_8"
    status_spec = next(
        item
        for item in procurement_setup.CUSTOM_FIELD_SPECS
        if item["logical_key"] == "certification_docs_status"
    )
    field_name = bitrix_setup._field_name_for_spec(entity_id, status_spec)
    field_id = "1004"

    def fake_list_userfields(webhook_base: str, *, entity_id: str):
        return [
            {
                "id": field_id,
                "fieldName": field_name,
                "xmlId": procurement_setup.field_xml_id_for_spec(status_spec),
                "userTypeId": "enumeration",
            }
        ]

    def fake_bitrix_call(webhook_base: str, method: str, params=None):
        assert method == "userfieldconfig.get"
        assert str(params["id"]) == field_id
        return {
            "result": {
                "field": {
                    "id": field_id,
                    "fieldName": field_name,
                    "xmlId": procurement_setup.field_xml_id_for_spec(status_spec),
                    "userTypeId": "enumeration",
                    "enum": [
                        {
                            "id": str(458 + index),
                            "value": option["value"],
                            "xmlId": option["xml_id"],
                        }
                        for index, option in enumerate(status_spec["enum"])
                    ],
                }
            }
        }

    monkeypatch.setattr(bitrix_setup, "list_userfields", fake_list_userfields)
    monkeypatch.setattr(bitrix_setup, "bitrix_call", fake_bitrix_call)

    rows = procurement_setup.discover_process_fields(
        "https://bitrix.example/rest/1/token",
        process_type={"id": 8},
        field_specs=[status_spec],
    )

    assert rows == [
        {
            "logical_key": "certification_docs_status",
            "title": "Сертификация: статус",
            "field_name": field_name,
            "field_id": field_id,
            "xml_id": "UF_CRM_PROCUREMENT_CERTIFICATION_DOCS_STATUS",
            "enum_map": {
                "draft": "458",
                "needs_data": "459",
                "blocked": "460",
                "ready": "461",
                "gtin_requested": "462",
                "sent_to_certifier": "463",
            },
            "action": "found",
        }
    ]


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
            "cargo": {"id": 53, "name": "Карго", "sort": 200},
            "ved_import": {"id": 12, "name": "ВЭД импорт", "sort": 300},
        },
        category_stages={
            "ordinary": {"supplier_order": {"STATUS_ID": "DT1056_52:NEW"}},
            "cargo": {"supplier_order": {"STATUS_ID": "DT1056_53:NEW"}},
            "ved_import": {"supplier_order": {"STATUS_ID": "DT1056_12:NEW"}},
        },
        custom_fields=[
            {
                "logical_key": "procurement_contour",
                "field_name": "UF_CRM_8_PROCUREMENTCONTOUR",
                "enum_map": {"ordinary": "367", "cargo": "368", "ved_import": "369"},
            }
        ],
        certificate_process={},
        product_passport_process={},
        details_paths={},
    )

    contract = mapping["onec_contour_contract"]

    assert contract["onec_document"] == "ЗаказПоставщику"
    assert contract["pre_onec_process"] == "Формирование заказа"
    assert contract["legacy_need_stage_policy"] == "system_new_stage_is_renamed_to_supplier_order"
    assert contract["onec_requisite"] == "КонтурЗакупки"
    assert (
        contract["blank_value_policy"]
        == "foreign_currency_cargo_rub_ordinary_otherwise_open_supplier_order"
    )
    assert contract["blank_cargo_dropoff_date_policy"] == "cargo"
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
        "initial_stage_key": "supplier_order",
        "initial_stage_id": "DT1056_12:NEW",
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


def test_fields_only_mapping_patch_refreshes_env_field_and_enum_maps() -> None:
    mapping = {
        "field_map": {"title": "TITLE"},
        "enum_map": {},
        "env": {
            "PROCUREMENT_BITRIX_FIELD_MAP": '{"title": "TITLE"}',
            "PROCUREMENT_BITRIX_ENUM_MAP": "{}",
        },
    }

    patched = procurement_setup.patch_mapping_process_fields(
        mapping,
        process_type={
            "id": 8,
            "entityTypeId": 1056,
            "title": "Закупка/Заказ",
            "code": "procurement_order",
        },
        custom_fields=[
            {
                "logical_key": "assortment_status_decision",
                "field_name": "UF_CRM_8_ASSORTMENTSTATUSDECISION",
                "enum_map": {"matrix": "481"},
            }
        ],
        certificate_process={},
        product_passport_process={},
    )

    field_map = json.loads(patched["env"]["PROCUREMENT_BITRIX_FIELD_MAP"])
    enum_map = json.loads(patched["env"]["PROCUREMENT_BITRIX_ENUM_MAP"])

    assert field_map["assortment_status_decision"] == "UF_CRM_8_ASSORTMENTSTATUSDECISION"
    assert enum_map["assortment_status_decision"] == {"matrix": "481"}
    assert patched["env"]["PROCUREMENT_BITRIX_ENTITY_TYPE_ID"] == 1056


def test_fields_only_setup_can_refresh_details_configuration(monkeypatch, tmp_path) -> None:
    calls: list[dict[str, object]] = []

    def fake_ensure_details(
        _webhook_base: str,
        *,
        mapping: dict[str, object],
        path,
    ):
        calls.append({"mapping": deepcopy(mapping), "path": path.name})
        return [], path

    monkeypatch.setattr(
        bitrix_setup,
        "ensure_common_details_configuration",
        fake_ensure_details,
    )
    mapping = {
        "process": {"entity_type_id": 1056},
        "category_map": {
            "ordinary": {"id": 52},
            "cargo": {"id": 53},
            "ved_import": {"id": 12},
        },
        "field_map": {"assortment_status_decision": "UF_CRM_8_ASSORTMENTSTATUSDECISION"},
    }

    paths = procurement_setup.ensure_procurement_details_configurations(
        "https://example.test/rest/1/token",
        mapping=mapping,
        details_config_path=tmp_path / "procurement_order_details_configuration.json",
    )

    assert paths == {
        "ordinary": str(tmp_path / "procurement_order_details_configuration_ordinary.json"),
        "cargo": str(tmp_path / "procurement_order_details_configuration_cargo.json"),
        "ved_import": str(tmp_path / "procurement_order_details_configuration_ved_import.json"),
    }
    assert [call["path"] for call in calls] == [
        "procurement_order_details_configuration_ordinary.json",
        "procurement_order_details_configuration_cargo.json",
        "procurement_order_details_configuration_ved_import.json",
    ]
    assert calls[0]["mapping"]["process"] == {"entity_type_id": 1056, "category_id": 52}
    assert calls[0]["mapping"]["fields"]["assortment_status_decision"] == (
        "UF_CRM_8_ASSORTMENTSTATUSDECISION"
    )


def test_bitrix_payload_for_onec_contour_value() -> None:
    mapping = {
        "category_map": {
            "cargo": {"id": 53, "name": "Карго"},
            "ved_import": {"id": 12, "name": "ВЭД импорт"},
        },
        "stage_map": {
            "cargo": {"supplier_order": "DT1056_53:NEW"},
            "ved_import": {"supplier_order": "DT1056_12:NEW"},
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
        "stage_id": "DT1056_12:NEW",
        "enum_id": "369",
        "fields": {
            "categoryId": 12,
            "stageId": "DT1056_12:NEW",
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

    cargo_by_date_payload = procurement_setup.bitrix_contour_payload_for_onec_value(
        "",
        mapping=mapping,
        currency="руб.",
        has_cargo_dropoff=True,
    )

    assert cargo_by_date_payload["logical_key"] == "cargo"
    assert cargo_by_date_payload["fields"]["categoryId"] == 53
