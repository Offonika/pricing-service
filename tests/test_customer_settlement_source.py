from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.customer_settlement import CustomerSettlementPilotAccess
from app.services.customer_settlement_source import (
    CustomerSettlementSourceError,
    _insert_counterparty_scope,
    fetch_customer_settlement_balances,
    fetch_customer_settlement_scope_eligibility,
    fetch_manual_customer_settlement_controls,
    validate_organization_field,
)
from app.services.customer_settlements import onec_ref_to_guid
from app.workers.customer_settlements import (
    run_customer_settlement_financial_sync,
    run_customer_settlement_mapping_sync,
)
from tasks import (
    check_customer_settlement_health,
    manage_customer_settlement_pilot,
    mock_customer_settlement_client,
    sync_customer_settlement_mapping,
    sync_customer_settlements,
)

ORG = "0x" + "a" * 32
CP_1 = "0x" + "1" * 32
CP_2 = "0x" + "2" * 32
CP_3 = "0x" + "3" * 32


class _FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def one(self):
        assert len(self.rows) == 1
        return self.rows[0]

    def __iter__(self):
        return iter(self.rows)


class _FakeTransaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.in_explicit_transaction = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.connection.in_explicit_transaction = False
        return False


class _FakeConnection:
    def __init__(self):
        self.isolation_level: str | None = None
        self.sql: list[str] = []
        self.parameters: list[object] = []
        self.in_explicit_transaction = False
        self.clock_transaction_states: list[bool] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def rollback(self):
        return None

    def execution_options(self, *, isolation_level: str):
        self.isolation_level = isolation_level
        return self

    def begin(self):
        return _FakeTransaction(self)

    def exec_driver_sql(self, statement: str):
        self.sql.append(statement)
        return _FakeResult([])

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.sql.append(sql)
        self.parameters.append(parameters)
        if "SYSUTCDATETIME()" in sql:
            self.clock_transaction_states.append(self.in_explicit_transaction)
            return _FakeResult(
                [
                    {
                        "utc_now": datetime(2026, 7, 29, 9, 30),
                        "local_now": datetime(2026, 7, 29, 12, 30),
                        "snapshot_isolation_state": 1,
                    }
                ]
            )
        if "WITH" in sql and "latest_opening_period" in sql:
            return _FakeResult(
                [
                    {
                        "counterparty_ref": CP_1,
                        "signed_balance": Decimal("0.00"),
                        "counterparty_exists": 1,
                        "marked_deleted": 0,
                    },
                    {
                        "counterparty_ref": CP_2,
                        "signed_balance": Decimal("-10.00"),
                        "counterparty_exists": 1,
                        "marked_deleted": 0,
                    },
                ]
            )
        return _FakeResult([])


class _FakeEngine:
    def __init__(self):
        self.dialect = SimpleNamespace(name="mssql")
        self.connection = _FakeConnection()

    def connect(self):
        return self.connection


class _ManualControlConnection(_FakeConnection):
    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.sql.append(sql)
        self.parameters.append(parameters)
        if "SYSUTCDATETIME()" in sql:
            return super().execute(statement, parameters)
        if "FROM dbo._Reference66 AS organization" in sql:
            return _FakeResult([{"marked_deleted": 0}])
        if "LEFT JOIN dbo._Reference54 AS counterparty" in sql:
            return _FakeResult(
                [
                    {
                        "counterparty_ref": CP_1,
                        "counterparty_code": "PILOT-1",
                        "counterparty_name": "Synthetic Pilot",
                        "counterparty_inn": "1234567890",
                        "marked_deleted": 0,
                        "is_element": 1,
                    }
                ]
            )
        if "JOIN dbo._Reference37 AS contract" in sql:
            return _FakeResult([{"counterparty_ref": CP_1, "currency_code": "643"}])
        return _FakeResult([])


class _ManualControlEngine(_FakeEngine):
    def __init__(self):
        self.dialect = SimpleNamespace(name="mssql")
        self.connection = _ManualControlConnection()


