from __future__ import annotations

import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from tasks import report_logistics_rtu_manual_review

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_report_logistics_rtu_manual_review_supports_documented_direct_invocation() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "tasks/report_logistics_rtu_manual_review.py",
            "--help",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Report open RTU logistics manual reviews." in result.stdout


def test_report_logistics_rtu_manual_review_cli_uses_read_only_scope(monkeypatch, capsys) -> None:
    session = object()
    scope_calls: list[bool] = []
    report_calls: list[tuple[object, str | None, int]] = []

    @contextmanager
    def fake_session_scope(*, read_only: bool = False):
        scope_calls.append(read_only)
        yield session

    def fake_build_report(
        current_session: object,
        *,
        review_type: str | None,
        examples_per_group: int,
    ) -> dict[str, object]:
        report_calls.append((current_session, review_type, examples_per_group))
        return {"open_count": 0, "by_type": {}, "groups": []}

    monkeypatch.setattr(
        report_logistics_rtu_manual_review,
        "session_scope",
        fake_session_scope,
    )
    monkeypatch.setattr(
        report_logistics_rtu_manual_review,
        "build_report",
        fake_build_report,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "report_logistics_rtu_manual_review",
            "--review-type",
            "rtu_target_warehouse_unresolved",
            "--examples",
            "7",
        ],
    )

    assert report_logistics_rtu_manual_review.main() == 0
    assert scope_calls == [True]
    assert report_calls == [
        (session, "rtu_target_warehouse_unresolved", 7),
    ]
    assert json.loads(capsys.readouterr().out) == {
        "open_count": 0,
        "by_type": {},
        "groups": [],
    }
