from __future__ import annotations

import json

import pytest

from tasks import sync_receivable_workflow as task


def test_cli_passes_safe_card_sync_options(monkeypatch, capsys) -> None:
    captured = {}

    def run(**kwargs):
        captured.update(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(task, "run_receivable_workflow_sync", run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "sync_receivable_workflow.py",
            "--date",
            "2026-07-16",
            "--force",
            "--bitrix-only",
            "--all-departments",
            "--batch-size",
            "25",
        ],
    )

    assert task.main() == 0
    assert captured["bitrix_only"] is True
    assert captured["all_departments"] is True
    assert captured["batch_size"] == 25
    assert captured["allow_closure"] is True
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_cli_rejects_batching_without_bitrix_only(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["sync_receivable_workflow.py", "--batch-size", "25"],
    )

    with pytest.raises(SystemExit) as exc_info:
        task.main()

    assert exc_info.value.code == 2
