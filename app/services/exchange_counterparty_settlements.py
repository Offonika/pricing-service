from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

MSK_TZ = ZoneInfo("Europe/Moscow")
DEFAULT_EXCHANGE_COUNTERPARTY_CODE = "РБ002085"
CANONICAL_SUMMARY_COUNTERPARTY_CODES = {"РБ005290"}
DEFAULT_MOVEMENT_TOLERANCE_RUB = Decimal("100.00")
DEFAULT_CLOSING_TOLERANCE_RUB = Decimal("10000.00")
DEFAULT_RATE_MISMATCH_TOLERANCE_RUB = Decimal("1.00")
DEFAULT_RATE_MISMATCH_LIMIT = 10
RUB_CURRENCY_CODES = {"643"}


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value).strip().replace(" ", "").replace(",", ".") or "0")


def _money(value: Any) -> Decimal:
    return _to_decimal(value).quantize(Decimal("0.01"))


def _native_amount(value: Any) -> Decimal:
    return _to_decimal(value).quantize(Decimal("0.01"))


def _optional_rate(numerator: Decimal, denominator: Decimal) -> str | None:
    if denominator == 0:
        return None
    return str((numerator / denominator).quantize(Decimal("0.000001")))


def _as_msk_naive(value: datetime | None) -> tuple[datetime, str]:
    current = value or datetime.now(MSK_TZ)
    if current.tzinfo is None:
        current_msk = current.replace(tzinfo=MSK_TZ)
    else:
        current_msk = current.astimezone(MSK_TZ)
    return current_msk.replace(tzinfo=None), current_msk.isoformat()


def _default_period_start(as_of_naive: datetime) -> date:
    return as_of_naive.date().replace(day=1)


def _default_rate_check_start(as_of_naive: datetime) -> date:
    return as_of_naive.date().replace(month=1, day=1)


def _is_rub_currency(currency_code: str | None, currency_name: str | None) -> bool:
    code = str(currency_code or "").strip()
    name = str(currency_name or "").strip().lower()
    return code in RUB_CURRENCY_CODES or name in {"руб", "rub", "rur"}


def _control_status(value: Decimal, tolerance: Decimal) -> str:
    return "ok" if abs(value) <= tolerance else "warning"


def _overall_status(*statuses: str) -> str:
    return "warning" if any(status != "ok" for status in statuses) else "ok"


def _uses_canonical_summary_source(counterparty_code: str) -> bool:
    return str(counterparty_code or "").strip() in CANONICAL_SUMMARY_COUNTERPARTY_CODES


