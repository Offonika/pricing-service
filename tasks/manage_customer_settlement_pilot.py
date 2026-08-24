from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db import get_application_session_factory
from app.models.customer_settlement import CustomerSettlementPilotAccess
from app.services.customer_settlements import (
    CustomerSettlementContextBusyError,
    CustomerSettlementRuntimeGuardError,
    assert_expected_application_database,
    normalize_site_user_id,
    set_pilot_access,
    try_customer_settlement_context_lock,
)


def _user_hash(site_user_id: str, salt: str | None) -> str:
    if not salt:
        raise ValueError("correlation_salt_is_not_configured")
    return hashlib.sha256(f"{salt}:{site_user_id}".encode()).hexdigest()[:16]


def _rollback_quietly(session: Session | None) -> bool:
    if session is None:
        return False
    try:
        session.rollback()
        return True
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safely preview or update the customer-settlement pilot whitelist."
    )
    parser.add_argument("--site-user-id", required=True)
    state = parser.add_mutually_exclusive_group(required=True)
    state.add_argument("--enable", action="store_true")
    state.add_argument("--disable", action="store_true")
    parser.add_argument("--reason")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    summary = {
        "audit_at": datetime.now(UTC).isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "enabled": bool(args.enable),
        "reason_present": bool(args.reason),
    }
    try:
        settings = get_settings()
        site_user_id = normalize_site_user_id(args.site_user_id)
        summary["user_hash"] = _user_hash(
            site_user_id,
            settings.customer_settlements_correlation_salt,
        )
    except Exception:
        summary.update({"status": "blocked", "error_code": "pilot_configuration_failed"})
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 2
    session = None
    commit_started = False
    committed = False
    try:
        session = get_application_session_factory()()
        assert_expected_application_database(
            session,
            expected_database_name=settings.customer_settlements_expected_database_name,
        )
        if not args.apply:
            if not try_customer_settlement_context_lock(session):
                raise CustomerSettlementContextBusyError("customer_settlement_context_busy")
            existing = session.scalar(
                select(CustomerSettlementPilotAccess).where(
                    CustomerSettlementPilotAccess.site_user_id == site_user_id
                )
            )
            previous_enabled = bool(existing.enabled) if existing is not None else None
            previous_reason = existing.reason if existing is not None else None
            requested_reason = str(args.reason).strip()[:255] if args.reason else None
            item, created = set_pilot_access(
                session,
                site_user_id=site_user_id,
                enabled=bool(args.enable),
                reason=args.reason,
            )
            session.refresh(item)
            preview_enabled = bool(item.enabled)
            preview_ok = preview_enabled == bool(args.enable)
            would_change = bool(
                created
                or previous_enabled != bool(args.enable)
                or previous_reason != requested_reason
            )
            rolled_back = _rollback_quietly(session)
            summary.update(
                {
                    "status": "validated" if preview_ok and rolled_back else "error",
                    "created": created,
                    "current_enabled": previous_enabled,
                    "preview_enabled": preview_enabled,
                    "preview_ok": preview_ok,
                    "would_change": would_change,
                    "rolled_back": rolled_back,
                }
            )
            if not rolled_back:
                summary["error_code"] = "pilot_dry_run_rollback_state_unknown"
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            return 0 if preview_ok and rolled_back else 1
        item, created = set_pilot_access(
            session,
            site_user_id=site_user_id,
            enabled=bool(args.enable),
            reason=args.reason,
        )
        commit_started = True
        session.commit()
        committed = True
        session.refresh(item)
        summary.update(
            {
                "created": created,
                "status": "applied",
                "readback_enabled": bool(item.enabled),
                "readback_ok": bool(item.enabled) == bool(args.enable),
            }
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0 if summary["readback_ok"] else 1
    except CustomerSettlementRuntimeGuardError:
        _rollback_quietly(session)
        summary.update({"status": "blocked", "error_code": "runtime_database_guard_failed"})
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 2
    except CustomerSettlementContextBusyError:
        _rollback_quietly(session)
        summary.update({"status": "blocked", "error_code": "settlement_context_busy"})
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 2
    except Exception:
        _rollback_quietly(session)
        summary.update(
            {
                "status": "error",
                "error_code": (
                    "pilot_update_committed_readback_failed"
                    if committed
                    else (
                        "pilot_update_commit_state_unknown"
                        if commit_started
                        else "pilot_update_failed"
                    )
                ),
            }
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 1
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
