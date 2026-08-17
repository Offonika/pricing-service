from __future__ import annotations

from pathlib import Path

from tasks.diff_assortment_lifecycle_classification import (
    _attach_order_and_capital_diff,
    _build_target_audit_rows,
    _load_order_rows,
    _target_audit_sections,
    build_snapshot,
    diff_snapshots,
)


def test_build_snapshot_captures_key_fields() -> None:
    records = [{"nomenclature_code": "A"}]
    snapshot = build_snapshot(records)
    assert snapshot["A"]["status"] == "fruit"
    assert {
        "status",
        "auto_order_allowed",
        "blockers",
        "manual_review_required",
        "recommended_status",
    }.issubset(snapshot["A"])
    assert snapshot["A"]["demand_state"] is None
    assert snapshot["A"]["first_receipt_at"] is None


def test_display_snapshot_keeps_archived_display_and_rejects_matrix_accessory() -> None:
    snapshot = build_snapshot(
        [
            {
                "nomenclature_code": "DISPLAY",
                "folder_path": "Архив",
                "subject_1c": "Дисплей",
            },
            {
                "nomenclature_code": "GLUE",
                "folder_path": "Проклейки для Apple MacBook",
                "subject_1c": "скотч",
                "name": "Проклейка матрицы Apple MacBook Air",
            },
        ],
        folder_filter="дисплеи",
    )

    assert set(snapshot) == {"DISPLAY"}


def test_target_snapshot_captures_dates_sales_availability_and_cost() -> None:
    record = {
        "nomenclature_code": "FACTS",
        "first_receipt_at": "2020-01-01",
        "last_receipt_at": "2026-07-01",
        "history_age_days": 2400,
        "first_sale_at": "2020-01-10",
        "last_sale_at": "2026-08-01",
        "sales_qty_short": "3",
        "sales_qty_medium": "8",
        "sales_qty_long": "15",
        "days_in_sale_short": "30",
        "days_in_sale_medium": "80",
        "days_in_sale_long": "150",
        "inventory_cost_per_unit": "100.50",
        "cost_quartile": "Q2",
        "minimum_representation_qty": 13,
    }

    item = build_snapshot([record], target_model=True)["FACTS"]

    assert item["first_sale_at"] == "2020-01-10"
    assert item["sales_qty_short"] == "3"
    assert item["days_in_sale_long"] == "150"
    assert item["inventory_cost_per_unit"] == "100.50"


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


def test_current_audit_snapshot_uses_persisted_previous_stage() -> None:
    snapshot = build_snapshot(
        [
            {
                "nomenclature_code": "OLD",
                "previous_status": "working",
            }
        ],
        use_previous_status=True,
    )

    assert snapshot["OLD"]["status"] == "working"


def test_current_audit_snapshot_uses_persisted_previous_flags() -> None:
    item = build_snapshot(
        [
            {
                "nomenclature_code": "OLD",
                "previous_status": "working",
                "previous_classification_available": True,
                "previous_auto_order_allowed": False,
                "previous_manual_review_required": True,
                "previous_blockers": ["legacy_blocker"],
                "previous_reason_codes": ["legacy_reason"],
            }
        ],
        use_previous_status=True,
    )["OLD"]

    assert item["auto_order_allowed"] is False
    assert item["manual_review_required"] is True
    assert item["blockers"] == ["legacy_blocker"]
    assert item["reason_codes"] == ["legacy_reason"]


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


def test_target_audit_adds_order_capital_and_required_sections() -> None:
    before = {
        "OLD": {"status": "sale", "demand_state": None},
        "SPIKE": {"status": "working", "demand_state": None},
    }
    after = {
        "OLD": {
            "status": "working",
            "demand_state": "stable",
            "inventory_cost_per_unit": "100",
        },
        "SPIKE": {
            "status": "working",
            "demand_state": "spike",
            "inventory_cost_per_unit": "50",
        },
    }
    diff = diff_snapshots(before, after)
    diff["audit_rows"] = _build_target_audit_rows(before, after)
    _attach_order_and_capital_diff(
        diff,
        {"OLD": {"recommended_order_qty": "3"}, "SPIKE": {"recommended_order_qty": "2"}},
        {"OLD": {"recommended_order_qty": "1"}, "SPIKE": {"recommended_order_qty": "4"}},
    )
    sections = _target_audit_sections(diff)
    assert sections["exits_from_growing"] == ["OLD"]
    assert sections["spikes"] == ["SPIKE"]
    assert diff["summary"]["recommended_order_delta_qty"] == 0
    assert diff["summary"]["capital_delta"] == -100


def test_target_audit_keeps_unchanged_skus_and_special_sections_use_full_cohort() -> None:
    before = {
        "UNCHANGED-MANUAL": {"status": "pension", "demand_state": "stable"},
        "UNCHANGED-UNKNOWN": {
            "status": "sales_start",
            "demand_state": "no_data",
            "blockers": ["demand_data_missing"],
        },
        "UNCHANGED-MISSING-ORDER": {
            "status": "newborn",
            "demand_state": "no_sales",
            "blockers": ["first_supplier_order_fact_missing"],
        },
    }
    after = {code: dict(value) for code, value in before.items()}
    diff = diff_snapshots(before, after)
    diff["audit_rows"] = _build_target_audit_rows(before, after)

    sections = _target_audit_sections(diff)

    assert diff["summary"]["changed"] == 0
    assert [row["nomenclature_code"] for row in diff["audit_rows"]] == [
        "UNCHANGED-MANUAL",
        "UNCHANGED-MISSING-ORDER",
        "UNCHANGED-UNKNOWN",
    ]
    assert all(row["changed"] is False for row in diff["audit_rows"])
    assert sections["manual_statuses"] == ["UNCHANGED-MANUAL"]
    assert sections["unknown_facts"] == [
        "UNCHANGED-MISSING-ORDER",
        "UNCHANGED-UNKNOWN",
    ]


def test_order_audit_loader_accepts_dry_run_csv(tmp_path: Path) -> None:
    path = tmp_path / "orders.csv"
    path.write_text(
        "nomenclature_code,recommended_order_qty\nSKU-1,7\n",
        encoding="utf-8",
    )

    assert _load_order_rows(path) == {
        "SKU-1": {"nomenclature_code": "SKU-1", "recommended_order_qty": "7"}
    }