def _build_canonical_summary_settlements(
    onec_engine,
    *,
    counterparty_code: str,
    as_of_naive: datetime,
    as_of_msk: str,
    resolved_period_start: date,
    period_start_dt: datetime,
    rate_check_start: date,
    movement_tolerance_rub: Decimal,
    closing_tolerance_rub: Decimal,
) -> dict[str, Any]:
    params = {
        "counterparty_code": counterparty_code,
        "period_start": period_start_dt,
        "as_of": as_of_naive,
    }

    counterparty_sql = text("""
        SELECT TOP 1
            master.dbo.fn_varbintohexstr(c._IDRRef) AS counterparty_ref,
            RTRIM(c._Code) AS counterparty_code,
            c._Description AS counterparty_name
        FROM dbo._Reference54 AS c WITH (NOLOCK)
        WHERE c._Marked = 0x00
          AND RTRIM(c._Code) = :counterparty_code
        """)

    canonical_sql = text("""
        WITH
        latest_opening_period AS (
            SELECT MAX(t._Period) AS period
            FROM dbo._AccumRgT7009 AS t WITH (NOLOCK)
            WHERE t._Period <= :period_start
        ),
        opening_rows AS (
            SELECT
                CAST(t._Fld7008 AS decimal(18, 2)) AS amount
            FROM dbo._AccumRgT7009 AS t WITH (NOLOCK)
            JOIN latest_opening_period AS p
                ON t._Period = p.period
            JOIN dbo._Reference54 AS counterparty WITH (NOLOCK)
                ON counterparty._IDRRef = t._Fld7006RRef
            WHERE RTRIM(counterparty._Code) = :counterparty_code
        ),
        movement_rows AS (
            SELECT
                CAST(
                    CASE
                        WHEN r._RecordKind = 0 THEN r._Fld7008
                        ELSE -r._Fld7008
                    END AS decimal(18, 2)
                ) AS amount,
                r._Period AS movement_at
            FROM dbo._AccumRg7002 AS r WITH (NOLOCK)
            JOIN dbo._Reference54 AS counterparty WITH (NOLOCK)
                ON counterparty._IDRRef = r._Fld7006RRef
            WHERE RTRIM(counterparty._Code) = :counterparty_code
              AND r._Active = 0x01
              AND r._Period >= :period_start
              AND r._Period < :as_of
        )
        SELECT
            CAST((SELECT period FROM latest_opening_period) AS date) AS opening_period,
            CAST(COALESCE((SELECT SUM(amount) FROM opening_rows), 0) AS decimal(18, 2))
                AS opening_balance_rub,
            CAST(COALESCE((SELECT SUM(amount) FROM movement_rows), 0) AS decimal(18, 2))
                AS movement_amount_rub,
            (SELECT COUNT(*) FROM movement_rows) AS movement_count,
            (SELECT MAX(movement_at) FROM movement_rows) AS last_movement_at
        """)

    with onec_engine.connect() as conn:
        counterparty = conn.execute(counterparty_sql, params).mappings().first()
        if counterparty is None:
            return {
                "status": "missing",
                "counterparty_code": counterparty_code,
                "generated_at_msk": as_of_msk,
                "note": "Контрагент не найден в 1С",
            }
        row = conn.execute(canonical_sql, params).mappings().first() or {}

    opening_balance_rub = _money(row.get("opening_balance_rub"))
    signed_movement_rub = _money(row.get("movement_amount_rub"))
    current_balance_rub = _money(opening_balance_rub + signed_movement_rub)
    inflow_amount_rub = signed_movement_rub if signed_movement_rub > 0 else Decimal("0")
    outflow_amount_rub = -signed_movement_rub if signed_movement_rub < 0 else Decimal("0")
    movement_diff_rub = Decimal("0.00")
    movement_status = _control_status(movement_diff_rub, movement_tolerance_rub)
    closing_status = _control_status(current_balance_rub, closing_tolerance_rub)
    rub_control_status = _overall_status(movement_status, closing_status)
    last_movement_at = row.get("last_movement_at")
    movement_count = int(row.get("movement_count") or 0)
    opening_period = row.get("opening_period")
    opening_period_text = opening_period.isoformat() if opening_period else None

    summary_by_currency = [
        {
            "contract_currency_code": "643",
            "contract_currency_name": "руб",
            "contract_count": 0,
            "opening_balance": str(opening_balance_rub),
            "inflow_amount": str(_money(inflow_amount_rub)),
            "outflow_amount": str(_money(outflow_amount_rub)),
            "current_balance": str(current_balance_rub),
            "opening_balance_rub": str(opening_balance_rub),
            "inflow_amount_rub": str(_money(inflow_amount_rub)),
            "outflow_amount_rub": str(_money(outflow_amount_rub)),
            "current_balance_rub": str(current_balance_rub),
            "effective_rate": "1.000000",
            "movement_count": movement_count,
            "last_movement_at": last_movement_at.isoformat() if last_movement_at else None,
        }
    ]

    return {
        "status": "ready",
        "control_status": rub_control_status,
        "counterparty_ref": counterparty["counterparty_ref"],
        "counterparty_code": counterparty["counterparty_code"],
        "counterparty_name": counterparty["counterparty_name"],
        "generated_at_msk": as_of_msk,
        "period_start": resolved_period_start.isoformat(),
        "period_end_msk": as_of_msk,
        "source": "1c_mutual_settlements_canonical_summary",
        "canonical_opening_period": opening_period_text,
        "summary_by_currency": summary_by_currency,
        "rub_control": {
            "rub_inflow": str(_money(inflow_amount_rub)),
            "foreign_outflow_rub": str(_money(outflow_amount_rub)),
            "movement_diff_rub": str(_money(movement_diff_rub)),
            "closing_balance_rub": str(current_balance_rub),
            "movement_tolerance_rub": str(_money(movement_tolerance_rub)),
            "closing_balance_tolerance_rub": str(_money(closing_tolerance_rub)),
            "movement_status": movement_status,
            "closing_status": closing_status,
            "status": rub_control_status,
        },
        "rate_mismatch_control": {
            "status": "ok",
            "check_from": rate_check_start.isoformat(),
            "check_to_msk": as_of_msk,
            "mismatch_count": 0,
            "total_diff_rub": "0.00",
            "total_abs_diff_rub": "0.00",
            "tolerance_rub": str(_money(DEFAULT_RATE_MISMATCH_TOLERANCE_RUB)),
            "returned_count": 0,
            "items": [],
        },
        "contract_balances": [],
        "detail_rows": [],
        "movement_count": movement_count,
    }


