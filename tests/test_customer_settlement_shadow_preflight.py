from __future__ import annotations

from types import SimpleNamespace

from app.core.config import Settings
from tasks import preflight_customer_settlement_shadow as preflight

ORG = "0xb34a0025901e48ef11e211128227ea80"


def _settings(**overrides) -> Settings:
    values = {
        "environment": "staging",
        "database_url": "postgresql+psycopg2://stage:secret@127.0.0.1/settlements_stage",
        "onec_database_url": "mssql+pyodbc://readonly:secret@onec/ut",
        "customer_settlements_enabled": False,
        "customer_settlements_shadow_enabled": True,
        "customer_settlements_source_validated": True,
        "customer_settlements_organization_ref": ORG,
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
        "alembic_revision": "c3d4e5f6a7b9",
        "alembic_revision_count": 1,
        "enabled_pilots": 10,
        "active_mapping_revisions": 0,
        "active_financial_revisions": 0,
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
    monkeypatch.setattr(preflight, "_collect_database_facts", lambda session: _facts())

    report = preflight.build_shadow_preflight_report(
        _settings(),
        SimpleNamespace(),
        phase="bootstrap",
        expected_database_name="settlements_stage",
        expected_organization_ref=ORG,
        expected_pilot_count=10,
    )

    assert report["status"] == "ready"
    assert report["failed_checks"] == []
    assert report["metrics"]["enabled_pilots"] == 10


def test_bootstrap_preflight_blocks_client_api_and_missing_crm(monkeypatch) -> None:
    monkeypatch.setattr(preflight, "_collect_database_facts", lambda session: _facts())

    report = preflight.build_shadow_preflight_report(
        _settings(
            customer_settlements_enabled=True,
            customer_settlements_crm_webhook_url=None,
        ),
        SimpleNamespace(),
        phase="bootstrap",
        expected_database_name="settlements_stage",
        expected_organization_ref=ORG,
        expected_pilot_count=10,
    )

    assert report["status"] == "blocked"
    assert "client_api_disabled" in report["failed_checks"]
    assert "crm_readonly_source_configured" in report["failed_checks"]


def test_bootstrap_preflight_blocks_multiple_alembic_heads(monkeypatch) -> None:
    monkeypatch.setattr(
        preflight,
        "_collect_database_facts",
        lambda session: _facts(alembic_revision=None, alembic_revision_count=2),
    )

    report = preflight.build_shadow_preflight_report(
        _settings(),
        SimpleNamespace(),
        phase="bootstrap",
        expected_database_name="settlements_stage",
        expected_organization_ref=ORG,
        expected_pilot_count=10,
    )

    assert report["status"] == "blocked"
    assert "alembic_revision_is_current" in report["failed_checks"]


def test_ready_preflight_requires_fresh_compatible_revisions(monkeypatch) -> None:
    ready_facts = _facts(
        active_mapping_revisions=1,
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
        lambda session: ready_facts,
    )

    report = preflight.build_shadow_preflight_report(
        _settings(),
        SimpleNamespace(),
        phase="ready",
        expected_database_name="settlements_stage",
        expected_organization_ref=ORG,
        expected_pilot_count=10,
    )

    assert report["status"] == "ready"
    assert report["metrics"]["compatible_pilots"] == 10
    assert report["metrics"]["financial_zero_rows"] == 3


def test_preflight_report_never_contains_connection_strings_or_identifiers(monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr(preflight, "_collect_database_facts", lambda session: _facts())

    report = preflight.build_shadow_preflight_report(
        settings,
        SimpleNamespace(),
        phase="bootstrap",
        expected_database_name="settlements_stage",
        expected_organization_ref=ORG,
        expected_pilot_count=10,
    )
    rendered = str(report)

    assert "secret" not in rendered
    assert "crm.example" not in rendered
    assert ORG not in rendered
