#!/usr/bin/env python3
"""Fail CI when hardening boundaries regress."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOMAIN_NAMES = {
    "catalog",
    "matching",
    "pricing",
    "assortment",
    "procurement",
    "receivables",
    "management",
    "expertise",
    "logistics",
    "telephony",
}
TEXT_SUFFIXES = {".py", ".md", ".sh", ".yml", ".yaml", ".toml", ".example"}
LEGACY_BOUNDARY_ALLOWLIST = {
    Path("app/domains/management/application/weekly_kpi_ingest.py"),
}


def _text_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for relative_root in ("app", "tasks", "infra", "scripts", "tests", "docs"):
        for path in (root / relative_root).rglob("*"):
            if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
                files.append(path)
    files.append(root / ".env.example")
    return files


def find_violations(root: Path = REPO_ROOT) -> list[str]:
    violations: list[str] = []
    for path in _text_files(root):
        if not path.exists():
            continue
        relative = path.relative_to(root)
        if relative == Path("scripts/check_architecture_boundaries.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        lowered = text.lower()

        if "topcontrol" in lowered:
            is_legacy = relative.parts[:2] == ("docs", "legacy")
            is_changelog = relative.name.lower().startswith(("changelog", "release_notes"))
            if not is_legacy and not is_changelog:
                violations.append(f"deprecated TopControl reference: {relative}")

        foreign_patterns = (
            "/opt/MM/mm-compensation/.env",
            "/opt/MM/mastermobile/.env",
            "../mm-compensation/build",
            "../mastermobile/build",
        )
        for pattern in foreign_patterns:
            if pattern in text:
                violations.append(f"foreign project runtime path {pattern}: {relative}")

        if (
            relative.suffix == ".py"
            and relative.parts
            and relative.parts[0] in {"app", "tasks", "scripts", "infra"}
            and "create_engine" in text
            and relative != Path("app/infrastructure/db/engines.py")
        ):
            violations.append(f"create_engine outside DB factory: {relative}")

        if relative.parts[:2] == ("app", "domains") and path.suffix == ".py":
            violations.extend(_domain_import_violations(relative, text))

    markdown_tasks = sorted((root / "tasks").glob("*.md"))
    for path in markdown_tasks:
        violations.append(f"Markdown task must live in docs: {path.relative_to(root)}")

    domain_root = root / "app/domains"
    existing_domains = {path.name for path in domain_root.iterdir() if path.is_dir()}
    for name in sorted(DOMAIN_NAMES - existing_domains):
        violations.append(f"missing domain package: app/domains/{name}")
    return sorted(set(violations))


def _domain_import_violations(relative: Path, text: str) -> list[str]:
    if relative in LEGACY_BOUNDARY_ALLOWLIST:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [f"cannot parse domain module: {relative}"]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    violations: list[str] = []
    forbidden_roots = ("fastapi", "sqlalchemy", "app.api", "app.infrastructure", "app.models")
    for name in sorted(imported):
        if name in forbidden_roots or name.startswith(
            tuple(f"{root}." for root in forbidden_roots)
        ):
            violations.append(f"domain imports framework/infrastructure {name}: {relative}")

        parts = name.split(".")
        if len(parts) >= 3 and parts[:2] == ["app", "domains"]:
            current_domain = relative.parts[2] if len(relative.parts) > 2 else ""
            target_domain = parts[2]
            allowed_surface = len(parts) >= 4 and parts[3] in {"contracts", "public"}
            if target_domain != current_domain and not allowed_surface:
                violations.append(f"cross-domain private import {name}: {relative}")
    return violations


def main() -> int:
    violations = find_violations()
    if violations:
        print("Architecture boundary violations:")
        for violation in violations:
            print(f"- {violation}")
        return 1
    print("Architecture boundaries: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
