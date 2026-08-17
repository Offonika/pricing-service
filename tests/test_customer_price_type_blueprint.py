from __future__ import annotations

from copy import deepcopy
from typing import Any

import scripts.build_customer_price_type_blueprint as blueprint
import scripts.ensure_expertise_bitrix_process as bitrix_setup


def test_blueprint_is_local_dry_run_for_customer_price_type_process() -> None:
    result = blueprint.build_blueprint()
    fields = {item["logical_key"]: item for item in result["fields"]}
    stage_names = [item["name"] for item in result["stages"]]

    assert result["mode"] == "dry-run"
    assert result["safety"]["bitrix_writes"] is False
    assert result["process"]["title"] == "Типы Цен"
    assert result["process"]["code"] == "customer_price_type"
    assert result["process"]["current_process_kept_as_history"] is None
    assert "Рабочий список" not in stage_names
    assert len(result["stages"]) == 13
    assert [item["code"] for item in result["stages"]] == [
        "NEW_SNAPSHOT",
        "PRECLOSE_SIGNAL",
        "RETENTION_WORK",
        "ISOLATE_1M",
        "RECOVERY_CONTROL",
        "QUALITY_CHECK",
        "CREDIT_ECONOMICS_CHECK",
        "DATA_CHECK",
        "UPGRADE_APPROVAL",
        "DOWNGRADE_APPROVAL",
        "READY_FOR_1C",
        "CLOSED_KEEP",
        "CLOSED_CHANGED",
    ]

    assert fields["current_price_type"]["type"] == "enumeration"
    assert fields["target_price_type_candidate"]["type"] == "enumeration"
    assert fields["final_decision"]["type"] == "enumeration"
    assert fields["automation_level"]["type"] == "enumeration"
    assert fields["approved_target_price_type"]["type"] == "enumeration"
    assert fields["recommended_payment_window_days"]["type"] == "integer"

    assert result["pilot"]["onec_folder_filter"] == "Покупатели"
    assert result["access_rules"]["other_onec_folders"] == "owner approval required"
    assert result["approval_gates"][0]["key"] == "retail_to_b2b_upgrade_gate"


def test_live_snapshot_uses_only_read_only_bitrix_methods(monkeypatch) -> None:
    calls: list[str] = []
    categories = [{"id": 77, "name": "Управление типами цен", "isDefault": "Y"}]
    stages = [
        {"STATUS_ID": "DT1188_77:NEW_SNAPSHOT", "NAME": "Новый срез", "SORT": "100"},
        {"STATUS_ID": "DT1188_77:ISOLATE_1M", "NAME": "Изолятор 1 месяц", "SORT": "400"},
    ]

    def fake_bitrix_call(webhook_base: str, method: str, params: dict[str, Any] | None = None):
        calls.append(method)
        assert method in blueprint.READ_ONLY_BITRIX_METHODS
        if method == "crm.type.list":
            return {
                "result": {
                    "types": [
                        {
                            "id": 44,
                            "entityTypeId": 1188,
                            "title": "Типы Цен",
                            "code": "customer_price_type",
                        }
                    ]
                }
            }
        if method == "crm.category.list":
            return {"result": {"categories": deepcopy(categories)}}
        if method == "crm.status.list":
            return {"result": deepcopy(stages)}
        if method == "crm.item.fields":
            return {
                "result": {
                    "fields": {
                        "title": {"title": "Название", "type": "string"},
                        "ufCrm44Currentpricetype": {
                            "title": "Текущий тип цен",
                            "type": "string",
                        },
                    }
                }
            }
        if method == "crm.item.list":
            return {
                "result": {
                    "items": [
                        {
                            "id": 9,
                            "title": "Типы цен: РБ154 / 2.Бронзовый -> Розница",
                            "stageId": "DT1188_77:ISOLATE_1M",
                        }
                    ]
                }
            }
        if method == "user.search":
            return {
                "result": [
                    {
                        "ID": "6759",
                        "NAME": "Арсен",
                        "LAST_NAME": "Сагиян",
                        "WORK_POSITION": "Руководитель сети торговых точек",
                        "UF_DEPARTMENT": [123],
                        "ACTIVE": True,
                    }
                ]
            }
        if method == "department.get":
            return {
                "result": [
                    {
                        "ID": str(params["ID"]),
                        "NAME": "Розничные магазины",
                        "PARENT": "3266",
                        "UF_HEAD": "6759",
                    }
                ]
            }
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(bitrix_setup, "bitrix_call", fake_bitrix_call)

    snapshot = blueprint.build_live_snapshot(
        "https://bitrix.example/rest/1/token",
        current_entity_type_id=0,
    )

    assert snapshot["status"] == "ready"
    assert snapshot["write_methods_used"] == []
    assert snapshot["current_process"]["title"] == "Типы Цен"
    assert snapshot["resolved_entity_type_id"] == 1188
    assert snapshot["current_item_count"] == 1
    assert snapshot["arsen_candidates"][0]["ID"] == "6759"
    assert snapshot["arsen_departments"][0]["NAME"] == "Розничные магазины"
    assert calls == [
        "crm.type.list",
        "crm.category.list",
        "crm.status.list",
        "crm.item.fields",
        "crm.item.list",
        "user.search",
        "department.get",
    ]


def test_main_writes_blueprint_without_bitrix_by_default(tmp_path, capsys) -> None:
    output_path = tmp_path / "blueprint.json"

    exit_code = blueprint.main(["--output-path", str(output_path)])

    assert exit_code == 0
    payload = output_path.read_text(encoding="utf-8")
    assert "Типы Цен" in payload
    assert '"status": "not_requested"' in payload
    assert "Типы Цен" in capsys.readouterr().out
