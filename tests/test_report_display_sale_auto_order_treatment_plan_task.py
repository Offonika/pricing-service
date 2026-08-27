from __future__ import annotations

import csv
import json
from contextlib import contextmanager

from tasks import report_display_sale_auto_order_treatment_plan


def test_display_sale_treatment_plan_cli_uses_read_only_scope_and_db_override(
    tmp_path, monkeypatch, capsys
) -> None:
    session = object()
    scope_calls: list[tuple[bool, str | None]] = []
    load_calls: list[tuple[object, str]] = []

    @contextmanager
    def fake_session_scope(*, read_only: bool = False, database_url: str | None = None):
        scope_calls.append((read_only, database_url))
        yield session

    def fake_load_sale_rows(current_session: object, *, folder: str):
        load_calls.append((current_session, folder))
        return [
            {
                "nomenclature_code": "РБ0001",
                "name": "Дисплей тестовый",
                "status": "sale",
                "future_ka_mapping_status": "needs_mapping",
                "demand_method_code": "manual_review",
                "manual_review_required": True,
                "quality_raw": "",
                "blockers": ["working_confirmation_required"],
                "export_blockers": [],
                "expensive_profile": "",
                "reason_text": "Нужна проверка",
            }
        ]

    output_csv = tmp_path / "display-sale-treatment-plan.csv"
    monkeypatch.setattr(
        report_display_sale_auto_order_treatment_plan,
        "session_scope",
        fake_session_scope,
    )
    monkeypatch.setattr(
        report_display_sale_auto_order_treatment_plan,
        "load_sale_rows",
        fake_load_sale_rows,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "report_display_sale_auto_order_treatment_plan",
            "--database-url",
            "sqlite:///override.db",
            "--folder",
            "дисплеи",
            "--output-csv",
            str(output_csv),
            "--json",
        ],
    )

    assert report_display_sale_auto_order_treatment_plan.main() == 0
    assert scope_calls == [(True, "sqlite:///override.db")]
    assert load_calls == [(session, "дисплеи")]

    csv_rows = list(csv.DictReader(output_csv.read_text(encoding="utf-8-sig").splitlines()))
    assert [row["code"] for row in csv_rows] == ["РБ0001"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"
    assert payload["items"] == 1
    assert payload["output_csv"] == str(output_csv)
