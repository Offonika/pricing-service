from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.infrastructure.db import (
    build_onec_engine,
    get_application_session_factory,
)
from app.models.customer_settlement import CustomerSettlementMappingRevision
from app.services.customer_settlement_mapping import (
    build_mapping_entries,
    fetch_crm_cluster_rows,
    resolve_crm_counterparty_hashes,
)
from app.services.customer_settlement_reconciliation import (
    CustomerSettlementReconciliationError,
    customer_settlement_reconciliation_context_hash,
    customer_settlement_reconciliation_run_is_current,
)
from app.services.customer_settlement_reconciliation import (
    latest_customer_settlement_reconciliation as _latest_customer_settlement_reconciliation,
)
from app.services.customer_settlement_source import (
    CustomerSettlementSourceError,
    fetch_customer_settlement_balances,
    fetch_customer_settlement_scope_eligibility,
)
from app.services.customer_settlements import (
    CustomerSettlementContextBusyError,
    CustomerSettlementRuntimeGuardError,
    SettlementMappingInput,
    activate_financial_revision,
    activate_mapping_revision,
    active_pilot_counterparty_refs,
    active_pilot_site_user_ids,
    assert_expected_application_database,
    cleanup_customer_settlements,
    mark_financial_revision_failed,
    mark_mapping_revision_failed,
    replace_pilot_access_scope,
    try_customer_settlement_context_lock,
    utc_now,
)

_MAPPING_LOCK = "customer-settlements:mapping"
_FINANCIAL_LOCK = "customer-settlements:financial"


def _lock_key(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big", signed=True)


def _rollback_quietly(session: Session) -> None:
    try:
        session.rollback()
    except Exception:
        pass


def _close_quietly(session: Session) -> None:
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


def _source_timeout_is_bounded(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and 0 < float(value) <= 30


@contextmanager
def _advisory_lock(session: Session, name: str) -> Iterator[bool]:
    """Hold a worker lock until the current transaction commits or rolls back."""
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        yield True
        return
    key = _lock_key(name)
    acquired = bool(
        session.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}).scalar()
    )
    yield acquired


