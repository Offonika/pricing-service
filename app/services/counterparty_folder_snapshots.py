from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.models.counterparty_folder_snapshot import CounterpartyFolderSnapshot
from app.services.counterparty_folder_recommendations import (
    CounterpartyFolderRow,
    _normalize_ref,
    _ref_key,
)
from app.services.receivables import _hex_ref_expr, _with_nolock


@dataclass(frozen=True)
class CounterpartyFolderSnapshotSyncResult:
    snapshot_date: date
    fetched_count: int
    inserted_count: int
    updated_count: int
    deleted_count: int


def _active_counterparty_filter(*, dialect_name: str) -> str:
    if dialect_name == "mssql":
        return "cp._Marked = 0x00 AND cp._Folder = 0x01"
    return "cp._Marked = 0 AND cp._Folder = 1"


def _folder_key(ref_value: str | None, name_value: str | None) -> str:
    ref = _normalize_ref(ref_value)
    if ref:
        return _ref_key(ref)
    return str(name_value or "").strip().casefold()


def fetch_all_counterparty_current_folders(onec_engine) -> list[CounterpartyFolderRow]:
    dialect_name = onec_engine.dialect.name
    nolock = _with_nolock(dialect_name=dialect_name)
    counterparty_ref_expr = _hex_ref_expr("cp._IDRRef", dialect_name=dialect_name)
    current_folder_ref_expr = _hex_ref_expr("folder._IDRRef", dialect_name=dialect_name)
    active_filter = _active_counterparty_filter(dialect_name=dialect_name)

    stmt = text(f"""
        SELECT
            {counterparty_ref_expr} AS counterparty_ref,
            cp._Code AS counterparty_code,
            cp._Description AS counterparty_name,
            {current_folder_ref_expr} AS current_folder_ref,
            folder._Description AS current_folder_name
        FROM _Reference54 AS cp {nolock}
        LEFT JOIN _Reference54 AS folder {nolock}
            ON folder._IDRRef = cp._ParentIDRRef
        WHERE {active_filter}
    """)

    rows: list[CounterpartyFolderRow] = []
    with onec_engine.connect() as conn:
        for row in conn.execute(stmt).mappings():
            counterparty_ref = _normalize_ref(row.get("counterparty_ref"))
            if not counterparty_ref:
                continue
            rows.append(
                CounterpartyFolderRow(
                    counterparty_ref=counterparty_ref,
                    counterparty_code=_normalize_ref(row.get("counterparty_code")) or None,
                    counterparty_name=_normalize_ref(row.get("counterparty_name")) or None,
                    current_folder_ref=_normalize_ref(row.get("current_folder_ref")) or None,
                    current_folder_name=_normalize_ref(row.get("current_folder_name")) or None,
                )
            )
    return rows


def sync_counterparty_folder_snapshot(
    session: Session,
    *,
    onec_engine,
    snapshot_date: date,
) -> CounterpartyFolderSnapshotSyncResult:
    source_rows = fetch_all_counterparty_current_folders(onec_engine)
    source_by_ref = {_ref_key(row.counterparty_ref): row for row in source_rows}
    existing_rows = (
        session.execute(
            select(CounterpartyFolderSnapshot).where(
                CounterpartyFolderSnapshot.snapshot_date == snapshot_date
            )
        )
        .scalars()
        .all()
    )
    existing_by_ref = {_ref_key(row.counterparty_ref): row for row in existing_rows}

    inserted_count = 0
    updated_count = 0
    for key, source_row in source_by_ref.items():
        existing = existing_by_ref.get(key)
        if existing is None:
            session.add(
                CounterpartyFolderSnapshot(
                    snapshot_date=snapshot_date,
                    counterparty_ref=source_row.counterparty_ref,
                    counterparty_name=source_row.counterparty_name,
                    current_folder_ref=source_row.current_folder_ref,
                    current_folder_name=source_row.current_folder_name,
                )
            )
            inserted_count += 1
            continue

        changed = (
            existing.counterparty_name != source_row.counterparty_name
            or existing.current_folder_ref != source_row.current_folder_ref
            or existing.current_folder_name != source_row.current_folder_name
        )
        if changed:
            existing.counterparty_name = source_row.counterparty_name
            existing.current_folder_ref = source_row.current_folder_ref
            existing.current_folder_name = source_row.current_folder_name
            updated_count += 1

    missing_keys = set(existing_by_ref) - set(source_by_ref)
    deleted_count = 0
    if missing_keys:
        stale_refs = [existing_by_ref[key].counterparty_ref for key in missing_keys]
        result = session.execute(
            delete(CounterpartyFolderSnapshot).where(
                CounterpartyFolderSnapshot.snapshot_date == snapshot_date,
                CounterpartyFolderSnapshot.counterparty_ref.in_(stale_refs),
            )
        )
        deleted_count = int(result.rowcount or 0)

    session.commit()
    return CounterpartyFolderSnapshotSyncResult(
        snapshot_date=snapshot_date,
        fetched_count=len(source_by_ref),
        inserted_count=inserted_count,
        updated_count=updated_count,
        deleted_count=deleted_count,
    )


