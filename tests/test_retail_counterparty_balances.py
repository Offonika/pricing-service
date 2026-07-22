from datetime import date, datetime
from decimal import Decimal

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.services.retail_counterparty_balances import (
    build_retail_counterparty_zero_balances,
    build_unavailable_retail_counterparty_zero_balances,
    normalize_counterparty_codes,
)


class FakeRows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.statement = ""
        self.params = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, statement, params):
        self.statement = str(statement)
        self.params = params
        return FakeRows(self.rows)


class FakeEngine:
    def __init__(self, rows):
        self.connection = FakeConnection(rows)
        self.disposed = False

    def connect(self):
        return self.connection

    def dispose(self):
        self.disposed = True


def test_normalize_counterparty_codes_preserves_order_and_deduplicates() -> None:
    assert normalize_counterparty_codes([" РБ1 ", "РБ2", "РБ1", ""]) == ["РБ1", "РБ2"]


def test_retail_zero_balance_control_uses_canonical_registers_and_exact_zero() -> None:
    engine = FakeEngine(
        [
            {
                "counterparty_ref": "0x1",
                "counterparty_code": "РБ1",
                "counterparty_name": "Розница 1",
                "current_balance_rub": Decimal("0.00"),
            },
            {
                "counterparty_ref": "0x2",
                "counterparty_code": "РБ2",
                "counterparty_name": "Розница 2",
                "current_balance_rub": Decimal("-0.01"),
            },
        ]
    )

    result = build_retail_counterparty_zero_balances(
        engine,
        counterparty_codes=["РБ1", "РБ2", "РБ1", "РБ3"],
        period_start=date(2026, 7, 1),
        as_of=datetime(2026, 7, 14, 9, 10),
    )

    assert result["status"] == "partial"
    assert result["requested_count"] == 3
    assert result["checked_count"] == 2
    assert result["warning_count"] == 1
    assert result["missing_count"] == 1
    assert [item["status"] for item in result["items"]] == ["ok", "warning", "missing"]
    assert result["items"][1]["current_balance_rub"] == Decimal("-0.01")
    assert engine.connection.params["counterparty_codes"] == ["РБ1", "РБ2", "РБ3"]
    assert "_AccumRgT7009" in engine.connection.statement
    assert "_AccumRg7002" in engine.connection.statement
    assert "_AccumRgT7622" not in engine.connection.statement
    assert "_AccumRg7614" not in engine.connection.statement


def test_unavailable_retail_zero_balance_control_marks_every_requested_code() -> None:
    result = build_unavailable_retail_counterparty_zero_balances(
        ["РБ1", "РБ2", "РБ1"],
        as_of=datetime(2026, 7, 14, 9, 10),
    )

    assert result["status"] == "unavailable"
    assert result["requested_count"] == 2
    assert result["checked_count"] == 0
    assert result["unavailable_count"] == 2
    assert [item["status"] for item in result["items"]] == ["unavailable", "unavailable"]


def test_retail_zero_balance_control_rejects_reverse_period() -> None:
    engine = FakeEngine([])

    try:
        build_retail_counterparty_zero_balances(
            engine,
            counterparty_codes=["РБ1"],
            period_start=date(2026, 7, 15),
            as_of=datetime(2026, 7, 14, 9, 10),
        )
    except ValueError as error:
        assert str(error) == "period_start must not be later than as_of"
    else:
        raise AssertionError("reverse period must be rejected")

    assert engine.connection.params == {}


def test_retail_zero_balance_endpoint_accepts_repeated_codes(monkeypatch) -> None:
    engine = FakeEngine([])
    captured = {}

    def fake_build(_engine, *, counterparty_codes, period_start=None, as_of=None):
        captured["codes"] = counterparty_codes
        return {
            "status": "ready",
            "source": "1c_mutual_settlements_canonical_summary",
            "generated_at_msk": "2026-07-14T09:10:00+03:00",
            "expected_balance_rub": "0.00",
            "requested_count": 2,
            "checked_count": 2,
            "warning_count": 1,
            "missing_count": 0,
            "unavailable_count": 0,
            "items": [
                {
                    "counterparty_code": "РБ1",
                    "counterparty_name": "Розница 1",
                    "counterparty_ref": "0x1",
                    "current_balance_rub": "0.00",
                    "status": "ok",
                },
                {
                    "counterparty_code": "РБ2",
                    "counterparty_name": "Розница 2",
                    "counterparty_ref": "0x2",
                    "current_balance_rub": "1.00",
                    "status": "warning",
                },
            ],
        }

    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
    monkeypatch.setattr("app.api.management._build_onec_engine", lambda: engine)
    monkeypatch.setattr(
        "app.api.management.build_retail_counterparty_zero_balances",
        fake_build,
    )
    get_settings.cache_clear()
    client = TestClient(app)

    unauthorized = client.get(
        "/api/management/retail-counterparty-zero-balances",
        params=[("counterparty_code", "РБ1")],
    )
    assert unauthorized.status_code == 401

    response = client.get(
        "/api/management/retail-counterparty-zero-balances",
        params=[("counterparty_code", "РБ1"), ("counterparty_code", "РБ2")],
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 200
    assert captured["codes"] == ["РБ1", "РБ2"]
    assert response.json()["warning_count"] == 1
    assert engine.disposed is True
    get_settings.cache_clear()


def test_retail_zero_balance_endpoint_rejects_reverse_period_before_onec(
    monkeypatch,
) -> None:
    def unexpected_engine():
        raise AssertionError("1C engine must not be opened for an invalid period")

    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
    monkeypatch.setattr("app.api.management._build_onec_engine", unexpected_engine)
    get_settings.cache_clear()
    client = TestClient(app)

    response = client.get(
        "/api/management/retail-counterparty-zero-balances",
        params={
            "counterparty_code": "РБ1",
            "period_start": "2026-07-15",
            "as_of": "2026-07-14T09:10:00+03:00",
        },
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "period_start must not be later than as_of"
    get_settings.cache_clear()


def test_retail_zero_balance_endpoint_returns_unavailable_items(monkeypatch) -> None:
    def unavailable_engine():
        raise HTTPException(status_code=503, detail="1C source is unavailable")

    monkeypatch.setenv("MANAGEMENT_INTERNAL_API_TOKEN", "secret-token")
    monkeypatch.setattr("app.api.management._build_onec_engine", unavailable_engine)
    get_settings.cache_clear()
    client = TestClient(app)

    response = client.get(
        "/api/management/retail-counterparty-zero-balances",
        params=[("counterparty_code", "РБ1"), ("counterparty_code", "РБ2")],
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["unavailable_count"] == 2
    get_settings.cache_clear()
