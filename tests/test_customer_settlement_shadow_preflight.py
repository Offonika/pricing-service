from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from tasks import preflight_customer_settlement_shadow as preflight

ORG = "0xb34a0025901e48ef11e211128227ea80"
ORG_GUID = "8227ea80-1112-11e2-b34a-0025901e48ef"


def _settings(**overrides) -> Settings:
    values = {
        "environment": "staging",
        "database_url": "postgresql+psycopg2://stage:secret@127.0.0.1/settlements_stage",
        "onec_database_url": "mssql+pyodbc://readonly:secret@onec/ut",
        "customer_settlements_enabled": False,
        "customer_settlements_eligibility_enabled": False,
        "customer_settlements_shadow_enabled": True,
        "customer_settlements_expected_database_name": "settlements_stage",
        "customer_settlements_source_validated": False,
        "customer_settlements_mapping_mode": "crm_readonly",
        "customer_settlements_organization_ref": ORG,
        "customer_settlements_organization_guid": ORG_GUID,
        "customer_settlements_opening_organization_field": "_Fld7005RRef",
        "customer_settlements_movement_organization_field": "_Fld7005RRef",
        "customer_settlements_crm_webhook_url": "https://crm.example/rest/readonly",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _facts(**overrides):
    values = {
        "database_dialect": "postgresql",
        "current_database": "settlements_stage",
        "alembic_revision": "6e8f0a2b4c6d",
        "alembic_revision_count": 1,
        "active_mapping_source_name": None,
        "latest_reconciliation_status": None,
        "latest_reconciliation_context_hash": None,
        "expected_reconciliation_context_hash": None,
        "latest_reconciliation_has_complete_hashes": False,
        "latest_reconciliation_is_current": False,
        "reconciliation_expected_count": 0,
        "reconciliation_matched_count": 0,
        "reconciliation_mismatch_count": 0,
        "latest_reconciliation_max_abs_difference": None,
        "enabled_pilots": 10,
        "active_mapping_revisions": 0,
        "mapping_revisions_total": 0,
        "active_financial_revisions": 0,
        "financial_revisions_total": 0,
        "reconciliation_runs_total": 0,
        "customer_accounts_total": 0,
        "site_bindings_total": 0,
        "source_bindings_total": 0,
        "loading_mapping_revisions": 0,
        "loading_financial_revisions": 0,
        "mapping_entries_total": 0,
        "financial_balances_total": 0,
        "linked_pilots": 0,
        "ambiguous_pilots": 0,
        "pilot_counterparties": 0,
        "compatible_pilots": 0,
        "financial_expected_rows": 0,
        "financial_loaded_rows": 0,
        "financial_zero_rows": 0,
        "health": {
            "freshness_status": "critical",
            "mapping_status": "critical",
            "financial_age_seconds": None,
            "mapping_age_seconds": None,
            "expected_rows": 0,
            "loaded_rows": 0,
            "zero_rows": 0,
            "mapping_entries": 0,
            "ambiguous_entries": 0,
        },
    }
    values.update(overrides)
    return values


def test_bootstrap_preflight_accepts_empty_fail_closed_staging(monkeypatch) -> None:
    monkeypatch.setattr(preflight, "_collect_database_facts", lambda session, settings: _facts())

    report = preflight.build_shadow_preflight_report(
        _settings(),
        SimpleNamespace(),
        phase="bootstrap",
        expected_database_name="settlements_stage",
        expected_organization_ref=ORG,
        expected_organization_guid=ORG_GUID,
        expected_pilot_count=10,
    )

    assert report["status"] == "ready"
    assert report["failed_checks"] == []
    assert report["metrics"]["enabled_pilots"] == 10


def test_database_facts_fail_before_queries_when_context_is_busy(monkeypatch) -> None:
    monkeypatch.setattr(
        preflight,
        "try_customer_settlement_context_read_lock",
        lambda session: False,
    )

    with pytest.raises(
        preflight.CustomerSettlementContextBusyError,
        match="customer_settlement_context_busy",
    ):
        preflight._collect_database_facts(SimpleNamespace(), _settings())


def test_bootstrap_preflight_blocks_client_and_eligibility_api(
    monkeypatch,
) -> None:
    monkeypatch.setattr(preflight, "_collect_database_facts", lambda session, settings: _facts())

    report = preflight.build_shadow_preflight_report(
        _settings(
            customer_settlements_enabled=True,
            customer_settlements_eligibility_enabled=True,
        ),
        SimpleNamespace(),
        phase="bootstrap",
        expected_database_name="settlements_stage",
        expected_organization_ref=ORG,
        expected_organization_guid=ORG_GUID,
        expected_pilot_count=10,
    )

    assert report["status"] == "blocked"
    assert "client_api_disabled" in report["failed_checks"]
    assert "eligibility_api_disabled" in report["failed_checks"]


def test_bootstrap_preflight_requires_crm_readonly_with_webhook(monkeypatch) -> None:
    monkeypatch.setattr(preflight, "_collect_database_facts", lambda session, settings: _facts())
    report = preflight.build_shadow_preflight_report(
        _settings(
            customer_settlements_mapping_mode="crm_readonly",
            customer_settlements_crm_webhook_url=None,
        ),
        SimpleNamespace(),
        phase="bootstrap",
        expected_database_name="settlements_stage",
        expected_organization_ref=ORG,
        expected_organization_guid=ORG_GUID,
        expected_pilot_count=10,
    )
    assert "mapping_source_configured" in report["failed_checks"]

    manual_report = preflight.build_shadow_preflight_report(
        _settings(customer_settlements_mapping_mode="manual_confirmed"),
        SimpleNamespace(),
        phase="bootstrap",
        expected_database_name="settlements_stage",
        expected_organization_ref=ORG,
        expected_organization_guid=ORG_GUID,
        expected_pilot_count=10,
    )
    assert "mapping_source_configured" in manual_report["failed_checks"]


@pytest.mark.parametrize(
    "webhook_url",
    (
        "http://crm.example/rest/readonly",
        "https://user:password@crm.example/rest/readonly",
        "https://crm.example/rest/readonly?token=secret",
        "https://crm.example/rest/readonly#fragment",
        "https://[broken/rest/readonly",
        "https://crm.example:invalid/rest/readonly",
    ),
)
def test_bootstrap_preflight_rejects_unsafe_crm_webhook(
    monkeypatch,
    webhook_url: str,
) -> None:
    monkeypatch.setattr(preflight, "_collect_database_facts", lambda session, settings: _facts())

    report = preflight.build_shadow_preflight_report(
        _settings(customer_settlements_crm_webhook_url=webhook_url),
        SimpleNamespace(),
        phase="bootstrap",
        expected_database_name="settlements_stage",
        expected_organization_ref=ORG,
        expected_organization_guid=ORG_GUID,
        expected_pilot_count=10,
    )

    assert "mapping_source_configured" in report["failed_checks"]


def test_bootstrap_preflight_requires_source_gate_closed(monkeypatch) -> None:
    monkeypatch.setattr(preflight, "_collect_database_facts", lambda session, settings: _facts())

    report = preflight.build_shadow_preflight_report(
        _settings(customer_settlements_source_validated=True),
        SimpleNamespace(),
        phase="bootstrap",
        expected_database_name="settlements_stage",
        expected_organization_ref=ORG,
        expected_organization_guid=ORG_GUID,
        expected_pilot_count=10,
    )

    assert "source_reconciliation_gate_closed" in report["failed_checks"]


def test_bootstrap_preflight_blocks_multiple_alembic_heads(monkeypatch) -> None:
    monkeypatch.setattr(
        preflight,
        "_collect_database_facts",
        lambda session, settings: _facts(alembic_revision=None, alembic_revision_count=2),
    )

    report = preflight.build_shadow_preflight_report(
        _settings(),
        SimpleNamespace(),
        phase="bootstrap",
        expected_database_name="settlements_stage",
        expected_organization_ref=ORG,
        expected_organization_guid=ORG_GUID,
        expected_pilot_count=10,
    )

    assert report["status"] == "blocked"
    assert "alembic_revision_is_current" in report["failed_checks"]


def test_bootstrap_preflight_rejects_reusable_historical_reconciliation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        preflight,
        "_collect_database_facts",
        lambda session, settings: _facts(reconciliation_runs_total=1),
    )

    report = preflight.build_shadow_preflight_report(
        _settings(),
        SimpleNamespace(),
        phase="bootstrap",
        expected_database_name="settlements_stage",
        expected_organization_ref=ORG,
        expected_organization_guid=ORG_GUID,
        expected_pilot_count=10,
    )

    assert report["status"] == "blocked"
    assert "no_reconciliation_runs_before_first_sync" in report["failed_checks"]


