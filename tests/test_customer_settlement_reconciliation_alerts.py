from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.models.customer_settlement import CustomerSettlementAlertOutbox
from app.services.customer_settlement_alerts import (
    dispatch_customer_settlement_alerts,
    enqueue_health_alert_if_needed,
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
from app.services.customer_settlements import SettlementBalanceInput, onec_ref_to_guid
from app.services.importers.onec_mutual_settlements import (
    OneCMutualSettlementCurrentBalanceRow,
)
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


def test_alert_delivery_is_restricted_to_approved_task(db_session: Session) -> None:
    with pytest.raises(RuntimeError, match="task_is_not_allowed"):
        dispatch_customer_settlement_alerts(
            db_session,
            webhook_url="https://example.invalid/rest/1/token",
            task_id="9999",
        )


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
