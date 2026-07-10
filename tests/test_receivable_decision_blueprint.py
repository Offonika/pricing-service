from __future__ import annotations

from copy import deepcopy
from typing import Any

import scripts.build_receivable_decision_blueprint as blueprint
import scripts.ensure_expertise_bitrix_process as bitrix_setup


def test_blueprint_is_local_dry_run_for_decision_process() -> None:
    result = blueprint.build_blueprint()
    fields = {item["logical_key"]: item for item in result["fields"]}
    stage_names = [item["name"] for item in result["stages"]]

    assert result["mode"] == "dry-run"
    assert result["safety"]["bitrix_writes"] is False
    assert result["process"]["title"] == "Дебиторка Решение"
    assert result["process"]["code"] == "receivable_decision"
    assert result["process"]["current_process_kept_as_history"] == "Дебиторка покупателей"
    assert "Рабочий список" not in stage_names

    assert fields["trend_coefficient"]["title"] == "Коэффициент тенденции"
    assert fields["payment_behavior_group"]["type"] == "enumeration"
    assert fields["recommended_decision"]["type"] == "enumeration"
    assert fields["advisor_summary"]["title"] == "Советник: картина"
    assert fields["recommended_first_payment_pct"]["type"] == "double"
    assert fields["recommended_payment_window_days"]["type"] == "integer"

    assert result["pilot"]["onec_folder_filter"] == "Покупатели"
    assert result["advisor_rules"]["first_payment_pct_range"] == [20, 35]
    assert result["advisor_rules"]["payment_window_days_range"] == [7, 10]
    assert result["access_rules"]["other_onec_folders"] == "owner approval required"


def test_live_snapshot_uses_only_read_only_bitrix_methods(monkeypatch) -> None:
    calls: list[str] = []
    categories = [{"id": 50, "name": "Работа с дебиторкой", "isDefault": "Y"}]
    stages = [
        {"STATUS_ID": "DT1132_50:NEW", "NAME": "Новый долг", "SORT": "100"},
        {"STATUS_ID": "DT1132_50:NO_PHONE", "NAME": "Нет телефона", "SORT": "350"},
    ]

    def fake_bitrix_call(webhook_base: str, method: str, params: dict[str, Any] | None = None):
        calls.append(method)
        assert method in blueprint.READ_ONLY_BITRIX_METHODS
        if method == "crm.type.list":
            return {
                "result": {
                    "types": [
                        {
                            "id": 35,
                            "entityTypeId": 1132,
                            "title": "Дебиторка покупателей",
                            "code": "receivable_buyers",
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
                        "ufCrm35Currentbalance": {
                            "title": "Сумма просрочки",
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
                            "id": 5,
                            "title": "Дебиторка: 154 Саша / 53920 руб.",
                            "stageId": "DT1132_50:NO_PHONE",
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
        current_entity_type_id=1132,
    )

    assert snapshot["status"] == "ready"
    assert snapshot["write_methods_used"] == []
    assert snapshot["current_process"]["title"] == "Дебиторка покупателей"
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
    assert "Дебиторка Решение" in payload
    assert '"status": "not_requested"' in payload
    assert "Дебиторка Решение" in capsys.readouterr().out
