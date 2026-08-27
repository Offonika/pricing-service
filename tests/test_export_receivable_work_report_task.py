from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path

from tasks import export_receivable_work_report


def test_export_receivable_work_report_cli_uses_read_only_scope(
    tmp_path, monkeypatch, capsys
) -> None:
    session = object()
    scope_calls: list[bool] = []
    load_calls: list[object] = []
    export_calls: list[tuple[list[object], Path]] = []

    @contextmanager
    def fake_session_scope(*, read_only: bool = False):
        scope_calls.append(read_only)
        yield session

    def fake_load_items(current_session: object) -> list[object]:
        load_calls.append(current_session)
        return []

    def fake_export(items: list[object], *, output_path: Path) -> Path:
        export_calls.append((items, output_path))
        return output_path

    business_date = date(2026, 8, 27)
    expected_path = (
        tmp_path / business_date.isoformat() / f"Дебиторка покупателей {business_date}.xlsx"
    )
    monkeypatch.setattr(
        export_receivable_work_report,
        "session_scope",
        fake_session_scope,
    )
    monkeypatch.setattr(
        export_receivable_work_report,
        "load_receivable_work_report_items",
        fake_load_items,
    )
    monkeypatch.setattr(
        export_receivable_work_report,
        "export_receivable_work_report",
        fake_export,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "export_receivable_work_report",
            "--date",
            business_date.isoformat(),
            "--output-dir",
            str(tmp_path),
        ],
    )

    assert export_receivable_work_report.main() is None
    assert scope_calls == [True]
    assert load_calls == [session]
    assert export_calls == [([], expected_path)]
    assert capsys.readouterr().out.strip() == str(expected_path)
