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
from app.services import customer_settlement_alerts as settlement_alerts
from app.services.customer_settlement_alerts import (
    dispatch_customer_settlement_alerts,
    enqueue_health_alert_if_needed,
    overall_health_status,
)
from app.services.customer_settlement_reconciliation import (
    CustomerSettlementReconciliationError,
    customer_settlement_reconciliation_context_hash,
    customer_settlement_reconciliation_input_hash,
    customer_settlement_reconciliation_run_is_current,
    end_of_day_boundary_utc,
    latest_customer_settlement_reconciliation,
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
CP_3 = "0x" + "3" * 32
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

    with pytest.raises(
        CustomerSettlementReconciliationError,
        match="reconciliation_result_is_superseded",
    ):
        store_reconciliation_result(db_session, result)

    first.report_hash = "e" * 64
    db_session.flush()
    with pytest.raises(CustomerSettlementReconciliationError, match="payload_mismatch"):
        store_reconciliation_result(db_session, result)


def test_unfiltered_report_may_omit_only_an_explicit_source_zero() -> None:
    as_of = end_of_day_boundary_utc(REPORT_DATE)
    source = CustomerSettlementSourceResult(
        source_db_time=as_of + timedelta(minutes=5),
        as_of=as_of,
        balances=(
            SettlementBalanceInput(CP_1, Decimal("10.00")),
            SettlementBalanceInput(CP_2, Decimal("0.00")),
        ),
        isolation_level="SNAPSHOT",
        duration_seconds=0.1,
    )
    report_rows = [
        OneCMutualSettlementCurrentBalanceRow(
            REPORT_DATE,
            "Клиент Один",
            Decimal("10.00"),
            1,
        )
    ]
    controls = (_control(CP_1, "Клиент Один"), _control(CP_2, "Клиент Два"))

    with pytest.raises(
        CustomerSettlementReconciliationError,
        match="pilot_missing_from_report_or_source",
    ):
        reconcile_customer_settlement_rows(
            report_hash="a" * 64,
            context_hash="f" * 64,
            report_rows=report_rows,
            controls=controls,
            source=source,
        )

    result = reconcile_customer_settlement_rows(
        report_hash="a" * 64,
        context_hash="f" * 64,
        report_rows=report_rows,
        controls=controls,
        source=source,
        report_allows_implicit_zero_rows=True,
    )
    assert result.status == "matched"
    assert result.expected_count == 2
    assert result.matched_count == 2


def test_unfiltered_report_cannot_hide_a_nonzero_source_balance() -> None:
    as_of = end_of_day_boundary_utc(REPORT_DATE)
    source = CustomerSettlementSourceResult(
        source_db_time=as_of + timedelta(minutes=5),
        as_of=as_of,
        balances=(SettlementBalanceInput(CP_1, Decimal("0.01")),),
        isolation_level="SNAPSHOT",
        duration_seconds=0.1,
    )

    with pytest.raises(
        CustomerSettlementReconciliationError,
        match="pilot_missing_from_report_or_source",
    ):
        reconcile_customer_settlement_rows(
            report_hash="a" * 64,
            context_hash="f" * 64,
            report_rows=[
                OneCMutualSettlementCurrentBalanceRow(
                    REPORT_DATE,
                    "Другой клиент",
                    Decimal("0.00"),
                    1,
                )
            ],
            controls=(_control(CP_1, "Клиент Один"),),
            source=source,
            report_allows_implicit_zero_rows=True,
        )


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


def test_all_linked_reconciliation_aggregates_duplicate_names() -> None:
    as_of = end_of_day_boundary_utc(REPORT_DATE)
    result = reconcile_customer_settlement_rows(
        report_hash="b" * 64,
        context_hash="f" * 64,
        report_rows=[
            OneCMutualSettlementCurrentBalanceRow(
                REPORT_DATE,
                "Одинаковое имя",
                Decimal("7.00"),
                1,
            ),
            OneCMutualSettlementCurrentBalanceRow(
                REPORT_DATE,
                "Одинаковое имя",
                Decimal("3.00"),
                2,
            ),
        ],
        controls=(
            _control(CP_1, "Одинаковое имя"),
            _control(CP_2, "Одинаковое имя"),
        ),
        source=CustomerSettlementSourceResult(
            source_db_time=as_of + timedelta(minutes=5),
            as_of=as_of,
            balances=(
                SettlementBalanceInput(CP_1, Decimal("4.00")),
                SettlementBalanceInput(CP_2, Decimal("6.00")),
            ),
            isolation_level="SNAPSHOT",
            duration_seconds=0.1,
        ),
        max_scope_users=20,
        aggregate_duplicate_names=True,
    )

    assert result.status == "matched"
    assert result.expected_count == result.matched_count == 2
    assert result.max_abs_difference == Decimal("0.00")


def test_reconciliation_requires_exact_unique_control_and_source_scope() -> None:
    as_of = end_of_day_boundary_utc(REPORT_DATE)
    rows = [
        OneCMutualSettlementCurrentBalanceRow(REPORT_DATE, "Клиент Один", Decimal("10"), 1),
        OneCMutualSettlementCurrentBalanceRow(REPORT_DATE, "Клиент Два", Decimal("20"), 2),
    ]
    source_with_unexpected_counterparty = CustomerSettlementSourceResult(
        source_db_time=as_of + timedelta(minutes=5),
        as_of=as_of,
        balances=(
            SettlementBalanceInput(CP_1, Decimal("10.00")),
            SettlementBalanceInput(CP_3, Decimal("20.00")),
        ),
        isolation_level="SNAPSHOT",
        duration_seconds=0.1,
    )
    with pytest.raises(
        CustomerSettlementReconciliationError,
        match="source_pilot_count_mismatch",
    ):
        reconcile_customer_settlement_rows(
            report_hash="d" * 64,
            context_hash="f" * 64,
            report_rows=rows,
            controls=(_control(CP_1, "Клиент Один"), _control(CP_2, "Клиент Два")),
            source=source_with_unexpected_counterparty,
        )

    duplicate_controls = (
        _control(CP_1, "Клиент Один"),
        _control(CP_1, "Клиент Два"),
    )
    with pytest.raises(
        CustomerSettlementReconciliationError,
        match="control_identity_is_invalid",
    ):
        reconcile_customer_settlement_rows(
            report_hash="e" * 64,
            context_hash="f" * 64,
            report_rows=rows,
            controls=duplicate_controls,
            source=source_with_unexpected_counterparty,
        )


@pytest.mark.parametrize("tolerance", (Decimal("0"), Decimal("0.02"), Decimal("NaN")))
def test_reconciliation_rejects_non_contract_tolerance(tolerance: Decimal) -> None:
    as_of = end_of_day_boundary_utc(REPORT_DATE)
    source = CustomerSettlementSourceResult(
        source_db_time=as_of + timedelta(minutes=5),
        as_of=as_of,
        balances=(SettlementBalanceInput(CP_1, Decimal("10.00")),),
        isolation_level="SNAPSHOT",
        duration_seconds=0.1,
    )

    with pytest.raises(CustomerSettlementReconciliationError, match="tolerance_is_invalid"):
        reconcile_customer_settlement_rows(
            report_hash="a" * 64,
            context_hash="f" * 64,
            report_rows=[
                OneCMutualSettlementCurrentBalanceRow(
                    REPORT_DATE,
                    "Клиент Один",
                    Decimal("10"),
                    1,
                )
            ],
            controls=(_control(CP_1, "Клиент Один"),),
            source=source,
            tolerance=tolerance,
        )


def test_reconciliation_rejects_empty_pilot_and_inactive_source_identity() -> None:
    as_of = end_of_day_boundary_utc(REPORT_DATE)
    report_rows = [
        OneCMutualSettlementCurrentBalanceRow(
            REPORT_DATE,
            "Клиент Один",
            Decimal("0"),
            1,
        )
    ]
    inactive_source = CustomerSettlementSourceResult(
        source_db_time=as_of + timedelta(minutes=5),
        as_of=as_of,
        balances=(
            SettlementBalanceInput(
                CP_1,
                Decimal("0.00"),
                exists=False,
            ),
        ),
        isolation_level="SNAPSHOT",
        duration_seconds=0.1,
    )

    with pytest.raises(CustomerSettlementReconciliationError, match="pilot_count_is_invalid"):
        reconcile_customer_settlement_rows(
            report_hash="a" * 64,
            context_hash="f" * 64,
            report_rows=report_rows,
            controls=(),
            source=inactive_source,
        )
    with pytest.raises(CustomerSettlementReconciliationError, match="source_identity_is_invalid"):
        reconcile_customer_settlement_rows(
            report_hash="a" * 64,
            context_hash="f" * 64,
            report_rows=report_rows,
            controls=(_control(CP_1, "Клиент Один"),),
            source=inactive_source,
        )


def test_current_reconciliation_requires_completed_report_boundary() -> None:
    report_hash = "a" * 64
    context_hash = "b" * 64
    source_hash = "c" * 64
    valid = {
        "report_date": REPORT_DATE,
        "as_of": end_of_day_boundary_utc(REPORT_DATE),
        "report_hash": report_hash,
        "context_hash": context_hash,
        "source_hash": source_hash,
        "input_hash": customer_settlement_reconciliation_input_hash(
            report_hash=report_hash,
            context_hash=context_hash,
            source_hash=source_hash,
        ),
        "status": "matched",
        "expected_count": 1,
        "matched_count": 1,
        "mismatch_count": 0,
        "max_abs_difference": Decimal("0.00"),
    }
    assert customer_settlement_reconciliation_run_is_current(
        SimpleNamespace(**valid),
        context_hash=context_hash,
        expected_count=1,
    )
    assert not customer_settlement_reconciliation_run_is_current(
        SimpleNamespace(
            **{
                **valid,
                "as_of": end_of_day_boundary_utc(REPORT_DATE) + timedelta(seconds=1),
            }
        ),
        context_hash=context_hash,
        expected_count=1,
    )


def test_latest_reconciliation_uses_insert_order_when_database_clock_moves_back(
    db_session: Session,
) -> None:
    common = {
        "report_date": REPORT_DATE,
        "as_of": end_of_day_boundary_utc(REPORT_DATE),
        "context_hash": "b" * 64,
        "source_hash": "c" * 64,
        "status": "matched",
        "expected_count": 1,
        "matched_count": 1,
        "mismatch_count": 0,
        "max_abs_difference": Decimal("0.00"),
    }
    first = CustomerSettlementReconciliationRun(
        **common,
        report_hash="a" * 64,
        input_hash="d" * 64,
        created_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
    )
    db_session.add(first)
    db_session.flush()
    second = CustomerSettlementReconciliationRun(
        **common,
        report_hash="e" * 64,
        input_hash="f" * 64,
        created_at=datetime(2026, 8, 23, 11, 59, tzinfo=UTC),
    )
    db_session.add(second)
    db_session.flush()

    assert latest_customer_settlement_reconciliation(db_session).id == second.id


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
    assert alert.message == (
        "Взаиморасчёты: требуется проверка\n"
        "Финансовые данные: требуется проверка.\n"
        "Связь кабинетов с клиентами 1С: работает.\n"
        "Загружено клиентов: 10 из 10.\n"
        "Без долга и аванса: 1.\n"
        "Суммы и данные клиентов в сообщение не включаются."
    )
    assert "mapping" not in alert.message
    assert "expected" not in alert.message
    assert "loaded" not in alert.message
    assert "zero" not in alert.message
    assert "critical" not in alert.message.lower()
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
    assert recovery.message == (
        "Взаиморасчёты снова работают нормально\n"
        "Финансовые данные: обновлены.\n"
        "Связь кабинетов с клиентами 1С: работает.\n"
        "Загружено клиентов: 10 из 10.\n"
        "Без долга и аванса: 1.\n"
        "Суммы и данные клиентов в сообщение не включаются."
    )


def test_same_alert_transition_is_not_lost_inside_repeat_window(db_session: Session) -> None:
    now = datetime(2026, 8, 22, 20, 0, tzinfo=UTC)
    ok = {"freshness_status": "ok", "mapping_status": "ok"}
    critical = {"freshness_status": "critical", "mapping_status": "ok"}

    assert (
        enqueue_health_alert_if_needed(
            db_session,
            metrics=ok,
            repeat_seconds=21600,
            now=now,
        )
        is None
    )
    first = enqueue_health_alert_if_needed(
        db_session,
        metrics=critical,
        repeat_seconds=21600,
        now=now + timedelta(minutes=1),
    )
    recovery = enqueue_health_alert_if_needed(
        db_session,
        metrics=ok,
        repeat_seconds=21600,
        now=now + timedelta(minutes=2),
    )
    second = enqueue_health_alert_if_needed(
        db_session,
        metrics=critical,
        repeat_seconds=21600,
        now=now + timedelta(minutes=3),
    )

    assert first is not None
    assert recovery is not None
    assert second is not None
    assert len({first.event_key, recovery.event_key, second.event_key}) == 3


def test_alert_enqueue_rejects_non_contract_repeat_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace()
    monkeypatch.setattr(
        settlement_alerts,
        "utc_now",
        lambda: pytest.fail("invalid repeat must fail before clock or database access"),
    )

    with pytest.raises(
        RuntimeError,
        match="customer_settlement_alert_repeat_is_invalid",
    ):
        enqueue_health_alert_if_needed(
            session,
            metrics={"freshness_status": "critical", "mapping_status": "critical"},
            repeat_seconds=0,
        )


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
    assert "не удалось определить" in alert.message
    assert "Загружено клиентов: не определено из не определено." in alert.message
    assert "Без долга и аванса: не определено." in alert.message


def test_alert_delivery_labels_the_deduplication_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_key = "d" * 64
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_request(*, url: str, payload: dict[str, str], timeout_seconds: float):
        calls.append((url, payload))
        assert timeout_seconds == 3.0
        if url.endswith("/task.commentitem.getlist.json"):
            return {"result": []}
        return {"result": "comment-42"}

    monkeypatch.setattr(settlement_alerts, "_request_bitrix_json", fake_request)

    assert (
        settlement_alerts._post_bitrix_comment(
            webhook_url="https://example.invalid/rest/1/token",
            task_id="2883",
            event_key=event_key,
            message="Взаиморасчёты: требуется проверка",
            timeout_seconds=3.0,
        )
        == "comment-42"
    )
    assert len(calls) == 2
    assert calls[1][1]["arFields[POST_MESSAGE]"] == (
        "Взаиморасчёты: требуется проверка\n"
        "Служебная метка для защиты от повторной отправки: "
        f"[#mm-settlements:{event_key}]"
    )


def test_alert_delivery_is_restricted_to_approved_task(db_session: Session) -> None:
    with pytest.raises(RuntimeError, match="task_is_not_allowed"):
        dispatch_customer_settlement_alerts(
            db_session,
            webhook_url="https://example.invalid/rest/1/token",
            task_id="9999",
        )


@pytest.mark.parametrize(
    "webhook_url",
    (
        "http://example.invalid/rest/1/token",
        "https://user:password@example.invalid/rest/1/token",
        "https://example.invalid/rest/1/token?query=1",
        "https://example.invalid/rest/1/token#fragment",
        "https://[broken/rest/1/token",
        "https://example.invalid:invalid/rest/1/token",
    ),
)
def test_alert_delivery_rejects_unsafe_webhook_url(
    db_session: Session,
    webhook_url: str,
) -> None:
    now = datetime(2026, 8, 22, 20, 0, tzinfo=UTC)
    db_session.add(
        CustomerSettlementAlertOutbox(
            event_key="f" * 64,
            status="pending",
            severity="critical",
            message="synthetic safe alert",
            attempt_count=0,
            next_attempt_at=now,
        )
    )
    db_session.flush()

    assert dispatch_customer_settlement_alerts(
        db_session,
        webhook_url=webhook_url,
        task_id=" 2883 ",
        now=now,
    ) == {"processed": 1, "sent": 0, "failed": 1, "exhausted": 0}


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


def test_alert_delivery_readback_prevents_duplicate_after_unknown_commit(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 22, 20, 0, tzinfo=UTC)
    event_key = "d" * 64
    db_session.add(
        CustomerSettlementAlertOutbox(
            event_key=event_key,
            status="pending",
            severity="critical",
            message="synthetic safe alert",
            attempt_count=0,
            next_attempt_at=now,
        )
    )
    db_session.flush()
    calls: list[str] = []

    def fake_request(*, url: str, payload: dict[str, str], timeout_seconds: float):
        calls.append(url)
        assert timeout_seconds == 3.0
        assert payload["TASKID"] == "2883"
        return {
            "result": [
                {
                    "ID": "comment-42",
                    "POST_MESSAGE": f"previous delivery\n[#mm-settlements:{event_key}]",
                }
            ]
        }

    monkeypatch.setattr(settlement_alerts, "_request_bitrix_json", fake_request)

    assert dispatch_customer_settlement_alerts(
        db_session,
        webhook_url="https://example.invalid/rest/1/token",
        task_id="2883",
        now=now,
    ) == {"processed": 1, "sent": 1, "failed": 0, "exhausted": 0}
    assert calls == ["https://example.invalid/rest/1/token/task.commentitem.getlist.json"]


def test_alert_delivery_readback_follows_checked_pagination(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 22, 20, 0, tzinfo=UTC)
    event_key = "e" * 64
    db_session.add(
        CustomerSettlementAlertOutbox(
            event_key=event_key,
            status="pending",
            severity="critical",
            message="synthetic safe alert",
            attempt_count=0,
            next_attempt_at=now,
        )
    )
    db_session.flush()
    starts: list[str] = []

    def fake_request(*, url: str, payload: dict[str, str], timeout_seconds: float):
        assert url.endswith("/task.commentitem.getlist.json")
        assert timeout_seconds == 3.0
        starts.append(payload["start"])
        if payload["start"] == "0":
            return {"result": [{"ID": "1", "POST_MESSAGE": "other"}], "next": 50}
        return {
            "result": [
                {
                    "ID": "2",
                    "POST_MESSAGE": f"previous delivery\n[#mm-settlements:{event_key}]",
                }
            ]
        }

    monkeypatch.setattr(settlement_alerts, "_request_bitrix_json", fake_request)

    result = dispatch_customer_settlement_alerts(
        db_session,
        webhook_url="https://example.invalid/rest/1/token",
        task_id="2883",
        now=now,
    )

    assert result == {"processed": 1, "sent": 1, "failed": 0, "exhausted": 0}
    assert starts == ["0", "50"]


@pytest.mark.parametrize(
    ("timeout_seconds", "max_attempts"),
    ((0.0, 5), (3.1, 5), (3.0, 0), (3.0, 6)),
)
def test_alert_delivery_rejects_non_contract_runtime_limits_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
    timeout_seconds: float,
    max_attempts: int,
) -> None:
    session = SimpleNamespace()
    monkeypatch.setattr(
        settlement_alerts,
        "utc_now",
        lambda: pytest.fail("invalid limits must fail before clock or database access"),
    )

    with pytest.raises(RuntimeError, match="delivery_contract_is_invalid"):
        dispatch_customer_settlement_alerts(
            session,
            webhook_url="https://example.invalid/rest/1/token",
            task_id="2883",
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
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


def test_reconciliation_task_locks_and_rechecks_context_before_store(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    organization_ref = "0x" + "a" * 32
    organization_guid = onec_ref_to_guid(organization_ref)
    activate_mapping_revision(
        db_session,
        entries=(SettlementMappingInput("101", "cluster-a", CP_1, "linked"),),
        source_checked_at=datetime(2026, 8, 23, 10, 0, tzinfo=UTC),
        organization_ref=organization_ref,
        organization_guid=organization_guid,
    )
    set_pilot_access(db_session, site_user_id="101", enabled=True)
    db_session.commit()
    report_path = tmp_path / "statement.xlsx"
    report_path.write_bytes(b"synthetic")
    settings = Settings(
        _env_file=None,
        customer_settlements_organization_ref=organization_ref,
        customer_settlements_organization_guid=organization_guid,
        customer_settlements_opening_organization_field="_Fld7005RRef",
        customer_settlements_movement_organization_field="_Fld7005RRef",
        onec_database_url="mssql+pyodbc://synthetic",
    )
    as_of = end_of_day_boundary_utc(REPORT_DATE)
    monkeypatch.setattr(reconciliation_task, "get_settings", lambda: settings)
    monkeypatch.setattr(reconciliation_task, "report_sha256", lambda _path: "a" * 64)
    monkeypatch.setattr(
        reconciliation_task,
        "load_onec_mutual_settlements_current_balances_file",
        lambda *_args, **_kwargs: [
            OneCMutualSettlementCurrentBalanceRow(
                REPORT_DATE,
                "Pilot",
                Decimal("0.00"),
                1,
            )
        ],
    )
    monkeypatch.setattr(
        reconciliation_task,
        "get_application_session_factory",
        lambda: lambda: Session(db_session.get_bind()),
    )
    monkeypatch.setattr(
        reconciliation_task,
        "assert_expected_application_database",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reconciliation_task,
        "build_onec_engine",
        lambda *_args, **_kwargs: SimpleNamespace(dispose=lambda: None),
    )
    monkeypatch.setattr(
        reconciliation_task,
        "fetch_manual_customer_settlement_controls",
        lambda *_args, **_kwargs: (_control(CP_1, "Pilot"),),
    )
    monkeypatch.setattr(
        reconciliation_task,
        "fetch_customer_settlement_balances",
        lambda *_args, **_kwargs: CustomerSettlementSourceResult(
            source_db_time=as_of,
            as_of=as_of,
            balances=(SettlementBalanceInput(CP_1, Decimal("0.00")),),
            isolation_level="READ COMMITTED",
            duration_seconds=0.1,
        ),
    )
    monkeypatch.setattr(
        reconciliation_task,
        "try_customer_settlement_context_lock",
        lambda _session: False,
    )
    monkeypatch.setattr(
        reconciliation_task,
        "store_reconciliation_result",
        lambda *_args, **_kwargs: pytest.fail("unlocked reconciliation must not be stored"),
    )

    assert reconciliation_task.main([str(report_path)]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked",
        "error_code": "settlement_context_busy",
    }


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
    monkeypatch.setattr(
        settlement_workers,
        "assert_expected_application_database",
        lambda *_args, **_kwargs: None,
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


def test_financial_worker_rechecks_mapping_context_before_activation(
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
    context_hash = customer_settlement_reconciliation_context_hash(
        mapping_source_hash=mapping.source_hash,
        organization_ref=organization_ref,
        organization_guid=organization_guid,
        source_mode=settings.customer_settlements_source_mode,
        opening_organization_field="_Fld7005RRef",
        movement_organization_field="_Fld7005RRef",
        counterparty_refs=(CP_1,),
    )
    report_hash = "a" * 64
    source_hash = "c" * 64
    db_session.add(
        CustomerSettlementReconciliationRun(
            report_date=REPORT_DATE,
            as_of=end_of_day_boundary_utc(REPORT_DATE),
            report_hash=report_hash,
            context_hash=context_hash,
            source_hash=source_hash,
            input_hash=customer_settlement_reconciliation_input_hash(
                report_hash=report_hash,
                context_hash=context_hash,
                source_hash=source_hash,
            ),
            status="matched",
            expected_count=1,
            matched_count=1,
            mismatch_count=0,
            max_abs_difference=Decimal("0.00"),
        )
    )
    db_session.commit()

    scopes = iter(((CP_1,), (CP_2,)))
    monkeypatch.setattr(
        settlement_workers,
        "get_application_session_factory",
        lambda: lambda: Session(db_session.get_bind()),
    )
    monkeypatch.setattr(
        settlement_workers,
        "assert_expected_application_database",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        settlement_workers,
        "active_pilot_counterparty_refs",
        lambda _session: next(scopes),
    )
    monkeypatch.setattr(
        settlement_workers,
        "active_pilot_site_user_ids",
        lambda _session: ("101",),
    )
    monkeypatch.setattr(
        settlement_workers,
        "build_onec_engine",
        lambda *_args, **_kwargs: SimpleNamespace(dispose=lambda: None),
    )
    monkeypatch.setattr(
        settlement_workers,
        "fetch_customer_settlement_balances",
        lambda *_args, **_kwargs: CustomerSettlementSourceResult(
            source_db_time=datetime(2026, 8, 23, 10, 30, tzinfo=UTC),
            as_of=datetime(2026, 8, 23, 10, 30, tzinfo=UTC),
            balances=(SettlementBalanceInput(CP_1, Decimal("10.00")),),
            isolation_level="READ COMMITTED",
            duration_seconds=0.1,
        ),
    )
    monkeypatch.setattr(
        settlement_workers,
        "activate_financial_revision",
        lambda *_args, **_kwargs: pytest.fail("stale context must not be activated"),
    )

    assert settlement_workers.run_customer_settlement_financial_sync(settings=settings) == {
        "status": "blocked",
        "reason": "financial_context_changed",
    }


def test_financial_worker_locks_final_context_after_source_read(
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
    context_hash = customer_settlement_reconciliation_context_hash(
        mapping_source_hash=mapping.source_hash,
        organization_ref=organization_ref,
        organization_guid=organization_guid,
        source_mode=settings.customer_settlements_source_mode,
        opening_organization_field="_Fld7005RRef",
        movement_organization_field="_Fld7005RRef",
        counterparty_refs=(CP_1,),
    )
    report_hash = "a" * 64
    source_hash = "c" * 64
    db_session.add(
        CustomerSettlementReconciliationRun(
            report_date=REPORT_DATE,
            as_of=end_of_day_boundary_utc(REPORT_DATE),
            report_hash=report_hash,
            context_hash=context_hash,
            source_hash=source_hash,
            input_hash=customer_settlement_reconciliation_input_hash(
                report_hash=report_hash,
                context_hash=context_hash,
                source_hash=source_hash,
            ),
            status="matched",
            expected_count=1,
            matched_count=1,
            mismatch_count=0,
            max_abs_difference=Decimal("0.00"),
        )
    )
    db_session.commit()

    source_read = False

    def fetch_source(*_args, **_kwargs):
        nonlocal source_read
        source_read = True
        return CustomerSettlementSourceResult(
            source_db_time=datetime(2026, 8, 23, 10, 30, tzinfo=UTC),
            as_of=datetime(2026, 8, 23, 10, 30, tzinfo=UTC),
            balances=(SettlementBalanceInput(CP_1, Decimal("10.00")),),
            isolation_level="READ COMMITTED",
            duration_seconds=0.1,
        )

    monkeypatch.setattr(
        settlement_workers,
        "get_application_session_factory",
        lambda: lambda: Session(db_session.get_bind()),
    )
    monkeypatch.setattr(
        settlement_workers,
        "assert_expected_application_database",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        settlement_workers,
        "build_onec_engine",
        lambda *_args, **_kwargs: SimpleNamespace(dispose=lambda: None),
    )
    monkeypatch.setattr(
        settlement_workers,
        "fetch_customer_settlement_balances",
        fetch_source,
    )
    monkeypatch.setattr(
        settlement_workers,
        "try_customer_settlement_context_lock",
        lambda _session: False if source_read else pytest.fail("lock acquired before source read"),
    )
    monkeypatch.setattr(
        settlement_workers,
        "activate_financial_revision",
        lambda *_args, **_kwargs: pytest.fail("activation must not run without the lock"),
    )

    assert settlement_workers.run_customer_settlement_financial_sync(settings=settings) == {
        "status": "skipped_lock",
        "reason": "context_lock",
    }


def test_financial_worker_rechecks_latest_reconciliation_under_context_lock(
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
    db_session.commit()
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
    context_hash = customer_settlement_reconciliation_context_hash(
        mapping_source_hash=mapping.source_hash,
        organization_ref=organization_ref,
        organization_guid=organization_guid,
        source_mode=settings.customer_settlements_source_mode,
        opening_organization_field="_Fld7005RRef",
        movement_organization_field="_Fld7005RRef",
        counterparty_refs=(CP_1,),
    )
    report_hash = "a" * 64
    source_hash = "c" * 64
    valid = SimpleNamespace(
        status="matched",
        report_date=REPORT_DATE,
        as_of=end_of_day_boundary_utc(REPORT_DATE),
        report_hash=report_hash,
        context_hash=context_hash,
        source_hash=source_hash,
        input_hash=customer_settlement_reconciliation_input_hash(
            report_hash=report_hash,
            context_hash=context_hash,
            source_hash=source_hash,
        ),
        expected_count=1,
        matched_count=1,
        mismatch_count=0,
        max_abs_difference=Decimal("0.00"),
    )
    changed = SimpleNamespace(**{**vars(valid), "status": "mismatched"})
    reconciliations = iter((valid, changed))

    monkeypatch.setattr(
        settlement_workers,
        "get_application_session_factory",
        lambda: lambda: Session(db_session.get_bind()),
    )
    monkeypatch.setattr(
        settlement_workers,
        "assert_expected_application_database",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        settlement_workers,
        "_latest_customer_settlement_reconciliation",
        lambda _session: next(reconciliations),
    )
    monkeypatch.setattr(
        settlement_workers,
        "build_onec_engine",
        lambda *_args, **_kwargs: SimpleNamespace(dispose=lambda: None),
    )
    monkeypatch.setattr(
        settlement_workers,
        "fetch_customer_settlement_balances",
        lambda *_args, **_kwargs: CustomerSettlementSourceResult(
            source_db_time=datetime(2026, 8, 23, 10, 30, tzinfo=UTC),
            as_of=datetime(2026, 8, 23, 10, 30, tzinfo=UTC),
            balances=(SettlementBalanceInput(CP_1, Decimal("10.00")),),
            isolation_level="READ COMMITTED",
            duration_seconds=0.1,
        ),
    )
    monkeypatch.setattr(
        settlement_workers,
        "activate_financial_revision",
        lambda *_args, **_kwargs: pytest.fail("changed reconciliation must block activation"),
    )

    assert settlement_workers.run_customer_settlement_financial_sync(settings=settings) == {
        "status": "blocked",
        "reason": "financial_reconciliation_not_current",
    }
