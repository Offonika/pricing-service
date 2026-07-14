"""Build UT 10.3 quality property updates for display rows missing quality."""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select, text

from app.core.config import get_settings
from app.infrastructure.db.engines import build_engine
from app.services.assortment_lifecycle_classification_store import (
    ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE,
)
from app.services.display_normalization import normalize_display_quality
from app.services.display_quality_raw_mapping import extract_quality_token_as_in_name
from app.services.exporters.ut103_exchange import load_ut103_env_file, resolve_ut103_exchange_root
from app.services.exporters.ut103_nomenclature_properties import (
    DEFAULT_SOURCE,
    NomenclaturePropertyUpdateMessage,
    NomenclaturePropertyUpdateRow,
    build_nomenclature_property_updates_xml,
    write_nomenclature_property_updates_message,
)

FEATURE_SNAPSHOT_SCHEMA = "procurement_feature_snapshot.v1"
QUALITY_PROPERTY_NAME = "Качество"
CARD_QUALITY_MAX_AGE_DAYS = 183
ONEC_EMPTY_DATE = date(1753, 1, 1)
DO_NOT_ORDER_STATUS = "do_not_order"
NAME_TOKEN_QUALITY_ALIASES = {
    "orig": "ORIG",
    "or": "ORIG",
    "or100": "ORIG100",
    "orig100": "ORIG100",
    "100% or": "ORIG100",
    "or 100%": "ORIG100",
    "or (sp)": "ORIG100 (SP)",
    "original": "ORIG",
    "oem": "OEM",
    "optima": "Optima",
    "оптима": "Optima",
    "стандарт": "Medium",
    "1-я категория": "Medium",
    "premium": "Premium",
    "premium quality": "Premium",
    "премиум": "Premium",
    "aaa": "AAA",
    "ааа": "AAA",
    "hq": "High",
    "аналог": "Аналог",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "original refurbished": "Биток",
}
NORMALIZED_QUALITY_TO_1C_RAW = {
    "Original": "ORIG",
    "Original Refurbished": "Биток",
    "OEM": "OEM",
    "Copy High": "High",
    "Copy Medium": "Medium",
    "Copy Low": "Low",
}
DEFAULT_REPORT_CSV = (
    Path("reports/assortment_lifecycle")
    / date.today().isoformat()
    / "missing-display-quality-updates.csv"
)
DEFAULT_ROWS_JSON = (
    Path("build/assortment") / date.today().isoformat() / "missing-display-quality-update-rows.json"
)
DEFAULT_STATUS_REVIEW_EXCLUSIONS_JSON = Path(
    "config/assortment/display-quality-status-review-exclusions.json"
)

CSV_COLUMNS = [
    "nomenclature_code",
    "article",
    "name",
    "folder",
    "status",
    "status_label",
    "brand_compatibility",
    "model_compatibility",
    "data_quality_score",
    "calculation_unit_level",
    "demand_method_code",
    "suggested_quality_raw",
    "suggestion_source",
    "suggestion_confidence",
    "evidence_field",
    "evidence_text",
    "card_created_at",
    "card_age_days",
    "short_name_1c",
    "additional_name_1c",
    "vendor_sku_1c",
    "update_ready",
    "skip_reason",
    "reason",
]


@dataclass(frozen=True)
class DisplayQualityUpdateBuildResult:
    candidates: tuple[dict[str, Any], ...]
    rows: tuple[NomenclaturePropertyUpdateRow, ...]
    skipped: tuple[dict[str, str], ...]
    source_counts: dict[str, int]
    quality_counts: dict[str, int]


