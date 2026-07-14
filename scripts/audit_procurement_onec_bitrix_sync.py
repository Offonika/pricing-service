#!/usr/bin/env python3
"""Audit latest 1C supplier-order sync artifacts against procurement-stage rules."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULT_PATHS = [
    REPO_ROOT / "build/bitrix/onec_open_procurement_supplier_orders_result.json",
    REPO_ROOT / "build/bitrix/onec_blank_contour_cargo_dropoff_orders_result.json",
]
DEFAULT_OUTPUT_PATH = REPO_ROOT / "build/bitrix/procurement_onec_bitrix_audit.json"
FORBIDDEN_STAGE_KEYS = {"need"}


def clean_string(value: Any) -> str:
    return str(value or "").strip()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def source_number(row: dict[str, Any]) -> str:
    return clean_string(
        row.get("source_number") or row.get("number") or row.get("onec_source_number")
    )


def input_orders(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = load_json(path)
    if isinstance(payload, dict) and isinstance(payload.get("orders"), list):
        return [row for row in payload["orders"] if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def has_onec_source_number_field(row: dict[str, Any]) -> bool:
    fields = row.get("field_names") or []
    if not isinstance(fields, list):
        return False
    return any("ONECSOURCENUMBER" in clean_string(field).upper() for field in fields)


def audit_result_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "path": str(path),
            "status": "missing",
            "rows": 0,
            "violations": [{"type": "missing_result_file", "path": str(path)}],
        }

    payload = load_json(path)
    rows = payload.get("rows") if isinstance(payload, dict) else []
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    input_path_value = clean_string(payload.get("input_json")) if isinstance(payload, dict) else ""
    input_path = Path(input_path_value) if input_path_value else None
    input_rows = input_orders(input_path) if input_path else []

    violations: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        number = source_number(row)
        if not number:
            violations.append({"type": "missing_source_number", "row": index})
        for key in ("initial_stage_key", "stage_key"):
            stage_key = clean_string(row.get(key))
            if stage_key in FORBIDDEN_STAGE_KEYS:
                violations.append(
                    {
                        "type": "forbidden_stage",
                        "row": index,
                        "source_number": number,
                        "field": key,
                        "stage_key": stage_key,
                    }
                )
        if rows and not has_onec_source_number_field(row):
            violations.append(
                {
                    "type": "missing_onec_source_number_field",
                    "row": index,
                    "source_number": number,
                }
            )

    input_numbers = {source_number(row) for row in input_rows if source_number(row)}
    result_numbers = {source_number(row) for row in rows if source_number(row)}
    for number in sorted(input_numbers - result_numbers):
        violations.append({"type": "input_order_missing_result", "source_number": number})
    for number in sorted(result_numbers - input_numbers):
        if input_rows:
            violations.append({"type": "result_without_input_order", "source_number": number})

    contours = Counter(clean_string(row.get("contour")) or "unknown" for row in rows)
    stages = Counter(clean_string(row.get("stage_key")) or "unknown" for row in rows)
    actions = Counter(clean_string(row.get("action")) or "unknown" for row in rows)
    blocked = sum(1 for row in rows if bool(row.get("blocked_supplier")))

    return {
        "path": str(path),
        "status": "ok" if not violations else "attention",
        "mode": payload.get("mode") if isinstance(payload, dict) else None,
        "input_json": str(input_path) if input_path else "",
        "rows": len(rows),
        "input_rows": len(input_rows),
        "contours": dict(contours),
        "stages": dict(stages),
        "bitrix_actions": dict(actions),
        "blocked_supplier": blocked,
        "violations": violations,
    }


def build_audit(paths: list[Path]) -> dict[str, Any]:
    files = [audit_result_file(path) for path in paths]
    all_violations = [
        {"file": file_result["path"], **violation}
        for file_result in files
        for violation in file_result.get("violations", [])
    ]
    totals = {
        "rows": sum(int(item.get("rows") or 0) for item in files),
        "input_rows": sum(int(item.get("input_rows") or 0) for item in files),
        "violations": len(all_violations),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "ok" if not all_violations else "attention",
        "acceptance": {
            "all_rows_have_1c_source_number": not any(
                item["type"] == "missing_source_number" for item in all_violations
            ),
            "no_1c_orders_in_need_stage": not any(
                item["type"] == "forbidden_stage" for item in all_violations
            ),
            "every_input_order_has_result": not any(
                item["type"] == "input_order_missing_result" for item in all_violations
            ),
        },
        "totals": totals,
        "files": files,
        "violations": all_violations,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--result-json",
        type=Path,
        action="append",
        dest="result_jsons",
        help="Sync result JSON to audit. Can be passed multiple times.",
    )
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--strict", action="store_true", help="Exit with status 1 on violations.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = args.result_jsons or DEFAULT_RESULT_PATHS
    audit = build_audit(paths)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 1 if args.strict and audit["status"] != "ok" else 0


if __name__ == "__main__":
    raise SystemExit(main())
