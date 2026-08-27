from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from tasks import export_display_quality_review_workbook


class FakeWorkbook:
    def __init__(self) -> None:
        self.saved_paths: list[Path] = []

    def save(self, output_path: Path) -> None:
        self.saved_paths.append(output_path)


def test_display_quality_review_workbook_uses_read_only_scope(tmp_path, monkeypatch) -> None:
    session = object()
    scope_calls: list[bool] = []
    report_calls: list[object] = []
    workbook = FakeWorkbook()

    @contextmanager
    def fake_session_scope(*, read_only: bool = False):
        scope_calls.append(read_only)
        yield session

    def fake_build_report(current_session: object):
        report_calls.append(current_session)
        return {"display_matches": 2}, []

    monkeypatch.setattr(
        export_display_quality_review_workbook,
        "session_scope",
        fake_session_scope,
    )
    monkeypatch.setattr(
        export_display_quality_review_workbook,
        "build_report",
        fake_build_report,
    )
    monkeypatch.setattr(
        export_display_quality_review_workbook,
        "Workbook",
        lambda: workbook,
    )
    for writer_name in (
        "_write_instruction_sheet",
        "_write_reference_sheet",
        "_write_review_sheet",
        "_write_candidates_sheet",
        "_write_raw_sheet",
    ):
        monkeypatch.setattr(
            export_display_quality_review_workbook,
            writer_name,
            lambda *args: None,
        )

    output_path = tmp_path / "nested" / "display-quality-review.xlsx"
    summary = export_display_quality_review_workbook.build_workbook(output_path)

    assert scope_calls == [True]
    assert report_calls == [session]
    assert workbook.saved_paths == [output_path]
    assert summary == {
        "display_matches": 2,
        "written_rows": 0,
        "quality_options": 0,
        "output": str(output_path),
    }