def test_bootstrap_preflight_rejects_stale_durable_customer_bindings(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        preflight,
        "_collect_database_facts",
        lambda session, settings: _facts(customer_accounts_total=1),
    )

    report = preflight.build_shadow_preflight_report(
        _settings(),
        SimpleNamespace(),
        phase="bootstrap",
        expected_database_name="settlements_stage",
        expected_organization_ref=ORG,
        expected_organization_guid=ORG_GUID,
        expected_pilot_count=10,
    )

    assert report["status"] == "blocked"
    assert "no_durable_customer_bindings_before_first_sync" in report["failed_checks"]


def test_ready_preflight_requires_fresh_compatible_revisions(monkeypatch) -> None:
    ready_facts = _facts(
        active_mapping_revisions=1,
        active_mapping_source_name="bitrix_crm_customer_cluster",
        latest_reconciliation_status="matched",
        latest_reconciliation_context_hash="e" * 64,
        expected_reconciliation_context_hash="e" * 64,
        latest_reconciliation_has_complete_hashes=True,
        latest_reconciliation_is_current=True,
        reconciliation_expected_count=10,
        reconciliation_matched_count=10,
        latest_reconciliation_max_abs_difference=0,
        active_financial_revisions=1,
        mapping_entries_total=4103,
        financial_balances_total=10,
        linked_pilots=10,
        pilot_counterparties=10,
        compatible_pilots=10,
        financial_expected_rows=10,
        financial_loaded_rows=10,
        financial_zero_rows=3,
        health={
            "freshness_status": "ok",
            "mapping_status": "ok",
            "financial_age_seconds": 20,
            "mapping_age_seconds": 30,
            "expected_rows": 10,
            "loaded_rows": 10,
            "zero_rows": 3,
            "mapping_entries": 4103,
            "ambiguous_entries": 0,
        },
    )
    monkeypatch.setattr(
        preflight,
        "_collect_database_facts",
        lambda session, settings: ready_facts,
    )

    report = preflight.build_shadow_preflight_report(
        _settings(customer_settlements_source_validated=True),
        SimpleNamespace(),
        phase="ready",
        expected_database_name="settlements_stage",
        expected_organization_ref=ORG,
        expected_organization_guid=ORG_GUID,
        expected_pilot_count=10,
    )

    assert report["status"] == "ready"
    assert report["metrics"]["compatible_pilots"] == 10
    assert report["metrics"]["financial_zero_rows"] == 3


