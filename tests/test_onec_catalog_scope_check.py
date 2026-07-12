from scripts.check_onec_catalog_scope import evaluate_catalog_scope


def test_catalog_scope_accepts_existing_two_item_gap() -> None:
    result = evaluate_catalog_scope(
        {"A", "B", "C"},
        {"A"},
        baseline_source_count=3,
        max_missing=2,
        max_outside=0,
        max_drop_percent=0.5,
    )

    assert result["status"] == "ok"
    assert result["source_missing_in_active"] == 2


def test_catalog_scope_blocks_outside_and_large_drop() -> None:
    result = evaluate_catalog_scope(
        {"A"},
        {"A", "OUTSIDE"},
        baseline_source_count=100,
        max_missing=2,
        max_outside=0,
        max_drop_percent=0.5,
    )

    assert result["status"] == "failed"
    assert result["active_outside_source"] == 1
    assert result["checks"]["source_count_ok"] is False
