from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

MSK_TZ = ZoneInfo("Europe/Moscow")

CATEGORY_LABELS = {
    "bank_accounts": "счета",
    "cashboxes": "кассы",
    "cards": "карты/эквайринг",
    "other": "прочее",
}


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value).strip().replace(" ", "").replace(",", ".") or "0")


def _money(value: Any) -> Decimal:
    return _to_decimal(value).quantize(Decimal("0.01"))


def _as_msk_naive(value: datetime | None) -> tuple[datetime, str]:
    current = value or datetime.now(MSK_TZ)
    if current.tzinfo is None:
        current_msk = current.replace(tzinfo=MSK_TZ)
    else:
        current_msk = current.astimezone(MSK_TZ)
    return current_msk.replace(tzinfo=None), current_msk.isoformat()


def _default_period_start(as_of_naive: datetime) -> date:
    return as_of_naive.date().replace(day=1)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _chain_for(row: dict[str, Any], rows_by_ref: dict[str, dict[str, Any]]) -> list[str]:
    chain = [_clean(row.get("name"))]
    parent_ref = _clean(row.get("parent_ref")).lower()
    seen: set[str] = set()
    while parent_ref and parent_ref not in seen:
        seen.add(parent_ref)
        parent = rows_by_ref.get(parent_ref)
        if parent is None:
            break
        chain.append(_clean(parent.get("name")))
        parent_ref = _clean(parent.get("parent_ref")).lower()
    return [item for item in chain if item]


def classify_money_place(name: str, chain: list[str]) -> str:
    normalized_name = name.lower().replace("ё", "е")
    normalized_chain = " / ".join(chain).lower().replace("ё", "е")
    if (
        "банковские счета" in normalized_chain
        or "cберсчета" in normalized_chain
        or "сберсчета" in normalized_chain
        or "сберегательный счет" in normalized_name
    ):
        return "bank_accounts"
    if (
        "карта" in normalized_name
        or "карты" in normalized_chain
        or "kарты" in normalized_chain
        or "cloudpayments" in normalized_name
        or "яндекс касса" in normalized_name
    ):
        return "cards"
    if "касса" in normalized_name or "сейф" in normalized_name:
        return "cashboxes"
    return "other"


