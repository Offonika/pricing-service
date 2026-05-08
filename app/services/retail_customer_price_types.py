from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.models import ReceivableLedgerEvent

BUYERS_CONTRACT_KIND_NAME = "С покупателем"
BUYERS_COUNTERPARTY_GROUP_NAME = "ПОКУПАТЕЛИ"
REGULAR_RECEIVABLES_LAYER = "regular_receivables"
PRICE_LEVEL_BRONZE = "bronze"
PRICE_LEVEL_SILVER = "silver"
PRICE_LEVEL_GOLD = "gold"
PRICE_LEVEL_UNKNOWN = "unknown"

PRICE_LEVEL_RANK = {
    PRICE_LEVEL_UNKNOWN: 0,
    PRICE_LEVEL_BRONZE: 1,
    PRICE_LEVEL_SILVER: 2,
    PRICE_LEVEL_GOLD: 3,
}
PRICE_LEVEL_LABEL = {
    PRICE_LEVEL_UNKNOWN: "Не распознан",
    PRICE_LEVEL_BRONZE: "Бронза",
    PRICE_LEVEL_SILVER: "Серебро",
    PRICE_LEVEL_GOLD: "Золото",
}
REGLEMENT_PRICE_TYPE = {
    PRICE_LEVEL_BRONZE: "2.Бронзовый",
    PRICE_LEVEL_SILVER: "3.Серебряный",
    PRICE_LEVEL_GOLD: "4.Золотой",
}

BRONZE_MIN_AMOUNT = Decimal("5000")
SILVER_MIN_AMOUNT = Decimal("300000")
GOLD_MIN_AMOUNT = Decimal("1200000")
GOLD_REGLEMENT_UPPER_HINT = Decimal("2500000")

ACTION_KEEP = "keep"
ACTION_SET_SILVER = "set_silver"
ACTION_SET_GOLD = "set_gold"
ACTION_DOWNGRADE_TO_SILVER = "downgrade_to_silver"
ACTION_DOWNGRADE_TO_BRONZE = "downgrade_to_bronze"
ACTION_REVIEW_CURRENT_TYPE = "review_current_type"

ACTION_LABEL = {
    ACTION_KEEP: "Оставить без изменений",
    ACTION_SET_SILVER: "Поставить серебро",
    ACTION_SET_GOLD: "Поставить золото",
    ACTION_DOWNGRADE_TO_SILVER: "Понизить до серебра",
    ACTION_DOWNGRADE_TO_BRONZE: "Перевести на бронзу",
    ACTION_REVIEW_CURRENT_TYPE: "Проверить текущий тип цен",
}


@dataclass(slots=True)
class _MonthlyCounterpartyTotals:
    counterparty_ref: str
    counterparty_name: str | None
    sales_amount: Decimal = Decimal("0.00")
    return_amount: Decimal = Decimal("0.00")
    purchase_amount: Decimal = Decimal("0.00")
    document_count: int = 0
    last_sale_at: datetime | None = None


