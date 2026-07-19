from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.domains.customer_price_types import (
    ContractFact,
    CustomerPriceTypeRulesEngine,
    load_price_type_ruleset,
)
from app.infrastructure import customer_price_type_sources as source_module
from app.infrastructure.customer_price_type_sources import (
    CustomerPriceTypeBulkSource,
    CustomerPriceTypeSourceEnrichments,
)
from app.models import Base


def _ref(value: int) -> str:
    return f"0x{value:032x}"


def test_bulk_source_uses_all_contracts_direct_sales_and_ledger_reconciliation(
    tmp_path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'sources.db'}")
    Base.metadata.create_all(engine)
    onec_engine = create_engine("sqlite://")
    ref = _ref(1)
    try:
        with Session(engine) as session:
            source = CustomerPriceTypeBulkSource(
                onec_engine=onec_engine,
                application_session=session,
                buyers_root_group_ref=_ref(900),
                contract_kind_ref=_ref(901),
            )
            monkeypatch.setattr(
                source,
                "_contracts",
                lambda: {
                    ref: {
                        "counterparty_code": "РБ000001",
                        "counterparty_name": "Клиент",
                        "department_ref": _ref(700),
                        "department_name": "Подразделение",
                        "contracts": [ContractFact(_ref(100), "Основной", "3.Серебряный")],
                    }
                },
            )
            monkeypatch.setattr(
                source,
                "_direct_monthly",
                lambda *_: (
                    {
                        ref: {
                            "2026-04": Decimal("100"),
                            "2026-05": Decimal("100"),
                            "2026-06": Decimal("100"),
                        }
                    },
                    {ref: date(2024, 1, 15)},
                ),
            )
            monkeypatch.setattr(
                source,
                "_ledger_monthly",
                lambda *_: (
                    {
                        ref: {
                            "2026-04": Decimal("50"),
                            "2026-05": Decimal("50"),
                            "2026-06": Decimal("50"),
                        }
                    },
                    {ref: (_ref(800), "Актуальный менеджер")},
                ),
            )
            monkeypatch.setattr(source, "_duplicate_refs", lambda: {ref})

            facts = source.collect(snapshot_month=date(2026, 6, 1))

        assert len(facts) == 1
        fact = facts[0]
        assert fact.monthly_sales["2026-06"] == Decimal("100")
        assert fact.direct_onec_total_3m == Decimal("300")
        assert fact.ledger_total_3m == Decimal("150")
        assert fact.department_name == "Подразделение"
        assert fact.owner_name == "Актуальный менеджер"
        assert fact.first_activity_date == date(2024, 1, 15)
        assert fact.history_coverage_months == 12
        assert fact.duplicate_flag is True

        ruleset = load_price_type_ruleset("config/price_types/ruleset.yaml")
        decision = CustomerPriceTypeRulesEngine(ruleset).evaluate(fact)
        assert decision.reasons == ("duplicate_counterparty",)
    finally:
        engine.dispose()
        onec_engine.dispose()


def test_contract_bulk_sql_has_no_price_type_prefix_filter() -> None:
    sql = str(source_module._BUYERS_CONTRACTS_SQL)

    assert "LIKE :prefix" not in sql
    assert "LEFT JOIN _Reference87" in sql
    assert "price_type_marked" in sql
    assert "buyers_group.department_ref" in sql


def test_bulk_source_uses_proven_history_and_bulk_enrichments(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'enrichments.db'}")
    Base.metadata.create_all(engine)
    onec_engine = create_engine("sqlite://")
    ref = _ref(2)
    calls: list[tuple[date, tuple[str, ...]]] = []

    def load_enrichments(snapshot_month, refs):
        calls.append((snapshot_month, tuple(refs)))
        return CustomerPriceTypeSourceEnrichments(
            economics={ref: {"status": "ready", "gross_profit_90": "100.00"}},
            payments={ref: {"payment_form_primary": "bank"}},
            return_signals={ref: {"return_rate_pct": "5.00"}},
        )

    try:
        with Session(engine) as session:
            source = CustomerPriceTypeBulkSource(
                onec_engine=onec_engine,
                application_session=session,
                buyers_root_group_ref=_ref(900),
                contract_kind_ref=_ref(901),
                enrichment_loader=load_enrichments,
            )
            monkeypatch.setattr(
                source,
                "_contracts",
                lambda: {
                    ref: {
                        "counterparty_code": "РБ000002",
                        "counterparty_name": "Key Account клиент",
                        "department_ref": _ref(700),
                        "department_name": "Подразделение",
                        "contracts": [ContractFact(_ref(102), "Основной", "Key Account")],
                    }
                },
            )
            monkeypatch.setattr(
                source,
                "_direct_monthly",
                lambda *_: ({ref: {"2026-06": Decimal("-10")}}, {ref: date(2026, 6, 15)}),
            )
            monkeypatch.setattr(
                source,
                "_ledger_monthly",
                lambda *_: ({ref: {"2026-06": Decimal("-10")}}, {ref: (_ref(800), "Менеджер")}),
            )
            monkeypatch.setattr(source, "_duplicate_refs", lambda: set())

            facts = source.collect(snapshot_month=date(2026, 6, 1))

        fact = facts[0]
        assert calls == [(date(2026, 6, 1), (ref,))]
        assert fact.history_coverage_months == 0
        assert fact.monthly_sales["2026-06"] == Decimal("-10")
        assert fact.key_account_flag is True
        assert fact.economics_status == "ready"
        assert fact.payments["payment_form_primary"] == "bank"
        assert fact.returns["return_rate_pct"] == "5.00"
    finally:
        engine.dispose()
        onec_engine.dispose()
