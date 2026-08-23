from __future__ import annotations

import json

from app.core.config import get_settings
from app.infrastructure.db import get_application_session_factory
from app.services.customer_settlement_alerts import (
    ALERT_TASK_ID,
    dispatch_customer_settlement_alerts,
    enqueue_health_alert_if_needed,
    overall_health_status,
)
from app.services.customer_settlements import (
    CustomerSettlementRuntimeGuardError,
    assert_expected_application_database,
    customer_settlement_health_metrics,
)


def _print_result(
    *,
    status: str,
    metrics: dict[str, object],
    alerts: dict[str, int],
    error_code: str | None = None,
) -> None:
    payload: dict[str, object] = {"status": status, "metrics": metrics, "alerts": alerts}
    if error_code:
        payload["error_code"] = error_code
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main() -> int:
    session = None
    alert_delivery = {"processed": 0, "sent": 0, "failed": 0, "exhausted": 0}
    metrics: dict[str, object] = {}
    try:
        settings = get_settings()
        session = get_application_session_factory()()
        assert_expected_application_database(
            session,
            expected_database_name=settings.customer_settlements_expected_database_name,
        )
        metrics = customer_settlement_health_metrics(
            session,
            stale_after_seconds=settings.customer_settlements_stale_after_seconds,
            hide_after_seconds=settings.customer_settlements_hide_after_seconds,
            mapping_stale_after_seconds=(settings.customer_settlements_mapping_stale_after_seconds),
        )
        if settings.customer_settlements_alerts_enabled:
            if (
                not settings.customer_settlements_alert_webhook_url
                or str(settings.customer_settlements_alert_task_id or "").strip() != ALERT_TASK_ID
            ):
                _print_result(
                    status="critical",
                    metrics=metrics,
                    alerts=alert_delivery,
                    error_code="alert_delivery_not_configured",
                )
                return 2
            enqueue_health_alert_if_needed(
                session,
                metrics=metrics,
                repeat_seconds=settings.customer_settlements_alert_repeat_seconds,
            )
            session.commit()
            alert_delivery = dispatch_customer_settlement_alerts(
                session,
                webhook_url=settings.customer_settlements_alert_webhook_url,
                task_id=str(settings.customer_settlements_alert_task_id),
            )
            session.commit()
            if alert_delivery["failed"] or alert_delivery["exhausted"]:
                _print_result(
                    status="critical",
                    metrics=metrics,
                    alerts=alert_delivery,
                    error_code="alert_delivery_failed",
                )
                return 2
        overall_status = overall_health_status(metrics)
        _print_result(status=overall_status, metrics=metrics, alerts=alert_delivery)
        return {"ok": 0, "warning": 1, "critical": 2}[overall_status]
    except CustomerSettlementRuntimeGuardError:
        if session is not None:
            session.rollback()
        _print_result(
            status="critical",
            metrics={},
            alerts=alert_delivery,
            error_code="runtime_database_guard_failed",
        )
        return 2
    except Exception:
        if session is not None:
            session.rollback()
        _print_result(
            status="critical",
            metrics={},
            alerts=alert_delivery,
            error_code="health_check_failed",
        )
        return 2
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
