from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from app.domains.customer_price_types import (
    ContractFact,
    CustomerPriceTypeFacts,
    CustomerPriceTypeRulesEngine,
    load_price_type_ruleset,
)
from app.services.customer_price_type_review_batches import ReviewBatchSourceRow
from tasks.run_customer_price_type_shadow import _review_preview


def _fact(code: str, contracts: tuple[ContractFact, ...]) -> CustomerPriceTypeFacts:
    return CustomerPriceTypeFacts(
        counterparty_ref=f"0x{int(code[-3:]):032x}",
        counterparty_code=code,
        counterparty_name=code,
        snapshot_month=date(2026, 6, 1),
        contracts=contracts,
        monthly_sales={
            "2026-04": Decimal("4000"),
            "2026-05": Decimal("4000"),
            "2026-06": Decimal("4000"),
        },
        source_statuses={
            "contracts": "ready",
            "sales_history": "ready",
            "ledger_reconciliation": "ready",
            "master_data": "ready",
        },
        history_coverage_months=12,
        direct_onec_total_3m=Decimal("12000"),
        ledger_total_3m=Decimal("12000"),
        economics_status="ok",
    )


def test_review_preview_distinguishes_business_conflict_and_technical_gap() -> None:
    ruleset = load_price_type_ruleset("config/price_types/ruleset.yaml")
    engine = CustomerPriceTypeRulesEngine(ruleset)
    bronze = _fact(
        "РБ000001",
        (
            ContractFact(
                "contract-1",
                "Основной",
                "2.Бронзовый",
                sale_document_count_12m=2,
                is_working=True,
            ),
        ),
    )
    conflict = _fact(
        "РБ000002",
        (
            ContractFact(
                "contract-2a",
                "Бронзовый",
                "2.Бронзовый",
                sale_document_count_12m=2,
                is_working=True,
            ),
            ContractFact(
                "contract-2b",
                "Розничный",
                "Розница",
                sale_document_count_12m=1,
                is_working=True,
            ),
        ),
    )
    rows = [
        ReviewBatchSourceRow("РБ000001", "working_bronze", "2.Бронзовый", "a.csv", 2),
        ReviewBatchSourceRow("РБ000002", "review_queue", None, "b.csv", 2),
    ]

    preview = _review_preview(
        facts=[bronze, conflict],
        decisions=[engine.evaluate(bronze), engine.evaluate(conflict)],
        rows=rows,
        required_sources=ruleset.required_sources,
    )
    assert preview["counts"] == {"working_bronze": 1, "review_queue": 1}
    assert preview["review_status_counts"] == {"ready": 1, "business_conflict": 1}
    assert preview["mismatch_count"] == 0

    incomplete_conflict = replace(
        conflict,
        source_statuses={**conflict.source_statuses, "master_data": "missing"},
    )
    incomplete_preview = _review_preview(
        facts=[bronze, incomplete_conflict],
        decisions=[engine.evaluate(bronze), engine.evaluate(incomplete_conflict)],
        rows=rows,
        required_sources=ruleset.required_sources,
    )
    assert incomplete_preview["review_status_counts"]["technical_incomplete"] == 1
    assert incomplete_preview["mismatch_count"] == 1
