from __future__ import annotations

from typing import Any

import scripts.build_receivable_work_blueprint as blueprint
import scripts.ensure_expertise_bitrix_process as bitrix_setup


def test_receivable_work_blueprint_is_write_free_and_has_target_stages() -> None:
    result = blueprint.build_blueprint()

    assert result["mode"] == "dry-run"
    assert result["safety"]["bitrix_writes"] is False
    assert result["live_snapshot"] == {"status": "not_requested"}
    assert result["process"] == {
        "title": "Работа с дебиторкой",
        "code": "receivable_work",
        "legacy_process_kept_as_history": "Дебиторка покупателей",
    }
    assert [stage["name"] for stage in result["stages"]] == [
        "Новый",
        "В работе",
        "Ожидаем оплату",
        "Спор",
        "Эскалация",
        "Закрыто",
    ]
    assert "last_sms_status" in result["non_stage_signals"]
    assert result["migration"]["full_legacy_history_copy"] is False
    assert result["pilot"]["department_owner"] == "Арсен Сагиян"


def test_live_snapshot_uses_only_read_only_bitrix_methods(monkeypatch) -> None:
    calls: list[str] = []

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
    snapshot = blueprint.build_live_snapshot("https://bitrix.example/rest/1/token")

    assert snapshot["status"] == "ready"
    assert snapshot["write_methods_used"] == []
    assert snapshot["target_process_candidates"] == []
    assert snapshot["target_collision"] is False
    assert snapshot["legacy_process"]["entityTypeId"] == 1132
    assert snapshot["arsen_candidates"][0]["ID"] == "6759"
    assert snapshot["arsen_departments"][0]["NAME"] == "Розничные магазины"
    assert calls == ["crm.type.list", "user.search", "department.get"]


def test_main_writes_blueprint_without_bitrix_by_default(tmp_path, capsys) -> None:
    output_path = tmp_path / "blueprint.json"

    exit_code = blueprint.main(["--output-path", str(output_path)])

    assert exit_code == 0
    payload = output_path.read_text(encoding="utf-8")
    assert "Работа с дебиторкой" in payload
    assert '"status": "not_requested"' in payload
    assert "Работа с дебиторкой" in capsys.readouterr().out


def test_live_snapshot_reads_existing_target_metadata_without_writes(monkeypatch) -> None:
    calls: list[str] = []

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
                        },
                        {
                            "id": 40,
                            "entityTypeId": 1200,
                            "title": "Работа с дебиторкой",
                            "code": "receivable_work",
                        },
                    ]
                }
            }
        if method == "crm.category.list":
            return {"result": {"categories": [{"id": 50, "name": "Основная"}]}}
        if method == "crm.status.list":
            return {"result": [{"STATUS_ID": "DT1200_50:NEW", "NAME": "Новый", "SORT": "100"}]}
        if method == "crm.item.fields":
            return {
                "result": {
                    "fields": {
                        "title": {"title": "Название", "type": "string"},
                        "ufCrm40StableKey": {
                            "title": "Технический ключ кейса",
                            "type": "string",
                        },
                    }
                }
            }
        if method == "user.search":
            return {"result": []}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(bitrix_setup, "bitrix_call", fake_bitrix_call)
    snapshot = blueprint.build_live_snapshot("https://bitrix.example/rest/1/token")

    assert snapshot["target_collision"] is True
    assert snapshot["target_entity_type_id"] == 1200
    assert snapshot["target_stages"]["50"][0]["NAME"] == "Новый"
    assert snapshot["target_user_fields"][0]["title"] == "Технический ключ кейса"
    assert snapshot["write_methods_used"] == []
    assert calls == [
        "crm.type.list",
        "crm.category.list",
        "crm.status.list",
        "crm.item.fields",
        "user.search",
    ]
