from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.services.procurement_order_metrics import (
    _contract_rows_to_terms,
    _price_rows_to_metrics,
    build_line_metric_payload,
    defect_confidence,
    price_change_pct,
    profitability_pct,
    rate_pct,
)


def test_supplier_terms_require_exact_contract_owner_and_stay_missing() -> None:
    terms = _contract_rows_to_terms(
        [
            {
                "contract_ref": "0xcontract",
                "supplier_ref": "0xsupplier",
                "contract_code": "CON-1",
                "contract_name": "Договор поставщика",
            }
        ],
        items=[
            {"supplier_ref": "0xsupplier", "contract_ref": "0xcontract"},
            {"supplier_ref": "0xother", "contract_ref": "0xcontract"},
        ],
    )

    exact = terms[("0xsupplier", "0xcontract")]
    assert exact["contract_source_status"] == "exact_contract_verified"
    assert exact["terms_status"] == "missing"
    assert exact["credit_days"] is None
    assert terms[("0xother", "0xcontract")]["contract_source_status"] == "supplier_mismatch"


def test_price_currency_mismatch_has_explicit_evidence() -> None:
    payload = build_line_metric_payload(
        product_metrics=None,
        price_metrics={
            "status": "currency_mismatch",
            "expected_currency": "RUB",
            "available_currencies": ["CNY", "USD"],
        },
        as_of=date(2026, 8, 1),
    )

    assert payload["price_change_status"] == "currency_mismatch"
    assert payload["price_history_expected_currency"] == "RUB"
    assert payload["price_history_available_currencies"] == ["CNY", "USD"]


def test_price_history_uses_distinct_orders_and_keeps_currencies_separate() -> None:
    rows = [
        {
            "code": "SKU-1",
            "supplier_ref": "0xsupplier",
            "currency_ref": "0xrub",
            "currency_code": "643",
            "order_ref": "0xold",
            "order_number": "1",
            "line_number": 1,
            "price": 100,
            "price_at": datetime(2026, 7, 1),
        },
        {
            "code": "SKU-1",
            "supplier_ref": "0xsupplier",
            "currency_ref": "0xrub",
            "currency_code": "643",
            "order_ref": "0xold",
            "order_number": "1",
            "line_number": 2,
            "price": 105,
            "price_at": datetime(2026, 7, 1),
        },
        {
            "code": "SKU-1",
            "supplier_ref": "0xsupplier",
            "currency_ref": "0xrub",
            "currency_code": "643",
            "order_ref": "0xnew",
            "order_number": "2",
            "line_number": 1,
            "price": 110,
            "price_at": datetime(2026, 7, 10),
        },
        {
            "code": "SKU-1",
            "supplier_ref": "0xsupplier",
            "currency_ref": "0xusd",
            "currency_code": "840",
            "order_ref": "0xusd-order",
            "order_number": "3",
            "line_number": 1,
            "price": 99,
            "price_at": datetime(2026, 7, 15),
        },
    ]

    metrics = _price_rows_to_metrics(rows)

    assert metrics[("SKU-1", "0xsupplier", "RUB")]["latest_price"] == 110
    assert metrics[("SKU-1", "0xsupplier", "RUB")]["previous_price"] == 105
    assert metrics[("SKU-1", "0xsupplier", "RUB")]["history_count"] == 2
    assert metrics[("SKU-1", "0xsupplier", "USD")]["previous_price"] is None


def test_procurement_metric_formulas_and_zero_denominators() -> None:
    assert profitability_pct("301611.00", "166912.17") == Decimal("44.66")
    assert profitability_pct("0", "166912.17") is None
    assert profitability_pct("150", "0") == Decimal("100.00")
    assert price_change_pct("110", "100") == Decimal("10.00")
    assert price_change_pct("110", "0") is None
    assert rate_pct("12", "100") == Decimal("12.00")
    assert rate_pct("12", "0") is None
    assert defect_confidence(29) == "weak"
    assert defect_confidence(30) == "warning"
    assert defect_confidence(100) == "reliable"


def test_product_defect_does_not_become_supplier_defect_without_exact_trace() -> None:
    payload = build_line_metric_payload(
        product_metrics={
            "sales_qty": 100,
            "sales_amount": 2000,
            "return_amount": 100,
            "cost_amount": 1000,
            "defect_return_qty": 12,
        },
        price_metrics={
            "latest_price": 110,
            "previous_price": 100,
            "history_count": 2,
            "currency_ref": "currency-rub",
        },
        as_of=date(2026, 8, 1),
    )

    assert payload["profitability_pct"] == "47.37"
    assert payload["profitability_calculation_basis"] == "net_sales_amount"
    assert payload["product_defect_pct"] == "12.00"
    assert payload["product_defect_confidence"] == "reliable"
    assert payload["supplier_defect_attribution"] == "unconfirmed"
    assert "supplier_defect_pct" not in payload
    assert payload["price_change_pct"] == "10.00"


def test_zero_revenue_has_empty_profitability_and_explicit_reason() -> None:
    payload = build_line_metric_payload(
        product_metrics={
            "sales_qty": 0,
            "sales_amount": 0,
            "return_amount": 0,
            "cost_amount": 100,
        },
        price_metrics=None,
        as_of=date(2026, 8, 1),
    )

    assert "profitability_pct" not in payload
    assert payload["profitability_status"] == "revenue_missing"
    assert payload["profitability_calculation_basis"] == "net_sales_amount"


def test_exact_supplier_defect_uses_its_own_basis() -> None:
    payload = build_line_metric_payload(
        product_metrics={"sales_qty": 200, "defect_return_qty": 20},
        price_metrics=None,
        supplier_defect_metrics={
            "attribution": "supplier_exact",
            "history_units": 100,
            "defect_units": 11,
            "source": "onec_supplier_lot_trace",
        },
        as_of=date(2026, 8, 1),
    )

    assert payload["product_defect_pct"] == "10.00"
    assert payload["supplier_defect_pct"] == "11.00"
    assert payload["supplier_defect_confidence"] == "reliable"
    assert payload["supplier_defect_attribution"] == "supplier_exact"