def main() -> int:
    load_ut103_env_file()
    args = _parse_args()
    settings = get_settings()
    database_url = args.database_url or os.environ.get("DATABASE_URL") or settings.database_url

    engine = build_engine(database_url, pool_pre_ping=True)
    try:
        candidates = load_missing_display_quality_candidates(
            engine,
            folder=args.folder,
            limit=args.limit,
            include_do_not_order=args.include_do_not_order,
            excluded_status_review_codes=(
                ()
                if args.include_status_review_required
                else load_status_review_exclusions(args.status_review_exclusions_json)
            ),
        )
        reference_rows = (
            []
            if args.no_reference_suggestions
            else load_display_quality_reference_rows(engine, folder=args.folder)
        )
    finally:
        engine.dispose()

    quality_catalog_values: set[str] | None = None
    if args.quality_catalog_json:
        quality_catalog_values = _load_quality_catalog_json(args.quality_catalog_json)
    if args.validate_onec_catalog:
        onec_database_url = (
            args.onec_database_url
            or os.environ.get("ONEC_DATABASE_URL", "")
            or settings.onec_database_url
            or ""
        )
        if not onec_database_url:
            raise SystemExit("ONEC_DATABASE_URL is required for --validate-onec-catalog")
        onec_engine = build_engine(onec_database_url, pool_pre_ping=True)
        try:
            quality_catalog_values = load_onec_quality_catalog_values(onec_engine)
        finally:
            onec_engine.dispose()

    result = build_missing_display_quality_update_rows(
        candidates,
        quality_overrides=load_quality_overrides(args.quality_map_json),
        reference_quality_by_key=build_reference_quality_by_model(reference_rows),
        quality_catalog_values=quality_catalog_values,
        allow_reference_updates=args.allow_reference_updates,
        run_date=args.run_date,
        card_max_age_days=args.card_max_age_days,
    )

    output_csv = args.output_csv
    output_rows_json = args.output_rows_json
    if output_csv:
        write_candidates_csv(output_csv, result.candidates)
    if output_rows_json:
        write_rows_json(output_rows_json, result.rows)

    message_id = (
        args.message_id or f"missing-display-quality-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    )
    message = None
    if result.rows:
        message = NomenclaturePropertyUpdateMessage(
            message_id=message_id,
            rows=result.rows,
            mode=args.mode,
            approved_by=args.approved_by,
            source=args.source,
        )

    payload: dict[str, Any] = {
        "status": "ready",
        "candidates": len(result.candidates),
        "update_rows": len(result.rows),
        "skipped": len(result.skipped),
        "source_counts": result.source_counts,
        "quality_counts": result.quality_counts,
        "output_csv": str(output_csv) if output_csv else None,
        "output_rows_json": str(output_rows_json) if output_rows_json else None,
        "message_id": message_id,
    }

    if args.print_xml:
        if message is None:
            if args.allow_empty:
                _print_payload(payload, json_mode=args.json)
                return 0
            raise SystemExit("No display quality update rows to export")
        print(build_nomenclature_property_updates_xml(message).decode("windows-1251"))
        return 0

    if args.write_ready:
        if message is None:
            if args.allow_empty:
                _print_payload(payload, json_mode=args.json)
                return 0
            raise SystemExit("No display quality update rows to export")
        exchange_root = resolve_ut103_exchange_root(args.exchange_root)
        output_path = write_nomenclature_property_updates_message(
            exchange_root,
            message,
            overwrite=args.overwrite,
        )
        payload["path"] = str(output_path)

    _print_payload(payload, json_mode=args.json)
    return 0


def load_missing_display_quality_candidates(
    engine,
    *,
    folder: str = "дисплеи",
    limit: int | None = None,
    include_do_not_order: bool = False,
    excluded_status_review_codes: Iterable[str] = (),
) -> list[dict[str, Any]]:
    table = ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE
    excluded_codes = sorted({_clean(code) for code in excluded_status_review_codes if _clean(code)})
    query = (
        select(table)
        .where(table.c.feature_snapshot_schema == FEATURE_SNAPSHOT_SCHEMA)
        .where(table.c.quality_raw == "")
        .order_by(
            table.c.folder.asc(),
            table.c.brand_compatibility.asc(),
            table.c.model_compatibility.asc(),
            table.c.nomenclature_code.asc(),
        )
    )
    if folder:
        query = query.where(table.c.folder.ilike(f"%{folder}%"))
    if not include_do_not_order:
        query = query.where(table.c.status != DO_NOT_ORDER_STATUS)
    if excluded_codes:
        query = query.where(table.c.nomenclature_code.not_in(excluded_codes))
    if limit:
        query = query.limit(limit)
    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(query).mappings()]
    return [
        row for row in rows if "quality_raw" in _json_list(row.get("missing_required_attributes"))
    ]


