from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import distinct, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.infrastructure.db import get_application_session_factory
from app.models.customer_settlement import (
    CustomerAccount,
    CustomerAccountSiteBinding,
    CustomerAccountSourceBinding,
    CustomerSettlementBalance,
    CustomerSettlementMappingEntry,
    CustomerSettlementMappingRevision,
    CustomerSettlementPilotAccess,
    CustomerSettlementReconciliationRun,
    CustomerSettlementRevision,
)
from app.services.customer_settlement_mapping import (
    CustomerSettlementMappingSourceError,
    validate_crm_mapping_webhook_url,
)
from app.services.customer_settlement_reconciliation import (
    CustomerSettlementReconciliationError,
    customer_settlement_reconciliation_context_hash,
    customer_settlement_reconciliation_input_hash,
    customer_settlement_reconciliation_run_is_current,
    latest_customer_settlement_reconciliation,
)
from app.services.customer_settlements import (
    CustomerSettlementContextBusyError,
    active_pilot_counterparty_refs,
    customer_settlement_health_metrics,
    try_customer_settlement_context_read_lock,
)

EXPECTED_ALEMBIC_REVISION = "6e8f0a2b4c6d"
EXPECTED_ORGANIZATION_FIELD = "_Fld7005RRef"
EXPECTED_SOURCE_MODE = "onec_canonical_mutual_statement_7002"
DEFAULT_EXPECTED_DATABASE_NAME = "settlements_stage"
DEFAULT_EXPECTED_ORGANIZATION_REF = "0xb34a0025901e48ef11e211128227ea80"
DEFAULT_EXPECTED_ORGANIZATION_GUID = "8227ea80-1112-11e2-b34a-0025901e48ef"


def _check(name: str, ok: bool) -> dict[str, object]:
    return {"name": name, "ok": bool(ok)}


def _reconciliation_difference_is_acceptable(value: Any) -> bool:
    try:
        difference = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return False
    return difference.is_finite() and Decimal("0") <= difference <= Decimal("0.01")


def _reconciliation_hashes_are_complete(
    reconciliation: CustomerSettlementReconciliationRun | None,
) -> bool:
    if reconciliation is None:
        return False
    report_hash = str(reconciliation.report_hash or "")
    context_hash = str(reconciliation.context_hash or "")
    source_hash = str(reconciliation.source_hash or "")
    input_hash = str(reconciliation.input_hash or "")
    try:
        expected_input_hash = customer_settlement_reconciliation_input_hash(
            report_hash=report_hash,
            context_hash=context_hash,
            source_hash=source_hash,
        )
    except CustomerSettlementReconciliationError:
        return False
    return (
        all(
            value == value.strip().lower()
            for value in (report_hash, context_hash, source_hash, input_hash)
        )
        and input_hash == expected_input_hash
    )


def _crm_mapping_source_is_configured(settings: Settings) -> bool:
    if settings.customer_settlements_mapping_mode != "crm_readonly":
        return False
    try:
        validate_crm_mapping_webhook_url(settings.customer_settlements_crm_webhook_url)
    except CustomerSettlementMappingSourceError:
        return False
    return True


