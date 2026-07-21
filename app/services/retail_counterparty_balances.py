from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text

MSK_TZ = ZoneInfo("Europe/Moscow")
EXPECTED_ZERO_BALANCE_RUB = Decimal("0.00")
SOURCE_NAME = "1c_mutual_settlements_canonical_summary"


def _money(value: Any) -> Decimal:
    if value is None:
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"))
    return Decimal(str(value).strip().replace(" ", "").replace(",", ".") or "0").quantize(
        Decimal("0.01")
    )


def normalize_counterparty_codes(counterparty_codes: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in counterparty_codes:
        code = str(value or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        result.append(code)
    return result


def _as_msk_naive(value: datetime | None) -> tuple[datetime, str]:
    current = value or datetime.now(MSK_TZ)
    if current.tzinfo is None:
        current_msk = current.replace(tzinfo=MSK_TZ)
    else:
        current_msk = current.astimezone(MSK_TZ)
    return current_msk.replace(tzinfo=None), current_msk.isoformat()


def _default_period_start(as_of_naive: datetime) -> date:
    return as_of_naive.date().replace(day=1)


def validate_retail_balance_period(
    *,
    period_start: date | None,
    as_of: datetime | None,
) -> tuple[datetime, str, date]:
    as_of_naive, generated_at_msk = _as_msk_naive(as_of)
    resolved_period_start = period_start or _default_period_start(as_of_naive)
    if datetime.combine(resolved_period_start, time.min) > as_of_naive:
        raise ValueError("period_start must not be later than as_of")
    return as_of_naive, generated_at_msk, resolved_period_start


def build_unavailable_retail_counterparty_zero_balances(
    counterparty_codes: list[str],
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    codes = normalize_counterparty_codes(counterparty_codes)
    _, generated_at_msk = _as_msk_naive(as_of)
    return {
        "status": "unavailable",
        "source": SOURCE_NAME,
        "generated_at_msk": generated_at_msk,
        "expected_balance_rub": EXPECTED_ZERO_BALANCE_RUB,
        "requested_count": len(codes),
        "checked_count": 0,
        "warning_count": 0,
        "missing_count": 0,
        "unavailable_count": len(codes),
        "items": [
            {
                "counterparty_code": code,
                "counterparty_name": None,
                "counterparty_ref": None,
                "current_balance_rub": None,
                "status": "unavailable",
            }
            for code in codes
        ],
    }


def build_retail_counterparty_zero_balances(
    onec_engine,
    *,
    counterparty_codes: list[str],
    as_of: datetime | None = None,
    period_start: date | None = None,
) -> dict[str, Any]:
    codes = normalize_counterparty_codes(counterparty_codes)
    if not codes:
        raise ValueError("at least one counterparty code is required")

    as_of_naive, generated_at_msk, resolved_period_start = validate_retail_balance_period(
        period_start=period_start,
        as_of=as_of,
    )
    params = {
        "counterparty_codes": codes,
        "period_start": datetime.combine(resolved_period_start, time.min),
        "as_of": as_of_naive,
    }
    statement = text("""
        WITH latest_opening_period AS (
            SELECT MAX(t._Period) AS period
            FROM dbo._AccumRgT7009 AS t WITH (NOLOCK)
            WHERE t._Period <= :period_start
        )
        SELECT
            master.dbo.fn_varbintohexstr(counterparty._IDRRef) AS counterparty_ref,
            RTRIM(counterparty._Code) AS counterparty_code,
            counterparty._Description AS counterparty_name,
            CAST(
                COALESCE(opening.opening_balance_rub, 0)
                + COALESCE(movement.movement_amount_rub, 0)
                AS decimal(18, 2)
            ) AS current_balance_rub
        FROM dbo._Reference54 AS counterparty WITH (NOLOCK)
        OUTER APPLY (
            SELECT SUM(CAST(t._Fld7008 AS decimal(18, 2))) AS opening_balance_rub
            FROM dbo._AccumRgT7009 AS t WITH (NOLOCK)
            WHERE t._Period = (SELECT period FROM latest_opening_period)
              AND t._Fld7006RRef = counterparty._IDRRef
        ) AS opening
        OUTER APPLY (
            SELECT SUM(
                CAST(
                    CASE
                        WHEN movement_row._RecordKind = 0 THEN movement_row._Fld7008
                        ELSE -movement_row._Fld7008
                    END AS decimal(18, 2)
                )
            ) AS movement_amount_rub
            FROM dbo._AccumRg7002 AS movement_row WITH (NOLOCK)
            WHERE movement_row._Fld7006RRef = counterparty._IDRRef
              AND movement_row._Active = 0x01
              AND movement_row._Period >= :period_start
              AND movement_row._Period < :as_of
        ) AS movement
        WHERE counterparty._Marked = 0x00
          AND RTRIM(counterparty._Code) IN :counterparty_codes
        """).bindparams(bindparam("counterparty_codes", expanding=True))

    with onec_engine.connect() as connection:
        rows = connection.execute(statement, params).mappings().all()

    rows_by_code = {str(row.get("counterparty_code") or "").strip(): row for row in rows}
    items: list[dict[str, Any]] = []
    warning_count = 0
    missing_count = 0
    for code in codes:
        row = rows_by_code.get(code)
        if row is None:
            missing_count += 1
            items.append(
                {
                    "counterparty_code": code,
                    "counterparty_name": None,
                    "counterparty_ref": None,
                    "current_balance_rub": None,
                    "status": "missing",
                }
            )
            continue
        balance = _money(row.get("current_balance_rub"))
        item_status = "ok" if balance == EXPECTED_ZERO_BALANCE_RUB else "warning"
        if item_status == "warning":
            warning_count += 1
        items.append(
            {
                "counterparty_code": code,
                "counterparty_name": str(row.get("counterparty_name") or "").strip() or None,
                "counterparty_ref": str(row.get("counterparty_ref") or "").strip() or None,
                "current_balance_rub": balance,
                "status": item_status,
            }
        )

    checked_count = len(codes) - missing_count
    return {
        "status": "ready" if missing_count == 0 else "partial",
        "source": SOURCE_NAME,
        "generated_at_msk": generated_at_msk,
        "expected_balance_rub": EXPECTED_ZERO_BALANCE_RUB,
        "requested_count": len(codes),
        "checked_count": checked_count,
        "warning_count": warning_count,
        "missing_count": missing_count,
        "unavailable_count": 0,
        "items": items,
    }
