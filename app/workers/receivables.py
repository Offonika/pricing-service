from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from itertools import chain
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db import get_application_engine, get_onec_engine
from app.models import ReceivableBalanceSnapshot, ReceivableLedgerEvent, StaffMember
from app.services.receivables import (
    AuthoritativeReceivableBalanceRow,
    OneCReceivableLedgerExtractor,
    ReceivableLedgerRow,
    build_receivable_opening_import_events,
    fetch_counterparty_departments_from_onec_buyers_group,
    fetch_counterparty_refs_from_onec_group,
    fetch_current_balances_from_onec,
    fetch_employee_counterparty_refs_from_onec,
    fetch_staff_members_from_onec,
    rebuild_receivable_read_models,
    sync_receivable_ledger,
)
from app.services.receivables_extractors import (
    RECEIVABLE_DAILY_LAYER_NAMES,
    RECEIVABLE_LAYER_EMPLOYEE_MOVEMENTS,
    RECEIVABLE_LAYER_EMPLOYEE_OPENING,
    RECEIVABLE_LAYER_PAYMENTS,
    RECEIVABLE_LAYER_REGULAR_OPENING,
    RECEIVABLE_LAYER_SALES_RETURNS,
    RECEIVABLE_LAYER_SETTLEMENTS,
    RECEIVABLE_OPENING_LAYER_NAMES,
    build_receivable_layer_extractors,
)
from app.services.staffing import StaffMemberRow, upsert_staff_members

DEFAULT_RECEIVABLE_WINDOW_CHUNK_DAYS = 1
BUYERS_COUNTERPARTY_GROUP_NAME = "ПОКУПАТЕЛИ"


def _get_app_engine():
    return get_application_engine()


def _get_onec_engine():
    return get_onec_engine()


def _dispose_engine(engine) -> None:
    if engine is None:
        return
    dispose = getattr(engine, "dispose", None)
    if callable(dispose):
        dispose()


def _build_receivable_sync_windows(
    *,
    window_start: datetime | None,
    window_end: datetime | None,
    snapshot_date: date | None,
) -> list[tuple[datetime | None, datetime | None]]:
    settings = get_settings()
    effective_window_end = window_end
    if effective_window_end is None and snapshot_date is not None:
        effective_window_end = datetime.combine(snapshot_date + timedelta(days=1), time.min)

    if window_start is None or effective_window_end is None:
        return [(window_start, effective_window_end)]

    chunk_days = max(
        int(getattr(settings, "receivable_ledger_window_chunk_days", 0) or 0),
        DEFAULT_RECEIVABLE_WINDOW_CHUNK_DAYS,
    )
    chunk_size = timedelta(days=chunk_days)
    if effective_window_end - window_start <= chunk_size:
        return [(window_start, effective_window_end)]

    windows: list[tuple[datetime, datetime]] = []
    current_start = window_start
    while current_start < effective_window_end:
        current_end = min(current_start + chunk_size, effective_window_end)
        windows.append((current_start, current_end))
        current_start = current_end
    return windows


def _snapshot_window(snapshot_date: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(snapshot_date, time.min),
        datetime.combine(snapshot_date + timedelta(days=1), time.min),
    )


def _snapshot_window_with_lookback(
    snapshot_date: date, *, window_days: int = 1
) -> tuple[datetime, datetime]:
    normalized_window_days = max(int(window_days or 1), 1)
    return (
        datetime.combine(snapshot_date - timedelta(days=normalized_window_days - 1), time.min),
        datetime.combine(snapshot_date + timedelta(days=1), time.min),
    )


def _normalize_counterparty_match_key(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).replace("\xa0", " ").split()).strip()
    if not cleaned:
        return None
    return " ".join(cleaned.casefold().split())


def _load_local_employee_counterparty_refs(app_engine) -> tuple[str, ...]:
    with Session(app_engine) as session:
        rows = (
            session.query(StaffMember.store_ref)
            .filter(StaffMember.store_ref.is_not(None))
            .distinct()
            .order_by(StaffMember.store_ref)
            .all()
        )
    return tuple(item[0] for item in rows if item[0])


def _load_local_buyer_counterparty_refs(
    app_engine,
    *,
    employee_counterparty_refs: Sequence[str] = (),
) -> tuple[str, ...]:
    employee_ref_set = {ref.casefold() for ref in employee_counterparty_refs if ref}
    with Session(app_engine) as session:
        rows = (
            session.query(ReceivableLedgerEvent.counterparty_ref)
            .filter(ReceivableLedgerEvent.counterparty_ref.is_not(None))
            .filter(ReceivableLedgerEvent.contract_kind_name == "С покупателем")
            .distinct()
            .order_by(ReceivableLedgerEvent.counterparty_ref)
            .all()
        )
    return tuple(item[0] for item in rows if item[0] and item[0].casefold() not in employee_ref_set)


def _load_local_staff_rows(app_engine) -> list[StaffMemberRow]:
    with Session(app_engine) as session:
        items = (
            session.query(StaffMember)
            .order_by(
                StaffMember.employment_status,
                StaffMember.department_name,
                StaffMember.full_name,
            )
            .all()
        )
    return [
        StaffMemberRow(
            source=item.source,
            external_ref=item.external_ref,
            full_name=item.full_name,
            role_code=item.role_code,
            role_name=item.role_name,
            department_ref=item.department_ref,
            department_name=item.department_name,
            store_ref=item.store_ref,
            store_name=item.store_name,
            employment_status=item.employment_status,
            hire_date=item.hire_date,
            termination_date=item.termination_date,
            manager_ref=item.manager_ref,
            manager_name=item.manager_name,
        )
        for item in items
    ]