class _EligibilityConnection(_FakeConnection):
    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.sql.append(sql)
        self.parameters.append(parameters)
        if "AS active_element" in sql:
            return _FakeResult(
                [
                    {
                        "counterparty_ref": CP_1,
                        "active_element": 1,
                        "blank_name": 0,
                        "has_non_rub_contract": 0,
                    },
                    {
                        "counterparty_ref": CP_2,
                        "active_element": 1,
                        "blank_name": 1,
                        "has_non_rub_contract": 0,
                    },
                    {
                        "counterparty_ref": CP_3,
                        "active_element": 1,
                        "blank_name": 0,
                        "has_non_rub_contract": 1,
                    },
                ]
            )
        return _FakeResult([])


class _EligibilityEngine(_FakeEngine):
    def __init__(self):
        self.dialect = SimpleNamespace(name="mssql")
        self.connection = _EligibilityConnection()


def test_extractor_uses_exact_as_of_snapshot_and_explicit_zero() -> None:
    engine = _FakeEngine()
    as_of = datetime(2026, 7, 29, 9, 15, tzinfo=UTC)

    result = fetch_customer_settlement_balances(
        engine,
        organization_ref=ORG,
        opening_organization_field="_Fld7005RRef",
        movement_organization_field="_Fld7005RRef",
        counterparty_refs=[CP_2, CP_1],
        query_timeout_seconds=30,
        as_of=as_of,
    )

    assert result.as_of == as_of
    assert result.source_db_time == datetime(2026, 7, 29, 9, 30, tzinfo=UTC)
    assert result.isolation_level == "SNAPSHOT"
    assert [item.counterparty_ref for item in result.balances] == [CP_1, CP_2]
    assert result.balances[0].signed_balance == Decimal("0.00")
    assert result.balances[0].counterparty_guid == onec_ref_to_guid(CP_1)

    rendered_sql = "\n".join(engine.connection.sql)
    assert "NOLOCK" not in rendered_sql.upper()
    assert "r._Period < :movement_end" in rendered_sql
    assert "COALESCE(balances.signed_balance, 0)" in rendered_sql
    assert "counterparty._Folder = 0x01" in rendered_sql
    assert "#CustomerSettlementPilot" in rendered_sql
    assert "OBJECT_ID('tempdb..#CustomerSettlementPilot')" in rendered_sql
    assert "SET TRANSACTION ISOLATION LEVEL SNAPSHOT" in rendered_sql
    assert "CONVERT(varchar(34), :organization_ref)" in rendered_sql
    assert "CONVERT(varchar(34), :counterparty_ref_0)" in rendered_sql
    query_parameters = next(
        value
        for value in engine.connection.parameters
        if isinstance(value, dict) and "movement_end" in value
    )
    assert query_parameters["movement_end"] == datetime(2026, 7, 29, 12, 15)
    assert engine.connection.isolation_level is None
    assert engine.connection.clock_transaction_states == [False, True]


def test_temp_scope_is_inserted_in_parameterized_batches() -> None:
    connection = _FakeConnection()
    refs = tuple(f"0x{value:032x}" for value in range(1001))

    _insert_counterparty_scope(
        connection,
        table_name="#CustomerSettlementPilot",
        refs=refs,
    )

    assert len(connection.sql) == 3
    assert [len(value) for value in connection.parameters] == [500, 500, 1]
    assert all(isinstance(value, dict) for value in connection.parameters)
    assert all("0x" not in sql for sql in connection.sql)
    with pytest.raises(
        CustomerSettlementSourceError,
        match="customer_settlement_temp_table_is_invalid",
    ):
        _insert_counterparty_scope(connection, table_name="#untrusted", refs=(CP_1,))


def test_all_linked_scope_excludes_blank_names_and_non_rub_contracts() -> None:
    engine = _EligibilityEngine()
    result = fetch_customer_settlement_scope_eligibility(
        engine,
        counterparty_refs=(CP_3, CP_2, CP_1),
        query_timeout_seconds=30,
        max_counterparties=10,
    )

    assert result.total_counterparties == 3
    assert result.eligible_counterparty_refs == (CP_1,)
    assert result.blank_name_counterparties == 1
    assert result.non_rub_counterparties == 1
    assert "OBJECT_ID('tempdb..#CustomerSettlementManualPilot')" in "\n".join(engine.connection.sql)


