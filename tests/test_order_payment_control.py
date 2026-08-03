from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import OperationalError

from app.api import order_payment_control as api
from app.api.dependencies import require_order_payment_control_internal_token
from app.core.config import get_settings
from app.schemas.order_payment_control import OrderPaymentCheckRequest
from app.services import order_payment_control as service


class _Mappings:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self.rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, params):
        assert "_Document132" in str(statement)
        assert "NOLOCK" not in str(statement)
        assert params == {"site_order_number": "225550"}
        return _Mappings(self.rows)


class _Engine:
    def __init__(self, rows):
        self.rows = rows

    def connect(self):
        return _Connection(self.rows)


def _row(*, amount="5461.95", marked=b"\x00", posted=b"\x01", revision=b"rev00001"):
    return {
        "document_number": "РБГУ0047543   ",
        "document_amount": Decimal(amount) if amount is not None else None,
        "marked": marked,
        "posted": posted,
        "revision": revision,
    }


def _check(rows, *, site="5461.95", payment="5461.95"):
    return service.check_order_payment(
        _Engine(rows),
        site_order_number="225550",
        site_amount=Decimal(site),
        payment_amount=Decimal(payment),
    )


def test_payment_control_allows_only_three_matching_amounts() -> None:
    decision = _check([_row()])

    assert decision.allowed is True
    assert decision.reason == "amount_match"
    assert decision.onec_amount == Decimal("5461.95")
    assert decision.onec_document_number == "РБГУ0047543"
    assert decision.onec_revision == b"rev00001".hex()


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([], "onec_order_not_found"),
        ([_row(marked=b"\x01")], "onec_order_deleted"),
        ([_row(), _row(revision=b"rev00002")], "onec_order_ambiguous"),
        ([_row(posted=b"\x00")], "onec_order_unposted"),
        ([_row(amount=None)], "onec_amount_invalid"),
        ([_row(amount="3325.95")], "onec_amount_mismatch"),
    ],
)
def test_payment_control_denies_unsafe_onec_states(rows, reason) -> None:
    decision = _check(rows)

    assert decision.allowed is False
    assert decision.reason == reason


def test_payment_control_denies_local_site_payment_mismatch_without_onec_query() -> None:
    class _UnexpectedEngine:
        def connect(self):
            raise AssertionError("1C must not be queried for an already inconsistent payment")

    decision = service.check_order_payment(
        _UnexpectedEngine(),
        site_order_number="225550",
        site_amount=Decimal("5461.95"),
        payment_amount=Decimal("3325.95"),
    )

    assert decision.allowed is False
    assert decision.reason == "site_payment_mismatch"


def _configure(monkeypatch, token="payment-token") -> str:
    monkeypatch.setenv("ORDER_PAYMENT_CONTROL_INTERNAL_API_TOKEN", token)
    monkeypatch.delenv("ORDER_FULFILLMENT_INTERNAL_API_TOKEN", raising=False)
    monkeypatch.delenv("MANAGEMENT_INTERNAL_API_TOKEN", raising=False)
    get_settings.cache_clear()
    return token


def _payload() -> OrderPaymentCheckRequest:
    return OrderPaymentCheckRequest.model_validate(
        {
            "site_order_number": "225550",
            "site_amount": "5461.95",
            "payment_amount": "5461.95",
            "stage": "cloudpayments_check",
            "payment_id": "cp-1414",
        }
    )


def test_payment_control_endpoint_requires_dedicated_token(monkeypatch) -> None:
    _configure(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        require_order_payment_control_internal_token(None)

    assert exc_info.value.status_code == 401


def test_payment_control_endpoint_returns_structured_decision(monkeypatch) -> None:
    token = _configure(monkeypatch)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    assert require_order_payment_control_internal_token(credentials) == token
    monkeypatch.setattr(api, "get_onec_engine", lambda: _Engine([_row()]))

    response = api.check_order_payment(_payload())

    assert response.allowed is True
    assert response.reason == "amount_match"
    assert response.onec_amount == Decimal("5461.95")


def test_payment_control_endpoint_fails_closed_when_onec_is_unavailable(monkeypatch) -> None:
    _configure(monkeypatch)

    def fail():
        raise OperationalError("SELECT", {}, RuntimeError("offline"))

    monkeypatch.setattr(api, "get_onec_engine", fail)

    with pytest.raises(HTTPException) as exc_info:
        api.check_order_payment(_payload())

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "onec_unavailable"
