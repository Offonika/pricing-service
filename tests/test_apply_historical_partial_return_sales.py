from datetime import datetime
from decimal import Decimal

import pytest

from scripts import apply_historical_partial_return_sales as historical_returns


@pytest.mark.parametrize(
    ("payment", "refund", "ambiguous", "expected"),
    [
        ("1000.00", "1000.00", "0", "full_refund"),
        ("1000.00", "999.95", "0", "full_refund"),
        ("1000.00", "999.94", "0", "partial_refund"),
        ("1000.00", "0", "1000.00", "ambiguous_refund"),
        ("1000.00", "0", "0", "no_refund"),
        ("0", "1000.00", "0", "partial_refund"),
    ],
)
def test_money_refund_classification_is_fail_closed(
    payment: str,
    refund: str,
    ambiguous: str,
    expected: str,
) -> None:
    assert (
        historical_returns._classify_money_refund(  # noqa: SLF001
            payment_amount=Decimal(payment),
            refund_amount=Decimal(refund),
            ambiguous_refund_amount=Decimal(ambiguous),
        )
        == expected
    )


def test_explicit_order_selection_is_stable() -> None:
    candidates = [
        historical_returns.Candidate(order_number="100", deal_id=1),
        historical_returns.Candidate(order_number="200", deal_id=2),
        historical_returns.Candidate(order_number="300", deal_id=3),
    ]

    selected, offset = historical_returns._select_candidates(  # noqa: SLF001
        candidates,
        batch_number=None,
        order_numbers=["300", "100", "300"],
    )

    assert [item.order_number for item in selected] == ["300", "100"]
    assert offset == 0


def test_explicit_order_selection_rejects_closed_candidate() -> None:
    with pytest.raises(SystemExit, match="not open full-return candidates: 999"):
        historical_returns._select_candidates(  # noqa: SLF001
            [historical_returns.Candidate(order_number="100", deal_id=1)],
            batch_number=None,
            order_numbers=["999"],
        )


def test_explicit_partial_return_selection_has_specific_error() -> None:
    with pytest.raises(SystemExit, match="not open partial-return candidates: 999"):
        historical_returns._select_candidates(  # noqa: SLF001
            [historical_returns.Candidate(order_number="100", deal_id=1)],
            batch_number=None,
            order_numbers=["999"],
            candidate_kind="partial-return",
        )


def test_sale_amount_query_multiplies_quantity_by_unit_price(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class FakeMappings:
        def __iter__(self):
            return iter(())

    class FakeResult:
        def mappings(self):
            return FakeMappings()

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, _params):
            captured["statement"] = str(statement)
            return FakeResult()

    class FakeEngine:
        def connect(self):
            return FakeConnection()

        def dispose(self):
            return None

    monkeypatch.setattr(historical_returns, "build_engine", lambda *_args, **_kwargs: FakeEngine())
    monkeypatch.setattr(
        historical_returns,
        "get_settings",
        lambda: type("Settings", (), {"onec_database_url": "mssql://example"})(),
    )

    assert historical_returns._line_evidence(["100"]) == {}  # noqa: SLF001
    normalized = " ".join(captured["statement"].split())
    assert (
        "SUM( CAST(line._Fld4971 AS decimal(18, 4)) "
        "* CAST(line._Fld4982 AS decimal(18, 2)) ) AS sale_amount"
    ) in normalized


def test_pickup_payment_between_rtu_and_return_confirms_issue() -> None:
    before, after, qualifying_at = historical_returns._pickup_payment_sequence(  # noqa: SLF001
        [
            historical_returns.PaymentMovement(
                paid_at=datetime(2026, 6, 19, 14, 40),
                amount=Decimal("700"),
                source="cash_sale",
            )
        ],
        latest_rtu_at=datetime(2026, 6, 18, 13, 44),
        latest_return_at=datetime(2026, 6, 20, 15, 11),
        posted_sale_amount=Decimal("690"),
    )

    assert before == Decimal("700")
    assert after == Decimal("0")
    assert qualifying_at == datetime(2026, 6, 19, 14, 40)


def test_pickup_payment_created_after_return_does_not_confirm_issue() -> None:
    before, after, qualifying_at = historical_returns._pickup_payment_sequence(  # noqa: SLF001
        [
            historical_returns.PaymentMovement(
                paid_at=datetime(2026, 6, 20, 12, 44),
                amount=Decimal("700"),
                source="cash_sale",
            )
        ],
        latest_rtu_at=datetime(2026, 6, 19, 10, 0),
        latest_return_at=datetime(2026, 6, 20, 9, 39),
        posted_sale_amount=Decimal("690"),
    )

    assert before == Decimal("0")
    assert after == Decimal("700")
    assert qualifying_at is None


def test_issued_rtu_retained_or_returned_later_confirms_partial_sale() -> None:
    rows = [
        historical_returns.IssuedRtuMovement(
            rtu_number="RTU-1",
            sale_amount=Decimal("500"),
            issued=True,
            scanned_at=datetime(2026, 6, 1, 10, 0),
            returned_at=None,
        ),
        historical_returns.IssuedRtuMovement(
            rtu_number="RTU-2",
            sale_amount=Decimal("700"),
            issued=True,
            scanned_at=datetime(2026, 6, 1, 11, 0),
            returned_at=datetime(2026, 6, 2, 12, 0),
        ),
    ]

    assert [
        row.rtu_number
        for row in historical_returns._qualifying_issued_rtu_rows(rows)  # noqa: SLF001
    ] == ["RTU-1", "RTU-2"]


