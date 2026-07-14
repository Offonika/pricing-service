from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import StaffingSnapshot, StaffMember, StoreShiftFact, StoreShiftPlan

STAFF_ACTIVE = "active"
STAFF_FIRED = "fired"

ATTENDANCE_ASSIGNED = "assigned"
ATTENDANCE_CONFIRMED = "confirmed"
ATTENDANCE_ABSENT = "absent"

CRITICALITY_OK = "ok"
CRITICALITY_WARNING = "warning"
CRITICALITY_CRITICAL = "critical"


def _clean_string(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _to_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value).strip())


def _to_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).strip())


def _quantize_ratio(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def normalize_employment_status(value: Any) -> str:
    normalized = (_clean_string(value) or STAFF_ACTIVE).lower()
    aliases = {
        "active": STAFF_ACTIVE,
        "активен": STAFF_ACTIVE,
        "working": STAFF_ACTIVE,
        "fired": STAFF_FIRED,
        "уволен": STAFF_FIRED,
        "dismissed": STAFF_FIRED,
    }
    if normalized in aliases:
        return aliases[normalized]
    raise ValueError(f"unsupported employment status: {value!r}")


def normalize_attendance_status(value: Any) -> str:
    normalized = (_clean_string(value) or ATTENDANCE_ASSIGNED).lower()
    aliases = {
        "assigned": ATTENDANCE_ASSIGNED,
        "назначен": ATTENDANCE_ASSIGNED,
        "confirmed": ATTENDANCE_CONFIRMED,
        "confirmed_exit": ATTENDANCE_CONFIRMED,
        "подтвержден": ATTENDANCE_CONFIRMED,
        "подтверждён": ATTENDANCE_CONFIRMED,
        "absent": ATTENDANCE_ABSENT,
        "no_show": ATTENDANCE_ABSENT,
        "невыход": ATTENDANCE_ABSENT,
    }
    if normalized in aliases:
        return aliases[normalized]
    raise ValueError(f"unsupported attendance status: {value!r}")


def build_shift_business_key(
    *,
    source: str,
    external_shift_ref: str,
    shift_date: date,
    shift_code: str,
    store_ref: str,
    role_code: str | None,
    slot_no: int,
) -> str:
    raw = "|".join(
        [
            source,
            external_shift_ref,
            shift_date.isoformat(),
            shift_code,
            store_ref,
            role_code or "",
            str(slot_no),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class StaffMemberRow:
    source: str
    external_ref: str
    full_name: str
    role_code: str | None
    role_name: str | None
    department_ref: str | None
    department_name: str | None
    store_ref: str | None
    store_name: str | None
    employment_status: str
    hire_date: date | None
    termination_date: date | None
    manager_ref: str | None
    manager_name: str | None

    @classmethod
    def from_mapping(cls, row: dict[str, Any], *, default_source: str = "b24_hr") -> StaffMemberRow:
        return cls(
            source=_clean_string(row.get("source")) or default_source,
            external_ref=_clean_string(row.get("external_ref")) or "",
            full_name=_clean_string(row.get("full_name")) or "",
            role_code=_clean_string(row.get("role_code")),
            role_name=_clean_string(row.get("role_name")),
            department_ref=_clean_string(row.get("department_ref")),
            department_name=_clean_string(row.get("department_name")),
            store_ref=_clean_string(row.get("store_ref")),
            store_name=_clean_string(row.get("store_name")),
            employment_status=normalize_employment_status(row.get("employment_status")),
            hire_date=_to_date(row.get("hire_date")),
            termination_date=_to_date(row.get("termination_date")),
            manager_ref=_clean_string(row.get("manager_ref")),
            manager_name=_clean_string(row.get("manager_name")),
        )


@dataclass(slots=True)
class StoreShiftPlanRow:
    source: str
    external_shift_ref: str
    slot_no: int
    shift_date: date
    shift_code: str
    store_ref: str
    store_name: str | None
    role_code: str | None
    role_name: str | None
    planned_start_at: datetime | None
    planned_end_at: datetime | None
    staff_ref: str | None
    staff_name: str | None

    @property
    def business_key(self) -> str:
        return build_shift_business_key(
            source=self.source,
            external_shift_ref=self.external_shift_ref,
            shift_date=self.shift_date,
            shift_code=self.shift_code,
            store_ref=self.store_ref,
            role_code=self.role_code,
            slot_no=self.slot_no,
        )

    @classmethod
    def from_mapping(
        cls, row: dict[str, Any], *, default_source: str = "b24_schedule"
    ) -> StoreShiftPlanRow:
        return cls(
            source=_clean_string(row.get("source")) or default_source,
            external_shift_ref=_clean_string(row.get("external_shift_ref")) or "",
            slot_no=int(row.get("slot_no") or 1),
            shift_date=_to_date(row.get("shift_date")) or date.min,
            shift_code=_clean_string(row.get("shift_code")) or "",
            store_ref=_clean_string(row.get("store_ref")) or "",
            store_name=_clean_string(row.get("store_name")),
            role_code=_clean_string(row.get("role_code")),
            role_name=_clean_string(row.get("role_name")),
            planned_start_at=_to_datetime(row.get("planned_start_at")),
            planned_end_at=_to_datetime(row.get("planned_end_at")),
            staff_ref=_clean_string(row.get("staff_ref")),
            staff_name=_clean_string(row.get("staff_name")),
        )


@dataclass(slots=True)
class StoreShiftFactRow:
    source: str
    external_shift_ref: str
    slot_no: int
    shift_date: date
    shift_code: str
    store_ref: str
    store_name: str | None
    role_code: str | None
    role_name: str | None
    staff_ref: str | None
    staff_name: str | None
    attendance_status: str
    actual_start_at: datetime | None
    actual_end_at: datetime | None

    @property
    def business_key(self) -> str:
        return build_shift_business_key(
            source=self.source,
            external_shift_ref=self.external_shift_ref,
            shift_date=self.shift_date,
            shift_code=self.shift_code,
            store_ref=self.store_ref,
            role_code=self.role_code,
            slot_no=self.slot_no,
        )

    @classmethod
    def from_mapping(
        cls, row: dict[str, Any], *, default_source: str = "b24_schedule"
    ) -> StoreShiftFactRow:
        return cls(
            source=_clean_string(row.get("source")) or default_source,
            external_shift_ref=_clean_string(row.get("external_shift_ref")) or "",
            slot_no=int(row.get("slot_no") or 1),
            shift_date=_to_date(row.get("shift_date")) or date.min,
            shift_code=_clean_string(row.get("shift_code")) or "",
            store_ref=_clean_string(row.get("store_ref")) or "",
            store_name=_clean_string(row.get("store_name")),
            role_code=_clean_string(row.get("role_code")),
            role_name=_clean_string(row.get("role_name")),
            staff_ref=_clean_string(row.get("staff_ref")),
            staff_name=_clean_string(row.get("staff_name")),
            attendance_status=normalize_attendance_status(row.get("attendance_status")),
            actual_start_at=_to_datetime(row.get("actual_start_at")),
            actual_end_at=_to_datetime(row.get("actual_end_at")),
        )


def _is_staff_active_on(staff_member: StaffMember | None, shift_date: date) -> bool:
    if staff_member is None:
        return True
    if staff_member.hire_date and staff_member.hire_date > shift_date:
        return False
    if staff_member.termination_date and staff_member.termination_date <= shift_date:
        return False
    return staff_member.employment_status != STAFF_FIRED


def upsert_staff_members(session: Session, rows: Sequence[StaffMemberRow]) -> dict[str, int]:
    if not rows:
        return {"processed": 0, "inserted": 0, "updated": 0}

    refs = {row.external_ref for row in rows}
    existing_items = (
        session.execute(select(StaffMember).where(StaffMember.external_ref.in_(refs)))
        .scalars()
        .all()
    )
    existing = {(item.source, item.external_ref): item for item in existing_items}

    inserted = 0
    updated = 0
    for row in rows:
        item = existing.get((row.source, row.external_ref))
        if item is None:
            session.add(
                StaffMember(
                    source=row.source,
                    external_ref=row.external_ref,
                    full_name=row.full_name,
                    role_code=row.role_code,
                    role_name=row.role_name,
                    department_ref=row.department_ref,
                    department_name=row.department_name,
                    store_ref=row.store_ref,
                    store_name=row.store_name,
                    employment_status=row.employment_status,
                    hire_date=row.hire_date,
                    termination_date=row.termination_date,
                    manager_ref=row.manager_ref,
                    manager_name=row.manager_name,
                )
            )
            inserted += 1
            continue

        before = (
            item.full_name,
            item.role_code,
            item.role_name,
            item.department_ref,
            item.department_name,
            item.store_ref,
            item.store_name,
            item.employment_status,
            item.hire_date,
            item.termination_date,
            item.manager_ref,
            item.manager_name,
        )
        item.full_name = row.full_name
        item.role_code = row.role_code
        item.role_name = row.role_name
        item.department_ref = row.department_ref
        item.department_name = row.department_name
        item.store_ref = row.store_ref
        item.store_name = row.store_name
        item.employment_status = row.employment_status
        item.hire_date = row.hire_date
        item.termination_date = row.termination_date
        item.manager_ref = row.manager_ref
        item.manager_name = row.manager_name
        after = (
            item.full_name,
            item.role_code,
            item.role_name,
            item.department_ref,
            item.department_name,
            item.store_ref,
            item.store_name,
            item.employment_status,
            item.hire_date,
            item.termination_date,
            item.manager_ref,
            item.manager_name,
        )
        if before != after:
            updated += 1

    return {"processed": len(rows), "inserted": inserted, "updated": updated}


def upsert_shift_plans(session: Session, rows: Sequence[StoreShiftPlanRow]) -> dict[str, int]:
    if not rows:
        return {"processed": 0, "inserted": 0, "updated": 0}

    business_keys = [row.business_key for row in rows]
    existing_items = (
        session.execute(
            select(StoreShiftPlan).where(StoreShiftPlan.business_key.in_(business_keys))
        )
        .scalars()
        .all()
    )
    existing = {item.business_key: item for item in existing_items}

    inserted = 0
    updated = 0
    for row in rows:
        item = existing.get(row.business_key)
        if item is None:
            session.add(
                StoreShiftPlan(
                    source=row.source,
                    business_key=row.business_key,
                    external_shift_ref=row.external_shift_ref,
                    slot_no=row.slot_no,
                    shift_date=row.shift_date,
                    shift_code=row.shift_code,
                    store_ref=row.store_ref,
                    store_name=row.store_name,
                    role_code=row.role_code,
                    role_name=row.role_name,
                    planned_start_at=row.planned_start_at,
                    planned_end_at=row.planned_end_at,
                    staff_ref=row.staff_ref,
                    staff_name=row.staff_name,
                )
            )
            inserted += 1
            continue

        before = (
            item.store_name,
            item.role_code,
            item.role_name,
            item.planned_start_at,
            item.planned_end_at,
            item.staff_ref,
            item.staff_name,
        )
        item.store_name = row.store_name
        item.role_code = row.role_code
        item.role_name = row.role_name
        item.planned_start_at = row.planned_start_at
        item.planned_end_at = row.planned_end_at
        item.staff_ref = row.staff_ref
        item.staff_name = row.staff_name
        after = (
            item.store_name,
            item.role_code,
            item.role_name,
            item.planned_start_at,
            item.planned_end_at,
            item.staff_ref,
            item.staff_name,
        )
        if before != after:
            updated += 1

    return {"processed": len(rows), "inserted": inserted, "updated": updated}


def upsert_shift_facts(session: Session, rows: Sequence[StoreShiftFactRow]) -> dict[str, int]:
    if not rows:
        return {"processed": 0, "inserted": 0, "updated": 0}

    business_keys = [row.business_key for row in rows]
    existing_items = (
        session.execute(
            select(StoreShiftFact).where(StoreShiftFact.business_key.in_(business_keys))
        )
        .scalars()
        .all()
    )
    existing = {item.business_key: item for item in existing_items}

    inserted = 0
    updated = 0
    for row in rows:
        item = existing.get(row.business_key)
        if item is None:
            session.add(
                StoreShiftFact(
                    source=row.source,
                    business_key=row.business_key,
                    external_shift_ref=row.external_shift_ref,
                    slot_no=row.slot_no,
                    shift_date=row.shift_date,
                    shift_code=row.shift_code,
                    store_ref=row.store_ref,
                    store_name=row.store_name,
                    role_code=row.role_code,
                    role_name=row.role_name,
                    staff_ref=row.staff_ref,
                    staff_name=row.staff_name,
                    attendance_status=row.attendance_status,
                    actual_start_at=row.actual_start_at,
                    actual_end_at=row.actual_end_at,
                )
            )
            inserted += 1
            continue

        before = (
            item.store_name,
            item.role_code,
            item.role_name,
            item.staff_ref,
            item.staff_name,
            item.attendance_status,
            item.actual_start_at,
            item.actual_end_at,
        )
        item.store_name = row.store_name
        item.role_code = row.role_code
        item.role_name = row.role_name
        item.staff_ref = row.staff_ref
        item.staff_name = row.staff_name
        item.attendance_status = row.attendance_status
        item.actual_start_at = row.actual_start_at
        item.actual_end_at = row.actual_end_at
        after = (
            item.store_name,
            item.role_code,
            item.role_name,
            item.staff_ref,
            item.staff_name,
            item.attendance_status,
            item.actual_start_at,
            item.actual_end_at,
        )
        if before != after:
            updated += 1

    return {"processed": len(rows), "inserted": inserted, "updated": updated}


def build_staffing_snapshots(session: Session, *, snapshot_date: date) -> dict[str, int]:
    plans = (
        session.execute(select(StoreShiftPlan).where(StoreShiftPlan.shift_date == snapshot_date))
        .scalars()
        .all()
    )
    facts = (
        session.execute(select(StoreShiftFact).where(StoreShiftFact.shift_date == snapshot_date))
        .scalars()
        .all()
    )
    staff_members = session.execute(select(StaffMember)).scalars().all()
    staff_by_ref = {item.external_ref: item for item in staff_members}

    session.execute(delete(StaffingSnapshot).where(StaffingSnapshot.snapshot_date == snapshot_date))

    plans_by_group: dict[tuple[str, str | None, str], list[StoreShiftPlan]] = defaultdict(list)
    facts_by_group: dict[tuple[str, str | None, str], list[StoreShiftFact]] = defaultdict(list)

    for item in plans:
        plans_by_group[(item.store_ref, item.store_name, item.shift_code)].append(item)
    for item in facts:
        facts_by_group[(item.store_ref, item.store_name, item.shift_code)].append(item)

    inserted = 0
    for group_key, group_plans in plans_by_group.items():
        store_ref, store_name, shift_code = group_key
        group_facts = facts_by_group.get(group_key, [])

        planned_by_role: dict[str, int] = defaultdict(int)
        assigned_by_role: dict[str, int] = defaultdict(int)
        confirmed_by_role: dict[str, int] = defaultdict(int)
        no_show_by_role: dict[str, int] = defaultdict(int)

        for item in group_plans:
            planned_by_role[item.role_code or "unknown"] += 1

        assigned_count = 0
        confirmed_count = 0
        no_show_count = 0
        for item in group_facts:
            staff_member = staff_by_ref.get(item.staff_ref or "")
            if not _is_staff_active_on(staff_member, snapshot_date):
                continue

            role_key = item.role_code or "unknown"
            if item.attendance_status in {
                ATTENDANCE_ASSIGNED,
                ATTENDANCE_CONFIRMED,
                ATTENDANCE_ABSENT,
            }:
                assigned_count += 1
                assigned_by_role[role_key] += 1
            if item.attendance_status == ATTENDANCE_CONFIRMED:
                confirmed_count += 1
                confirmed_by_role[role_key] += 1
            if item.attendance_status == ATTENDANCE_ABSENT:
                no_show_count += 1
                no_show_by_role[role_key] += 1

        planned_count = len(group_plans)
        deficit_role_counts = {
            role_key: deficit
            for role_key, deficit in (
                (role_key, max(planned_count_role - confirmed_by_role.get(role_key, 0), 0))
                for role_key, planned_count_role in planned_by_role.items()
            )
            if deficit > 0
        }
        deficit_count = sum(deficit_role_counts.values())
        if planned_count > 0:
            fill_rate = _quantize_ratio(Decimal(confirmed_count) / Decimal(planned_count))
        else:
            fill_rate = Decimal("1.0000")

        if deficit_count >= 2 or fill_rate < Decimal("0.5000"):
            criticality = CRITICALITY_CRITICAL
        elif deficit_count > 0 or no_show_count > 0:
            criticality = CRITICALITY_WARNING
        else:
            criticality = CRITICALITY_OK

        session.add(
            StaffingSnapshot(
                snapshot_date=snapshot_date,
                store_ref=store_ref,
                store_name=store_name,
                shift_code=shift_code,
                planned_count=planned_count,
                assigned_count=assigned_count,
                confirmed_count=confirmed_count,
                no_show_count=no_show_count,
                deficit_count=deficit_count,
                fill_rate=fill_rate,
                criticality=criticality,
                deficit_role_counts=deficit_role_counts or None,
            )
        )
        inserted += 1

    return {"snapshots": inserted}


def _count_repeated_deficit_days(dates_with_deficit: list[date]) -> int:
    if not dates_with_deficit:
        return 0
    ordered = sorted(set(dates_with_deficit))
    repeated: set[date] = set()
    streak: list[date] = [ordered[0]]
    for current_date in ordered[1:]:
        if current_date == streak[-1] + timedelta(days=1):
            streak.append(current_date)
            continue
        if len(streak) >= 2:
            repeated.update(streak)
        streak = [current_date]
    if len(streak) >= 2:
        repeated.update(streak)
    return len(repeated)


def build_staffing_forecast(
    session: Session,
    *,
    anchor_date: date,
    horizons: Sequence[int] = (3, 7, 14),
) -> dict[str, dict[int, int]]:
    max_horizon = max(horizons, default=0)
    if max_horizon <= 0:
        return {}

    future_start = anchor_date + timedelta(days=1)
    future_end = anchor_date + timedelta(days=max_horizon)
    plans = (
        session.execute(
            select(StoreShiftPlan).where(
                StoreShiftPlan.shift_date >= future_start,
                StoreShiftPlan.shift_date <= future_end,
            )
        )
        .scalars()
        .all()
    )
    facts = (
        session.execute(
            select(StoreShiftFact).where(
                StoreShiftFact.shift_date >= future_start,
                StoreShiftFact.shift_date <= future_end,
            )
        )
        .scalars()
        .all()
    )
    staff_members = session.execute(select(StaffMember)).scalars().all()
    staff_by_ref = {item.external_ref: item for item in staff_members}

    plans_by_group: dict[tuple[date, str, str], list[StoreShiftPlan]] = defaultdict(list)
    facts_by_group: dict[tuple[date, str, str], list[StoreShiftFact]] = defaultdict(list)
    for item in plans:
        plans_by_group[(item.shift_date, item.store_ref, item.shift_code)].append(item)
    for item in facts:
        facts_by_group[(item.shift_date, item.store_ref, item.shift_code)].append(item)

    forecasts: dict[str, dict[int, int]] = defaultdict(lambda: {horizon: 0 for horizon in horizons})
    for (shift_date, store_ref, shift_code), group_plans in plans_by_group.items():
        group_facts = facts_by_group.get((shift_date, store_ref, shift_code), [])
        assigned_count = 0
        for item in group_facts:
            staff_member = staff_by_ref.get(item.staff_ref or "")
            if not _is_staff_active_on(staff_member, shift_date):
                continue
            if item.attendance_status in {
                ATTENDANCE_ASSIGNED,
                ATTENDANCE_CONFIRMED,
                ATTENDANCE_ABSENT,
            }:
                assigned_count += 1
        planned_count = len(group_plans)
        if planned_count <= assigned_count:
            continue

        delta_days = (shift_date - anchor_date).days
        for horizon in horizons:
            if delta_days <= horizon:
                forecasts[store_ref][horizon] += 1

    return {store_ref: dict(values) for store_ref, values in forecasts.items()}


def build_staffing_period_summary(
    session: Session,
    *,
    date_from: date,
    date_to: date,
    forecast_anchor_date: date | None = None,
) -> list[dict[str, Any]]:
    snapshots = (
        session.execute(
            select(StaffingSnapshot).where(
                StaffingSnapshot.snapshot_date >= date_from,
                StaffingSnapshot.snapshot_date <= date_to,
            )
        )
        .scalars()
        .all()
    )
    forecasts = (
        build_staffing_forecast(session, anchor_date=forecast_anchor_date)
        if forecast_anchor_date is not None
        else {}
    )

    grouped: dict[str, list[StaffingSnapshot]] = defaultdict(list)
    for item in snapshots:
        grouped[item.store_ref].append(item)

    summary: list[dict[str, Any]] = []
    for store_ref, items in sorted(grouped.items()):
        total_planned = sum(item.planned_count for item in items)
        total_assigned = sum(item.assigned_count for item in items)
        total_confirmed = sum(item.confirmed_count for item in items)
        total_no_shows = sum(item.no_show_count for item in items)
        deficit_dates = [item.snapshot_date for item in items if item.deficit_count > 0]
        critical_days = sum(1 for item in items if item.criticality == CRITICALITY_CRITICAL)
        average_fill_rate = (
            float(_quantize_ratio(Decimal(total_confirmed) / Decimal(total_planned)))
            if total_planned
            else 1.0
        )
        summary.append(
            {
                "store_ref": store_ref,
                "store_name": items[0].store_name,
                "period_start": date_from.isoformat(),
                "period_end": date_to.isoformat(),
                "total_planned_count": total_planned,
                "total_assigned_count": total_assigned,
                "total_confirmed_count": total_confirmed,
                "total_no_show_count": total_no_shows,
                "average_fill_rate": average_fill_rate,
                "days_with_deficit": len(set(deficit_dates)),
                "critical_days": critical_days,
                "repeated_deficit_days": _count_repeated_deficit_days(deficit_dates),
                "forecast_deficit_days": forecasts.get(store_ref, {3: 0, 7: 0, 14: 0}),
            }
        )
    return summary


def list_staffing_snapshots(
    session: Session,
    *,
    snapshot_date: date,
) -> list[StaffingSnapshot]:
    return (
        session.execute(
            select(StaffingSnapshot)
            .where(StaffingSnapshot.snapshot_date == snapshot_date)
            .order_by(StaffingSnapshot.store_ref, StaffingSnapshot.shift_code)
        )
        .scalars()
        .all()
    )


def sync_staffing_data(
    session: Session,
    *,
    staff_members: Sequence[StaffMemberRow],
    shift_plans: Sequence[StoreShiftPlanRow],
    shift_facts: Sequence[StoreShiftFactRow],
    snapshot_dates: Sequence[date] = (),
) -> dict[str, Any]:
    staff_result = upsert_staff_members(session, staff_members)
    plan_result = upsert_shift_plans(session, shift_plans)
    fact_result = upsert_shift_facts(session, shift_facts)

    snapshot_count = 0
    for snapshot_date in snapshot_dates:
        snapshot_count += build_staffing_snapshots(session, snapshot_date=snapshot_date)[
            "snapshots"
        ]

    return {
        "staff_members": staff_result,
        "shift_plans": plan_result,
        "shift_facts": fact_result,
        "snapshots": snapshot_count,
    }
