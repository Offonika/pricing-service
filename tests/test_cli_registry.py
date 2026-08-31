from __future__ import annotations

from scripts.validate_cli_registry import (
    _uses_central_read_only_scope,
    effective_metadata,
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


def test_manual_matching_commands_require_central_read_only_db_access() -> None:
    registry = load_registry()

    for filename in ("manual_matching_control.py", "manual_matching_bitrix_tasks.py"):
        metadata = effective_metadata(filename, registry)
        assert metadata["db_access"] == "application_read_only"


def test_compare_employee_receivable_report_declares_onec_read_only_access() -> None:
    metadata = effective_metadata("compare_employee_receivable_report.py", load_registry())

    assert metadata["kind"] == "report"
    assert metadata["dry_run"] == "not_applicable"
    assert metadata["idempotency"] == "read_only"
    assert metadata["side_effect_level"] == "read_only"
    assert metadata["db_access"] == "onec_read_only"
