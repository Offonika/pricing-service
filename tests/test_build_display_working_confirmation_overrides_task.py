from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date
from typing import Any

from tasks import build_display_working_confirmation_overrides


def test_display_working_confirmation_cli_uses_read_only_scope(
    tmp_path, monkeypatch, capsys
) -> None:
    session = object()
    candidate = {"nomenclature_code": "001"}
    payload = {"items": [{"nomenclature_code": "001"}]}
    scope_calls: list[tuple[bool, str | None]] = []
    load_calls: list[tuple[object, str, bool]] = []
    payload_calls: list[tuple[list[dict[str, str]], Any, int, str, str, date]] = []

    @contextmanager
    def fake_session_scope(
        *,
        read_only: bool = False,
        database_url: str | None = None,
    ):
        scope_calls.append((read_only, database_url))
        yield session

    def fake_load_candidates(
        current_session: object,
        *,
        folder: str,
        include_expensive: bool,
    ) -> tuple[list[dict[str, str]], int]:
        load_calls.append((current_session, folder, include_expensive))
        return [candidate], 17

    def fake_build_payload(
        candidates: list[dict[str, str]],
        *,
        base_overrides: Any,
        source_run_id: int,
        folder: str,
        approved_by: str,
        changed_at: date,
    ) -> dict[str, list[dict[str, str]]]:
        payload_calls.append(
            (
                candidates,
                base_overrides,
                source_run_id,
                folder,
                approved_by,
                changed_at,
            )
        )
        return payload

    base_path = tmp_path / "base-overrides.json"
    base_path.write_text(
        json.dumps({"items": [{"nomenclature_code": "base"}]}),
        encoding="utf-8",
    )
    output_path = tmp_path / "working-confirmed.json"
    monkeypatch.setattr(
        build_display_working_confirmation_overrides,
        "session_scope",
        fake_session_scope,
    )
    monkeypatch.setattr(
        build_display_working_confirmation_overrides,
        "load_working_confirmation_candidates",
        fake_load_candidates,
    )
    monkeypatch.setattr(
        build_display_working_confirmation_overrides,
        "build_override_payload",
        fake_build_payload,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "build_display_working_confirmation_overrides",
            "--database-url",
            "sqlite:///snapshot.db",
            "--folder",
            "дисплеи oled",
            "--base-overrides-json",
            str(base_path),
            "--output-json",
            str(output_path),
            "--approved-by",
            "owner",
            "--changed-at",
            "2026-08-28",
            "--include-expensive",
            "--json",
        ],
    )

    assert build_display_working_confirmation_overrides.main() == 0
    assert scope_calls == [(True, "sqlite:///snapshot.db")]
    assert load_calls == [(session, "дисплеи oled", True)]
    assert payload_calls == [
        (
            [candidate],
            {"items": [{"nomenclature_code": "base"}]},
            17,
            "дисплеи oled",
            "owner",
            date(2026, 8, 28),
        )
    ]
    assert json.loads(output_path.read_text(encoding="utf-8")) == payload
    assert json.loads(capsys.readouterr().out) == {
        "status": "ready",
        "source_run_id": 17,
        "working_confirmation_candidates": 1,
        "base_override_rows": 1,
        "output_items": 1,
        "output_json": str(output_path),
    }
