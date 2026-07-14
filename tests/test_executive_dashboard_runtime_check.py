from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_executive_dashboard_runtime import (  # noqa: E402
    collect_runtime_checks,
    evaluate_data_health,
)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _payloads() -> dict[str, dict]:
    return {
        "dashboard": {
            "source_status": "partial",
            "freshness_status": "partial",
            "blocks": [
                {
                    "key": "money_today",
                    "source_status": "ready",
                    "freshness_status": "fresh",
                },
                {
                    "key": "creditors_payables",
                    "source_status": "source_missing",
                    "freshness_status": "missing",
                },
                {
                    "key": "tasks",
                    "source_status": "ready",
                    "freshness_status": "fresh",
                },
                {
                    "key": "daily_focus",
                    "source_status": "ready",
                    "freshness_status": "fresh",
                },
            ],
            "source_freshness": [],
        },
        "actions": {
            "source_status": "empty",
            "freshness_status": "fresh",
            "total_count": 0,
            "payload": [],
        },
        "cashflow": {
            "source_status": "ready",
            "freshness_status": "fresh",
            "daily": [],
            "totals": {},
        },
        "profit_loss": {
            "source_status": "ready",
            "freshness_status": "fresh",
            "daily": [],
            "totals": {},
        },
        "sales": {
            "source_status": "ready",
            "freshness_status": "fresh",
            "daily": [],
            "totals": {},
        },
        "management_balance": {
            "month": "2026-07",
            "source_status": "partial",
            "freshness_status": "fresh",
            "assets": [],
            "liabilities": [],
            "equity": [],
            "validation_errors": [{"code": "shadow_mode"}],
            "source_summary": {
                "salary_reconciliation": {
                    "status": "partial",
                    "closing_blocked": True,
                }
            },
        },
        "service_accruals": {
            "month": "2026-07",
            "source_status": "ready",
            "freshness_status": "fresh",
            "total_count": 0,
            "items": [],
        },
    }


def _runtime_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/bitrix/executive-dashboard/":
        return httpx.Response(200, text='<html><div id="root"></div></html>')
    if path == "/api/bitrix/executive-dashboard/session":
        return httpx.Response(422, json={"detail": "expected for an empty probe"})

    payloads = _payloads()
    if path.endswith("/actions"):
        return httpx.Response(200, json=payloads["actions"])
    if path.endswith("/cashflow-period"):
        return httpx.Response(200, json=payloads["cashflow"])
    if path.endswith("/profit-loss-period"):
        return httpx.Response(200, json=payloads["profit_loss"])
    if path.endswith("/sales-period"):
        return httpx.Response(200, json=payloads["sales"])
    if path.endswith("/service-accruals"):
        return httpx.Response(200, json=payloads["service_accruals"])
    if path.endswith("/management-balance"):
        return httpx.Response(200, json=payloads["management_balance"])
    if path == "/api/management/executive-dashboard":
        return httpx.Response(200, json=payloads["dashboard"])
    return httpx.Response(404)


def test_collect_runtime_checks_accepts_empty_actions_and_session_probe_422() -> None:
    with httpx.Client(
        base_url="http://dashboard.test",
        transport=httpx.MockTransport(_runtime_handler),
    ) as client:
        checks, payloads, errors = collect_runtime_checks(
            client,
            requested_date=date(2026, 7, 11),
            headers={"Authorization": "Bearer test"},
        )

    assert not errors
    assert len(checks) == 9
    assert payloads["actions"]["payload"] == []


def test_collect_runtime_checks_rejects_http_and_schema_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/bitrix/executive-dashboard/":
            return httpx.Response(200, text='<div id="root"></div>')
        if request.url.path == "/api/bitrix/executive-dashboard/session":
            return httpx.Response(422, json={})
        if request.url.path.endswith("/cashflow-period"):
            return httpx.Response(500, json={})
        return httpx.Response(200, json={})

    with httpx.Client(
        base_url="http://dashboard.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        _, _, errors = collect_runtime_checks(
            client,
            requested_date=date(2026, 7, 11),
            headers={"Authorization": "Bearer test"},
        )

    assert "cashflow endpoint returned HTTP 500" in errors
    assert "dashboard response does not contain blocks" in errors
    assert "actions response does not contain a payload list" in errors


def test_monitor_fails_for_stale_cashflow() -> None:
    payloads = _payloads()
    payloads["cashflow"].update(source_status="stale", freshness_status="stale")

    status, degraded, errors = evaluate_data_health(
        payloads,
        now=datetime(2026, 7, 11, 1, 0, tzinfo=MOSCOW_TZ),
    )

    assert status == "failed"
    assert not degraded or all(item["name"] != "cashflow" for item in degraded)
    assert any("cashflow data is unhealthy" in error for error in errors)


