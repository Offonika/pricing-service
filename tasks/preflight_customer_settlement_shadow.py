from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import distinct, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.infrastructure.db import get_application_session_factory
from app.models.customer_settlement import (
    CustomerSettlementBalance,
    CustomerSettlementMappingEntry,
    CustomerSettlementMappingRevision,
    CustomerSettlementPilotAccess,
    CustomerSettlementRevision,
)
from app.services.customer_settlements import customer_settlement_health_metrics

EXPECTED_ALEMBIC_REVISION = "c3d4e5f6a7b9"
EXPECTED_ORGANIZATION_FIELD = "_Fld7005RRef"
EXPECTED_SOURCE_MODE = "onec_canonical_mutual_statement_7002"
DEFAULT_EXPECTED_DATABASE_NAME = "settlements_stage"
DEFAULT_EXPECTED_ORGANIZATION_REF = "0xb34a0025901e48ef11e211128227ea80"


def _check(name: str, ok: bool) -> dict[str, object]:
    return {"name": name, "ok": bool(ok)}


def _configuration_checks(
    settings: Settings,
    *,
    expected_organization_ref: str,
) -> list[dict[str, object]]:
    try:
        database_backend = make_url(settings.database_url).get_backend_name()
    except Exception:
        database_backend = ""
    return [
        _check("environment_is_staging", settings.environment.strip().lower() == "staging"),
        _check("client_api_disabled", settings.customer_settlements_enabled is False),
        _check("shadow_worker_enabled", settings.customer_settlements_shadow_enabled is True),
        _check("source_reconciliation_gate_open", settings.customer_settlements_source_validated),
        _check("application_database_is_postgresql", database_backend == "postgresql"),
        _check("onec_source_configured", bool(settings.onec_database_url)),
        _check(
            "crm_readonly_source_configured", bool(settings.customer_settlements_crm_webhook_url)
        ),
        _check(
            "organization_matches_reconciled_pilot",
            settings.customer_settlements_organization_ref == expected_organization_ref,
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
    ]


def _collect_database_facts(session: Session) -> dict[str, Any]:
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

    facts: dict[str, Any] = {
        "database_dialect": bind.dialect.name,
        "current_database": (
            session.scalar(text("SELECT current_database()"))
            if bind.dialect.name == "postgresql"
            else None
        ),
        "alembic_revision": alembic_revisions[0] if len(alembic_revisions) == 1 else None,
        "alembic_revision_count": len(alembic_revisions),
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
        "active_financial_revisions": session.scalar(
            select(func.count())
            .select_from(CustomerSettlementRevision)
            .where(CustomerSettlementRevision.status == "active")
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
                select(func.count(distinct(CustomerSettlementMappingEntry.counterparty_ref)))
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
                        CustomerSettlementBalance.counterparty_ref
                        == CustomerSettlementMappingEntry.counterparty_ref,
                    )
                    .where(
                        CustomerSettlementPilotAccess.enabled.is_(True),
                        CustomerSettlementMappingEntry.revision_id == active_mapping.id,
                        CustomerSettlementMappingEntry.status == "linked",
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
    expected_pilot_count: int,
) -> dict[str, object]:
    checks = _configuration_checks(
        settings,
        expected_organization_ref=expected_organization_ref,
    )
    facts = _collect_database_facts(session)
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
    args = parser.parse_args(argv)
    if args.expected_pilot_count <= 0:
        parser.error("--expected-pilot-count must be positive")

    settings = get_settings()
    session = get_application_session_factory()()
    try:
        report = build_shadow_preflight_report(
            settings,
            session,
            phase=args.phase,
            expected_database_name=args.expected_database_name,
            expected_organization_ref=args.expected_organization_ref,
            expected_pilot_count=args.expected_pilot_count,
        )
    except Exception as exc:
        session.rollback()
        report = {
            "status": "blocked",
            "phase": args.phase,
            "summary": {"passed": 0, "failed": 1},
            "failed_checks": ["database_preflight_completed"],
            "error_type": type(exc).__name__,
        }
    finally:
        session.rollback()
        session.close()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
