from __future__ import annotations

from contextlib import contextmanager
from datetime import date

from tasks import analyze_manual_matching_feedback


def test_analyze_manual_matching_feedback_cli_uses_read_only_scope_and_db_override(
    monkeypatch,
) -> None:
    session = object()
    scope_calls: list[tuple[bool, str | None]] = []
    report_calls: list[tuple[object, date | None, int]] = []

    @contextmanager
    def fake_session_scope(*, read_only: bool = False, database_url: str | None = None):
        scope_calls.append((read_only, database_url))
        yield session

    def fake_build_report(
        current_session: object,
        *,
        as_of: date | None = None,
        sample_limit: int = 20,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        report_calls.append((current_session, as_of, sample_limit))
        return {"as_of": "2026-08-27"}, []

    monkeypatch.setattr(
        analyze_manual_matching_feedback,
        "session_scope",
        fake_session_scope,
    )
    monkeypatch.setattr(
        analyze_manual_matching_feedback,
        "build_manual_matching_feedback_report",
        fake_build_report,
    )

    report = analyze_manual_matching_feedback.main(
        [
            "--as-of",
            "2026-08-27",
            "--database-url",
            "sqlite:///override.db",
            "--sample-limit",
            "7",
            "--no-files",
        ]
    )

    assert scope_calls == [(True, "sqlite:///override.db")]
    assert report_calls == [(session, date(2026, 8, 27), 7)]
    assert report == {"as_of": "2026-08-27", "artifacts": {}}
