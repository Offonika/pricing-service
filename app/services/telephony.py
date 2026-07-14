from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Sequence

from sqlalchemy import Engine, delete, func, select, text
from sqlalchemy.orm import Session

from app.models import StaffMember, TelephonyUserLineSnapshot

TELEPHONY_MAPPING_SOURCE_ONEC = "onec_user_workstation"
TELEPHONY_STATUS_ACTIVE = "active"
TELEPHONY_STATUS_FIRED = "fired"
TELEPHONY_STATUS_MARKED = "marked"
TELEPHONY_MAPPING_MODE_SINGLE_BITRIX = "single_active_bitrix_user"
TELEPHONY_MAPPING_MODE_SHARED = "shared_extension"
TELEPHONY_MAPPING_MODE_SINGLE_NO_BITRIX = "single_active_without_bitrix"
TELEPHONY_MAPPING_MODE_NO_ACTIVE_OWNER = "no_active_owner"
TELEPHONY_MAPPING_MODE_SERVICE_OVERLAY = "service_overlay"

ONEC_TELEPHONY_USER_LINE_SQL = """
WITH users AS (
    SELECT
        CONVERT(varchar(64), master.dbo.fn_varbintohexstr(u._IDRRef)) AS user_ref_hex,
        NULLIF(LTRIM(RTRIM(u._Description)), '') AS user_name,
        CONVERT(varchar(64), master.dbo.fn_varbintohexstr(u._Fld915RRef)) AS physical_person_ref_hex,
        NULLIF(LTRIM(RTRIM(u._Fld9494)), '') AS computer_name,
        CONVERT(varchar(64), master.dbo.fn_varbintohexstr(u._Fld8807RRef)) AS store_ref_hex,
        CONVERT(varchar(64), master.dbo.fn_varbintohexstr(u._Fld9524RRef)) AS department_ref_hex,
        CASE WHEN u._Marked = 0x01 THEN 1 ELSE 0 END AS is_marked
    FROM dbo._Reference69 AS u WITH (NOLOCK)
),
people AS (
    SELECT
        CONVERT(varchar(64), master.dbo.fn_varbintohexstr(p._IDRRef)) AS physical_person_ref_hex,
        NULLIF(LTRIM(RTRIM(p._Description)), '') AS physical_person_name
    FROM dbo._Reference94 AS p WITH (NOLOCK)
),
stores AS (
    SELECT
        CONVERT(varchar(64), master.dbo.fn_varbintohexstr(s._IDRRef)) AS store_ref_hex,
        NULLIF(LTRIM(RTRIM(s._Code)), '') AS store_code,
        NULLIF(LTRIM(RTRIM(s._Description)), '') AS store_name
    FROM dbo._Reference80 AS s WITH (NOLOCK)
),
departments AS (
    SELECT
        CONVERT(varchar(64), master.dbo.fn_varbintohexstr(d._IDRRef)) AS department_ref_hex,
        NULLIF(LTRIM(RTRIM(d._Code)), '') AS department_code,
        NULLIF(LTRIM(RTRIM(d._Description)), '') AS department_name
    FROM dbo._Reference68 AS d WITH (NOLOCK)
),
lines AS (
    SELECT DISTINCT
        NULLIF(LTRIM(RTRIM(vt._Fld9489)), '') AS computer_name,
        NULLIF(LTRIM(RTRIM(vt._Fld9519)), '') AS extension
    FROM dbo._Reference9471_VT9487 AS vt WITH (NOLOCK)
    WHERE NULLIF(LTRIM(RTRIM(vt._Fld9489)), '') IS NOT NULL
)
SELECT
    u.user_ref_hex,
    u.user_name,
    u.physical_person_ref_hex,
    p.physical_person_name,
    u.computer_name,
    l.extension,
    u.store_ref_hex,
    s.store_code,
    s.store_name,
    u.department_ref_hex,
    d.department_code,
    d.department_name,
    u.is_marked
FROM users AS u
LEFT JOIN people AS p
    ON p.physical_person_ref_hex = u.physical_person_ref_hex
LEFT JOIN stores AS s
    ON s.store_ref_hex = u.store_ref_hex
LEFT JOIN departments AS d
    ON d.department_ref_hex = u.department_ref_hex
LEFT JOIN lines AS l
    ON l.computer_name = u.computer_name
"""

