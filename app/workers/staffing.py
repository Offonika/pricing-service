from __future__ import annotations

from datetime import date
from typing import Any, Sequence

from sqlalchemy.orm import Session

from app.infrastructure.db import get_application_engine
from app.services.staffing import (
    StaffMemberRow,
    StoreShiftFactRow,
    StoreShiftPlanRow,
    sync_staffing_data,
)


def _get_app_engine():
    return get_application_engine()


def run_staffing_sync(
    *,
    staff_payload: Sequence[dict[str, Any]],
    shift_plan_payload: Sequence[dict[str, Any]],
    shift_fact_payload: Sequence[dict[str, Any]],
    snapshot_dates: Sequence[date] = (),
) -> dict[str, Any]:
    staff_members = [StaffMemberRow.from_mapping(item) for item in staff_payload]
    shift_plans = [StoreShiftPlanRow.from_mapping(item) for item in shift_plan_payload]
    shift_facts = [StoreShiftFactRow.from_mapping(item) for item in shift_fact_payload]

    with Session(_get_app_engine()) as session:
        result = sync_staffing_data(
            session,
            staff_members=staff_members,
            shift_plans=shift_plans,
            shift_facts=shift_facts,
            snapshot_dates=snapshot_dates,
        )
        session.commit()
    result["staff_payload_count"] = len(staff_members)
    result["shift_plan_payload_count"] = len(shift_plans)
    result["shift_fact_payload_count"] = len(shift_facts)
    return result
