from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.customer_settlement import (
    CustomerSettlementAlertOutbox,
    CustomerSettlementReconciliationRun,
)
from app.services.customer_settlement_alerts import (
    dispatch_customer_settlement_alerts,
    enqueue_health_alert_if_needed,
    overall_health_status,
)
from app.services.customer_settlement_reconciliation import (
    CustomerSettlementReconciliationError,
    end_of_day_boundary_utc,
    reconcile_customer_settlement_rows,
    store_reconciliation_result,
)
from app.services.customer_settlement_source import (
    CustomerSettlementSourceResult,
    ManualCustomerSettlementControl,
)
from app.services.customer_settlements import (
    SettlementBalanceInput,
    SettlementMappingInput,
    activate_mapping_revision,
    onec_ref_to_guid,
    set_pilot_access,
)
from app.services.importers.onec_mutual_settlements import (
    OneCMutualSettlementCurrentBalanceRow,
)
from app.workers import customer_settlements as settlement_workers
from tasks import reconcile_customer_settlements as reconciliation_task

CP_1 = "0x" + "1" * 32
CP_2 = "0x" + "2" * 32
REPORT_DATE = date(2026, 8, 22)


def _control(ref: str, name: str) -> ManualCustomerSettlementControl:
    return ManualCustomerSettlementControl(
        counterparty_ref=ref,
        counterparty_guid=onec_ref_to_guid(ref),
        counterparty_code="test",
        counterparty_name=name,
        counterparty_inn="",
        active_contract_currency_codes=("643",),
    )


def test_end_of_day_reconciliation_is_exact_and_idempotently_stored(
    db_session: Session,
) -> None:
    as_of = end_of_day_boundary_utc(REPORT_DATE)
    assert as_of == datetime(2026, 8, 22, 21, 0, tzinfo=UTC)
    source = CustomerSettlementSourceResult(
        source_db_time=as_of + timedelta(minutes=5),
        as_of=as_of,
        balances=(
            SettlementBalanceInput(CP_1, Decimal("10.00")),
            SettlementBalanceInput(CP_2, Decimal("-5.00")),
        ),
        isolation_level="SNAPSHOT",
        duration_seconds=0.1,
    )
    rows = [
        OneCMutualSettlementCurrentBalanceRow(REPORT_DATE, "  Клиент   Один ", Decimal("10"), 1),
        OneCMutualSettlementCurrentBalanceRow(REPORT_DATE, "КЛИЕНТ ДВА", Decimal("-5"), 2),
    ]
    result = reconcile_customer_settlement_rows(
        report_hash="a" * 64,
        context_hash="f" * 64,
        report_rows=rows,
        controls=(_control(CP_1, "Клиент Один"), _control(CP_2, "Клиент Два")),
        source=source,
    )
    assert result.status == "matched"
    assert result.mismatch_count == 0
    first = store_reconciliation_result(db_session, result)
    db_session.flush()
    second = store_reconciliation_result(db_session, result)
    assert second.id == first.id

    changed_source = CustomerSettlementSourceResult(
        source_db_time=as_of + timedelta(minutes=10),
        as_of=as_of,
        balances=(
            SettlementBalanceInput(CP_1, Decimal("11.00")),
            SettlementBalanceInput(CP_2, Decimal("-5.00")),
        ),
        isolation_level="SNAPSHOT",
        duration_seconds=0.1,
    )
    changed = reconcile_customer_settlement_rows(
        report_hash="a" * 64,
        context_hash="f" * 64,
        report_rows=rows,
        controls=(_control(CP_1, "Клиент Один"), _control(CP_2, "Клиент Два")),
        source=changed_source,
    )
    third = store_reconciliation_result(db_session, changed)
    assert third.id != first.id
    assert third.status == "mismatched"


