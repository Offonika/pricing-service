from __future__ import annotations

from scripts.validate_cli_registry import (
    _uses_central_read_only_scope,
    find_errors,
    load_registry,
    task_files,
)


def test_cli_registry_covers_all_tasks_and_cron_adapters() -> None:
    assert task_files()
    assert find_errors(load_registry()) == []


def test_central_read_only_scope_detection(tmp_path) -> None:
    command = tmp_path / "read_only_command.py"
    command.write_text(
        "from app.infrastructure.db import session_scope\n"
        "with session_scope(read_only=True) as session:\n"
        "    session.execute('SELECT 1')\n",
        encoding="utf-8",
    )

    assert _uses_central_read_only_scope(command) is True


def test_plain_session_is_not_central_read_only_scope(tmp_path) -> None:
    command = tmp_path / "plain_session_command.py"
    command.write_text(
        "from sqlalchemy.orm import Session\n"
        "with Session() as session:\n"
        "    session.execute('SELECT 1')\n",
        encoding="utf-8",
    )

    assert _uses_central_read_only_scope(command) is False
