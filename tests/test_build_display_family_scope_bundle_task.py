from __future__ import annotations

from copy import deepcopy

import pytest

from tasks.build_display_family_scope_bundle import build_successor_inventory


def _item(index: int, *, name: str, family: str) -> dict:
    return {
        "product_id": index,
        "nomenclature_code": f"CODE-{index}",
        "name": name,
        "proposed_family_id": family,
        "segment_id": "unknown|unknown|frame_unknown|ic_pad_unknown",
        "proposal_status": "singleton_unresolved_model",
        "proposal_warnings": ["quality_unknown"],
        "proposal_notes": [],
        "requires_manual_review": True,
        "scope_classification_reason": "explicit_display_module_name",
        "scope_classification_warnings": [],
        "matching_audit": {
            "accepted_count": 0,
            "manual_accepted_count": 0,
            "requires_review": False,
            "relation_counts": {},
            "property_disagreement_counts": {},
        },
    }


def test_successor_inventory_is_deterministic_and_filters_before_family_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tasks.build_display_family_scope_bundle.EXPECTED_SOURCE_MEMBER_COUNT", 3)
    monkeypatch.setattr("tasks.build_display_family_scope_bundle.EXPECTED_EXCLUDED_COUNT", 1)
    monkeypatch.setattr("tasks.build_display_family_scope_bundle.EXPECTED_TARGET_MEMBER_COUNT", 2)
    source = {
        "schema": "display_family_inventory.v2",
        "as_of": "2026-08-16",
        "inventory_checksum": "a" * 64,
        "summary": {
            "included_display_sku_count": 3,
            "proposed_family_count": 2,
            "display_scope_reason_counts": {"explicit_display_module_name": 3},
            "display_scope_warning_counts": {},
        },
        "scope_audit": {"conflict_count": 0, "conflicts": []},
        "source_quality": {"status": "ready", "gates": {}},
        "source_warnings": [],
        "items": [
            _item(1, name="Дисплей обычный 1", family="shared"),
            _item(2, name="Дисплей обычный 2", family="shared"),
            _item(3, name="Дисплей (биток)", family="excluded-singleton"),
        ],
    }
    manifest = {"artifact_sha256": {"inventory.json": "b" * 64}}

    first, exclusions = build_successor_inventory(source, source_manifest=manifest)
    second, _ = build_successor_inventory(deepcopy(source), source_manifest=manifest)

    assert first == second
    assert first["summary"]["included_display_sku_count"] == 2
    assert first["summary"]["proposed_family_count"] == 1
    assert first["scope_audit"]["excluded_item_count"] == 1
    assert exclusions[0]["reason_code"] == "excluded_display_name_bitok"
