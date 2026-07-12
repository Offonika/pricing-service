#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.core.config import get_settings

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
KNOWN_INCOMPLETE_BLOCKS = {"tasks", "daily_focus"}
UNHEALTHY_SOURCE_STATUSES = {"stale", "missing", "source_missing", "source_error", "error"}
UNHEALTHY_FRESHNESS_STATUSES = {"stale", "missing"}


def _request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    return client.request(method, path, headers=headers, json=json_body)


def _status_pair(payload: dict[str, Any]) -> tuple[str, str]:
    return (
        str(payload.get("source_status") or "missing"),
        str(payload.get("freshness_status") or "missing"),
    )


def _is_unhealthy(payload: dict[str, Any]) -> bool:
    source_status, freshness_status = _status_pair(payload)
    return (
        source_status in UNHEALTHY_SOURCE_STATUSES
        or freshness_status in UNHEALTHY_FRESHNESS_STATUSES
    )


def _is_partial(payload: dict[str, Any]) -> bool:
    source_status, freshness_status = _status_pair(payload)
    return source_status == "partial" or freshness_status == "partial"


def _validate_payload_shape(name: str, payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return [f"{name} response root is not an object"]

    errors: list[str] = []
    if name == "dashboard":
        if not isinstance(payload.get("blocks"), list):
            errors.append("dashboard response does not contain blocks")
        if not isinstance(payload.get("source_freshness"), list):
            errors.append("dashboard response does not contain source_freshness")
    elif name == "actions":
        if not isinstance(payload.get("payload"), list):
            errors.append("actions response does not contain a payload list")
        if not isinstance(payload.get("total_count"), int):
            errors.append("actions response does not contain total_count")
    elif name in {"cashflow", "profit_loss", "sales"}:
        for field, field_type in (
            ("source_status", str),
            ("freshness_status", str),
            ("daily", list),
            ("totals", dict),
        ):
            if not isinstance(payload.get(field), field_type):
                errors.append(f"{name} response has invalid or missing {field}")
    elif name == "management_balance":
        for field, field_type in (
            ("month", str),
            ("assets", list),
            ("liabilities", list),
            ("equity", list),
            ("validation_errors", list),
        ):
            if not isinstance(payload.get(field), field_type):
                errors.append(f"{name} response has invalid or missing {field}")
    return errors


def collect_runtime_checks(
    client: httpx.Client,
    *,
    requested_date: date,
    headers: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    errors: list[str] = []
    checks: list[dict[str, Any]] = []
    payloads: dict[str, dict[str, Any]] = {}

    page = _request(client, "GET", "/bitrix/executive-dashboard/")
    checks.append({"name": "page", "status_code": page.status_code, "bytes": len(page.content)})
    if page.status_code != 200 or 'id="root"' not in page.text:
        errors.append("dashboard page is unavailable or does not contain the application root")

    session_probe = _request(
        client,
        "POST",
        "/api/bitrix/executive-dashboard/session",
        json_body={},
    )
    checks.append({"name": "session_route", "status_code": session_probe.status_code})
    if session_probe.status_code == 404 or session_probe.status_code >= 500:
        errors.append("Bitrix dashboard session route is missing or failed")

    month_start = requested_date.replace(day=1).isoformat()
    endpoints = {
        "dashboard": f"/api/management/executive-dashboard?date={requested_date.isoformat()}",
        "actions": (
            "/api/management/executive-dashboard/actions"
            f"?date={requested_date.isoformat()}&status=open"
        ),
        "cashflow": (
            "/api/management/executive-dashboard/cashflow-period"
            f"?date_from={month_start}&date_to={requested_date.isoformat()}"
        ),
        "profit_loss": (
            "/api/management/executive-dashboard/profit-loss-period"
            f"?date_from={month_start}&date_to={requested_date.isoformat()}"
        ),
        "sales": f"/api/management/executive-dashboard/sales-period?month={month_start[:7]}",
        "management_balance": ("/api/management/executive-dashboard/management-balance"),
    }
    for name, path in endpoints.items():
        response = _request(client, "GET", path, headers=headers)
        item: dict[str, Any] = {"name": name, "status_code": response.status_code}
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                errors.append(f"{name} endpoint returned invalid JSON")
            else:
                shape_errors = _validate_payload_shape(name, payload)
                errors.extend(shape_errors)
                if isinstance(payload, dict):
                    payloads[name] = payload
                    item["source_status"] = payload.get("source_status")
                    item["freshness_status"] = payload.get("freshness_status")
        else:
            errors.append(f"{name} endpoint returned HTTP {response.status_code}")
        checks.append(item)

    return checks, payloads, errors


def evaluate_data_health(
    payloads: dict[str, dict[str, Any]],
    *,
    now: datetime,
    profit_loss_ready_after: time = time(4, 0),
    procurement_ready_after: time = time(11, 0),
    payables_ready_after: time = time(11, 0),
) -> tuple[str, list[dict[str, Any]], list[str]]:
    degraded_checks: list[dict[str, Any]] = []
    errors: list[str] = []

    cashflow = payloads.get("cashflow", {})
    if _is_unhealthy(cashflow):
        source_status, freshness_status = _status_pair(cashflow)
        errors.append(
            "cashflow data is unhealthy: "
            f"source_status={source_status}, freshness_status={freshness_status}"
        )
    elif _is_partial(cashflow):
        degraded_checks.append(
            {
                "name": "cashflow",
                "reason": "partial data",
                **dict(
                    zip(
                        ("source_status", "freshness_status"),
                        _status_pair(cashflow),
                        strict=True,
                    )
                ),
            }
        )

    profit_loss = payloads.get("profit_loss", {})
    if _is_unhealthy(profit_loss):
        source_status, freshness_status = _status_pair(profit_loss)
        local_now = now.astimezone(MOSCOW_TZ)
        if local_now.time() < profit_loss_ready_after:
            degraded_checks.append(
                {
                    "name": "profit_loss",
                    "reason": f"nightly refresh grace period until {profit_loss_ready_after.isoformat(timespec='minutes')}",
                    "source_status": source_status,
                    "freshness_status": freshness_status,
                }
            )
        else:
            errors.append(
                "profit_loss data is unhealthy after the refresh grace period: "
                f"source_status={source_status}, freshness_status={freshness_status}"
            )
    elif _is_partial(profit_loss):
        degraded_checks.append(
            {
                "name": "profit_loss",
                "reason": "partial data",
                **dict(
                    zip(
                        ("source_status", "freshness_status"),
                        _status_pair(profit_loss),
                        strict=True,
                    )
                ),
            }
        )

    management_balance = payloads.get("management_balance", {})
    if _is_unhealthy(management_balance):
        source_status, freshness_status = _status_pair(management_balance)
        degraded_checks.append(
            {
                "name": "management_balance",
                "reason": "monthly balance source is unavailable",
                "source_status": source_status,
                "freshness_status": freshness_status,
            }
        )

    if _is_partial(management_balance) or management_balance.get("validation_errors"):
        degraded_checks.append(
            {
                "name": "management_balance",
                "reason": "shadow mode: control sources are not fully reconciled",
                **dict(
                    zip(
                        ("source_status", "freshness_status"),
                        _status_pair(management_balance),
                        strict=True,
                    )
                ),
            }
        )

    dashboard = payloads.get("dashboard", {})
    for block in dashboard.get("blocks", []):
        if not isinstance(block, dict):
            continue
        key = str(block.get("key") or "unknown")
        if _is_unhealthy(block):
            source_status, freshness_status = _status_pair(block)
            if (
                key == "procurement_import"
                and now.astimezone(MOSCOW_TZ).time() >= procurement_ready_after
            ):
                errors.append(
                    "dashboard procurement_import is unhealthy after the refresh grace period: "
                    f"source_status={source_status}, freshness_status={freshness_status}"
                )
                continue
            if (
                key == "creditors_payables"
                and now.astimezone(MOSCOW_TZ).time() >= payables_ready_after
            ):
                errors.append(
                    "dashboard creditors_payables is unhealthy after the refresh grace period: "
                    f"source_status={source_status}, freshness_status={freshness_status}"
                )
                continue
            degraded_checks.append(
                {
                    "name": f"dashboard.{key}",
                    "reason": (
                        "known incomplete v1 block"
                        if key in KNOWN_INCOMPLETE_BLOCKS
                        else "dashboard source is stale or unavailable"
                    ),
                    "source_status": source_status,
                    "freshness_status": freshness_status,
                }
            )
        elif _is_partial(block):
            degraded_checks.append(
                {
                    "name": f"dashboard.{key}",
                    "reason": "partial data",
                    **dict(
                        zip(
                            ("source_status", "freshness_status"),
                            _status_pair(block),
                            strict=True,
                        )
                    ),
                }
            )

    data_status = "failed" if errors else "degraded" if degraded_checks else "healthy"
    return data_status, degraded_checks, errors


def _parse_clock(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected HH:MM") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Check the live executive dashboard surface.")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--mode", choices=("release", "monitor"), default="release")
    parser.add_argument("--profit-loss-ready-after", type=_parse_clock, default=time(4, 0))
    parser.add_argument("--procurement-ready-after", type=_parse_clock, default=time(11, 0))
    parser.add_argument("--payables-ready-after", type=_parse_clock, default=time(11, 0))
    args = parser.parse_args()

    settings = get_settings()
    token = (
        settings.management_internal_api_token
        or settings.counterparty_duplicate_internal_api_token
        or settings.return_scheme_internal_api_token
    )
    if not token:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "availability_status": "failed",
                    "data_status": "not_checked",
                    "errors": ["internal API token is missing"],
                }
            )
        )
        raise SystemExit(1)

    requested = date.fromisoformat(args.date)
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(
        base_url=args.base_url,
        timeout=args.timeout,
        trust_env=False,
        follow_redirects=True,
    ) as client:
        checks, payloads, availability_errors = collect_runtime_checks(
            client,
            requested_date=requested,
            headers=headers,
        )

    availability_status = "available" if not availability_errors else "failed"
    data_status = "not_checked"
    degraded_checks: list[dict[str, Any]] = []
    data_errors: list[str] = []
    if args.mode == "monitor" and not availability_errors:
        data_status, degraded_checks, data_errors = evaluate_data_health(
            payloads,
            now=datetime.now(tz=MOSCOW_TZ),
            profit_loss_ready_after=args.profit_loss_ready_after,
            procurement_ready_after=args.procurement_ready_after,
            payables_ready_after=args.payables_ready_after,
        )

    errors = availability_errors + data_errors
    result = {
        "status": "ok" if not errors else "failed",
        "mode": args.mode,
        "availability_status": availability_status,
        "data_status": data_status,
        "base_url": args.base_url,
        "date": args.date,
        "checks": checks,
        "degraded_checks": degraded_checks,
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "availability_status": "failed",
                    "data_status": "not_checked",
                    "errors": [f"runtime connection failed: {type(exc).__name__}: {exc}"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(1) from None