def test_rtu_scanned_only_after_return_does_not_confirm_issue() -> None:
    rows = [
        historical_returns.IssuedRtuMovement(
            rtu_number="RTU-1",
            sale_amount=Decimal("500"),
            issued=True,
            scanned_at=datetime(2026, 6, 2, 12, 0),
            returned_at=datetime(2026, 6, 1, 10, 0),
        )
    ]

    assert historical_returns._qualifying_issued_rtu_rows(rows) == []  # noqa: SLF001


def _historical_execution_snapshot(
    *,
    historical: bool = True,
) -> historical_returns.execution_reconciliation.ExecutionEvidenceSnapshot:
    return historical_returns.execution_reconciliation.ExecutionEvidenceSnapshot(
        site_order_number="240000",
        bitrix_deal_id=42,
        current_stage="EXECUTING",
        delivery_class="pickup",
        latest_assembled_at=datetime(2026, 6, 1, 10, 0),
        historical=historical,
    )


@pytest.mark.parametrize(
    (
        "onec_target",
        "onec_reason",
        "chat_event",
        "chat_confidence",
        "expected_target",
        "expected_reason",
    ),
    [
        (
            "WON",
            "pickup_printed_and_scanned",
            historical_returns.fulfillment.EVENT_PICKUP_RECEIVED,
            "strong",
            "WON",
            "onec_issued_and_later_pickup_received",
        ),
        (
            "FINAL_INVOICE",
            "assembled_without_return",
            historical_returns.fulfillment.EVENT_PICKUP_RECEIVED,
            "strong",
            "WON",
            "onec_assembled_and_later_pickup_received",
        ),
        (
            "LOSE",
            "full_unpaid_return",
            historical_returns.fulfillment.EVENT_PICKUP_DISMANTLING,
            "medium",
            "LOSE",
            "onec_full_unpaid_return_and_later_nonreceipt",
        ),
        (
            "LOSE",
            "full_unpaid_return",
            historical_returns.fulfillment.EVENT_PICKUP_UNCLAIMED,
            "medium",
            "LOSE",
            "onec_full_unpaid_return_and_later_nonreceipt",
        ),
        (
            "WON",
            "pickup_printed_and_scanned",
            historical_returns.fulfillment.EVENT_PICKUP_UNCLAIMED,
            "medium",
            None,
            "pickup_nonreceipt_conflicts_with_onec:WON",
        ),
        (
            "LOSE",
            "full_unpaid_return",
            historical_returns.fulfillment.EVENT_PICKUP_RECEIVED,
            "strong",
            None,
            "pickup_received_conflicts_with_onec:LOSE",
        ),
    ],
)
def test_stale_execution_composite_allows_only_compatible_evidence(
    onec_target: str,
    onec_reason: str,
    chat_event: str,
    chat_confidence: str,
    expected_target: str | None,
    expected_reason: str,
) -> None:
    target, reason = historical_returns._classify_stale_execution_composite(  # noqa: SLF001
        snapshot=_historical_execution_snapshot(),
        decision=historical_returns.execution_reconciliation.ExecutionDecision(
            action=historical_returns.execution_reconciliation.ACTION_UPDATE_STAGE,
            reason=onec_reason,
            event_type="execution_test",
            target_stage=onec_target,
        ),
        chat_event_type=chat_event,
        chat_event_at=datetime(2026, 6, 2, 10, 0),
        chat_event_source=historical_returns.fulfillment.SOURCE_BITRIX_CHAT,
        chat_event_confidence=chat_confidence,
    )

    assert (target, reason) == (expected_target, expected_reason)


@pytest.mark.parametrize(
    ("historical", "chat_at", "source", "confidence", "expected_reason"),
    [
        (
            False,
            datetime(2026, 6, 2, 10, 0),
            "bitrix_chat",
            "strong",
            "onec_evidence_not_historical",
        ),
        (True, datetime(2026, 6, 1, 10, 0), "bitrix_chat", "strong", "chat_event_not_later"),
        (
            True,
            datetime(2026, 6, 2, 10, 0),
            "system",
            "strong",
            "latest_event_not_chat_confirmation",
        ),
        (True, datetime(2026, 6, 2, 10, 0), "bitrix_chat", "medium", "pickup_received_not_strong"),
    ],
)
def test_stale_execution_composite_is_fail_closed(
    historical: bool,
    chat_at: datetime,
    source: str,
    confidence: str,
    expected_reason: str,
) -> None:
    target, reason = historical_returns._classify_stale_execution_composite(  # noqa: SLF001
        snapshot=_historical_execution_snapshot(historical=historical),
        decision=historical_returns.execution_reconciliation.ExecutionDecision(
            action=historical_returns.execution_reconciliation.ACTION_UPDATE_STAGE,
            reason="pickup_printed_and_scanned",
            event_type="execution_pickup_issued",
            target_stage="WON",
        ),
        chat_event_type=historical_returns.fulfillment.EVENT_PICKUP_RECEIVED,
        chat_event_at=chat_at,
        chat_event_source=source,
        chat_event_confidence=confidence,
    )

    assert target is None
    assert reason == expected_reason


def test_stale_execution_requires_bounded_explicit_orders() -> None:
    with pytest.raises(SystemExit, match="requires an explicit --orders"):
        historical_returns._run_stale_execution(  # noqa: SLF001
            apply=False,
            order_numbers=None,
            recover_pending=False,
            client=object(),
        )

    with pytest.raises(SystemExit, match="capped at 20"):
        historical_returns._run_stale_execution(  # noqa: SLF001
            apply=False,
            order_numbers=[str(value) for value in range(21)],
            recover_pending=False,
            client=object(),
        )
