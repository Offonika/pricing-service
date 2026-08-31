from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.infrastructure.db.session import session_scope
from app.services.customer_return_bitrix import (
    CustomerReturnActionDependencyPending,
    CustomerReturnBitrixApi,
    CustomerReturnBitrixConfig,
    CustomerReturnBitrixWriter,
    build_delivery_plan,
    claim_due_actions,
    complete_claimed_action,
    deliver_plan,
    fail_claimed_action,
    load_claimed_action,
    preview_due_actions,
    safe_plan_dict,
)
from app.services.expertise_bitrix import BitrixRestClient, BitrixRestError

SessionScopeFactory = Callable[..., AbstractContextManager[Session]]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deliver customer return outbox actions to one Bitrix24 task."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply DB leases and Bitrix24 writes; default mode is a read-only dry-run.",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration without DB or network access.",
    )
    parser.add_argument("--limit", type=int, help="Override configured batch size.")
    parser.add_argument(
        "--database-url",
        help="Override the application database for tests or one-off local runs.",
    )
    parser.add_argument("--compact", action="store_true", help="Print compact JSON output.")
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    settings_override: Settings | None = None,
    api: CustomerReturnBitrixApi | None = None,
    session_scope_factory: SessionScopeFactory = session_scope,
    now: datetime | None = None,
) -> dict[str, Any]:
    args = parse_args(argv)
    settings = settings_override or get_settings()
    config = CustomerReturnBitrixConfig.from_settings(settings)
    mode = "check" if args.check else "apply" if args.apply else "dry_run"
    configuration = config.check(apply=args.apply or (args.check and config.writes_enabled))
    if args.check:
        result = {"mode": mode, **configuration}
        _print_result(result, compact=args.compact)
        return result
    if args.apply and not configuration["ready"]:
        raise SystemExit("customer return Bitrix worker configuration is incomplete")

    current_time = _as_utc(now or datetime.now(timezone.utc))
    limit = args.limit or config.batch_size
    if limit < 1 or limit > 200:
        raise SystemExit("customer return worker limit must be between 1 and 200")

    if not args.apply:
        result = _dry_run(
            session_scope_factory=session_scope_factory,
            database_url=args.database_url,
            now=current_time,
            limit=limit,
            configuration=configuration,
        )
        _print_result(result, compact=args.compact)
        return result

    resolved_api = api or CustomerReturnBitrixWriter(
        BitrixRestClient(str(config.webhook_url), retry_transient_html_403=True)
    )
    with session_scope_factory(
        read_only=False,
        database_url=args.database_url,
    ) as claim_session:
        claims = claim_due_actions(
            claim_session,
            now=current_time,
            limit=limit,
            lease_seconds=config.lease_seconds,
        )

    results: list[dict[str, Any]] = []
    for claim in claims:
        with session_scope_factory(
            read_only=False,
            database_url=args.database_url,
        ) as delivery_session:
            try:
                action = load_claimed_action(delivery_session, claim)
                plan = build_delivery_plan(action)
                external_reference = deliver_plan(resolved_api, plan, config=config)
                complete_claimed_action(
                    delivery_session,
                    claim,
                    external_reference=external_reference,
                    now=current_time,
                )
                results.append({"status": "completed", **safe_plan_dict(plan)})
            except Exception as exc:
                error_code = _safe_error_code(exc)
                failed_action = fail_claimed_action(
                    delivery_session,
                    claim,
                    error_code=error_code,
                    now=current_time,
                    max_attempts=config.max_attempts,
                )
                results.append(
                    {
                        "actionId": claim.action_id,
                        "status": failed_action.status,
                        "errorCode": error_code,
                        "attemptCount": failed_action.attempt_count,
                        "nextAttemptAt": (
                            failed_action.next_attempt_at.isoformat()
                            if failed_action.next_attempt_at
                            else None
                        ),
                    }
                )

    result = {
        "mode": mode,
        **configuration,
        "claimed": len(claims),
        "completed": sum(item["status"] == "completed" for item in results),
        "retryPending": sum(item["status"] == "pending" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
        "results": results,
    }
    _print_result(result, compact=args.compact)
    return result


def _dry_run(
    *,
    session_scope_factory: SessionScopeFactory,
    database_url: str | None,
    now: datetime,
    limit: int,
    configuration: dict[str, Any],
) -> dict[str, Any]:
    plans: list[dict[str, Any]] = []
    with session_scope_factory(read_only=True, database_url=database_url) as session:
        actions = preview_due_actions(session, now=now, limit=limit)
        for action in actions:
            try:
                plans.append(
                    {"status": "would_deliver", **safe_plan_dict(build_delivery_plan(action))}
                )
            except CustomerReturnActionDependencyPending as exc:
                plans.append(
                    {
                        "actionId": action.id,
                        "shipmentId": action.shipment_id,
                        "actionType": action.action_type,
                        "status": "dependency_pending",
                        "errorCode": str(exc),
                    }
                )
    return {
        "mode": "dry_run",
        **configuration,
        "count": len(plans),
        "wouldDeliver": sum(item["status"] == "would_deliver" for item in plans),
        "dependencyPending": sum(item["status"] == "dependency_pending" for item in plans),
        "results": plans,
    }


def _safe_error_code(exc: Exception) -> str:
    if isinstance(exc, BitrixRestError):
        return exc.code
    if isinstance(exc, CustomerReturnActionDependencyPending):
        return str(exc)
    value = exc.__class__.__name__.strip() or "customer_return_worker_error"
    return value[:128]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _print_result(result: dict[str, Any], *, compact: bool) -> None:
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=None if compact else 2,
            separators=(",", ":") if compact else None,
        )
    )


def _cli_exit_code(result: dict[str, Any]) -> int:
    if result.get("mode") == "check" and not result.get("ready", False):
        return 2
    if result.get("mode") == "apply" and result.get("failed", 0):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_exit_code(main()))
