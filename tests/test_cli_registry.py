from __future__ import annotations

from scripts.validate_cli_registry import find_errors, load_registry, task_files


def test_cli_registry_covers_all_tasks_and_cron_adapters() -> None:
    assert task_files()
    assert find_errors(load_registry()) == []
