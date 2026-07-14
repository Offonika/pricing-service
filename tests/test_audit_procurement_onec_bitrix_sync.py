from __future__ import annotations

import json

from scripts.audit_procurement_onec_bitrix_sync import build_audit


def test_procurement_onec_bitrix_audit_accepts_current_cargo_rows(tmp_path) -> None:
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps({"orders": [{"number": "РБГУ0000300"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    result_path = tmp_path / "result.json"
    result_path.write_text(
        json.dumps(
            {
                "mode": "apply",
                "input_json": str(input_path),
                "rows": [
                    {
                        "source_number": "РБГУ0000300",
                        "action": "noop",
                        "contour": "cargo",
                        "initial_stage_key": "payment_work",
                        "stage_key": "payment_work",
                        "blocked_supplier": False,
                        "field_names": ["UF_CRM_8_ONECSOURCENUMBER", "stageId"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    audit = build_audit([result_path])

    assert audit["status"] == "ok"
    assert audit["acceptance"] == {
        "all_rows_have_1c_source_number": True,
        "no_1c_orders_in_need_stage": True,
        "every_input_order_has_result": True,
    }
    assert audit["totals"]["rows"] == 1


def test_procurement_onec_bitrix_audit_flags_need_stage_and_missing_source(tmp_path) -> None:
    result_path = tmp_path / "result.json"
    result_path.write_text(
        json.dumps(
            {
                "mode": "apply",
                "rows": [
                    {
                        "source_number": "",
                        "action": "created",
                        "contour": "ved_import",
                        "initial_stage_key": "need",
                        "stage_key": "need",
                        "field_names": ["stageId"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    audit = build_audit([result_path])

    assert audit["status"] == "attention"
    assert audit["acceptance"] == {
        "all_rows_have_1c_source_number": False,
        "no_1c_orders_in_need_stage": False,
        "every_input_order_has_result": True,
    }
    assert {violation["type"] for violation in audit["violations"]} == {
        "missing_source_number",
        "forbidden_stage",
        "missing_onec_source_number_field",
    }