def load_display_quality_reference_rows(
    engine,
    *,
    folder: str = "дисплеи",
) -> list[dict[str, Any]]:
    table = ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE
    query = (
        select(table)
        .where(table.c.feature_snapshot_schema == FEATURE_SNAPSHOT_SCHEMA)
        .where(table.c.quality_raw != "")
        .order_by(table.c.brand_compatibility.asc(), table.c.model_compatibility.asc())
    )
    if folder:
        query = query.where(table.c.folder.ilike(f"%{folder}%"))
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(query).mappings()]


def load_onec_quality_catalog_values(engine) -> set[str]:
    query = text("""
        SELECT DISTINCT LTRIM(RTRIM(v._Description)) AS quality_value
        FROM _Reference42 v
        JOIN _Chrc401 ch ON ch._IDRRef = v._OwnerIDRRef
        WHERE ch._Description = :property_name
          AND v._Marked = 0
          AND LTRIM(RTRIM(v._Description)) <> ''
        ORDER BY LTRIM(RTRIM(v._Description))
    """)
    with engine.connect() as conn:
        rows = [
            dict(row._mapping)
            for row in conn.execute(query, {"property_name": QUALITY_PROPERTY_NAME})
        ]
    return {value for row in rows if (value := _clean(row.get("quality_value")))}


def build_reference_quality_by_model(
    reference_rows: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], str]:
    values_by_key: dict[tuple[str, str], set[str]] = {}
    for row in reference_rows:
        key = _brand_model_key(row)
        quality_raw = _clean(row.get("quality_raw"))
        if key and quality_raw:
            values_by_key.setdefault(key, set()).add(quality_raw)
    return {key: next(iter(values)) for key, values in values_by_key.items() if len(values) == 1}


def load_quality_overrides(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, Mapping) and "items" not in payload:
        return {
            _clean(code): {"quality_raw": _clean(value), "reason": "manual_map"}
            for code, value in payload.items()
            if _clean(code) and _clean(value)
        }
    raw_items = payload.get("items") if isinstance(payload, Mapping) else payload
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        raise ValueError("quality map must be a list or an object with items")
    result: dict[str, dict[str, str]] = {}
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            raise ValueError("quality map item must be an object")
        code = _clean(raw.get("nomenclature_code") or raw.get("code"))
        quality_raw = _clean(
            raw.get("quality_raw") or raw.get("quality") or raw.get("new_value_name")
        )
        if not code or not quality_raw:
            continue
        result[code] = {
            "quality_raw": quality_raw,
            "reason": _clean(raw.get("reason")) or "manual_map",
            "approved_by": _clean(raw.get("approved_by")),
        }
    return result