def test_extractor_derives_onec_boundary_from_sql_utc_clock() -> None:
    engine = _FakeEngine()
    original_execute = engine.connection.execute

    def execute_with_misleading_local_time(statement, parameters=None):
        if "SYSUTCDATETIME()" in str(statement):
            return _FakeResult(
                [
                    {
                        "utc_now": datetime(2026, 7, 29, 9, 30),
                        "local_now": datetime(2026, 7, 29, 1, 0),
                        "snapshot_isolation_state": 1,
                    }
                ]
            )
        return original_execute(statement, parameters)

    engine.connection.execute = execute_with_misleading_local_time

    fetch_customer_settlement_balances(
        engine,
        organization_ref=ORG,
        opening_organization_field="_Fld7005RRef",
        movement_organization_field="_Fld7005RRef",
        counterparty_refs=[CP_1, CP_2],
        query_timeout_seconds=30,
    )

    query_parameters = next(
        value
        for value in engine.connection.parameters
        if isinstance(value, dict) and "movement_end" in value
    )
    assert query_parameters["movement_end"] == datetime(2026, 7, 29, 12, 30)


def test_extractor_rejects_unvalidated_dimensions_future_time_and_wrong_database() -> None:
    assert validate_organization_field("_Fld7005RRef") == "_Fld7005RRef"
    with pytest.raises(CustomerSettlementSourceError):
        validate_organization_field("_Fld7005")

    for invalid_timeout in (True, 0, 31, 1.5):
        with pytest.raises(
            CustomerSettlementSourceError,
            match="query_timeout_must_be_between_1_and_30_seconds",
        ):
            fetch_customer_settlement_balances(
                _FakeEngine(),
                organization_ref=ORG,
                opening_organization_field="_Fld7005RRef",
                movement_organization_field="_Fld7005RRef",
                counterparty_refs=[CP_1],
                query_timeout_seconds=invalid_timeout,
            )

    engine = _FakeEngine()
    with pytest.raises(CustomerSettlementSourceError, match="as_of_cannot_be_in_the_future"):
        fetch_customer_settlement_balances(
            engine,
            organization_ref=ORG,
            opening_organization_field="_Fld7005RRef",
            movement_organization_field="_Fld7005RRef",
            counterparty_refs=[CP_1],
            query_timeout_seconds=30,
            as_of=datetime(2026, 7, 29, 9, 31, tzinfo=UTC),
        )

    engine.dialect.name = "sqlite"
    with pytest.raises(CustomerSettlementSourceError, match="requires MSSQL"):
        fetch_customer_settlement_balances(
            engine,
            organization_ref=ORG,
            opening_organization_field="_Fld7005RRef",
            movement_organization_field="_Fld7005RRef",
            counterparty_refs=[CP_1],
            query_timeout_seconds=30,
        )


def test_manual_controls_support_nonhierarchical_organization_and_ut103_element_flag() -> None:
    engine = _ManualControlEngine()

    controls = fetch_manual_customer_settlement_controls(
        engine,
        organization_ref=ORG,
        organization_guid=onec_ref_to_guid(ORG),
        counterparty_guids=[onec_ref_to_guid(CP_1)],
        counterparty_inn_field="_Fld611",
        query_timeout_seconds=30,
    )

    assert len(controls) == 1
    assert controls[0].counterparty_ref == CP_1
    rendered_sql = "\n".join(engine.connection.sql)
    organization_sql = next(
        value for value in engine.connection.sql if "_Reference66 AS organization" in value
    )
    assert "organization._Folder" not in organization_sql
    assert "counterparty._Folder = 0x01" in rendered_sql


