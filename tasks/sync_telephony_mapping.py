from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.telephony import (
    build_retail_line_map_projection,
    load_telephony_user_line_snapshot,
)
from app.workers.telephony import run_telephony_user_line_sync


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _export_csv(
    *,
    path: Path,
    rows: list[dict[str, object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if fieldnames:
            writer.writeheader()
            writer.writerows(rows)


def _snapshot_model_to_dict(item) -> dict[str, object]:
    return {
        "snapshot_date": item.snapshot_date,
        "mapping_source": item.mapping_source,
        "user_ref_hex": item.user_ref_hex,
        "user_name": item.user_name,
        "physical_person_ref_hex": item.physical_person_ref_hex,
        "physical_person_name": item.physical_person_name,
        "computer_name": item.computer_name,
        "extension": item.extension,
        "store_ref_hex": item.store_ref_hex,
        "store_code": item.store_code,
        "store_name": item.store_name,
        "department_ref_hex": item.department_ref_hex,
        "department_code": item.department_code,
        "department_name": item.department_name,
        "employment_status": item.employment_status,
        "staff_store_ref": item.staff_store_ref,
        "staff_store_name": item.staff_store_name,
        "staff_department_ref": item.staff_department_ref,
        "staff_department_name": item.staff_department_name,
        "bitrix_user_id": item.bitrix_user_id,
        "bitrix_full_name": item.bitrix_full_name,
        "mdm_employee_code": item.mdm_employee_code,
        "bitrix_status": item.bitrix_status,
        "is_marked": item.is_marked,
        "has_extension": item.has_extension,
        "has_bitrix": item.has_bitrix,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync telephony user/computer/extension mapping from 1C into pricing DB"
    )
    parser.add_argument("--snapshot-date", help="Snapshot date in YYYY-MM-DD format")
    parser.add_argument(
        "--export-dir",
        help="Optional directory for user-line and retail-line CSV exports",
    )
    args = parser.parse_args()

    effective_snapshot_date = _parse_date(args.snapshot_date)
    result = run_telephony_user_line_sync(snapshot_date=effective_snapshot_date)

    if args.export_dir:
        settings = get_settings()
        engine = create_engine(settings.database_url)
        snapshot_date = date.fromisoformat(str(result["snapshot_date"]))
        try:
            with Session(engine) as session:
                _, items = load_telephony_user_line_snapshot(
                    session,
                    snapshot_date=snapshot_date,
                )
                service_line_labels = settings.telephony_service_line_labels
                review_line_ids = settings.telephony_review_line_ids
                retail_projection = build_retail_line_map_projection(
                    [item for item in items if item.extension],
                    service_line_labels=service_line_labels,
                    exclude_line_ids=review_line_ids,
                )
        finally:
            engine.dispose()

        export_dir = Path(args.export_dir)
        user_csv = export_dir / f"telephony-user-line-map-{snapshot_date.isoformat()}.csv"
        retail_csv = export_dir / f"telephony-retail-line-map-{snapshot_date.isoformat()}.csv"
        _export_csv(path=user_csv, rows=[_snapshot_model_to_dict(item) for item in items])
        _export_csv(path=retail_csv, rows=[asdict(item) for item in retail_projection])
        result["user_csv"] = str(user_csv)
        result["retail_csv"] = str(retail_csv)

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
