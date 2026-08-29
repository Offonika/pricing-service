from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.infrastructure.db import build_onec_engine, get_application_session_factory
from app.models.customer_settlement import CustomerSettlementMappingRevision
from app.services.customer_settlement_reconciliation import (
    CustomerSettlementReconciliationError,
    customer_settlement_reconciliation_context_hash,
    end_of_day_boundary_utc,
    reconcile_customer_settlement_rows,
    report_sha256,
    store_reconciliation_result,
)
from app.services.customer_settlement_source import (
    CustomerSettlementSourceError,
    fetch_customer_settlement_balances,
    fetch_manual_customer_settlement_controls,
)
from app.services.customer_settlements import (
    CustomerSettlementRuntimeGuardError,
    active_pilot_counterparty_refs,
    assert_expected_application_database,
    onec_ref_to_guid,
    try_customer_settlement_context_lock,
)
from app.services.importers.onec_mutual_settlements import (
    load_onec_mutual_settlements_current_balances_file,
    onec_mutual_settlements_report_file_allows_implicit_zero_rows,
)


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
        description="Compare a completed-day 1C statement with a read-only exact source slice."
    )
    parser.add_argument("report_path", type=Path)
    args = parser.parse_args(argv)
    session = None
    onec_engine = None
    try:
        settings = get_settings()
        if not settings.onec_database_url:
            raise CustomerSettlementReconciliationError("onec_source_not_configured")
        initial_report_hash = report_sha256(args.report_path)
        try:
            report_rows = load_onec_mutual_settlements_current_balances_file(
                args.report_path,
                counterparty_filter_mode="all",
            )
            report_allows_implicit_zero_rows = (
                onec_mutual_settlements_report_file_allows_implicit_zero_rows(args.report_path)
            )
        except Exception as exc:
            raise CustomerSettlementReconciliationError("report_parse_failed") from exc
        if not report_rows:
            raise CustomerSettlementReconciliationError("report_has_no_counterparties")
        report_date = report_rows[0].snapshot_date
        session = get_application_session_factory()()
        assert_expected_application_database(
            session,
            expected_database_name=settings.customer_settlements_expected_database_name,
        )
        active_mapping = session.scalar(
            select(CustomerSettlementMappingRevision).where(
                CustomerSettlementMappingRevision.status == "active"
            )
        )
        if active_mapping is None:
            raise CustomerSettlementReconciliationError("active_mapping_is_missing")
        refs = active_pilot_counterparty_refs(session)
        if not refs or len(refs) > settings.customer_settlements_max_scope_users:
            raise CustomerSettlementReconciliationError("pilot_count_is_invalid")
        context_hash = customer_settlement_reconciliation_context_hash(
            mapping_source_hash=active_mapping.source_hash,
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
            login_timeout_seconds=min(settings.customer_settlements_query_timeout_seconds, 6),
            poolclass=NullPool,
        )
        controls = fetch_manual_customer_settlement_controls(
            onec_engine,
            organization_ref=str(settings.customer_settlements_organization_ref or ""),
            organization_guid=str(settings.customer_settlements_organization_guid or ""),
            counterparty_guids=[onec_ref_to_guid(value) for value in refs],
            counterparty_inn_field=settings.customer_settlements_counterparty_inn_field,
            query_timeout_seconds=settings.customer_settlements_query_timeout_seconds,
            max_counterparties=settings.customer_settlements_max_scope_users,
        )
        source = fetch_customer_settlement_balances(
            onec_engine,
            organization_ref=str(settings.customer_settlements_organization_ref or ""),
            organization_guid=settings.customer_settlements_organization_guid,
            opening_organization_field=str(
                settings.customer_settlements_opening_organization_field or ""
            ),
            movement_organization_field=str(
                settings.customer_settlements_movement_organization_field or ""
            ),
            counterparty_refs=refs,
            query_timeout_seconds=settings.customer_settlements_query_timeout_seconds,
            as_of=end_of_day_boundary_utc(report_date),
            max_counterparties=settings.customer_settlements_max_scope_users,
        )
        final_report_hash = report_sha256(args.report_path)
        if final_report_hash != initial_report_hash:
            raise CustomerSettlementReconciliationError("report_changed_during_reconciliation")
        result = reconcile_customer_settlement_rows(
            report_hash=initial_report_hash,
            context_hash=context_hash,
            report_rows=report_rows,
            controls=controls,
            source=source,
            report_allows_implicit_zero_rows=report_allows_implicit_zero_rows,
            max_scope_users=settings.customer_settlements_max_scope_users,
            aggregate_duplicate_names=(settings.customer_settlements_access_mode == "all_linked"),
        )
        if not try_customer_settlement_context_lock(session):
            raise CustomerSettlementReconciliationError("settlement_context_busy")
        current_mapping = session.scalar(
            select(CustomerSettlementMappingRevision).where(
                CustomerSettlementMappingRevision.status == "active"
            )
        )
        current_refs = active_pilot_counterparty_refs(session)
        if current_mapping is None or not current_refs:
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
                    "report_date": result.report_date.isoformat(),
                    "expected_count": result.expected_count,
                    "matched_count": result.matched_count,
                    "mismatch_count": result.mismatch_count,
                    "within_tolerance": result.mismatch_count == 0,
                },
                sort_keys=True,
            )
        )
        return 0 if result.mismatch_count == 0 else 2
    except CustomerSettlementRuntimeGuardError:
        _rollback_quietly(session)
        print(
            json.dumps(
                {"status": "blocked", "error_code": "runtime_database_guard_failed"},
                sort_keys=True,
            )
        )
        return 2
    except (CustomerSettlementReconciliationError, CustomerSettlementSourceError, OSError) as exc:
        _rollback_quietly(session)
        error_code = (
            str(exc)[:96]
            if isinstance(
                exc,
                (CustomerSettlementReconciliationError, CustomerSettlementSourceError),
            )
            else "report_io_failed"
        )
        print(
            json.dumps(
                {"status": "blocked", "error_code": error_code},
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


if __name__ == "__main__":
    raise SystemExit(main())