def test_reconciliation_rejects_duplicate_control_names_and_source_rows() -> None:
    as_of = end_of_day_boundary_utc(REPORT_DATE)
    rows = [
        OneCMutualSettlementCurrentBalanceRow(REPORT_DATE, "Клиент Один", Decimal("10"), 1),
        OneCMutualSettlementCurrentBalanceRow(REPORT_DATE, "Клиент Два", Decimal("-5"), 2),
    ]
    source = CustomerSettlementSourceResult(
        source_db_time=as_of + timedelta(minutes=5),
        as_of=as_of,
        balances=(
            SettlementBalanceInput(CP_1, Decimal("10.00")),
            SettlementBalanceInput(CP_2, Decimal("-5.00")),
        ),
        isolation_level="SNAPSHOT",
        duration_seconds=0.1,
    )
    with pytest.raises(CustomerSettlementReconciliationError, match="duplicate_pilot_name"):
        reconcile_customer_settlement_rows(
            report_hash="b" * 64,
            context_hash="f" * 64,
            report_rows=rows,
            controls=(_control(CP_1, "Одинаковое имя"), _control(CP_2, "Одинаковое имя")),
            source=source,
        )

    duplicate_source = CustomerSettlementSourceResult(
        source_db_time=as_of + timedelta(minutes=5),
        as_of=as_of,
        balances=(
            SettlementBalanceInput(CP_1, Decimal("10.00")),
            SettlementBalanceInput(CP_1, Decimal("10.00")),
        ),
        isolation_level="SNAPSHOT",
        duration_seconds=0.1,
    )
    with pytest.raises(CustomerSettlementReconciliationError, match="duplicate_counterparty"):
        reconcile_customer_settlement_rows(
            report_hash="c" * 64,
            context_hash="f" * 64,
            report_rows=rows[:1],
            controls=(_control(CP_1, "Клиент Один"),),
            source=duplicate_source,
        )


def test_health_alerts_are_transition_based_and_do_not_contain_financial_data(
    db_session: Session,
) -> None:
    now = datetime(2026, 8, 22, 20, 0, tzinfo=UTC)
    ok = {
        "freshness_status": "ok",
        "mapping_status": "ok",
        "expected_rows": 10,
        "loaded_rows": 10,
        "zero_rows": 1,
    }
    critical = {**ok, "freshness_status": "critical"}
    assert (
        enqueue_health_alert_if_needed(db_session, metrics=ok, repeat_seconds=21600, now=now)
        is None
    )
    alert = enqueue_health_alert_if_needed(
        db_session,
        metrics=critical,
        repeat_seconds=21600,
        now=now + timedelta(minutes=1),
    )
    assert isinstance(alert, CustomerSettlementAlertOutbox)
    assert "суммы" in alert.message
    assert "site_user" not in alert.message
    db_session.flush()
    assert (
        enqueue_health_alert_if_needed(
            db_session,
            metrics=critical,
            repeat_seconds=21600,
            now=now + timedelta(hours=1),
        )
        is None
    )
    recovery = enqueue_health_alert_if_needed(
        db_session,
        metrics=ok,
        repeat_seconds=21600,
        now=now + timedelta(hours=2),
    )
    assert recovery is not None
    assert "восстановлено" in recovery.message


def test_health_alerts_fail_closed_and_sanitize_invalid_metrics(db_session: Session) -> None:
    assert overall_health_status({}) == "critical"
    metrics = {
        "freshness_status": "private-client-id",
        "mapping_status": "ok",
        "expected_rows": "private-client-id",
        "loaded_rows": -1,
        "zero_rows": True,
    }
    alert = enqueue_health_alert_if_needed(
        db_session,
        metrics=metrics,
        repeat_seconds=21600,
        now=datetime(2026, 8, 22, 20, 0, tzinfo=UTC),
    )
    assert alert is not None
    assert "private-client-id" not in alert.message
    assert "unknown/unknown/unknown" in alert.message


def test_alert_delivery_is_restricted_to_approved_task(db_session: Session) -> None:
    with pytest.raises(RuntimeError, match="task_is_not_allowed"):
        dispatch_customer_settlement_alerts(
            db_session,
            webhook_url="https://example.invalid/rest/1/token",
            task_id="9999",
        )


