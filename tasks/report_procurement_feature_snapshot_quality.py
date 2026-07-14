from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import select

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE,
)

DEFAULT_OUTPUT_CSV = (
    Path("reports/assortment_lifecycle")
    / date.today().isoformat()
    / ("procurement-feature-snapshot-quality.csv")
)

CSV_COLUMNS = [
    "nomenclature_code",
    "name",
    "folder",
    "status",
    "status_label",
    "future_ka_mapping_status",
    "data_quality_score",
    "missing_required_attributes",
    "subject_1c",
    "quality_raw",
    "quality_normalized",
    "brand_compatibility",
    "model_compatibility",
    "price_segment",
    "calculation_unit_level",
    "calculation_unit_key",
    "calculation_unit_source",
    "calculation_unit_confidence",
    "demand_method_code",
    "demand_method_confidence",
    "manual_review_required",
    "auto_order_allowed",
    "blockers",
]
FEATURE_SNAPSHOT_SCHEMA = "procurement_feature_snapshot.v1"


def main() -> int:
    args = _parse_args()
    settings = get_settings()
    database_url = args.database_url or os.environ.get("DATABASE_URL") or settings.database_url
    engine = build_engine(database_url, pool_pre_ping=True)
    try:
        rows = load_feature_snapshot_rows(
            engine,
            folder=args.folder,
            only_missing=args.only_missing,
            limit=args.limit,
        )
    finally:
        engine.dispose()

    summary = build_summary(rows)
    output_csv = args.output_csv
    if output_csv:
        write_csv(output_csv, rows)
    payload = {
        "status": "ready",
        "items": len(rows),
        "summary": summary,
        "output_csv": str(output_csv) if output_csv else None,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def load_feature_snapshot_rows(
    engine,
    *,
    folder: str = "",
    only_missing: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    table = ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE
    query = (
        select(table)
        .order_by(
            table.c.future_ka_mapping_status.asc(),
            table.c.data_quality_score.asc(),
            table.c.nomenclature_code.asc(),
        )
        .where(table.c.feature_snapshot_schema == FEATURE_SNAPSHOT_SCHEMA)
    )
    if folder:
        query = query.where(table.c.folder.ilike(f"%{folder}%"))
    if only_missing:
        query = query.where(table.c.future_ka_mapping_status != "ready")
    if limit:
        query = query.limit(limit)
    with engine.connect() as conn:
        return [normalize_row(row) for row in conn.execute(query).mappings()]


def build_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    missing_counter: Counter[str] = Counter()
    unit_counter: Counter[str] = Counter()
    demand_counter: Counter[str] = Counter()
    status_counter: Counter[str] = Counter()
    for row in rows:
        status_counter[str(row.get("future_ka_mapping_status") or "unknown")] += 1
        unit_counter[str(row.get("calculation_unit_level") or "unknown")] += 1
        demand_counter[str(row.get("demand_method_code") or "unknown")] += 1
        for field_name in _json_list(row.get("missing_required_attributes")):
            missing_counter[str(field_name)] += 1
    return {
        "future_ka_mapping_status": dict(sorted(status_counter.items())),
        "missing_required_attributes": dict(sorted(missing_counter.items())),
        "calculation_unit_level": dict(sorted(unit_counter.items())),
        "demand_method_code": dict(sorted(demand_counter.items())),
        "auto_order_allowed": sum(1 for row in rows if row.get("auto_order_allowed")),
        "manual_review_required": sum(1 for row in rows if row.get("manual_review_required")),
    }


def normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for field_name in (
        "missing_required_attributes",
        "blockers",
        "item_tags",
        "characteristic_values",
    ):
        result[field_name] = _json_value(result.get(field_name))
    return result


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _csv_value(row.get(column)) for column in CSV_COLUMNS})
    return path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report procurement_feature_snapshot.v1 quality for assortment classification."
    )
    parser.add_argument("--database-url", default="")
    parser.add_argument("--folder", default="дисплеи")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Export only rows where required procurement feature attributes are missing.",
    )
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be positive")
    return args


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _csv_value(value: Any) -> str:
    value = _json_value(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None:
        return ""
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
