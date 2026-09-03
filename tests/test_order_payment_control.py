from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import OperationalError

from app.api import order_payment_control as api
from app.api.dependencies import require_order_payment_control_internal_token
from app.core.config import get_settings
from app.infrastructure.db.engines import DatabaseNotConfiguredError
from app.schemas.order_payment_control import OrderPaymentCheckRequest
from app.services import order_payment_control as service

WAREHOUSE_GUID = "11111111-2222-3333-4444-555555555555"
OTHER_WAREHOUSE_GUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
WAREHOUSE_REF = service.onec_ref_from_guid(WAREHOUSE_GUID)
OTHER_WAREHOUSE_REF = service.onec_ref_from_guid(OTHER_WAREHOUSE_GUID)
ORDER_REF = b"order-ref-000001"
PRODUCT_REF = b"p" * 16
CHARACTERISTIC_REF = b"c" * 16
SERIES_REF = b"s" * 16
UNIT_REF = b"u" * 16
ZERO_REF = bytes(16)


class _Mappings:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self.rows


class _Dialect:
    def __init__(self, name):
        self.name = name


class _Connection:
    def __init__(self, engine):
        self.engine = engine
        self.dialect = engine.dialect

    def __enter__(self):
        self.engine.active_connections += 1
        return self

    def __exit__(self, *_args):
        self.engine.active_connections -= 1
        return None

    def execution_options(self, **options):
        self.engine.isolation_options.append(options)
        return self

    def begin(self):
        return _Transaction(self.engine)

    def exec_driver_sql(self, statement):
        self.engine.driver_sql.append(statement)

    def execute(self, statement, params=None):
        sql = str(statement)
        assert "NOLOCK" not in sql
        if "_Document135_VT2569" in sql:
            assert params is None
            assert f"0x{ORDER_REF.hex()}" in sql
            return _Mappings(self.engine.closure_rows)
        if "_Document132_VT2427" in sql:
            assert params is None
            assert f"0x{ORDER_REF.hex()}" in sql
            return _Mappings(self.engine.line_rows)
        if "_AccumRgT7662" in sql:
            assert params is None
            assert "_Fld7657_RTRef = 0x00000084" in sql
            assert f"0x{ORDER_REF.hex()}" in sql
            return _Mappings(self.engine.reserve_rows)
        assert "_Document132" in sql
        assert params == {"site_order_number": "225550"}
        return _Mappings(self.engine.header_rows)


class _Transaction:
    def __init__(self, engine):
        self.engine = engine

    def __enter__(self):
        self.engine.active_transactions += 1
        return self

    def __exit__(self, *_args):
        self.engine.active_transactions -= 1
        return None


class _Engine:
    def __init__(
        self,
        rows,
        *,
        closure_rows=(),
        line_rows=None,
        reserve_rows=None,
        dialect_name="sqlite",
    ):
        self.header_rows = list(rows)
        self.closure_rows = list(closure_rows)
        self.line_rows = list(line_rows if line_rows is not None else [_line_row()])
        self.reserve_rows = list(reserve_rows if reserve_rows is not None else [_reserve_row()])
        self.connect_count = 0
        self.active_connections = 0
        self.active_transactions = 0
        self.isolation_options = []
        self.driver_sql = []
        self.dialect = _Dialect(dialect_name)

    def connect(self):
        self.connect_count += 1
        return _Connection(self)


def _row(
    *,
    amount="5461.95",
    marked=b"\x00",
    posted=b"\x01",
    revision=b"rev00001",
    warehouse_ref=WAREHOUSE_REF,
):
    return {
        "order_ref": ORDER_REF,
        "document_number": "РБГУ0047543   ",
        "document_amount": Decimal(amount) if amount is not None else None,
        "marked": marked,
        "posted": posted,
        "revision": revision,
        "warehouse_ref": warehouse_ref,
    }


def _line_row(
    *,
    line_number=1,
    product_ref=PRODUCT_REF,
    characteristic_ref=CHARACTERISTIC_REF,
    series_ref=SERIES_REF,
    placement_ref=WAREHOUSE_REF,
    line_unit_ref=UNIT_REF,
    storage_unit_ref=UNIT_REF,
    quantity="1.000",
    coefficient="1.000",
    storage_coefficient="1.000",
):
    return {
        "line_number": line_number,
        "product_ref": product_ref,
        "characteristic_ref": characteristic_ref,
        "series_ref": series_ref,
        "placement_ref": placement_ref,
        "line_unit_ref": line_unit_ref,
        "storage_unit_ref": storage_unit_ref,
        "quantity": Decimal(quantity),
        "coefficient": Decimal(coefficient),
        "storage_coefficient": Decimal(storage_coefficient),
    }


def _reserve_row(
    *,
    warehouse_ref=WAREHOUSE_REF,
    product_ref=PRODUCT_REF,
    characteristic_ref=CHARACTERISTIC_REF,
    series_ref=SERIES_REF,
    quantity="1.000",
):
    return {
        "warehouse_ref": warehouse_ref,
        "product_ref": product_ref,
        "characteristic_ref": characteristic_ref,
        "series_ref": series_ref,
        "reserve_quantity": Decimal(quantity),
    }