def _find_previous_snapshot_date(session: Session, *, snapshot_date: date) -> date | None:
    return session.execute(
        select(func.max(CounterpartyFolderSnapshot.snapshot_date)).where(
            CounterpartyFolderSnapshot.snapshot_date < snapshot_date
        )
    ).scalar_one()


def _build_report_revision(
    *,
    snapshot_date: date,
    previous_snapshot_date: date | None,
    items: Sequence[dict[str, Any]],
) -> str:
    revision_payload = [
        {
            "counterparty_ref": item["counterparty_ref"],
            "old_folder_ref": item.get("old_folder_ref"),
            "new_folder_ref": item.get("new_folder_ref"),
        }
        for item in items
    ]
    raw = json.dumps(
        {
            "date": snapshot_date.isoformat(),
            "previous_date": (
                previous_snapshot_date.isoformat() if previous_snapshot_date is not None else None
            ),
            "items": revision_payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _recommendations_by_counterparty(report: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(report, dict):
        return {}
    payload = report.get("payload")
    if not isinstance(payload, list):
        return {}
    by_ref: dict[str, dict[str, Any]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        counterparty_ref = _normalize_ref(item.get("counterparty_ref"))
        if counterparty_ref:
            by_ref[_ref_key(counterparty_ref)] = item
    return by_ref


def build_counterparty_folder_changes(
    session: Session,
    *,
    snapshot_date: date,
    previous_snapshot_date: date | None = None,
    recommendations_report: dict[str, Any] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    if previous_snapshot_date is None:
        previous_snapshot_date = _find_previous_snapshot_date(session, snapshot_date=snapshot_date)

    current_rows = (
        session.execute(
            select(CounterpartyFolderSnapshot)
            .where(CounterpartyFolderSnapshot.snapshot_date == snapshot_date)
            .order_by(CounterpartyFolderSnapshot.counterparty_name)
        )
        .scalars()
        .all()
    )
    previous_rows: list[CounterpartyFolderSnapshot] = []
    if previous_snapshot_date is not None:
        previous_rows = (
            session.execute(
                select(CounterpartyFolderSnapshot).where(
                    CounterpartyFolderSnapshot.snapshot_date == previous_snapshot_date
                )
            )
            .scalars()
            .all()
        )

    recommendations_by_ref = _recommendations_by_counterparty(recommendations_report)
    previous_by_ref = {_ref_key(row.counterparty_ref): row for row in previous_rows}
    items: list[dict[str, Any]] = []
    for current in current_rows:
        previous = previous_by_ref.get(_ref_key(current.counterparty_ref))
        if previous is None:
            continue
        old_key = _folder_key(previous.current_folder_ref, previous.current_folder_name)
        new_key = _folder_key(current.current_folder_ref, current.current_folder_name)
        if old_key == new_key:
            continue

        recommendation = recommendations_by_ref.get(_ref_key(current.counterparty_ref), {})
        item = {
            "snapshot_date": snapshot_date,
            "previous_snapshot_date": previous_snapshot_date,
            "counterparty_ref": current.counterparty_ref,
            "counterparty_name": current.counterparty_name,
            "old_folder_ref": previous.current_folder_ref,
            "old_folder_name": previous.current_folder_name,
            "new_folder_ref": current.current_folder_ref,
            "new_folder_name": current.current_folder_name,
            "current_balance": recommendation.get("current_balance"),
            "origin_document_ref": recommendation.get("origin_document_ref"),
            "origin_document_number": recommendation.get("origin_document_number"),
            "origin_document_date": recommendation.get("origin_document_date"),
            "recommended_folder_ref": recommendation.get("recommended_folder_ref"),
            "recommended_folder_name": recommendation.get("recommended_folder_name"),
            "debt_department_ref": recommendation.get("debt_department_ref"),
            "debt_department_name": recommendation.get("debt_department_name"),
        }
        if item["current_balance"] is None:
            item["current_balance"] = Decimal("0")
        items.append(item)

    items.sort(
        key=lambda item: (
            str(item.get("counterparty_name") or ""),
            str(item.get("counterparty_ref") or ""),
        )
    )
    if limit is not None:
        items = items[:limit]

    open_debt_items = [item for item in items if Decimal(str(item["current_balance"])) > 0]
    return {
        "snapshot_date": snapshot_date,
        "previous_snapshot_date": previous_snapshot_date,
        "report_revision": _build_report_revision(
            snapshot_date=snapshot_date,
            previous_snapshot_date=previous_snapshot_date,
            items=items,
        ),
        "summary": {
            "total_count": len(items),
            "current_snapshot_count": len(current_rows),
            "previous_snapshot_count": len(previous_rows),
            "open_debt_count": len(open_debt_items),
            "open_debt_amount": sum(
                (Decimal(str(item["current_balance"])) for item in open_debt_items),
                Decimal("0"),
            ),
        },
        "payload": items,
    }
