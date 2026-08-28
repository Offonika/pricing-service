from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db import get_application_engine
from app.models import SiteOrderStageOutbox
from app.services.site_order_stage_outbox import (
    CRM_STAGE_PICKUP_TRANSIT,
    CRM_STAGE_PICKUP_WAITING,
    STATUS_MANUAL_REVIEW,
    STATUS_PENDING,
    STATUS_RETRY,
    _pilot_warehouse_allowed,
)

PILOT_TARGET_STAGES = (CRM_STAGE_PICKUP_TRANSIT, CRM_STAGE_PICKUP_WAITING)


def build_health_report(
    session: Session,
    *,
    pilot_warehouse_external_ids: list[str],
    now: datetime | None = None,
    max_delay_seconds: int = 30,
) -> dict:
    current_time = now or datetime.now()
    cutoff = current_time - timedelta(seconds=max_delay_seconds)
    rows = session.scalars(
        select(SiteOrderStageOutbox).where(
            SiteOrderStageOutbox.target_stage.in_(PILOT_TARGET_STAGES)
        )
    ).all()
    pilot_rows = [
        row for row in rows if _pilot_warehouse_allowed(session, row, pilot_warehouse_external_ids)
    ]
    active_rows = [row for row in pilot_rows if row.status in {STATUS_PENDING, STATUS_RETRY}]
    delayed_rows = [row for row in active_rows if row.created_at <= cutoff]
    retry_rows = [row for row in pilot_rows if row.status == STATUS_RETRY]
    manual_review_rows = [row for row in pilot_rows if row.status == STATUS_MANUAL_REVIEW]
    oldest_age_seconds = max(
        (max(0, int((current_time - row.created_at).total_seconds())) for row in active_rows),
        default=0,
    )
    status = "critical" if delayed_rows else ("warning" if retry_rows else "ok")
    return {
        "status": status,
        "max_delay_seconds": max_delay_seconds,
        "pilot_rows": len(pilot_rows),
        "pending": sum(row.status == STATUS_PENDING for row in pilot_rows),
        "retry": len(retry_rows),
        "manual_review": len(manual_review_rows),
        "delayed": len(delayed_rows),
        "oldest_active_age_seconds": oldest_age_seconds,
        "delayed_outbox_ids": [row.id for row in delayed_rows[:20]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check pilot logistics stage-outbox delay and failures."
    )
    parser.add_argument("--max-delay-seconds", type=int, default=30)
    args = parser.parse_args()
    if args.max_delay_seconds <= 0:
        raise SystemExit("--max-delay-seconds must be greater than zero")

    settings = get_settings()
    with Session(get_application_engine()) as session:
        report = build_health_report(
            session,
            pilot_warehouse_external_ids=(settings.logistics_stage_pilot_warehouse_external_ids),
            max_delay_seconds=args.max_delay_seconds,
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if report["status"] == "critical" else 0


if __name__ == "__main__":
    raise SystemExit(main())
