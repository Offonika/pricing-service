from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

from tasks import check_logistics_stage_outbox_health


def test_cli_uses_read_only_scope_and_preserves_critical_exit_code(
    monkeypatch,
    capsys,
) -> None:
    session = object()
    scope_calls: list[bool] = []
    report_calls: list[tuple[object, list[str], int]] = []
    report = {
        "status": "critical",
        "max_delay_seconds": 45,
        "pilot_rows": 1,
        "pending": 1,
        "retry": 0,
        "manual_review": 0,
        "delayed": 1,
        "oldest_active_age_seconds": 46,
        "delayed_outbox_ids": [17],
    }

    @contextmanager
    def fake_session_scope(*, read_only: bool = False):
        scope_calls.append(read_only)
        yield session

    def fake_build_health_report(
        current_session: object,
        *,
        pilot_warehouse_external_ids: list[str],
        max_delay_seconds: int,
    ) -> dict[str, object]:
        report_calls.append(
            (
                current_session,
                pilot_warehouse_external_ids,
                max_delay_seconds,
            )
        )
        return report

    monkeypatch.setattr(
        check_logistics_stage_outbox_health,
        "session_scope",
        fake_session_scope,
    )
    monkeypatch.setattr(
        check_logistics_stage_outbox_health,
        "get_settings",
        lambda: SimpleNamespace(
            logistics_stage_pilot_warehouse_external_ids=["central"],
        ),
    )
    monkeypatch.setattr(
        check_logistics_stage_outbox_health,
        "build_health_report",
        fake_build_health_report,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_logistics_stage_outbox_health",
            "--max-delay-seconds",
            "45",
        ],
    )

    assert check_logistics_stage_outbox_health.main() == 1
    assert scope_calls == [True]
    assert report_calls == [(session, ["central"], 45)]
    assert json.loads(capsys.readouterr().out) == report
