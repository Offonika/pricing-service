from __future__ import annotations

from app.services.display_scope_policy import (
    DISPLAY_SCOPE_POLICY_VERSION,
    EXCLUDED_DISPLAY_NAME_BITOK,
    display_scope_exclusion_reason,
    filter_display_scope_records,
    normalize_display_scope_name,
)


def test_bitok_scope_rule_normalizes_case_yo_and_spaces() -> None:
    assert normalize_display_scope_name("  БИТОК\tЁлка  ") == "биток елка"
    for name in (
        "Дисплей (биток)",
        "Дисплей, БИТОК!",
        "Дисплей биток-то",
        "Дисплей\tбиток\nORIG",
    ):
        assert display_scope_exclusion_reason(name) == EXCLUDED_DISPLAY_NAME_BITOK


def test_bitok_scope_rule_requires_a_whole_word() -> None:
    for name in ("Дисплей небиток", "Дисплей биток123", "Дисплей битоковый"):
        assert display_scope_exclusion_reason(name) is None


def test_scope_filter_builds_unique_reasoned_exclusion_registry() -> None:
    rows = [
        {"nomenclature_code": "A", "name": "Дисплей (биток)"},
        {"nomenclature_code": "A", "name": "Дисплей (биток)"},
        {"nomenclature_code": "B", "name": "Дисплей обычный"},
    ]

    result = filter_display_scope_records(rows)

    assert [row["nomenclature_code"] for row in result.included] == ["B"]
    assert result.audit == {
        "scope_policy_version": DISPLAY_SCOPE_POLICY_VERSION,
        "source_item_count": 3,
        "included_item_count": 1,
        "excluded_item_count": 1,
        "excluded_row_count": 2,
        "excluded_reason_counts": {EXCLUDED_DISPLAY_NAME_BITOK: 1},
        "exclusions": [
            {
                "nomenclature_code": "A",
                "name": "Дисплей (биток)",
                "reason_code": EXCLUDED_DISPLAY_NAME_BITOK,
                "scope_policy_version": DISPLAY_SCOPE_POLICY_VERSION,
            }
        ],
    }
