from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, StaffingSnapshot, StaffMember, StoreShiftFact, StoreShiftPlan
from app.services.staffing import (
    StaffMemberRow,
    StoreShiftFactRow,
    StoreShiftPlanRow,
    build_staffing_period_summary,
    sync_staffing_data,
)


def _staff_rows() -> list[StaffMemberRow]:
    return [
        StaffMemberRow.from_mapping(
            {
                "external_ref": "staff-1",
                "full_name": "Продавец 1",
                "role_code": "seller",
                "role_name": "Продавец",
                "department_ref": "dep-1",
                "department_name": "Розница",
                "store_ref": "store-1",
                "store_name": "Точка 1",
                "employment_status": "active",
                "hire_date": "2025-01-01",
                "manager_ref": "mgr-1",
                "manager_name": "Руководитель 1",
            }
        ),
        StaffMemberRow.from_mapping(
            {
                "external_ref": "staff-2",
                "full_name": "Продавец 2",
                "role_code": "seller",
                "role_name": "Продавец",
                "department_ref": "dep-1",
                "department_name": "Розница",
                "store_ref": "store-1",
                "store_name": "Точка 1",
                "employment_status": "active",
                "hire_date": "2025-01-01",
                "manager_ref": "mgr-1",
                "manager_name": "Руководитель 1",
            }
        ),
        StaffMemberRow.from_mapping(
            {
                "external_ref": "staff-3",
                "full_name": "Продавец 3",
                "role_code": "seller",
                "role_name": "Продавец",
                "department_ref": "dep-1",
                "department_name": "Розница",
                "store_ref": "store-1",
                "store_name": "Точка 1",
                "employment_status": "fired",
                "hire_date": "2024-01-01",
                "termination_date": "2026-03-21",
                "manager_ref": "mgr-1",
                "manager_name": "Руководитель 1",
            }
        ),
        StaffMemberRow.from_mapping(
            {
                "external_ref": "staff-4",
                "full_name": "Мастер 1",
                "role_code": "master",
                "role_name": "Мастер",
                "department_ref": "dep-1",
                "department_name": "Розница",
                "store_ref": "store-1",
                "store_name": "Точка 1",
                "employment_status": "active",
                "hire_date": "2025-01-01",
                "manager_ref": "mgr-1",
                "manager_name": "Руководитель 1",
            }
        ),
        StaffMemberRow.from_mapping(
            {
                "external_ref": "staff-5",
                "full_name": "Продавец 5",
                "role_code": "seller",
                "role_name": "Продавец",
                "department_ref": "dep-2",
                "department_name": "Розница",
                "store_ref": "store-2",
                "store_name": "Точка 2",
                "employment_status": "active",
                "hire_date": "2025-01-01",
                "manager_ref": "mgr-2",
                "manager_name": "Руководитель 2",
            }
        ),
    ]


def _plan_rows() -> list[StoreShiftPlanRow]:
    raw_rows = [
        {
            "external_shift_ref": "sh-20-1",
            "slot_no": 1,
            "shift_date": "2026-03-20",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-1",
            "staff_name": "Продавец 1",
        },
        {
            "external_shift_ref": "sh-20-1",
            "slot_no": 2,
            "shift_date": "2026-03-20",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-2",
            "staff_name": "Продавец 2",
        },
        {
            "external_shift_ref": "sh-20-1",
            "slot_no": 3,
            "shift_date": "2026-03-20",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "master",
            "role_name": "Мастер",
            "staff_ref": "staff-4",
            "staff_name": "Мастер 1",
        },
        {
            "external_shift_ref": "sh-21-1",
            "slot_no": 1,
            "shift_date": "2026-03-21",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-1",
            "staff_name": "Продавец 1",
        },
        {
            "external_shift_ref": "sh-21-1",
            "slot_no": 2,
            "shift_date": "2026-03-21",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-3",
            "staff_name": "Продавец 3",
        },
        {
            "external_shift_ref": "sh-21-1",
            "slot_no": 3,
            "shift_date": "2026-03-21",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "master",
            "role_name": "Мастер",
            "staff_ref": "staff-4",
            "staff_name": "Мастер 1",
        },
        {
            "external_shift_ref": "sh-22-1",
            "slot_no": 1,
            "shift_date": "2026-03-22",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-1",
            "staff_name": "Продавец 1",
        },
        {
            "external_shift_ref": "sh-22-1",
            "slot_no": 2,
            "shift_date": "2026-03-22",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-3",
            "staff_name": "Продавец 3",
        },
        {
            "external_shift_ref": "sh-22-1",
            "slot_no": 3,
            "shift_date": "2026-03-22",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "master",
            "role_name": "Мастер",
            "staff_ref": "staff-4",
            "staff_name": "Мастер 1",
        },
        {
            "external_shift_ref": "sh-23-1",
            "slot_no": 1,
            "shift_date": "2026-03-23",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-1",
            "staff_name": "Продавец 1",
        },
        {
            "external_shift_ref": "sh-23-1",
            "slot_no": 2,
            "shift_date": "2026-03-23",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "seller",
            "role_name": "Продавец",
        },
        {
            "external_shift_ref": "sh-23-1",
            "slot_no": 3,
            "shift_date": "2026-03-23",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "master",
            "role_name": "Мастер",
            "staff_ref": "staff-4",
            "staff_name": "Мастер 1",
        },
        {
            "external_shift_ref": "sh-24-1",
            "slot_no": 1,
            "shift_date": "2026-03-24",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-1",
            "staff_name": "Продавец 1",
        },
        {
            "external_shift_ref": "sh-24-1",
            "slot_no": 2,
            "shift_date": "2026-03-24",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "seller",
            "role_name": "Продавец",
        },
        {
            "external_shift_ref": "sh-24-1",
            "slot_no": 3,
            "shift_date": "2026-03-24",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "master",
            "role_name": "Мастер",
            "staff_ref": "staff-4",
            "staff_name": "Мастер 1",
        },
        {
            "external_shift_ref": "sh-20-2",
            "slot_no": 1,
            "shift_date": "2026-03-20",
            "shift_code": "open",
            "store_ref": "store-2",
            "store_name": "Точка 2",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-5",
            "staff_name": "Продавец 5",
        },
        {
            "external_shift_ref": "sh-21-2",
            "slot_no": 1,
            "shift_date": "2026-03-21",
            "shift_code": "open",
            "store_ref": "store-2",
            "store_name": "Точка 2",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-5",
            "staff_name": "Продавец 5",
        },
        {
            "external_shift_ref": "sh-22-2",
            "slot_no": 1,
            "shift_date": "2026-03-22",
            "shift_code": "open",
            "store_ref": "store-2",
            "store_name": "Точка 2",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-5",
            "staff_name": "Продавец 5",
        },
    ]
    return [StoreShiftPlanRow.from_mapping(item) for item in raw_rows]


