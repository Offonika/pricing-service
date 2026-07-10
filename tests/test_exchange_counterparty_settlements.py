from datetime import date, datetime
from decimal import Decimal

from app.services.exchange_counterparty_settlements import (
    _control_status,
    _optional_rate,
    _uses_canonical_summary_source,
    build_exchange_counterparty_settlements,
)


def test_exchange_control_status_respects_tolerance() -> None:
    assert _control_status(Decimal("99.99"), Decimal("100.00")) == "ok"
    assert _control_status(Decimal("-100.01"), Decimal("100.00")) == "warning"


def test_exchange_optional_rate_is_blank_for_zero_denominator() -> None:
    assert _optional_rate(Decimal("100.00"), Decimal("0.00")) is None
    assert _optional_rate(Decimal("73988330.01"), Decimal("5845070")) == "12.658245"


def test_exchange_defect_counterparty_uses_canonical_summary_source() -> None:
    assert _uses_canonical_summary_source(" РБ005290 ")
    assert not _uses_canonical_summary_source("РБ002085")


def test_exchange_defect_counterparty_balance_uses_canonical_registers() -> None:
    class FakeRows:
        def __init__(self, rows):
            self.rows = rows

        def mappings(self):
            return self

        def first(self):
            return self.rows[0] if self.rows else None

    class FakeConnection:
        def __init__(self):
            self.statements = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement, params):
            sql = str(statement)
            self.statements.append(sql)
            assert params["counterparty_code"] == "РБ005290"
            if "SELECT TOP 1" in sql:
                return FakeRows(
                    [
                        {
                            "counterparty_ref": "0xdefect",
                            "counterparty_code": "РБ005290",
                            "counterparty_name": "Обмен брака",
                        }
                    ]
                )
            return FakeRows(
                [
                    {
                        "opening_period": date(2026, 7, 1),
                        "opening_balance_rub": Decimal("0.00"),
                        "movement_amount_rub": Decimal("0.00"),
                        "movement_count": 78,
                        "last_movement_at": datetime(2026, 7, 8, 18, 38, 5),
                    }
                ]
            )

    class FakeEngine:
        def __init__(self):
            self.connection = FakeConnection()

        def connect(self):
            return self.connection

    engine = FakeEngine()

    result = build_exchange_counterparty_settlements(
        engine,
        counterparty_code=" РБ005290 ",
        period_start=date(2026, 7, 1),
        as_of=datetime(2026, 7, 9, 9, 42, 30),
    )

    assert result["source"] == "1c_mutual_settlements_canonical_summary"
    assert result["counterparty_code"] == "РБ005290"
    assert result["rub_control"]["closing_balance_rub"] == "0.00"
    assert result["movement_count"] == 78

    executed_sql = "\n".join(engine.connection.statements)
    assert "_AccumRgT7009" in executed_sql
    assert "_AccumRg7002" in executed_sql
    assert "_AccumRgT7622" not in executed_sql
    assert "_AccumRg7614" not in executed_sql