def run_customer_settlement_mapping_sync(
    *,
    settings: Settings | None = None,
) -> dict[str, object]:
    settings = settings or get_settings()
    if not (settings.customer_settlements_shadow_enabled or settings.customer_settlements_enabled):
        return {"status": "disabled"}
    mapping_mode = str(settings.customer_settlements_mapping_mode or "").strip().lower()
    access_mode = str(settings.customer_settlements_access_mode or "").strip().lower()
    max_scope_users = settings.customer_settlements_max_scope_users
    if access_mode not in {"pilot_whitelist", "all_linked"}:
        return {"status": "blocked", "reason": "unsupported_access_mode"}
    if access_mode == "all_linked" and mapping_mode != "crm_readonly":
        return {"status": "blocked", "reason": "all_linked_requires_crm_mapping"}
    if mapping_mode == "manual_confirmed":
        session = get_application_session_factory()()
        try:
            assert_expected_application_database(
                session,
                expected_database_name=settings.customer_settlements_expected_database_name,
            )
            revision = session.scalar(
                select(CustomerSettlementMappingRevision).where(
                    CustomerSettlementMappingRevision.status == "active",
                    CustomerSettlementMappingRevision.source_name == "manual_confirmed_pilot",
                )
            )
            if revision is None:
                return {"status": "blocked", "reason": "manual_mapping_not_imported"}
            return {
                "status": "unchanged",
                "revision_id": revision.id,
                "mapping_entries": revision.loaded_entry_count,
            }
        except CustomerSettlementRuntimeGuardError:
            _rollback_quietly(session)
            return {"status": "blocked", "reason": "runtime_database_guard_failed"}
        except Exception:
            _rollback_quietly(session)
            return {"status": "error", "reason": "mapping_sync_failed"}
        finally:
            _close_quietly(session)
    if mapping_mode != "crm_readonly":
        return {"status": "blocked", "reason": "unsupported_mapping_mode"}
    if not _source_timeout_is_bounded(settings.customer_settlements_query_timeout_seconds):
        return {"status": "blocked", "reason": "mapping_source_timeout_invalid"}
    session = get_application_session_factory()()
    onec_engine = None
    try:
        assert_expected_application_database(
            session,
            expected_database_name=settings.customer_settlements_expected_database_name,
        )
        with _advisory_lock(session, _MAPPING_LOCK) as acquired:
            if not acquired:
                return {"status": "skipped_lock"}
            if not settings.customer_settlements_crm_webhook_url:
                return {"status": "blocked", "reason": "crm_mapping_source_not_configured"}
            if not settings.onec_database_url:
                return {"status": "blocked", "reason": "onec_mapping_source_not_configured"}
            if not (
                settings.customer_settlements_organization_ref
                and settings.customer_settlements_organization_guid
            ):
                return {"status": "blocked", "reason": "mapping_organization_not_configured"}
            onec_engine = build_onec_engine(
                settings.onec_database_url,
                query_timeout_seconds=settings.customer_settlements_query_timeout_seconds,
                login_timeout_seconds=min(
                    settings.onec_login_timeout_seconds,
                    settings.customer_settlements_query_timeout_seconds,
                ),
                poolclass=NullPool,
            )
            rows = fetch_crm_cluster_rows(
                webhook_url=settings.customer_settlements_crm_webhook_url,
                timeout_seconds=settings.customer_settlements_crm_timeout_seconds,
            )
            rows = resolve_crm_counterparty_hashes(
                rows,
                onec_engine=onec_engine,
            )
            all_entries = build_mapping_entries(rows)
            entries_by_user = {item.site_user_id: item for item in all_entries}
            scope_eligibility = None
            if access_mode == "all_linked":
                linked_counterparty_refs = tuple(
                    item.counterparty_ref
                    for item in all_entries
                    if item.status == "linked" and item.counterparty_ref
                )
                scope_eligibility = fetch_customer_settlement_scope_eligibility(
                    onec_engine,
                    counterparty_refs=linked_counterparty_refs,
                    query_timeout_seconds=settings.customer_settlements_query_timeout_seconds,
                    max_counterparties=max_scope_users,
                )
            if not try_customer_settlement_context_lock(session):
                return {"status": "skipped_lock", "reason": "context_lock"}
            if access_mode == "all_linked":
                eligible_refs = set(scope_eligibility.eligible_counterparty_refs)
                entries = tuple(
                    item
                    for item in all_entries
                    if item.status == "linked" and item.counterparty_ref in eligible_refs
                )
                pilot_user_ids = tuple(item.site_user_id for item in entries)
            else:
                pilot_user_ids = active_pilot_site_user_ids(session)
                entries = tuple(
                    entries_by_user.get(user_id)
                    or SettlementMappingInput(
                        site_user_id=user_id,
                        cluster_id=None,
                        counterparty_ref=None,
                        status="not_linked",
                    )
                    for user_id in pilot_user_ids
                )
            if not pilot_user_ids:
                return {"status": "blocked", "reason": "pilot_users_not_configured"}
            if len(pilot_user_ids) > max_scope_users:
                return {"status": "blocked", "reason": "pilot_user_limit_exceeded"}
            invalid_source_rows = sum(
                row.has_invalid_site_user_id or row.has_invalid_counterparty_ref for row in rows
            )
            revision, activated = activate_mapping_revision(
                session,
                entries=entries,
                source_checked_at=utc_now(),
                organization_ref=str(settings.customer_settlements_organization_ref),
                organization_guid=str(settings.customer_settlements_organization_guid),
                max_scope_users=max_scope_users,
            )
            scope_changes = None
            if access_mode == "all_linked":
                scope_changes = replace_pilot_access_scope(
                    session,
                    site_user_ids=pilot_user_ids,
                    max_scope_users=max_scope_users,
                )
            session.commit()
            result = {
                "status": "activated" if activated else "unchanged",
                "revision_id": revision.id,
                "source_rows": len(rows),
                "pilot_rows": len(entries),
                "mapping_entries": revision.loaded_entry_count,
                "ambiguous_entries": revision.ambiguous_count,
                "invalid_source_rows": invalid_source_rows,
            }
            if scope_changes is not None:
                result["access_scope_changes"] = scope_changes
                result["scope_eligibility"] = {
                    "total_counterparties": scope_eligibility.total_counterparties,
                    "eligible_counterparties": len(scope_eligibility.eligible_counterparty_refs),
                    "blank_name_counterparties": (scope_eligibility.blank_name_counterparties),
                    "non_rub_counterparties": scope_eligibility.non_rub_counterparties,
                    "duration_seconds": round(scope_eligibility.duration_seconds, 3),
                }
            return result
    except CustomerSettlementRuntimeGuardError:
        _rollback_quietly(session)
        return {"status": "blocked", "reason": "runtime_database_guard_failed"}
    except Exception as exc:
        _rollback_quietly(session)
        try:
            mark_mapping_revision_failed(
                session,
                error_code="mapping_sync_failed",
                error_detail=type(exc).__name__,
            )
            session.commit()
        except Exception:
            _rollback_quietly(session)
        return {"status": "error", "reason": "mapping_sync_failed"}
    finally:
        _dispose_quietly(onec_engine)
        _close_quietly(session)


