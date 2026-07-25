from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.sql.elements import TextClause

MONEY = Decimal("0.01")
QUANTITY = Decimal("0.001")
CURRENT_TOTALS_PERIOD = datetime(3999, 11, 1)
# Перечисления.СтатусыПартийТоваров.ПоОрдеру. Стандартный отчёт УТ 10.3
# исключает этот статус из расчёта средней себестоимости партий.
UNBILLED_PARTY_STATUS_HEX = "974183E5A661EE844981A81E806CF09D"


def _build_inventory_cost_sql(
    *,
    stock_source_sql: str,
    party_source_sql: str,
) -> TextClause:
    """Build the SQL equivalent of the standard UT 10.3 mixed-mode report."""
    return text(f"""
        WITH stock_source AS (
            {stock_source_sql}
        ),
        stock AS (
            SELECT
                item_ref,
                characteristic_ref,
                warehouse_ref,
                SUM(quantity) AS quantity
            FROM stock_source
            GROUP BY item_ref, characteristic_ref, warehouse_ref
            HAVING SUM(quantity) <> 0
        ),
        party_source AS (
            {party_source_sql}
        ),
        valuation_party AS (
            SELECT
                item_ref,
                characteristic_ref,
                SUM(quantity) AS quantity,
                SUM(amount) AS amount
            FROM party_source
            WHERE is_unbilled = 0
            GROUP BY item_ref, characteristic_ref
            HAVING SUM(quantity) <> 0 OR SUM(amount) <> 0
        ),
        valuation_rows AS (
            SELECT
                stock.quantity AS stock_quantity,
                party.quantity AS party_quantity,
                party.amount AS party_amount,
                CAST(
                    party.amount
                    / CASE
                        WHEN party.quantity IS NULL OR party.quantity = 0 THEN 1
                        ELSE party.quantity
                      END
                    * stock.quantity
                    AS decimal(15, 2)
                ) AS amount,
                CASE WHEN party.item_ref IS NULL THEN 1 ELSE 0 END AS unmatched_stock,
                CASE
                    WHEN party.item_ref IS NOT NULL AND party.quantity = 0 THEN 1
                    ELSE 0
                END AS zero_party_quantity
            FROM stock
            LEFT JOIN valuation_party AS party
              ON party.item_ref = stock.item_ref
             AND party.characteristic_ref = stock.characteristic_ref
        ),
        source_summary AS (
            SELECT
                (SELECT COUNT_BIG(*) FROM stock_source) AS stock_source_row_count,
                (SELECT COUNT_BIG(*) FROM party_source) AS party_source_row_count
        ),
        stock_summary AS (
            SELECT
                COUNT_BIG(*) AS stock_row_count,
                SUM(quantity) AS stock_quantity
            FROM stock
        ),
        party_summary AS (
            SELECT
                COUNT_BIG(*) AS party_row_count,
                SUM(quantity) AS valuation_party_quantity,
                SUM(amount) AS valuation_party_amount
            FROM valuation_party
        ),
        party_control_summary AS (
            SELECT
                SUM(quantity) AS party_quantity,
                SUM(amount) AS party_amount,
                SUM(CASE WHEN is_unbilled = 1 THEN quantity ELSE 0 END)
                    AS excluded_party_quantity,
                SUM(CASE WHEN is_unbilled = 1 THEN amount ELSE 0 END)
                    AS excluded_party_amount
            FROM party_source
        ),
        valuation_summary AS (
            SELECT
                SUM(amount) AS amount,
                SUM(CASE WHEN amount < 0 THEN 1 ELSE 0 END) AS negative_cost_row_count,
                SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END) AS negative_cost_amount,
                SUM(unmatched_stock) AS unmatched_stock_row_count,
                SUM(
                    CASE WHEN unmatched_stock = 1 THEN stock_quantity ELSE 0 END
                ) AS unmatched_stock_quantity,
                SUM(
                    CASE WHEN unmatched_stock = 1 THEN ABS(stock_quantity) ELSE 0 END
                ) AS unmatched_stock_quantity_abs,
                SUM(zero_party_quantity) AS zero_party_quantity_row_count
            FROM valuation_rows
        )
        SELECT
            source.stock_source_row_count + source.party_source_row_count
                AS source_row_count,
            source.stock_source_row_count,
            source.party_source_row_count,
            stock.stock_row_count,
            party.party_row_count,
            CAST(COALESCE(stock.stock_quantity, 0) AS decimal(28, 3)) AS quantity,
            CAST(COALESCE(valuation.amount, 0) AS decimal(28, 2)) AS amount,
            CAST(COALESCE(control.party_quantity, 0) AS decimal(28, 3))
                AS party_quantity,
            CAST(COALESCE(control.party_amount, 0) AS decimal(28, 2))
                AS party_amount,
            CAST(COALESCE(party.valuation_party_quantity, 0) AS decimal(28, 3))
                AS valuation_party_quantity,
            CAST(COALESCE(party.valuation_party_amount, 0) AS decimal(28, 2))
                AS valuation_party_amount,
            CAST(COALESCE(control.excluded_party_quantity, 0) AS decimal(28, 3))
                AS excluded_party_quantity,
            CAST(COALESCE(control.excluded_party_amount, 0) AS decimal(28, 2))
                AS excluded_party_amount,
            CAST(
                COALESCE(stock.stock_quantity, 0) - COALESCE(control.party_quantity, 0)
                AS decimal(28, 3)
            ) AS quantity_difference,
            COALESCE(valuation.negative_cost_row_count, 0) AS negative_cost_row_count,
            CAST(COALESCE(valuation.negative_cost_amount, 0) AS decimal(28, 2))
                AS negative_cost_amount,
            COALESCE(valuation.unmatched_stock_row_count, 0)
                AS unmatched_stock_row_count,
            CAST(COALESCE(valuation.unmatched_stock_quantity, 0) AS decimal(28, 3))
                AS unmatched_stock_quantity,
            CAST(COALESCE(valuation.unmatched_stock_quantity_abs, 0) AS decimal(28, 3))
                AS unmatched_stock_quantity_abs,
            COALESCE(valuation.zero_party_quantity_row_count, 0)
                AS zero_party_quantity_row_count
        FROM source_summary AS source
        CROSS JOIN stock_summary AS stock
        CROSS JOIN party_summary AS party
        CROSS JOIN party_control_summary AS control
        CROSS JOIN valuation_summary AS valuation
    """)


