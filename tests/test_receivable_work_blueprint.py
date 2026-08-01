from __future__ import annotations

import scripts.build_receivable_work_blueprint as blueprint


def test_receivable_work_blueprint_is_write_free_and_has_target_stages() -> None:
    result = blueprint.build_blueprint()

    assert result["mode"] == "dry-run"
    assert result["safety"]["bitrix_writes"] is False
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
