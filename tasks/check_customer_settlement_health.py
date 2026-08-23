from __future__ import annotations

import json

from app.core.config import get_settings
from app.infrastructure.db import get_application_session_factory
from app.services.customer_settlement_alerts import (
    dispatch_customer_settlement_alerts,
    enqueue_health_alert_if_needed,
    overall_health_status,
)
from app.services.customer_settlements import customer_settlement_health_metrics


def main() -> int:
    settings = get_settings()
    session = get_application_session_factory()()
    alert_delivery = {"processed": 0, "sent": 0, "failed": 0}
    try:
        metrics = customer_settlement_health_metrics(
            session,
            stale_after_seconds=settings.customer_settlements_stale_after_seconds,
            hide_after_seconds=settings.customer_settlements_hide_after_seconds,
            mapping_stale_after_seconds=(settings.customer_settlements_mapping_stale_after_seconds),
        )
        if settings.customer_settlements_alerts_enabled:
            enqueue_health_alert_if_needed(
                session,
                metrics=metrics,
                repeat_seconds=settings.customer_settlements_alert_repeat_seconds,
            )
            session.commit()
            if (
                settings.customer_settlements_alert_webhook_url
                and settings.customer_settlements_alert_task_id
            ):
                alert_delivery = dispatch_customer_settlement_alerts(
                    session,
                    webhook_url=settings.customer_settlements_alert_webhook_url,
                    task_id=settings.customer_settlements_alert_task_id,
                )
                session.commit()
    finally:
        session.close()
    overall_status = overall_health_status(metrics)
    print(
        json.dumps(
            {"status": overall_status, "metrics": metrics, "alerts": alert_delivery},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return {"ok": 0, "warning": 1, "critical": 2}[overall_status]


if __name__ == "__main__":
    raise SystemExit(main())
