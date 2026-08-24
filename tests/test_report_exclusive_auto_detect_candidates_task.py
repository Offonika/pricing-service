from __future__ import annotations

import json
from contextlib import contextmanager

from tasks import report_exclusive_auto_detect_candidates


def test_report_cli_uses_read_only_scope(monkeypatch, capsys) -> None:
    session = object()
    scope_calls: list[bool] = []
    report_calls: list[tuple[object, str, int | None]] = []

    @contextmanager
    def fake_session_scope(*, read_only: bool = False):
        scope_calls.append(read_only)
        yield session

    def fake_build_report(
        current_session: object, *, folder_filter: str, limit: int | None
    ) -> dict[str, object]:
        report_calls.append((current_session, folder_filter, limit))
        return {"candidate_count": 0, "candidates": []}

    monkeypatch.setattr(
        report_exclusive_auto_detect_candidates,
        "session_scope",
        fake_session_scope,
    )
    monkeypatch.setattr(
        report_exclusive_auto_detect_candidates,
        "build_report",
        fake_build_report,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "report_exclusive_auto_detect_candidates",
            "--folder",
            "запчасти",
            "--limit",
            "7",
        ],
    )

    assert report_exclusive_auto_detect_candidates.main() == 0

    assert scope_calls == [True]
    assert report_calls == [(session, "запчасти", 7)]
    assert json.loads(capsys.readouterr().out) == {
        "candidate_count": 0,
        "candidates": [],
    }
