from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
    insert,
    inspect,
    select,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

ASSORTMENT_LIFECYCLE_METADATA = MetaData()

ASSORTMENT_LIFECYCLE_RUN_TABLE = Table(
    "assortment_lifecycle_classification_run",
    ASSORTMENT_LIFECYCLE_METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_key", String(128), nullable=False, unique=True),
    Column("folder", String(255), nullable=False),
    Column("source", String(64), nullable=False),
    Column("source_status", String(32), nullable=False, default="ready"),
    Column("items_total", Integer, nullable=False, default=0),
    Column("summary", JSON, nullable=False, default=dict),
    Column("error_text", Text, nullable=True),
    Column("started_at", DateTime, nullable=False, default=func.now()),
    Column("finished_at", DateTime, nullable=True),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
)

ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE = Table(
    "assortment_lifecycle_classification",
    ASSORTMENT_LIFECYCLE_METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("nomenclature_code", String(64), nullable=False, unique=True),
    Column("name", Text, nullable=False, default=""),
    Column("folder", Text, nullable=False, default=""),
    Column("status", String(64), nullable=False),
    Column("status_label", String(128), nullable=False),
    Column("recommended_status", String(64), nullable=True),
    Column("reason_codes", JSON, nullable=False, default=list),
    Column("reason_text", Text, nullable=False, default=""),
    Column("blockers", JSON, nullable=False, default=list),
    Column("export_blockers", JSON, nullable=False, default=list),
    Column("auto_order_allowed", Boolean, nullable=False, default=False),
    Column("manual_review_required", Boolean, nullable=False, default=False),
    Column("expensive_profile", String(64), nullable=True),
    Column("expensive_profile_label", String(128), nullable=False, default=""),
    Column("expensive_reason_codes", JSON, nullable=False, default=list),
    Column("commercial_marks", JSON, nullable=False, default=list),
    Column("commercial_mark_labels", JSON, nullable=False, default=list),
    Column("commercial_mark_blockers", JSON, nullable=False, default=list),
    Column("exclusive_kind", String(64), nullable=False, default=""),
    Column("exclusive_confidence", String(64), nullable=False, default=""),
    Column("exclusive_checked_at", String(32), nullable=True),
    Column("exclusive_review_at", String(32), nullable=True),
    Column("exclusive_reason", Text, nullable=False, default=""),
    Column("exclusive_approved_by", String(255), nullable=False, default=""),
    Column("exclusive_evidence_refs", JSON, nullable=False, default=list),
    Column("exclusive_min_stock_qty", String(64), nullable=True),
    Column("feature_snapshot_schema", String(64), nullable=False, default=""),
    Column("product_ref", String(64), nullable=False, default=""),
    Column("article", String(128), nullable=False, default=""),
    Column("kind_1c", Text, nullable=False, default=""),
    Column("subject_1c", Text, nullable=False, default=""),
    Column("category_1c", Text, nullable=False, default=""),
    Column("item_tags", JSON, nullable=False, default=list),
    Column("brand_compatibility", Text, nullable=False, default=""),
    Column("model_compatibility", Text, nullable=False, default=""),
    Column("quality_raw", Text, nullable=False, default=""),
    Column("quality_normalized", String(128), nullable=False, default=""),
    Column("characteristic_values", JSON, nullable=False, default=dict),
    Column("price_segment", String(64), nullable=False, default=""),
    Column("data_quality_score", String(16), nullable=False, default=""),
    Column("missing_required_attributes", JSON, nullable=False, default=list),
    Column("future_ka_mapping_status", String(32), nullable=False, default=""),
    Column("calculation_unit_level", String(64), nullable=False, default=""),
    Column("calculation_unit_key", Text, nullable=False, default=""),
    Column("calculation_unit_source", String(64), nullable=False, default=""),
    Column("calculation_unit_confidence", String(16), nullable=False, default=""),
    Column("calculation_unit_reason", Text, nullable=False, default=""),
    Column("demand_method_code", String(64), nullable=False, default=""),
    Column("demand_method_reason", Text, nullable=False, default=""),
    Column("demand_method_confidence", String(16), nullable=False, default=""),
    Column("sales_point_warehouse_codes", JSON, nullable=False, default=list),
    Column("manager_need_signals", JSON, nullable=False, default=list),
    Column("source_record", JSON, nullable=False, default=dict),
    Column("source_hash", String(64), nullable=False),
    Column("source", String(64), nullable=False),
    Column("classified_at", DateTime, nullable=False),
    Column(
        "last_run_id",
        Integer,
        ForeignKey("assortment_lifecycle_classification_run.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("updated_at", DateTime, nullable=False, server_default=func.now(), onupdate=func.now()),
)


@dataclass(frozen=True)
class AssortmentLifecycleClassificationResult:
    run_id: int | None
    run_key: str
    folder: str
    source: str
    source_status: str
    items_total: int
    written_items: int
    summary: dict[str, Any]
    dry_run: bool = False


def utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def build_classification_rows(
    *,
    records: Sequence[Mapping[str, Any]],
    summaries: Sequence[Mapping[str, Any]],
    source: str,
    classified_at: datetime,
) -> list[dict[str, Any]]:
    records_by_code = {
        str(record.get("nomenclature_code") or record.get("NomenclatureCode") or "").strip(): record
        for record in records
    }
    rows: list[dict[str, Any]] = []
    for summary in summaries:
        code = str(summary.get("nomenclature_code") or "").strip()
        if not code:
            continue
        source_record = dict(records_by_code.get(code, {}))
        rows.append(
            {
                "nomenclature_code": code,
                "name": str(summary.get("name") or ""),
                "folder": str(summary.get("folder") or ""),
                "status": str(summary.get("status") or ""),
                "status_label": str(summary.get("status_label") or ""),
                "recommended_status": summary.get("recommended_status"),
                "reason_codes": _json_list(summary.get("reason_codes")),
                "reason_text": str(summary.get("reason_text") or ""),
                "blockers": _json_list(summary.get("blockers")),
                "export_blockers": _json_list(summary.get("export_blockers")),
                "auto_order_allowed": bool(summary.get("auto_order_allowed")),
                "manual_review_required": bool(summary.get("manual_review_required")),
                "expensive_profile": summary.get("expensive_profile"),
                "expensive_profile_label": str(summary.get("expensive_profile_label") or ""),
                "expensive_reason_codes": _json_list(summary.get("expensive_reason_codes")),
                "commercial_marks": _json_list(summary.get("commercial_marks")),
                "commercial_mark_labels": _json_list(summary.get("commercial_mark_labels")),
                "commercial_mark_blockers": _json_list(summary.get("commercial_mark_blockers")),
                "exclusive_kind": str(summary.get("exclusive_kind") or ""),
                "exclusive_confidence": str(summary.get("exclusive_confidence") or ""),
                "exclusive_checked_at": summary.get("exclusive_checked_at"),
                "exclusive_review_at": summary.get("exclusive_review_at"),
                "exclusive_reason": str(summary.get("exclusive_reason") or ""),
                "exclusive_approved_by": str(summary.get("exclusive_approved_by") or ""),
                "exclusive_evidence_refs": _json_list(summary.get("exclusive_evidence_refs")),
                "exclusive_min_stock_qty": summary.get("exclusive_min_stock_qty"),
                "feature_snapshot_schema": _source_text(
                    source_record,
                    "feature_snapshot_schema",
                ),
                "product_ref": _source_text(
                    source_record,
                    "product_ref",
                    "nomenclature_ref",
                    "ref",
                ),
                "article": _source_text(source_record, "article", "sku", "SKU"),
                "kind_1c": _source_text(
                    source_record,
                    "kind_1c",
                    "nomenclature_kind",
                ),
                "subject_1c": _source_text(source_record, "subject_1c", "subject"),
                "category_1c": _source_text(source_record, "category_1c", "category"),
                "item_tags": _json_list(source_record.get("item_tags")),
                "brand_compatibility": _source_text(source_record, "brand_compatibility"),
                "model_compatibility": _source_text(source_record, "model_compatibility"),
                "quality_raw": _source_text(source_record, "quality_raw"),
                "quality_normalized": _source_text(source_record, "quality_normalized"),
                "characteristic_values": _json_object(source_record.get("characteristic_values")),
                "price_segment": _source_text(source_record, "price_segment"),
                "data_quality_score": _source_text(source_record, "data_quality_score"),
                "missing_required_attributes": _json_list(
                    source_record.get("missing_required_attributes")
                ),
                "future_ka_mapping_status": _source_text(
                    source_record,
                    "future_ka_mapping_status",
                ),
                "calculation_unit_level": _source_text(
                    source_record,
                    "calculation_unit_level",
                ),
                "calculation_unit_key": _source_text(source_record, "calculation_unit_key"),
                "calculation_unit_source": _source_text(
                    source_record,
                    "calculation_unit_source",
                ),
                "calculation_unit_confidence": _source_text(
                    source_record,
                    "calculation_unit_confidence",
                ),
                "calculation_unit_reason": _source_text(
                    source_record,
                    "calculation_unit_reason",
                ),
                "demand_method_code": _source_text(source_record, "demand_method_code"),
                "demand_method_reason": _source_text(source_record, "demand_method_reason"),
                "demand_method_confidence": _source_text(
                    source_record,
                    "demand_method_confidence",
                ),
                "sales_point_warehouse_codes": _json_list(
                    summary.get("sales_point_warehouse_codes")
                ),
                "manager_need_signals": _json_list(summary.get("manager_need_signals")),
                "source_record": source_record,
                "source_hash": _source_hash(source_record),
                "source": source,
                "classified_at": classified_at,
            }
        )
    return rows


def build_classification_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(row.get("status") or "") for row in rows)
    statuses.pop("", None)
    commercial_marks: Counter[str] = Counter()
    for row in rows:
        for mark in _json_list(row.get("commercial_marks")):
            commercial_marks[str(mark)] += 1
    return {
        "statuses": dict(sorted(statuses.items())),
        "commercial_marks": dict(sorted(commercial_marks.items())),
        "manual_review_required": sum(1 for row in rows if row.get("manual_review_required")),
        "export_blocked": sum(1 for row in rows if row.get("export_blockers")),
        "auto_order_allowed": sum(1 for row in rows if row.get("auto_order_allowed")),
        "feature_snapshot_ready": sum(
            1 for row in rows if row.get("future_ka_mapping_status") == "ready"
        ),
        "feature_snapshot_needs_mapping": sum(
            1 for row in rows if row.get("future_ka_mapping_status") == "needs_mapping"
        ),
        "expensive_profile": dict(
            sorted(
                Counter(
                    str(row.get("expensive_profile") or "none")
                    for row in rows
                    if row.get("expensive_profile")
                ).items()
            )
        ),
    }


def fetch_previous_statuses(
    engine: Engine,
    *,
    nomenclature_codes: Sequence[str] = (),
) -> dict[str, str]:
    """Статус, присвоенный прошлым расчётом, по коду номенклатуры.

    Нужен гистерезису в формуле: карточка без явного роста или спада остаётся
    в прежнем статусе и не мигает между «Растим» и «Поддерживаем» от прогона к
    прогону. Таблицы ещё нет (первый запуск, чистая тестовая база) — возвращаем
    пусто, формула просто посчитает статус с нуля.
    """
    table_name = ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.name
    if not inspect(engine).has_table(table_name):
        return {}
    codes = [code for code in {str(value or "").strip() for value in nomenclature_codes} if code]
    statement = select(
        ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.c.nomenclature_code,
        ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.c.status,
    )
    result: dict[str, str] = {}
    with engine.connect() as connection:
        if codes:
            for chunk_start in range(0, len(codes), 1000):
                chunk = codes[chunk_start : chunk_start + 1000]
                rows = connection.execute(
                    statement.where(
                        ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.c.nomenclature_code.in_(chunk)
                    )
                ).mappings()
                for row in rows:
                    _collect_previous_status(result, row)
        else:
            for row in connection.execute(statement).mappings():
                _collect_previous_status(result, row)
    return result


def _collect_previous_status(result: dict[str, str], row: Mapping[str, Any]) -> None:
    code = str(row.get("nomenclature_code") or "").strip()
    status = str(row.get("status") or "").strip()
    if code and status:
        result[code] = status


def persist_classification_rows(
    engine: Engine,
    *,
    rows: Sequence[Mapping[str, Any]],
    run_key: str,
    folder: str,
    source: str,
    started_at: datetime,
    finished_at: datetime,
    dry_run: bool = False,
) -> AssortmentLifecycleClassificationResult:
    row_values = [dict(row) for row in rows]
    summary = build_classification_summary(row_values)
    if dry_run:
        return AssortmentLifecycleClassificationResult(
            run_id=None,
            run_key=run_key,
            folder=folder,
            source=source,
            source_status="dry_run",
            items_total=len(row_values),
            written_items=0,
            summary=summary,
            dry_run=True,
        )

    with engine.begin() as conn:
        run_result = conn.execute(
            insert(ASSORTMENT_LIFECYCLE_RUN_TABLE).values(
                run_key=run_key,
                folder=folder,
                source=source,
                source_status="ready",
                items_total=len(row_values),
                summary=summary,
                started_at=started_at,
                finished_at=finished_at,
            )
        )
        run_id = int(run_result.inserted_primary_key[0])
        if row_values:
            values_with_run = [{**row, "last_run_id": run_id} for row in row_values]
            conn.execute(_upsert_classification_statement(engine, values_with_run))

    return AssortmentLifecycleClassificationResult(
        run_id=run_id,
        run_key=run_key,
        folder=folder,
        source=source,
        source_status="ready",
        items_total=len(row_values),
        written_items=len(row_values),
        summary=summary,
    )


def result_to_mapping(result: AssortmentLifecycleClassificationResult) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "run_key": result.run_key,
        "folder": result.folder,
        "source": result.source,
        "source_status": result.source_status,
        "items_total": result.items_total,
        "written_items": result.written_items,
        "summary": result.summary,
        "dry_run": result.dry_run,
    }


def _upsert_classification_statement(engine: Engine, rows: Sequence[Mapping[str, Any]]):
    if engine.dialect.name == "postgresql":
        statement = postgresql_insert(ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE).values(rows)
    elif engine.dialect.name == "sqlite":
        statement = sqlite_insert(ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE).values(rows)
    else:
        raise RuntimeError(f"Unsupported database dialect for upsert: {engine.dialect.name}")

    excluded = statement.excluded
    update_columns = {
        column.name: excluded[column.name]
        for column in ASSORTMENT_LIFECYCLE_CLASSIFICATION_TABLE.columns
        if column.name not in {"id", "nomenclature_code", "created_at"}
    }
    update_columns["updated_at"] = func.now()
    return statement.on_conflict_do_update(
        index_elements=["nomenclature_code"],
        set_=update_columns,
    )


def _source_hash(record: Mapping[str, Any]) -> str:
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _source_text(source_record: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = source_record.get(name)
        if value not in (None, ""):
            return str(value)
    return ""
