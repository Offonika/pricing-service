from __future__ import annotations

import json
import sys

from infra.cron import bitrix_ingest_calls


def test_plan_only_json_has_no_side_effects(monkeypatch, capsys, tmp_path) -> None:
    state_path = tmp_path / "progress.json"
    monkeypatch.setattr(
        bitrix_ingest_calls,
        "load_env",
        lambda _path: {
            "BITRIX24_WEBHOOK_URL": "https://example.invalid/rest/1/masked",
            "DATABASE_URL": "postgresql://example/masked",
            "BITRIX_INGEST_STATE_FILE": str(state_path),
        },
    )
    monkeypatch.setattr(
        bitrix_ingest_calls,
        "ensure_calls_schema",
        lambda _env: (_ for _ in ()).throw(AssertionError("DDL must not run in plan-only")),
    )
    monkeypatch.setattr(
        bitrix_ingest_calls,
        "run_psql",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("SQL must not run in plan-only")
        ),
    )
    monkeypatch.setattr(sys, "argv", ["bitrix_ingest_calls.py", "--plan-only", "--json"])

    bitrix_ingest_calls.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"
    assert payload["side_effects"] is False
    assert "calls" in payload["would_touch_tables"]
    assert payload["counts"]["progress_exists"] is False


def test_plan_only_reports_missing_required_env() -> None:
    payload = bitrix_ingest_calls.build_plan({})

    assert payload["status"] == "blocked"
    assert payload["side_effects"] is False
    assert "missing env: BITRIX_INGEST_WEBHOOK_URL or BITRIX24_WEBHOOK_URL" in payload["errors"]
    assert "missing env: DATABASE_URL" in payload["errors"]
