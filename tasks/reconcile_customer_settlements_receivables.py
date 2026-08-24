from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.infrastructure.db import build_onec_engine, get_application_session_factory
from app.models.customer_settlement import CustomerSettlementMappingRevision
from app.services.customer_settlement_receivable_drift import (
    CustomerSettlementReceivableDriftError,
    build_customer_settlement_receivable_reconciliation,
)
from app.services.customer_settlement_reconciliation import (
    CustomerSettlementReconciliationError,
    customer_settlement_reconciliation_context_hash,
    end_of_day_boundary_utc,
    store_reconciliation_result,
)
from app.services.customer_settlement_source import (
    CustomerSettlementSourceError,
    fetch_customer_settlement_balances,
)
from app.services.customer_settlements import (
    CustomerSettlementRuntimeGuardError,
    active_pilot_counterparty_refs,
    assert_expected_application_database,
    try_customer_settlement_context_lock,
)
from tasks.check_customer_settlement_receivable_drift import (
    _build_readonly_receivable_engine,
    _load_receivable_rows,
    _read_database_url_from_env_file,
)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("completed date must be YYYY-MM-DD") from exc


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


def _dispose_quietly(engine: Engine | None) -> None:
    if engine is None:
        return
    try:
        engine.dispose()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Store an exact all-linked reconciliation from the receivables snapshot."
    )
    parser.add_argument(
        "--completed-date",
        type=_parse_date,
        default=None,
    )
    parser.add_argument(
        "--receivable-env-file",
        default=os.getenv("CUSTOMER_SETTLEMENTS_RECEIVABLE_ENV_FILE"),
    )
    parser.add_argument(
        "--expected-receivable-database-name",
        default=os.getenv("CUSTOMER_SETTLEMENTS_RECEIVABLE_EXPECTED_DATABASE_NAME"),
    )
    parser.add_argument(
        "--expected-scope-count",
        type=int,
        default=int(os.getenv("CUSTOMER_SETTLEMENTS_EXPECTED_PILOT_COUNT", "10")),
    )
    args = parser.parse_args(argv)

    session: Session | None = None
    onec_engine: Engine | None = None
    receivable_engine: Engine | None = None
    try:
        settings = get_settings()
        if (
            settings.environment.strip().lower() != "staging"
            or settings.customer_settlements_shadow_enabled is not True
            or settings.customer_settlements_mapping_mode != "crm_readonly"
            or settings.customer_settlements_access_mode != "all_linked"
        ):
            raise CustomerSettlementReceivableDriftError("reconciliation_runtime_guard_failed")
        if not settings.onec_database_url:
            raise CustomerSettlementReceivableDriftError("onec_source_not_configured")
        if not args.receivable_env_file:
            raise CustomerSettlementReceivableDriftError("receivable_env_file_missing")
        if not args.expected_receivable_database_name:
            raise CustomerSettlementReceivableDriftError(
                "expected_receivable_database_name_missing"
            )
        if not 0 < args.expected_scope_count <= settings.customer_settlements_max_scope_users:
            raise CustomerSettlementReceivableDriftError("invalid_expected_pilot_count")

        completed_date = args.completed_date or (datetime.now(MOSCOW_TZ).date() - timedelta(days=1))
        session = get_application_session_factory()()
        assert_expected_application_database(
            session,
            expected_database_name=settings.customer_settlements_expected_database_name,
        )
        mapping = session.scalar(
            select(CustomerSettlementMappingRevision).where(
                CustomerSettlementMappingRevision.status == "active"
            )
        )
        refs = active_pilot_counterparty_refs(session)
        if mapping is None or len(refs) != args.expected_scope_count:
            raise CustomerSettlementReceivableDriftError("pilot_count_mismatch")
        context_hash = customer_settlement_reconciliation_context_hash(
            mapping_source_hash=mapping.source_hash,
            organization_ref=str(settings.customer_settlements_organization_ref or ""),
            organization_guid=str(settings.customer_settlements_organization_guid or ""),
            source_mode=settings.customer_settlements_source_mode,
            opening_organization_field=str(
                settings.customer_settlements_opening_organization_field or ""
            ),
            movement_organization_field=str(
                settings.customer_settlements_movement_organization_field or ""
            ),
            counterparty_refs=refs,
            max_scope_users=settings.customer_settlements_max_scope_users,
        )

        onec_engine = build_onec_engine(
            settings.onec_database_url,
            query_timeout_seconds=settings.customer_settlements_query_timeout_seconds,
            login_timeout_seconds=min(
                settings.onec_login_timeout_seconds,
                settings.customer_settlements_query_timeout_seconds,
            ),
            poolclass=NullPool,
        )
        source = fetch_customer_settlement_balances(
            onec_engine,
            organization_ref=str(settings.customer_settlements_organization_ref or ""),
            organization_guid=str(settings.customer_settlements_organization_guid or ""),
            opening_organization_field=str(
                settings.customer_settlements_opening_organization_field or ""
            ),
            movement_organization_field=str(
                settings.customer_settlements_movement_organization_field or ""
            ),
            counterparty_refs=refs,
            query_timeout_seconds=settings.customer_settlements_query_timeout_seconds,
            as_of=end_of_day_boundary_utc(completed_date),
            max_counterparties=settings.customer_settlements_max_scope_users,
        )
        receivable_database_url = _read_database_url_from_env_file(str(args.receivable_env_file))
        receivable_engine = _build_readonly_receivable_engine(receivable_database_url)
        receivable_rows, receivable_total_rows = _load_receivable_rows(
            receivable_engine,
            completed_date=completed_date,
            counterparty_refs=refs,
            expected_database_name=str(args.expected_receivable_database_name),
        )
        result, drift = build_customer_settlement_receivable_reconciliation(
            context_hash=context_hash,
            source=source,
            completed_date=completed_date,
            expected_count=len(refs),
            receivable_rows=receivable_rows,
            receivable_total_rows=receivable_total_rows,
        )

        if not try_customer_settlement_context_lock(session):
            raise CustomerSettlementReconciliationError("settlement_context_busy")
        current_mapping = session.scalar(
            select(CustomerSettlementMappingRevision).where(
                CustomerSettlementMappingRevision.status == "active"
            )
        )
        current_refs = active_pilot_counterparty_refs(session)
        if current_mapping is None or current_refs != refs:
            raise CustomerSettlementReconciliationError("reconciliation_context_changed")
        current_context_hash = customer_settlement_reconciliation_context_hash(
            mapping_source_hash=current_mapping.source_hash,
            organization_ref=str(settings.customer_settlements_organization_ref or ""),
            organization_guid=str(settings.customer_settlements_organization_guid or ""),
            source_mode=settings.customer_settlements_source_mode,
            opening_organization_field=str(
                settings.customer_settlements_opening_organization_field or ""
            ),
            movement_organization_field=str(
                settings.customer_settlements_movement_organization_field or ""
            ),
            counterparty_refs=current_refs,
            max_scope_users=settings.customer_settlements_max_scope_users,
        )
        if current_context_hash != context_hash:
            raise CustomerSettlementReconciliationError("reconciliation_context_changed")
        store_reconciliation_result(session, result)
        try:
            session.commit()
        except Exception as exc:
            _rollback_quietly(session)
            raise CustomerSettlementReconciliationError(
                "reconciliation_commit_state_unknown"
            ) from exc
        print(
            json.dumps(
                {
                    "status": result.status,
                    "completed_date": completed_date.isoformat(),
                    "expected_count": result.expected_count,
                    "matched_count": result.matched_count,
                    "mismatch_count": result.mismatch_count,
                    "missing_zero_count": drift.missing_zero_count,
                    "receivable_present_count": drift.receivable_present_count,
                },
                sort_keys=True,
            )
        )
        return 0 if result.status == "matched" else 1
    except CustomerSettlementRuntimeGuardError:
        _rollback_quietly(session)
        print(
            json.dumps(
                {"status": "blocked", "error_code": "runtime_database_guard_failed"},
                sort_keys=True,
            )
        )
        return 2
    except (
        CustomerSettlementReceivableDriftError,
        CustomerSettlementReconciliationError,
        CustomerSettlementSourceError,
    ) as exc:
        _rollback_quietly(session)
        print(
            json.dumps(
                {"status": "blocked", "error_code": str(exc)[:96]},
                sort_keys=True,
            )
        )
        return 2
    except Exception:
        _rollback_quietly(session)
        print(
            json.dumps(
                {"status": "blocked", "error_code": "reconciliation_failed"},
                sort_keys=True,
            )
        )
        return 2
    finally:
        _close_quietly(session)
        _dispose_quietly(onec_engine)
        _dispose_quietly(receivable_engine)


if __name__ == "__main__":
    raise SystemExit(main())
