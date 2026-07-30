from __future__ import annotations

import json

from app.core.config import get_settings
from app.infrastructure.db import get_application_session_factory
from app.services.customer_settlements import customer_settlement_health_metrics


def main() -> int:
    settings = get_settings()
    session = get_application_session_factory()()
    try:
        metrics = customer_settlement_health_metrics(
            session,
            stale_after_seconds=settings.customer_settlements_stale_after_seconds,
            hide_after_seconds=settings.customer_settlements_hide_after_seconds,
            mapping_stale_after_seconds=(settings.customer_settlements_mapping_stale_after_seconds),
        )
    finally:
        session.close()
    statuses = {str(metrics["freshness_status"]), str(metrics["mapping_status"])}
    overall_status = (
        "critical" if "critical" in statuses else "warning" if "warning" in statuses else "ok"
    )
    print(
        json.dumps(
            {"status": overall_status, "metrics": metrics},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return {"ok": 0, "warning": 1, "critical": 2}[overall_status]


if __name__ == "__main__":
    raise SystemExit(main())
