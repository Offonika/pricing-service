from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.services.procurement_assortment_decisions import (
    REPO_ROOT,
    SUPPORTED_DECISIONS,
    load_mapping,
    sync_decision_to_manual_overrides,
    update_decision,
    validate_mapping,
)


class FakeBitrixClient:
    def __init__(self, item: dict[str, Any]) -> None:
        self.item = deepcopy(item)
        self.updates: list[tuple[int, str, dict[str, Any]]] = []

    def get_item(self, *, entity_type_id: int, item_id: str) -> dict[str, Any]:
        assert entity_type_id == 1056
        assert item_id == "7001"
        return deepcopy(self.item)

    def update_item(self, *, entity_type_id: int, item_id: str, fields: dict[str, Any]) -> None:
        assert entity_type_id == 1056
        assert item_id == "7001"
        self.updates.append((entity_type_id, item_id, deepcopy(fields)))
        self.item.update(fields)


def _mapping() -> dict[str, Any]:
    return {
        "process": {"entity_type_id": 1056},
        "field_map": {
            "auto_order_sku_code": "UF_CRM_8_AUTOORDERSKUCODE",
            "auto_order_sku_name": "UF_CRM_8_AUTOORDERSKUNAME",
            "assortment_status_decision": "UF_CRM_8_ASSORTMENTSTATUSDECISION",
            "assortment_status_reason": "UF_CRM_8_ASSORTMENTSTATUSREASON",
            "assortment_status_approved_by": "UF_CRM_8_ASSORTMENTSTATUSAPPROVEDBY",
            "assortment_status_changed_at": "UF_CRM_8_ASSORTMENTSTATUSCHANGEDAT",
            "assortment_commercial_marks": "UF_CRM_8_ASSORTMENTCOMMERCIALMARKS",
        },
        "enum_map": {
            "assortment_status_decision": {
                "no_change": "480",
                "matrix": "481",
                "working": "482",
                "on_demand": "483",
                "replace_candidate": "484",
                "nonliquid": "485",
                "do_not_order": "486",
            }
        },
    }


def _item(**overrides: Any) -> dict[str, Any]:
    item = {
        "id": "7001",
        "title": "Автозаказ витрины",
        "updatedTime": "2026-07-06T12:30:00+03:00",
        "ufCrm8Autoorderskucode": "РБ000075803",
        "ufCrm8Autoorderskuname": "Дисплей F5ENERGY",
        "ufCrm8Assortmentstatusdecision": "480",
        "ufCrm8Assortmentstatusreason": "",
        "ufCrm8Assortmentstatusapprovedby": "",
        "ufCrm8Assortmentstatuschangedat": "",
        "ufCrm8Assortmentcommercialmarks": "",
    }
    item.update(overrides)
    return item


def _settings(tmp_path: Path) -> Settings:
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(_mapping(), ensure_ascii=False), encoding="utf-8")
    return Settings(
        procurement_labels_mapping_path=str(mapping_path),
        procurement_assortment_manual_overrides_path=str(tmp_path / "manual-overrides.json"),
    )


def test_update_decision_writes_bitrix_fields_and_returns_readable_decision(tmp_path: Path) -> None:
    client = FakeBitrixClient(_item())

    decision = update_decision(
        "7001",
        {
            "status_decision": "matrix",
            "status_reason": "Собственная марка, заказано 1000 шт.",
            "status_approved_by": "Омар",
            "status_changed_at": "2026-07-06",
            "commercial_marks": ["own_brand", "rare_market_item"],
        },
        settings=_settings(tmp_path),
        client=client,
    )

    assert client.updates == [
        (
            1056,
            "7001",
            {
                "ufCrm8Assortmentstatusdecision": "481",
                "ufCrm8Assortmentstatusreason": "Собственная марка, заказано 1000 шт.",
                "ufCrm8Assortmentstatusapprovedby": "Омар",
                "ufCrm8Assortmentstatuschangedat": "2026-07-06",
                "ufCrm8Assortmentcommercialmarks": "own_brand, rare_market_item",
            },
        )
    ]
    assert decision["status_decision"] == "matrix"
    assert decision["sync_blockers"] == []
    assert decision["manual_override_preview"]["manual_status"] == "matrix"


def test_sync_decision_writes_manual_override_when_required_fields_are_filled(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    client = FakeBitrixClient(
        _item(
            ufCrm8Assortmentstatusdecision="481",
            ufCrm8Assortmentstatusreason="Собственная марка, заказано 1000 шт.",
            ufCrm8Assortmentstatusapprovedby="Омар",
            ufCrm8Assortmentstatuschangedat="2026-07-06",
        )
    )

    result = sync_decision_to_manual_overrides("7001", settings=settings, client=client)

    assert result["synced"] is True
    assert result["merge_action"] == "added"
    payload = json.loads(Path(settings.procurement_assortment_manual_overrides_path).read_text())
    assert payload["items"][0]["nomenclature_code"] == "РБ000075803"
    assert payload["items"][0]["manual_status"] == "matrix"


def test_sync_decision_reports_blockers_without_writing_manual_override(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manual_path = Path(settings.procurement_assortment_manual_overrides_path)
    client = FakeBitrixClient(
        _item(
            ufCrm8Assortmentstatusdecision="481",
            ufCrm8Assortmentstatusapprovedby="Омар",
            ufCrm8Assortmentstatuschangedat="2026-07-06",
        )
    )

    result = sync_decision_to_manual_overrides("7001", settings=settings, client=client)

    assert result["synced"] is False
    assert result["blockers"] == ["manual_reason_required"]
    assert not manual_path.exists()


def test_production_mapping_enum_map_covers_every_supported_decision() -> None:
    # Контрактный тест против РЕАЛЬНОГО файла build/bitrix/procurement_order_mapping.json,
    # а не тестовой фикстуры. Раньше все тесты этого модуля использовали _mapping()
    # с уже заполненным enum_map, поэтому пустой enum_map в проде ни разу не был замечен
    # тестами: write-путь (update_decision) в реальности падает с ValueError на первой же
    # попытке записать решение, а тесты этого не видели.
    #
    # Если этот тест красный — значит build/bitrix/procurement_order_mapping.json ещё не
    # синхронизирован с реальными Bitrix enum ID. Чинить нужно НЕ здесь: перезапустить
    # scripts/ensure_procurement_bitrix_process.py против боевого Bitrix (он идемпотентно
    # подтягивает enum_map из текущего состояния поля "Статус ассортимента: решение").
    mapping_path = REPO_ROOT / "build/bitrix/procurement_order_mapping.json"
    if not mapping_path.is_file():
        pytest.skip("production Bitrix mapping overlay is not available")
    mapping = load_mapping(mapping_path)

    validate_mapping(mapping)

    enum_map = (mapping.get("enum_map") or {}).get("assortment_status_decision") or {}
    required = {*SUPPORTED_DECISIONS, "no_change"}
    missing = sorted(required - enum_map.keys())
    assert not missing, (
        "enum_map.assortment_status_decision не содержит ID для: "
        f"{missing}. Запись решения оператора в Bitrix упадёт для этих значений. "
        "Починка: перезапустить scripts/ensure_procurement_bitrix_process.py "
        "против боевого Bitrix, не редактировать JSON руками."
    )