def test_monitor_allows_profit_loss_grace_period_but_fails_after_0400() -> None:
    payloads = _payloads()
    payloads["profit_loss"].update(source_status="stale", freshness_status="stale")

    early_status, early_degraded, early_errors = evaluate_data_health(
        payloads,
        now=datetime(2026, 7, 11, 3, 59, tzinfo=MOSCOW_TZ),
    )
    late_status, _, late_errors = evaluate_data_health(
        payloads,
        now=datetime(2026, 7, 11, 4, 0, tzinfo=MOSCOW_TZ),
    )

    assert early_status == "degraded"
    assert not early_errors
    assert any(item["name"] == "profit_loss" for item in early_degraded)
    assert late_status == "failed"
    assert any("after the refresh grace period" in error for error in late_errors)


def test_monitor_reports_known_missing_blocks_and_partial_data_as_degraded_before_payables_sla() -> (
    None
):
    payloads = _payloads()
    payloads["profit_loss"].update(source_status="partial", freshness_status="partial")

    status, degraded, errors = evaluate_data_health(
        payloads,
        now=datetime(2026, 7, 11, 10, 59, tzinfo=MOSCOW_TZ),
    )

    assert status == "degraded"
    assert not errors
    names = {item["name"] for item in degraded}
    assert "profit_loss" in names
    assert "dashboard.creditors_payables" in names


def test_monitor_fails_for_unhealthy_payables_after_1100() -> None:
    payloads = _payloads()

    status, degraded, errors = evaluate_data_health(
        payloads,
        now=datetime(2026, 7, 11, 11, 0, tzinfo=MOSCOW_TZ),
    )

    assert status == "failed"
    assert not any(item["name"] == "dashboard.creditors_payables" for item in degraded)
    assert any("creditors_payables is unhealthy" in error for error in errors)


def test_monitor_allows_service_accrual_grace_but_fails_after_1100() -> None:
    payloads = _payloads()
    payloads["dashboard"]["blocks"][1].update(source_status="ready", freshness_status="fresh")
    payloads["service_accruals"].update(source_status="source_missing", freshness_status="missing")

    early_status, early_degraded, early_errors = evaluate_data_health(
        payloads,
        now=datetime(2026, 7, 11, 10, 59, tzinfo=MOSCOW_TZ),
    )
    late_status, _, late_errors = evaluate_data_health(
        payloads,
        now=datetime(2026, 7, 11, 11, 0, tzinfo=MOSCOW_TZ),
    )

    assert early_status == "degraded"
    assert not early_errors
    assert any(item["name"] == "service_accruals" for item in early_degraded)
    assert late_status == "failed"
    assert any("service accrual source is unhealthy" in item for item in late_errors)


def test_monitor_reports_other_stale_dashboard_blocks_without_false_outage() -> None:
    payloads = _payloads()
    payloads["dashboard"]["blocks"][1].update(source_status="ready", freshness_status="fresh")
    payloads["dashboard"]["blocks"].append(
        {
            "key": "debtors",
            "source_status": "stale",
            "freshness_status": "stale",
        }
    )

    status, degraded, errors = evaluate_data_health(
        payloads,
        now=datetime(2026, 7, 11, 12, 0, tzinfo=MOSCOW_TZ),
    )

    assert status == "degraded"
    assert not errors
    assert any(item["name"] == "dashboard.debtors" for item in degraded)


def test_monitor_allows_procurement_grace_period_but_fails_after_1100() -> None:
    payloads = _payloads()
    payloads["dashboard"]["blocks"].append(
        {
            "key": "procurement_import",
            "source_status": "stale",
            "freshness_status": "stale",
        }
    )

    early_status, early_degraded, early_errors = evaluate_data_health(
        payloads,
        now=datetime(2026, 7, 11, 10, 59, tzinfo=MOSCOW_TZ),
    )
    late_status, late_degraded, late_errors = evaluate_data_health(
        payloads,
        now=datetime(2026, 7, 11, 11, 0, tzinfo=MOSCOW_TZ),
    )

    assert early_status == "degraded"
    assert not early_errors
    assert any(item["name"] == "dashboard.procurement_import" for item in early_degraded)
    assert late_status == "failed"
    assert not any(item["name"] == "dashboard.procurement_import" for item in late_degraded)
    assert any("procurement_import is unhealthy" in error for error in late_errors)


def test_monitor_owner_control_issue_keeps_live_degraded_status() -> None:
    payloads = _payloads()
    next(
        block for block in payloads["dashboard"]["blocks"] if block["key"] == "creditors_payables"
    ).update(source_status="partial", freshness_status="partial")
    payloads["cashflow"].update(
        source_status="partial",
        freshness_status="partial",
        quality_issues=[
            {
                "issue_type": "owner_transfer_unmatched_incoming",
                "severity": "high",
                "status": "open",
            }
        ],
    )

    status, degraded, errors = evaluate_data_health(
        payloads,
        now=datetime(2026, 7, 11, 12, 0, tzinfo=MOSCOW_TZ),
    )

    assert status == "degraded"
    assert not errors
    assert any(item["name"] == "cashflow" for item in degraded)