def _fact_rows() -> list[StoreShiftFactRow]:
    raw_rows = [
        {
            "external_shift_ref": "sh-20-1",
            "slot_no": 1,
            "shift_date": "2026-03-20",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-1",
            "staff_name": "Продавец 1",
            "attendance_status": "confirmed",
        },
        {
            "external_shift_ref": "sh-20-1",
            "slot_no": 2,
            "shift_date": "2026-03-20",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-2",
            "staff_name": "Продавец 2",
            "attendance_status": "absent",
        },
        {
            "external_shift_ref": "sh-20-1",
            "slot_no": 3,
            "shift_date": "2026-03-20",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "master",
            "role_name": "Мастер",
            "staff_ref": "staff-4",
            "staff_name": "Мастер 1",
            "attendance_status": "confirmed",
        },
        {
            "external_shift_ref": "sh-21-1",
            "slot_no": 1,
            "shift_date": "2026-03-21",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-1",
            "staff_name": "Продавец 1",
            "attendance_status": "confirmed",
        },
        {
            "external_shift_ref": "sh-21-1",
            "slot_no": 2,
            "shift_date": "2026-03-21",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-3",
            "staff_name": "Продавец 3",
            "attendance_status": "confirmed",
        },
        {
            "external_shift_ref": "sh-21-1",
            "slot_no": 3,
            "shift_date": "2026-03-21",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "master",
            "role_name": "Мастер",
            "staff_ref": "staff-4",
            "staff_name": "Мастер 1",
            "attendance_status": "confirmed",
        },
        {
            "external_shift_ref": "sh-22-1",
            "slot_no": 1,
            "shift_date": "2026-03-22",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-1",
            "staff_name": "Продавец 1",
            "attendance_status": "confirmed",
        },
        {
            "external_shift_ref": "sh-22-1",
            "slot_no": 3,
            "shift_date": "2026-03-22",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "master",
            "role_name": "Мастер",
            "staff_ref": "staff-4",
            "staff_name": "Мастер 1",
            "attendance_status": "confirmed",
        },
        {
            "external_shift_ref": "sh-23-1",
            "slot_no": 1,
            "shift_date": "2026-03-23",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-1",
            "staff_name": "Продавец 1",
            "attendance_status": "assigned",
        },
        {
            "external_shift_ref": "sh-23-1",
            "slot_no": 3,
            "shift_date": "2026-03-23",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "master",
            "role_name": "Мастер",
            "staff_ref": "staff-4",
            "staff_name": "Мастер 1",
            "attendance_status": "assigned",
        },
        {
            "external_shift_ref": "sh-24-1",
            "slot_no": 1,
            "shift_date": "2026-03-24",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-1",
            "staff_name": "Продавец 1",
            "attendance_status": "assigned",
        },
        {
            "external_shift_ref": "sh-24-1",
            "slot_no": 3,
            "shift_date": "2026-03-24",
            "shift_code": "open",
            "store_ref": "store-1",
            "store_name": "Точка 1",
            "role_code": "master",
            "role_name": "Мастер",
            "staff_ref": "staff-4",
            "staff_name": "Мастер 1",
            "attendance_status": "assigned",
        },
        {
            "external_shift_ref": "sh-20-2",
            "slot_no": 1,
            "shift_date": "2026-03-20",
            "shift_code": "open",
            "store_ref": "store-2",
            "store_name": "Точка 2",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-5",
            "staff_name": "Продавец 5",
            "attendance_status": "confirmed",
        },
        {
            "external_shift_ref": "sh-21-2",
            "slot_no": 1,
            "shift_date": "2026-03-21",
            "shift_code": "open",
            "store_ref": "store-2",
            "store_name": "Точка 2",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-5",
            "staff_name": "Продавец 5",
            "attendance_status": "confirmed",
        },
        {
            "external_shift_ref": "sh-22-2",
            "slot_no": 1,
            "shift_date": "2026-03-22",
            "shift_code": "open",
            "store_ref": "store-2",
            "store_name": "Точка 2",
            "role_code": "seller",
            "role_name": "Продавец",
            "staff_ref": "staff-5",
            "staff_name": "Продавец 5",
            "attendance_status": "confirmed",
        },
    ]
    return [StoreShiftFactRow.from_mapping(item) for item in raw_rows]


