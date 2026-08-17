"""Verify the accepted display-name exclusion against an immutable inventory.

The task is local and read-only with respect to databases and external systems.
It never mutates the source inventory and writes a separate audit bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.services.display_scope_policy import (
    DISPLAY_SCOPE_POLICY_VERSION,
    EXCLUDED_DISPLAY_NAME_BITOK,
    display_scope_exclusion_reason,
    filter_display_scope_records,
)

DRY_RUN_SCHEMA = "display_scope_exclusion_dry_run.v1"
DEFAULT_SOURCE = Path(
    "reports/assortment_lifecycle/" "display-family-registry-preflight-v2-2026-08-16/inventory.json"
)
DEFAULT_OUTPUT_DIR = Path("reports/assortment_lifecycle/display-scope-bitok-dry-run-2026-08-16")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_display_scope_exclusion_dry_run(
    source_payload: Mapping[str, Any],
    *,
    expected_source_count: int,
    expected_excluded_count: int,
    expected_included_count: int,
) -> dict[str, Any]:
    raw_items = source_payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("display_scope_source_items_must_be_a_list")
    if not all(isinstance(row, Mapping) for row in raw_items):
        raise ValueError("display_scope_source_item_must_be_an_object")

    scope_result = filter_display_scope_records(raw_items)
    audit = scope_result.audit
    included_items = [dict(row) for row in scope_result.included]
    leaked_codes = sorted(
        str(row.get("nomenclature_code") or "")
        for row in included_items
        if display_scope_exclusion_reason(row.get("name"))
    )
    exclusion_keys = [
        (
            row["nomenclature_code"],
            row["name"],
            row["reason_code"],
            row["scope_policy_version"],
        )
        for row in audit["exclusions"]
    ]
    checks = {
        "source_count_matches": len(raw_items) == expected_source_count,
        "excluded_count_matches": audit["excluded_item_count"] == expected_excluded_count,
        "included_count_matches": len(included_items) == expected_included_count,
        "included_cohort_has_no_bitok": not leaked_codes,
        "exclusion_registry_is_unique": len(exclusion_keys) == len(set(exclusion_keys)),
        "reason_count_matches": audit["excluded_reason_counts"]
        == {EXCLUDED_DISPLAY_NAME_BITOK: expected_excluded_count},
    }
    accepted = all(checks.values())
    summary = {
        "schema": DRY_RUN_SCHEMA,
        "status": "accepted" if accepted else "failed",
        "production_action": "none_read_only",
        "source_schema": source_payload.get("schema"),
        "scope_policy_version": DISPLAY_SCOPE_POLICY_VERSION,
        "source_item_count": len(raw_items),
        "excluded_item_count": audit["excluded_item_count"],
        "included_item_count": len(included_items),
        "excluded_reason_counts": audit["excluded_reason_counts"],
        "expected": {
            "source_item_count": expected_source_count,
            "excluded_item_count": expected_excluded_count,
            "included_item_count": expected_included_count,
        },
        "checks": checks,
        "leaked_included_codes": leaked_codes,
    }
    return {
        "summary": summary,
        "included_items": included_items,
        "exclusions": audit["exclusions"],
    }


def write_display_scope_exclusion_dry_run(
    output_dir: Path,
    *,
    result: Mapping[str, Any],
    source_path: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "summary.json"
    included_path = output_dir / "included-items.json"
    exclusions_path = output_dir / "exclusions.json"
    _atomic_write_json(summary_path, result["summary"])
    _atomic_write_json(
        included_path,
        {
            "schema": "display_scope_included_items.v1",
            "scope_policy_version": DISPLAY_SCOPE_POLICY_VERSION,
            "items": result["included_items"],
        },
    )
    _atomic_write_json(
        exclusions_path,
        {
            "schema": "display_scope_exclusions.v1",
            "scope_policy_version": DISPLAY_SCOPE_POLICY_VERSION,
            "items": result["exclusions"],
        },
    )
    manifest = {
        **dict(result["summary"]),
        "source_path": str(source_path),
        "source_sha256": _sha256(source_path),
        "artifacts": {
            "summary.json": _sha256(summary_path),
            "included-items.json": _sha256(included_path),
            "exclusions.json": _sha256(exclusions_path),
        },
    }
    _atomic_write_json(output_dir / "manifest.json", manifest)
    return manifest


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--expected-source-count", type=int, default=2689)
    parser.add_argument("--expected-excluded-count", type=int, default=11)
    parser.add_argument("--expected-included-count", type=int, default=2678)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    source_payload = json.loads(args.source.read_text(encoding="utf-8-sig"))
    result = build_display_scope_exclusion_dry_run(
        source_payload,
        expected_source_count=args.expected_source_count,
        expected_excluded_count=args.expected_excluded_count,
        expected_included_count=args.expected_included_count,
    )
    manifest = write_display_scope_exclusion_dry_run(
        args.output_dir,
        result=result,
        source_path=args.source,
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0 if manifest["status"] == "accepted" else 2


if __name__ == "__main__":
    raise SystemExit(main())
