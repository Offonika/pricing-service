#!/usr/bin/env python3
"""Validate effective metadata for pricing-service CLI and backfill commands."""

from __future__ import annotations

import ast
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
ALLOWED_DB_ACCESS = {
    "none",
    "application_read_only",
    "application_write",
    "onec_read_only",
    "mixed",
}
ALLOWED_TRANSACTION_SCOPES = {"unit_of_work"}


def _uses_central_read_only_scope(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports_scope = any(
        isinstance(node, ast.ImportFrom)
        and node.module in {"app.infrastructure.db", "app.infrastructure.db.session"}
        and any(alias.name == "session_scope" for alias in node.names)
        for node in ast.walk(tree)
    )
    calls_read_only_scope = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "session_scope"
        and any(
            keyword.arg == "read_only"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        )
        for node in ast.walk(tree)
    )
    return imports_scope and calls_read_only_scope


def _uses_application_unit_of_work(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports_unit_of_work = any(
        isinstance(node, ast.ImportFrom)
        and node.module in {"app.infrastructure.db", "app.infrastructure.db.unit_of_work"}
        and any(alias.name == "SqlAlchemyUnitOfWork" for alias in node.names)
        for node in ast.walk(tree)
    )
    calls_unit_of_work = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SqlAlchemyUnitOfWork"
        for node in ast.walk(tree)
    )
    return imports_unit_of_work and calls_unit_of_work


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
        db_access = metadata.get("db_access")
        if db_access is not None and db_access not in ALLOWED_DB_ACCESS:
            errors.append(f"{path.name}: unsupported db_access {db_access!r}")
        transaction_scope = metadata.get("transaction_scope")
        if transaction_scope is not None and transaction_scope not in ALLOWED_TRANSACTION_SCOPES:
            errors.append(f"{path.name}: unsupported transaction_scope {transaction_scope!r}")
        if db_access == "application_read_only":
            source = path.read_text(encoding="utf-8")
            if not _uses_central_read_only_scope(path):
                errors.append(
                    f"{path.name}: application_read_only requires session_scope(read_only=True)"
                )
            if "build_engine" in source or "get_application_engine(" in source:
                errors.append(
                    f"{path.name}: application_read_only must not construct/access an engine"
                )
        if transaction_scope == "unit_of_work":
            source = path.read_text(encoding="utf-8")
            if db_access != "application_write":
                errors.append(f"{path.name}: unit_of_work requires db_access='application_write'")
            if not _uses_application_unit_of_work(path):
                errors.append(f"{path.name}: unit_of_work requires SqlAlchemyUnitOfWork")
            if "build_engine" in source or "get_application_engine(" in source:
                errors.append(f"{path.name}: unit_of_work must not construct/access an engine")
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
