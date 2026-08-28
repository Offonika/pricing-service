from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.infrastructure.db.session import session_scope
from app.services.expertise_bitrix import BitrixRestClient, BitrixRestError
from app.services.site_service_request_email_worker import (
    process_site_service_email_events,
    resolved_site_service_email_field_map,
)
from app.services.site_service_requests import (
    SiteServiceRequestCipher,
    build_site_service_request_cipher,
    record_site_service_request_worker_failure,
    record_site_service_request_worker_started,
    record_site_service_request_worker_success,
)
from app.services.site_service_requests_worker import (
    SiteServiceRequestBitrixApi,
    SiteServiceRequestBitrixReader,
    SiteServiceRequestBitrixWriter,
    SiteServiceRequestFileCleanup,
    apply_site_service_request_worker_plans,
    build_site_service_request_worker_plans,
    cleanup_uploaded_site_service_request_files,
    collect_site_service_request_outbound_commands,
    deliver_site_service_request_daily_report,
    preflight_site_service_request_users,
    reconcile_site_service_request_assignments,
    resolved_site_service_request_field_map,
    safe_site_service_request_plan_dict,
    sync_staged_site_service_request_files,
    validate_site_service_request_enum_map,
)

SessionScopeFactory = Callable[..., AbstractContextManager[Session]]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synchronize site service requests with the Bitrix service queue."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply DB and Bitrix writes; default mode is read-only dry-run.",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration without DB or network access.",
    )
    parser.add_argument("--limit", type=int, help="Override configured worker batch size.")
    parser.add_argument("--compact", action="store_true", help="Print compact JSON output.")
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    settings_override: Settings | None = None,
    api: SiteServiceRequestBitrixApi | None = None,
    session_scope_factory: SessionScopeFactory = session_scope,
) -> dict[str, Any]:
    args = parse_args(argv)
    settings = settings_override or get_settings()
    mode = "check" if args.check else "apply" if args.apply else "dry_run"
    if args.apply:
        with session_scope_factory(read_only=False) as heartbeat_session:
            record_site_service_request_worker_started(
                heartbeat_session,
                now=datetime.now(UTC),
            )
    try:
        result = _run_worker(
            args=args,
            settings=settings,
            mode=mode,
            api=api,
            session_scope_factory=session_scope_factory,
        )
    except BaseException as exc:
        if args.apply:
            try:
                with session_scope_factory(read_only=False) as heartbeat_session:
                    record_site_service_request_worker_failure(
                        heartbeat_session,
                        error_code=_safe_worker_failure_code(exc),
                        now=datetime.now(UTC),
                    )
            except Exception:
                # Never replace the original worker failure with a secondary
                # heartbeat-storage exception.
                pass
        raise
    if args.apply:
        with session_scope_factory(read_only=False) as heartbeat_session:
            record_site_service_request_worker_success(
                heartbeat_session,
                now=datetime.now(UTC),
            )
    _print_result(result, compact=args.compact)
    return result


def _run_worker(
    *,
    args: argparse.Namespace,
    settings: Settings,
    mode: str,
    api: SiteServiceRequestBitrixApi | None,
    session_scope_factory: SessionScopeFactory,
) -> dict[str, Any]:
    check = _configuration_check(
        settings,
        apply=args.apply or (args.check and settings.site_service_requests_bitrix_writes_enabled),
    )
    if args.check:
        return {"mode": mode, **check}
    if not check["ready"]:
        raise SystemExit("site service request worker configuration is incomplete")

    resolved_api = api or BitrixRestClient(
        str(settings.site_service_requests_bitrix_webhook_url),
        retry_transient_html_403=True,
    )
    user_preflight = (
        preflight_site_service_request_users(api=resolved_api, settings=settings)
        if args.apply
        else []
    )
    reader = SiteServiceRequestBitrixReader(resolved_api)
    cipher = build_site_service_request_cipher(settings)
    cleanup_paths: list[SiteServiceRequestFileCleanup] = []
    with session_scope_factory(read_only=not args.apply) as session:
        planning_failures: list[Any] = []
        plans = build_site_service_request_worker_plans(
            session,
            settings=settings,
            reader=reader,
            cipher=cipher,
            limit=args.limit,
            failure_results=planning_failures if args.apply else None,
            failure_writer=(SiteServiceRequestBitrixWriter(resolved_api) if args.apply else None),
        )
        if args.apply:
            site_results = [
                *planning_failures,
                *apply_site_service_request_worker_plans(
                    session,
                    plans=plans,
                    settings=settings,
                    reader=reader,
                    writer=SiteServiceRequestBitrixWriter(resolved_api),
                    cipher=cipher,
                ),
            ]
            email_results = process_site_service_email_events(
                session,
                settings=settings,
                api=resolved_api,
                cipher=cipher,
                limit=args.limit,
            )
            results = [*site_results, *email_results]
            assignments = reconcile_site_service_request_assignments(
                session,
                settings=settings,
                reader=reader,
                writer=SiteServiceRequestBitrixWriter(resolved_api),
                limit=args.limit,
            )
            files = sync_staged_site_service_request_files(
                session,
                settings=settings,
                writer=SiteServiceRequestBitrixWriter(resolved_api),
                limit=args.limit,
                cleanup_paths=cleanup_paths,
            )
            # File rows and the corresponding Bitrix readback must be durable
            # before outbound polling starts committing per-card checkpoints.
            session.commit()
            cleanup_uploaded_site_service_request_files(session, cleanup_paths)
            session.commit()
            cleanup_paths.clear()
            commands = collect_site_service_request_outbound_commands(
                session,
                settings=settings,
                writer=SiteServiceRequestBitrixWriter(resolved_api),
                cipher=cipher,
                limit=args.limit,
            )
            daily_report = deliver_site_service_request_daily_report(
                session,
                settings=settings,
                writer=SiteServiceRequestBitrixWriter(resolved_api),
            )
            result = {
                "mode": mode,
                "count": len(results),
                "results": [
                    {
                        "eventId": item.event_id,
                        "status": item.status,
                        "bitrixItemId": item.bitrix_item_id,
                        "errorCode": item.error_code,
                    }
                    for item in results
                ],
                "emailCount": len(email_results),
                "assignments": assignments,
                "files": files,
                "commands": commands,
                "dailyReport": daily_report,
                "userPreflight": user_preflight,
            }
        else:
            result = {
                "mode": mode,
                "count": len(plans),
                "plans": [safe_site_service_request_plan_dict(plan) for plan in plans],
            }
    return result