def _configuration_checks(
    settings: Settings,
    *,
    phase: str,
    expected_database_name: str,
    expected_organization_ref: str,
    expected_organization_guid: str,
) -> list[dict[str, object]]:
    try:
        database_backend = make_url(settings.database_url).get_backend_name()
    except Exception:
        database_backend = ""
    return [
        _check("environment_is_staging", settings.environment.strip().lower() == "staging"),
        _check("client_api_disabled", settings.customer_settlements_enabled is False),
        _check(
            "eligibility_api_disabled",
            settings.customer_settlements_eligibility_enabled is False,
        ),
        _check("shadow_worker_enabled", settings.customer_settlements_shadow_enabled is True),
        _check(
            (
                "source_reconciliation_gate_closed"
                if phase == "bootstrap"
                else "source_reconciliation_gate_open"
            ),
            (
                settings.customer_settlements_source_validated is False
                if phase == "bootstrap"
                else settings.customer_settlements_source_validated is True
            ),
        ),
        _check("application_database_is_postgresql", database_backend == "postgresql"),
        _check(
            "expected_database_guard_matches_staging",
            str(settings.customer_settlements_expected_database_name or "").strip()
            == expected_database_name,
        ),
        _check("onec_source_configured", bool(settings.onec_database_url)),
        _check(
            "mapping_source_configured",
            _crm_mapping_source_is_configured(settings),
        ),
        _check(
            "organization_matches_reconciled_pilot",
            settings.customer_settlements_organization_ref == expected_organization_ref,
        ),
        _check(
            "organization_guid_matches_reconciled_pilot",
            settings.customer_settlements_organization_guid == expected_organization_guid,
        ),
        _check(
            "opening_organization_field_matches_reconciliation",
            settings.customer_settlements_opening_organization_field == EXPECTED_ORGANIZATION_FIELD,
        ),
        _check(
            "movement_organization_field_matches_reconciliation",
            settings.customer_settlements_movement_organization_field
            == EXPECTED_ORGANIZATION_FIELD,
        ),
        _check(
            "source_mode_matches_reconciliation",
            settings.customer_settlements_source_mode == EXPECTED_SOURCE_MODE,
        ),
        _check(
            "financial_query_timeout_bounded",
            0 < settings.customer_settlements_query_timeout_seconds <= 30,
        ),
        _check(
            "crm_timeout_bounded",
            0 < settings.customer_settlements_crm_timeout_seconds <= 6,
        ),
        _check(
            "freshness_thresholds_match_contract",
            settings.customer_settlements_stale_after_seconds == 7200
            and settings.customer_settlements_hide_after_seconds == 21600
            and settings.customer_settlements_mapping_stale_after_seconds == 7200,
        ),
        _check(
            "retention_matches_contract",
            settings.customer_settlements_success_retention_days == 30
            and settings.customer_settlements_failed_retention_days == 7
            and settings.customer_settlements_jti_retention_hours == 24,
        ),
        _check(
            "alert_repeat_matches_contract",
            settings.customer_settlements_alert_repeat_seconds == 21600,
        ),
    ]