def test_worker_requires_explicit_source_reconciliation_gate() -> None:
    settings = Settings(
        _env_file=None,
        customer_settlements_shadow_enabled=True,
        customer_settlements_source_validated=False,
        customer_settlements_organization_ref=ORG,
        customer_settlements_opening_organization_field="_Fld7005RRef",
        customer_settlements_movement_organization_field="_Fld7005RRef",
        onec_database_url="mssql+pyodbc://synthetic",
    )
    assert run_customer_settlement_financial_sync(settings=settings) == {
        "status": "blocked",
        "reason": "financial_source_not_validated",
    }


def test_customer_settlement_rollout_flags_are_fail_closed_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.customer_settlements_enabled is False
    assert settings.customer_settlements_shadow_enabled is False
    assert settings.customer_settlements_source_validated is False
    assert run_customer_settlement_mapping_sync(settings=settings) == {"status": "disabled"}


@pytest.mark.parametrize(
    ("worker_status", "exit_code"),
    [
        ("activated", 0),
        ("unchanged", 0),
        ("skipped_lock", 0),
        ("disabled", 0),
        ("blocked", 2),
        ("error", 1),
    ],
)
def test_mapping_task_distinguishes_retryable_errors_from_blocked_states(
    monkeypatch,
    worker_status: str,
    exit_code: int,
) -> None:
    monkeypatch.setattr(
        sync_customer_settlement_mapping,
        "run_customer_settlement_mapping_sync",
        lambda: {"status": worker_status},
    )
    assert sync_customer_settlement_mapping.main() == exit_code


def test_mapping_task_retries_transient_context_lock(monkeypatch) -> None:
    monkeypatch.setattr(
        sync_customer_settlement_mapping,
        "run_customer_settlement_mapping_sync",
        lambda: {"status": "skipped_lock", "reason": "context_lock"},
    )

    assert sync_customer_settlement_mapping.main() == 1


@pytest.mark.parametrize("timeout_seconds", (0, 31))
def test_mapping_worker_blocks_unbounded_source_timeout(timeout_seconds) -> None:
    settings = Settings(
        _env_file=None,
        customer_settlements_shadow_enabled=True,
        customer_settlements_mapping_mode="crm_readonly",
        customer_settlements_query_timeout_seconds=timeout_seconds,
    )

    assert run_customer_settlement_mapping_sync(settings=settings) == {
        "status": "blocked",
        "reason": "mapping_source_timeout_invalid",
    }


@pytest.mark.parametrize(
    ("worker_status", "exit_code"),
    [
        ("activated", 0),
        ("unchanged", 0),
        ("skipped_lock", 0),
        ("blocked", 0),
        ("disabled", 0),
        ("error", 1),
    ],
)
def test_financial_task_retries_only_real_errors(
    monkeypatch,
    worker_status: str,
    exit_code: int,
) -> None:
    monkeypatch.setattr(
        sync_customer_settlements,
        "run_customer_settlement_financial_sync",
        lambda: {"status": worker_status},
    )
    assert sync_customer_settlements.main() == exit_code


def test_financial_task_retries_transient_context_lock(monkeypatch) -> None:
    monkeypatch.setattr(
        sync_customer_settlements,
        "run_customer_settlement_financial_sync",
        lambda: {"status": "skipped_lock", "reason": "context_lock"},
    )

    assert sync_customer_settlements.main() == 1


@pytest.mark.parametrize(
    ("freshness_status", "mapping_status", "exit_code"),
    [
        ("ok", "ok", 0),
        ("warning", "ok", 1),
        ("ok", "critical", 2),
    ],
)
def test_health_task_exposes_monitoring_exit_codes(
    monkeypatch,
    freshness_status: str,
    mapping_status: str,
    exit_code: int,
) -> None:
    class FakeSession:
        def close(self):
            return None

    monkeypatch.setattr(
        check_customer_settlement_health,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            customer_settlements_source_validated=True,
        ),
    )
    monkeypatch.setattr(
        check_customer_settlement_health,
        "get_application_session_factory",
        lambda: lambda: FakeSession(),
    )
    monkeypatch.setattr(
        check_customer_settlement_health,
        "assert_expected_application_database",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        check_customer_settlement_health,
        "customer_settlement_health_metrics",
        lambda *args, **kwargs: {
            "freshness_status": freshness_status,
            "mapping_status": mapping_status,
        },
    )
    monkeypatch.setattr(
        check_customer_settlement_health,
        "active_customer_settlement_reconciliation_is_current",
        lambda *_args, **_kwargs: True,
    )
    assert check_customer_settlement_health.main() == exit_code