def test_ready_preflight_requires_open_gate_and_latest_matched_reconciliation(
    monkeypatch,
) -> None:
    ready_facts = _facts(
        active_mapping_revisions=1,
        active_mapping_source_name="bitrix_crm_customer_cluster",
        active_financial_revisions=1,
        linked_pilots=10,
        pilot_counterparties=10,
        compatible_pilots=10,
        financial_expected_rows=10,
        financial_loaded_rows=10,
        latest_reconciliation_status="mismatched",
        latest_reconciliation_context_hash="e" * 64,
        expected_reconciliation_context_hash="e" * 64,
        latest_reconciliation_has_complete_hashes=True,
        reconciliation_expected_count=10,
        reconciliation_matched_count=9,
        reconciliation_mismatch_count=1,
        latest_reconciliation_max_abs_difference=1,
        health={
            "freshness_status": "ok",
            "mapping_status": "ok",
            "expected_rows": 10,
            "loaded_rows": 10,
            "zero_rows": 0,
            "mapping_entries": 10,
            "ambiguous_entries": 0,
        },
    )
    monkeypatch.setattr(
        preflight,
        "_collect_database_facts",
        lambda session, settings: ready_facts,
    )

    report = preflight.build_shadow_preflight_report(
        _settings(customer_settlements_source_validated=False),
        SimpleNamespace(),
        phase="ready",
        expected_database_name="settlements_stage",
        expected_organization_ref=ORG,
        expected_organization_guid=ORG_GUID,
        expected_pilot_count=10,
    )

    assert "source_reconciliation_gate_open" in report["failed_checks"]
    assert "latest_reconciliation_is_matched" in report["failed_checks"]


@pytest.mark.parametrize(
    "corrupted_difference",
    ("NaN", "Infinity", "-Infinity", "not-a-number", "-0.01"),
)
def test_ready_preflight_fails_closed_for_invalid_reconciliation_difference(
    corrupted_difference,
) -> None:
    facts = _facts(
        active_mapping_revisions=1,
        active_financial_revisions=1,
        linked_pilots=10,
        pilot_counterparties=10,
        compatible_pilots=10,
        financial_expected_rows=10,
        financial_loaded_rows=10,
        latest_reconciliation_status="matched",
        latest_reconciliation_context_hash="e" * 64,
        expected_reconciliation_context_hash="e" * 64,
        latest_reconciliation_has_complete_hashes=True,
        latest_reconciliation_is_current=True,
        reconciliation_expected_count=10,
        reconciliation_matched_count=10,
        latest_reconciliation_max_abs_difference=corrupted_difference,
        health={"freshness_status": "ok", "mapping_status": "ok"},
    )

    checks = {
        item["name"]: item["ok"]
        for item in preflight._database_checks(
            facts,
            phase="ready",
            expected_database_name="settlements_stage",
            expected_pilot_count=10,
        )
    }

    assert checks["latest_reconciliation_is_complete_match"] is False


def test_preflight_report_never_contains_connection_strings_or_identifiers(monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr(preflight, "_collect_database_facts", lambda session, settings: _facts())

    report = preflight.build_shadow_preflight_report(
        settings,
        SimpleNamespace(),
        phase="bootstrap",
        expected_database_name="settlements_stage",
        expected_organization_ref=ORG,
        expected_organization_guid=ORG_GUID,
        expected_pilot_count=10,
    )
    rendered = str(report)

    assert "secret" not in rendered
    assert "crm.example" not in rendered
    assert ORG not in rendered


def test_preflight_settings_failure_is_sanitized(monkeypatch, capsys) -> None:
    def fail_settings():
        raise RuntimeError("private-connection-string")

    monkeypatch.setattr(preflight, "get_settings", fail_settings)

    assert preflight.main([]) == 2
    output = capsys.readouterr().out
    assert "private-connection-string" not in output
    assert json.loads(output)["error_type"] == "RuntimeError"
