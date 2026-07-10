from __future__ import annotations

from datetime import date
from pathlib import Path

from tasks.build_display_analog_winner_overrides import (
    ANALOG_WINNER_RULE,
    build_override_payload,
    load_analog_winner_rows,
)


def test_load_analog_winner_rows_keeps_only_review_winners_with_need(tmp_path: Path) -> None:
    path = tmp_path / "review.csv"
    path.write_text(
        "\n".join(
            [
                "nomenclature_code,name,analog_role,analog_group_recommended_order_qty,dry_run_decision,warnings",
                "RB-WIN,Winner,primary_analog,5,manual_review,analog_group_consolidated; analog_winner_not_auto_order_allowed",
                "RB-ZERO,Zero,primary_analog,0,manual_review,analog_winner_not_auto_order_allowed",
                "RB-LOSER,Loser,transition_to_better_analog,5,do_not_order,analog_transition_to_better_item",
                "RB-ORDER,Order,primary_analog,5,order,analog_group_consolidated",
            ]
        ),
        encoding="utf-8",
    )

    rows = load_analog_winner_rows(path)

    assert [row["nomenclature_code"] for row in rows] == ["RB-WIN"]


def test_build_override_payload_preserves_manual_stops_and_adds_winner() -> None:
    payload = build_override_payload(
        [
            {
                "nomenclature_code": "RB-WIN",
                "name": "Winner display",
                "analog_group_id": "analog-1",
                "analog_group_size": "3",
                "analog_group_recommended_order_qty": "5",
                "analog_group_net_sales_qty": "100",
                "analog_winner_score": "200",
            },
            {
                "nomenclature_code": "RB-STOP",
                "name": "Stopped display",
                "analog_group_id": "analog-2",
                "analog_group_size": "2",
                "analog_group_recommended_order_qty": "4",
                "analog_group_net_sales_qty": "80",
                "analog_winner_score": "150",
            },
        ],
        base_overrides={
            "items": [
                {
                    "nomenclature_code": "RB-STOP",
                    "manual_status": "nonliquid",
                    "manual_reason": "manual stop",
                }
            ]
        },
        review_csv=Path("review.csv"),
        approved_by="chat_test",
        changed_at=date(2026, 7, 4),
    )

    by_code = {item["nomenclature_code"]: item for item in payload["items"]}
    assert by_code["RB-WIN"]["working_confirmed_by_folder_responsible"] is True
    assert by_code["RB-WIN"]["analog_winner_confirmed_by_folder_responsible"] is True
    assert by_code["RB-WIN"]["approval_rule"] == ANALOG_WINNER_RULE
    assert by_code["RB-WIN"]["manual_approved_by"] == "chat_test"
    assert "победитель группы аналогов" in by_code["RB-WIN"]["manual_reason"]

    assert by_code["RB-STOP"]["manual_status"] == "nonliquid"
    assert "working_confirmed_by_folder_responsible" not in by_code["RB-STOP"]
    assert payload["_analog_winner_confirmation_added"] == 1
    assert payload["_analog_winner_confirmation_skipped_manual_stops"] == ["RB-STOP"]