def load_status_review_exclusions(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    raw_items = payload.get("items") if isinstance(payload, Mapping) else payload
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        raise ValueError("status review exclusions must be a list or an object with items")
    result: set[str] = set()
    for raw in raw_items:
        if isinstance(raw, Mapping):
            code = _clean(raw.get("nomenclature_code") or raw.get("code"))
            excluded = raw.get("exclude_from_quality_queue", True) is not False
        else:
            code = _clean(raw)
            excluded = True
        if code and excluded:
            result.add(code)
    return result


def build_missing_display_quality_update_rows(
    candidates: Sequence[Mapping[str, Any]],
    *,
    quality_overrides: Mapping[str, Mapping[str, str]] | None = None,
    reference_quality_by_key: Mapping[tuple[str, str], str] | None = None,
    quality_catalog_values: Iterable[str] | None = None,
    allow_reference_updates: bool = False,
    run_date: date | None = None,
    card_max_age_days: int = CARD_QUALITY_MAX_AGE_DAYS,
) -> DisplayQualityUpdateBuildResult:
    quality_overrides = quality_overrides or {}
    reference_quality_by_key = reference_quality_by_key or {}
    catalog_by_normalized = (
        {_normalize_quality_key(value): value for value in quality_catalog_values if _clean(value)}
        if quality_catalog_values is not None
        else None
    )
    effective_date = run_date or date.today()
    if card_max_age_days <= 0:
        raise ValueError("card_max_age_days must be positive")

    report_rows: list[dict[str, Any]] = []
    update_rows: list[NomenclaturePropertyUpdateRow] = []
    skipped: list[dict[str, str]] = []
    source_counts: Counter[str] = Counter()
    quality_counts: Counter[str] = Counter()

    for candidate in candidates:
        code = _clean(candidate.get("nomenclature_code"))
        name = _clean(candidate.get("name"))
        if not code:
            skipped.append({"reason": "missing_nomenclature_code"})
            continue

        suggestion = _suggest_quality(
            candidate,
            quality_overrides=quality_overrides,
            reference_quality_by_key=reference_quality_by_key,
            effective_date=effective_date,
            card_max_age_days=card_max_age_days,
        )
        suggested_quality = suggestion["quality_raw"]
        source = suggestion["source"]
        skip_reason = suggestion.get("skip_reason", "")
        source_record = _json_object(candidate.get("source_record"))
        update_ready = (
            bool(suggested_quality)
            and (source != "same_model_snapshot" or allow_reference_updates)
            and bool(suggestion.get("eligible", True))
        )

        if not suggested_quality:
            skip_reason = "quality_not_suggested"
        elif not suggestion.get("eligible", True):
            update_ready = False
        elif catalog_by_normalized is not None:
            canonical_quality = catalog_by_normalized.get(_normalize_quality_key(suggested_quality))
            if not canonical_quality:
                skip_reason = "quality_catalog_value_missing"
                update_ready = False
            else:
                suggested_quality = canonical_quality
        elif source == "same_model_snapshot" and not allow_reference_updates:
            skip_reason = "needs_manual_quality_approval"

        report_row = {
            "nomenclature_code": code,
            "article": _clean(candidate.get("article") or source_record.get("article")),
            "name": name,
            "folder": _clean(candidate.get("folder")),
            "status": _clean(candidate.get("status")),
            "status_label": _clean(candidate.get("status_label")),
            "brand_compatibility": _clean(candidate.get("brand_compatibility")),
            "model_compatibility": _clean(candidate.get("model_compatibility")),
            "data_quality_score": _clean(candidate.get("data_quality_score")),
            "calculation_unit_level": _clean(candidate.get("calculation_unit_level")),
            "demand_method_code": _clean(candidate.get("demand_method_code")),
            "suggested_quality_raw": suggested_quality,
            "suggestion_source": source,
            "suggestion_confidence": suggestion["confidence"],
            "evidence_field": suggestion["evidence_field"],
            "evidence_text": suggestion["evidence_text"],
            "card_created_at": suggestion.get("card_created_at", ""),
            "card_age_days": suggestion.get("card_age_days", ""),
            "short_name_1c": _clean(source_record.get("short_name_1c")),
            "additional_name_1c": _clean(source_record.get("additional_name_1c")),
            "vendor_sku_1c": _clean(source_record.get("vendor_sku_1c")),
            "update_ready": update_ready,
            "skip_reason": skip_reason,
            "reason": suggestion["reason"],
        }
        report_rows.append(report_row)

        if not update_ready:
            skipped.append({"nomenclature_code": code, "reason": skip_reason})
            continue

        update_rows.append(
            NomenclaturePropertyUpdateRow(
                idempotency_key=(
                    f"nom-prop:{code}:{QUALITY_PROPERTY_NAME}:{effective_date.isoformat()}:r1"
                ),
                nomenclature_code=code,
                property_name=QUALITY_PROPERTY_NAME,
                value_type="property_value",
                new_value_name=suggested_quality,
                reason=suggestion["reason"],
                approved_by=_clean(quality_overrides.get(code, {}).get("approved_by")),
            )
        )
        source_counts[source] += 1
        quality_counts[suggested_quality] += 1

    return DisplayQualityUpdateBuildResult(
        candidates=tuple(report_rows),
        rows=tuple(update_rows),
        skipped=tuple(skipped),
        source_counts=dict(sorted(source_counts.items())),
        quality_counts=dict(sorted(quality_counts.items())),
    )


def write_candidates_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _csv_value(row.get(column)) for column in CSV_COLUMNS})
    return path