def build_finance_cash_position(
    onec_engine,
    *,
    as_of: datetime | None = None,
    period_start: date | None = None,
    include_zero: bool = False,
    top_limit: int = 15,
) -> dict[str, Any]:
    as_of_naive, as_of_msk = _as_msk_naive(as_of)
    resolved_period_start = period_start or _default_period_start(as_of_naive)
    period_start_dt = datetime.combine(resolved_period_start, time.min)

    params = {
        "period_start": period_start_dt,
        "as_of": as_of_naive,
    }
    sql = text("""
        WITH money_places AS (
            SELECT
                c._IDRRef AS money_place_ref,
                master.dbo.fn_varbintohexstr(c._IDRRef) AS money_place_ref_hex,
                RTRIM(c._Code) AS money_place_code,
                c._Description AS money_place_name,
                master.dbo.fn_varbintohexstr(c._ParentIDRRef) AS parent_ref_hex,
                c._ParentIDRRef AS parent_ref,
                c._Folder AS folder_flag,
                RTRIM(currency._Code) AS currency_code,
                currency._Description AS currency_name
            FROM dbo._Reference45 AS c WITH (NOLOCK)
            LEFT JOIN dbo._Reference20 AS currency WITH (NOLOCK)
                ON currency._IDRRef = c._Fld564RRef
            WHERE c._Marked = 0x00
        ),
        opening AS (
            SELECT
                t._Fld7067_RRRef AS money_place_ref,
                CAST(SUM(CAST(t._Fld7069 AS decimal(18, 2))) AS decimal(18, 2)) AS opening_balance
            FROM dbo._AccumRgT7071 AS t WITH (NOLOCK)
            WHERE t._Period = :period_start
            GROUP BY t._Fld7067_RRRef
        ),
        movements AS (
            SELECT
                r._Fld7067_RRRef AS money_place_ref,
                CAST(SUM(CASE WHEN r._RecordKind = 0 THEN CAST(r._Fld7069 AS decimal(18, 2)) ELSE 0 END) AS decimal(18, 2)) AS inflow_amount,
                CAST(SUM(CASE WHEN r._RecordKind = 1 THEN CAST(r._Fld7069 AS decimal(18, 2)) ELSE 0 END) AS decimal(18, 2)) AS outflow_amount,
                COUNT(*) AS movement_count,
                MAX(r._Period) AS last_movement_at
            FROM dbo._AccumRg7065 AS r WITH (NOLOCK)
            WHERE r._Active = 0x01
              AND r._Period >= :period_start
              AND r._Period < :as_of
            GROUP BY r._Fld7067_RRRef
        )
        SELECT
            p.money_place_ref_hex,
            p.money_place_code,
            p.money_place_name,
            p.parent_ref_hex,
            master.dbo.fn_varbintohexstr(p.folder_flag) AS folder_flag_hex,
            p.currency_code,
            p.currency_name,
            COALESCE(o.opening_balance, 0) AS opening_balance,
            COALESCE(m.inflow_amount, 0) AS inflow_amount,
            COALESCE(m.outflow_amount, 0) AS outflow_amount,
            COALESCE(m.movement_count, 0) AS movement_count,
            m.last_movement_at
        FROM money_places AS p
        LEFT JOIN opening AS o ON o.money_place_ref = p.money_place_ref
        LEFT JOIN movements AS m ON m.money_place_ref = p.money_place_ref
        """)

    with onec_engine.connect() as conn:
        raw_rows = [dict(row) for row in conn.execute(sql, params).mappings().all()]

    rows_by_ref = {_clean(row.get("money_place_ref_hex")).lower(): row for row in raw_rows}
    detail_rows: list[dict[str, Any]] = []
    summary: dict[tuple[str, str | None, str | None], dict[str, Any]] = defaultdict(
        lambda: {
            "category": "",
            "category_name": "",
            "currency_code": None,
            "currency_name": None,
            "place_count": 0,
            "nonzero_place_count": 0,
            "opening_balance": Decimal("0"),
            "inflow_amount": Decimal("0"),
            "outflow_amount": Decimal("0"),
            "current_balance": Decimal("0"),
            "movement_count": 0,
        }
    )

    for row in raw_rows:
        folder_flag = _clean(row.get("folder_flag_hex")).lower()
        if folder_flag == "0x00":
            continue
        opening = _money(row.get("opening_balance"))
        inflow = _money(row.get("inflow_amount"))
        outflow = _money(row.get("outflow_amount"))
        current = _money(opening + inflow - outflow)
        if not include_zero and current == 0 and inflow == 0 and outflow == 0:
            continue
        name = _clean(row.get("money_place_name"))
        chain = _chain_for(
            {"name": name, "parent_ref": row.get("parent_ref_hex")},
            rows_by_ref,
        )
        category = classify_money_place(name, chain)
        currency_code = _clean(row.get("currency_code")) or None
        currency_name = _clean(row.get("currency_name")) or None
        movement_count = int(row.get("movement_count") or 0)
        detail = {
            "money_place_ref": _clean(row.get("money_place_ref_hex")),
            "money_place_code": _clean(row.get("money_place_code")) or None,
            "money_place_name": name,
            "category": category,
            "category_name": CATEGORY_LABELS.get(category, category),
            "currency_code": currency_code,
            "currency_name": currency_name,
            "opening_balance": str(opening),
            "inflow_amount": str(inflow),
            "outflow_amount": str(outflow),
            "current_balance": str(current),
            "movement_count": movement_count,
            "last_movement_at": (
                row["last_movement_at"].isoformat() if row.get("last_movement_at") else None
            ),
        }
        detail_rows.append(detail)

        key = (category, currency_code, currency_name)
        item = summary[key]
        item["category"] = category
        item["category_name"] = CATEGORY_LABELS.get(category, category)
        item["currency_code"] = currency_code
        item["currency_name"] = currency_name
        item["place_count"] += 1
        item["nonzero_place_count"] += 1 if current != 0 else 0
        item["opening_balance"] += opening
        item["inflow_amount"] += inflow
        item["outflow_amount"] += outflow
        item["current_balance"] += current
        item["movement_count"] += movement_count

    detail_rows.sort(key=lambda item: abs(_to_decimal(item["current_balance"])), reverse=True)

    summary_by_category_currency = []
    for item in summary.values():
        summary_by_category_currency.append(
            {
                "category": item["category"],
                "category_name": item["category_name"],
                "currency_code": item["currency_code"],
                "currency_name": item["currency_name"],
                "place_count": item["place_count"],
                "nonzero_place_count": item["nonzero_place_count"],
                "opening_balance": str(_money(item["opening_balance"])),
                "inflow_amount": str(_money(item["inflow_amount"])),
                "outflow_amount": str(_money(item["outflow_amount"])),
                "current_balance": str(_money(item["current_balance"])),
                "movement_count": item["movement_count"],
            }
        )
    summary_by_category_currency.sort(
        key=lambda item: (
            item["category"],
            str(item.get("currency_code") or ""),
            abs(_to_decimal(item["current_balance"])),
        ),
        reverse=True,
    )

    return {
        "status": "ready",
        "generated_at_msk": as_of_msk,
        "period_start": resolved_period_start.isoformat(),
        "period_end_msk": as_of_msk,
        "source": "1c_money_places",
        "summary_by_category_currency": summary_by_category_currency,
        "top_balances": detail_rows[: max(0, top_limit)],
        "money_place_count": len(detail_rows),
    }
