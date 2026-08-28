from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from tasks import export_display_matching_review_workbook


def test_display_matching_review_workbook_cli_uses_read_only_scope(
    tmp_path, monkeypatch, capsys
) -> None:
    session = object()
    scope_calls: list[bool] = []
    export_calls: list[tuple[object, Path, Path, float, float]] = []

    @contextmanager
    def fake_session_scope(*, read_only: bool = False):
        scope_calls.append(read_only)
        yield session

    def fake_export_workbook(
        current_session: object,
        *,
        input_report: Path,
        output: Path,
        safe_score: float,
        safe_gap: float,
    ) -> dict[str, int]:
        export_calls.append((current_session, input_report, output, safe_score, safe_gap))
        return {"display_rows": 3, "safe_suggested": 2}

    input_path = tmp_path / "display-matching.csv"
    output_path = tmp_path / "display-matching-review.xlsx"
    monkeypatch.setattr(
        export_display_matching_review_workbook,
        "session_scope",
        fake_session_scope,
    )
    monkeypatch.setattr(
        export_display_matching_review_workbook,
        "export_workbook",
        fake_export_workbook,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "export_display_matching_review_workbook",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--safe-score",
            "0.91",
            "--safe-gap",
            "0.04",
        ],
    )

    assert export_display_matching_review_workbook.main() is None
    assert scope_calls == [True]
    assert export_calls == [(session, input_path, output_path, 0.91, 0.04)]
    assert json.loads(capsys.readouterr().out) == {
        "display_rows": 3,
        "safe_suggested": 2,
    }
