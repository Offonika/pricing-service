from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from scripts import analyze_site_defect_working_cases


@pytest.mark.parametrize(
    ("mode", "expected_apply"),
    [
        ("--dry-run", False),
        ("--apply", True),
    ],
)
def test_analyze_site_defect_working_cases_uses_central_read_only_scope(
    mode: str,
    expected_apply: bool,
    monkeypatch,
    capsys,
) -> None:
    session = object()
    settings = SimpleNamespace()
    scope_calls: list[bool] = []
    analyze_calls: list[tuple[object, object, str, int, bool]] = []
    summary = {"status": "ready", "apply": expected_apply, "items": [{"id": "321"}]}

    @contextmanager
    def fake_session_scope(*, read_only: bool = False):
        scope_calls.append(read_only)
        yield session

    def fake_analyze_bitrix_working_reclamations(
        current_session: object,
        *,
        settings: object,
        item_id: str,
        limit: int,
        apply: bool,
    ) -> dict[str, object]:
        analyze_calls.append((current_session, settings, item_id, limit, apply))
        return summary

    monkeypatch.setattr(
        analyze_site_defect_working_cases,
        "get_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        analyze_site_defect_working_cases,
        "session_scope",
        fake_session_scope,
    )
    monkeypatch.setattr(
        analyze_site_defect_working_cases,
        "analyze_bitrix_working_reclamations",
        fake_analyze_bitrix_working_reclamations,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "analyze_site_defect_working_cases",
            "--case-id",
            "321",
            "--limit",
            "7",
            mode,
        ],
    )

    assert analyze_site_defect_working_cases.main() == 0

    assert scope_calls == [True]
    assert analyze_calls == [(session, settings, "321", 7, expected_apply)]
    assert json.loads(capsys.readouterr().out) == summary