def _collect_database_facts(session: Session, settings: Settings) -> dict[str, Any]:
    if not try_customer_settlement_context_read_lock(session):
        raise CustomerSettlementContextBusyError("customer_settlement_context_busy")
    bind = session.get_bind()
    alembic_revisions = tuple(
        session.execute(text("SELECT version_num FROM alembic_version")).scalars()
    )
    active_mapping = session.scalar(
        select(CustomerSettlementMappingRevision).where(
            CustomerSettlementMappingRevision.status == "active"
        )
    )
    active_financial = session.scalar(
        select(CustomerSettlementRevision).where(CustomerSettlementRevision.status == "active")
    )
    latest_reconciliation = latest_customer_settlement_reconciliation(session)
    pilot_counterparty_refs = active_pilot_counterparty_refs(session)
    expected_reconciliation_context_hash = None
    if active_mapping is not None and pilot_counterparty_refs:
        try:
            expected_reconciliation_context_hash = customer_settlement_reconciliation_context_hash(
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
                counterparty_refs=pilot_counterparty_refs,
            )
        except CustomerSettlementReconciliationError:
            expected_reconciliation_context_hash = None

    facts: dict[str, Any] = {
        "database_dialect": bind.dialect.name,
        "current_database": (
            session.scalar(text("SELECT current_database()"))
            if bind.dialect.name == "postgresql"
            else None
        ),
        "alembic_revision": alembic_revisions[0] if len(alembic_revisions) == 1 else None,
        "alembic_revision_count": len(alembic_revisions),
        "active_mapping_source_name": (
            active_mapping.source_name if active_mapping is not None else None
        ),
        "latest_reconciliation_status": (
            latest_reconciliation.status if latest_reconciliation is not None else None
        ),
        "latest_reconciliation_context_hash": (
            latest_reconciliation.context_hash if latest_reconciliation is not None else None
        ),
        "latest_reconciliation_has_complete_hashes": (
            _reconciliation_hashes_are_complete(latest_reconciliation)
        ),
        "latest_reconciliation_is_current": bool(
            expected_reconciliation_context_hash is not None
            and customer_settlement_reconciliation_run_is_current(
                latest_reconciliation,
                context_hash=expected_reconciliation_context_hash,
                expected_count=len(pilot_counterparty_refs),
            )
        ),
        "reconciliation_expected_count": (
            latest_reconciliation.expected_count if latest_reconciliation is not None else 0
        ),
        "reconciliation_matched_count": (
            latest_reconciliation.matched_count if latest_reconciliation is not None else 0
        ),
        "reconciliation_mismatch_count": (
            latest_reconciliation.mismatch_count if latest_reconciliation is not None else 0
        ),
        "latest_reconciliation_max_abs_difference": (
            latest_reconciliation.max_abs_difference if latest_reconciliation is not None else None
        ),
        "expected_reconciliation_context_hash": expected_reconciliation_context_hash,
        "enabled_pilots": session.scalar(
            select(func.count())
            .select_from(CustomerSettlementPilotAccess)
            .where(CustomerSettlementPilotAccess.enabled.is_(True))
        )
        or 0,
        "active_mapping_revisions": session.scalar(
            select(func.count())
            .select_from(CustomerSettlementMappingRevision)
            .where(CustomerSettlementMappingRevision.status == "active")
        )
        or 0,
        "mapping_revisions_total": session.scalar(
            select(func.count()).select_from(CustomerSettlementMappingRevision)
        )
        or 0,
        "active_financial_revisions": session.scalar(
            select(func.count())
            .select_from(CustomerSettlementRevision)
            .where(CustomerSettlementRevision.status == "active")
        )
        or 0,
        "financial_revisions_total": session.scalar(
            select(func.count()).select_from(CustomerSettlementRevision)
        )
        or 0,
        "reconciliation_runs_total": session.scalar(
            select(func.count()).select_from(CustomerSettlementReconciliationRun)
        )
        or 0,
        "customer_accounts_total": session.scalar(select(func.count()).select_from(CustomerAccount))
        or 0,
        "site_bindings_total": session.scalar(
            select(func.count()).select_from(CustomerAccountSiteBinding)
        )
        or 0,
        "source_bindings_total": session.scalar(
            select(func.count()).select_from(CustomerAccountSourceBinding)
        )
        or 0,
        "loading_mapping_revisions": session.scalar(
            select(func.count())
            .select_from(CustomerSettlementMappingRevision)
            .where(CustomerSettlementMappingRevision.status == "loading")
        )
        or 0,
        "loading_financial_revisions": session.scalar(
            select(func.count())
            .select_from(CustomerSettlementRevision)
            .where(CustomerSettlementRevision.status == "loading")
        )
        or 0,
        "mapping_entries_total": session.scalar(
            select(func.count()).select_from(CustomerSettlementMappingEntry)
        )
        or 0,
        "financial_balances_total": session.scalar(
            select(func.count()).select_from(CustomerSettlementBalance)
        )
        or 0,
        "linked_pilots": 0,
        "ambiguous_pilots": 0,
        "pilot_counterparties": 0,
        "compatible_pilots": 0,
        "financial_expected_rows": 0,
        "financial_loaded_rows": 0,
        "financial_zero_rows": 0,
    }

    if active_mapping is not None:
        pilot_mapping_filter = (
            CustomerSettlementMappingEntry.revision_id == active_mapping.id,
            CustomerSettlementPilotAccess.enabled.is_(True),
        )
        facts["linked_pilots"] = (
            session.scalar(
                select(func.count(distinct(CustomerSettlementPilotAccess.site_user_id)))
                .select_from(CustomerSettlementPilotAccess)
                .join(
                    CustomerSettlementMappingEntry,
                    CustomerSettlementMappingEntry.site_user_id
                    == CustomerSettlementPilotAccess.site_user_id,
                )
                .where(
                    *pilot_mapping_filter,
                    CustomerSettlementMappingEntry.status == "linked",
                    CustomerSettlementMappingEntry.counterparty_ref.is_not(None),
                    CustomerSettlementMappingEntry.counterparty_guid.is_not(None),
                    CustomerSettlementMappingEntry.customer_account_id.is_not(None),
                    CustomerSettlementMappingEntry.source_binding_id.is_not(None),
                )
            )
            or 0
        )
        facts["ambiguous_pilots"] = (
            session.scalar(
                select(func.count(distinct(CustomerSettlementPilotAccess.site_user_id)))
                .select_from(CustomerSettlementPilotAccess)
                .join(
                    CustomerSettlementMappingEntry,
                    CustomerSettlementMappingEntry.site_user_id
                    == CustomerSettlementPilotAccess.site_user_id,
                )
                .where(
                    *pilot_mapping_filter,
                    CustomerSettlementMappingEntry.status == "ambiguous",
                )
            )
            or 0
        )
        facts["pilot_counterparties"] = (
            session.scalar(
                select(func.count(distinct(CustomerSettlementMappingEntry.counterparty_guid)))
                .select_from(CustomerSettlementPilotAccess)
                .join(
                    CustomerSettlementMappingEntry,
                    CustomerSettlementMappingEntry.site_user_id
                    == CustomerSettlementPilotAccess.site_user_id,
                )
                .where(
                    *pilot_mapping_filter,
                    CustomerSettlementMappingEntry.status == "linked",
                    CustomerSettlementMappingEntry.counterparty_guid.is_not(None),
                )
            )
            or 0
        )

    if active_financial is not None:
        facts["financial_expected_rows"] = active_financial.expected_row_count
        facts["financial_loaded_rows"] = active_financial.loaded_row_count
        facts["financial_zero_rows"] = active_financial.zero_row_count
        if active_mapping is not None:
            facts["compatible_pilots"] = (
                session.scalar(
                    select(func.count(distinct(CustomerSettlementPilotAccess.site_user_id)))
                    .select_from(CustomerSettlementPilotAccess)
                    .join(
                        CustomerSettlementMappingEntry,
                        CustomerSettlementMappingEntry.site_user_id
                        == CustomerSettlementPilotAccess.site_user_id,
                    )
                    .join(
                        CustomerSettlementBalance,
                        CustomerSettlementBalance.counterparty_guid
                        == CustomerSettlementMappingEntry.counterparty_guid,
                    )
                    .join(
                        CustomerAccountSourceBinding,
                        CustomerAccountSourceBinding.id
                        == CustomerSettlementMappingEntry.source_binding_id,
                    )
                    .join(
                        CustomerAccount,
                        CustomerAccount.id == CustomerSettlementMappingEntry.customer_account_id,
                    )
                    .where(
                        CustomerSettlementPilotAccess.enabled.is_(True),
                        CustomerSettlementMappingEntry.revision_id == active_mapping.id,
                        CustomerSettlementMappingEntry.status == "linked",
                        CustomerSettlementMappingEntry.customer_account_id.is_not(None),
                        CustomerSettlementMappingEntry.source_binding_id.is_not(None),
                        CustomerAccountSourceBinding.status == "active",
                        CustomerAccount.status == "active",
                        CustomerSettlementBalance.revision_id == active_financial.id,
                    )
                )
                or 0
            )

    facts["health"] = customer_settlement_health_metrics(
        session,
        stale_after_seconds=7200,
        hide_after_seconds=21600,
        mapping_stale_after_seconds=7200,
        expected_source_mode=settings.customer_settlements_source_mode,
        expected_mapping_source_name="bitrix_crm_customer_cluster",
        expected_source_system="ut103",
        expected_organization_ref=settings.customer_settlements_organization_ref,
        expected_organization_guid=settings.customer_settlements_organization_guid,
    )
    return facts


