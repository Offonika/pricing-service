"""Import every Python task referenced by cron before a release switch."""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CRON_ROOT = REPO_ROOT / "infra" / "cron"
TASK_PATTERN = re.compile(r"-m\s+(tasks\.[A-Za-z0-9_]+)")


def cron_task_modules() -> list[str]:
    modules: set[str] = set()
    for path in CRON_ROOT.rglob("*"):
        if path.is_file() and path.suffix in {".sh", ".cron", ".py"}:
            modules.update(TASK_PATTERN.findall(path.read_text(encoding="utf-8", errors="replace")))
    return sorted(modules)


def find_import_errors() -> list[str]:
    errors: list[str] = []
    for module_name in cron_task_modules():
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - collect every release import failure
            errors.append(f"{module_name}: {type(exc).__name__}: {exc}")
    return errors


def main() -> int:
    errors = find_import_errors()
    if errors:
        print("Cron module import violations:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Cron module imports: OK ({len(cron_task_modules())} modules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
