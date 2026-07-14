from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Engine

MONEY = Decimal("0.01")
QUANTITY = Decimal("0.001")
CURRENT_TOTALS_PERIOD = datetime(3999, 11, 1)

CURRENT_INVENTORY_COST_SQL = text("""
    SELECT
        COUNT_BIG(*) AS source_row_count,
        CAST(SUM(CAST(t._Fld7462 AS decimal(28, 3))) AS decimal(28, 3)) AS quantity,
        CAST(SUM(CAST(t._Fld7463 AS decimal(28, 2))) AS decimal(28, 2)) AS amount,
        SUM(CASE WHEN t._Fld7463 < 0 THEN 1 ELSE 0 END) AS negative_cost_row_count,
        CAST(
            SUM(
                CASE
                    WHEN t._Fld7463 < 0 THEN CAST(t._Fld7463 AS decimal(28, 2))
                    ELSE 0
                END
            )
            AS decimal(28, 2)
        ) AS negative_cost_amount
    FROM dbo._AccumRgT7473 AS t WITH (NOLOCK)
    WHERE t._Period = :current_totals_period
""")

HISTORICAL_INVENTORY_COST_SQL = text("""
    WITH opening AS (
        SELECT
            COUNT_BIG(*) AS source_row_count,
            SUM(CAST(t._Fld7462 AS decimal(28, 3))) AS quantity,
            SUM(CAST(t._Fld7463 AS decimal(28, 2))) AS amount
        FROM dbo._AccumRgT7473 AS t WITH (NOLOCK)
        WHERE t._Period = :month_start
    ),
    movements AS (
        SELECT
            COUNT_BIG(*) AS source_row_count,
            SUM(
                CAST(
                    CASE WHEN r._RecordKind = 0 THEN r._Fld7462 ELSE -r._Fld7462 END
                    AS decimal(28, 3)
                )
            ) AS quantity,
            SUM(
                CAST(
                    CASE WHEN r._RecordKind = 0 THEN r._Fld7463 ELSE -r._Fld7463 END
                    AS decimal(28, 2)
                )
            ) AS amount
        FROM dbo._AccumRg7453 AS r WITH (NOLOCK)
        WHERE r._Active = 0x01
          AND r._Period >= :month_start
          AND r._Period < :date_to
    )
    SELECT
        opening.source_row_count + movements.source_row_count AS source_row_count,
        CAST(
            COALESCE(opening.quantity, 0) + COALESCE(movements.quantity, 0)
            AS decimal(28, 3)
        ) AS quantity,
        CAST(
            COALESCE(opening.amount, 0) + COALESCE(movements.amount, 0)
            AS decimal(28, 2)
        ) AS amount,
        CAST(0 AS int) AS negative_cost_row_count,
        CAST(0 AS decimal(28, 2)) AS negative_cost_amount
    FROM opening
    CROSS JOIN movements
""")


@dataclass(frozen=True)
class OneCInventoryCostSnapshot:
    amount: Decimal
    quantity: Decimal
    as_of: date
    source_row_count: int
    negative_cost_row_count: int = 0
    negative_cost_amount: Decimal = Decimal("0.00")
    source_status: str = "ready"
    source_key: str = "onec_inventory_cost"
    source_title: str = "1С УТ 10.3: ПартииТоваровНаСкладах.СтоимостьОстаток"


class OneCInventoryCostError(RuntimeError):
    pass


def fetch_onec_inventory_cost(
    onec_engine: Engine,
    *,
    as_of: date,
) -> OneCInventoryCostSnapshot:
    if as_of > date.today():
        raise OneCInventoryCostError("Нельзя получить товарные остатки на будущую дату")

    if as_of == date.today():
        statement = CURRENT_INVENTORY_COST_SQL
        params = {"current_totals_period": CURRENT_TOTALS_PERIOD}
    else:
        month_start = as_of.replace(day=1)
        statement = HISTORICAL_INVENTORY_COST_SQL
        params = {
            "month_start": datetime.combine(month_start, time.min),
            "date_to": datetime.combine(as_of + timedelta(days=1), time.min),
        }

    with onec_engine.connect() as connection:
        row = connection.execute(statement, params).mappings().one()

    source_row_count = int(row.get("source_row_count") or 0)
    if source_row_count <= 0 or row.get("amount") is None or row.get("quantity") is None:
        raise OneCInventoryCostError("В регистре 1С нет итогов товарных партий на выбранную дату")

    amount = Decimal(str(row["amount"])).quantize(MONEY)
    quantity = Decimal(str(row["quantity"])).quantize(QUANTITY)
    if amount < 0:
        raise OneCInventoryCostError("Итоговая стоимость товарных остатков отрицательная")

    return OneCInventoryCostSnapshot(
        amount=amount,
        quantity=quantity,
        as_of=as_of,
        source_row_count=source_row_count,
        negative_cost_row_count=int(row.get("negative_cost_row_count") or 0),
        negative_cost_amount=Decimal(str(row.get("negative_cost_amount") or 0)).quantize(MONEY),
    )