MDM_TELEPHONY_BITRIX_SQL = """
SELECT
    onec_primary_ref,
    bitrix_user_id,
    bitrix_full_name,
    mdm_employee_code,
    bitrix_status
FROM reconciliation.employee_master_map
WHERE onec_primary_ref IS NOT NULL
"""


@dataclass(slots=True)
class TelephonyUserLineRow:
    snapshot_date: date
    mapping_source: str
    user_ref_hex: str
    user_name: str | None = None
    physical_person_ref_hex: str | None = None
    physical_person_name: str | None = None
    computer_name: str | None = None
    extension: str | None = None
    store_ref_hex: str | None = None
    store_code: str | None = None
    store_name: str | None = None
    department_ref_hex: str | None = None
    department_code: str | None = None
    department_name: str | None = None
    employment_status: str | None = None
    staff_store_ref: str | None = None
    staff_store_name: str | None = None
    staff_department_ref: str | None = None
    staff_department_name: str | None = None
    bitrix_user_id: str | None = None
    bitrix_full_name: str | None = None
    mdm_employee_code: str | None = None
    bitrix_status: str | None = None
    is_marked: bool = False
    has_extension: bool = False
    has_bitrix: bool = False

    def to_model(self) -> TelephonyUserLineSnapshot:
        return TelephonyUserLineSnapshot(
            snapshot_date=self.snapshot_date,
            mapping_source=self.mapping_source,
            user_ref_hex=self.user_ref_hex,
            user_name=self.user_name,
            physical_person_ref_hex=self.physical_person_ref_hex,
            physical_person_name=self.physical_person_name,
            computer_name=self.computer_name,
            extension=self.extension,
            store_ref_hex=self.store_ref_hex,
            store_code=self.store_code,
            store_name=self.store_name,
            department_ref_hex=self.department_ref_hex,
            department_code=self.department_code,
            department_name=self.department_name,
            employment_status=self.employment_status,
            staff_store_ref=self.staff_store_ref,
            staff_store_name=self.staff_store_name,
            staff_department_ref=self.staff_department_ref,
            staff_department_name=self.staff_department_name,
            bitrix_user_id=self.bitrix_user_id,
            bitrix_full_name=self.bitrix_full_name,
            mdm_employee_code=self.mdm_employee_code,
            bitrix_status=self.bitrix_status,
            is_marked=self.is_marked,
            has_extension=self.has_extension,
            has_bitrix=self.has_bitrix,
        )


@dataclass(slots=True)
class TelephonyRetailLineMapRow:
    line_id: str
    phone_number: str | None
    store_id: str
    store_name: str
    mapping_mode: str
    active_user_count: int
    total_user_count: int
    store_names: list[str]
    employee_names: list[str]
    bitrix_user_ids: list[str]
    primary_bitrix_user_id: str | None
    primary_employee_name: str | None
    primary_store_name: str | None


def _clean_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _lower(value: str | None) -> str | None:
    if value is None:
        return None
    return value.lower()


def _normalize_extension(value: str | None) -> str | None:
    cleaned = _clean_string(value)
    if cleaned is None:
        return None
    return cleaned


def _is_active_status(value: str | None) -> bool:
    return (value or "").strip().lower() == TELEPHONY_STATUS_ACTIVE


