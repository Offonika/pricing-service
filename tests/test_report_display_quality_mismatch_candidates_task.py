from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from tasks import report_display_quality_mismatch_candidates


def test_report_display_quality_mismatch_cli_uses_read_only_scope(
    tmp_path, monkeypatch, capsys
) -> None:
    session = object()
    scope_calls: list[bool] = []
    report_calls: list[object] = []
    write_calls: list[tuple[Path, list[dict[str, object]]]] = []

    @contextmanager
    def fake_session_scope(*, read_only: bool = False):
        scope_calls.append(read_only)
        yield session

    rows: list[dict[str, object]] = [
        {"matching_quality_candidate_count": 0},
        {"matching_quality_candidate_count": 1},
    ]

    def fake_build_report(current_session: object):
        report_calls.append(current_session)
        return {"display_matches": 2, "quality_mismatches": 2}, rows

    def fake_write_csv(path: Path, output_rows: list[dict[str, object]]) -> None:
        write_calls.append((path, output_rows))

    output_path = tmp_path / "display-quality-mismatch.csv"
    monkeypatch.setattr(
        report_display_quality_mismatch_candidates,
        "session_scope",
        fake_session_scope,
    )
    monkeypatch.setattr(
        report_display_quality_mismatch_candidates,
        "build_report",
        fake_build_report,
    )
    monkeypatch.setattr(
        report_display_quality_mismatch_candidates,
        "write_csv",
        fake_write_csv,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "report_display_quality_mismatch_candidates",
            "--output",
            str(output_path),
            "--only-candidates",
        ],
    )

    assert report_display_quality_mismatch_candidates.main() is None
    assert scope_calls == [True]
    assert report_calls == [session]
    assert write_calls == [(output_path, [rows[1]])]
    assert json.loads(capsys.readouterr().out) == {
        "display_matches": 2,
        "quality_mismatches": 2,
        "written_rows": 1,
        "output": str(output_path),
    }