def test_alert_delivery_reports_exhausted_outbox_rows(db_session: Session) -> None:
    now = datetime(2026, 8, 22, 20, 0, tzinfo=UTC)
    db_session.add(
        CustomerSettlementAlertOutbox(
            event_key="e" * 64,
            status="failed",
            severity="critical",
            message="synthetic safe alert",
            attempt_count=5,
            next_attempt_at=now,
        )
    )
    db_session.flush()

    assert dispatch_customer_settlement_alerts(
        db_session,
        webhook_url="https://example.invalid/rest/1/token",
        task_id="2883",
        now=now,
    ) == {"processed": 0, "sent": 0, "failed": 0, "exhausted": 1}


def test_reconciliation_task_hides_report_path_on_parse_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report_path = tmp_path / "sensitive-report-name.xlsx"
    report_path.write_bytes(b"not-an-xlsx")
    monkeypatch.setattr(
        reconciliation_task,
        "get_settings",
        lambda: SimpleNamespace(onec_database_url="mssql://synthetic"),
    )

    assert reconciliation_task.main([str(report_path)]) == 2
    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "blocked", "error_code": "report_parse_failed"}
    assert str(report_path) not in output


def test_reconciliation_task_hides_unexpected_source_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report_path = tmp_path / "private-statement.xlsx"
    report_path.write_bytes(b"synthetic")
    monkeypatch.setattr(
        reconciliation_task,
        "get_settings",
        lambda: SimpleNamespace(onec_database_url="mssql://synthetic"),
    )
    monkeypatch.setattr(reconciliation_task, "report_sha256", lambda path: "a" * 64)
    monkeypatch.setattr(
        reconciliation_task,
        "load_onec_mutual_settlements_current_balances_file",
        lambda *args, **kwargs: [
            OneCMutualSettlementCurrentBalanceRow(
                REPORT_DATE,
                "Synthetic",
                Decimal("0.00"),
                1,
            )
        ],
    )

    def fail_session_factory():
        raise RuntimeError("sensitive-driver-details")

    monkeypatch.setattr(
        reconciliation_task,
        "get_application_session_factory",
        fail_session_factory,
    )

    assert reconciliation_task.main([str(report_path)]) == 2
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "status": "blocked",
        "error_code": "reconciliation_failed",
    }
    assert "sensitive-driver-details" not in output
    assert str(report_path) not in output


def test_financial_worker_rejects_reconciliation_from_another_pilot_context(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_ref = "0x" + "a" * 32
    organization_guid = onec_ref_to_guid(organization_ref)
    mapping, _ = activate_mapping_revision(
        db_session,
        entries=(SettlementMappingInput("101", "cluster-a", CP_1, "linked"),),
        source_checked_at=datetime(2026, 8, 23, 10, 0, tzinfo=UTC),
        organization_ref=organization_ref,
        organization_guid=organization_guid,
    )
    set_pilot_access(db_session, site_user_id="101", enabled=True)
    db_session.add(
        CustomerSettlementReconciliationRun(
            report_date=REPORT_DATE,
            as_of=end_of_day_boundary_utc(REPORT_DATE),
            report_hash="a" * 64,
            context_hash="b" * 64,
            source_hash="c" * 64,
            input_hash="d" * 64,
            status="matched",
            expected_count=1,
            matched_count=1,
            mismatch_count=0,
            max_abs_difference=Decimal("0.00"),
        )
    )
    db_session.commit()
    assert mapping.source_hash != "b" * 64

    monkeypatch.setattr(
        settlement_workers,
        "get_application_session_factory",
        lambda: lambda: Session(db_session.get_bind()),
    )
    settings = Settings(
        _env_file=None,
        customer_settlements_shadow_enabled=True,
        customer_settlements_source_validated=True,
        customer_settlements_organization_ref=organization_ref,
        customer_settlements_organization_guid=organization_guid,
        customer_settlements_opening_organization_field="_Fld7005RRef",
        customer_settlements_movement_organization_field="_Fld7005RRef",
        onec_database_url="mssql+pyodbc://synthetic",
    )

    assert settlement_workers.run_customer_settlement_financial_sync(settings=settings) == {
        "status": "blocked",
        "reason": "financial_reconciliation_not_current",
    }