def _safe_worker_failure_code(exc: BaseException) -> str:
    if isinstance(exc, BitrixRestError):
        return exc.code
    if isinstance(exc, SQLAlchemyError):
        return "worker_storage_unavailable"
    if isinstance(exc, SystemExit):
        return "worker_configuration_error"
    return "worker_failure"


def _configuration_check(settings: Settings, *, apply: bool) -> dict[str, Any]:
    errors: list[str] = []
    if not settings.site_service_requests_bitrix_webhook_url:
        errors.append("bitrix_webhook_missing")
    if not settings.site_service_requests_event_encryption_key:
        errors.append("encryption_key_missing")
    else:
        try:
            SiteServiceRequestCipher(settings.site_service_requests_event_encryption_key)
        except RuntimeError:
            errors.append("encryption_key_invalid")
    if not settings.site_service_requests_first_line_user_ids:
        errors.append("first_line_users_missing")
    if apply:
        if settings.site_service_requests_escalation_user_id is None:
            errors.append("escalation_user_missing")
        if settings.site_service_requests_finance_user_id is None:
            errors.append("finance_user_missing")
        if (
            settings.site_service_requests_daily_report_enabled
            and not str(settings.site_service_requests_daily_report_dialog_id or "").strip()
        ):
            errors.append("daily_report_dialog_missing")
        if settings.site_service_requests_bitrix_root_folder_id is None:
            errors.append("bitrix_root_folder_missing")
        configured_user_ids = {
            *settings.site_service_requests_first_line_user_ids,
            *(
                [settings.site_service_requests_escalation_user_id]
                if settings.site_service_requests_escalation_user_id is not None
                else []
            ),
            *(
                [settings.site_service_requests_finance_user_id]
                if settings.site_service_requests_finance_user_id is not None
                else []
            ),
        }
        if any(
            not str(
                settings.site_service_requests_expected_user_names.get(str(user_id)) or ""
            ).strip()
            for user_id in configured_user_ids
        ):
            errors.append("expected_user_names_incomplete")
        if not settings.site_service_requests_bitrix_writes_enabled:
            errors.append("bitrix_writes_disabled")
        try:
            field_map = resolved_site_service_request_field_map(settings)
        except RuntimeError:
            errors.append("bitrix_field_map_incomplete")
            field_map = {}
        if settings.site_service_requests_email_ingest_enabled:
            try:
                resolved_site_service_email_field_map(settings)
            except RuntimeError:
                errors.append("bitrix_email_field_map_incomplete")
        try:
            validate_site_service_request_enum_map(settings)
        except RuntimeError:
            errors.append("bitrix_enum_map_incomplete")
        if not settings.site_service_requests_bitrix_stage_map.get("new"):
            errors.append("bitrix_new_stage_missing")
        if not settings.site_service_requests_bitrix_stage_map.get("success"):
            errors.append("bitrix_success_stage_missing")
        if not settings.site_service_requests_bitrix_stage_map.get("failure"):
            errors.append("bitrix_failure_stage_missing")
        if settings.site_service_requests_outbound_replies_enabled:
            if any(
                not field_map.get(key)
                for key in ("site_reply_text", "site_reply_action", "site_reply_status")
            ):
                errors.append("outbound_field_map_incomplete")
            if any(
                not settings.site_service_requests_bitrix_enum_map.get(key)
                for key in (
                    "reply_action_send",
                    "reply_status_pending",
                    "reply_status_sent",
                    "reply_status_error",
                )
            ):
                errors.append("outbound_enum_map_incomplete")
    return {
        "ready": not errors,
        "errors": errors,
        "bitrixWritesEnabled": settings.site_service_requests_bitrix_writes_enabled,
        "emailIngestEnabled": settings.site_service_requests_email_ingest_enabled,
        "outboundRepliesEnabled": settings.site_service_requests_outbound_replies_enabled,
        "dailyReportEnabled": settings.site_service_requests_daily_report_enabled,
    }


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
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_exit_code(main()))