def write_rows_json(path: Path, rows: Sequence[NomenclaturePropertyUpdateRow]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"items": [_row_to_mapping(row) for row in rows]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _suggest_quality(
    candidate: Mapping[str, Any],
    *,
    quality_overrides: Mapping[str, Mapping[str, str]],
    reference_quality_by_key: Mapping[tuple[str, str], str],
    effective_date: date,
    card_max_age_days: int,
) -> dict[str, Any]:
    code = _clean(candidate.get("nomenclature_code"))
    card_created_at = _candidate_card_created_at(candidate)
    card_age_days = _card_age_days(card_created_at, effective_date)
    card_age_fields = _card_age_report_fields(card_created_at, card_age_days)

    override = quality_overrides.get(code)
    if override:
        quality_raw = _clean(override.get("quality_raw"))
        if quality_raw:
            reason = _clean(override.get("reason")) or "Ручное заполнение пустого качества."
            return {
                "quality_raw": quality_raw,
                "source": "manual_map",
                "confidence": "1.00",
                "reason": reason,
                "evidence_field": "quality_map_json",
                "evidence_text": quality_raw,
                "eligible": True,
                "skip_reason": "",
                **card_age_fields,
            }

    for field_name, field_value in _quality_text_candidates(candidate):
        name_quality = _quality_raw_from_name(field_value)
        if name_quality:
            return _card_suggestion(
                quality_raw=name_quality,
                source="name_token",
                confidence="0.90",
                reason="Качество явно указано в одном из наименований номенклатуры.",
                evidence_field=field_name,
                evidence_text=field_value,
                card_created_at=card_created_at,
                card_age_days=card_age_days,
                card_max_age_days=card_max_age_days,
            )

    for field_name, field_value in _quality_sku_candidates(candidate):
        sku_quality = _quality_raw_from_vendor_sku(field_value)
        if sku_quality:
            return _card_suggestion(
                quality_raw=sku_quality,
                source="vendor_sku_suffix",
                confidence="0.85",
                reason="Качество указано суффиксом в дополнительном коде/SKU.",
                evidence_field=field_name,
                evidence_text=field_value,
                card_created_at=card_created_at,
                card_age_days=card_age_days,
                card_max_age_days=card_max_age_days,
            )

    reference_quality = reference_quality_by_key.get(_brand_model_key(candidate))
    if reference_quality:
        return {
            "quality_raw": reference_quality,
            "source": "same_model_snapshot",
            "confidence": "0.80",
            "reason": "В витрине есть такая же связка бренд+модель с единственным качеством.",
            "evidence_field": "same_model_snapshot",
            "evidence_text": reference_quality,
            "eligible": True,
            "skip_reason": "",
            **card_age_fields,
        }

    return {
        "quality_raw": "",
        "source": "",
        "confidence": "0.00",
        "reason": "",
        "evidence_field": "",
        "evidence_text": "",
        "eligible": False,
        "skip_reason": "",
        **card_age_fields,
    }


def _card_suggestion(
    *,
    quality_raw: str,
    source: str,
    confidence: str,
    reason: str,
    evidence_field: str,
    evidence_text: str,
    card_created_at: date | None,
    card_age_days: int | None,
    card_max_age_days: int,
) -> dict[str, Any]:
    if card_age_days is None:
        eligible = False
        skip_reason = "card_age_unknown"
    elif card_age_days > card_max_age_days:
        eligible = False
        skip_reason = "card_older_than_6_months"
    else:
        eligible = True
        skip_reason = ""
    return {
        "quality_raw": quality_raw,
        "source": source,
        "confidence": confidence,
        "reason": reason,
        "evidence_field": evidence_field,
        "evidence_text": evidence_text,
        "eligible": eligible,
        "skip_reason": skip_reason,
        **_card_age_report_fields(card_created_at, card_age_days),
    }


def _quality_text_candidates(candidate: Mapping[str, Any]) -> list[tuple[str, str]]:
    source_record = _json_object(candidate.get("source_record"))
    return _dedupe_field_values(
        (
            ("name", candidate.get("name")),
            ("source_record.name", source_record.get("name")),
            ("source_record.short_name_1c", source_record.get("short_name_1c")),
            ("source_record.additional_name_1c", source_record.get("additional_name_1c")),
        )
    )


def _quality_sku_candidates(candidate: Mapping[str, Any]) -> list[tuple[str, str]]:
    source_record = _json_object(candidate.get("source_record"))
    return _dedupe_field_values(
        (
            ("source_record.vendor_sku_1c", source_record.get("vendor_sku_1c")),
            ("article", candidate.get("article")),
            ("source_record.article", source_record.get("article")),
        )
    )


def _candidate_card_created_at(candidate: Mapping[str, Any]) -> date | None:
    source_record = _json_object(candidate.get("source_record"))
    for value in (
        source_record.get("card_created_at"),
        source_record.get("created_at"),
        source_record.get("onec_novelty_date"),
    ):
        parsed = _parse_date(value)
        if parsed is not None:
            return parsed
    return None


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        result = value.date()
    elif isinstance(value, date):
        result = value
    else:
        text_value = str(value).strip().removesuffix("Z")
        if "T" in text_value:
            text_value = text_value.split("T", 1)[0]
        if " " in text_value:
            text_value = text_value.split(" ", 1)[0]
        try:
            result = date.fromisoformat(text_value)
        except ValueError:
            return None
    return None if result <= ONEC_EMPTY_DATE else result


def _card_age_days(card_created_at: date | None, effective_date: date) -> int | None:
    if card_created_at is None:
        return None
    return max(0, (effective_date - card_created_at).days)


def _card_age_report_fields(
    card_created_at: date | None,
    card_age_days: int | None,
) -> dict[str, str]:
    return {
        "card_created_at": card_created_at.isoformat() if card_created_at else "",
        "card_age_days": str(card_age_days) if card_age_days is not None else "",
    }


def _dedupe_field_values(values: Iterable[tuple[str, Any]]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for field_name, value in values:
        text_value = _clean(value)
        key = text_value.casefold()
        if text_value and key not in seen:
            result.append((field_name, text_value))
            seen.add(key)
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build review rows and optional nomenclature_property_updates.v1 package "
            "for display rows where procurement_feature_snapshot.v1 misses quality_raw."
        )
    )
    parser.add_argument("--database-url", default="")
    parser.add_argument("--folder", default="дисплеи")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--include-do-not-order",
        action="store_true",
        help="Include do_not_order rows in the quality review queue.",
    )
    parser.add_argument(
        "--status-review-exclusions-json",
        type=Path,
        default=DEFAULT_STATUS_REVIEW_EXCLUSIONS_JSON,
        help=(
            "JSON list/object with nomenclature codes excluded from quality queue "
            "because procurement status review is required first."
        ),
    )
    parser.add_argument(
        "--include-status-review-required",
        action="store_true",
        help="Include rows from status-review exclusions in the quality review queue.",
    )
    parser.add_argument(
        "--quality-map-json",
        type=Path,
        help="JSON object/list with approved quality_raw values by nomenclature_code.",
    )
    parser.add_argument(
        "--quality-catalog-json",
        type=Path,
        help="Optional JSON list of allowed 1C quality values for local validation.",
    )
    parser.add_argument(
        "--validate-onec-catalog",
        action="store_true",
        help="Read live 1C property catalog for Качество and validate update values.",
    )
    parser.add_argument("--onec-database-url", default="")
    parser.add_argument(
        "--allow-reference-updates",
        action="store_true",
        help="Allow same-model snapshot suggestions to become 1C update rows.",
    )
    parser.add_argument(
        "--no-reference-suggestions",
        action="store_true",
        help="Do not suggest quality from ready rows with the same brand/model.",
    )
    parser.add_argument("--run-date", type=date.fromisoformat, default=None)
    parser.add_argument(
        "--card-max-age-days",
        type=int,
        default=CARD_QUALITY_MAX_AGE_DAYS,
        help="Max card age for auto-using quality from 1C card fields.",
    )
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_REPORT_CSV)
    parser.add_argument("--output-rows-json", type=Path, default=DEFAULT_ROWS_JSON)
    parser.add_argument("--message-id", help="Stable message id for the export package")
    parser.add_argument("--mode", choices=("dry_run", "apply"), default="dry_run")
    parser.add_argument("--approved-by", default="", help="Required for apply mode")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="Source value for XML header")
    parser.add_argument("--print-xml", action="store_true", help="Print XML without writing file")
    parser.add_argument(
        "--write-ready", action="store_true", help="Write ready XML to exchange root"
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Exit successfully without XML when no quality rows are ready for 1C.",
    )
    parser.add_argument("--exchange-root", help="UT103 exchange root")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing ready XML")
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary")
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be positive")
    if args.card_max_age_days <= 0:
        raise SystemExit("--card-max-age-days must be positive")
    return args