def _dedupe_preserve_order(values: Iterable[str | None]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_string(value)
        if cleaned is None or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def _row_value(item: TelephonyUserLineRow | TelephonyUserLineSnapshot, field: str) -> Any:
    return getattr(item, field)


def _display_employee_name(item: TelephonyUserLineRow | TelephonyUserLineSnapshot) -> str | None:
    return (
        _row_value(item, "bitrix_full_name")
        or _row_value(item, "physical_person_name")
        or _row_value(item, "user_name")
    )


def _display_store_name(item: TelephonyUserLineRow | TelephonyUserLineSnapshot) -> str | None:
    return (
        _row_value(item, "staff_store_name")
        or _row_value(item, "store_name")
        or _row_value(item, "staff_department_name")
        or _row_value(item, "department_name")
    )


def _line_display_name(
    *,
    extension: str,
    store_names: Sequence[str],
    employee_names: Sequence[str],
) -> str:
    base = store_names[0] if store_names else (employee_names[0] if employee_names else None)
    if not base:
        return f"Line {extension}"
    if extension in base:
        return base
    return f"{base} {extension}"


def _normalize_service_line_labels(
    labels: Mapping[str, str] | None,
) -> dict[str, str]:
    if not labels:
        return {}
    normalized: dict[str, str] = {}
    for line_id, label in labels.items():
        normalized_line_id = _normalize_extension(str(line_id))
        normalized_label = _clean_string(label)
        if not normalized_line_id or not normalized_label:
            continue
        normalized[normalized_line_id] = normalized_label
    return normalized


def _normalize_line_id_set(line_ids: Collection[str] | None) -> set[str]:
    if not line_ids:
        return set()
    return {
        normalized
        for line_id in line_ids
        if (normalized := _normalize_extension(str(line_id))) is not None
    }


def _projection_sort_key(item: TelephonyUserLineRow | TelephonyUserLineSnapshot) -> tuple[Any, ...]:
    return (
        0 if _is_active_status(_row_value(item, "employment_status")) else 1,
        0 if _row_value(item, "bitrix_user_id") else 1,
        0 if _display_employee_name(item) else 1,
        _display_employee_name(item)
        or _row_value(item, "user_name")
        or _row_value(item, "user_ref_hex"),
    )


def fetch_onec_telephony_user_line_rows(
    onec_engine: Engine,
    *,
    snapshot_date: date,
) -> list[TelephonyUserLineRow]:
    with onec_engine.connect() as connection:
        rows = connection.execute(text(ONEC_TELEPHONY_USER_LINE_SQL)).mappings().all()

    return [
        TelephonyUserLineRow(
            snapshot_date=snapshot_date,
            mapping_source=TELEPHONY_MAPPING_SOURCE_ONEC,
            user_ref_hex=str(row["user_ref_hex"]),
            user_name=_clean_string(row["user_name"]),
            physical_person_ref_hex=_clean_string(row["physical_person_ref_hex"]),
            physical_person_name=_clean_string(row["physical_person_name"]),
            computer_name=_clean_string(row["computer_name"]),
            extension=_clean_string(row["extension"]),
            store_ref_hex=_clean_string(row["store_ref_hex"]),
            store_code=_clean_string(row["store_code"]),
            store_name=_clean_string(row["store_name"]),
            department_ref_hex=_clean_string(row["department_ref_hex"]),
            department_code=_clean_string(row["department_code"]),
            department_name=_clean_string(row["department_name"]),
            is_marked=bool(row["is_marked"]),
            has_extension=bool(_clean_string(row["extension"])),
        )
        for row in rows
    ]


def attach_staffing_metadata(
    session: Session,
    rows: list[TelephonyUserLineRow],
) -> int:
    if not rows:
        return 0

    refs = {_lower(row.physical_person_ref_hex) for row in rows if row.physical_person_ref_hex}
    ref_values = [value for value in refs if value]
    if not ref_values:
        for row in rows:
            if row.is_marked and not row.employment_status:
                row.employment_status = TELEPHONY_STATUS_MARKED
        return 0

    items = (
        session.execute(
            select(
                StaffMember.external_ref,
                StaffMember.employment_status,
                StaffMember.store_ref,
                StaffMember.store_name,
                StaffMember.department_ref,
                StaffMember.department_name,
            ).where(
                StaffMember.source == "onec_physical_person",
                StaffMember.external_ref.in_(ref_values),
            )
        )
        .mappings()
        .all()
    )
    by_ref = {_lower(item["external_ref"]): item for item in items}

    matched = 0
    for row in rows:
        key = _lower(row.physical_person_ref_hex)
        staff = by_ref.get(key)
        if staff is None:
            if row.is_marked and not row.employment_status:
                row.employment_status = TELEPHONY_STATUS_MARKED
            continue
        matched += 1
        row.employment_status = _clean_string(staff["employment_status"])
        row.staff_store_ref = _clean_string(staff["store_ref"])
        row.staff_store_name = _clean_string(staff["store_name"])
        row.staff_department_ref = _clean_string(staff["department_ref"])
        row.staff_department_name = _clean_string(staff["department_name"])
    return matched


def attach_bitrix_metadata(
    rows: list[TelephonyUserLineRow],
    *,
    mdm_engine: Engine | None,
) -> int:
    if not rows or mdm_engine is None:
        return 0

    with mdm_engine.connect() as connection:
        items = connection.execute(text(MDM_TELEPHONY_BITRIX_SQL)).mappings().all()
    by_ref = {_lower(_clean_string(item["onec_primary_ref"])): item for item in items}

    matched = 0
    for row in rows:
        key = _lower(row.physical_person_ref_hex)
        item = by_ref.get(key)
        if item is None:
            row.has_bitrix = False
            continue
        matched += 1
        row.bitrix_user_id = _clean_string(item["bitrix_user_id"])
        row.bitrix_full_name = _clean_string(item["bitrix_full_name"])
        row.mdm_employee_code = _clean_string(item["mdm_employee_code"])
        row.bitrix_status = _clean_string(item["bitrix_status"])
        row.has_bitrix = bool(row.bitrix_user_id)
    return matched


def build_telephony_snapshot_metrics(
    rows: Sequence[TelephonyUserLineRow | TelephonyUserLineSnapshot],
) -> dict[str, int]:
    rows_total = len(rows)
    active_rows = [row for row in rows if _is_active_status(_row_value(row, "employment_status"))]
    rows_with_extension = [row for row in rows if _row_value(row, "extension")]
    active_rows_with_extension = [row for row in active_rows if _row_value(row, "extension")]
    active_rows_with_extension_and_bitrix = [
        row for row in active_rows_with_extension if _row_value(row, "bitrix_user_id")
    ]
    unique_extensions_total = {
        _row_value(row, "extension") for row in rows if _row_value(row, "extension")
    }
    unique_extensions_active = {
        _row_value(row, "extension")
        for row in active_rows_with_extension
        if _row_value(row, "extension")
    }
    shared_extensions_active = 0
    if active_rows_with_extension:
        by_extension: dict[str, set[str]] = defaultdict(set)
        for row in active_rows_with_extension:
            by_extension[str(_row_value(row, "extension"))].add(
                str(_row_value(row, "user_ref_hex"))
            )
        shared_extensions_active = sum(1 for values in by_extension.values() if len(values) > 1)

    return {
        "rows_total": rows_total,
        "active_rows": len(active_rows),
        "rows_with_extension": len(rows_with_extension),
        "active_rows_with_extension": len(active_rows_with_extension),
        "active_rows_with_extension_and_bitrix": len(active_rows_with_extension_and_bitrix),
        "unique_extensions_total": len(unique_extensions_total),
        "unique_extensions_active": len(unique_extensions_active),
        "shared_extensions_active": shared_extensions_active,
    }


def sync_telephony_user_line_snapshot(
    session: Session,
    *,
    rows: list[TelephonyUserLineRow],
    snapshot_date: date,
) -> dict[str, int | str]:
    if not rows:
        raise ValueError("telephony sync returned no rows; refusing to overwrite snapshot")

    deleted = (
        session.execute(
            delete(TelephonyUserLineSnapshot).where(
                TelephonyUserLineSnapshot.snapshot_date == snapshot_date
            )
        ).rowcount
        or 0
    )
    session.add_all([row.to_model() for row in rows])
    metrics = build_telephony_snapshot_metrics(rows)
    return {
        "snapshot_date": snapshot_date.isoformat(),
        "deleted": int(deleted),
        "inserted": len(rows),
        **metrics,
    }


def get_latest_telephony_snapshot_date(session: Session) -> date | None:
    return session.scalar(select(func.max(TelephonyUserLineSnapshot.snapshot_date)))


def load_telephony_user_line_snapshot(
    session: Session,
    *,
    snapshot_date: date | None = None,
    active_only: bool = False,
    with_extension_only: bool = False,
    with_bitrix_only: bool = False,
    limit: int | None = None,
) -> tuple[date | None, list[TelephonyUserLineSnapshot]]:
    effective_snapshot_date = snapshot_date or get_latest_telephony_snapshot_date(session)
    if effective_snapshot_date is None:
        return None, []

    stmt = select(TelephonyUserLineSnapshot).where(
        TelephonyUserLineSnapshot.snapshot_date == effective_snapshot_date
    )
    if active_only:
        stmt = stmt.where(TelephonyUserLineSnapshot.employment_status == TELEPHONY_STATUS_ACTIVE)
    if with_extension_only:
        stmt = stmt.where(TelephonyUserLineSnapshot.extension.is_not(None))
    if with_bitrix_only:
        stmt = stmt.where(TelephonyUserLineSnapshot.bitrix_user_id.is_not(None))

    stmt = stmt.order_by(
        TelephonyUserLineSnapshot.employment_status,
        TelephonyUserLineSnapshot.extension,
        TelephonyUserLineSnapshot.staff_store_name,
        TelephonyUserLineSnapshot.physical_person_name,
        TelephonyUserLineSnapshot.user_name,
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    items = session.execute(stmt).scalars().all()
    return effective_snapshot_date, items


def build_retail_line_map_projection(
    rows: Sequence[TelephonyUserLineRow | TelephonyUserLineSnapshot],
    *,
    service_line_labels: Mapping[str, str] | None = None,
    exclude_line_ids: Collection[str] | None = None,
) -> list[TelephonyRetailLineMapRow]:
    overlay_labels = _normalize_service_line_labels(service_line_labels)
    excluded_line_ids = _normalize_line_id_set(exclude_line_ids)
    by_extension: dict[str, list[TelephonyUserLineRow | TelephonyUserLineSnapshot]] = defaultdict(
        list
    )
    for row in rows:
        extension = _normalize_extension(_row_value(row, "extension"))
        if not extension or extension in excluded_line_ids:
            continue
        by_extension[extension].append(row)

    projection: list[TelephonyRetailLineMapRow] = []
    for extension, group in sorted(by_extension.items()):
        active_group = [
            row for row in group if _is_active_status(_row_value(row, "employment_status"))
        ]
        base_group = active_group or list(group)
        primary = sorted(base_group, key=_projection_sort_key)[0]
        employee_names = _dedupe_preserve_order(_display_employee_name(row) for row in base_group)
        store_names = _dedupe_preserve_order(_display_store_name(row) for row in base_group)
        bitrix_user_ids = _dedupe_preserve_order(
            _clean_string(_row_value(row, "bitrix_user_id")) for row in base_group
        )

        if len(active_group) == 1 and len(bitrix_user_ids) == 1:
            mapping_mode = TELEPHONY_MAPPING_MODE_SINGLE_BITRIX
            store_id = f"telephony_user_{bitrix_user_ids[0]}"
            store_name = employee_names[0] if employee_names else f"Line {extension}"
        elif len(active_group) > 1:
            mapping_mode = TELEPHONY_MAPPING_MODE_SHARED
            store_id = f"telephony_line_{extension}"
            store_name = _line_display_name(
                extension=extension,
                store_names=store_names,
                employee_names=employee_names,
            )
        elif len(active_group) == 1:
            mapping_mode = TELEPHONY_MAPPING_MODE_SINGLE_NO_BITRIX
            store_id = f"telephony_line_{extension}"
            store_name = _line_display_name(
                extension=extension,
                store_names=store_names,
                employee_names=employee_names,
            )
        else:
            mapping_mode = TELEPHONY_MAPPING_MODE_NO_ACTIVE_OWNER
            store_id = f"telephony_line_{extension}"
            store_name = _line_display_name(
                extension=extension,
                store_names=store_names,
                employee_names=employee_names,
            )

        projection.append(
            TelephonyRetailLineMapRow(
                line_id=extension,
                phone_number=None,
                store_id=store_id,
                store_name=store_name,
                mapping_mode=mapping_mode,
                active_user_count=len(active_group),
                total_user_count=len(group),
                store_names=store_names,
                employee_names=employee_names,
                bitrix_user_ids=bitrix_user_ids,
                primary_bitrix_user_id=_clean_string(_row_value(primary, "bitrix_user_id")),
                primary_employee_name=_display_employee_name(primary),
                primary_store_name=_display_store_name(primary),
            )
        )

    existing_line_ids = {item.line_id for item in projection}
    for line_id, label in sorted(overlay_labels.items()):
        if line_id in existing_line_ids or line_id in excluded_line_ids:
            continue
        projection.append(
            TelephonyRetailLineMapRow(
                line_id=line_id,
                phone_number=None,
                store_id=f"telephony_line_{line_id}",
                store_name=label,
                mapping_mode=TELEPHONY_MAPPING_MODE_SERVICE_OVERLAY,
                active_user_count=0,
                total_user_count=0,
                store_names=[label],
                employee_names=[],
                bitrix_user_ids=[],
                primary_bitrix_user_id=None,
                primary_employee_name=None,
                primary_store_name=label,
            )
        )
    projection.sort(key=lambda item: item.line_id)
    return projection


def build_telephony_health(
    session: Session,
    *,
    requested_date: date,
    max_lag_days: int,
    service_line_labels: Mapping[str, str] | None = None,
    exclude_line_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    snapshot_date, rows = load_telephony_user_line_snapshot(session)
    if snapshot_date is None:
        return {
            "as_of": requested_date,
            "status": "missing",
            "snapshot_date": None,
            "freshness_status": "missing",
            "source_status": "empty",
            "lag_days": None,
            "metrics": {
                "rows_total": 0,
                "unique_extensions_total": 0,
                "unique_projection_rows": 0,
            },
        }

    metrics = build_telephony_snapshot_metrics(rows)
    projection = build_retail_line_map_projection(
        rows,
        service_line_labels=service_line_labels,
        exclude_line_ids=exclude_line_ids,
    )
    lag_days = max((requested_date - snapshot_date).days, 0)
    freshness_status = "fresh" if lag_days <= max_lag_days else "stale"
    source_status = "ready" if rows else "empty"
    status = "ok" if freshness_status == "fresh" and source_status == "ready" else "degraded"
    return {
        "as_of": requested_date,
        "status": status,
        "snapshot_date": snapshot_date,
        "freshness_status": freshness_status,
        "source_status": source_status,
        "lag_days": lag_days,
        "metrics": {
            **metrics,
            "unique_projection_rows": len(projection),
            "single_active_bitrix_rows": sum(
                1 for row in projection if row.mapping_mode == TELEPHONY_MAPPING_MODE_SINGLE_BITRIX
            ),
            "shared_projection_rows": sum(
                1 for row in projection if row.mapping_mode == TELEPHONY_MAPPING_MODE_SHARED
            ),
        },
    }