@dataclass(slots=True)
class _CurrentPriceType:
    counterparty_ref: str
    counterparty_name: str | None
    contract_ref: str | None
    contract_name: str | None
    current_price_type: str | None
    current_level: str
    last_seen_at: datetime | None


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _ratio(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _month_start(month: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("month must be in YYYY-MM format")
    return date.fromisoformat(f"{month}-01")


def _add_months(value: date, months: int) -> date:
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    return date(year, month, 1)


def _normalize_text(value: Any) -> str:
    lowered = str(value or "").lower().replace("ё", "е")
    normalized = re.sub(r"[^a-z0-9а-я]+", " ", lowered, flags=re.IGNORECASE)
    return " ".join(normalized.split())


def _normalize_ref(value: Any) -> str:
    return str(value or "").strip().upper()


def normalize_price_level(value: Any) -> str:
    normalized = _normalize_text(value)
    if not normalized:
        return PRICE_LEVEL_UNKNOWN
    if "золот" in normalized or "gold" in normalized:
        return PRICE_LEVEL_GOLD
    if "сереб" in normalized or "silver" in normalized:
        return PRICE_LEVEL_SILVER
    if "бронз" in normalized or "bronze" in normalized:
        return PRICE_LEVEL_BRONZE
    return PRICE_LEVEL_UNKNOWN


def recommended_level_for_purchase_amount(amount: Decimal) -> str:
    if amount >= GOLD_MIN_AMOUNT:
        return PRICE_LEVEL_GOLD
    if amount >= SILVER_MIN_AMOUNT:
        return PRICE_LEVEL_SILVER
    return PRICE_LEVEL_BRONZE


def _rule_note(amount: Decimal, recommended_level: str) -> str:
    if recommended_level == PRICE_LEVEL_GOLD:
        if amount > GOLD_REGLEMENT_UPPER_HINT:
            return (
                "Чистые продажи от 1 200 000 ₽; сумма выше опубликованной верхней границы "
                "2 500 000 ₽, поэтому оставлен максимальный уровень регламента."
            )
        return "Чистые продажи от 1 200 000 ₽: по регламенту уровень Золото."
    if recommended_level == PRICE_LEVEL_SILVER:
        return "Чистые продажи от 300 000 ₽ до 1 200 000 ₽: по регламенту уровень Серебро."
    if amount < BRONZE_MIN_AMOUNT:
        return (
            "Чистые продажи ниже 5 000 ₽; отдельного нижнего уровня в регламенте нет, "
            "для понижения используется Бронза."
        )
    return "Чистые продажи ниже 300 000 ₽: по регламенту уровень Бронза."


def _action_for_levels(current_level: str, recommended_level: str) -> str:
    if current_level == recommended_level:
        return ACTION_KEEP
    if recommended_level == PRICE_LEVEL_GOLD:
        return ACTION_SET_GOLD
    if recommended_level == PRICE_LEVEL_SILVER:
        if current_level == PRICE_LEVEL_GOLD:
            return ACTION_DOWNGRADE_TO_SILVER
        return ACTION_SET_SILVER
    if recommended_level == PRICE_LEVEL_BRONZE and current_level in {
        PRICE_LEVEL_SILVER,
        PRICE_LEVEL_GOLD,
    }:
        return ACTION_DOWNGRADE_TO_BRONZE
    return ACTION_REVIEW_CURRENT_TYPE


def _is_actionable(action: str) -> bool:
    return action not in {ACTION_KEEP, ACTION_REVIEW_CURRENT_TYPE}


def _load_monthly_totals(
    session: Session,
    *,
    period_start: datetime,
    period_end: datetime,
) -> dict[str, _MonthlyCounterpartyTotals]:
    rows = (
        session.query(
            ReceivableLedgerEvent.counterparty_ref.label("counterparty_ref"),
            func.max(ReceivableLedgerEvent.counterparty_name).label("counterparty_name"),
            ReceivableLedgerEvent.event_type.label("event_type"),
            func.sum(ReceivableLedgerEvent.amount_delta).label("amount"),
            func.count(ReceivableLedgerEvent.id).label("document_count"),
            func.max(ReceivableLedgerEvent.external_document_date).label("last_sale_at"),
        )
        .filter(
            ReceivableLedgerEvent.external_document_date >= period_start,
            ReceivableLedgerEvent.external_document_date < period_end,
            ReceivableLedgerEvent.event_type.in_(("sale", "return")),
            ReceivableLedgerEvent.source_layer == REGULAR_RECEIVABLES_LAYER,
            ReceivableLedgerEvent.contract_kind_name == BUYERS_CONTRACT_KIND_NAME,
        )
        .group_by(ReceivableLedgerEvent.counterparty_ref, ReceivableLedgerEvent.event_type)
        .all()
    )

    totals: dict[str, _MonthlyCounterpartyTotals] = {}
    for row in rows:
        counterparty_ref = str(row.counterparty_ref or "").strip()
        if not counterparty_ref:
            continue
        item = totals.setdefault(
            counterparty_ref,
            _MonthlyCounterpartyTotals(
                counterparty_ref=counterparty_ref,
                counterparty_name=row.counterparty_name,
            ),
        )
        if row.counterparty_name and not item.counterparty_name:
            item.counterparty_name = row.counterparty_name
        amount = _to_decimal(row.amount)
        if row.event_type == "sale":
            item.sales_amount += amount
        elif row.event_type == "return":
            item.return_amount += abs(amount)
            item.sales_amount += Decimal("0.00")
            amount = -abs(amount) if amount > 0 else amount
        item.purchase_amount += amount
        item.document_count += int(row.document_count or 0)
        if row.last_sale_at and (item.last_sale_at is None or row.last_sale_at > item.last_sale_at):
            item.last_sale_at = row.last_sale_at

    for item in totals.values():
        item.purchase_amount = max(Decimal("0.00"), _money(item.purchase_amount))
        item.sales_amount = _money(item.sales_amount)
        item.return_amount = _money(item.return_amount)
    return totals


def _load_current_price_types(
    session: Session,
    *,
    period_end: datetime,
) -> dict[str, _CurrentPriceType]:
    latest_dates = (
        session.query(
            ReceivableLedgerEvent.counterparty_ref.label("counterparty_ref"),
            func.max(ReceivableLedgerEvent.external_document_date).label("last_seen_at"),
        )
        .filter(
            ReceivableLedgerEvent.external_document_date < period_end,
            ReceivableLedgerEvent.source_layer == REGULAR_RECEIVABLES_LAYER,
            ReceivableLedgerEvent.contract_kind_name == BUYERS_CONTRACT_KIND_NAME,
            ReceivableLedgerEvent.contract_name.isnot(None),
        )
        .group_by(ReceivableLedgerEvent.counterparty_ref)
        .subquery()
    )

    rows = (
        session.query(
            ReceivableLedgerEvent.counterparty_ref,
            ReceivableLedgerEvent.counterparty_name,
            ReceivableLedgerEvent.contract_ref,
            ReceivableLedgerEvent.contract_name,
            ReceivableLedgerEvent.external_document_date,
            ReceivableLedgerEvent.id,
        )
        .join(
            latest_dates,
            and_(
                ReceivableLedgerEvent.counterparty_ref == latest_dates.c.counterparty_ref,
                ReceivableLedgerEvent.external_document_date == latest_dates.c.last_seen_at,
            ),
        )
        .order_by(
            ReceivableLedgerEvent.counterparty_ref,
            ReceivableLedgerEvent.external_document_date.desc(),
            ReceivableLedgerEvent.id.desc(),
        )
        .all()
    )

    current: dict[str, _CurrentPriceType] = {}
    for row in rows:
        counterparty_ref = str(row.counterparty_ref or "").strip()
        if not counterparty_ref or counterparty_ref in current:
            continue
        level = normalize_price_level(row.contract_name)
        current[counterparty_ref] = _CurrentPriceType(
            counterparty_ref=counterparty_ref,
            counterparty_name=row.counterparty_name,
            contract_ref=row.contract_ref,
            contract_name=row.contract_name,
            current_price_type=row.contract_name,
            current_level=level,
            last_seen_at=row.external_document_date,
        )
    return current


def build_retail_customer_price_type_recommendations(
    session: Session,
    *,
    month: str,
    actionable_only: bool = True,
    limit: int | None = None,
    allowed_counterparty_refs: set[str] | None = None,
    counterparty_codes_by_ref: dict[str, str] | None = None,
    contract_price_type_loader: Callable[[set[str]], dict[str, str]] | None = None,
    previous_purchase_amounts_by_ref: dict[str, Decimal] | None = None,
) -> dict[str, Any]:
    start_date = _month_start(month)
    end_date = _add_months(start_date, 1)
    previous_start_date = _add_months(start_date, -1)
    period_start = datetime.combine(start_date, time.min)
    period_end = datetime.combine(end_date, time.min)
    previous_period_start = datetime.combine(previous_start_date, time.min)

    monthly_totals = _load_monthly_totals(
        session,
        period_start=period_start,
        period_end=period_end,
    )
    previous_monthly_totals = _load_monthly_totals(
        session,
        period_start=previous_period_start,
        period_end=period_start,
    )
    current_types = _load_current_price_types(session, period_end=period_end)
    contract_price_types_by_ref: dict[str, str] = {}
    if contract_price_type_loader is not None:
        contract_refs = {
            current.contract_ref
            for current in current_types.values()
            if current.contract_ref is not None
        }
        contract_price_types_by_ref = {
            _normalize_ref(contract_ref): str(price_type).strip()
            for contract_ref, price_type in contract_price_type_loader(contract_refs).items()
            if _normalize_ref(contract_ref) and str(price_type or "").strip()
        }

    def _current_price_type(current: _CurrentPriceType) -> str | None:
        if current.contract_ref is not None:
            price_type = contract_price_types_by_ref.get(_normalize_ref(current.contract_ref))
            if price_type:
                return price_type
        return current.current_price_type

    def _current_level(current: _CurrentPriceType) -> str:
        return normalize_price_level(_current_price_type(current))

    candidate_refs = set(monthly_totals)
    candidate_refs.update(
        ref
        for ref, current in current_types.items()
        if _current_level(current) in {PRICE_LEVEL_SILVER, PRICE_LEVEL_GOLD}
    )
    allowed_ref_keys = (
        {_normalize_ref(value) for value in allowed_counterparty_refs if _normalize_ref(value)}
        if allowed_counterparty_refs is not None
        else None
    )
    if allowed_ref_keys is not None:
        candidate_refs = {
            counterparty_ref
            for counterparty_ref in candidate_refs
            if _normalize_ref(counterparty_ref) in allowed_ref_keys
        }
    code_mapping = {
        _normalize_ref(counterparty_ref): str(counterparty_code).strip()
        for counterparty_ref, counterparty_code in (counterparty_codes_by_ref or {}).items()
        if _normalize_ref(counterparty_ref) and str(counterparty_code or "").strip()
    }
    previous_purchase_mapping = {
        _normalize_ref(counterparty_ref): _money(_to_decimal(purchase_amount))
        for counterparty_ref, purchase_amount in (previous_purchase_amounts_by_ref or {}).items()
        if _normalize_ref(counterparty_ref)
    }

    rows: list[dict[str, Any]] = []
    action_counts: dict[str, int] = {action: 0 for action in ACTION_LABEL}
    for counterparty_ref in candidate_refs:
        totals = monthly_totals.get(
            counterparty_ref,
            _MonthlyCounterpartyTotals(counterparty_ref=counterparty_ref, counterparty_name=None),
        )
        current = current_types.get(
            counterparty_ref,
            _CurrentPriceType(
                counterparty_ref=counterparty_ref,
                counterparty_name=totals.counterparty_name,
                contract_ref=None,
                contract_name=None,
                current_price_type=None,
                current_level=PRICE_LEVEL_UNKNOWN,
                last_seen_at=None,
            ),
        )
        current_price_type = _current_price_type(current)
        current_level = normalize_price_level(current_price_type)
        recommended_level = recommended_level_for_purchase_amount(totals.purchase_amount)
        previous_totals = previous_monthly_totals.get(counterparty_ref)
        previous_purchase_amount = (
            previous_purchase_mapping.get(_normalize_ref(counterparty_ref))
            if _normalize_ref(counterparty_ref) in previous_purchase_mapping
            else (
                _money(previous_totals.purchase_amount)
                if previous_totals is not None
                else Decimal("0.00")
            )
        )
        purchase_delta_amount = _money(totals.purchase_amount - previous_purchase_amount)
        purchase_delta_pct = (
            _ratio(purchase_delta_amount / previous_purchase_amount)
            if previous_purchase_amount > 0
            else None
        )
        action = _action_for_levels(current_level, recommended_level)
        action_counts[action] = action_counts.get(action, 0) + 1
        if actionable_only and not _is_actionable(action):
            continue

        counterparty_name = totals.counterparty_name or current.counterparty_name
        counterparty_code = code_mapping.get(_normalize_ref(counterparty_ref))
        rows.append(
            {
                "counterparty_ref": counterparty_ref,
                "counterparty_code": counterparty_code,
                "counterparty_name": counterparty_name,
                "current_price_type": current_price_type,
                "current_level": current_level,
                "current_level_label": PRICE_LEVEL_LABEL[current_level],
                "recommended_price_type": REGLEMENT_PRICE_TYPE[recommended_level],
                "recommended_level": recommended_level,
                "recommended_level_label": PRICE_LEVEL_LABEL[recommended_level],
                "action": action,
                "action_label": ACTION_LABEL[action],
                "purchase_amount": _money(totals.purchase_amount),
                "net_sales_amount": _money(totals.purchase_amount),
                "previous_purchase_amount": previous_purchase_amount,
                "previous_net_sales_amount": previous_purchase_amount,
                "purchase_delta_amount": purchase_delta_amount,
                "net_sales_delta_amount": purchase_delta_amount,
                "purchase_delta_pct": purchase_delta_pct,
                "net_sales_delta_pct": purchase_delta_pct,
                "sales_amount": _money(totals.sales_amount),
                "return_amount": _money(totals.return_amount),
                "document_count": totals.document_count,
                "last_sale_at": totals.last_sale_at,
                "current_price_seen_at": current.last_seen_at,
                "rule_note": _rule_note(totals.purchase_amount, recommended_level),
            }
        )

    rows.sort(
        key=lambda item: (
            PRICE_LEVEL_RANK.get(str(item["recommended_level"]), 0),
            _to_decimal(item["purchase_amount"]),
            str(item.get("counterparty_name") or ""),
        ),
        reverse=True,
    )
    if limit is not None:
        rows = rows[:limit]

    return {
        "month": month,
        "previous_month": previous_start_date.strftime("%Y-%m"),
        "month_start": start_date,
        "month_end": end_date - timedelta(days=1),
        "freshness_status": "fresh" if monthly_totals or current_types else "missing",
        "source_status": "ready" if monthly_totals or current_types else "empty",
        "summary": {
            "total_candidates": len(candidate_refs),
            "returned_count": len(rows),
            "buyer_group_counterparty_count": (
                len(allowed_ref_keys) if allowed_ref_keys is not None else None
            ),
            "actionable_count": sum(
                count for action, count in action_counts.items() if _is_actionable(action)
            ),
            "keep_count": action_counts.get(ACTION_KEEP, 0),
            "set_silver_count": action_counts.get(ACTION_SET_SILVER, 0),
            "set_gold_count": action_counts.get(ACTION_SET_GOLD, 0),
            "downgrade_to_silver_count": action_counts.get(ACTION_DOWNGRADE_TO_SILVER, 0),
            "downgrade_to_bronze_count": action_counts.get(ACTION_DOWNGRADE_TO_BRONZE, 0),
            "review_current_type_count": action_counts.get(ACTION_REVIEW_CURRENT_TYPE, 0),
            "rules": {
                "bronze": "5 000 ₽ <= чистые продажи < 300 000 ₽",
                "silver": "300 000 ₽ <= чистые продажи < 1 200 000 ₽",
                "gold": (
                    "1 200 000 ₽ <= чистые продажи; " "в регламенте верхняя подсказка 2 500 000 ₽"
                ),
            },
        },
        "payload": rows,
    }
