#!/usr/bin/env python3
"""Validate the pricing-service docs manifest and spec lifecycle metadata."""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "docs/manifest.yml"
SPECS_ROOT = REPO_ROOT / "docs/specs"
SPEC_INDEX_PATH = SPECS_ROOT / "README.md"

REQUIRED_DOCUMENT_FIELDS = {
    "path": str,
    "title": str,
    "doc_type": str,
    "domain": str,
    "status": str,
    "owner": str,
    "source_of_truth": bool,
    "related_code": list,
    "related_tests": list,
    "keywords": list,
}
REQUIRED_SPEC_FIELDS = {
    "spec_id": str,
    "title": str,
    "doc_type": str,
    "domain": str,
    "status": str,
    "owner": str,
    "source_of_truth": bool,
    "related_code": list,
    "related_tests": list,
    "contracts": list,
    "depends_on": list,
    "supersedes": list,
    "rollout_required": bool,
    "updated_at": str,
}
SPEC_PATH_FIELDS = ("related_code", "related_tests", "contracts", "depends_on", "supersedes")
ALLOWED_SPEC_STATUSES = {"draft", "review", "accepted", "implemented", "superseded"}
STRICT_SPEC_STATUSES = {"accepted", "implemented"}
REQUIRED_SPEC_SECTIONS = ("Source of Truth", "API / Data Contracts", "Tests", "Rollout")
QUALITY_SPEC_SECTIONS = (
    "Change Summary / Spec Delta",
    "Acceptance Criteria",
    "Implementation Checklist",
)
QUALITY_ENFORCEMENT_START = date(2026, 6, 3)
SPEC_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


class DocsError(Exception):
    """Raised when project documentation violates the navigation contract."""


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DocsError(f"{path.relative_to(REPO_ROOT)}: invalid YAML: {exc}") from exc
    if not isinstance(value, dict):
        raise DocsError(f"{path.relative_to(REPO_ROOT)}: root must be a mapping")
    return value


def validate_non_empty_string(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DocsError(f"{label}: must be a non-empty string")


def validate_string_list(value: Any, label: str) -> None:
    if not isinstance(value, list):
        raise DocsError(f"{label}: must be a list")
    for index, item in enumerate(value):
        validate_non_empty_string(item, f"{label}[{index}]")
    if len(value) != len(set(value)):
        raise DocsError(f"{label}: duplicate values are not allowed")


def validate_fields(value: dict[str, Any], required: dict[str, type], label: str) -> None:
    missing = sorted(set(required) - set(value))
    if missing:
        raise DocsError(f"{label}: missing fields: {', '.join(missing)}")
    for field, expected_type in required.items():
        field_value = value[field]
        field_label = f"{label}.{field}"
        if expected_type is str:
            validate_non_empty_string(field_value, field_label)
        elif expected_type is bool:
            if not isinstance(field_value, bool):
                raise DocsError(f"{field_label}: must be a boolean")
        elif expected_type is list:
            validate_string_list(field_value, field_label)


def project_path(value: str, label: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise DocsError(f"{label}: must be project-relative: {value}")
    target = REPO_ROOT / relative
    if not target.exists():
        raise DocsError(f"{label}: target does not exist: {value}")
    return target


def validate_manifest() -> set[str]:
    manifest = load_yaml(MANIFEST_PATH)
    if manifest.get("version") != 1:
        raise DocsError("docs/manifest.yml: version must be 1")
    if manifest.get("project") != "pricing-service":
        raise DocsError("docs/manifest.yml: project must be pricing-service")

    documents = manifest.get("documents")
    if not isinstance(documents, list) or not documents:
        raise DocsError("docs/manifest.yml: documents must be a non-empty list")

    paths: set[str] = set()
    for index, document in enumerate(documents):
        label = f"docs/manifest.yml: documents[{index}]"
        if not isinstance(document, dict):
            raise DocsError(f"{label}: must be a mapping")
        validate_fields(document, REQUIRED_DOCUMENT_FIELDS, label)
        relative_path = document["path"]
        if relative_path in paths:
            raise DocsError(f"{label}.path: duplicate path: {relative_path}")
        project_path(relative_path, f"{label}.path")
        paths.add(relative_path)
    return paths


def parse_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    label = str(path.relative_to(REPO_ROOT))
    if not match:
        raise DocsError(f"{label}: missing YAML frontmatter")
    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise DocsError(f"{label}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise DocsError(f"{label}: frontmatter must be a mapping")
    return frontmatter, match.group(2)


def section_bodies(markdown: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in markdown.splitlines():
        if line.startswith("# "):
            current = line[2:].strip()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return {title: "\n".join(lines).strip() for title, lines in sections.items()}


def validate_spec(path: Path, manifest_paths: set[str]) -> None:
    label = str(path.relative_to(REPO_ROOT))
    frontmatter, markdown = parse_frontmatter(path)
    validate_fields(frontmatter, REQUIRED_SPEC_FIELDS, label)

    if frontmatter["doc_type"] != "spec":
        raise DocsError(f"{label}: doc_type must be 'spec'")
    if frontmatter["status"] not in ALLOWED_SPEC_STATUSES:
        raise DocsError(f"{label}: unsupported status: {frontmatter['status']}")
    if not SPEC_ID_RE.fullmatch(frontmatter["spec_id"]):
        raise DocsError(f"{label}: spec_id must use lower-kebab-case")
    try:
        updated_at = date.fromisoformat(frontmatter["updated_at"])
    except ValueError as exc:
        raise DocsError(f"{label}: updated_at must use YYYY-MM-DD") from exc

    if label not in manifest_paths:
        raise DocsError(f"{label}: spec must be listed in docs/manifest.yml")
    for field in SPEC_PATH_FIELDS:
        for value in frontmatter[field]:
            project_path(value, f"{label}: {field}")

    if frontmatter["status"] not in STRICT_SPEC_STATUSES:
        return
    sections = section_bodies(markdown)
    required_sections = list(REQUIRED_SPEC_SECTIONS)
    if updated_at >= QUALITY_ENFORCEMENT_START:
        required_sections.extend(QUALITY_SPEC_SECTIONS)
    missing_sections = [name for name in required_sections if not sections.get(name)]
    if missing_sections:
        raise DocsError(f"{label}: missing sections: {', '.join(missing_sections)}")


def main() -> int:
    if not SPEC_INDEX_PATH.is_file():
        raise DocsError("docs/specs/README.md: spec index does not exist")
    manifest_paths = validate_manifest()
    specs = sorted(path for path in SPECS_ROOT.glob("*.md") if path.name.lower() != "readme.md")
    for path in specs:
        validate_spec(path, manifest_paths)
    print(f"validated pricing-service docs ({len(manifest_paths)} documents, {len(specs)} specs)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DocsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