def _database_checks(
    facts: dict[str, Any],
    *,
    phase: str,
    expected_database_name: str,
    expected_pilot_count: int,
) -> list[dict[str, object]]:
    checks = [
        _check("connected_database_is_postgresql", facts["database_dialect"] == "postgresql"),
        _check(
            "connected_database_matches_staging",
            facts["current_database"] == expected_database_name,
        ),
        _check(
            "alembic_revision_is_current",
            facts["alembic_revision_count"] == 1
            and facts["alembic_revision"] == EXPECTED_ALEMBIC_REVISION,
        ),
        _check("enabled_pilot_count_matches", facts["enabled_pilots"] == expected_pilot_count),
        _check("no_loading_mapping_revision", facts["loading_mapping_revisions"] == 0),
        _check("no_loading_financial_revision", facts["loading_financial_revisions"] == 0),
    ]
    if phase == "bootstrap":
        checks.extend(
            [
                _check(
                    "no_active_mapping_before_first_sync", facts["active_mapping_revisions"] == 0
                ),
                _check(
                    "no_active_financial_before_first_sync",
                    facts["active_financial_revisions"] == 0,
                ),
                _check(
                    "no_mapping_revisions_before_first_sync",
                    facts["mapping_revisions_total"] == 0,
                ),
                _check(
                    "no_financial_revisions_before_first_sync",
                    facts["financial_revisions_total"] == 0,
                ),
                _check(
                    "no_reconciliation_runs_before_first_sync",
                    facts["reconciliation_runs_total"] == 0,
                ),
                _check(
                    "no_durable_customer_bindings_before_first_sync",
                    facts["customer_accounts_total"] == 0
                    and facts["site_bindings_total"] == 0
                    and facts["source_bindings_total"] == 0,
                ),
                _check("no_mapping_rows_before_first_sync", facts["mapping_entries_total"] == 0),
                _check(
                    "no_financial_rows_before_first_sync",
                    facts["financial_balances_total"] == 0,
                ),
                _check(
                    "missing_revision_health_is_fail_closed",
                    facts["health"]["freshness_status"] == "critical"
                    and facts["health"]["mapping_status"] == "critical",
                ),
            ]
        )
        return checks

    checks.extend(
        [
            _check("exactly_one_active_mapping_revision", facts["active_mapping_revisions"] == 1),
            _check(
                "exactly_one_active_financial_revision",
                facts["active_financial_revisions"] == 1,
            ),
            _check(
                "all_pilots_have_linked_mapping", facts["linked_pilots"] == expected_pilot_count
            ),
            _check("no_ambiguous_pilot_mapping", facts["ambiguous_pilots"] == 0),
            _check(
                "all_pilots_have_compatible_balance",
                facts["compatible_pilots"] == expected_pilot_count,
            ),
            _check(
                "latest_reconciliation_is_matched",
                facts["latest_reconciliation_status"] == "matched",
            ),
            _check(
                "latest_reconciliation_matches_active_context",
                facts["expected_reconciliation_context_hash"] is not None
                and facts["latest_reconciliation_context_hash"]
                == facts["expected_reconciliation_context_hash"],
            ),
            _check(
                "latest_reconciliation_is_complete_match",
                facts["latest_reconciliation_is_current"] is True
                and facts["latest_reconciliation_has_complete_hashes"] is True
                and facts["reconciliation_expected_count"] == facts["pilot_counterparties"]
                and facts["reconciliation_matched_count"] == facts["pilot_counterparties"]
                and facts["reconciliation_mismatch_count"] == 0
                and _reconciliation_difference_is_acceptable(
                    facts["latest_reconciliation_max_abs_difference"]
                ),
            ),
            _check(
                "financial_revision_is_complete",
                facts["financial_expected_rows"] == facts["pilot_counterparties"]
                and facts["financial_loaded_rows"] == facts["pilot_counterparties"]
                and 0 <= facts["financial_zero_rows"] <= facts["financial_loaded_rows"],
            ),
            _check(
                "active_revisions_are_fresh",
                facts["health"]["freshness_status"] == "ok"
                and facts["health"]["mapping_status"] == "ok",
            ),
        ]
    )
    return checks