def run_customer_settlement_financial_sync(
    *,
    settings: Settings | None = None,
) -> dict[str, object]:
    settings = settings or get_settings()
    if not (settings.customer_settlements_shadow_enabled or settings.customer_settlements_enabled):
        return {"status": "disabled"}
    if not settings.customer_settlements_source_validated:
        return {"status": "blocked", "reason": "financial_source_not_validated"}
    if not _source_timeout_is_bounded(settings.customer_settlements_query_timeout_seconds):
        return {"status": "blocked", "reason": "financial_source_timeout_invalid"}
    required_config = (
        settings.customer_settlements_organization_ref,
        settings.customer_settlements_organization_guid,
        settings.customer_settlements_opening_organization_field,
        settings.customer_settlements_movement_organization_field,
        settings.onec_database_url,
    )
    if not all(required_config):
        return {"status": "blocked", "reason": "financial_source_not_configured"}

    session = get_application_session_factory()()
    onec_engine = None
    max_scope_users = settings.customer_settlements_max_scope_users
    try:
        assert_expected_application_database(
            session,
            expected_database_name=settings.customer_settlements_expected_database_name,
        )
        with _advisory_lock(session, _FINANCIAL_LOCK) as acquired:
            if not acquired:
                return {"status": "skipped_lock"}
            counterparty_refs = active_pilot_counterparty_refs(session)
            if not counterparty_refs:
                return {"status": "blocked", "reason": "pilot_counterparties_not_configured"}
            pilot_user_ids = active_pilot_site_user_ids(session)
            if len(pilot_user_ids) > max_scope_users:
                return {"status": "blocked", "reason": "pilot_user_limit_exceeded"}
            mapping_revision = session.scalar(
                select(CustomerSettlementMappingRevision).where(
                    CustomerSettlementMappingRevision.status == "active"
                )
            )
            if mapping_revision is None:
                return {"status": "blocked", "reason": "active_mapping_not_configured"}
            try:
                reconciliation_context_hash = customer_settlement_reconciliation_context_hash(
                    mapping_source_hash=mapping_revision.source_hash,
                    organization_ref=settings.customer_settlements_organization_ref,
                    organization_guid=settings.customer_settlements_organization_guid,
                    source_mode=settings.customer_settlements_source_mode,
                    opening_organization_field=(
                        settings.customer_settlements_opening_organization_field
                    ),
                    movement_organization_field=(
                        settings.customer_settlements_movement_organization_field
                    ),
                    counterparty_refs=counterparty_refs,
                    max_scope_users=max_scope_users,
                )
            except CustomerSettlementReconciliationError:
                return {"status": "blocked", "reason": "financial_reconciliation_not_current"}
            latest_reconciliation = _latest_customer_settlement_reconciliation(session)
            if not customer_settlement_reconciliation_run_is_current(
                latest_reconciliation,
                context_hash=reconciliation_context_hash,
                expected_count=len(counterparty_refs),
            ):
                return {"status": "blocked", "reason": "financial_reconciliation_not_current"}
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
                organization_ref=settings.customer_settlements_organization_ref,
                organization_guid=settings.customer_settlements_organization_guid,
                opening_organization_field=(
                    settings.customer_settlements_opening_organization_field
                ),
                movement_organization_field=(
                    settings.customer_settlements_movement_organization_field
                ),
                counterparty_refs=counterparty_refs,
                query_timeout_seconds=settings.customer_settlements_query_timeout_seconds,
                max_counterparties=max_scope_users,
            )
            if not try_customer_settlement_context_lock(session):
                return {"status": "skipped_lock", "reason": "context_lock"}
            current_counterparty_refs = active_pilot_counterparty_refs(session)
            current_pilot_user_ids = active_pilot_site_user_ids(session)
            current_mapping_revision = session.scalar(
                select(CustomerSettlementMappingRevision).where(
                    CustomerSettlementMappingRevision.status == "active"
                )
            )
            if (
                current_mapping_revision is None
                or current_mapping_revision.id != mapping_revision.id
                or current_mapping_revision.source_hash != mapping_revision.source_hash
                or current_counterparty_refs != counterparty_refs
                or current_pilot_user_ids != pilot_user_ids
            ):
                return {"status": "blocked", "reason": "financial_context_changed"}
            current_reconciliation = _latest_customer_settlement_reconciliation(session)
            if not customer_settlement_reconciliation_run_is_current(
                current_reconciliation,
                context_hash=reconciliation_context_hash,
                expected_count=len(current_counterparty_refs),
            ):
                return {
                    "status": "blocked",
                    "reason": "financial_reconciliation_not_current",
                }
            revision, activated = activate_financial_revision(
                session,
                organization_ref=settings.customer_settlements_organization_ref,
                as_of=source.as_of,
                source_db_time=source.source_db_time,
                source_mode=settings.customer_settlements_source_mode,
                expected_counterparty_refs=counterparty_refs,
                balances=source.balances,
                max_scope_users=max_scope_users,
            )
            session.commit()
            return {
                "status": "activated" if activated else "unchanged",
                "revision_id": revision.id,
                "loaded_rows": revision.loaded_row_count,
                "zero_rows": revision.zero_row_count,
                "isolation_level": source.isolation_level,
                "duration_seconds": round(source.duration_seconds, 3),
            }
    except CustomerSettlementRuntimeGuardError:
        _rollback_quietly(session)
        return {"status": "blocked", "reason": "runtime_database_guard_failed"}
    except Exception as exc:
        _rollback_quietly(session)
        try:
            mark_financial_revision_failed(
                session,
                organization_ref=settings.customer_settlements_organization_ref,
                organization_guid=settings.customer_settlements_organization_guid,
                as_of=utc_now(),
                source_mode=settings.customer_settlements_source_mode,
                error_code=(
                    str(exc)
                    if isinstance(exc, CustomerSettlementSourceError)
                    else "financial_sync_failed"
                ),
                error_detail=type(exc).__name__,
            )
            session.commit()
        except Exception:
            _rollback_quietly(session)
        return {"status": "error", "reason": "financial_sync_failed"}
    finally:
        _dispose_quietly(onec_engine)
        _close_quietly(session)


def run_customer_settlement_cleanup(
    *,
    settings: Settings | None = None,
) -> dict[str, object]:
    settings = settings or get_settings()
    session = get_application_session_factory()()
    try:
        assert_expected_application_database(
            session,
            expected_database_name=settings.customer_settlements_expected_database_name,
        )
        result = cleanup_customer_settlements(
            session,
            successful_retention_days=settings.customer_settlements_success_retention_days,
            failed_retention_days=settings.customer_settlements_failed_retention_days,
            jti_retention_hours=settings.customer_settlements_jti_retention_hours,
        )
        session.commit()
        return {"status": "ok", **result}
    except CustomerSettlementRuntimeGuardError:
        _rollback_quietly(session)
        return {"status": "blocked", "reason": "runtime_database_guard_failed"}
    except CustomerSettlementContextBusyError:
        _rollback_quietly(session)
        return {"status": "skipped_lock", "reason": "context_lock"}
    except Exception:
        _rollback_quietly(session)
        return {"status": "error", "reason": "cleanup_failed"}
    finally:
        _close_quietly(session)
