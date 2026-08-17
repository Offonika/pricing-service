from __future__ import annotations

import json
from pathlib import Path

import pytest

from tasks.report_display_scope_exclusion_dry_run import (
    DEFAULT_SOURCE,
    build_display_scope_exclusion_dry_run,
    write_display_scope_exclusion_dry_run,
)


def test_scope_exclusion_dry_run_writes_separate_read_only_bundle(tmp_path: Path) -> None:
    source = tmp_path / "inventory.json"
    source.write_text(
        json.dumps(
            {
                "schema": "display_family_inventory.v2",
                "items": [
                    {"nomenclature_code": "A", "name": "Дисплей (биток)"},
                    {"nomenclature_code": "B", "name": "Дисплей обычный"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    payload = json.loads(source.read_text(encoding="utf-8"))
    result = build_display_scope_exclusion_dry_run(
        payload,
        expected_source_count=2,
        expected_excluded_count=1,
        expected_included_count=1,
    )

    manifest = write_display_scope_exclusion_dry_run(
        tmp_path / "result",
        result=result,
        source_path=source,
    )

    assert manifest["status"] == "accepted"
    assert manifest["production_action"] == "none_read_only"
    assert source.read_text(encoding="utf-8") == json.dumps(payload, ensure_ascii=False)
    exclusions = json.loads((tmp_path / "result" / "exclusions.json").read_text(encoding="utf-8"))
    assert exclusions["items"] == [
        {
            "name": "Дисплей (биток)",
            "nomenclature_code": "A",
            "reason_code": "excluded_display_name_bitok",
            "scope_policy_version": "display_scope_policy.v1",
        }
    ]


def test_accepted_inventory_control_is_exactly_11_excluded_and_2678_included() -> None:
    if not DEFAULT_SOURCE.exists():
        pytest.skip("accepted display-family inventory is an external runtime artifact")
    payload = json.loads(DEFAULT_SOURCE.read_text(encoding="utf-8-sig"))

    result = build_display_scope_exclusion_dry_run(
        payload,
        expected_source_count=2689,
        expected_excluded_count=11,
        expected_included_count=2678,
    )

    assert result["summary"]["status"] == "accepted"
    assert result["summary"]["checks"] == {
        "source_count_matches": True,
        "excluded_count_matches": True,
        "included_count_matches": True,
        "included_cohort_has_no_bitok": True,
        "exclusion_registry_is_unique": True,
        "reason_count_matches": True,
    }