def test_sync_staffing_is_idempotent_and_builds_snapshots() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        staff_rows = _staff_rows()
        plan_rows = _plan_rows()
        fact_rows = _fact_rows()

        result1 = sync_staffing_data(
            session,
            staff_members=staff_rows,
            shift_plans=plan_rows,
            shift_facts=fact_rows,
            snapshot_dates=[date(2026, 3, 20), date(2026, 3, 21), date(2026, 3, 22)],
        )
        session.commit()

        assert result1["staff_members"]["inserted"] == 5
        assert result1["shift_plans"]["inserted"] == 18
        assert result1["shift_facts"]["inserted"] == 15
        assert result1["snapshots"] == 6

        result2 = sync_staffing_data(
            session,
            staff_members=staff_rows,
            shift_plans=plan_rows,
            shift_facts=fact_rows,
            snapshot_dates=[date(2026, 3, 20), date(2026, 3, 21), date(2026, 3, 22)],
        )
        session.commit()

        assert result2["staff_members"]["inserted"] == 0
        assert result2["shift_plans"]["inserted"] == 0
        assert result2["shift_facts"]["inserted"] == 0
        assert session.query(StaffMember).count() == 5
        assert session.query(StoreShiftPlan).count() == 18
        assert session.query(StoreShiftFact).count() == 15
        assert session.query(StaffingSnapshot).count() == 6

        snapshot_20 = (
            session.query(StaffingSnapshot)
            .filter(
                StaffingSnapshot.snapshot_date == date(2026, 3, 20),
                StaffingSnapshot.store_ref == "store-1",
                StaffingSnapshot.shift_code == "open",
            )
            .one()
        )
        assert snapshot_20.planned_count == 3
        assert snapshot_20.assigned_count == 3
        assert snapshot_20.confirmed_count == 2
        assert snapshot_20.no_show_count == 1
        assert snapshot_20.deficit_count == 1
        assert snapshot_20.criticality == "warning"
        assert snapshot_20.deficit_role_counts == {"seller": 1}

        snapshot_21 = (
            session.query(StaffingSnapshot)
            .filter(
                StaffingSnapshot.snapshot_date == date(2026, 3, 21),
                StaffingSnapshot.store_ref == "store-1",
                StaffingSnapshot.shift_code == "open",
            )
            .one()
        )
        assert snapshot_21.assigned_count == 2
        assert snapshot_21.confirmed_count == 2
        assert snapshot_21.deficit_count == 1
        assert snapshot_21.deficit_role_counts == {"seller": 1}


def test_staffing_period_summary_uses_snapshots_and_builds_forecast() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        sync_staffing_data(
            session,
            staff_members=_staff_rows(),
            shift_plans=_plan_rows(),
            shift_facts=_fact_rows(),
            snapshot_dates=[date(2026, 3, 20), date(2026, 3, 21), date(2026, 3, 22)],
        )
        session.commit()

        session.query(StoreShiftFact).filter(
            StoreShiftFact.shift_date <= date(2026, 3, 22)
        ).delete()
        session.commit()

        summary = build_staffing_period_summary(
            session,
            date_from=date(2026, 3, 20),
            date_to=date(2026, 3, 22),
            forecast_anchor_date=date(2026, 3, 22),
        )

        store_1 = next(item for item in summary if item["store_ref"] == "store-1")
        assert store_1["average_fill_rate"] == 0.6667
        assert store_1["days_with_deficit"] == 3
        assert store_1["critical_days"] == 0
        assert store_1["repeated_deficit_days"] == 3
        assert store_1["forecast_deficit_days"] == {3: 2, 7: 2, 14: 2}

        store_2 = next(item for item in summary if item["store_ref"] == "store-2")
        assert store_2["average_fill_rate"] == 1.0
        assert store_2["days_with_deficit"] == 0
        assert store_2["repeated_deficit_days"] == 0
        assert store_2["forecast_deficit_days"] == {3: 0, 7: 0, 14: 0}
