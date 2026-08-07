from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services.customer_settlement_source import (
    CustomerSettlementSourceError,
    fetch_customer_settlement_balances,
    validate_organization_field,
)
from app.workers.customer_settlements import run_customer_settlement_financial_sync
from tasks import (
    check_customer_settlement_health,
    mock_customer_settlement_client,
    sync_customer_settlements,
)

ORG = "0x" + "a" * 32
CP_1 = "0x" + "1" * 32
CP_2 = "0x" + "2" * 32


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
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class _FakeConnection:
    def __init__(self):
        self.isolation_level: str | None = None
        self.sql: list[str] = []
        self.parameters: list[object] = []

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
        return _FakeTransaction()

    def exec_driver_sql(self, statement: str):
        self.sql.append(statement)
        return _FakeResult([])

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.sql.append(sql)
        self.parameters.append(parameters)
        if "SYSUTCDATETIME()" in sql:
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

    rendered_sql = "\n".join(engine.connection.sql)
    assert "NOLOCK" not in rendered_sql.upper()
    assert "r._Period < :movement_end" in rendered_sql
    assert "COALESCE(balances.signed_balance, 0)" in rendered_sql
    assert "#CustomerSettlementPilot" in rendered_sql
    query_parameters = next(
        value
        for value in engine.connection.parameters
        if isinstance(value, dict) and "movement_end" in value
    )
    assert query_parameters["movement_end"] == datetime(2026, 7, 29, 12, 15)
    assert engine.connection.isolation_level == "SNAPSHOT"


def test_extractor_rejects_unvalidated_dimensions_future_time_and_wrong_database() -> None:
    assert validate_organization_field("_Fld7005RRef") == "_Fld7005RRef"
    with pytest.raises(CustomerSettlementSourceError):
        validate_organization_field("_Fld7005")

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
        lambda: Settings(_env_file=None),
    )
    monkeypatch.setattr(
        check_customer_settlement_health,
        "get_application_session_factory",
        lambda: lambda: FakeSession(),
    )
    monkeypatch.setattr(
        check_customer_settlement_health,
        "customer_settlement_health_metrics",
        lambda *args, **kwargs: {
            "freshness_status": freshness_status,
            "mapping_status": mapping_status,
        },
    )
    assert check_customer_settlement_health.main() == exit_code


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


def test_cron_artifacts_bound_hung_processes_and_retry_only_financial_errors() -> None:
    project_root = Path(__file__).resolve().parents[1]
    scripts = {
        path.name: path.read_text(encoding="utf-8")
        for path in (
            project_root / "infra/cron/customer_settlement_mapping_sync.sh",
            project_root / "infra/cron/customer_settlement_financial_sync.sh",
            project_root / "infra/cron/customer_settlement_cleanup.sh",
            project_root / "infra/cron/customer_settlement_health.sh",
        )
    }
    assert all("timeout --signal=TERM --kill-after=5s" in content for content in scripts.values())
    assert (
        scripts["customer_settlement_financial_sync.sh"].count("-m tasks.sync_customer_settlements")
        == 1
    )
    assert 'sleep "${RETRY_DELAY_SECONDS}"' in scripts["customer_settlement_financial_sync.sh"]
