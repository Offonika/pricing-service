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