def test_health_task_marks_noncurrent_reconciliation_critical(monkeypatch, capsys) -> None:
    class FakeSession:
        def close(self):
            return None

    monkeypatch.setattr(
        check_customer_settlement_health,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            customer_settlements_source_validated=True,
        ),
    )
    monkeypatch.setattr(
        check_customer_settlement_health,
        "get_application_session_factory",
        lambda: lambda: FakeSession(),
    )
    monkeypatch.setattr(
        check_customer_settlement_health,
        "assert_expected_application_database",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        check_customer_settlement_health,
        "customer_settlement_health_metrics",
        lambda *args, **kwargs: {
            "freshness_status": "ok",
            "mapping_status": "ok",
        },
    )
    monkeypatch.setattr(
        check_customer_settlement_health,
        "active_customer_settlement_reconciliation_is_current",
        lambda *_args, **_kwargs: False,
    )

    assert check_customer_settlement_health.main() == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "critical"
    assert payload["metrics"]["reconciliation_current"] is False


def test_health_task_blocks_enabled_alerts_without_approved_delivery_config(
    monkeypatch,
    capsys,
) -> None:
    class FakeSession:
        def close(self):
            return None

    monkeypatch.setattr(
        check_customer_settlement_health,
        "get_settings",
        lambda: Settings(_env_file=None, customer_settlements_alerts_enabled=True),
    )
    monkeypatch.setattr(
        check_customer_settlement_health,
        "get_application_session_factory",
        lambda: lambda: FakeSession(),
    )
    monkeypatch.setattr(
        check_customer_settlement_health,
        "assert_expected_application_database",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        check_customer_settlement_health,
        "customer_settlement_health_metrics",
        lambda *args, **kwargs: {
            "freshness_status": "critical",
            "mapping_status": "critical",
            "expected_rows": 0,
            "loaded_rows": 0,
            "zero_rows": 0,
        },
    )

    assert check_customer_settlement_health.main() == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == ("alert_delivery_not_configured")


