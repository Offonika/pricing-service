from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text

CARD_CASHBOX_RE = re.compile(
    r"^\s*(?P<last4>\d{4})\s+(?P<store>.*?)\s+карта\s+(?P<employee>.+?)\s*$",
    re.IGNORECASE,
)
CARD_CASHBOX_NO_STORE_RE = re.compile(
    r"^\s*(?P<last4>\d{4})\s+карта\s+(?P<employee>.+?)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedCashboxName:
    card_last4: str | None
    store_name: str | None
    employee_last_name: str | None
    needs_manual_review: bool
    review_reason: str | None


def clean_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def parse_cashbox_name(value: str | None) -> ParsedCashboxName:
    name = clean_string(value)
    if not name:
        return ParsedCashboxName(None, None, None, True, "empty_cashbox_name")

    match = CARD_CASHBOX_RE.match(name)
    if not match:
        no_store_match = CARD_CASHBOX_NO_STORE_RE.match(name)
        if no_store_match:
            return ParsedCashboxName(
                card_last4=no_store_match.group("last4").strip(),
                store_name=None,
                employee_last_name=no_store_match.group("employee").strip() or None,
                needs_manual_review=False,
                review_reason=None,
            )
    if not match:
        reasons: list[str] = []
        if not re.match(r"^\s*\d{4}\b", name):
            reasons.append("missing_leading_last4")
        if "карта" not in name.lower():
            reasons.append("missing_card_keyword")
        return ParsedCashboxName(
            None,
            None,
            None,
            True,
            ",".join(reasons) or "unparsed_cashbox_name",
        )

    return ParsedCashboxName(
        card_last4=match.group("last4").strip(),
        store_name=match.group("store").strip() or None,
        employee_last_name=match.group("employee").strip() or None,
        needs_manual_review=False,
        review_reason=None,
    )


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    normalized = str(value).strip().replace(" ", "").replace(",", ".")
    if not normalized:
        return None
    return Decimal(normalized)


def calculate_closing_balance(
    *,
    opening_balance: Any = None,
    inflow_amount: Any = None,
    outflow_amount: Any = None,
) -> Decimal:
    return (
        (decimal_or_none(opening_balance) or Decimal("0"))
        + (decimal_or_none(inflow_amount) or Decimal("0"))
        - (decimal_or_none(outflow_amount) or Decimal("0"))
    ).quantize(Decimal("0.01"))


def normalize_cashbox_registry_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_last4: dict[str, int] = {}
    seen_last4_employee: dict[tuple[str, str], int] = {}
    normalized_rows: list[dict[str, Any]] = []

    for raw in rows:
        row = dict(raw)
        name = clean_string(row.get("onec_cashbox_name") or row.get("cashbox_name"))
        parsed = parse_cashbox_name(name)
        item = {
            "onec_cashbox_ref_hex": clean_string(row.get("onec_cashbox_ref_hex")),
            "onec_cashbox_code": clean_string(
                row.get("onec_cashbox_code") or row.get("cashbox_code")
            ),
            "onec_cashbox_name": name,
            "currency_code": clean_string(row.get("currency_code")),
            "currency_name": clean_string(row.get("currency_name")),
            "card_last4": parsed.card_last4,
            "store_name": parsed.store_name,
            "employee_last_name": parsed.employee_last_name,
            "is_active": bool(row.get("is_active", True)),
            "needs_manual_review": parsed.needs_manual_review,
            "review_reason": parsed.review_reason,
            "payload": row,
        }
        if item["card_last4"]:
            seen_last4[item["card_last4"]] = seen_last4.get(item["card_last4"], 0) + 1
        employee_key = _employee_key(item["employee_last_name"])
        if item["card_last4"] and employee_key:
            key = (item["card_last4"], employee_key)
            seen_last4_employee[key] = seen_last4_employee.get(key, 0) + 1
        normalized_rows.append(item)

    for item in normalized_rows:
        if not item["onec_cashbox_code"] or not item["onec_cashbox_name"]:
            item["needs_manual_review"] = True
            item["review_reason"] = _append_reason(
                item["review_reason"], "missing_required_onec_fields"
            )
        employee_key = _employee_key(item["employee_last_name"])
        if (
            item["card_last4"]
            and employee_key
            and seen_last4_employee[(item["card_last4"], employee_key)] > 1
        ):
            item["needs_manual_review"] = True
            item["review_reason"] = _append_reason(
                item["review_reason"], "duplicate_last4_employee"
            )
        if item["card_last4"] and not employee_key and seen_last4[item["card_last4"]] > 1:
            item["needs_manual_review"] = True
            item["review_reason"] = _append_reason(
                item["review_reason"], "duplicate_last4_without_employee"
            )
        result.append(item)
    return result


def _employee_key(value: str | None) -> str | None:
    normalized = clean_string(value)
    if normalized and " " in normalized:
        normalized = normalized.split()[-1]
    return normalized.lower().replace("ё", "е") if normalized else None


def _append_reason(current: str | None, reason: str) -> str:
    if not current:
        return reason
    parts = [item for item in current.split(",") if item]
    if reason not in parts:
        parts.append(reason)
    return ",".join(parts)


class OneCCardBalanceExtractor:
    def __init__(self, onec_engine):
        self.onec_engine = onec_engine

    def fetch_cashbox_registry(self) -> list[dict[str, Any]]:
        sql = text("""
            SELECT
                master.dbo.fn_varbintohexstr(c._IDRRef) AS onec_cashbox_ref_hex,
                RTRIM(c._Code) AS onec_cashbox_code,
                c._Description AS onec_cashbox_name,
                RTRIM(cur._Code) AS currency_code,
                cur._Description AS currency_name,
                CASE WHEN c._Marked = 0x00 THEN 1 ELSE 0 END AS is_active
            FROM dbo._Reference45 c WITH (NOLOCK)
            LEFT JOIN dbo._Reference20 cur WITH (NOLOCK) ON cur._IDRRef = c._Fld564RRef
            WHERE c._Marked = 0x00
              AND LOWER(c._Description) LIKE N'%карта%'
            ORDER BY c._Description
            """)
        with self.onec_engine.connect() as conn:
            rows = conn.execute(sql).mappings().all()
        return normalize_cashbox_registry_rows(rows)

    def fetch_balance_by_cashbox_codes(
        self,
        *,
        business_date: date,
        cashbox_codes: Iterable[str] | None = None,
    ) -> dict[str, Decimal]:
        month_start = business_date.replace(day=1)
        period_from = datetime.combine(month_start, time.min)
        period_to = datetime.combine(business_date + timedelta(days=1), time.min)
        params: dict[str, Any] = {
            "period_from": period_from,
            "period_to": period_to,
        }
        code_filter = ""
        codes = [code for code in (cashbox_codes or []) if code]
        if codes:
            bind_names: list[str] = []
            for index, code in enumerate(codes):
                key = f"code_{index}"
                params[key] = code
                bind_names.append(f":{key}")
            code_filter = f"AND RTRIM(c._Code) IN ({', '.join(bind_names)})"

        sql = text(f"""
            WITH card_cashboxes AS (
                SELECT
                    c._IDRRef AS cashbox_ref,
                    RTRIM(c._Code) AS onec_cashbox_code
                FROM dbo._Reference45 c WITH (NOLOCK)
                WHERE c._Marked = 0x00
                  AND LOWER(c._Description) LIKE N'%карта%'
                  {code_filter}
            ), opening AS (
                SELECT
                    t._Fld7067_RRRef AS cashbox_ref,
                    SUM(CAST(t._Fld7069 AS decimal(18,2))) AS opening_balance
                FROM dbo._AccumRgT7071 t WITH (NOLOCK)
                WHERE t._Period = :period_from
                GROUP BY t._Fld7067_RRRef
            ), movements AS (
                SELECT
                    r._Fld7067_RRRef AS cashbox_ref,
                    SUM(CASE WHEN r._RecordKind = 0.0 THEN CAST(r._Fld7069 AS decimal(18,2)) ELSE 0 END) AS inflow_amount,
                    SUM(CASE WHEN r._RecordKind = 1.0 THEN CAST(r._Fld7069 AS decimal(18,2)) ELSE 0 END) AS outflow_amount
                FROM dbo._AccumRg7065 r WITH (NOLOCK)
                WHERE r._Active = 0x01
                  AND r._Period >= :period_from
                  AND r._Period < :period_to
                GROUP BY r._Fld7067_RRRef
            )
            SELECT
                cc.onec_cashbox_code,
                COALESCE(o.opening_balance, 0) AS opening_balance,
                COALESCE(m.inflow_amount, 0) AS inflow_amount,
                COALESCE(m.outflow_amount, 0) AS outflow_amount
            FROM card_cashboxes cc
            LEFT JOIN opening o ON o.cashbox_ref = cc.cashbox_ref
            LEFT JOIN movements m ON m.cashbox_ref = cc.cashbox_ref
            """)
        with self.onec_engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
        return {
            str(row["onec_cashbox_code"]): calculate_closing_balance(
                opening_balance=row.get("opening_balance"),
                inflow_amount=row.get("inflow_amount"),
                outflow_amount=row.get("outflow_amount"),
            )
            for row in rows
        }
