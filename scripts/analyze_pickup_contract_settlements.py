"""Build contract-level 1C settlement report for pickup orders.

The input is a previously built pickup debt/unclaimed reconciliation CSV. The
script keeps the check read-only: it only reads 1C and writes local artifacts.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from app.core.config import get_settings

MSK_TZ = ZoneInfo("Europe/Moscow")
DEFAULT_OUTPUT_DIR = Path(".local/order-fulfillment-pilot")
DEFAULT_RECONCILE_GROUP = "нет в последнем вечернем остатке, РТУ была до отчета"
MONEY_TOLERANCE = Decimal("0.05")
PAYMENT_MATCH_TOLERANCE = Decimal("5.00")
PAYMENT_LOOKBACK_DAYS = 1
PAYMENT_LOOKAHEAD_DAYS = 3
MIN_REAL_PLANNED_PAYMENT_DATE = datetime(2020, 1, 1)


@dataclass(frozen=True)
class BalanceCheck:
    label: str
    action: str


@dataclass(frozen=True)
class PaymentCandidate:
    kind: str
    number: str
    document_at: datetime | None
    amount: Decimal
    counterparty_ref: bytes | None
    contract_ref: bytes | None
    organization_ref: bytes | None
    base_kind: str
    base_number: str
    base_site_order_number: str
    base_ref: bytes | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path, help="Pickup no-payment reconcile CSV.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for generated CSV/Markdown artifacts.",
    )
    parser.add_argument(
        "--reconcile-group",
        default=DEFAULT_RECONCILE_GROUP,
        help="Value of `сверка_с_остатком` to analyze.",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="1C balance upper bound in ISO format. Defaults to current Moscow time.",
    )
    return parser.parse_args()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"))
    raw = _clean(value).replace(" ", "").replace(",", ".")
    if not raw:
        return Decimal("0.00")
    try:
        return Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0.00")


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


def _date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return _clean(value)


def _bytes_hex(value: bytes | None) -> str:
    if not value:
        return ""
    return "0x" + value.hex()


def _same_ref(left: bytes | None, right: bytes | None) -> bool:
    return bool(left and right and left == right)


def _parse_as_of(raw: str | None) -> datetime:
    if raw:
        value = datetime.fromisoformat(raw)
        if value.tzinfo is not None:
            return value.astimezone(MSK_TZ).replace(tzinfo=None)
        return value
    return datetime.now(MSK_TZ).replace(tzinfo=None)


def _load_candidates(path: Path, reconcile_group: str) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    candidates = [
        row
        for row in rows
        if _clean(row.get("сверка_с_остатком")) == reconcile_group
        and _clean(row.get("группа")) == "нет привязанной оплаты"
    ]
    return candidates


def _fetch_contract_balances(order_numbers: list[str]) -> dict[str, dict[str, Any]]:
    if not order_numbers:
        return {}
    settings = get_settings()
    if not settings.onec_database_url:
        raise RuntimeError("ONEC_DATABASE_URL is not configured")

    params: dict[str, Any] = {}
    params.update({f"order_{index}": order for index, order in enumerate(order_numbers)})
    placeholders = ", ".join(f":order_{index}" for index in range(len(order_numbers)))

    base_statement = text(f"""
        SELECT
            NULLIF(LTRIM(RTRIM(o._Fld2425)), N'') AS site_order_number,
            NULLIF(LTRIM(RTRIM(o._Number)), N'') AS onec_order_number,
            o._IDRRef AS order_ref_raw,
            o._Date_Time AS order_date,
            o._Fld2405RRef AS order_counterparty_ref,
            o._Fld2401RRef AS order_contract_ref,
            NULLIF(LTRIM(RTRIM(counterparty._Code)), N'') AS counterparty_code,
            counterparty._Description AS counterparty_name,
            NULLIF(counterparty._Fld9516, CAST('1753-01-01' AS datetime))
                AS planned_payment_date,
            CAST(counterparty._Fld9865 AS int) AS credit_depth_days,
            CASE WHEN counterparty._Fld9866 = 0x01 THEN 1 ELSE 0 END AS shipment_ban,
            NULLIF(LTRIM(RTRIM(contract._Code)), N'') AS contract_code,
            contract._Description AS contract_name,
            master.dbo.fn_varbintohexstr(o._Fld2405RRef) AS counterparty_ref,
            master.dbo.fn_varbintohexstr(o._Fld2401RRef) AS contract_ref,
            sale._IDRRef AS sale_ref_raw,
            sale._Date_Time AS sale_date,
            NULLIF(LTRIM(RTRIM(sale._Number)), N'') AS sale_number,
            sale._Fld4932RRef AS organization_ref_raw,
            master.dbo.fn_varbintohexstr(sale._Fld4932RRef) AS organization_ref,
            organization._Description AS organization_name,
            CAST(sale._Fld4948 AS decimal(18, 2)) AS sale_amount
        FROM dbo._Document132 AS o WITH (NOLOCK)
        JOIN dbo._Document203 AS sale WITH (NOLOCK)
            ON sale._Fld4939_TYPE = 0x08
           AND sale._Fld4939_RTRef = 0x00000084
           AND sale._Fld4939_RRRef = o._IDRRef
        LEFT JOIN dbo._Reference54 AS counterparty WITH (NOLOCK)
            ON counterparty._IDRRef = o._Fld2405RRef
        LEFT JOIN dbo._Reference37 AS contract WITH (NOLOCK)
            ON contract._IDRRef = o._Fld2401RRef
        LEFT JOIN dbo._Reference66 AS organization WITH (NOLOCK)
            ON organization._IDRRef = sale._Fld4932RRef
        WHERE LTRIM(RTRIM(o._Fld2425)) IN ({placeholders})
          AND sale._Posted = 0x01
          AND sale._Marked <> 0x01
        ORDER BY
            o._Fld2425,
            sale._Date_Time
        """)

    current_totals_statement = text("""
        SELECT
            t._Fld7619RRef AS counterparty_ref,
            t._Fld7615RRef AS contract_ref,
            t._Fld7618RRef AS organization_ref,
            CAST(t._Fld7620 AS decimal(18, 2)) AS amount
        FROM dbo._AccumRgT7622 AS t WITH (NOLOCK)
        WHERE t._Period = (
            SELECT MAX(_Period)
            FROM dbo._AccumRgT7622 WITH (NOLOCK)
        )
        """)

    engine = create_engine(settings.onec_database_url, pool_pre_ping=True)
    with engine.connect() as connection:
        sale_rows = [
            dict(row)
            for row in connection.execute(base_statement, params).mappings()
        ]
        contract_balances: defaultdict[tuple[bytes, bytes, bytes], Decimal] = defaultdict(
            lambda: Decimal("0.00")
        )
        counterparty_balances: defaultdict[tuple[bytes, bytes], Decimal] = defaultdict(
            lambda: Decimal("0.00")
        )
        for balance_row in connection.execute(current_totals_statement).mappings():
            counterparty_ref = balance_row.get("counterparty_ref")
            contract_ref = balance_row.get("contract_ref")
            organization_ref = balance_row.get("organization_ref")
            amount = _decimal(balance_row.get("amount"))
            if counterparty_ref is None or organization_ref is None:
                continue
            counterparty_balances[(counterparty_ref, organization_ref)] += amount
            if contract_ref is not None:
                contract_balances[(counterparty_ref, contract_ref, organization_ref)] += amount

    base_rows: dict[str, dict[str, Any]] = {}
    for sale_row in sale_rows:
        order_number = _clean(sale_row.get("site_order_number"))
        if not order_number:
            continue
        sale_date = sale_row.get("sale_date")
        sale_amount = _decimal(sale_row.get("sale_amount"))
        row = base_rows.setdefault(
            order_number,
            {
                "site_order_number": order_number,
                "onec_order_number": sale_row.get("onec_order_number"),
                "order_ref_raw": sale_row.get("order_ref_raw"),
                "order_date": sale_row.get("order_date"),
                "order_counterparty_ref": sale_row.get("order_counterparty_ref"),
                "order_contract_ref": sale_row.get("order_contract_ref"),
                "counterparty_ref": sale_row.get("counterparty_ref"),
                "counterparty_code": sale_row.get("counterparty_code"),
                "counterparty_name": sale_row.get("counterparty_name"),
                "planned_payment_date": sale_row.get("planned_payment_date"),
                "credit_depth_days": sale_row.get("credit_depth_days"),
                "shipment_ban": sale_row.get("shipment_ban"),
                "contract_ref": sale_row.get("contract_ref"),
                "contract_code": sale_row.get("contract_code"),
                "contract_name": sale_row.get("contract_name"),
                "organization_ref": sale_row.get("organization_ref"),
                "organization_name": sale_row.get("organization_name"),
                "organization_ref_raw": sale_row.get("organization_ref_raw"),
                "sale_refs_raw": [],
                "sale_numbers": [],
                "posted_sale_count": 0,
                "first_sale_at": sale_date,
                "latest_sale_at": sale_date,
                "latest_sale_number": sale_row.get("sale_number"),
                "posted_sale_amount": Decimal("0.00"),
            },
        )
        row["posted_sale_count"] += 1
        row["posted_sale_amount"] += sale_amount
        row["sale_refs_raw"].append(sale_row.get("sale_ref_raw"))
        row["sale_numbers"].append(_clean(sale_row.get("sale_number")))
        if row["first_sale_at"] is None or (
            sale_date is not None and sale_date < row["first_sale_at"]
        ):
            row["first_sale_at"] = sale_date
        if row["latest_sale_at"] is None or (
            sale_date is not None and sale_date >= row["latest_sale_at"]
        ):
            row["latest_sale_at"] = sale_date
            row["latest_sale_number"] = sale_row.get("sale_number")
            row["organization_ref"] = sale_row.get("organization_ref")
            row["organization_name"] = sale_row.get("organization_name")
            row["organization_ref_raw"] = sale_row.get("organization_ref_raw")

    for item in base_rows.values():
        counterparty_ref = item.get("order_counterparty_ref")
        contract_ref = item.get("order_contract_ref")
        organization_ref = item.get("organization_ref_raw")
        if counterparty_ref is None or contract_ref is None or organization_ref is None:
            continue
        item["contract_balance_now"] = contract_balances[
            (counterparty_ref, contract_ref, organization_ref)
        ]
        item["counterparty_balance_now"] = counterparty_balances[
            (counterparty_ref, organization_ref)
        ]
    return base_rows


def _payment_window(row: dict[str, Any]) -> tuple[datetime, datetime] | None:
    first_sale_at = row.get("first_sale_at")
    latest_sale_at = row.get("latest_sale_at")
    if not isinstance(first_sale_at, datetime) or not isinstance(latest_sale_at, datetime):
        return None
    start = datetime.combine(first_sale_at.date(), datetime.min.time()) - timedelta(
        days=PAYMENT_LOOKBACK_DAYS
    )
    end = datetime.combine(latest_sale_at.date(), datetime.min.time()) + timedelta(
        days=PAYMENT_LOOKAHEAD_DAYS + 1
    )
    return start, end


def _fetch_nearby_payment_rows(
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[PaymentCandidate]:
    settings = get_settings()
    if not settings.onec_database_url:
        raise RuntimeError("ONEC_DATABASE_URL is not configured")

    pko_statement = text("""
        SELECT
            N'ПКО' AS kind,
            NULLIF(LTRIM(RTRIM(pko._Number)), N'') AS document_number,
            pko._Date_Time AS document_at,
            pko._Fld4684_RRRef AS counterparty_ref,
            vt._Fld4710RRef AS contract_ref,
            pko._Fld4680RRef AS organization_ref,
            CAST(COALESCE(vt._Fld4713, pko._Fld4688) AS decimal(18, 2)) AS amount,
            CASE
                WHEN pko._Fld4697_RTRef = 0x00000084 THEN N'заказ'
                WHEN pko._Fld4697_RTRef = 0x000000CB THEN N'РТУ'
                WHEN pko._Fld4697_RTRef IS NULL THEN N''
                ELSE master.dbo.fn_varbintohexstr(pko._Fld4697_RTRef)
            END AS base_kind,
            COALESCE(
                NULLIF(LTRIM(RTRIM(base_order._Fld2425)), N''),
                NULLIF(LTRIM(RTRIM(base_order._Number)), N''),
                NULLIF(LTRIM(RTRIM(base_sale._Number)), N'')
            ) AS base_number,
            NULLIF(LTRIM(RTRIM(base_order._Fld2425)), N'') AS base_site_order_number,
            pko._Fld4697_RRRef AS base_ref
        FROM dbo._Document196 AS pko WITH (NOLOCK)
        LEFT JOIN dbo._Document196_VT4708 AS vt WITH (NOLOCK)
            ON vt._Document196_IDRRef = pko._IDRRef
        LEFT JOIN dbo._Document132 AS base_order WITH (NOLOCK)
            ON pko._Fld4697_RTRef = 0x00000084
           AND base_order._IDRRef = pko._Fld4697_RRRef
        LEFT JOIN dbo._Document203 AS base_sale WITH (NOLOCK)
            ON pko._Fld4697_RTRef = 0x000000CB
           AND base_sale._IDRRef = pko._Fld4697_RRRef
        WHERE pko._Posted = 0x01
          AND pko._Marked <> 0x01
          AND pko._Fld4684_RTRef = 0x00000036
          AND pko._Fld4684_RRRef <> 0x00000000000000000000000000000000
          AND pko._Date_Time >= :window_start
          AND pko._Date_Time < :window_end
        """)

    register_statement = text("""
        WITH movement_summary AS (
            SELECT
                r._RecorderTRef AS recorder_tref,
                r._RecorderRRef AS recorder_ref,
                r._Fld7619RRef AS counterparty_ref,
                r._Fld7615RRef AS contract_ref,
                r._Fld7618RRef AS organization_ref,
                MAX(r._Period) AS movement_at,
                CAST(
                    SUM(
                        CASE
                            WHEN r._RecordKind = 0 THEN r._Fld7620
                            ELSE -r._Fld7620
                        END
                    ) AS decimal(18, 2)
                ) AS signed_amount
            FROM dbo._AccumRg7614 AS r WITH (NOLOCK)
            WHERE r._Active = 0x01
              AND r._RecorderTRef IN (0x000000A9, 0x000000BA)
              AND r._Fld7619RRef <> 0x00000000000000000000000000000000
              AND r._Period >= :window_start
              AND r._Period < :window_end
            GROUP BY
                r._RecorderTRef,
                r._RecorderRRef,
                r._Fld7619RRef,
                r._Fld7615RRef,
                r._Fld7618RRef
            HAVING SUM(
                CASE
                    WHEN r._RecordKind = 0 THEN r._Fld7620
                    ELSE -r._Fld7620
                END
            ) < 0
        )
        SELECT
            CASE
                WHEN m.recorder_tref = 0x000000A9 THEN N'эквайринг/банк'
                WHEN m.recorder_tref = 0x000000BA THEN N'банк'
                ELSE N'оплата по регистру'
            END AS kind,
            COALESCE(
                NULLIF(LTRIM(RTRIM(doc169._Number)), N''),
                NULLIF(LTRIM(RTRIM(doc186._Number)), N'')
            ) AS document_number,
            COALESCE(doc169._Date_Time, doc186._Date_Time, m.movement_at) AS document_at,
            m.counterparty_ref,
            m.contract_ref,
            m.organization_ref,
            CAST(-m.signed_amount AS decimal(18, 2)) AS amount,
            N'' AS base_kind,
            N'' AS base_number,
            N'' AS base_site_order_number,
            NULL AS base_ref
        FROM movement_summary AS m
        LEFT JOIN dbo._Document169 AS doc169 WITH (NOLOCK)
            ON m.recorder_tref = 0x000000A9
           AND doc169._IDRRef = m.recorder_ref
        LEFT JOIN dbo._Document186 AS doc186 WITH (NOLOCK)
            ON m.recorder_tref = 0x000000BA
           AND doc186._IDRRef = m.recorder_ref
        WHERE (doc169._IDRRef IS NULL OR (doc169._Posted = 0x01 AND doc169._Marked <> 0x01))
          AND (doc186._IDRRef IS NULL OR (doc186._Posted = 0x01 AND doc186._Marked <> 0x01))
        """)

    engine = create_engine(settings.onec_database_url, pool_pre_ping=True)
    rows: list[PaymentCandidate] = []
    with engine.connect() as connection:
        params = {"window_start": window_start, "window_end": window_end}
        for statement in (pko_statement, register_statement):
            source = list(connection.execute(statement, params).mappings())
            for item in source:
                rows.append(
                    PaymentCandidate(
                        kind=_clean(item.get("kind")),
                        number=_clean(item.get("document_number")),
                        document_at=item.get("document_at"),
                        amount=_decimal(item.get("amount")),
                        counterparty_ref=item.get("counterparty_ref"),
                        contract_ref=item.get("contract_ref"),
                        organization_ref=item.get("organization_ref"),
                        base_kind=_clean(item.get("base_kind")),
                        base_number=_clean(item.get("base_number")),
                        base_site_order_number=_clean(
                            item.get("base_site_order_number")
                        ),
                        base_ref=item.get("base_ref"),
                    )
                )
    return rows


def _payment_relation(row: dict[str, Any], payment: PaymentCandidate) -> str:
    order_ref = row.get("order_ref_raw")
    sale_refs = {ref for ref in row.get("sale_refs_raw", []) if ref}
    if _same_ref(payment.base_ref, order_ref):
        return "уже привязана к заказу"
    if payment.base_ref in sale_refs:
        return "уже привязана к РТУ заказа"
    if payment.base_kind and payment.base_number:
        return f"основание: {payment.base_kind} {payment.base_number}"
    if payment.base_kind:
        return f"основание: {payment.base_kind}"
    return "основание не указано"


def _payment_contract_relation(row: dict[str, Any], payment: PaymentCandidate) -> str:
    order_contract_ref = row.get("order_contract_ref")
    if _same_ref(payment.contract_ref, order_contract_ref):
        return "тот же договор"
    if not payment.contract_ref:
        return "договор не указан"
    return "другой договор"


def _payment_amount_gap(
    row: dict[str, Any],
    payment: PaymentCandidate,
    report_debt: Decimal,
) -> Decimal:
    reference_amounts = [
        _decimal(row.get("posted_sale_amount")),
        report_debt,
        _decimal(row.get("contract_balance_now")),
    ]
    positive_reference_amounts = [
        amount for amount in reference_amounts if amount > MONEY_TOLERANCE
    ]
    if not positive_reference_amounts:
        return Decimal("999999999.00")
    return min(abs(payment.amount - amount) for amount in positive_reference_amounts)


def _payment_sort_key(
    row: dict[str, Any],
    payment: PaymentCandidate,
    report_debt: Decimal,
) -> tuple[int, Decimal, int, str]:
    contract_rank = {
        "тот же договор": 0,
        "договор не указан": 1,
        "другой договор": 2,
    }[_payment_contract_relation(row, payment)]
    sale_at = row.get("latest_sale_at")
    date_gap = 999999
    if isinstance(sale_at, datetime) and isinstance(payment.document_at, datetime):
        date_gap = abs((payment.document_at - sale_at).days)
    return (
        contract_rank,
        _payment_amount_gap(row, payment, report_debt),
        date_gap,
        payment.number,
    )


def _find_nearby_payments(
    onec_rows: dict[str, dict[str, Any]],
) -> dict[str, list[PaymentCandidate]]:
    windows = {
        order_number: window
        for order_number, row in onec_rows.items()
        if (window := _payment_window(row)) is not None
    }
    if not windows:
        return {}

    global_start = min(window[0] for window in windows.values())
    global_end = max(window[1] for window in windows.values())
    payment_rows = _fetch_nearby_payment_rows(
        window_start=global_start,
        window_end=global_end,
    )

    result: dict[str, list[PaymentCandidate]] = {}
    for order_number, row in onec_rows.items():
        window = windows.get(order_number)
        if window is None:
            continue
        start, end = window
        counterparty_ref = row.get("order_counterparty_ref")
        organization_ref = row.get("organization_ref_raw")
        matches: list[PaymentCandidate] = []
        for payment in payment_rows:
            if not _same_ref(payment.counterparty_ref, counterparty_ref):
                continue
            if organization_ref and payment.organization_ref:
                if not _same_ref(payment.organization_ref, organization_ref):
                    continue
            if isinstance(payment.document_at, datetime):
                if payment.document_at < start or payment.document_at >= end:
                    continue
            matches.append(payment)
        report_debt = _decimal(row.get("contract_balance_now"))
        matches.sort(key=lambda payment: _payment_sort_key(row, payment, report_debt))
        result[order_number] = matches[:5]
    return result


def _format_payment_candidates(
    row: dict[str, Any],
    payments: list[PaymentCandidate],
    report_debt: Decimal,
) -> str:
    parts: list[str] = []
    for payment in payments[:3]:
        amount_gap = _payment_amount_gap(row, payment, report_debt)
        parts.append(
            "; ".join(
                [
                    f"{payment.kind} {payment.number or '-'}",
                    _date(payment.document_at),
                    f"{_money(payment.amount)} руб.",
                    _payment_contract_relation(row, payment),
                    _payment_relation(row, payment),
                    f"разница {_money(amount_gap)}",
                ]
            )
        )
    return " | ".join(parts)


def _has_strong_payment_candidate(
    row: dict[str, Any],
    payments: list[PaymentCandidate],
    report_debt: Decimal,
) -> bool:
    for payment in payments:
        if _payment_contract_relation(row, payment) != "тот же договор":
            continue
        if _payment_relation(row, payment).startswith("уже привязана"):
            continue
        if payment.base_kind and payment.base_number:
            continue
        if _payment_amount_gap(row, payment, report_debt) <= PAYMENT_MATCH_TOLERANCE:
            return True
    return False


def _has_similar_payment_candidate(
    row: dict[str, Any],
    payments: list[PaymentCandidate],
    report_debt: Decimal,
) -> bool:
    for payment in payments:
        if _payment_contract_relation(row, payment) != "тот же договор":
            continue
        relation = _payment_relation(row, payment)
        if payment.base_kind and not relation.startswith("уже привязана"):
            continue
        if _payment_amount_gap(row, payment, report_debt) <= PAYMENT_MATCH_TOLERANCE:
            return True
    return False


def _credit_hint(row: dict[str, Any]) -> str:
    parts: list[str] = []
    planned_payment_date = row.get("planned_payment_date")
    credit_depth_days = int(_decimal(row.get("credit_depth_days")))
    shipment_ban = _clean(row.get("shipment_ban"))
    counterparty_name = _clean(row.get("counterparty_name")).lower()
    contract_name = _clean(row.get("contract_name")).lower()
    if (
        isinstance(planned_payment_date, datetime)
        and planned_payment_date >= MIN_REAL_PLANNED_PAYMENT_DATE
    ):
        parts.append(f"план оплаты: {planned_payment_date:%Y-%m-%d}")
    if credit_depth_days > 0:
        parts.append(f"отсрочка {credit_depth_days} дн.")
    if shipment_ban == "1":
        parts.append("запрет отгрузки")
    if any(
        marker in f"{counterparty_name} {contract_name}"
        for marker in ("без оплаты", "по запросу", "отсроч", "в долг")
    ):
        parts.append("в названии есть признак выдачи без оплаты/по запросу")
    return "; ".join(parts)


def _has_credit_hint(row: dict[str, Any]) -> bool:
    hint = _credit_hint(row)
    return bool(hint) and "запрет отгрузки" not in hint


def _refine_action(
    *,
    row: dict[str, Any],
    check: BalanceCheck,
    report_debt: Decimal,
    payments: list[PaymentCandidate],
) -> tuple[str, str]:
    if check.label != "по договору сейчас есть долг":
        return check.label, check.action

    sale_amount = _decimal(row.get("posted_sale_amount"))
    contract_now = _decimal(row.get("contract_balance_now"))
    if _has_strong_payment_candidate(row, payments, report_debt):
        return (
            "похоже на непривязанную оплату",
            "проверить найденную оплату рядом с РТУ и привязать ее к заказу/РТУ",
        )
    if _has_similar_payment_candidate(row, payments, report_debt):
        return (
            "есть похожая оплата рядом",
            "проверить, относится ли найденная оплата к этому заказу; если да, исправить привязку",
        )
    if _has_credit_hint(row):
        return (
            "возможная продажа в долг/отсрочка",
            "подтвердить договоренность по отсрочке; операционное закрытие отдельно от контроля дебиторки",
        )
    if sale_amount > MONEY_TOLERANCE:
        if abs(contract_now - sale_amount) <= PAYMENT_MATCH_TOLERANCE:
            return (
                "похоже на неоплаченный заказ",
                "не закрывать как оплаченный; получить оплату или оформить подтвержденный долг",
            )
        if contract_now < sale_amount:
            return (
                "остаточный долг после частичной оплаты/зачета",
                "проверить недостающую сумму, скидку, взаимозачет или непривязанную часть оплаты",
            )
        return (
            "общий долг клиента больше этого заказа",
            "разделить старый долг и текущий заказ; проверить условия клиента и оплаты рядом с РТУ",
        )
    return check.label, check.action


def _classify(row: dict[str, Any], report_debt: Decimal) -> BalanceCheck:
    contract_now = _decimal(row.get("contract_balance_now"))
    counterparty_now = _decimal(row.get("counterparty_balance_now"))

    if not _clean(row.get("first_sale_at")):
        return BalanceCheck(
            "не удалось проверить РТУ в 1С",
            "проверить заказ вручную: не найдена проведенная РТУ в текущем запросе",
        )
    if abs(contract_now) <= MONEY_TOLERANCE:
        return BalanceCheck(
            "долга по договору сейчас нет",
            "можно закрывать операционно после подтверждения выдачи; оплату/зачет 1С уже закрыл по договору",
        )
    if contract_now < -MONEY_TOLERANCE:
        return BalanceCheck(
            "по договору переплата",
            "можно закрывать операционно после подтверждения выдачи; проверить корректность зачета переплаты",
        )
    if contract_now > MONEY_TOLERANCE and counterparty_now <= MONEY_TOLERANCE:
        return BalanceCheck(
            "по договору долг, по контрагенту в целом долга нет",
            "проверить договор платежа/зачет: деньги могут быть на другом договоре этого клиента",
        )
    if report_debt > 0:
        return BalanceCheck(
            "по договору сейчас есть долг",
            "реальный долг вероятен: если клиент платил, привязать ПКО/эквайринг; если не платил, не закрывать как оплаченный",
        )
    return BalanceCheck(
        "требует ручной проверки",
        "проверить движения по договору и контрагенту в 1С",
    )


def _build_report_rows(
    candidates: list[dict[str, str]],
    onec_rows: dict[str, dict[str, Any]],
    payment_candidates: dict[str, list[PaymentCandidate]],
) -> list[dict[str, str]]:
    report_rows: list[dict[str, str]] = []
    for source in candidates:
        order_number = _clean(source.get("заказ"))
        onec = onec_rows.get(order_number, {})
        report_debt = _decimal(source.get("долг"))
        check = _classify(onec, report_debt) if onec else BalanceCheck(
            "не найден заказ в 1С",
            "проверить номер заказа и дубль вручную",
        )
        payments = payment_candidates.get(order_number, [])
        refined_label, refined_action = _refine_action(
            row=onec,
            check=check,
            report_debt=report_debt,
            payments=payments,
        )

        contract_now = _decimal(onec.get("contract_balance_now"))
        counterparty_now = _decimal(onec.get("counterparty_balance_now"))
        credit_hint = _credit_hint(onec)
        row = {
            "заказ": order_number,
            "сделка": _clean(source.get("сделка")),
            "дата_рту_из_отчета": _clean(source.get("дата_последней_рту")),
            "номер_рту_из_отчета": _clean(source.get("номер_последней_рту")),
            "сумма_рту_из_отчета": _money(_decimal(source.get("сумма_рту"))),
            "долг_по_заказу_из_отчета": _money(report_debt),
            "контрагент_код": _clean(onec.get("counterparty_code")),
            "контрагент": _clean(onec.get("counterparty_name")),
            "договор_код": _clean(onec.get("contract_code")),
            "договор": _clean(onec.get("contract_name")),
            "организация": _clean(onec.get("organization_name")),
            "первая_рту_1с": _date(onec.get("first_sale_at")),
            "последняя_рту_1с": _date(onec.get("latest_sale_at")),
            "количество_рту_1с": _clean(onec.get("posted_sale_count")),
            "сумма_рту_1с": _money(_decimal(onec.get("posted_sale_amount"))),
            "баланс_договора_сейчас": _money(contract_now),
            "баланс_контрагента_сейчас": _money(counterparty_now),
            "сверка_договора": check.label,
            "уточнение": refined_label,
            "найденные_оплаты_рядом": _format_payment_candidates(
                onec,
                payments,
                report_debt,
            ),
            "признаки_отсрочки": credit_hint,
            "контрагент_ref": _bytes_hex(onec.get("order_counterparty_ref")),
            "договор_ref": _bytes_hex(onec.get("order_contract_ref")),
            "действие": refined_action,
        }
        report_rows.append(row)
    return sorted(
        report_rows,
        key=lambda item: (
            item["уточнение"],
            -_decimal(item["долг_по_заказу_из_отчета"]),
            item["заказ"],
        ),
    )


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _md_cell(value: str) -> str:
    return (value or "-").replace("|", "<br>").replace("\n", " ")


def _append_table(
    lines: list[str],
    title: str,
    rows: list[dict[str, str]],
    *,
    limit: int = 20,
) -> None:
    if not rows:
        return
    lines.append(f"## {title}")
    lines.append("")
    lines.append(
        "| Заказ | Сделка | РТУ | Долг заказа | Договор сейчас | Оплаты рядом | Признаки отсрочки | Действие |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: | --- | --- | --- |")
    for row in rows[:limit]:
        payments = row["найденные_оплаты_рядом"] or "-"
        if len(payments) > 180:
            payments = payments[:177] + "..."
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_cell(row["заказ"]),
                    _md_cell(row["сделка"]),
                    _md_cell(row["сумма_рту_1с"]),
                    _md_cell(row["долг_по_заказу_из_отчета"]),
                    _md_cell(row["баланс_договора_сейчас"]),
                    _md_cell(payments),
                    _md_cell(row["признаки_отсрочки"]),
                    _md_cell(row["действие"]),
                ]
            )
            + " |"
        )
    if len(rows) > limit:
        lines.append("")
        lines.append(f"Показано {limit} из {len(rows)}. Полный список см. в CSV.")
    lines.append("")


def _write_markdown(
    path: Path,
    *,
    rows: list[dict[str, str]],
    input_csv: Path,
    as_of: datetime,
) -> None:
    counts = Counter(row["уточнение"] for row in rows)
    sums: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for row in rows:
        sums[row["уточнение"]] += _decimal(row["долг_по_заказу_из_отчета"])

    lines = [
        "# Сверка самовывозов по договору/контрагенту и оплатам в 1С",
        "",
        f"Источник: `{input_csv}`",
        f"Баланс рассчитан на: **{as_of:%Y-%m-%d %H:%M:%S}**",
        "Фактический остаток взят из текущих итогов регистра взаиморасчетов 1С.",
        (
            "Оплаты рядом ищутся по тому же контрагенту в окне: день до РТУ, "
            "день РТУ и 3 дня после."
        ),
        "",
        "Проверка read-only: сделки и документы не изменялись.",
        "",
        "## Группы",
        "",
    ]
    for label, count in counts.most_common():
        lines.append(f"- {label}: {count} заказов, долг по заказам {_money(sums[label])} руб.")
    lines.append("")

    preferred_order = [
        "похоже на непривязанную оплату",
        "есть похожая оплата рядом",
        "возможная продажа в долг/отсрочка",
        "похоже на неоплаченный заказ",
        "остаточный долг после частичной оплаты/зачета",
        "общий долг клиента больше этого заказа",
        "долга по договору сейчас нет",
        "по договору переплата",
        "по договору долг, по контрагенту в целом долга нет",
        "по договору сейчас есть долг",
        "не удалось проверить РТУ в 1С",
        "не найден заказ в 1С",
        "требует ручной проверки",
    ]
    for label in preferred_order:
        group_rows = [row for row in rows if row["уточнение"] == label]
        group_rows.sort(
            key=lambda item: _decimal(item["долг_по_заказу_из_отчета"]),
            reverse=True,
        )
        _append_table(lines, label, group_rows)

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    as_of = _parse_as_of(args.as_of)
    candidates = _load_candidates(args.input_csv, args.reconcile_group)
    order_numbers = [_clean(row.get("заказ")) for row in candidates if _clean(row.get("заказ"))]
    onec_rows = _fetch_contract_balances(order_numbers)
    payment_candidates = _find_nearby_payments(onec_rows)
    report_rows = _build_report_rows(candidates, onec_rows, payment_candidates)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = as_of.strftime("%Y%m%d-%H%M%S")
    csv_path = args.output_dir / f"pickup-contract-settlement-check-{stamp}.csv"
    md_path = args.output_dir / f"pickup-contract-settlement-check-{stamp}.md"
    _write_csv(csv_path, report_rows)
    _write_markdown(
        md_path,
        rows=report_rows,
        input_csv=args.input_csv,
        as_of=as_of,
    )
    print(f"Rows: {len(report_rows)}")
    print(f"CSV: {csv_path}")
    print(f"Markdown: {md_path}")


if __name__ == "__main__":
    main()