CURRENT_INVENTORY_COST_SQL = _build_inventory_cost_sql(
    stock_source_sql="""
        SELECT
            t._Fld7738RRef AS item_ref,
            t._Fld7739RRef AS characteristic_ref,
            t._Fld7742RRef AS warehouse_ref,
            CAST(t._Fld7743 AS decimal(28, 3)) AS quantity
        FROM dbo._AccumRgT7745 AS t WITH (NOLOCK)
        WHERE t._Period = :current_totals_period
    """,
    party_source_sql=f"""
        SELECT
            t._Fld7454RRef AS item_ref,
            t._Fld7456RRef AS characteristic_ref,
            CAST(t._Fld7462 AS decimal(28, 3)) AS quantity,
            CAST(t._Fld7463 AS decimal(28, 2)) AS amount,
            CASE
                WHEN t._Fld7459RRef = 0x{UNBILLED_PARTY_STATUS_HEX} THEN 1
                ELSE 0
            END AS is_unbilled
        FROM dbo._AccumRgT7473 AS t WITH (NOLOCK)
        WHERE t._Period = :current_totals_period
    """,
)

HISTORICAL_INVENTORY_COST_SQL = _build_inventory_cost_sql(
    stock_source_sql="""
        SELECT
            t._Fld7738RRef AS item_ref,
            t._Fld7739RRef AS characteristic_ref,
            t._Fld7742RRef AS warehouse_ref,
            CAST(t._Fld7743 AS decimal(28, 3)) AS quantity
        FROM dbo._AccumRgT7745 AS t WITH (NOLOCK)
        WHERE t._Period = :month_start

        UNION ALL

        SELECT
            r._Fld7738RRef AS item_ref,
            r._Fld7739RRef AS characteristic_ref,
            r._Fld7742RRef AS warehouse_ref,
            CAST(
                CASE WHEN r._RecordKind = 0 THEN r._Fld7743 ELSE -r._Fld7743 END
                AS decimal(28, 3)
            ) AS quantity
        FROM dbo._AccumRg7735 AS r WITH (NOLOCK)
        WHERE r._Active = 0x01
          AND r._Period >= :month_start
          AND r._Period < :date_to
    """,
    party_source_sql=f"""
        SELECT
            t._Fld7454RRef AS item_ref,
            t._Fld7456RRef AS characteristic_ref,
            CAST(t._Fld7462 AS decimal(28, 3)) AS quantity,
            CAST(t._Fld7463 AS decimal(28, 2)) AS amount,
            CASE
                WHEN t._Fld7459RRef = 0x{UNBILLED_PARTY_STATUS_HEX} THEN 1
                ELSE 0
            END AS is_unbilled
        FROM dbo._AccumRgT7473 AS t WITH (NOLOCK)
        WHERE t._Period = :month_start

        UNION ALL

        SELECT
            r._Fld7454RRef AS item_ref,
            r._Fld7456RRef AS characteristic_ref,
            CAST(
                CASE WHEN r._RecordKind = 0 THEN r._Fld7462 ELSE -r._Fld7462 END
                AS decimal(28, 3)
            ) AS quantity,
            CAST(
                CASE WHEN r._RecordKind = 0 THEN r._Fld7463 ELSE -r._Fld7463 END
                AS decimal(28, 2)
            ) AS amount,
            CASE
                WHEN r._Fld7459RRef = 0x{UNBILLED_PARTY_STATUS_HEX} THEN 1
                ELSE 0
            END AS is_unbilled
        FROM dbo._AccumRg7453 AS r WITH (NOLOCK)
        WHERE r._Active = 0x01
          AND r._Period >= :month_start
          AND r._Period < :date_to
    """,
)


