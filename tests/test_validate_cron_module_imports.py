from __future__ import annotations

import os
import subprocess
from pathlib import Path

from scripts import validate_cron_module_imports


def test_cron_module_import_validator_finds_https_import_task():
    assert "tasks.import_competitor_http" in validate_cron_module_imports.cron_task_modules()


def test_cron_module_import_validator_imports_current_cron_modules():
    assert validate_cron_module_imports.find_import_errors() == []


def test_site_service_worker_cron_rejects_invalid_start_delay(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    script = project_root / "infra/cron/site_service_requests_worker.sh"
    syntax = subprocess.run(
        ["bash", "-n", str(script)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr

    environment = {
        **os.environ,
        "REPO_DIR": str(project_root),
        "LOG_DIR": str(tmp_path / "logs"),
        "LOCK_FILE": str(tmp_path / "worker.lock"),
        "SITE_SERVICE_REQUESTS_WORKER_START_DELAY_SECONDS": "56",
    }
    result = subprocess.run(
        ["bash", str(script)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 2
    assert "invalid start delay" in (tmp_path / "logs/site_service_requests_worker.log").read_text()
