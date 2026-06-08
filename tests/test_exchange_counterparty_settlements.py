from decimal import Decimal

from app.services.exchange_counterparty_settlements import _control_status, _optional_rate


def test_exchange_control_status_respects_tolerance() -> None:
    assert _control_status(Decimal("99.99"), Decimal("100.00")) == "ok"
    assert _control_status(Decimal("-100.01"), Decimal("100.00")) == "warning"


def test_exchange_optional_rate_is_blank_for_zero_denominator() -> None:
    assert _optional_rate(Decimal("100.00"), Decimal("0.00")) is None
    assert _optional_rate(Decimal("73988330.01"), Decimal("5845070")) == "12.658245"
