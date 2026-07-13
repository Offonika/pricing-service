#!/usr/bin/env python3
"""Validate effective metadata for pricing-service CLI and backfill commands."""

from __future__ import annotations

import fnmatch
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "docs/registry/cli-jobs.json"
TASKS_ROOT = REPO_ROOT / "tasks"
CRON_ROOT = REPO_ROOT / "infra/cron"
REQUIRED_FIELDS = {
    "owner",
    "kind",
    "dry_run",
    "idempotency",
    "side_effect_level",
    "spec",
    "remove_after",
}
ALLOWED_KINDS = {"permanent_cli", "maintenance", "report", "export", "backfill"}
ALLOWED_DRY_RUN = {"supported", "not_supported", "not_applicable", "not_verified"}
ALLOWED_SIDE_EFFECTS = {"read_only", "artifact_write", "db_write", "external_write", "mixed"}


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def task_files() -> list[Path]:
    return sorted(path for path in TASKS_ROOT.glob("*.py") if path.name != "__init__.py")


def effective_metadata(filename: str, registry: dict[str, Any]) -> dict[str, Any]:
    result = dict(registry.get("defaults") or {})
    for rule in registry.get("rules") or []:
        if fnmatch.fnmatch(filename, str(rule.get("glob") or "")):
            result.update({key: value for key, value in rule.items() if key != "glob"})
    result.update((registry.get("commands") or {}).get(filename) or {})
    return result


def cron_task_modules() -> set[str]:
    modules: set[str] = set()
    pattern = re.compile(r"-m\s+tasks\.([A-Za-z0-9_]+)")
    for path in CRON_ROOT.rglob("*"):
        if path.is_file() and path.suffix in {".sh", ".cron", ".py"}:
            modules.update(pattern.findall(path.read_text(encoding="utf-8", errors="replace")))
    return modules


def find_errors(registry: dict[str, Any] | None = None) -> list[str]:
    registry = registry or load_registry()
    errors: list[str] = []
    commands = registry.get("commands") or {}
    existing = {path.name for path in task_files()}

    for path in task_files():
        metadata = effective_metadata(path.name, registry)
        missing = sorted(REQUIRED_FIELDS - metadata.keys())
        if missing:
            errors.append(f"{path.name}: missing fields {', '.join(missing)}")
            continue
        if metadata["kind"] not in ALLOWED_KINDS:
            errors.append(f"{path.name}: unsupported kind {metadata['kind']!r}")
        if metadata["dry_run"] not in ALLOWED_DRY_RUN:
            errors.append(f"{path.name}: unsupported dry_run {metadata['dry_run']!r}")
        if metadata["side_effect_level"] not in ALLOWED_SIDE_EFFECTS:
            errors.append(
                f"{path.name}: unsupported side_effect_level {metadata['side_effect_level']!r}"
            )
        spec_path = REPO_ROOT / str(metadata["spec"])
        if not spec_path.is_file():
            errors.append(f"{path.name}: spec does not exist: {metadata['spec']}")
        if path.name.startswith("backfill_"):
            if path.name not in commands:
                errors.append(f"{path.name}: backfill requires an explicit registry entry")
            try:
                remove_after = date.fromisoformat(str(metadata["remove_after"]))
            except ValueError:
                errors.append(f"{path.name}: remove_after must be an ISO date")
            else:
                if remove_after <= date(2026, 7, 12):
                    errors.append(f"{path.name}: remove_after must be in the future")

    unknown_entries = sorted(set(commands) - existing)
    for filename in unknown_entries:
        errors.append(f"registry entry has no adapter: tasks/{filename}")

    for module in sorted(cron_task_modules()):
        filename = f"{module}.py"
        if filename not in existing:
            errors.append(f"cron references missing adapter: tasks/{filename}")
    return sorted(set(errors))


def main() -> int:
    errors = find_errors()
    if errors:
        print("CLI registry violations:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"CLI registry: OK ({len(task_files())} commands)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
