#!/usr/bin/env python3
"""Fail CI when hardening boundaries regress."""

from __future__ import annotations

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

    markdown_tasks = sorted((root / "tasks").glob("*.md"))
    for path in markdown_tasks:
        violations.append(f"Markdown task must live in docs: {path.relative_to(root)}")

    domain_root = root / "app/domains"
    existing_domains = {path.name for path in domain_root.iterdir() if path.is_dir()}
    for name in sorted(DOMAIN_NAMES - existing_domains):
        violations.append(f"missing domain package: app/domains/{name}")
    return sorted(set(violations))


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
