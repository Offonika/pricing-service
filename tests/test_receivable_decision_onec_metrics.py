from __future__ import annotations

from decimal import Decimal

from app.services.receivable_decision_onec_metrics import (
    ONEC_COUNTERPARTY_PAYMENT_FORM_SQL,
    ONEC_COUNTERPARTY_PROFITABILITY_SQL,
    build_payment_form_metrics_from_rows,
    build_profitability_metrics_from_rows,
)


def test_onec_counterparty_profitability_sql_uses_cost_and_return_reason_sources() -> None:
    assert "_AccumRg7550" in ONEC_COUNTERPARTY_PROFITABILITY_SQL
    assert "_AccumRg7580" in ONEC_COUNTERPARTY_PROFITABILITY_SQL
    assert "_Fld7588" in ONEC_COUNTERPARTY_PROFITABILITY_SQL
    assert "_Document109_VT1698" in ONEC_COUNTERPARTY_PROFITABILITY_SQL
    assert "_Fld8914_S" in ONEC_COUNTERPARTY_PROFITABILITY_SQL
    assert "_Fld8914_RRRef" in ONEC_COUNTERPARTY_PROFITABILITY_SQL
    assert "_Reference8913" in ONEC_COUNTERPARTY_PROFITABILITY_SQL
    assert "LIKE N'%брак%'" in ONEC_COUNTERPARTY_PROFITABILITY_SQL
    assert "LIKE N'%качеств%'" in ONEC_COUNTERPARTY_PROFITABILITY_SQL


def test_onec_counterparty_payment_form_sql_uses_cash_and_bank_sources() -> None:
    assert "_Document196" in ONEC_COUNTERPARTY_PAYMENT_FORM_SQL
    assert "_AccumRg7614" in ONEC_COUNTERPARTY_PAYMENT_FORM_SQL
    assert "0x000000BA" in ONEC_COUNTERPARTY_PAYMENT_FORM_SQL
    assert "0x000000A9" in ONEC_COUNTERPARTY_PAYMENT_FORM_SQL
    assert "cash_amount_90" in ONEC_COUNTERPARTY_PAYMENT_FORM_SQL
    assert "bank_amount_90" in ONEC_COUNTERPARTY_PAYMENT_FORM_SQL


def test_build_profitability_metrics_from_rows_calculates_margin_and_profitability() -> None:
    metrics = build_profitability_metrics_from_rows(
        [
            {
                "counterparty_ref": "0xabc",
                "revenue_30": Decimal("1000.00"),
                "revenue_60": Decimal("2000.00"),
                "revenue_90": Decimal("3000.00"),
                "cost_of_sales_30": Decimal("600.00"),
                "cost_of_sales_60": Decimal("1200.00"),
                "cost_of_sales_90": Decimal("2100.00"),
                "defect_return_amount_30": Decimal("50.00"),
                "defect_return_amount_60": Decimal("70.00"),
                "defect_return_amount_90": Decimal("90.00"),
            }
        ]
    )

    row = metrics["0XABC"]

    assert row.source_status == "ready"
    assert row.gross_profit_30 == Decimal("400.00")
    assert row.gross_profit_60 == Decimal("800.00")
    assert row.gross_profit_90 == Decimal("900.00")
    assert row.gross_margin_pct_90 == Decimal("30.00")
    assert row.profitability_pct_90 == Decimal("42.86")
    assert row.defect_return_amount_90 == Decimal("90.00")


def test_build_payment_form_metrics_from_rows_calculates_primary_form() -> None:
    metrics = build_payment_form_metrics_from_rows(
        [
            {
                "counterparty_ref": "0xabc",
                "cash_amount_90": Decimal("300.00"),
                "bank_amount_90": Decimal("700.00"),
            },
            {
                "counterparty_ref": "0xdef",
                "cash_amount_90": Decimal("800.00"),
                "bank_amount_90": Decimal("200.00"),
            },
        ]
    )

    bank_row = metrics["0XABC"]
    cash_row = metrics["0XDEF"]

    assert bank_row.payment_form_primary == "bank"
    assert bank_row.cash_share_90 == Decimal("30.00")
    assert bank_row.bank_share_90 == Decimal("70.00")
    assert bank_row.source_status == "ready"
    assert cash_row.payment_form_primary == "cash"
