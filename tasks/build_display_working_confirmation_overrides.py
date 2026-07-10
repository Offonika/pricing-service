from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import create_engine, func, select

from app.core.config import get_settings
from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE,
)

DEFAULT_BASE_OVERRIDES_JSON = Path("config/assortment/display-manual-overrides.json")
DEFAULT_OUTPUT_JSON = (
    Path("build/assortment")
    / date.today().isoformat()
    / "display-manual-overrides-working-confirmed.json"
)
WORKING_CONFIRMATION_RULE = "working_confirmation_required"


def main() -> int:
    args = _parse_args()
    settings = get_settings()
    database_url = args.database_url or os.environ.get("DATABASE_URL") or settings.database_url
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        candidates, last_run_id = load_working_confirmation_candidates(
            engine,
            folder=args.folder,
            include_expensive=args.include_expensive,
        )
    finally:
        engine.dispose()

    payload = build_override_payload(
        candidates,
        base_overrides=_load_optional_json(args.base_overrides_json),
        source_run_id=last_run_id,
        folder=args.folder,
        approved_by=args.approved_by,
        changed_at=args.changed_at,
    )
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    result = {
        "status": "ready",
        "source_run_id": last_run_id,
        "working_confirmation_candidates": len(candidates),
        "base_override_rows": len(_items(_load_optional_json(args.base_overrides_json))),
        "output_items": len(payload["items"]),
        "output_json": str(args.output_json) if args.output_json else None,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def load_working_confirmation_candidates(
    engine,
    *,
    folder: str,
    include_expensive: bool,
) -> tuple[list[dict[str, Any]], int | None]:
    table = ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE
    with engine.connect() as conn:
        last_run_id = conn.execute(
            select(func.max(table.c.last_run_id)).where(table.c.folder.ilike(f"%{folder}%"))
        ).scalar()
        rows = (
            conn.execute(
                select(table)
                .where(
                    table.c.folder.ilike(f"%{folder}%"),
                    table.c.last_run_id == last_run_id,
                    table.c.status == "sale",
                    table.c.future_ka_mapping_status == "ready",
                    table.c.demand_method_code == "available_days_average",
                    table.c.manual_review_required.is_(True),
                )
                .order_by(table.c.nomenclature_code.asc())
            )
            .mappings()
            .all()
        )
    candidates = []
    for row in rows:
        blockers = _json_list(row.get("blockers")) + _json_list(row.get("export_blockers"))
        if WORKING_CONFIRMATION_RULE not in blockers:
            continue
        if not include_expensive and _clean(row.get("expensive_profile")):
            continue
        if not _clean(row.get("quality_raw")):
            continue
        candidates.append(dict(row))
    return candidates, last_run_id


def build_override_payload(
    candidates: Sequence[Mapping[str, Any]],
    *,
    base_overrides: Mapping[str, Any] | Sequence[Any] | None,
    source_run_id: int | None,
    folder: str,
    approved_by: str,
    changed_at: date,
) -> dict[str, Any]:
    base_items = _items(base_overrides)
    items_by_code: dict[str, dict[str, Any]] = {}
    for item in base_items:
        code = _code(item)
        if code:
            items_by_code[code] = dict(item)

    for row in candidates:
        code = _clean(row.get("nomenclature_code"))
        if not code:
            continue
        items_by_code[code] = {
            "nomenclature_code": code,
            "working_confirmed_by_folder_responsible": True,
            "manual_reason": (
                "Подтверждено как рабочий товар: "
                f"{row.get('reason_text') or 'есть достаточная история поступлений.'}"
            ),
            "manual_approved_by": approved_by,
            "manual_changed_at": changed_at.isoformat(),
            "approval_source": "display_working_confirmation_batch_v1",
            "approval_rule": WORKING_CONFIRMATION_RULE,
            "approval_rule_ru": "нужно подтвердить, что товар стал рабочим",
            "source_run_id": source_run_id,
        }

    return {
        "_description": (
            "Merged manual overrides for display pilot. Existing manual stops are preserved; "
            "working_confirmation_required rows are approved as Рабочий for dry-run/pilot review."
        ),
        "_generated_at": datetime.now().isoformat(timespec="seconds"),
        "_folder": folder,
        "_source_run_id": source_run_id,
        "_approval_rule": WORKING_CONFIRMATION_RULE,
        "_approval_rule_ru": "нужно подтвердить, что товар стал рабочим",
        "_approved_by": approved_by,
        "_changed_at": changed_at.isoformat(),
        "items": sorted(items_by_code.values(), key=lambda item: _code(item)),
    }


def _items(payload: Mapping[str, Any] | Sequence[Any] | None) -> list[dict[str, Any]]:
    if not payload:
        return []
    raw_items: Any
    if isinstance(payload, Mapping):
        raw_items = payload.get("items")
        if raw_items is None:
            raw_items = [
                {"nomenclature_code": code, **value}
                for code, value in payload.items()
                if isinstance(value, Mapping)
            ]
    else:
        raw_items = payload
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        raise SystemExit("manual overrides must be a list or an object with items")
    if not all(isinstance(item, Mapping) for item in raw_items):
        raise SystemExit("manual override items must be objects")
    return [dict(item) for item in raw_items]


def _load_optional_json(path: Path | None) -> Any:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return [value]
        return parsed if isinstance(parsed, list) else [parsed]
    return [value]


def _code(item: Mapping[str, Any]) -> str:
    return _clean(item.get("nomenclature_code") or item.get("NomenclatureCode"))


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build merged display manual overrides with Рабочий confirmations."
    )
    parser.add_argument("--database-url", default="")
    parser.add_argument("--folder", default="дисплеи")
    parser.add_argument("--base-overrides-json", type=Path, default=DEFAULT_BASE_OVERRIDES_JSON)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_JSON)
    parser.add_argument("--approved-by", default=f"chat_{date.today().isoformat()}")
    parser.add_argument("--changed-at", type=_parse_date, default=date.today())
    parser.add_argument(
        "--include-expensive",
        action="store_true",
        help="Also approve expensive rows; by default they stay for separate limited-mode review.",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"date must be YYYY-MM-DD, got: {value}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