def _closure_row(*, reason="Отмена заказа", number="РБ000000245   "):
    return {
        "closure_number": number,
        "closure_date": None,
        "closure_reason": reason,
    }


def _check(
    rows,
    *,
    site="5461.95",
    payment="5461.95",
    closures=(),
    lines=None,
    reserves=None,
    warehouse_guid=WAREHOUSE_GUID,
    **kwargs,
):
    return service.check_order_payment(
        _Engine(
            rows,
            closure_rows=closures,
            line_rows=lines,
            reserve_rows=reserves,
        ),
        site_order_number="225550",
        site_amount=Decimal(site),
        payment_amount=Decimal(payment),
        source_warehouse_xml_id=warehouse_guid,
        **kwargs,
    )


def test_onec_guid_reference_conversion_round_trip() -> None:
    assert service.onec_guid_from_ref(service.onec_ref_from_guid(WAREHOUSE_GUID)) == (
        WAREHOUSE_GUID
    )


def test_payment_control_allows_only_amounts_warehouse_and_full_reservation() -> None:
    ready_at = datetime(2026, 9, 1, 9, 30, tzinfo=timezone.utc)
    decision = _check(
        [_row()],
        confirmed_ready_at_resolver=lambda order_number, checked_at: ready_at,
    )

    assert decision.allowed is True
    assert decision.reason == "amount_and_full_reservation_match"
    assert decision.onec_amount == Decimal("5461.95")
    assert decision.onec_document_number == "РБГУ0047543"
    assert decision.onec_revision == b"rev00001".hex()
    assert decision.source_warehouse_xml_id == WAREHOUSE_GUID
    assert decision.reservation_state == "FULL"
    assert decision.reservation_quantity_match is True
    assert decision.reservation_confirmed_at == decision.checked_at
    assert decision.confirmed_ready_at == ready_at


def test_payment_control_reads_onec_in_one_serializable_transaction() -> None:
    engine = _Engine([_row()])

    decision = service.check_order_payment(
        engine,
        site_order_number="225550",
        site_amount=Decimal("5461.95"),
        payment_amount=Decimal("5461.95"),
        source_warehouse_xml_id=WAREHOUSE_GUID,
        confirmed_ready_at_resolver=lambda *_args: (
            None
            if engine.active_connections == 0 and engine.active_transactions == 0
            else pytest.fail("CRM readiness must be read after the 1C transaction closes")
        ),
    )

    assert decision.allowed is True
    assert engine.connect_count == 1
    assert engine.isolation_options == [{"isolation_level": "SERIALIZABLE"}]


def test_payment_control_uses_sql_server_set_isolation_for_pytds() -> None:
    engine = _Engine([_row()], dialect_name="mssql")

    decision = service.check_order_payment(
        engine,
        site_order_number="225550",
        site_amount=Decimal("5461.95"),
        payment_amount=Decimal("5461.95"),
        source_warehouse_xml_id=WAREHOUSE_GUID,
    )

    assert decision.allowed is True
    assert engine.isolation_options == []
    assert engine.driver_sql == ["SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"]


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([], "onec_order_not_found"),
        ([_row(marked=b"\x01")], "onec_order_deleted"),
        ([_row(), _row(revision=b"rev00002")], "onec_order_ambiguous"),
        ([_row(amount=None)], "onec_amount_invalid"),
        ([_row(amount="3325.95")], "onec_amount_mismatch"),
        ([_row(warehouse_ref=ZERO_REF)], "onec_warehouse_missing"),
        ([_row(warehouse_ref=OTHER_WAREHOUSE_REF)], "onec_warehouse_mismatch"),
    ],
)
def test_payment_control_denies_unsafe_onec_states(rows, reason) -> None:
    decision = _check(rows)

    assert decision.allowed is False
    assert decision.reason == reason


def test_payment_control_always_denies_unposted_order() -> None:
    decision = _check([_row(posted=b"\x00")])

    assert decision.allowed is False
    assert decision.reason == "onec_order_unposted"


@pytest.mark.parametrize(
    "reason",
    ["Отмена заказа", "Клиент не пришёл в срок резерва", "Дублированный заказ", ""],
)
def test_payment_control_denies_order_closed_as_cancellation(reason) -> None:
    decision = _check([_row()], closures=[_closure_row(reason=reason)])

    assert decision.allowed is False
    assert decision.reason == "onec_order_closed"
    assert decision.onec_closure_document == "РБ000000245"


@pytest.mark.parametrize("reason", ["Исполнение заказа", "  частичное  исполнение заказа "])
def test_payment_control_allows_order_closed_as_fulfilled(reason) -> None:
    decision = _check([_row()], closures=[_closure_row(reason=reason)])

    assert decision.allowed is True
    assert decision.reason == "amount_and_full_reservation_match"


def test_payment_control_ignores_closures_when_guard_is_disabled() -> None:
    decision = _check(
        [_row()],
        closures=[_closure_row()],
        closure_blocks_payment=False,
    )

    assert decision.allowed is True