@dataclass(frozen=True)
class OneCInventoryCostSnapshot:
    amount: Decimal
    quantity: Decimal
    as_of: date
    source_row_count: int
    party_quantity: Decimal = Decimal("0.000")
    party_amount: Decimal = Decimal("0.00")
    valuation_party_quantity: Decimal = Decimal("0.000")
    valuation_party_amount: Decimal = Decimal("0.00")
    excluded_party_quantity: Decimal = Decimal("0.000")
    excluded_party_amount: Decimal = Decimal("0.00")
    quantity_difference: Decimal = Decimal("0.000")
    stock_source_row_count: int = 0
    party_source_row_count: int = 0
    stock_row_count: int = 0
    party_row_count: int = 0
    unmatched_stock_row_count: int = 0
    unmatched_stock_quantity: Decimal = Decimal("0.000")
    unmatched_stock_quantity_abs: Decimal = Decimal("0.000")
    zero_party_quantity_row_count: int = 0
    negative_cost_row_count: int = 0
    negative_cost_amount: Decimal = Decimal("0.00")
    source_status: str = "ready"
    reconciliation_status: str = "ready"
    valuation_method: str = "ut103_mixed_stock_quantity_party_average"
    source_key: str = "onec_inventory_cost"
    source_title: str = (
        "1С УТ 10.3: смешанный режим стандартного отчёта — количество по складам "
        "× средняя себестоимость партий"
    )


class OneCInventoryCostError(RuntimeError):
    pass


def _decimal_or_default(
    value: object | None,
    *,
    default: Decimal,
    quantum: Decimal,
) -> Decimal:
    return Decimal(str(default if value is None else value)).quantize(quantum)


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

    party_quantity = _decimal_or_default(
        row.get("party_quantity"),
        default=quantity,
        quantum=QUANTITY,
    )
    party_amount = _decimal_or_default(
        row.get("party_amount"),
        default=amount,
        quantum=MONEY,
    )
    valuation_party_quantity = _decimal_or_default(
        row.get("valuation_party_quantity"),
        default=party_quantity,
        quantum=QUANTITY,
    )
    valuation_party_amount = _decimal_or_default(
        row.get("valuation_party_amount"),
        default=party_amount,
        quantum=MONEY,
    )
    quantity_difference = _decimal_or_default(
        row.get("quantity_difference"),
        default=quantity - party_quantity,
        quantum=QUANTITY,
    )
    unmatched_stock_row_count = int(row.get("unmatched_stock_row_count") or 0)
    unmatched_stock_quantity_abs = _decimal_or_default(
        row.get("unmatched_stock_quantity_abs"),
        default=Decimal("0"),
        quantum=QUANTITY,
    )
    zero_party_quantity_row_count = int(row.get("zero_party_quantity_row_count") or 0)
    reconciled = (
        abs(quantity_difference) < QUANTITY
        and unmatched_stock_row_count == 0
        and unmatched_stock_quantity_abs < QUANTITY
        and zero_party_quantity_row_count == 0
    )

    return OneCInventoryCostSnapshot(
        amount=amount,
        quantity=quantity,
        as_of=as_of,
        source_row_count=source_row_count,
        party_quantity=party_quantity,
        party_amount=party_amount,
        valuation_party_quantity=valuation_party_quantity,
        valuation_party_amount=valuation_party_amount,
        excluded_party_quantity=_decimal_or_default(
            row.get("excluded_party_quantity"),
            default=Decimal("0"),
            quantum=QUANTITY,
        ),
        excluded_party_amount=_decimal_or_default(
            row.get("excluded_party_amount"),
            default=Decimal("0"),
            quantum=MONEY,
        ),
        quantity_difference=quantity_difference,
        stock_source_row_count=int(row.get("stock_source_row_count") or 0),
        party_source_row_count=int(row.get("party_source_row_count") or 0),
        stock_row_count=int(row.get("stock_row_count") or 0),
        party_row_count=int(row.get("party_row_count") or 0),
        unmatched_stock_row_count=unmatched_stock_row_count,
        unmatched_stock_quantity=_decimal_or_default(
            row.get("unmatched_stock_quantity"),
            default=Decimal("0"),
            quantum=QUANTITY,
        ),
        unmatched_stock_quantity_abs=unmatched_stock_quantity_abs,
        zero_party_quantity_row_count=zero_party_quantity_row_count,
        negative_cost_row_count=int(row.get("negative_cost_row_count") or 0),
        negative_cost_amount=Decimal(str(row.get("negative_cost_amount") or 0)).quantize(MONEY),
        source_status="ready" if reconciled else "partial",
        reconciliation_status="ready" if reconciled else "quantity_mismatch",
    )
