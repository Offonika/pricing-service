from __future__ import annotations

from scripts import validate_cron_module_imports


def test_cron_module_import_validator_finds_https_import_task():
    assert "tasks.import_competitor_http" in validate_cron_module_imports.cron_task_modules()


def test_cron_module_import_validator_imports_current_cron_modules():
    assert validate_cron_module_imports.find_import_errors() == []
