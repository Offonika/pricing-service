"""Deprecated compatibility projection for customer price-type recommendations.

The public compatibility endpoint remains read-only, but all decisions are made
by the canonical customer-price-type domain engine and the versioned ruleset.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.domains.customer_price_types import (
    ContractFact,
    CustomerPriceTypeFacts,
    CustomerPriceTypeRulesEngine,
    load_price_type_ruleset,
    proven_history_coverage_months,
)
from app.models import ReceivableLedgerEvent

REPO_ROOT = Path(__file__).resolve().parents[2]
RULESET = load_price_type_ruleset(REPO_ROOT / "config/price_types/ruleset.yaml")
ENGINE = CustomerPriceTypeRulesEngine(RULESET)

BUYERS_CONTRACT_KIND_NAME = "С покупателем"
BUYERS_COUNTERPARTY_GROUP_NAME = "ПОКУПАТЕЛИ"
REGULAR_RECEIVABLES_LAYER = "regular_receivables"

PRICE_LEVEL_RETAIL = "retail"
PRICE_LEVEL_BRONZE = "bronze"
PRICE_LEVEL_SILVER = "silver"
PRICE_LEVEL_GOLD = "gold"
PRICE_LEVEL_PLATINUM = "platinum"
PRICE_LEVEL_UNKNOWN = "unknown"

ACTION_KEEP = "keep"
ACTION_SET_SILVER = "set_silver"
ACTION_SET_GOLD = "set_gold"
ACTION_DOWNGRADE_TO_GOLD = "downgrade_to_gold"
ACTION_DOWNGRADE_TO_SILVER = "downgrade_to_silver"
ACTION_DOWNGRADE_TO_BRONZE = "downgrade_to_bronze"
ACTION_DOWNGRADE_TO_RETAIL = "downgrade_to_retail"
ACTION_MANAGER_RETENTION = "manager_retention"
ACTION_ISOLATE = "isolate"
ACTION_RECOVERY = "recovery"
ACTION_DATA_CHECK = "data_check"
ACTION_SPECIAL_REVIEW = "special_review"
ACTION_REVIEW_CURRENT_TYPE = "review_current_type"

ACTION_LABEL = {
    ACTION_KEEP: "Оставить без изменений",
    ACTION_SET_SILVER: "Повышения заморожены",
    ACTION_SET_GOLD: "Повышения заморожены",
    ACTION_DOWNGRADE_TO_GOLD: "К проверке: понижение до золота",
    ACTION_DOWNGRADE_TO_SILVER: "К проверке: понижение до серебра",
    ACTION_DOWNGRADE_TO_BRONZE: "К проверке: понижение до бронзы",
    ACTION_DOWNGRADE_TO_RETAIL: "К проверке: перевод в розницу",
    ACTION_MANAGER_RETENTION: "Удержание и работа менеджера",
    ACTION_ISOLATE: "Полный месяц изолятора",
    ACTION_RECOVERY: "CRM-реанимация",
    ACTION_DATA_CHECK: "Сверка данных",
    ACTION_SPECIAL_REVIEW: "Специальная ручная проверка",
    ACTION_REVIEW_CURRENT_TYPE: "Проверить текущий тип цен",
}


def _month_start(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError("month must be in YYYY-MM format")
    return date.fromisoformat(f"{value}-01")


def _add_months(value: date, months: int) -> date:
    total = value.year * 12 + value.month - 1 + months
    return date(total // 12, total % 12 + 1, 1)


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _ratio(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _normalized_ref(value: Any) -> str:
    return str(value or "").strip().casefold()


def _engine_ref(value: str) -> str:
    normalized = _normalized_ref(value)
    if re.fullmatch(r"0x[0-9a-f]{32}", normalized):
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
    return f"0x{digest}"


def normalize_price_level(value: Any) -> str:
    raw = " ".join(str(value or "").split()).casefold()
    for prefix in RULESET.retail_prefixes:
        if raw.startswith(prefix.casefold()):
            return PRICE_LEVEL_RETAIL
    for level in RULESET.levels:
        if raw.startswith(level.price_type_prefix.casefold()):
            return level.key
    return PRICE_LEVEL_UNKNOWN


def recommended_level_for_purchase_amount(amount: Decimal) -> str:
    """Return an informational level only; v1 never turns it into an upgrade."""
    result = PRICE_LEVEL_BRONZE
    for level in RULESET.levels:
        if amount >= level.retention_norm_3m:
            result = level.key
    return result


def _month_expression(session: Session):
    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "sqlite":
        return func.strftime("%Y-%m", ReceivableLedgerEvent.external_document_date)
    return func.to_char(ReceivableLedgerEvent.external_document_date, "YYYY-MM")


def _load_rows(
    session: Session,
    *,
    period_start: datetime,
    period_end: datetime,
) -> tuple[
    dict[str, dict[str, Decimal]],
    dict[str, dict[str, Any]],
]:
    month_expr = _month_expression(session)
    rows = (
        session.query(
            ReceivableLedgerEvent.counterparty_ref,
            func.max(ReceivableLedgerEvent.counterparty_name).label("counterparty_name"),
            ReceivableLedgerEvent.contract_ref,
            func.max(ReceivableLedgerEvent.contract_name).label("contract_name"),
            month_expr.label("month_key"),
            ReceivableLedgerEvent.event_type,
            func.sum(ReceivableLedgerEvent.amount_delta).label("amount"),
            func.count(ReceivableLedgerEvent.id).label("document_count"),
            func.max(ReceivableLedgerEvent.external_document_date).label("last_sale_at"),
            func.min(ReceivableLedgerEvent.external_document_date).label("first_activity_at"),
        )
        .filter(
            ReceivableLedgerEvent.external_document_date >= period_start,
            ReceivableLedgerEvent.external_document_date < period_end,
            ReceivableLedgerEvent.event_type.in_(("sale", "return")),
            ReceivableLedgerEvent.source_layer == REGULAR_RECEIVABLES_LAYER,
            ReceivableLedgerEvent.contract_kind_name == BUYERS_CONTRACT_KIND_NAME,
        )
        .group_by(
            ReceivableLedgerEvent.counterparty_ref,
            ReceivableLedgerEvent.contract_ref,
            month_expr,
            ReceivableLedgerEvent.event_type,
        )
        .all()
    )
    raw: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    metadata: dict[str, dict[str, Any]] = {}
    for row in rows:
        ref = _normalized_ref(row.counterparty_ref)
        if not ref:
            continue
        amount = Decimal(str(row.amount or 0))
        if row.event_type == "return":
            amount = -abs(amount)
        raw[(ref, str(row.month_key))] += amount
        item = metadata.setdefault(
            ref,
            {
                "counterparty_name": row.counterparty_name,
                "contracts": {},
                "sales_amount": Decimal("0"),
                "return_amount": Decimal("0"),
                "document_count": 0,
                "last_sale_at": None,
                "first_activity_at": None,
            },
        )
        if row.contract_ref:
            item["contracts"].setdefault(
                str(row.contract_ref), str(row.contract_name or "").strip() or None
            )
        if row.event_type == "sale":
            item["sales_amount"] += Decimal(str(row.amount or 0))
        else:
            item["return_amount"] += abs(Decimal(str(row.amount or 0)))
        item["document_count"] += int(row.document_count or 0)
        if row.last_sale_at and (
            item["last_sale_at"] is None or row.last_sale_at > item["last_sale_at"]
        ):
            item["last_sale_at"] = row.last_sale_at
        if row.first_activity_at and (
            item["first_activity_at"] is None or row.first_activity_at < item["first_activity_at"]
        ):
            item["first_activity_at"] = row.first_activity_at
    first_activity_rows = (
        session.query(
            ReceivableLedgerEvent.counterparty_ref,
            func.min(ReceivableLedgerEvent.external_document_date).label("first_activity_at"),
        )
        .filter(
            ReceivableLedgerEvent.external_document_date < period_end,
            ReceivableLedgerEvent.event_type.in_(("sale", "return")),
            ReceivableLedgerEvent.source_layer == REGULAR_RECEIVABLES_LAYER,
            ReceivableLedgerEvent.contract_kind_name == BUYERS_CONTRACT_KIND_NAME,
        )
        .group_by(ReceivableLedgerEvent.counterparty_ref)
        .all()
    )
    for row in first_activity_rows:
        ref = _normalized_ref(row.counterparty_ref)
        if ref in metadata:
            metadata[ref]["first_activity_at"] = row.first_activity_at
    monthly: dict[str, dict[str, Decimal]] = defaultdict(dict)
    for (ref, month), amount in raw.items():
        monthly[ref][month] = _money(amount)
    return monthly, metadata


def _decision_action(decision) -> str:
    target = decision.recommended_price_type
    if decision.recommendation == "keep_current":
        return ACTION_KEEP
    if decision.recommendation == "manager_retention":
        return ACTION_MANAGER_RETENTION
    if decision.recommendation == "isolate":
        if target == "4.Золотой":
            return ACTION_DOWNGRADE_TO_GOLD
        if target == "3.Серебряный":
            return ACTION_DOWNGRADE_TO_SILVER
        if target == "2.Бронзовый":
            return ACTION_DOWNGRADE_TO_BRONZE
        if target == "Розница":
            return ACTION_DOWNGRADE_TO_RETAIL
        return ACTION_ISOLATE
    if decision.recommendation == "downgrade_to_retail":
        return ACTION_DOWNGRADE_TO_RETAIL
    if decision.recommendation == "recovery":
        return ACTION_RECOVERY
    if decision.recommendation == "data_check":
        return ACTION_DATA_CHECK
    if decision.recommendation.startswith("manual_override:"):
        return ACTION_SPECIAL_REVIEW if decision.action_required else ACTION_KEEP
    if decision.recommendation == "special_review":
        return ACTION_SPECIAL_REVIEW
    return ACTION_REVIEW_CURRENT_TYPE


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
    snapshot_month = _month_start(month)
    period_start = _add_months(snapshot_month, -11)
    period_end = _add_months(snapshot_month, 1)
    monthly, metadata = _load_rows(
        session,
        period_start=datetime.combine(period_start, time.min),
        period_end=datetime.combine(period_end, time.min),
    )
    candidate_refs = set(metadata)
    if allowed_counterparty_refs is not None:
        allowed = {_normalized_ref(value) for value in allowed_counterparty_refs}
        candidate_refs &= allowed
    code_mapping = {
        _normalized_ref(ref): str(code).strip()
        for ref, code in (counterparty_codes_by_ref or {}).items()
    }
    all_contract_refs = {
        contract_ref for ref in candidate_refs for contract_ref in metadata[ref]["contracts"]
    }
    loaded_price_types = (
        {
            _normalized_ref(ref): str(value).strip()
            for ref, value in contract_price_type_loader(all_contract_refs).items()
        }
        if contract_price_type_loader and all_contract_refs
        else {}
    )

    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = defaultdict(int)
    case_counts: dict[str, int] = defaultdict(int)
    window_keys = [_add_months(snapshot_month, delta).strftime("%Y-%m") for delta in (-2, -1, 0)]
    previous_keys = [_add_months(snapshot_month, delta).strftime("%Y-%m") for delta in (-3, -2, -1)]
    for ref in sorted(candidate_refs):
        item = metadata[ref]
        contracts = tuple(
            ContractFact(
                contract_ref=contract_ref,
                contract_name=contract_name,
                price_type_name=(
                    loaded_price_types.get(_normalized_ref(contract_ref)) or contract_name
                ),
            )
            for contract_ref, contract_name in sorted(item["contracts"].items())
        )
        values = monthly.get(ref, {})
        total = sum((values.get(key, Decimal("0")) for key in window_keys), Decimal("0"))
        previous_total = sum((values.get(key, Decimal("0")) for key in previous_keys), Decimal("0"))
        external_previous = (previous_purchase_amounts_by_ref or {}).get(ref)
        if external_previous is None:
            external_previous = (previous_purchase_amounts_by_ref or {}).get(ref.upper())
        if external_previous is not None:
            previous_total = Decimal(str(external_previous))
        first_activity_at = item["first_activity_at"]
        first_activity_date = (
            first_activity_at.date()
            if isinstance(first_activity_at, datetime)
            else first_activity_at
        )
        facts = CustomerPriceTypeFacts(
            counterparty_ref=_engine_ref(ref),
            counterparty_code=code_mapping.get(ref),
            counterparty_name=item["counterparty_name"],
            snapshot_month=snapshot_month,
            contracts=contracts,
            monthly_sales=dict(values),
            source_statuses={
                "contracts": "ready",
                "sales_history": "ready",
                "ledger_reconciliation": "ready",
                "master_data": "ready",
            },
            first_activity_date=first_activity_date,
            history_coverage_months=proven_history_coverage_months(
                first_activity_date, snapshot_month
            ),
            direct_onec_total_3m=total,
            ledger_total_3m=total,
            economics_status="missing",
            returns={"return_amount": str(_money(item["return_amount"]))},
        )
        decision = ENGINE.evaluate(facts)
        action = _decision_action(decision)
        counts[action] += 1
        if decision.case_type:
            case_counts[decision.case_type] += 1
        if actionable_only and not decision.action_required:
            continue
        delta = _money(total - previous_total)
        delta_pct = _ratio(delta / previous_total) if previous_total > 0 else None
        rows.append(
            {
                "counterparty_ref": ref,
                "counterparty_code": code_mapping.get(ref),
                "counterparty_name": item["counterparty_name"],
                "current_price_type": decision.current_price_type,
                "current_level": decision.current_level or PRICE_LEVEL_UNKNOWN,
                "current_level_label": decision.current_level or "Не распознан",
                "recommended_price_type": decision.recommended_price_type,
                "recommended_level": normalize_price_level(decision.recommended_price_type),
                "recommended_level_label": decision.recommended_price_type or "Ручная проверка",
                "action": action,
                "action_label": ACTION_LABEL[action],
                "purchase_amount": _money(total),
                "net_sales_amount": _money(total),
                "previous_purchase_amount": _money(previous_total),
                "previous_net_sales_amount": _money(previous_total),
                "purchase_delta_amount": delta,
                "net_sales_delta_amount": delta,
                "purchase_delta_pct": delta_pct,
                "net_sales_delta_pct": delta_pct,
                "sales_amount": _money(item["sales_amount"]),
                "return_amount": _money(item["return_amount"]),
                "document_count": item["document_count"],
                "last_sale_at": item["last_sale_at"],
                "current_price_seen_at": item["last_sale_at"],
                "rule_note": decision.recommendation_reason,
            }
        )

    rows.sort(
        key=lambda item: (
            Decimal(str(item["purchase_amount"])),
            str(item.get("counterparty_name") or ""),
        ),
        reverse=True,
    )
    if limit is not None:
        rows = rows[:limit]
    actionable_count = sum(
        count
        for action, count in counts.items()
        if action not in {ACTION_KEEP, ACTION_REVIEW_CURRENT_TYPE}
    )
    return {
        "month": month,
        "previous_month": _add_months(snapshot_month, -1).strftime("%Y-%m"),
        "month_start": _add_months(snapshot_month, -2),
        "month_end": _add_months(snapshot_month, 1) - timedelta(days=1),
        "freshness_status": "fresh" if candidate_refs else "missing",
        "source_status": "ready" if candidate_refs else "empty",
        "summary": {
            "total_candidates": len(candidate_refs),
            "returned_count": len(rows),
            "buyer_group_counterparty_count": (
                len(allowed_counterparty_refs) if allowed_counterparty_refs is not None else None
            ),
            "actionable_count": actionable_count,
            "keep_count": counts[ACTION_KEEP],
            "set_silver_count": 0,
            "set_gold_count": 0,
            "downgrade_to_gold_count": counts[ACTION_DOWNGRADE_TO_GOLD],
            "downgrade_to_silver_count": counts[ACTION_DOWNGRADE_TO_SILVER],
            "downgrade_to_bronze_count": counts[ACTION_DOWNGRADE_TO_BRONZE],
            "downgrade_to_retail_count": counts[ACTION_DOWNGRADE_TO_RETAIL],
            "manager_work_count": case_counts["manager_work"],
            "isolate_count": case_counts["isolate"],
            "recovery_count": case_counts["recovery"],
            "data_check_count": case_counts["data_check"],
            "special_review_count": case_counts["special_review"],
            "review_current_type_count": counts[ACTION_REVIEW_CURRENT_TYPE],
            "ruleset_version": RULESET.version,
            "rules": {
                level.key: (
                    f"3м >= {level.retention_norm_3m}; "
                    f"последний месяц >= {level.hold_last_month}"
                )
                for level in RULESET.levels
            },
        },
        "payload": rows,
    }