def build_shadow_preflight_report(
    settings: Settings,
    session: Session,
    *,
    phase: str,
    expected_database_name: str,
    expected_organization_ref: str,
    expected_organization_guid: str,
    expected_pilot_count: int,
) -> dict[str, object]:
    checks = _configuration_checks(
        settings,
        phase=phase,
        expected_database_name=expected_database_name,
        expected_organization_ref=expected_organization_ref,
        expected_organization_guid=expected_organization_guid,
    )
    facts = _collect_database_facts(session, settings)
    if phase == "ready":
        checks.append(
            _check(
                "active_mapping_source_matches_mode",
                facts["active_mapping_source_name"] == "bitrix_crm_customer_cluster",
            )
        )
    checks.extend(
        _database_checks(
            facts,
            phase=phase,
            expected_database_name=expected_database_name,
            expected_pilot_count=expected_pilot_count,
        )
    )
    failed_checks = [item["name"] for item in checks if not item["ok"]]
    safe_metrics = {
        key: value
        for key, value in facts.items()
        if key
        not in {
            "database_dialect",
            "current_database",
            "alembic_revision",
            "active_mapping_source_name",
            "latest_reconciliation_status",
            "latest_reconciliation_context_hash",
            "expected_reconciliation_context_hash",
            "latest_reconciliation_max_abs_difference",
            "health",
        }
    }
    safe_metrics.update(facts["health"])
    return {
        "status": "ready" if not failed_checks else "blocked",
        "phase": phase,
        "summary": {
            "passed": len(checks) - len(failed_checks),
            "failed": len(failed_checks),
        },
        "failed_checks": failed_checks,
        "metrics": safe_metrics,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed preflight for the customer-settlement staging shadow run."
    )
    parser.add_argument("--phase", choices=("bootstrap", "ready"), default="bootstrap")
    parser.add_argument("--expected-pilot-count", type=int, default=10)
    parser.add_argument(
        "--expected-database-name",
        default=DEFAULT_EXPECTED_DATABASE_NAME,
    )
    parser.add_argument(
        "--expected-organization-ref",
        default=DEFAULT_EXPECTED_ORGANIZATION_REF,
    )
    parser.add_argument(
        "--expected-organization-guid",
        default=DEFAULT_EXPECTED_ORGANIZATION_GUID,
    )
    args = parser.parse_args(argv)
    if args.expected_pilot_count <= 0:
        parser.error("--expected-pilot-count must be positive")

    session = None
    try:
        settings = get_settings()
        session = get_application_session_factory()()
        report = build_shadow_preflight_report(
            settings,
            session,
            phase=args.phase,
            expected_database_name=args.expected_database_name,
            expected_organization_ref=args.expected_organization_ref,
            expected_organization_guid=args.expected_organization_guid,
            expected_pilot_count=args.expected_pilot_count,
        )
    except Exception as exc:
        if session is not None:
            try:
                session.rollback()
            except Exception:
                pass
        report = {
            "status": "blocked",
            "phase": args.phase,
            "summary": {"passed": 0, "failed": 1},
            "failed_checks": ["database_preflight_completed"],
            "error_type": type(exc).__name__,
        }
    finally:
        if session is not None:
            try:
                session.rollback()
            except Exception:
                pass
            try:
                session.close()
            except Exception:
                pass
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