def _load_local_counterparty_ref_mapping(
    app_engine,
    *,
    employee_counterparty_refs: Sequence[str],
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    employee_ref_set = {ref.casefold() for ref in employee_counterparty_refs if ref}
    buyer_mapping: dict[str, dict[str, str]] = {}
    employee_mapping: dict[str, dict[str, str]] = {}

    with Session(app_engine) as session:
        ledger_rows = (
            session.query(
                ReceivableLedgerEvent.counterparty_ref,
                ReceivableLedgerEvent.counterparty_name,
                ReceivableLedgerEvent.external_document_date,
                ReceivableLedgerEvent.id,
            )
            .filter(ReceivableLedgerEvent.counterparty_ref.is_not(None))
            .order_by(
                ReceivableLedgerEvent.external_document_date.desc(),
                ReceivableLedgerEvent.id.desc(),
            )
            .all()
        )
        staff_rows = (
            session.query(StaffMember.store_ref, StaffMember.store_name)
            .filter(StaffMember.store_ref.is_not(None))
            .order_by(StaffMember.updated_at.desc(), StaffMember.id.desc())
            .all()
        )

    def register(
        mapping: dict[str, dict[str, str]],
        *,
        counterparty_ref: str | None,
        counterparty_name: str | None,
    ) -> None:
        key = _normalize_counterparty_match_key(counterparty_name)
        if key is None or not counterparty_ref or not counterparty_name:
            return
        mapping.setdefault(
            key,
            {
                "counterparty_ref": counterparty_ref,
                "counterparty_name": counterparty_name,
            },
        )

    for row in ledger_rows:
        target = (
            employee_mapping
            if row.counterparty_ref.casefold() in employee_ref_set
            else buyer_mapping
        )
        register(
            target,
            counterparty_ref=row.counterparty_ref,
            counterparty_name=row.counterparty_name,
        )

    for store_ref, store_name in staff_rows:
        register(
            employee_mapping,
            counterparty_ref=store_ref,
            counterparty_name=store_name,
        )

    return buyer_mapping, employee_mapping


def _resolve_employee_counterparty_refs(
    onec_engine,
    *,
    app_engine=None,
    employee_counterparty_refs: Sequence[str] = (),
) -> tuple[str, ...]:
    if employee_counterparty_refs:
        return tuple(employee_counterparty_refs)
    if app_engine is not None:
        local_refs = _load_local_employee_counterparty_refs(app_engine)
        if local_refs:
            return local_refs
    return fetch_employee_counterparty_refs_from_onec(onec_engine)


def _resolve_buyer_counterparty_refs(
    onec_engine,
    *,
    app_engine=None,
    employee_counterparty_refs: Sequence[str] = (),
) -> tuple[str, ...]:
    refs = fetch_counterparty_refs_from_onec_group(
        onec_engine,
        group_name=BUYERS_COUNTERPARTY_GROUP_NAME,
    )
    if refs:
        return tuple(refs)
    if app_engine is not None:
        local_refs = _load_local_buyer_counterparty_refs(
            app_engine,
            employee_counterparty_refs=employee_counterparty_refs,
        )
        if local_refs:
            return local_refs
    return ()


def _resolve_buyer_counterparty_departments(
    onec_engine,
    *,
    buyers_group_name: str = BUYERS_COUNTERPARTY_GROUP_NAME,
) -> dict[str, Any]:
    try:
        return fetch_counterparty_departments_from_onec_buyers_group(
            onec_engine,
            buyers_group_name=buyers_group_name,
        )
    except Exception as exc:
        print(
            (
                "[receivables] buyer department preload skipped "
                f"buyers_group={buyers_group_name!r} error={exc}"
            ),
            flush=True,
        )
        return {}


def _resolve_staff_rows(
    onec_engine,
    *,
    app_engine=None,
    staff_rows: Sequence[StaffMemberRow] | None = None,
) -> list[StaffMemberRow]:
    if staff_rows is not None:
        return list(staff_rows)
    if app_engine is not None:
        local_rows = _load_local_staff_rows(app_engine)
        if local_rows:
            return local_rows
    return [
        StaffMemberRow.from_mapping(item, default_source="onec_physical_person")
        for item in fetch_staff_members_from_onec(onec_engine)
    ]


def _merge_authoritative_balance_rows(
    rows: Iterable[AuthoritativeReceivableBalanceRow],
) -> list[AuthoritativeReceivableBalanceRow]:
    items: dict[str, AuthoritativeReceivableBalanceRow] = {}
    for row in rows:
        if not row.counterparty_ref:
            continue
        current = items.get(row.counterparty_ref)
        if current is None:
            items[row.counterparty_ref] = AuthoritativeReceivableBalanceRow(
                counterparty_ref=row.counterparty_ref,
                counterparty_code=row.counterparty_code,
                counterparty_name=row.counterparty_name,
                current_balance=Decimal(str(row.current_balance)),
                current_manager_ref=row.current_manager_ref,
                current_manager_name=row.current_manager_name,
                source=row.source,
            )
            continue
        items[row.counterparty_ref] = AuthoritativeReceivableBalanceRow(
            counterparty_ref=row.counterparty_ref,
            counterparty_code=row.counterparty_code or current.counterparty_code,
            counterparty_name=row.counterparty_name or current.counterparty_name,
            current_balance=Decimal(str(current.current_balance))
            + Decimal(str(row.current_balance)),
            current_manager_ref=row.current_manager_ref or current.current_manager_ref,
            current_manager_name=row.current_manager_name or current.current_manager_name,
            source=current.source,
        )

    return sorted(
        (row for row in items.values() if Decimal(str(row.current_balance)) != 0),
        key=lambda item: (item.counterparty_name or "", item.counterparty_ref),
    )


def _opening_import_balance_rows(
    onec_engine,
    *,
    opening_import_path: str,
) -> list[AuthoritativeReceivableBalanceRow]:
    rows: list[AuthoritativeReceivableBalanceRow] = []
    for event in build_receivable_opening_import_events(
        onec_engine,
        report_path=Path(opening_import_path),
    ):
        rows.append(
            AuthoritativeReceivableBalanceRow(
                counterparty_ref=event.counterparty_ref,
                counterparty_name=event.counterparty_name,
                current_balance=Decimal(str(event.amount_delta)),
                source="onec_opening_import_balance",
            )
        )
    return _merge_authoritative_balance_rows(rows)


def _synthetic_counterparty_ref_count(rows: Iterable[AuthoritativeReceivableBalanceRow]) -> int:
    return sum(1 for row in rows if str(row.counterparty_ref).startswith("synthetic:"))


def _synthetic_event_counterparty_refs(events: Iterable[ReceivableLedgerRow]) -> tuple[str, ...]:
    refs = sorted(
        {
            str(event.counterparty_ref)
            for event in events
            if str(event.counterparty_ref).startswith("synthetic:")
        }
    )
    return tuple(refs)


def _raise_on_synthetic_event_counterparties(events: Sequence[ReceivableLedgerRow]) -> None:
    synthetic_refs = _synthetic_event_counterparty_refs(events)
    if synthetic_refs:
        sample = ", ".join(synthetic_refs[:5])
        raise ValueError(
            "opening seed содержит synthetic counterparty refs; "
            f"нужно сначала исправить mapping 1С. Примеры: {sample}"
        )


def _opening_snapshot_balance_rows(
    app_engine,
    *,
    snapshot_date: date,
) -> list[AuthoritativeReceivableBalanceRow]:
    rows: list[AuthoritativeReceivableBalanceRow] = []
    with Session(app_engine) as session:
        items = (
            session.query(ReceivableBalanceSnapshot)
            .filter(ReceivableBalanceSnapshot.snapshot_date == snapshot_date)
            .order_by(
                ReceivableBalanceSnapshot.counterparty_name,
                ReceivableBalanceSnapshot.counterparty_ref,
            )
            .all()
        )
    for item in items:
        rows.append(
            AuthoritativeReceivableBalanceRow(
                counterparty_ref=item.counterparty_ref,
                counterparty_code=item.counterparty_code,
                counterparty_name=item.counterparty_name,
                current_balance=Decimal(str(item.current_balance)),
                current_manager_ref=item.current_manager_ref,
                current_manager_name=item.current_manager_name,
                source="authoritative_snapshot_seed",
            )
        )
    return _merge_authoritative_balance_rows(rows)


def _projection_bool(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return value != 0
    if isinstance(value, (int, float)):
        return value != 0
    normalized = str(value).strip().casefold()
    return normalized not in {"", "0", "0.0", "false", "none"}


def _projection_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _projection_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if value is None:
        return datetime.min
    text_value = str(value).strip()
    if not text_value:
        return datetime.min
    normalized = text_value.replace("T", " ")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.min


def _projection_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _projection_row_order_key(row: Any) -> tuple[datetime, int, str]:
    return (
        _projection_datetime(row.get("external_document_date")),
        _projection_int(row.get("line_no")),
        str(row.get("external_document_ref") or ""),
    )


def _project_authoritative_balance_rows_from_onec(
    onec_engine,
    *,
    operations_sql: str,
    snapshot_date: date,
    opening_balance_date: date,
    include_sql_opening: bool,
) -> list[AuthoritativeReceivableBalanceRow]:
    normalized_sql = operations_sql.strip()
    while normalized_sql.endswith(";"):
        normalized_sql = normalized_sql[:-1].rstrip()
    if not normalized_sql:
        return []

    stmt = text(normalized_sql)

    params = {
        "window_start": datetime.combine(opening_balance_date, time.min),
        "window_end": datetime.combine(snapshot_date + timedelta(days=1), time.min),
        "opening_balance_date": opening_balance_date if include_sql_opening else None,
    }

    balances: dict[str, Decimal] = {}
    latest_counterparty_codes: dict[str, tuple[tuple[datetime, int, str], str]] = {}
    latest_counterparty_names: dict[str, tuple[tuple[datetime, int, str], str]] = {}
    latest_managers: dict[str, tuple[tuple[datetime, int, str], str | None, str | None]] = {}

    with onec_engine.connect() as conn:
        result_rows = conn.execute(stmt, params).mappings()
        for row in result_rows:
            if _projection_bool(row.get("skip_ingest")):
                continue

            counterparty_ref = _projection_string(row.get("counterparty_ref"))
            if counterparty_ref is None:
                continue

            balances[counterparty_ref] = balances.get(counterparty_ref, Decimal("0.00")) + Decimal(
                str(row.get("amount_delta") or 0)
            )

            order_key = _projection_row_order_key(row)

            counterparty_code = _projection_string(row.get("counterparty_code"))
            if counterparty_code is not None:
                current_code = latest_counterparty_codes.get(counterparty_ref)
                if current_code is None or order_key >= current_code[0]:
                    latest_counterparty_codes[counterparty_ref] = (order_key, counterparty_code)

            counterparty_name = _projection_string(row.get("counterparty_name"))
            if counterparty_name is not None:
                current_name = latest_counterparty_names.get(counterparty_ref)
                if current_name is None or order_key >= current_name[0]:
                    latest_counterparty_names[counterparty_ref] = (order_key, counterparty_name)

            manager_ref = _projection_string(row.get("manager_ref"))
            manager_name = _projection_string(row.get("manager_name"))
            if manager_ref is not None or manager_name is not None:
                current_manager = latest_managers.get(counterparty_ref)
                if current_manager is None or order_key >= current_manager[0]:
                    latest_managers[counterparty_ref] = (order_key, manager_ref, manager_name)

    rows: list[AuthoritativeReceivableBalanceRow] = []
    for counterparty_ref, balance in balances.items():
        if balance == 0:
            continue
        latest_name = latest_counterparty_names.get(counterparty_ref)
        latest_code = latest_counterparty_codes.get(counterparty_ref)
        latest_manager = latest_managers.get(counterparty_ref)
        rows.append(
            AuthoritativeReceivableBalanceRow(
                counterparty_ref=counterparty_ref,
                counterparty_code=latest_code[1] if latest_code is not None else None,
                counterparty_name=latest_name[1] if latest_name is not None else None,
                current_balance=balance,
                current_manager_ref=latest_manager[1] if latest_manager is not None else None,
                current_manager_name=latest_manager[2] if latest_manager is not None else None,
                source="onec_authoritative_projection",
            )
        )

    return sorted(rows, key=lambda item: (item.counterparty_name or "", item.counterparty_ref))


def _upsert_staff_rows(app_engine, staff_rows: Sequence[StaffMemberRow]) -> dict[str, int]:
    with Session(app_engine) as staff_session:
        result = upsert_staff_members(staff_session, staff_rows)
        staff_session.commit()
    return result


def _validate_seeded_receivable_ledger_ready(
    app_engine,
    *,
    required_opening_balance_date: date = date(2025, 1, 1),
) -> dict[str, Any]:
    with Session(app_engine) as session:
        row = session.execute(text("""
                SELECT
                    COUNT(*) AS ledger_row_count,
                    MIN(external_document_date)::date AS min_event_date,
                    MAX(external_document_date)::date AS max_event_date,
                    SUM(
                        CASE
                            WHEN source_layer = 'opening_import_1c'
                              OR source = 'onec_opening_import'
                            THEN 1 ELSE 0
                        END
                    ) AS opening_import_row_count
                FROM receivable_ledger_event
            """)).mappings().one()

    ledger_row_count = int(row["ledger_row_count"] or 0)
    opening_import_row_count = int(row["opening_import_row_count"] or 0)
    min_event_date = row["min_event_date"]
    if (
        ledger_row_count == 0
        or opening_import_row_count == 0
        or min_event_date != required_opening_balance_date
    ):
        raise RuntimeError(
            "receivable ledger не готов для production rebuild: "
            f"rows={ledger_row_count}, min_event_date={min_event_date}, "
            f"opening_import_rows={opening_import_row_count}. "
            "Сначала загрузите seed 2025-01-01 и движения 1С в receivable_ledger_event."
        )

    return {
        "ledger_row_count": ledger_row_count,
        "ledger_min_event_date": min_event_date,
        "ledger_max_event_date": row["max_event_date"],
        "opening_import_event_count": opening_import_row_count,
    }


def _resolve_authoritative_balance_rows(
    *,
    onec_engine,
    app_engine=None,
    snapshot_date: date,
    employee_counterparty_refs: Sequence[str],
    operations_sql: str | None,
    opening_balance_date: date | None,
    opening_import_path: str | None,
    opening_snapshot_date: date | None,
    current_import_path: str | None,
    current_import_counterparty_group: str | None,
    employee_current_import_path: str | None,
    employee_current_import_counterparty_group: str | None,
) -> tuple[list[AuthoritativeReceivableBalanceRow] | None, dict[str, Any]]:
    if opening_snapshot_date is not None:
        raise ValueError(
            "opening_snapshot_date больше не поддерживается в primary balance path: "
            "authoritative остатки строятся из receivable_ledger_event."
        )
    if current_import_path or employee_current_import_path:
        raise ValueError(
            "current_import_path/employee_current_import_path больше не поддерживаются "
            "в production balance path: Excel используется только для сверки."
        )
    if operations_sql or opening_import_path or opening_balance_date is not None:
        raise ValueError(
            "read-model rebuild больше не принимает opening/projection параметры: "
            "сначала загрузите seed и движения в receivable_ledger_event, "
            "затем пересоберите snapshot из ledger."
        )

    _ = (
        app_engine,
        current_import_counterparty_group,
        employee_current_import_counterparty_group,
    )
    return fetch_current_balances_from_onec(
        onec_engine,
        snapshot_date=snapshot_date,
        employee_counterparty_refs=employee_counterparty_refs,
    )


def _layered_sync_result_template() -> dict[str, Any]:
    return {
        "processed": 0,
        "inserted": 0,
        "updated": 0,
        "existing": 0,
        "assignments": 0,
        "snapshots": 0,
        "reconciliation_snapshots": 0,
        "cases": 0,
        "case_segments": {},
        "layers": {},
        "reset": {
            "ledger_events_deleted": 0,
            "manager_assignments_deleted": 0,
            "snapshots_deleted": 0,
            "reconciliation_snapshots_deleted": 0,
            "cases_deleted": 0,
        },
    }


def _merge_sync_result(target: dict[str, Any], chunk_result: dict[str, Any]) -> None:
    target["processed"] += chunk_result.get("processed", 0)
    target["inserted"] += chunk_result.get("inserted", 0)
    target["updated"] += chunk_result.get("updated", 0)
    target["existing"] += chunk_result.get("existing", 0)
    target["assignments"] = chunk_result.get("assignments", target.get("assignments", 0))
    target["snapshots"] = chunk_result.get("snapshots", target.get("snapshots", 0))
    target["reconciliation_snapshots"] = chunk_result.get(
        "reconciliation_snapshots",
        target.get("reconciliation_snapshots", 0),
    )
    target["cases"] = chunk_result.get("cases", target.get("cases", 0))
    target["case_segments"] = chunk_result.get("case_segments", target.get("case_segments", {}))


def _sync_event_layer(
    *,
    app_engine,
    extractor: OneCReceivableLedgerExtractor,
    layer_name: str,
    windows: Sequence[tuple[datetime | None, datetime | None]],
    employee_counterparty_refs: Sequence[str] = (),
    opening_balance_date: date | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    layer_result = {
        "processed": 0,
        "inserted": 0,
        "updated": 0,
        "existing": 0,
        "window_count": len(windows),
        "elapsed_seconds": 0.0,
    }
    started_at = monotonic()
    employee_counterparty_refs_cf = {
        ref.casefold() for ref in employee_counterparty_refs if isinstance(ref, str) and ref
    }

    for index, (window_start, window_end) in enumerate(windows):
        window_started_at = monotonic()
        print(
            (
                f"[receivables] layer={layer_name} "
                f"window={index + 1}/{len(windows)} "
                f"start={window_start} end={window_end} "
                f"opening_balance_date={opening_balance_date}"
            ),
            flush=True,
        )
        raw_events = extractor.iter_receivable_events(
            window_start=window_start,
            window_end=window_end,
            opening_balance_date=opening_balance_date,
        )
        if employee_counterparty_refs_cf:
            if layer_name in {
                RECEIVABLE_LAYER_REGULAR_OPENING,
                RECEIVABLE_LAYER_SALES_RETURNS,
                RECEIVABLE_LAYER_PAYMENTS,
                RECEIVABLE_LAYER_SETTLEMENTS,
            }:
                events = (
                    event
                    for event in raw_events
                    if event.counterparty_ref.casefold() not in employee_counterparty_refs_cf
                )
            elif layer_name in {
                RECEIVABLE_LAYER_EMPLOYEE_OPENING,
                RECEIVABLE_LAYER_EMPLOYEE_MOVEMENTS,
            }:
                events = (
                    event
                    for event in raw_events
                    if event.counterparty_ref.casefold() in employee_counterparty_refs_cf
                )
            else:
                events = raw_events
        else:
            events = raw_events
        with Session(app_engine) as session:
            chunk_result = sync_receivable_ledger(
                session,
                events,
                replace_existing=replace_existing and index == 0,
                rebuild_read_models=False,
            )
            session.commit()
        _merge_sync_result(layer_result, chunk_result)
        print(
            (
                f"[receivables] layer={layer_name} "
                f"window={index + 1}/{len(windows)} done "
                f"processed={chunk_result['processed']} "
                f"inserted={chunk_result['inserted']} "
                f"updated={chunk_result['updated']} "
                f"existing={chunk_result['existing']} "
                f"sec={monotonic() - window_started_at:.1f}"
            ),
            flush=True,
        )

    layer_result["elapsed_seconds"] = round(monotonic() - started_at, 3)
    return layer_result


def _sync_event_sequence(
    *,
    app_engine,
    layers: Sequence[tuple[str, Iterable[ReceivableLedgerRow]]],
    replace_existing: bool = False,
) -> dict[str, Any]:
    aggregate_result = _layered_sync_result_template()

    for index, (layer_name, events) in enumerate(layers):
        started_at = monotonic()
        print(f"[receivables] layer={layer_name} import start", flush=True)
        with Session(app_engine) as session:
            chunk_result = sync_receivable_ledger(
                session,
                events,
                replace_existing=replace_existing and index == 0,
                rebuild_read_models=False,
            )
            session.commit()
        _merge_sync_result(aggregate_result, chunk_result)
        if index == 0:
            aggregate_result["reset"] = chunk_result["reset"]
        aggregate_result["layers"][layer_name] = {
            "processed": chunk_result["processed"],
            "inserted": chunk_result["inserted"],
            "updated": chunk_result["updated"],
            "existing": chunk_result["existing"],
            "window_count": 1,
            "elapsed_seconds": round(monotonic() - started_at, 3),
        }
        print(
            (
                f"[receivables] layer={layer_name} import done "
                f"processed={chunk_result['processed']} "
                f"inserted={chunk_result['inserted']} "
                f"updated={chunk_result['updated']} "
                f"existing={chunk_result['existing']} "
                f"sec={monotonic() - started_at:.1f}"
            ),
            flush=True,
        )
    return aggregate_result


def run_receivable_opening_sync(
    *,
    opening_balance_date: date,
    opening_import_path: str | None = None,
    employee_counterparty_refs: tuple[str, ...] = (),
    replace_existing: bool = False,
    layer_names: Sequence[str] | None = RECEIVABLE_OPENING_LAYER_NAMES,
    onec_engine=None,
    app_engine=None,
) -> dict[str, Any]:
    sync_started_at = monotonic()
    resolved_onec_engine = onec_engine or _get_onec_engine()
    resolved_app_engine = app_engine or _get_app_engine()
    resolved_employee_refs = _resolve_employee_counterparty_refs(
        resolved_onec_engine,
        app_engine=resolved_app_engine,
        employee_counterparty_refs=employee_counterparty_refs,
    )
    layer_extractors = build_receivable_layer_extractors(resolved_onec_engine)

    aggregate_result = _layered_sync_result_template()
    aggregate_result["employee_counterparty_ref_count"] = len(resolved_employee_refs)

    extra_layers: list[tuple[str, Iterable[ReceivableLedgerRow]]] = []
    opening_import_events: list[ReceivableLedgerRow] = []
    if opening_import_path:
        opening_import_events = build_receivable_opening_import_events(
            resolved_onec_engine,
            report_path=Path(opening_import_path),
        )
        _raise_on_synthetic_event_counterparties(opening_import_events)
        extra_layers.append(("opening_import_1c", opening_import_events))
    aggregate_result["opening_import_event_count"] = len(opening_import_events)

    if extra_layers:
        imported_result = _sync_event_sequence(
            app_engine=resolved_app_engine,
            layers=extra_layers,
            replace_existing=replace_existing,
        )
        _merge_sync_result(aggregate_result, imported_result)
        aggregate_result["layers"].update(imported_result["layers"])
        aggregate_result["reset"] = imported_result["reset"]

    replace_existing_used = bool(extra_layers)
    effective_layer_names = (
        () if opening_import_path else tuple(layer_names or RECEIVABLE_OPENING_LAYER_NAMES)
    )
    for layer_name in effective_layer_names:
        extractor = layer_extractors[layer_name]
        layer_result = _sync_event_layer(
            app_engine=resolved_app_engine,
            extractor=extractor,
            layer_name=layer_name,
            windows=[(None, None)],
            employee_counterparty_refs=resolved_employee_refs,
            opening_balance_date=opening_balance_date,
            replace_existing=replace_existing and not replace_existing_used,
        )
        replace_existing_used = True
        _merge_sync_result(aggregate_result, layer_result)
        aggregate_result["layers"][layer_name] = layer_result

    aggregate_result["sync_elapsed_seconds"] = round(monotonic() - sync_started_at, 3)
    return aggregate_result


def run_receivable_daily_events_sync(
    *,
    snapshot_date: date | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    window_days: int = 1,
    employee_counterparty_refs: tuple[str, ...] = (),
    replace_existing: bool = False,
    layer_names: Sequence[str] | None = RECEIVABLE_DAILY_LAYER_NAMES,
    onec_engine=None,
    app_engine=None,
) -> dict[str, Any]:
    sync_started_at = monotonic()
    owns_onec_engine = onec_engine is None
    owns_app_engine = app_engine is None
    resolved_onec_engine = onec_engine or _get_onec_engine()
    resolved_app_engine = app_engine or _get_app_engine()
    try:
        resolved_employee_refs = _resolve_employee_counterparty_refs(
            resolved_onec_engine,
            app_engine=resolved_app_engine,
            employee_counterparty_refs=employee_counterparty_refs,
        )

        effective_window_start = window_start
        effective_window_end = window_end
        if (
            snapshot_date is not None
            and effective_window_start is None
            and effective_window_end is None
        ):
            effective_window_start, effective_window_end = _snapshot_window_with_lookback(
                snapshot_date,
                window_days=window_days,
            )

        sync_windows = _build_receivable_sync_windows(
            window_start=effective_window_start,
            window_end=effective_window_end,
            snapshot_date=snapshot_date,
        )
        layer_extractors = build_receivable_layer_extractors(resolved_onec_engine)
        aggregate_result = _layered_sync_result_template()
        aggregate_result["employee_counterparty_ref_count"] = len(resolved_employee_refs)
        aggregate_result["sync_window_count"] = len(sync_windows)

        effective_layer_names = tuple(layer_names or RECEIVABLE_DAILY_LAYER_NAMES)
        for index, layer_name in enumerate(effective_layer_names):
            layer_result = _sync_event_layer(
                app_engine=resolved_app_engine,
                extractor=layer_extractors[layer_name],
                layer_name=layer_name,
                windows=sync_windows,
                employee_counterparty_refs=resolved_employee_refs,
                replace_existing=replace_existing and index == 0,
            )
            _merge_sync_result(aggregate_result, layer_result)
            aggregate_result["layers"][layer_name] = layer_result

        aggregate_result["sync_elapsed_seconds"] = round(monotonic() - sync_started_at, 3)
        return aggregate_result
    finally:
        if owns_onec_engine:
            _dispose_engine(resolved_onec_engine)
        if owns_app_engine:
            _dispose_engine(resolved_app_engine)


def run_receivable_read_model_rebuild(
    *,
    snapshot_date: date,
    operations_sql: str | None = None,
    opening_balance_date: date | None = None,
    opening_import_path: str | None = None,
    opening_snapshot_date: date | None = None,
    current_import_path: str | None = None,
    current_import_counterparty_group: str | None = "ПОКУПАТЕЛИ",
    employee_counterparty_refs: tuple[str, ...] = (),
    employee_current_import_path: str | None = None,
    employee_current_import_counterparty_group: str | None = "СОТРУДНИКИ",
    fired_manager_refs: tuple[str, ...] = (),
    staff_rows: Sequence[StaffMemberRow] | None = None,
    require_seeded_ledger: bool = False,
    onec_engine=None,
    app_engine=None,
) -> dict[str, Any]:
    started_at = monotonic()
    owns_onec_engine = onec_engine is None
    owns_app_engine = app_engine is None
    resolved_onec_engine = onec_engine or _get_onec_engine()
    resolved_app_engine = app_engine or _get_app_engine()
    try:
        print(
            f"[receivables] read-model rebuild snapshot_date={snapshot_date.isoformat()} phase=preload",
            flush=True,
        )
        resolved_employee_refs = _resolve_employee_counterparty_refs(
            resolved_onec_engine,
            app_engine=resolved_app_engine,
            employee_counterparty_refs=employee_counterparty_refs,
        )
        resolved_buyer_refs = _resolve_buyer_counterparty_refs(
            resolved_onec_engine,
            app_engine=resolved_app_engine,
            employee_counterparty_refs=resolved_employee_refs,
        )
        resolved_buyer_departments = _resolve_buyer_counterparty_departments(
            resolved_onec_engine,
            buyers_group_name=BUYERS_COUNTERPARTY_GROUP_NAME,
        )
        resolved_staff_rows = _resolve_staff_rows(
            resolved_onec_engine,
            app_engine=resolved_app_engine,
            staff_rows=staff_rows,
        )
        print(
            (
                "[receivables] read-model rebuild phase=staff_upsert "
                f"employee_refs={len(resolved_employee_refs)} "
                f"buyer_refs={len(resolved_buyer_refs)} "
                f"buyer_departments={len(resolved_buyer_departments)} "
                f"staff_rows={len(resolved_staff_rows)}"
            ),
            flush=True,
        )
        staff_result = _upsert_staff_rows(resolved_app_engine, resolved_staff_rows)
        ledger_ready_meta = (
            _validate_seeded_receivable_ledger_ready(resolved_app_engine)
            if require_seeded_ledger
            else {}
        )
        print("[receivables] read-model rebuild phase=authoritative_balance", flush=True)
        authoritative_balance_rows, authoritative_meta = _resolve_authoritative_balance_rows(
            onec_engine=resolved_onec_engine,
            app_engine=resolved_app_engine,
            snapshot_date=snapshot_date,
            employee_counterparty_refs=resolved_employee_refs,
            operations_sql=operations_sql,
            opening_balance_date=opening_balance_date,
            opening_import_path=opening_import_path,
            opening_snapshot_date=opening_snapshot_date,
            current_import_path=current_import_path,
            current_import_counterparty_group=current_import_counterparty_group,
            employee_current_import_path=employee_current_import_path,
            employee_current_import_counterparty_group=employee_current_import_counterparty_group,
        )

        print(
            (
                "[receivables] read-model rebuild phase=snapshot_build "
                f"authoritative_rows={len(authoritative_balance_rows or [])}"
            ),
            flush=True,
        )
        with Session(resolved_app_engine) as session:
            result = rebuild_receivable_read_models(
                session,
                snapshot_date=snapshot_date,
                authoritative_balance_rows=authoritative_balance_rows,
                authoritative_opening_balance_dates=authoritative_meta.get("opening_balance_dates"),
                employee_counterparty_refs=resolved_employee_refs,
                counterparty_departments_by_ref=resolved_buyer_departments,
                buyer_counterparty_refs=resolved_buyer_refs,
                fired_manager_refs=fired_manager_refs,
            )
            print("[receivables] read-model rebuild phase=commit", flush=True)
            session.commit()

        result["staff_members"] = staff_result
        result["staff_member_payload_count"] = len(resolved_staff_rows)
        result["employee_counterparty_ref_count"] = len(resolved_employee_refs)
        result["buyer_counterparty_ref_count"] = len(resolved_buyer_refs)
        result["buyer_counterparty_department_count"] = len(resolved_buyer_departments)
        result["sync_elapsed_seconds"] = round(monotonic() - started_at, 3)
        result.update(ledger_ready_meta)
        result.update(authoritative_meta)
        print(
            (
                "[receivables] read-model rebuild done "
                f"snapshot_date={snapshot_date.isoformat()} "
                f"sec={result['sync_elapsed_seconds']}"
            ),
            flush=True,
        )
        return result
    finally:
        if owns_onec_engine:
            _dispose_engine(resolved_onec_engine)
        if owns_app_engine:
            _dispose_engine(resolved_app_engine)


def run_receivable_history_backfill(
    *,
    date_from: date,
    date_to: date,
    operations_sql: str | None = None,
    opening_balance_date: date | None = None,
    opening_import_path: str | None = None,
    opening_snapshot_date: date | None = None,
    rebuild_snapshot_dates: Sequence[date] = (),
    current_import_path: str | None = None,
    current_import_counterparty_group: str | None = "ПОКУПАТЕЛИ",
    employee_counterparty_refs: tuple[str, ...] = (),
    employee_current_import_path: str | None = None,
    employee_current_import_counterparty_group: str | None = "СОТРУДНИКИ",
    daily_layer_names: Sequence[str] | None = RECEIVABLE_DAILY_LAYER_NAMES,
    fired_manager_refs: tuple[str, ...] = (),
    replace_existing: bool = False,
    onec_engine=None,
    app_engine=None,
) -> dict[str, Any]:
    if date_to < date_from:
        raise ValueError("date_to must be greater than or equal to date_from")
    if opening_snapshot_date is not None:
        raise ValueError(
            "opening_snapshot_date больше не поддерживается в history backfill: "
            "authoritative snapshot должен строиться только из 1С."
        )
    if current_import_path or employee_current_import_path:
        raise ValueError(
            "current_import_path/employee_current_import_path больше не поддерживаются "
            "в history backfill: Excel используется только для сверки."
        )

    started_at = monotonic()
    owns_onec_engine = onec_engine is None
    owns_app_engine = app_engine is None
    resolved_onec_engine = onec_engine or _get_onec_engine()
    resolved_app_engine = app_engine or _get_app_engine()
    try:
        print(
            (
                "[receivables] history backfill start "
                f"date_from={date_from.isoformat()} date_to={date_to.isoformat()}"
            ),
            flush=True,
        )
        resolved_employee_refs = _resolve_employee_counterparty_refs(
            resolved_onec_engine,
            app_engine=resolved_app_engine,
            employee_counterparty_refs=employee_counterparty_refs,
        )
        resolved_staff_rows = _resolve_staff_rows(
            resolved_onec_engine,
            app_engine=resolved_app_engine,
        )

        result: dict[str, Any] = {
            "processed": 0,
            "inserted": 0,
            "updated": 0,
            "existing": 0,
            "opening": None,
            "daily": {},
            "rebuilds": {},
            "employee_counterparty_ref_count": len(resolved_employee_refs),
            "staff_member_payload_count": len(resolved_staff_rows),
        }

        replace_existing_pending = replace_existing
        if opening_balance_date is not None or opening_import_path:
            if opening_balance_date is None:
                raise ValueError("opening_import_path requires opening_balance_date")
            print(
                (
                    "[receivables] history backfill phase=opening_sync "
                    f"opening_balance_date={opening_balance_date.isoformat()}"
                ),
                flush=True,
            )
            opening_result = run_receivable_opening_sync(
                opening_balance_date=opening_balance_date,
                opening_import_path=opening_import_path,
                employee_counterparty_refs=resolved_employee_refs,
                replace_existing=replace_existing_pending,
                onec_engine=resolved_onec_engine,
                app_engine=resolved_app_engine,
            )
            result["opening"] = opening_result
            result["processed"] += opening_result["processed"]
            result["inserted"] += opening_result["inserted"]
            result["updated"] += opening_result["updated"]
            result["existing"] += opening_result["existing"]
            replace_existing_pending = False

        current_date = date_from
        while current_date <= date_to:
            print(
                (
                    "[receivables] history backfill phase=daily_sync "
                    f"snapshot_date={current_date.isoformat()}"
                ),
                flush=True,
            )
            day_result = run_receivable_daily_events_sync(
                snapshot_date=current_date,
                employee_counterparty_refs=resolved_employee_refs,
                layer_names=daily_layer_names,
                replace_existing=replace_existing_pending,
                onec_engine=resolved_onec_engine,
                app_engine=resolved_app_engine,
            )
            result["daily"][current_date.isoformat()] = day_result
            result["processed"] += day_result["processed"]
            result["inserted"] += day_result["inserted"]
            result["updated"] += day_result["updated"]
            result["existing"] += day_result["existing"]
            replace_existing_pending = False
            current_date += timedelta(days=1)

        for rebuild_snapshot_date in rebuild_snapshot_dates:
            print(
                (
                    "[receivables] history backfill phase=read_model_rebuild "
                    f"snapshot_date={rebuild_snapshot_date.isoformat()}"
                ),
                flush=True,
            )
            rebuild_result = run_receivable_read_model_rebuild(
                snapshot_date=rebuild_snapshot_date,
                operations_sql=None,
                opening_balance_date=None,
                opening_import_path=None,
                opening_snapshot_date=None,
                current_import_path=None,
                current_import_counterparty_group=current_import_counterparty_group,
                employee_counterparty_refs=resolved_employee_refs,
                employee_current_import_path=None,
                employee_current_import_counterparty_group=employee_current_import_counterparty_group,
                fired_manager_refs=fired_manager_refs,
                staff_rows=resolved_staff_rows,
                require_seeded_ledger=bool(opening_import_path),
                onec_engine=resolved_onec_engine,
                app_engine=resolved_app_engine,
            )
            result["rebuilds"][rebuild_snapshot_date.isoformat()] = rebuild_result

        result["sync_elapsed_seconds"] = round(monotonic() - started_at, 3)
        print(
            (
                "[receivables] history backfill done "
                f"processed={result['processed']} sec={result['sync_elapsed_seconds']}"
            ),
            flush=True,
        )
        return result
    finally:
        if owns_onec_engine:
            _dispose_engine(resolved_onec_engine)
        if owns_app_engine:
            _dispose_engine(resolved_app_engine)


def run_receivable_ledger_sync(
    *,
    operations_sql: str,
    snapshot_date: date | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    opening_balance_date: date | None = None,
    opening_import_path: str | None = None,
    opening_snapshot_date: date | None = None,
    current_import_path: str | None = None,
    current_import_counterparty_group: str | None = "ПОКУПАТЕЛИ",
    employee_counterparty_refs: tuple[str, ...] = (),
    employee_current_import_path: str | None = None,
    employee_current_import_counterparty_group: str | None = "СОТРУДНИКИ",
    fired_manager_refs: tuple[str, ...] = (),
    replace_existing: bool = False,
) -> dict:
    if opening_snapshot_date is not None:
        raise ValueError("opening_snapshot_date больше не поддерживается в receivable_ledger_sync.")
    if current_import_path or employee_current_import_path:
        raise ValueError(
            "current_import_path/employee_current_import_path больше не поддерживаются "
            "в receivable_ledger_sync."
        )
    if opening_import_path and opening_balance_date is None:
        raise ValueError("opening_import_path requires opening_balance_date")
    sync_started_at = monotonic()
    onec_engine = _get_onec_engine()
    app_engine = _get_app_engine()
    print("[receivables] preload employee counterparties", flush=True)
    preload_started_at = monotonic()
    resolved_employee_refs = _resolve_employee_counterparty_refs(
        onec_engine,
        app_engine=app_engine,
        employee_counterparty_refs=employee_counterparty_refs,
    )
    print(
        (
            "[receivables] preload employee counterparties done "
            f"count={len(resolved_employee_refs)} sec={monotonic() - preload_started_at:.1f}"
        ),
        flush=True,
    )
    print("[receivables] preload staff members", flush=True)
    preload_started_at = monotonic()
    resolved_staff_rows = _resolve_staff_rows(onec_engine, app_engine=app_engine)
    print(
        (
            "[receivables] preload staff members done "
            f"count={len(resolved_staff_rows)} sec={monotonic() - preload_started_at:.1f}"
        ),
        flush=True,
    )
    extractor = OneCReceivableLedgerExtractor(onec_engine, operations_sql=operations_sql)
    opening_import_events: list[ReceivableLedgerRow] = []
    if opening_import_path:
        print("[receivables] preload opening import", flush=True)
        preload_started_at = monotonic()
        opening_import_events = build_receivable_opening_import_events(
            onec_engine,
            report_path=Path(opening_import_path),
        )
        _raise_on_synthetic_event_counterparties(opening_import_events)
        print(
            (
                "[receivables] preload opening import done "
                f"count={len(opening_import_events)} sec={monotonic() - preload_started_at:.1f}"
            ),
            flush=True,
        )
    authoritative_balance_rows: list[AuthoritativeReceivableBalanceRow] | None = None
    authoritative_meta: dict[str, Any] = {
        "regular_current_override_count": 0,
        "current_import_override_count": 0,
        "total_current_override_count": 0,
        "employee_current_import_override_count": 0,
        "authoritative_balance_row_count": 0,
        "opening_import_balance_row_count": 0,
        "opening_snapshot_balance_row_count": 0,
        "balance_source_mode": "ledger_events_authoritative",
    }
    effective_window_start = window_start
    if opening_import_path and opening_balance_date is not None and effective_window_start is None:
        effective_window_start = datetime.combine(opening_balance_date, time.min)
    sync_windows = _build_receivable_sync_windows(
        window_start=effective_window_start,
        window_end=window_end,
        snapshot_date=snapshot_date,
    )

    aggregate_result = _layered_sync_result_template()
    staff_result = {"processed": 0, "inserted": 0, "updated": 0}
    with Session(app_engine) as staff_session:
        print("[receivables] upsert staff members", flush=True)
        staff_started_at = monotonic()
        staff_result = upsert_staff_members(staff_session, resolved_staff_rows)
        staff_session.commit()
    print(
        (
            "[receivables] upsert staff members done "
            f"processed={staff_result['processed']} "
            f"inserted={staff_result['inserted']} "
            f"updated={staff_result['updated']} "
            f"sec={monotonic() - staff_started_at:.1f}"
        ),
        flush=True,
    )

    total_windows = len(sync_windows)
    for index, (chunk_window_start, chunk_window_end) in enumerate(sync_windows):
        window_no = index + 1
        chunk_started_at = monotonic()
        print(
            (
                "[receivables] window "
                f"{window_no}/{total_windows} "
                f"start={chunk_window_start} end={chunk_window_end}"
            ),
            flush=True,
        )
        sql_opening_balance_date = (
            None if opening_import_path or index != 0 else opening_balance_date
        )
        streamed_events = extractor.iter_receivable_events(
            window_start=chunk_window_start,
            window_end=chunk_window_end,
            opening_balance_date=sql_opening_balance_date,
        )
        events = (
            chain(opening_import_events, streamed_events)
            if opening_import_events and index == 0
            else streamed_events
        )
        with Session(app_engine) as session:
            chunk_result = sync_receivable_ledger(
                session,
                events,
                snapshot_date=snapshot_date if index + 1 == len(sync_windows) else None,
                authoritative_balance_rows=(
                    authoritative_balance_rows if index + 1 == len(sync_windows) else None
                ),
                employee_counterparty_refs=resolved_employee_refs,
                fired_manager_refs=fired_manager_refs,
                replace_existing=replace_existing if index == 0 else False,
                rebuild_read_models=index + 1 == len(sync_windows),
            )
            session.commit()
        chunk_elapsed = monotonic() - chunk_started_at
        print(
            (
                "[receivables] window "
                f"{window_no}/{total_windows} done "
                f"processed={chunk_result['processed']} "
                f"inserted={chunk_result['inserted']} "
                f"updated={chunk_result['updated']} "
                f"existing={chunk_result['existing']} "
                f"sec={chunk_elapsed:.1f}"
            ),
            flush=True,
        )

        _merge_sync_result(aggregate_result, chunk_result)
        if index == 0:
            aggregate_result["reset"] = chunk_result["reset"]

    aggregate_result["staff_members"] = staff_result
    aggregate_result["opening_import_event_count"] = len(opening_import_events)
    aggregate_result.update(authoritative_meta)
    aggregate_result["fetched_events"] = aggregate_result["processed"]
    aggregate_result["staff_member_payload_count"] = len(resolved_staff_rows)
    aggregate_result["employee_counterparty_ref_count"] = len(resolved_employee_refs)
    aggregate_result["sync_window_count"] = len(sync_windows)
    aggregate_result["sync_elapsed_seconds"] = round(monotonic() - sync_started_at, 3)
    return aggregate_result