def build_exchange_counterparty_settlements(
    onec_engine,
    *,
    counterparty_code: str = DEFAULT_EXCHANGE_COUNTERPARTY_CODE,
    as_of: datetime | None = None,
    period_start: date | None = None,
    movement_tolerance_rub: Decimal = DEFAULT_MOVEMENT_TOLERANCE_RUB,
    closing_tolerance_rub: Decimal = DEFAULT_CLOSING_TOLERANCE_RUB,
    rate_mismatch_tolerance_rub: Decimal = DEFAULT_RATE_MISMATCH_TOLERANCE_RUB,
    rate_mismatch_limit: int = DEFAULT_RATE_MISMATCH_LIMIT,
) -> dict[str, Any]:
    as_of_naive, as_of_msk = _as_msk_naive(as_of)
    counterparty_code = str(counterparty_code or "").strip()
    resolved_period_start = period_start or _default_period_start(as_of_naive)
    period_start_dt = datetime.combine(resolved_period_start, time.min)
    rate_check_start = _default_rate_check_start(as_of_naive)
    rate_check_start_dt = datetime.combine(rate_check_start, time.min)

    if _uses_canonical_summary_source(counterparty_code):
        return _build_canonical_summary_settlements(
            onec_engine,
            counterparty_code=counterparty_code,
            as_of_naive=as_of_naive,
            as_of_msk=as_of_msk,
            resolved_period_start=resolved_period_start,
            period_start_dt=period_start_dt,
            rate_check_start=rate_check_start,
            movement_tolerance_rub=movement_tolerance_rub,
            closing_tolerance_rub=closing_tolerance_rub,
        )

    params = {
        "counterparty_code": counterparty_code,
        "period_start": period_start_dt,
        "rate_check_start": rate_check_start_dt,
        "rate_mismatch_tolerance_rub": rate_mismatch_tolerance_rub,
        "as_of": as_of_naive,
    }

    counterparty_sql = text("""
        SELECT TOP 1
            master.dbo.fn_varbintohexstr(c._IDRRef) AS counterparty_ref,
            RTRIM(c._Code) AS counterparty_code,
            c._Description AS counterparty_name
        FROM dbo._Reference54 AS c WITH (NOLOCK)
        WHERE c._Marked = 0x00
          AND RTRIM(c._Code) = :counterparty_code
        """)

    opening_sql = text("""
        SELECT
            master.dbo.fn_varbintohexstr(t._Fld7615RRef) AS contract_ref,
            RTRIM(contract._Code) AS contract_code,
            contract._Description AS contract_name,
            RTRIM(currency._Code) AS currency_code,
            currency._Description AS currency_name,
            CAST(SUM(CAST(t._Fld7620 AS decimal(18, 2))) AS decimal(18, 2)) AS opening_balance,
            CAST(SUM(CAST(t._Fld7621 AS decimal(18, 2))) AS decimal(18, 2)) AS opening_balance_rub
        FROM dbo._AccumRgT7622 AS t WITH (NOLOCK)
        JOIN dbo._Reference54 AS counterparty WITH (NOLOCK)
            ON counterparty._IDRRef = t._Fld7619RRef
        LEFT JOIN dbo._Reference37 AS contract WITH (NOLOCK)
            ON contract._IDRRef = t._Fld7615RRef
        LEFT JOIN dbo._Reference20 AS currency WITH (NOLOCK)
            ON currency._IDRRef = contract._Fld498RRef
        WHERE RTRIM(counterparty._Code) = :counterparty_code
          AND t._Period = :period_start
        GROUP BY
            t._Fld7615RRef,
            contract._Code,
            contract._Description,
            currency._Code,
            currency._Description
        """)

    movements_sql = text("""
        SELECT
            master.dbo.fn_varbintohexstr(r._Fld7615RRef) AS contract_ref,
            RTRIM(contract._Code) AS contract_code,
            contract._Description AS contract_name,
            RTRIM(currency._Code) AS currency_code,
            currency._Description AS currency_name,
            CAST(SUM(CASE WHEN r._RecordKind = 0 THEN CAST(r._Fld7620 AS decimal(18, 2)) ELSE 0 END) AS decimal(18, 2)) AS inflow_amount,
            CAST(SUM(CASE WHEN r._RecordKind = 1 THEN CAST(r._Fld7620 AS decimal(18, 2)) ELSE 0 END) AS decimal(18, 2)) AS outflow_amount,
            CAST(SUM(CASE WHEN r._RecordKind = 0 THEN CAST(r._Fld7621 AS decimal(18, 2)) ELSE 0 END) AS decimal(18, 2)) AS inflow_amount_rub,
            CAST(SUM(CASE WHEN r._RecordKind = 1 THEN CAST(r._Fld7621 AS decimal(18, 2)) ELSE 0 END) AS decimal(18, 2)) AS outflow_amount_rub,
            COUNT(*) AS movement_count,
            MAX(r._Period) AS last_movement_at
        FROM dbo._AccumRg7614 AS r WITH (NOLOCK)
        JOIN dbo._Reference54 AS counterparty WITH (NOLOCK)
            ON counterparty._IDRRef = r._Fld7619RRef
        LEFT JOIN dbo._Reference37 AS contract WITH (NOLOCK)
            ON contract._IDRRef = r._Fld7615RRef
        LEFT JOIN dbo._Reference20 AS currency WITH (NOLOCK)
            ON currency._IDRRef = contract._Fld498RRef
        WHERE RTRIM(counterparty._Code) = :counterparty_code
          AND r._Active = 0x01
          AND r._Period >= :period_start
          AND r._Period < :as_of
        GROUP BY
            r._Fld7615RRef,
            contract._Code,
            contract._Description,
            currency._Code,
            currency._Description
        """)

    rate_mismatch_sql = text("""
        WITH document_rates AS (
            SELECT
                pko._IDRRef AS document_ref,
                master.dbo.fn_varbintohexstr(pko._IDRRef) AS document_ref_hex,
                RTRIM(pko._Number) AS document_number,
                pko._Date_Time AS document_at,
                vt._LineNo4709 AS line_number,
                vt._Fld4710RRef AS contract_ref,
                CAST(vt._Fld4712 AS decimal(18, 6)) AS document_rate,
                CAST(vt._Fld4713 AS decimal(18, 2)) AS document_amount,
                CAST(vt._Fld4714 AS decimal(18, 6)) AS document_multiplicity,
                CAST(
                    CASE
                        WHEN vt._Fld4714 = 0 THEN NULL
                        ELSE vt._Fld4713 * vt._Fld4712 / vt._Fld4714
                    END AS decimal(18, 2)
                ) AS expected_rub
            FROM dbo._Document196 AS pko WITH (NOLOCK)
            JOIN dbo._Document196_VT4708 AS vt WITH (NOLOCK)
                ON vt._Document196_IDRRef = pko._IDRRef
            JOIN dbo._Reference54 AS counterparty WITH (NOLOCK)
                ON counterparty._IDRRef = pko._Fld4684_RRRef
            WHERE RTRIM(counterparty._Code) = :counterparty_code
              AND pko._Marked = 0x00
              AND pko._Posted = 0x01
              AND pko._Date_Time >= :rate_check_start
              AND pko._Date_Time < :as_of
        ),
        movements AS (
            SELECT
                r._RecorderRRef AS document_ref,
                r._Fld7615RRef AS contract_ref,
                RTRIM(contract._Description) AS contract_name,
                RTRIM(currency._Description) AS currency_name,
                CAST(r._Fld7620 AS decimal(18, 2)) AS movement_amount,
                CAST(r._Fld7621 AS decimal(18, 2)) AS movement_rub,
                r._RecordKind AS record_kind
            FROM dbo._AccumRg7614 AS r WITH (NOLOCK)
            JOIN dbo._Reference54 AS counterparty WITH (NOLOCK)
                ON counterparty._IDRRef = r._Fld7619RRef
            LEFT JOIN dbo._Reference37 AS contract WITH (NOLOCK)
                ON contract._IDRRef = r._Fld7615RRef
            LEFT JOIN dbo._Reference20 AS currency WITH (NOLOCK)
                ON currency._IDRRef = contract._Fld498RRef
            WHERE RTRIM(counterparty._Code) = :counterparty_code
              AND r._Active = 0x01
              AND r._RecorderTRef = 0x000000c4
              AND r._RecordKind = 1
              AND (RTRIM(currency._Description) <> N'руб' OR currency._Description IS NULL)
              AND r._Period >= :rate_check_start
              AND r._Period < :as_of
        )
        SELECT
            d.document_ref_hex,
            d.document_number,
            d.document_at,
            d.line_number,
            m.contract_name,
            m.currency_name,
            d.document_amount,
            d.document_rate,
            d.document_multiplicity,
            d.expected_rub,
            m.movement_amount,
            m.movement_rub,
            CAST(d.expected_rub - m.movement_rub AS decimal(18, 2)) AS diff_rub
        FROM document_rates AS d
        JOIN movements AS m
            ON m.document_ref = d.document_ref
           AND m.contract_ref = d.contract_ref
        WHERE d.expected_rub IS NOT NULL
          AND ABS(CAST(d.expected_rub - m.movement_rub AS decimal(18, 2)))
              > :rate_mismatch_tolerance_rub
        ORDER BY
            ABS(CAST(d.expected_rub - m.movement_rub AS decimal(18, 2))) DESC,
            d.document_at
        """)

    with onec_engine.connect() as conn:
        counterparty = conn.execute(counterparty_sql, params).mappings().first()
        if counterparty is None:
            return {
                "status": "missing",
                "counterparty_code": counterparty_code,
                "generated_at_msk": as_of_msk,
                "note": "Контрагент не найден в 1С",
            }
        opening_rows = conn.execute(opening_sql, params).mappings().all()
        movement_rows = conn.execute(movements_sql, params).mappings().all()
        rate_mismatch_rows = conn.execute(rate_mismatch_sql, params).mappings().all()

    contracts: dict[str, dict[str, Any]] = {}

    def _state(row: Any) -> dict[str, Any]:
        contract_ref = str(row.get("contract_ref") or "")
        return contracts.setdefault(
            contract_ref,
            {
                "contract_ref": contract_ref,
                "contract_code": str(row.get("contract_code") or "").strip() or None,
                "contract_name": str(row.get("contract_name") or "").strip() or None,
                "contract_currency_code": str(row.get("currency_code") or "").strip() or None,
                "contract_currency_name": str(row.get("currency_name") or "").strip() or None,
                "opening_balance": Decimal("0"),
                "opening_balance_rub": Decimal("0"),
                "inflow_amount": Decimal("0"),
                "outflow_amount": Decimal("0"),
                "inflow_amount_rub": Decimal("0"),
                "outflow_amount_rub": Decimal("0"),
                "movement_count": 0,
                "last_movement_at": None,
            },
        )

    for row in opening_rows:
        state = _state(row)
        state["opening_balance"] += _native_amount(row.get("opening_balance"))
        state["opening_balance_rub"] += _money(row.get("opening_balance_rub"))

    for row in movement_rows:
        state = _state(row)
        state["inflow_amount"] += _native_amount(row.get("inflow_amount"))
        state["outflow_amount"] += _native_amount(row.get("outflow_amount"))
        state["inflow_amount_rub"] += _money(row.get("inflow_amount_rub"))
        state["outflow_amount_rub"] += _money(row.get("outflow_amount_rub"))
        state["movement_count"] += int(row.get("movement_count") or 0)
        last_movement_at = row.get("last_movement_at")
        if last_movement_at and (
            state["last_movement_at"] is None or last_movement_at > state["last_movement_at"]
        ):
            state["last_movement_at"] = last_movement_at

    contract_balances: list[dict[str, Any]] = []
    summary_state: dict[tuple[str | None, str | None], dict[str, Any]] = defaultdict(
        lambda: {
            "contract_currency_code": None,
            "contract_currency_name": None,
            "contract_count": 0,
            "opening_balance": Decimal("0"),
            "inflow_amount": Decimal("0"),
            "outflow_amount": Decimal("0"),
            "current_balance": Decimal("0"),
            "opening_balance_rub": Decimal("0"),
            "inflow_amount_rub": Decimal("0"),
            "outflow_amount_rub": Decimal("0"),
            "current_balance_rub": Decimal("0"),
            "movement_count": 0,
            "last_movement_at": None,
        }
    )

    rub_inflow = Decimal("0")
    foreign_outflow_rub = Decimal("0")
    closing_balance_rub = Decimal("0")
    total_movement_count = 0

    for state in contracts.values():
        current_balance = _native_amount(
            state["opening_balance"] + state["inflow_amount"] - state["outflow_amount"]
        )
        current_balance_rub = _money(
            state["opening_balance_rub"] + state["inflow_amount_rub"] - state["outflow_amount_rub"]
        )
        currency_code = state["contract_currency_code"]
        currency_name = state["contract_currency_name"]
        is_rub = _is_rub_currency(currency_code, currency_name)
        if is_rub:
            rub_inflow += _money(state["inflow_amount_rub"])
        else:
            foreign_outflow_rub += _money(state["outflow_amount_rub"])
        closing_balance_rub += current_balance_rub
        total_movement_count += int(state["movement_count"])

        movement_native = _native_amount(state["inflow_amount"] or state["outflow_amount"])
        movement_rub = _money(state["inflow_amount_rub"] or state["outflow_amount_rub"])
        contract_item = {
            "contract_ref": state["contract_ref"],
            "contract_code": state["contract_code"],
            "contract_name": state["contract_name"],
            "contract_currency_code": currency_code,
            "contract_currency_name": currency_name,
            "opening_balance": str(_native_amount(state["opening_balance"])),
            "inflow_amount": str(_native_amount(state["inflow_amount"])),
            "outflow_amount": str(_native_amount(state["outflow_amount"])),
            "current_balance": str(current_balance),
            "opening_balance_rub": str(_money(state["opening_balance_rub"])),
            "inflow_amount_rub": str(_money(state["inflow_amount_rub"])),
            "outflow_amount_rub": str(_money(state["outflow_amount_rub"])),
            "current_balance_rub": str(current_balance_rub),
            "effective_rate": _optional_rate(movement_rub, movement_native),
            "movement_count": int(state["movement_count"]),
            "last_movement_at": (
                state["last_movement_at"].isoformat() if state["last_movement_at"] else None
            ),
        }
        if any(
            _to_decimal(contract_item[key]) != 0
            for key in (
                "opening_balance",
                "inflow_amount",
                "outflow_amount",
                "current_balance",
                "current_balance_rub",
            )
        ):
            contract_balances.append(contract_item)

        key = (currency_code, currency_name)
        summary = summary_state[key]
        summary["contract_currency_code"] = currency_code
        summary["contract_currency_name"] = currency_name
        summary["contract_count"] += 1
        summary["opening_balance"] += _native_amount(state["opening_balance"])
        summary["inflow_amount"] += _native_amount(state["inflow_amount"])
        summary["outflow_amount"] += _native_amount(state["outflow_amount"])
        summary["current_balance"] += current_balance
        summary["opening_balance_rub"] += _money(state["opening_balance_rub"])
        summary["inflow_amount_rub"] += _money(state["inflow_amount_rub"])
        summary["outflow_amount_rub"] += _money(state["outflow_amount_rub"])
        summary["current_balance_rub"] += current_balance_rub
        summary["movement_count"] += int(state["movement_count"])
        if state["last_movement_at"] and (
            summary["last_movement_at"] is None
            or state["last_movement_at"] > summary["last_movement_at"]
        ):
            summary["last_movement_at"] = state["last_movement_at"]

    def _summary_item(item: dict[str, Any]) -> dict[str, Any]:
        movement_native = _native_amount(item["inflow_amount"] or item["outflow_amount"])
        movement_rub = _money(item["inflow_amount_rub"] or item["outflow_amount_rub"])
        return {
            "contract_currency_code": item["contract_currency_code"],
            "contract_currency_name": item["contract_currency_name"],
            "contract_count": item["contract_count"],
            "opening_balance": str(_native_amount(item["opening_balance"])),
            "inflow_amount": str(_native_amount(item["inflow_amount"])),
            "outflow_amount": str(_native_amount(item["outflow_amount"])),
            "current_balance": str(_native_amount(item["current_balance"])),
            "opening_balance_rub": str(_money(item["opening_balance_rub"])),
            "inflow_amount_rub": str(_money(item["inflow_amount_rub"])),
            "outflow_amount_rub": str(_money(item["outflow_amount_rub"])),
            "current_balance_rub": str(_money(item["current_balance_rub"])),
            "effective_rate": _optional_rate(movement_rub, movement_native),
            "movement_count": item["movement_count"],
            "last_movement_at": (
                item["last_movement_at"].isoformat() if item["last_movement_at"] else None
            ),
        }

    movement_diff_rub = _money(rub_inflow - foreign_outflow_rub)
    closing_balance_rub = _money(closing_balance_rub)
    movement_status = _control_status(movement_diff_rub, movement_tolerance_rub)
    closing_status = _control_status(closing_balance_rub, closing_tolerance_rub)
    rub_control_status = _overall_status(movement_status, closing_status)

    rate_mismatch_total_rub = _money(
        sum((_to_decimal(row.get("diff_rub")) for row in rate_mismatch_rows), Decimal("0"))
    )
    rate_mismatch_abs_total_rub = _money(
        sum((abs(_to_decimal(row.get("diff_rub"))) for row in rate_mismatch_rows), Decimal("0"))
    )
    rate_mismatch_status = "ok" if not rate_mismatch_rows else "warning"
    status = _overall_status(rub_control_status, rate_mismatch_status)

    rate_mismatch_items = []
    for row in rate_mismatch_rows[: max(0, int(rate_mismatch_limit))]:
        document_at = row.get("document_at")
        rate_mismatch_items.append(
            {
                "document_type": "Приходный кассовый ордер",
                "document_ref": str(row.get("document_ref_hex") or ""),
                "document_number": str(row.get("document_number") or "").strip(),
                "document_at": document_at.isoformat() if document_at else None,
                "line_number": int(row.get("line_number") or 0),
                "contract_name": str(row.get("contract_name") or "").strip() or None,
                "currency_name": str(row.get("currency_name") or "").strip() or None,
                "document_amount": str(_native_amount(row.get("document_amount"))),
                "document_rate": str(_to_decimal(row.get("document_rate"))),
                "document_multiplicity": str(_to_decimal(row.get("document_multiplicity"))),
                "expected_rub": str(_money(row.get("expected_rub"))),
                "movement_amount": str(_native_amount(row.get("movement_amount"))),
                "movement_rub": str(_money(row.get("movement_rub"))),
                "diff_rub": str(_money(row.get("diff_rub"))),
            }
        )

    contract_balances.sort(
        key=lambda item: (
            str(item.get("contract_currency_code") or ""),
            str(item.get("contract_name") or ""),
        )
    )
    summary_by_currency = sorted(
        (_summary_item(item) for item in summary_state.values()),
        key=lambda item: str(item.get("contract_currency_code") or ""),
    )

    return {
        "status": "ready",
        "control_status": status,
        "counterparty_ref": counterparty["counterparty_ref"],
        "counterparty_code": counterparty["counterparty_code"],
        "counterparty_name": counterparty["counterparty_name"],
        "generated_at_msk": as_of_msk,
        "period_start": resolved_period_start.isoformat(),
        "period_end_msk": as_of_msk,
        "source": "1c_mutual_settlements",
        "summary_by_currency": summary_by_currency,
        "rub_control": {
            "rub_inflow": str(_money(rub_inflow)),
            "foreign_outflow_rub": str(_money(foreign_outflow_rub)),
            "movement_diff_rub": str(movement_diff_rub),
            "closing_balance_rub": str(closing_balance_rub),
            "movement_tolerance_rub": str(_money(movement_tolerance_rub)),
            "closing_balance_tolerance_rub": str(_money(closing_tolerance_rub)),
            "movement_status": movement_status,
            "closing_status": closing_status,
            "status": rub_control_status,
        },
        "rate_mismatch_control": {
            "status": rate_mismatch_status,
            "check_from": rate_check_start.isoformat(),
            "check_to_msk": as_of_msk,
            "mismatch_count": len(rate_mismatch_rows),
            "total_diff_rub": str(rate_mismatch_total_rub),
            "total_abs_diff_rub": str(rate_mismatch_abs_total_rub),
            "tolerance_rub": str(_money(rate_mismatch_tolerance_rub)),
            "returned_count": len(rate_mismatch_items),
            "items": rate_mismatch_items,
        },
        "contract_balances": contract_balances,
        "detail_rows": [],
        "movement_count": total_movement_count,
    }
