from __future__ import annotations

from tasks.diff_assortment_lifecycle_classification import build_snapshot, diff_snapshots


def test_build_snapshot_captures_key_fields() -> None:
    records = [{"nomenclature_code": "A"}]
    snapshot = build_snapshot(records)
    assert snapshot["A"]["status"] == "fruit"
    assert set(snapshot["A"]) == {
        "status",
        "auto_order_allowed",
        "blockers",
        "manual_review_required",
        "recommended_status",
    }


def test_fact_overlay_changes_status_and_audit_detects_it() -> None:
    records = [
        {"nomenclature_code": "A"},  # чистый Плод, без оверлея
        {  # формула -> Плод, но реестр решений форсит СП
            "nomenclature_code": "B",
            "fact_status_decision": {"target_status": "sales_start"},
        },
    ]

    with_overlay = build_snapshot(records, fact_overlay=True)
    formula_only = build_snapshot(records, fact_overlay=False)

    assert with_overlay["B"]["status"] == "sales_start"
    assert formula_only["B"]["status"] == "fruit"
    assert with_overlay["A"] == formula_only["A"]  # без оверлея А не меняется

    # overlay-audit сравнивает "формула+оверлей" (было) с "чистой формулой" (станет).
    diff = diff_snapshots(with_overlay, formula_only)
    assert diff["summary"]["changed"] == 1
    assert diff["summary"]["status_transitions"] == {"sales_start -> fruit": 1}
    assert diff["changed"][0]["nomenclature_code"] == "B"


def test_diff_reports_auto_order_and_review_flips() -> None:
    before = {
        "X": {
            "status": "sale",
            "auto_order_allowed": True,
            "blockers": [],
            "manual_review_required": False,
        }
    }
    after = {
        "X": {
            "status": "sale",
            "auto_order_allowed": False,
            "blockers": ["working_window_missed"],
            "manual_review_required": True,
        }
    }
    diff = diff_snapshots(before, after)
    summary = diff["summary"]
    assert summary["changed"] == 1
    assert summary["auto_order_flips"] == {"enabled": 0, "disabled": 1}
    assert summary["manual_review_flips"] == {"enabled": 1, "disabled": 0}
    assert summary["blocker_changed"] == 1


def test_diff_tracks_added_and_removed() -> None:
    diff = diff_snapshots({"gone": {"status": "fruit"}}, {"fresh": {"status": "fruit"}})
    assert diff["removed"] == ["gone"]
    assert diff["added"] == ["fresh"]
    assert diff["summary"]["changed"] == 0
