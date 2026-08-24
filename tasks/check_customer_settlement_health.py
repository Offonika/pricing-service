from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db import get_application_session_factory
from app.services.customer_settlement_alerts import (
    ALERT_TASK_ID,
    dispatch_customer_settlement_alerts,
    enqueue_health_alert_if_needed,
    overall_health_status,
    validate_customer_settlement_alert_webhook_url,
)
from app.services.customer_settlement_reconciliation import (
    active_customer_settlement_reconciliation_is_current,
)
from app.services.customer_settlements import (
    CustomerSettlementRuntimeGuardError,
    assert_expected_application_database,
    customer_settlement_health_metrics,
    validate_customer_settlement_freshness_contract,
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


def _rollback_quietly(session: Session | None) -> None:
    if session is None:
        return
    try:
        session.rollback()
    except Exception:
        pass


def _close_quietly(session: Session | None) -> None:
    if session is None:
        return
    try:
        session.close()
    except Exception:
        pass


def main() -> int:
    session = None
    alert_delivery = {"processed": 0, "sent": 0, "failed": 0, "exhausted": 0}
    metrics: dict[str, object] = {}
    try:
        settings = get_settings()
        validate_customer_settlement_freshness_contract(
            stale_after_seconds=settings.customer_settlements_stale_after_seconds,
            hide_after_seconds=settings.customer_settlements_hide_after_seconds,
            mapping_stale_after_seconds=(settings.customer_settlements_mapping_stale_after_seconds),
        )
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
            expected_source_mode=settings.customer_settlements_source_mode,
            expected_mapping_source_name=(
                "bitrix_crm_customer_cluster"
                if settings.customer_settlements_mapping_mode == "crm_readonly"
                else (
                    "manual_confirmed_pilot"
                    if settings.customer_settlements_mapping_mode == "manual_confirmed"
                    else ""
                )
            ),
            expected_source_system="ut103",
            expected_organization_ref=settings.customer_settlements_organization_ref,
            expected_organization_guid=settings.customer_settlements_organization_guid,
            max_scope_users=settings.customer_settlements_max_scope_users,
        )
        reconciliation_current = (
            settings.customer_settlements_source_validated
            and active_customer_settlement_reconciliation_is_current(
                session,
                organization_ref=str(settings.customer_settlements_organization_ref or ""),
                organization_guid=str(settings.customer_settlements_organization_guid or ""),
                source_mode=settings.customer_settlements_source_mode,
                opening_organization_field=str(
                    settings.customer_settlements_opening_organization_field or ""
                ),
                movement_organization_field=str(
                    settings.customer_settlements_movement_organization_field or ""
                ),
                max_scope_users=settings.customer_settlements_max_scope_users,
            )
        )
        metrics["reconciliation_current"] = reconciliation_current
        if not reconciliation_current:
            metrics["freshness_status"] = "critical"
            metrics["mapping_status"] = "critical"
        if settings.customer_settlements_alerts_enabled:
            try:
                alert_webhook_url = validate_customer_settlement_alert_webhook_url(
                    settings.customer_settlements_alert_webhook_url
                )
            except RuntimeError:
                alert_webhook_url = None
            if alert_webhook_url is None or (
                str(settings.customer_settlements_alert_task_id or "").strip() != ALERT_TASK_ID
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
                webhook_url=alert_webhook_url,
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
        _rollback_quietly(session)
        _print_result(
            status="critical",
            metrics={},
            alerts=alert_delivery,
            error_code="runtime_database_guard_failed",
        )
        return 2
    except Exception:
        _rollback_quietly(session)
        _print_result(
            status="critical",
            metrics={},
            alerts=alert_delivery,
            error_code="health_check_failed",
        )
        return 2
    finally:
        _close_quietly(session)


if __name__ == "__main__":
    raise SystemExit(main())
