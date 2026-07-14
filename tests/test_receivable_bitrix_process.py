from __future__ import annotations

from copy import deepcopy
from typing import Any

import scripts.ensure_expertise_bitrix_process as bitrix_setup
import scripts.ensure_receivable_bitrix_process as receivable_setup


def test_receivable_detail_layout_hides_technical_fields() -> None:
    visible_fields = {
        logical_key
        for section in receivable_setup.DETAIL_SECTION_SPECS
        for logical_key in section["elements"]
    }

    assert "chain_documents" in visible_fields
    assert "stable_key" not in visible_fields
    assert "counterparty_ref" not in visible_fields
    assert "department_ref" not in visible_fields
    assert "status" not in visible_fields
    assert "source" not in visible_fields


def test_receivable_choice_fields_are_enumerations() -> None:
    specs = {item["logical_key"]: item for item in receivable_setup.CUSTOM_FIELD_SPECS}

    assert specs["current_balance"]["type"] == "double"
    assert specs["phone_status"]["type"] == "enumeration"
    assert [item["xml_id"] for item in specs["phone_status"]["enum"]] == [
        "present",
        "missing",
        "needs_check",
    ]
    assert specs["sms_status"]["type"] == "enumeration"
    assert specs["contact_result"]["type"] == "enumeration"
    assert specs["escalation_level"]["type"] == "enumeration"

    visible_fields = {
        logical_key
        for section in receivable_setup.DETAIL_SECTION_SPECS
        for logical_key in section["elements"]
    }
    assert "contact_result" in visible_fields
    assert "last_contact_comment" in visible_fields


def test_receivable_category_reuses_default_category(monkeypatch) -> None:
    categories: list[dict[str, Any]] = [
        {
            "id": 51,
            "name": "Работа с дебиторкой",
            "sort": 100,
            "entityTypeId": 1132,
            "isDefault": "N",
        },
        {
            "id": 50,
            "name": "Общая воронка",
            "sort": 500,
            "entityTypeId": 1132,
            "isDefault": "Y",
        },
    ]
    updates: list[dict[str, Any]] = []

    def fake_bitrix_call(webhook_base: str, method: str, params=None):
        if method == "crm.category.list":
            return {"result": {"categories": deepcopy(categories)}}
        assert method == "crm.category.update"
        updates.append(params)
        for category in categories:
            if int(category["id"]) == int(params["id"]):
                category.update(params["fields"])
                return {"result": {"category": deepcopy(category)}}
        raise AssertionError(f"unknown category id {params['id']}")

    monkeypatch.setattr(bitrix_setup, "bitrix_call", fake_bitrix_call)

    category = receivable_setup.ensure_receivable_category(
        "https://bitrix.example/rest/1/token",
        entity_type_id=1132,
        name="Работа с дебиторкой",
    )

    assert int(category["id"]) == 50
    assert category["name"] == "Работа с дебиторкой"
    assert category["sort"] == 100
    assert updates == [
        {
            "entityTypeId": 1132,
            "id": 50,
            "fields": {"name": "Работа с дебиторкой", "sort": 100},
        }
    ]


def test_ensure_stages_does_not_reuse_expected_existing_stage(monkeypatch) -> None:
    monkeypatch.setattr(
        bitrix_setup,
        "STAGE_SPECS",
        [
            {
                "logical_key": "new_debt",
                "code": "NEW",
                "name": "Новый долг",
                "sort": 100,
                "semantics": None,
            },
            {
                "logical_key": "waiting_payment",
                "code": "WAITING",
                "name": "Ожидаем оплату",
                "sort": 200,
                "semantics": None,
            },
        ],
    )
    stages: list[dict[str, Any]] = [
        {
            "ID": "1",
            "STATUS_ID": "DT1132_51:NEW",
            "NAME": "Начало",
            "SORT": 100,
            "SEMANTICS": None,
        },
        {
            "ID": "2",
            "STATUS_ID": "DT1132_51:PREPARATION",
            "NAME": "Подготовка",
            "SORT": 200,
            "SEMANTICS": None,
        },
    ]

    def fake_list_stages(webhook_base: str, *, entity_type_id: int, category_id: int):
        return deepcopy(stages)

    def fake_bitrix_call(webhook_base: str, method: str, params=None):
        assert method == "crm.status.update"
        stage_id = str(params["id"])
        fields = params["fields"]
        for stage in stages:
            if stage["ID"] == stage_id:
                stage.update(fields)
                return {"result": True}
        raise AssertionError(f"unknown stage id {stage_id}")

    monkeypatch.setattr(bitrix_setup, "list_stages", fake_list_stages)
    monkeypatch.setattr(bitrix_setup, "bitrix_call", fake_bitrix_call)

    result = bitrix_setup.ensure_stages(
        "https://bitrix.example/rest/1/token", entity_type_id=1132, category_id=51
    )

    assert result["new_debt"]["STATUS_ID"] == "DT1132_51:NEW"
    assert result["waiting_payment"]["STATUS_ID"] == "DT1132_51:PREPARATION"
    assert result["new_debt"]["STATUS_ID"] != result["waiting_payment"]["STATUS_ID"]
    assert stages[0]["NAME"] == "Новый долг"
    assert stages[1]["NAME"] == "Ожидаем оплату"


def test_ensure_stages_does_not_reuse_stage_reserved_for_later_spec(monkeypatch) -> None:
    monkeypatch.setattr(
        bitrix_setup,
        "STAGE_SPECS",
        [
            {
                "logical_key": "no_phone",
                "code": "NO_PHONE",
                "name": "Нет телефона",
                "sort": 350,
                "semantics": None,
            },
            {
                "logical_key": "calling",
                "code": "CALLING",
                "name": "Менеджер прозванивает",
                "sort": 400,
                "semantics": None,
            },
        ],
    )
    stages: list[dict[str, Any]] = [
        {
            "ID": "1",
            "STATUS_ID": "DT1132_51:CALLING",
            "NAME": "Менеджер прозванивает",
            "SORT": 400,
            "SEMANTICS": None,
        },
    ]

    def fake_list_stages(webhook_base: str, *, entity_type_id: int, category_id: int):
        return deepcopy(stages)

    def fake_bitrix_call(webhook_base: str, method: str, params=None):
        if method == "crm.status.add":
            fields = params["fields"]
            stages.append(
                {
                    "ID": "2",
                    "STATUS_ID": fields["STATUS_ID"],
                    "NAME": fields["NAME"],
                    "SORT": fields["SORT"],
                    "SEMANTICS": fields.get("SEMANTICS"),
                }
            )
            return {"result": True}
        assert method == "crm.status.update"
        for stage in stages:
            if stage["ID"] == str(params["id"]):
                stage.update(params["fields"])
                return {"result": True}
        raise AssertionError(f"unknown stage id {params['id']}")

    monkeypatch.setattr(bitrix_setup, "list_stages", fake_list_stages)
    monkeypatch.setattr(bitrix_setup, "bitrix_call", fake_bitrix_call)

    result = bitrix_setup.ensure_stages(
        "https://bitrix.example/rest/1/token", entity_type_id=1132, category_id=51
    )

    assert result["no_phone"]["STATUS_ID"] == "DT1132_51:NO_PHONE"
    assert result["calling"]["STATUS_ID"] == "DT1132_51:CALLING"
    assert result["no_phone"]["STATUS_ID"] != result["calling"]["STATUS_ID"]
