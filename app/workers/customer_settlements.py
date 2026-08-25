from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import select, text
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
from app.services.customer_settlement_source import (
    CustomerSettlementSourceError,
    fetch_customer_settlement_balances,
)
from app.services.customer_settlements import (
    activate_financial_revision,
    activate_mapping_revision,
    active_pilot_counterparty_refs,
    cleanup_customer_settlements,
    mark_financial_revision_failed,
    mark_mapping_revision_failed,
    utc_now,
)

_MAPPING_LOCK = "customer-settlements:mapping"
_FINANCIAL_LOCK = "customer-settlements:financial"


def _lock_key(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big", signed=True)


@contextmanager
def _advisory_lock(session: Session, name: str) -> Iterator[bool]:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        yield True
        return
    key = _lock_key(name)
    acquired = bool(
        session.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar()
    )
    try:
        yield acquired
    finally:
        if acquired:
            session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


def run_customer_settlement_mapping_sync(
    *,
    settings: Settings | None = None,
) -> dict[str, object]:
    settings = settings or get_settings()
    if not (settings.customer_settlements_shadow_enabled or settings.customer_settlements_enabled):
        return {"status": "disabled"}
    mapping_mode = str(settings.customer_settlements_mapping_mode or "").strip().lower()
    if mapping_mode == "manual_confirmed":
        session = get_application_session_factory()()
        try:
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
        finally:
            session.close()
    if mapping_mode != "crm_readonly":
        return {"status": "blocked", "reason": "unsupported_mapping_mode"}
    session = get_application_session_factory()()
    onec_engine = None
    try:
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
            try:
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
                entries = build_mapping_entries(rows)
                invalid_source_rows = sum(
                    row.has_invalid_site_user_id or row.has_invalid_counterparty_ref for row in rows
                )
                revision, activated = activate_mapping_revision(
                    session,
                    entries=entries,
                    source_checked_at=utc_now(),
                    organization_ref=str(settings.customer_settlements_organization_ref),
                    organization_guid=str(settings.customer_settlements_organization_guid),
                )
                session.commit()
                return {
                    "status": "activated" if activated else "unchanged",
                    "revision_id": revision.id,
                    "source_rows": len(rows),
                    "mapping_entries": revision.loaded_entry_count,
                    "ambiguous_entries": revision.ambiguous_count,
                    "invalid_source_rows": invalid_source_rows,
                }
            except Exception as exc:
                session.rollback()
                mark_mapping_revision_failed(
                    session,
                    error_code="mapping_sync_failed",
                    error_detail=type(exc).__name__,
                )
                session.commit()
                return {"status": "error", "reason": "mapping_sync_failed"}
    finally:
        if onec_engine is not None:
            onec_engine.dispose()
        session.close()


def run_customer_settlement_financial_sync(
    *,
    settings: Settings | None = None,
) -> dict[str, object]:
    settings = settings or get_settings()
    if not (settings.customer_settlements_shadow_enabled or settings.customer_settlements_enabled):
        return {"status": "disabled"}
    if not settings.customer_settlements_source_validated:
        return {"status": "blocked", "reason": "financial_source_not_validated"}
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
    try:
        with _advisory_lock(session, _FINANCIAL_LOCK) as acquired:
            if not acquired:
                return {"status": "skipped_lock"}
            counterparty_refs = active_pilot_counterparty_refs(session)
            if not counterparty_refs:
                return {"status": "blocked", "reason": "pilot_counterparties_not_configured"}
            onec_engine = build_onec_engine(
                settings.onec_database_url,
                query_timeout_seconds=settings.customer_settlements_query_timeout_seconds,
                login_timeout_seconds=min(
                    settings.onec_login_timeout_seconds,
                    settings.customer_settlements_query_timeout_seconds,
                ),
                poolclass=NullPool,
            )
            try:
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
                )
                revision, activated = activate_financial_revision(
                    session,
                    organization_ref=settings.customer_settlements_organization_ref,
                    as_of=source.as_of,
                    source_db_time=source.source_db_time,
                    source_mode=settings.customer_settlements_source_mode,
                    expected_counterparty_refs=counterparty_refs,
                    balances=source.balances,
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
            except Exception as exc:
                session.rollback()
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
                    session.rollback()
                return {"status": "error", "reason": "financial_sync_failed"}
    finally:
        if onec_engine is not None:
            onec_engine.dispose()
        session.close()


def run_customer_settlement_cleanup(
    *,
    settings: Settings | None = None,
) -> dict[str, object]:
    settings = settings or get_settings()
    session = get_application_session_factory()()
    try:
        result = cleanup_customer_settlements(
            session,
            successful_retention_days=settings.customer_settlements_success_retention_days,
            failed_retention_days=settings.customer_settlements_failed_retention_days,
            jti_retention_hours=settings.customer_settlements_jti_retention_hours,
        )
        session.commit()
        return {"status": "ok", **result}
    finally:
        session.close()