def _load_quality_catalog_json(path: Path) -> set[str]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    values = payload.get("items") if isinstance(payload, Mapping) else payload
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ValueError("quality catalog JSON must be a list or an object with items")
    return {_clean(value) for value in values if _clean(value)}


def _brand_model_key(row: Mapping[str, Any]) -> tuple[str, str]:
    brand = _normalize_key(row.get("brand_compatibility"))
    model = _normalize_key(row.get("model_compatibility"))
    if not brand or not model:
        return ("", "")
    return (brand, model)


def _normalize_quality_key(value: Any) -> str:
    return _normalize_key(value)


def _quality_raw_from_name(name: str) -> str:
    token_quality = _canonical_name_token_quality(extract_quality_token_as_in_name(name))
    if token_quality:
        return token_quality
    return _canonical_name_token_quality(name)


def _quality_raw_from_vendor_sku(value: str) -> str:
    parts = [_normalize_quality_key(part) for part in value.replace("_", "-").split("-")]
    parts = [part for part in parts if part]
    if not parts:
        return ""
    suffix = parts[-1]
    if suffix in {"or", "orig"}:
        return "ORIG"
    if suffix in {"or100", "orig100"}:
        return "ORIG100"
    return ""


def _canonical_name_token_quality(value: str | None) -> str:
    quality = _clean(value)
    if not quality:
        return ""
    normalized_quality = normalize_display_quality(quality)
    if not normalized_quality:
        return ""
    return NAME_TOKEN_QUALITY_ALIASES.get(
        _normalize_quality_key(quality),
        NORMALIZED_QUALITY_TO_1C_RAW.get(normalized_quality, ""),
    )


def _normalize_key(value: Any) -> str:
    return " ".join(_clean(value).casefold().split())


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except ValueError:
            return [value] if value else []
        return loaded if isinstance(loaded, list) else [loaded]
    if value in (None, ""):
        return []
    return [value]


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except ValueError:
            return {}
        return dict(loaded) if isinstance(loaded, Mapping) else {}
    return {}


def _row_to_mapping(row: NomenclaturePropertyUpdateRow) -> dict[str, Any]:
    return {
        "idempotency_key": row.idempotency_key,
        "nomenclature_code": row.nomenclature_code,
        "property_name": row.property_name,
        "value_type": row.value_type,
        "target_kind": row.target_kind,
        "new_value": row.new_value,
        "new_value_name": row.new_value_name,
        "new_value_tag": row.new_value_tag,
        "expected_current_value_name": row.expected_current_value_name,
        "expected_current_value_tag": row.expected_current_value_tag,
        "reason": row.reason,
        "approved_by": row.approved_by,
    }


def _csv_value(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def _print_payload(payload: Mapping[str, Any], *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _clean(value: Any) -> str:
    return str(value or "").strip()


if __name__ == "__main__":
    raise SystemExit(main())
