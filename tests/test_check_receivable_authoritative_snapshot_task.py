from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date

from tasks import check_receivable_authoritative_snapshot


def test_authoritative_snapshot_cli_uses_read_only_scope(monkeypatch, capsys) -> None:
    session = object()
    scope_calls: list[bool] = []
    report_calls: list[tuple[object, date, tuple[str, ...]]] = []
    report = {
        "snapshot": {"snapshot_date": "2026-08-28", "row_count": 7},
        "synthetic": {
            "balance_snapshot_rows": 0,
            "reconciliation_snapshot_rows": 0,
            "case_rows": 0,
        },
        "case_segments": [],
        "controls": [],
    }

    @contextmanager
    def fake_session_scope(*, read_only: bool = False):
        scope_calls.append(read_only)
        yield session

    def fake_build_report(
        current_session: object,
        *,
        snapshot_date: date,
        control_names: tuple[str, ...],
    ) -> dict[str, object]:
        report_calls.append((current_session, snapshot_date, control_names))
        return report

    monkeypatch.setattr(
        check_receivable_authoritative_snapshot,
        "session_scope",
        fake_session_scope,
    )
    monkeypatch.setattr(
        check_receivable_authoritative_snapshot,
        "build_authoritative_snapshot_report",
        fake_build_report,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_receivable_authoritative_snapshot",
            "--snapshot-date",
            "2026-08-28",
            "--control-name",
            "Контрагент 1",
            "--control-name",
            "Контрагент 2",
        ],
    )

    assert check_receivable_authoritative_snapshot.main() is None
    assert scope_calls == [True]
    assert report_calls == [
        (
            session,
            date(2026, 8, 28),
            ("Контрагент 1", "Контрагент 2"),
        )
    ]
    assert json.loads(capsys.readouterr().out) == report