def test_payment_control_denies_local_site_payment_mismatch_without_onec_query() -> None:
    class _UnexpectedEngine:
        def connect(self):
            raise AssertionError("1C must not be queried for an inconsistent payment")

    decision = service.check_order_payment(
        _UnexpectedEngine(),
        site_order_number="225550",
        site_amount=Decimal("5461.95"),
        payment_amount=Decimal("3325.95"),
        source_warehouse_xml_id=WAREHOUSE_GUID,
    )

    assert decision.allowed is False
    assert decision.reason == "site_payment_mismatch"


@pytest.mark.parametrize(
    ("reserves", "state", "reason"),
    [
        ([], "NONE", "onec_reservation_none"),
        ([_reserve_row(quantity="0.500")], "PARTIAL", "onec_reservation_partial"),
        ([_reserve_row(quantity="-0.100")], "MISMATCH", "onec_reservation_mismatch"),
        ([_reserve_row(quantity="1.002")], "MISMATCH", "onec_reservation_mismatch"),
        (
            [_reserve_row(warehouse_ref=OTHER_WAREHOUSE_REF)],
            "MISMATCH",
            "onec_reservation_mismatch",
        ),
    ],
)
def test_payment_control_denies_non_full_reservation(reserves, state, reason) -> None:
    decision = _check([_row()], reserves=reserves)

    assert decision.allowed is False
    assert decision.reason == reason
    assert decision.reservation_state == state
    assert decision.reservation_quantity_match is False
    assert decision.reservation_confirmed_at is None


def test_payment_control_denies_line_placed_on_another_warehouse() -> None:
    decision = _check(
        [_row()],
        lines=[_line_row(placement_ref=OTHER_WAREHOUSE_REF)],
    )

    assert decision.reason == "onec_line_placement_mismatch"
    assert decision.reservation_state == "MISMATCH"


def test_payment_control_matches_duplicate_lines_by_product_characteristic_and_series() -> None:
    lines = [
        _line_row(line_number=1, quantity="0.400"),
        _line_row(line_number=2, quantity="0.600"),
    ]

    decision = _check([_row()], lines=lines, reserves=[_reserve_row(quantity="1.000")])

    assert decision.allowed is True
    assert decision.reservation_state == "FULL"


def test_payment_control_normalizes_units_like_task_3484() -> None:
    lines = [
        _line_row(quantity="2", coefficient="5", storage_coefficient="2"),
    ]

    decision = _check([_row()], lines=lines, reserves=[_reserve_row(quantity="5")])

    assert decision.allowed is True


def test_payment_control_accepts_reservation_tolerance_boundary() -> None:
    decision = _check([_row()], reserves=[_reserve_row(quantity="0.999")])

    assert decision.allowed is True


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
            "region_xml_id": "0000512213",
            "source_warehouse_xml_id": WAREHOUSE_GUID,
            "availability_snapshot_id": "availability-3520",
        }
    )


def test_payment_control_endpoint_requires_dedicated_token(monkeypatch) -> None:
    _configure(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        require_order_payment_control_internal_token(None)

    assert exc_info.value.status_code == 401


def test_payment_control_accepts_non_uuid_region_xml_identity() -> None:
    assert _payload().region_xml_id == "0000512213"


def test_payment_control_endpoint_returns_structured_decision(monkeypatch) -> None:
    token = _configure(monkeypatch)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    assert require_order_payment_control_internal_token(credentials) == token
    monkeypatch.setattr(api, "get_onec_engine", lambda: _Engine([_row()]))
    monkeypatch.setattr(api, "_confirmed_ready_at", lambda *_args: None)

    response = api.check_order_payment(_payload())

    assert response.allowed is True
    assert response.reason == "amount_and_full_reservation_match"
    assert response.onec_amount == Decimal("5461.95")
    assert response.reservation_state == "FULL"
    assert str(response.source_warehouse_xml_id) == WAREHOUSE_GUID


def test_payment_control_endpoint_fails_closed_when_onec_is_unavailable(monkeypatch) -> None:
    _configure(monkeypatch)

    def fail():
        raise OperationalError("SELECT", {}, RuntimeError("offline"))

    monkeypatch.setattr(api, "get_onec_engine", fail)

    with pytest.raises(HTTPException) as exc_info:
        api.check_order_payment(_payload())

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "onec_unavailable"


def test_payment_control_endpoint_fails_closed_on_malformed_onec_reference(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(
        api,
        "get_onec_engine",
        lambda: _Engine([_row(warehouse_ref=b"invalid")]),
    )

    with pytest.raises(HTTPException) as exc_info:
        api.check_order_payment(_payload())

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "onec_invalid_data"


def test_confirmed_ready_at_unconfigured_source_stays_nullable(monkeypatch) -> None:
    def fail():
        raise DatabaseNotConfiguredError("application database is unavailable")

    monkeypatch.setattr(api, "get_application_engine", fail)

    assert api._confirmed_ready_at("225550", datetime.now(timezone.utc)) is None
