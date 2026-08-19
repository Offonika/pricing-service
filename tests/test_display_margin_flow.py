from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.display_margin_flow import (
    MarginFlowPolicy,
    build_margin_flow_facts,
    calculate_point_rate,
    calculate_profitability_pct,
    fetch_point_safe_free_stock,
    qualifies_for_margin_flow,
)


class _Rows(list[dict[str, object]]):
    def mappings(self) -> _Rows:
        return self


class _ReadOnlyConnection:
    def __init__(self) -> None:
        self.accumulator_statements: list[str] = []

    def __enter__(self) -> _ReadOnlyConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: object, params: dict[str, object]) -> _Rows:
        sql = str(statement)
        if "FROM dbo._Reference62" in sql:
            return _Rows([{"product_ref": b"P" * 16, "product_code": "RB1"}])
        if "FROM dbo._Reference80" in sql:
            return _Rows([{"warehouse_ref": b"W" * 16, "warehouse_code": "STORE1"}])
        if "FROM dbo._Reference48" in sql:
            return _Rows([{"quality_ref": b"Q" * 16}])
        self.accumulator_statements.append(sql)
        assert params["product_refs"] == [b"P" * 16]
        assert params["warehouse_refs"] == (b"W" * 16,)
        if "FROM dbo._AccumRgT7745" in sql:
            assert params["quality_refs"] == (b"Q" * 16,)
            return _Rows(
                [
                    {
                        "product_ref": b"P" * 16,
                        "warehouse_ref": b"W" * 16,
                        "stock_qty": Decimal("5"),
                    }
                ]
            )
        if "FROM dbo._AccumRgT7662" in sql:
            return _Rows(
                [
                    {
                        "product_ref": b"P" * 16,
                        "warehouse_ref": b"W" * 16,
                        "reserved_qty": Decimal("2"),
                    }
                ]
            )
        raise AssertionError(sql)


class _ReadOnlyEngine:
    def __init__(self) -> None:
        self.connection = _ReadOnlyConnection()

    def connect(self) -> _ReadOnlyConnection:
        return self.connection


def test_profitability_uses_party_cost_against_realized_revenue() -> None:
    assert calculate_profitability_pct(
        gross_sale_qty=Decimal("10"),
        net_revenue_rub=Decimal("1000"),
        party_cost_per_unit=Decimal("60"),
    ) == Decimal("40")
    assert (
        calculate_profitability_pct(
            gross_sale_qty=Decimal("10"),
            net_revenue_rub=Decimal("1000"),
            party_cost_per_unit=None,
        )
        is None
    )


def test_point_rate_is_calculated_per_store_before_network_sum() -> None:
    facts = build_margin_flow_facts(
        codes=["RB1"],
        warehouse_codes=["STORE1", "STORE2", "SDEK"],
        point_sales={
            "RB1": {
                "STORE1": {30: Decimal("3"), 90: Decimal("9"), 180: Decimal("18")},
                "STORE2": {30: Decimal("0"), 90: Decimal("0"), 180: Decimal("0")},
                "SDEK": {30: Decimal("3"), 90: Decimal("9"), 180: Decimal("18")},
            }
        },
        point_availability={
            "RB1": {
                "STORE1": {30: Decimal("30"), 90: Decimal("90"), 180: Decimal("180")},
                "STORE2": {30: Decimal("0"), 90: Decimal("0"), 180: Decimal("0")},
                "SDEK": {30: Decimal("30"), 90: Decimal("90"), 180: Decimal("180")},
            }
        },
        party_costs={"RB1": Decimal("60")},
        rolling_revenue={
            "RB1": {"gross_sale_qty": Decimal("36"), "net_revenue_rub": Decimal("3600")}
        },
    )

    assert facts["RB1"]["point_rates"]["STORE1"] == Decimal("0.100000000000")
    assert facts["RB1"]["point_rates"]["STORE2"] == Decimal("0E-12")
    assert facts["RB1"]["point_rates"]["SDEK"] == Decimal("0.100000000000")
    assert facts["RB1"]["point_rate_sum"] == Decimal("0.200000000000")


def test_point_rate_falls_back_to_calendar_when_history_is_too_short() -> None:
    rate = calculate_point_rate(
        sales={30: Decimal("3"), 90: Decimal("3"), 180: Decimal("3")},
        availability_days={30: Decimal("3"), 90: Decimal("3"), 180: Decimal("3")},
    )
    # Календарные скорости 0,1 / 0,033 / 0,0167 показывают ускорение,
    # поэтому после безопасного отказа от поправки наличия берётся максимум.
    assert rate == Decimal("0.100000000000")


def test_margin_flow_scope_uses_stable_status_and_strict_profitability_boundary() -> None:
    policy = MarginFlowPolicy(enabled=True)
    assert qualifies_for_margin_flow(
        status_code="sale",
        point_rate_sum=Decimal("0.1"),
        profitability_pct=Decimal("31.0001"),
        policy=policy,
    )
    assert qualifies_for_margin_flow(
        status_code="sale",
        point_rate_sum=Decimal("0.25"),
        profitability_pct=Decimal("40"),
        policy=policy,
    )
    assert not qualifies_for_margin_flow(
        status_code="working",
        point_rate_sum=Decimal("0.2"),
        profitability_pct=Decimal("40"),
        policy=policy,
    )
    assert not qualifies_for_margin_flow(
        status_code="sale",
        point_rate_sum=Decimal("0.2"),
        profitability_pct=Decimal("31"),
        policy=policy,
    )


def test_as_of_type_remains_a_date_for_query_contract() -> None:
    assert date.fromisoformat("2026-08-17").isoformat() == "2026-08-17"


def test_point_free_stock_filters_accumulators_by_binary_references() -> None:
    engine = _ReadOnlyEngine()

    result = fetch_point_safe_free_stock(
        engine,  # type: ignore[arg-type]
        codes=["RB1"],
        warehouse_codes=["STORE1"],
        quality_names=["Новый"],
    )

    assert result["RB1"]["point_safe_free_stock_qty"] == Decimal("3")
    assert result["RB1"]["point_safe_free_stock_by_warehouse"] == {"STORE1": Decimal("3")}
    assert len(engine.connection.accumulator_statements) == 2
    assert all("JOIN dbo._Reference" not in sql for sql in engine.connection.accumulator_statements)