def test_mock_client_dry_run_never_prints_assertion_or_user_id(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        mock_customer_settlement_client,
        "get_settings",
        lambda: Settings(_env_file=None),
    )
    monkeypatch.setattr(
        mock_customer_settlement_client,
        "create_customer_settlement_assertion",
        lambda **kwargs: ("secret-assertion-value", 1_785_301_260),
    )
    assert (
        mock_customer_settlement_client.main(
            ["--site-user-id", "12345"],
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "secret-assertion-value" not in output
    assert "12345" not in output
    assert '"assertion_exposed": false' in output


def test_pilot_cli_settings_failure_is_sanitized(monkeypatch, capsys) -> None:
    def fail_settings():
        raise RuntimeError("private-settings-detail")

    monkeypatch.setattr(manage_customer_settlement_pilot, "get_settings", fail_settings)

    assert (
        manage_customer_settlement_pilot.main(
            ["--site-user-id", "12345", "--enable"],
        )
        == 2
    )
    output = capsys.readouterr().out
    assert "private-settings-detail" not in output
    assert "12345" not in output
    assert json.loads(output)["error_code"] == "pilot_configuration_failed"


def test_pilot_cli_dry_run_uses_guarded_database_and_rolls_back(
    db_session: Session,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        manage_customer_settlement_pilot,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            customer_settlements_correlation_salt="synthetic-correlation-salt",
        ),
    )
    monkeypatch.setattr(
        manage_customer_settlement_pilot,
        "get_application_session_factory",
        lambda: lambda: Session(db_session.get_bind()),
    )
    monkeypatch.setattr(
        manage_customer_settlement_pilot,
        "assert_expected_application_database",
        lambda *_args, **_kwargs: None,
    )

    assert (
        manage_customer_settlement_pilot.main(
            ["--site-user-id", "12345", "--enable"],
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "validated"
    assert payload["preview_enabled"] is True
    assert payload["would_change"] is True
    assert payload["rolled_back"] is True
    assert "12345" not in json.dumps(payload)
    assert db_session.scalar(select(func.count()).select_from(CustomerSettlementPilotAccess)) == 0


def test_pilot_cli_dry_run_reports_unknown_state_when_rollback_fails(
    monkeypatch,
    capsys,
) -> None:
    class FakeSession:
        def scalar(self, _statement):
            return None

        def refresh(self, _item):
            return None

        def rollback(self):
            raise RuntimeError("private-rollback-detail")

        def close(self):
            return None

    monkeypatch.setattr(
        manage_customer_settlement_pilot,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            customer_settlements_correlation_salt="synthetic-salt",
        ),
    )
    monkeypatch.setattr(
        manage_customer_settlement_pilot,
        "get_application_session_factory",
        lambda: lambda: FakeSession(),
    )
    monkeypatch.setattr(
        manage_customer_settlement_pilot,
        "assert_expected_application_database",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        manage_customer_settlement_pilot,
        "try_customer_settlement_context_lock",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        manage_customer_settlement_pilot,
        "set_pilot_access",
        lambda *_args, **_kwargs: (SimpleNamespace(enabled=True), True),
    )

    assert (
        manage_customer_settlement_pilot.main(
            ["--site-user-id", "12345", "--enable"],
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "private-rollback-detail" not in output
    payload = json.loads(output)
    assert payload["status"] == "error"
    assert payload["error_code"] == "pilot_dry_run_rollback_state_unknown"


def test_pilot_cli_distinguishes_committed_readback_failure(monkeypatch, capsys) -> None:
    class FakeSession:
        def commit(self):
            return None

        def refresh(self, _item):
            raise RuntimeError("private-readback-detail")

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(
        manage_customer_settlement_pilot,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            customer_settlements_correlation_salt="synthetic-salt",
        ),
    )
    monkeypatch.setattr(
        manage_customer_settlement_pilot,
        "get_application_session_factory",
        lambda: lambda: FakeSession(),
    )
    monkeypatch.setattr(
        manage_customer_settlement_pilot,
        "assert_expected_application_database",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        manage_customer_settlement_pilot,
        "set_pilot_access",
        lambda *_args, **_kwargs: (SimpleNamespace(enabled=True), True),
    )

    assert (
        manage_customer_settlement_pilot.main(
            ["--site-user-id", "12345", "--enable", "--apply"],
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "private-readback-detail" not in output
    assert json.loads(output)["error_code"] == "pilot_update_committed_readback_failed"


def test_pilot_cli_reports_unknown_state_when_commit_connection_fails(
    monkeypatch,
    capsys,
) -> None:
    class FakeSession:
        def commit(self):
            raise RuntimeError("private-commit-detail")

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(
        manage_customer_settlement_pilot,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            customer_settlements_correlation_salt="synthetic-salt",
        ),
    )
    monkeypatch.setattr(
        manage_customer_settlement_pilot,
        "get_application_session_factory",
        lambda: lambda: FakeSession(),
    )
    monkeypatch.setattr(
        manage_customer_settlement_pilot,
        "assert_expected_application_database",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        manage_customer_settlement_pilot,
        "set_pilot_access",
        lambda *_args, **_kwargs: (SimpleNamespace(enabled=True), True),
    )

    assert (
        manage_customer_settlement_pilot.main(
            ["--site-user-id", "12345", "--enable", "--apply"],
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "private-commit-detail" not in output
    assert json.loads(output)["error_code"] == "pilot_update_commit_state_unknown"


def test_cron_artifacts_bound_hung_processes_and_retry_only_real_errors() -> None:
    project_root = Path(__file__).resolve().parents[1]
    cron_template = (project_root / "infra/cron/customer_settlements.cron").read_text(
        encoding="utf-8"
    )
    scripts = {
        path.name: path.read_text(encoding="utf-8")
        for path in (
            project_root / "infra/cron/customer_settlement_mapping_sync.sh",
            project_root / "infra/cron/customer_settlement_financial_sync.sh",
            project_root / "infra/cron/customer_settlement_cleanup.sh",
            project_root / "infra/cron/customer_settlement_health.sh",
            project_root / "infra/cron/customer_settlement_shadow_checkpoint.sh",
        )
    }
    assert all("timeout --signal=TERM --kill-after=5s" in content for content in scripts.values())
    assert all("CUSTOMER_SETTLEMENTS_ENV_FILE" in content for content in scripts.values())
    assert all(
        "readonly REPO_DIR PYTHON_BIN ENV_FILE ENV_LOADER" in content
        for content in scripts.values()
    )
    assert (
        scripts["customer_settlement_financial_sync.sh"].count("-m tasks.sync_customer_settlements")
        == 1
    )
    assert (
        scripts["customer_settlement_mapping_sync.sh"].count(
            "-m tasks.sync_customer_settlement_mapping"
        )
        == 1
    )
    assert "first_exit_code == 2" in scripts["customer_settlement_mapping_sync.sh"]
    assert (
        "CUSTOMER_SETTLEMENTS_MAPPING_JOB_TIMEOUT_SECONDS"
        in scripts["customer_settlement_mapping_sync.sh"]
    )
    assert 'sleep "${RETRY_DELAY_SECONDS}"' in scripts["customer_settlement_mapping_sync.sh"]
    assert 'sleep "${RETRY_DELAY_SECONDS}"' in scripts["customer_settlement_financial_sync.sh"]
    assert (
        "REPO_DIR=/opt/MM/releases/pricing-service/customer-settlements-shadow-release"
        in cron_template
    )
    assert (
        "CUSTOMER_SETTLEMENTS_ENV_FILE=/etc/pricing-service/" "customer-settlements-shadow.env"
    ) in cron_template
    cron_jobs = [line for line in cron_template.splitlines() if line[:1].isdigit()]
    assert len(cron_jobs) == 4
    assert all(line.split()[5] == "root" for line in cron_jobs)
    assert "SHELL=/bin/bash" in cron_template
    assert "PYTHON_BIN=" not in cron_template
    assert "/opt/MM/pricing-service/" not in cron_template
    assert "/var/log/pricing-staging/" in cron_template


@pytest.mark.parametrize(
    ("exit_codes", "expected_calls", "expected_exit_code"),
    [
        ("0", 1, 0),
        ("1,0", 2, 0),
        ("1,1", 2, 1),
        ("2", 1, 2),
        ("124,0", 2, 0),
    ],
)
def test_mapping_cron_retries_only_retryable_errors(
    tmp_path: Path,
    exit_codes: str,
    expected_calls: int,
    expected_exit_code: int,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    fake_python = tmp_path / "fake-python"
    call_counter = tmp_path / "calls"
    fake_python.write_text(
        """#!/usr/bin/env python3
import os
from pathlib import Path
import sys

counter = Path(os.environ["FAKE_MAPPING_CALL_COUNTER"])
call_number = int(counter.read_text() or "0") if counter.exists() else 0
counter.write_text(str(call_number + 1))
exit_codes = [int(value) for value in os.environ["FAKE_MAPPING_EXIT_CODES"].split(",")]
raise SystemExit(exit_codes[min(call_number, len(exit_codes) - 1)])
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    env = {
        **os.environ,
        "REPO_DIR": str(project_root),
        "PYTHON_BIN": str(fake_python),
        "CUSTOMER_SETTLEMENTS_ENV_FILE": str(tmp_path / "missing.env"),
        "CUSTOMER_SETTLEMENTS_EXPECTED_DATABASE_NAME": "settlements-test",
        "CUSTOMER_SETTLEMENTS_JOB_TIMEOUT_SECONDS": "5",
        "CUSTOMER_SETTLEMENTS_MAPPING_JOB_TIMEOUT_SECONDS": "5",
        "CUSTOMER_SETTLEMENTS_RETRY_DELAY_SECONDS": "0",
        "FAKE_MAPPING_CALL_COUNTER": str(call_counter),
        "FAKE_MAPPING_EXIT_CODES": exit_codes,
    }

    result = subprocess.run(
        ["bash", str(project_root / "infra/cron/customer_settlement_mapping_sync.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == expected_exit_code
    assert int(call_counter.read_text()) == expected_calls


def test_shadow_checkpoint_loads_expected_database_from_secret_env(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    fake_python = tmp_path / "fake-python"
    calls = tmp_path / "calls"
    fake_python.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-" ]]; then
  exec "${REAL_PYTHON}" "$@"
fi
printf '%s\n' "$*" >> "${FAKE_CHECKPOINT_CALLS}"
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    env_file = tmp_path / "shadow.env"
    env_file.write_text(
        "CUSTOMER_SETTLEMENTS_EXPECTED_DATABASE_NAME=settlements-shadow-test\n"
        "CUSTOMER_SETTLEMENTS_RECEIVABLE_ENV_FILE=/secure/receivables.env\n"
        "CUSTOMER_SETTLEMENTS_RECEIVABLE_EXPECTED_DATABASE_NAME=pricing\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(project_root / "infra/cron/customer_settlement_shadow_checkpoint.sh")],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "REPO_DIR": str(project_root),
            "PYTHON_BIN": str(fake_python),
            "REAL_PYTHON": sys.executable,
            "FAKE_CHECKPOINT_CALLS": str(calls),
            "CUSTOMER_SETTLEMENTS_ENV_FILE": str(env_file),
        },
    )

    assert result.returncode == 0
    invocations = calls.read_text(encoding="utf-8")
    assert "--expected-database-name settlements-shadow-test" in invocations
    assert "tasks.check_customer_settlement_receivable_drift" in invocations
    assert "--receivable-env-file /secure/receivables.env" in invocations
    assert "--expected-receivable-database-name pricing" in invocations
    assert "tasks.check_customer_settlement_health" in invocations


def test_env_loader_rejects_shell_metacharacters_in_variable_name(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    marker = tmp_path / "must-not-exist"
    env_file = tmp_path / "invalid.env"
    env_file.write_text(
        f"BAD$(touch {marker})=value\n",
        encoding="utf-8",
    )
    command = (
        f'source "{project_root / "infra/cron/load_env.sh"}"; '
        f'load_env_file_preserve_json "{env_file}"'
    )
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", command],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHON_BIN": sys.executable},
    )

    assert result.returncode != 0
    assert not marker.exists()
    assert "BAD" not in result.stderr


def test_settlement_cron_wrapper_rejects_secret_runtime_path_override(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    marker = tmp_path / "overridden-python-was-called"
    overridden_python = tmp_path / "overridden-python"
    overridden_python.write_text(
        f"#!/usr/bin/env bash\ntouch {marker}\n",
        encoding="utf-8",
    )
    overridden_python.chmod(0o700)
    env_file = tmp_path / "shadow.env"
    env_file.write_text(
        f"PYTHON_BIN={overridden_python}\n"
        "CUSTOMER_SETTLEMENTS_EXPECTED_DATABASE_NAME=settlements-shadow-test\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(project_root / "infra/cron/customer_settlement_health.sh")],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "REPO_DIR": str(project_root),
            "PYTHON_BIN": sys.executable,
            "CUSTOMER_SETTLEMENTS_ENV_FILE": str(env_file),
        },
    )

    assert result.returncode != 0
    assert not marker.exists()


def test_env_loader_propagates_parser_failure_without_errexit(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    env_file = tmp_path / "invalid.env"
    env_file.write_text("INVALID-NAME=value\n", encoding="utf-8")
    command = (
        f'source "{project_root / "infra/cron/load_env.sh"}"; '
        f'load_env_file_preserve_json "{env_file}"'
    )
    result = subprocess.run(
        ["bash", "-uo", "pipefail", "-c", command],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHON_BIN": sys.executable},
    )

    assert result.returncode != 0
    assert "INVALID-NAME" not in result.stderr
