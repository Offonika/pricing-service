from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import Select, false, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.contracts import ContractIntegrityError, read_json_contract
from app.infrastructure.db.engines import DatabaseNotConfiguredError, get_onec_engine
from app.models import (
    ExecutiveActionItem,
    ExecutiveDashboardSnapshot,
    ExecutiveSourceFreshness,
    OneCSalesDailyKpi,
    ReceivableCase,
    ReceivableFolderRecommendationCache,
    ReceivableWorkItem,
)
from app.schemas.executive_dashboard import (
    ExecutiveCashflowDailyRow,
    ExecutiveCashflowPeriodBreakdownRow,
    ExecutiveCashflowPeriodRatio,
    ExecutiveCashflowPeriodResponse,
    ExecutiveCashflowQualityIssue,
    ExecutiveDashboardAction,
    ExecutiveDashboardActionsResponse,
    ExecutiveDashboardBlock,
    ExecutiveDashboardMetric,
    ExecutiveDashboardResponse,
    ExecutiveProfitLossBreakdownRow,
    ExecutiveProfitLossDailyRow,
    ExecutiveProfitLossExpenseBreakdownRow,
    ExecutiveProfitLossLineItem,
    ExecutiveProfitLossOpenQuestion,
    ExecutiveProfitLossPeriodResponse,
    ExecutiveProfitLossRatio,
    ExecutiveSalesBreakdownRow,
    ExecutiveSalesDailyRow,
    ExecutiveSalesDiagnosticKpi,
    ExecutiveSalesFilterOption,
    ExecutiveSalesMonthlyRow,
    ExecutiveSalesPeriodResponse,
    ExecutiveSalesPlanContext,
    ExecutiveSourceStatus,
)
from app.services.bitrix_executive_dashboard_auth import (
    EXECUTIVE_DASHBOARD_MONEY_BLOCK_KEYS,
    ExecutiveDashboardAuthContext,
    full_executive_dashboard_context,
    legacy_domain_executive_dashboard_context,
)
from app.services.executive_service_accruals import (
    service_accrual_balance_adjustments,
    service_accrual_profit_loss_summary,
)
from app.services.onec_inventory_cost import (
    OneCInventoryCostError,
    OneCInventoryCostSnapshot,
    fetch_onec_inventory_cost,
)
from app.services.receivables import CASE_BUYERS

AccessLevel = Literal["full", "domain"]

_MONEY_BLOCK_KEYS = set(EXECUTIVE_DASHBOARD_MONEY_BLOCK_KEYS)
_RECEIVABLE_CLOSED_STATUSES = {"closed", "paid"}
_SEVERITY_RANK = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _as_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            normalized = value.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None
    return None


def _metric(
    key: str,
    label: str,
    value: Decimal | int | float | str | None,
    *,
    unit: str | None = None,
    tone: str = "neutral",
    masked: bool = False,
    source_status: str = "ready",
) -> ExecutiveDashboardMetric:
    return ExecutiveDashboardMetric(
        key=key,
        label=label,
        value=None if masked else value,
        unit=unit,
        tone=tone,
        masked=masked,
        source_status=source_status,
    )


def _coerce_access_context(
    *,
    access_context: ExecutiveDashboardAuthContext | None = None,
    access_level: AccessLevel = "full",
    bitrix_user_id: str | None = None,
) -> ExecutiveDashboardAuthContext:
    if access_context is not None:
        return access_context
    if access_level == "full":
        return full_executive_dashboard_context()
    return legacy_domain_executive_dashboard_context(bitrix_user_id)


def _mask_finance(block_key: str, access_context: ExecutiveDashboardAuthContext) -> bool:
    return block_key in _MONEY_BLOCK_KEYS and not access_context.can_view_money_block(block_key)


def _freshness_from_status(source_status: str) -> str:
    if source_status == "ready":
        return "fresh"
    if source_status == "partial":
        return "partial"
    if source_status == "stale":
        return "stale"
    return "missing"


def _freshness_for_date(
    *,
    requested_date: date,
    source_as_of: date | datetime | None,
    max_lag_days: int | None = None,
) -> str:
    if source_as_of is None:
        return "missing"
    source_date = source_as_of.date() if isinstance(source_as_of, datetime) else source_as_of
    allowed_lag = (
        get_settings().executive_dashboard_source_max_lag_days
        if max_lag_days is None
        else max(int(max_lag_days), 0)
    )
    lag_days = max((requested_date - source_date).days, 0)
    return "fresh" if lag_days <= allowed_lag else "stale"


def _apply_date_freshness(
    source_status: str,
    *,
    requested_date: date,
    source_as_of: date | datetime | None,
    max_lag_days: int | None = None,
) -> tuple[str, str]:
    if source_status in {"source_missing", "source_error"}:
        return source_status, _freshness_from_status(source_status)
    freshness = _freshness_for_date(
        requested_date=requested_date,
        source_as_of=source_as_of,
        max_lag_days=max_lag_days,
    )
    if freshness == "stale" and source_status == "ready":
        return "stale", "stale"
    if freshness == "missing":
        return source_status, _freshness_from_status(source_status)
    if source_status == "partial" and freshness == "fresh":
        return "partial", "partial"
    return source_status, freshness


def _source_statuses(blocks: list[ExecutiveDashboardBlock]) -> tuple[str, str]:
    statuses = {block.source_status for block in blocks}
    if "source_error" in statuses:
        return "partial", "partial"
    if "ready" in statuses and len(statuses) == 1:
        return "fresh", "ready"
    if "ready" in statuses or "partial" in statuses:
        return "partial", "partial"
    if "stale" in statuses:
        return "stale", "stale"
    return "missing", "source_missing"


def _source_key_allowed(source_key: str, access_context: ExecutiveDashboardAuthContext) -> bool:
    finance_source_to_block = {
        "finance.cashflow": "money_today",
        "finance.cash_position": "money_today",
        "finance.profit_loss": "profit_loss",
        "finance.payables_1c": "creditors_payables",
        "finance.reconciliation": "reconciliation",
        "warehouse.piecework": "warehouse_operations",
    }
    return access_context.allows_block(finance_source_to_block.get(source_key, source_key))


def _resolve_shared_path(configured_value: str) -> Path:
    configured = Path(configured_value)
    if configured.is_absolute():
        return configured
    return Path.cwd() / configured


def _resolve_snapshot_path() -> Path:
    return _resolve_shared_path(get_settings().executive_dashboard_finance_snapshot_path)


def _load_finance_snapshot() -> tuple[dict[str, Any] | None, str, str]:
    path = _resolve_snapshot_path()
    if not path.exists():
        return None, "source_missing", f"finance snapshot is not found: {path}"
    try:
        payload = read_json_contract(path)
    except (OSError, json.JSONDecodeError, ContractIntegrityError) as exc:
        return None, "source_error", f"finance snapshot is not readable: {exc}"
    if not isinstance(payload, dict):
        return None, "source_error", "finance snapshot root must be an object"
    return payload, str(payload.get("source_status") or "ready"), str(path)


def _resolve_cashflow_period_cache_path() -> Path:
    return _resolve_shared_path(get_settings().executive_dashboard_cashflow_period_cache_path)


def _load_cashflow_period_cache() -> tuple[dict[str, Any] | None, str, str]:
    path = _resolve_cashflow_period_cache_path()
    if not path.exists():
        return None, "source_missing", f"cashflow period cache is not found: {path}"
    try:
        payload = read_json_contract(path)
    except (OSError, json.JSONDecodeError, ContractIntegrityError) as exc:
        return None, "source_error", f"cashflow period cache is not readable: {exc}"
    if not isinstance(payload, dict):
        return None, "source_error", "cashflow period cache root must be an object"
    return payload, str(payload.get("source_status") or "ready"), str(path)


def _resolve_warehouse_snapshot_path() -> Path:
    return _resolve_shared_path(get_settings().executive_dashboard_warehouse_snapshot_path)


def _load_warehouse_snapshot() -> tuple[dict[str, Any] | None, str, str]:
    path = _resolve_warehouse_snapshot_path()
    if not path.exists():
        return None, "source_missing", f"warehouse snapshot is not found: {path}"
    try:
        payload = read_json_contract(path)
    except (OSError, json.JSONDecodeError, ContractIntegrityError) as exc:
        return None, "source_error", f"warehouse snapshot is not readable: {exc}"
    if not isinstance(payload, dict):
        return None, "source_error", "warehouse snapshot root must be an object"
    return payload, str(payload.get("source_status") or "ready"), str(path)


def _resolve_owner_cash_control_snapshot_path() -> Path:
    return _resolve_shared_path(get_settings().executive_dashboard_owner_cash_control_snapshot_path)


def _load_owner_cash_control_snapshot() -> tuple[dict[str, Any] | None, str, str]:
    path = _resolve_owner_cash_control_snapshot_path()
    if not path.exists():
        return None, "source_missing", f"owner cash control snapshot is not found: {path}"
    try:
        payload = read_json_contract(path)
    except (OSError, json.JSONDecodeError, ContractIntegrityError) as exc:
        return None, "source_error", f"owner cash control snapshot is not readable: {exc}"
    if not isinstance(payload, dict):
        return None, "source_error", "owner cash control snapshot root must be an object"
    return payload, str(payload.get("source_status") or "ready"), str(path)


def _resolve_sales_plan_snapshot_path() -> Path:
    return _resolve_shared_path(get_settings().executive_dashboard_sales_plan_snapshot_path)


def _load_sales_plan_snapshot() -> tuple[dict[str, Any] | None, str, str]:
    path = _resolve_sales_plan_snapshot_path()
    if not path.exists():
        return None, "source_missing", f"sales plan snapshot is not found: {path}"
    try:
        payload = read_json_contract(path)
    except (OSError, json.JSONDecodeError, ContractIntegrityError) as exc:
        return None, "source_error", f"sales plan snapshot is not readable: {exc}"
    if not isinstance(payload, dict):
        return None, "source_error", "sales plan snapshot root must be an object"
    if payload.get("schema_version") != 1:
        return None, "source_error", "sales plan snapshot schema_version must be 1"
    return payload, str(payload.get("source_status") or "source_missing"), str(path)


def _combine_profit_loss_status(sales_status: str, expense_status: str) -> str:
    if sales_status == "source_missing":
        return "source_missing"
    if "source_error" in {sales_status, expense_status}:
        return "partial"
    if "stale" in {sales_status, expense_status}:
        return "stale"
    if sales_status != "ready" or expense_status != "ready":
        return "partial"
    return "ready"


def _profit_loss_expenses_from_cashflow_cache(
    *,
    session: Session,
    date_from: date,
    date_to: date,
) -> dict[str, Any]:
    payload, source_status, note = _load_cashflow_period_cache()
    if not payload:
        return {
            "source_status": source_status,
            "freshness_status": _freshness_from_status(source_status),
            "note": note,
            "totals": {
                "operating_expenses": Decimal("0"),
                "expense_open_question_count": 0,
                "expense_open_question_amount": Decimal("0"),
                "operating_expense_movement_count": 0,
                "operating_expense_review_count": 0,
            },
            "breakdown": [],
            "open_questions": [],
        }

    cache_period = payload.get("period") if isinstance(payload.get("period"), dict) else {}
    cache_from = _as_date(cache_period.get("date_from"))
    cache_to = _as_date(cache_period.get("date_to"))
    period_outside_cache = bool(
        cache_from is None or cache_to is None or date_from < cache_from or date_to > cache_to
    )
    rows_source = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    rows = [
        row
        for row in rows_source
        if isinstance(row, dict) and _date_in_range(row, date_from=date_from, date_to=date_to)
    ]
    classified_rows = [row for row in rows if row.get("profit_loss_class")]
    if rows and not classified_rows:
        return {
            "source_status": "source_missing",
            "freshness_status": "missing",
            "note": "Кэш ДДС еще не содержит классификацию расходов для ОПУ.",
            "totals": {
                "operating_expenses": Decimal("0"),
                "expense_open_question_count": 0,
                "expense_open_question_amount": Decimal("0"),
                "operating_expense_movement_count": 0,
                "operating_expense_review_count": 0,
            },
            "breakdown": [],
            "open_questions": [],
        }
    if not rows:
        effective_status = str(payload.get("source_status") or source_status or "ready")
        if effective_status == "ready":
            effective_status = "source_missing"
        return {
            "source_status": effective_status,
            "freshness_status": _freshness_from_status(effective_status),
            "note": "В кэше ДДС нет строк за выбранный период.",
            "totals": {
                "operating_expenses": Decimal("0"),
                "expense_open_question_count": 0,
                "expense_open_question_amount": Decimal("0"),
                "operating_expense_movement_count": 0,
                "operating_expense_review_count": 0,
            },
            "breakdown": [],
            "open_questions": [],
        }

    expense_buckets: dict[str, dict[str, Any]] = {}
    question_buckets: dict[str, dict[str, Any]] = {}
    for row in classified_rows:
        row_class = str(row.get("profit_loss_class") or "")
        if row_class == "operating_expense":
            key = str(row.get("profit_loss_line_key") or row.get("dds_subgroup") or "other")
            if key not in expense_buckets:
                expense_buckets[key] = {
                    "key": key,
                    "label": str(
                        row.get("profit_loss_line_label") or row.get("article_name") or key
                    ),
                    "amount": Decimal("0"),
                    "movement_count": 0,
                    "review_count": 0,
                    "source_status": "ready",
                    "recognition_method": str(
                        row.get("profit_loss_recognition_method") or "cashflow_fallback"
                    ),
                    "meta": {
                        "dds_subgroups": set(),
                        "article_keys": set(),
                    },
                }
            item = expense_buckets[key]
            item["amount"] += _decimal(row.get("outflow_amount"))
            item["movement_count"] += int(row.get("movement_count") or 0)
            item["review_count"] += int(row.get("review_count") or 0)
            item["meta"]["dds_subgroups"].add(str(row.get("dds_subgroup") or ""))
            item["meta"]["article_keys"].add(str(row.get("article_key") or ""))
        elif row_class == "open_question":
            key = str(
                row.get("profit_loss_question_key")
                or row.get("article_key")
                or row.get("dds_subgroup")
                or "manual_review"
            )
            if key not in question_buckets:
                question_buckets[key] = {
                    "key": key,
                    "label": str(row.get("article_name") or "Статья ДДС на разбор"),
                    "amount": Decimal("0"),
                    "reason": str(
                        row.get("profit_loss_question_reason") or "Требуется правило ОПУ."
                    ),
                    "proposed_action": row.get("profit_loss_question_action"),
                    "movement_count": 0,
                    "review_count": 0,
                    "source_status": "partial",
                    "recognition_method": str(
                        row.get("profit_loss_recognition_method") or "cashflow_fallback"
                    ),
                    "meta": {
                        "article_key": row.get("article_key"),
                        "dds_group": row.get("dds_group"),
                        "dds_subgroup": row.get("dds_subgroup"),
                        "group_label": row.get("group_label"),
                        "subgroup_label": row.get("subgroup_label"),
                    },
                }
            item = question_buckets[key]
            item["amount"] += _decimal(row.get("outflow_amount"))
            item["movement_count"] += int(row.get("movement_count") or 0)
            item["review_count"] += int(row.get("review_count") or 0)

    breakdown = []
    for item in expense_buckets.values():
        meta = item["meta"]
        breakdown.append(
            ExecutiveProfitLossExpenseBreakdownRow(
                key=item["key"],
                label=item["label"],
                amount=item["amount"],
                movement_count=item["movement_count"],
                review_count=item["review_count"],
                source_status="partial" if item["review_count"] else item["source_status"],
                recognition_method=item["recognition_method"],
                meta={
                    "dds_subgroups": sorted(value for value in meta["dds_subgroups"] if value),
                    "article_keys": sorted(value for value in meta["article_keys"] if value),
                },
            )
        )

    accrual_summary = service_accrual_profit_loss_summary(
        session,
        date_from=date_from,
        date_to=date_to,
    )
    breakdown_by_key = {item.key: item for item in breakdown}
    for accrual in accrual_summary["by_line"]:
        key = str(accrual["key"])
        current = breakdown_by_key.get(key)
        cashflow_amount = current.amount if current is not None else Decimal("0")
        replaced_amount = min(
            cashflow_amount,
            _decimal(accrual["cashflow_replaced_amount"]),
        )
        recognized_amount = _decimal(accrual["recognized_amount"])
        adjusted_amount = cashflow_amount - replaced_amount + recognized_amount
        updated = ExecutiveProfitLossExpenseBreakdownRow(
            key=key,
            label=str(accrual["label"]),
            amount=adjusted_amount,
            movement_count=current.movement_count if current is not None else 0,
            review_count=current.review_count if current is not None else 0,
            source_status=str(accrual["source_status"]),
            recognition_method="accrual",
            cashflow_amount=cashflow_amount,
            recognized_amount=recognized_amount,
            adjustment_amount=recognized_amount - replaced_amount,
            estimated_count=int(accrual["estimated_count"]),
            meta={
                **(current.meta if current is not None else {}),
                "service_accrual_entry_count": int(accrual["entry_count"]),
                "cashflow_replaced_amount": str(replaced_amount),
            },
        )
        if current is not None:
            breakdown[breakdown.index(current)] = updated
        else:
            breakdown.append(updated)
        breakdown_by_key[key] = updated
    breakdown.sort(key=lambda item: item.amount, reverse=True)

    open_questions = [
        ExecutiveProfitLossOpenQuestion(
            key=item["key"],
            label=item["label"],
            amount=item["amount"],
            reason=item["reason"],
            proposed_action=item["proposed_action"],
            movement_count=item["movement_count"],
            review_count=item["review_count"],
            source_status=item["source_status"],
            recognition_method=item["recognition_method"],
            meta=item["meta"],
        )
        for item in question_buckets.values()
    ]
    open_questions.sort(key=lambda item: item.amount, reverse=True)

    operating_expenses = sum((item.amount for item in breakdown), Decimal("0"))
    open_question_amount = sum((item.amount for item in open_questions), Decimal("0"))
    review_count = sum((item.review_count for item in breakdown), 0)
    expense_status = str(payload.get("source_status") or source_status or "ready")
    if expense_status == "ready" and (open_questions or review_count):
        expense_status = "partial"
    if accrual_summary["source_status"] == "partial":
        expense_status = "partial"
    if period_outside_cache:
        expense_status = "stale"

    return {
        "source_status": expense_status,
        "freshness_status": _freshness_from_status(expense_status),
        "note": (
            "Расходы определены по исходящим оплатам ДДС; договоры с активными "
            "правилами заменены управленческими начислениями без двойного учета."
            + (
                f" Запрошенный период выходит за кэш "
                f"{cache_from.isoformat() if cache_from else '?'}.."
                f"{cache_to.isoformat() if cache_to else '?'}."
                if period_outside_cache
                else ""
            )
        ),
        "totals": {
            "operating_expenses": operating_expenses,
            "expense_open_question_count": len(open_questions),
            "expense_open_question_amount": open_question_amount,
            "operating_expense_movement_count": sum((item.movement_count for item in breakdown), 0),
            "operating_expense_review_count": review_count,
        },
        "breakdown": breakdown,
        "open_questions": open_questions,
    }


def _latest_receivables_date(session: Session, requested_date: date) -> date | None:
    return session.scalar(
        select(func.max(ReceivableCase.snapshot_date)).where(
            ReceivableCase.snapshot_date <= requested_date,
            ReceivableCase.segment == CASE_BUYERS,
            ReceivableCase.current_balance > 0,
        )
    )


def _receivables_drilldown_url(snapshot_date: date) -> str:
    return f"/bitrix/receivables/?date={snapshot_date.isoformat()}"


def _receivables_control_drilldown_url(snapshot_date: date) -> str:
    return f"/bitrix/receivables/?date={snapshot_date.isoformat()}&tab=folders"


def _case_effective_due_date(row: ReceivableCase) -> tuple[datetime | None, bool]:
    if row.due_date is not None:
        return row.due_date, False
    if row.planned_payment_date is not None:
        return row.planned_payment_date, False
    if row.credit_depth_days and row.credit_depth_days > 0 and row.origin_document_date:
        return row.origin_document_date + timedelta(days=row.credit_depth_days), False
    if row.origin_document_date is not None:
        return row.origin_document_date + timedelta(days=7), True
    return None, False


def _case_effective_overdue_days(row: ReceivableCase, *, as_of: date) -> tuple[int, bool]:
    due_date, needs_default = _case_effective_due_date(row)
    if row.overdue_days is not None:
        return max(int(row.overdue_days or 0), 0), needs_default
    if due_date is None:
        return 0, needs_default
    return max((as_of - due_date.date()).days, 0), needs_default


def _load_buyer_receivable_cases(
    session: Session,
    *,
    snapshot_date: date,
) -> list[ReceivableCase]:
    return (
        session.execute(
            select(ReceivableCase).where(
                ReceivableCase.snapshot_date == snapshot_date,
                ReceivableCase.segment == CASE_BUYERS,
                ReceivableCase.current_balance > 0,
            )
        )
        .scalars()
        .all()
    )


def _load_receivable_work_items(
    session: Session,
    *,
    counterparty_refs: list[str],
) -> dict[str, ReceivableWorkItem]:
    if not counterparty_refs:
        return {}
    rows = (
        session.execute(
            select(ReceivableWorkItem).where(
                ReceivableWorkItem.counterparty_ref.in_(counterparty_refs)
            )
        )
        .scalars()
        .all()
    )
    return {row.counterparty_ref: row for row in rows}


def _payment_postponed_count(row: ReceivableWorkItem | None) -> int:
    if row is None or not isinstance(row.payload, dict):
        return 0
    try:
        count = int(row.payload.get("payment_postponed_count") or 0)
    except (TypeError, ValueError):
        count = 0
    if count <= 0 and row.payload.get("payment_postponed"):
        return 1
    return max(count, 0)


def _folder_control_snapshot(
    session: Session,
    *,
    snapshot_date: date,
) -> dict[str, Any]:
    row = session.scalar(
        select(ReceivableFolderRecommendationCache).where(
            ReceivableFolderRecommendationCache.snapshot_date == snapshot_date,
            ReceivableFolderRecommendationCache.status_scope == "all",
        )
    )
    if row is None:
        return {
            "source_status": "source_missing",
            "summary": {},
            "computed_at": None,
            "report_revision": None,
        }
    return {
        "source_status": "ready",
        "raw_source_status": row.source_status,
        "summary": row.summary or {},
        "computed_at": row.computed_at,
        "report_revision": row.report_revision,
    }


def _build_receivables_block(
    session: Session,
    *,
    requested_date: date,
    access_context: ExecutiveDashboardAuthContext,
) -> ExecutiveDashboardBlock:
    latest_date = _latest_receivables_date(session, requested_date)
    masked = _mask_finance("debtors", access_context)
    if latest_date is None:
        return ExecutiveDashboardBlock(
            key="debtors",
            title="Дебиторка покупателей",
            source_status="source_missing",
            freshness_status="missing",
            summary={
                "note": "Нет buyers-среза дебиторки в receivable_case",
                "source_segment": CASE_BUYERS,
            },
            metrics=[
                _metric(
                    "total_receivable",
                    "Долг покупателей",
                    None,
                    unit="RUB",
                    masked=masked,
                    source_status="source_missing",
                ),
                _metric(
                    "total_overdue",
                    "Просрочка",
                    None,
                    unit="RUB",
                    masked=masked,
                    source_status="source_missing",
                ),
            ],
            drilldown_url=_receivables_drilldown_url(requested_date),
        )

    debt_rows = _load_buyer_receivable_cases(session, snapshot_date=latest_date)
    work_items = _load_receivable_work_items(
        session,
        counterparty_refs=[row.counterparty_ref for row in debt_rows],
    )
    total_receivable = sum((_decimal(row.current_balance) for row in debt_rows), Decimal("0"))
    overdue_days_by_ref = {
        row.counterparty_ref: _case_effective_overdue_days(row, as_of=latest_date)[0]
        for row in debt_rows
    }
    overdue_rows = [row for row in debt_rows if overdue_days_by_ref[row.counterparty_ref] > 0]
    total_overdue = sum((_decimal(row.current_balance) for row in overdue_rows), Decimal("0"))
    overdue_30 = sum(
        (
            _decimal(row.current_balance)
            for row in debt_rows
            if overdue_days_by_ref[row.counterparty_ref] >= 30
        ),
        Decimal("0"),
    )
    overdue_90 = sum(
        (
            _decimal(row.current_balance)
            for row in debt_rows
            if overdue_days_by_ref[row.counterparty_ref] >= 90
        ),
        Decimal("0"),
    )
    promised_rows = [
        row
        for row in debt_rows
        if (work_items.get(row.counterparty_ref) or None) is not None
        and work_items[row.counterparty_ref].promised_payment_date is not None
    ]
    need_call_rows = [
        row
        for row in overdue_rows
        if (work_items.get(row.counterparty_ref) is None)
        or (
            work_items[row.counterparty_ref].status not in _RECEIVABLE_CLOSED_STATUSES
            and (
                work_items[row.counterparty_ref].needs_call_today
                or work_items[row.counterparty_ref].next_action_date is None
                or work_items[row.counterparty_ref].next_action_date.date() <= latest_date
            )
        )
    ]
    need_call_today_amount = sum(
        (_decimal(row.current_balance) for row in need_call_rows),
        Decimal("0"),
    )
    managers: dict[str, Decimal] = {}
    for row in overdue_rows:
        manager = row.current_manager_name or row.origin_manager_name or "Без ответственного"
        managers[manager] = managers.get(manager, Decimal("0")) + _decimal(row.current_balance)

    source_status, freshness_status = _apply_date_freshness(
        "ready",
        requested_date=requested_date,
        source_as_of=latest_date,
    )
    return ExecutiveDashboardBlock(
        key="debtors",
        title="Дебиторка покупателей",
        source_status=source_status,
        freshness_status=freshness_status,
        as_of=latest_date,
        summary={
            "source_segment": CASE_BUYERS,
            "row_count": len(debt_rows),
            "overdue_counterparty_count": len(overdue_rows),
            "promised_payment_count": len(promised_rows),
            "need_call_today_count": len(need_call_rows),
            "need_call_today_amount": str(need_call_today_amount),
            "overdue_30_amount": str(overdue_30),
            "drilldown_label": "Открыть рабочее место дебиторки",
            "top_overdue_managers": [
                {"manager_name": manager, "amount": str(amount)}
                for manager, amount in sorted(
                    managers.items(), key=lambda item: item[1], reverse=True
                )[:5]
            ],
            "note": "Считается только buyers-сегмент: положительная дебиторка покупателей из рабочего контура pricing-service.",
        },
        metrics=[
            _metric(
                "total_receivable",
                "Долг покупателей",
                total_receivable,
                unit="RUB",
                masked=masked,
                tone="info",
            ),
            _metric(
                "total_overdue",
                "Просрочка",
                total_overdue,
                unit="RUB",
                masked=masked,
                tone="warning",
            ),
            _metric("overdue_90", "90+", overdue_90, unit="RUB", masked=masked, tone="danger"),
            _metric("customer_count", "Клиентов", len(debt_rows), tone="info"),
        ],
        drilldown_url=_receivables_drilldown_url(latest_date),
    )


def _build_receivables_control_block(
    session: Session,
    *,
    requested_date: date,
) -> ExecutiveDashboardBlock:
    latest_date = _latest_receivables_date(session, requested_date)
    if latest_date is None:
        return ExecutiveDashboardBlock(
            key="receivables_control",
            title="Контроль дебиторки",
            source_status="source_missing",
            freshness_status="missing",
            summary={
                "note": "Нет buyers-среза, поэтому очередь контроля дебиторки не рассчитана",
            },
            metrics=[
                _metric("need_call_today_count", "К звонку", None, source_status="source_missing"),
                _metric(
                    "folder_needs_review_count",
                    "Папки к проверке",
                    None,
                    source_status="source_missing",
                ),
            ],
            drilldown_url=_receivables_control_drilldown_url(requested_date),
        )

    cases = _load_buyer_receivable_cases(session, snapshot_date=latest_date)
    work_items = _load_receivable_work_items(
        session,
        counterparty_refs=[row.counterparty_ref for row in cases],
    )
    overdue_days_by_ref = {
        row.counterparty_ref: _case_effective_overdue_days(row, as_of=latest_date) for row in cases
    }
    need_call_count = 0
    no_phone_count = 0
    credit_depth_default_count = 0
    payment_postponed_count = 0
    for row in cases:
        item = work_items.get(row.counterparty_ref)
        overdue_days, needs_default = overdue_days_by_ref[row.counterparty_ref]
        if needs_default:
            credit_depth_default_count += 1
        if item is not None and item.phone_status == "missing":
            no_phone_count += 1
        if item is None or (
            item.status not in _RECEIVABLE_CLOSED_STATUSES
            and (
                item.needs_call_today
                or item.next_action_date is None
                or item.next_action_date.date() <= latest_date
            )
            and overdue_days > 0
        ):
            need_call_count += 1
        payment_postponed_count += _payment_postponed_count(item)

    folder_control = _folder_control_snapshot(session, snapshot_date=latest_date)
    folder_summary = folder_control["summary"]
    folder_needs_review = int(folder_summary.get("needs_review_count") or 0)
    folder_move_recommended = int(folder_summary.get("move_recommended_count") or 0)
    source_status, freshness_status = _apply_date_freshness(
        "ready",
        requested_date=requested_date,
        source_as_of=latest_date,
    )
    if freshness_status == "fresh" and folder_control["source_status"] == "source_missing":
        source_status = "partial"
        freshness_status = "partial"

    metrics = [
        _metric("need_call_today_count", "К звонку", need_call_count, tone="warning"),
        _metric(
            "folder_needs_review_count",
            "Папки к проверке",
            folder_needs_review,
            tone="warning",
        ),
        _metric(
            "credit_depth_default_count",
            "Срок 7 дней",
            credit_depth_default_count,
            tone="warning",
        ),
        _metric("no_phone_count", "Без телефона", no_phone_count, tone="danger"),
    ]
    if payment_postponed_count > 0:
        metrics.append(
            _metric(
                "payment_postponed_count",
                "Переносы оплаты",
                payment_postponed_count,
                tone="info",
            )
        )
    if folder_move_recommended > 0:
        metrics.append(
            _metric(
                "folder_move_recommended_count",
                "Готово к переносу",
                folder_move_recommended,
                tone="info",
            )
        )

    return ExecutiveDashboardBlock(
        key="receivables_control",
        title="Контроль дебиторки",
        source_status=source_status,
        freshness_status=freshness_status,
        as_of=latest_date,
        summary={
            "note": (
                "Блок показывает очередь действий и качество данных. "
                "Запись в 1C не выполняется: будущие команды бота идут только через dry-run/apply файлового обмена."
            ),
            "folder_control_source_status": folder_control["source_status"],
            "folder_control_raw_source_status": folder_control.get("raw_source_status"),
            "folder_control_report_revision": folder_control.get("report_revision"),
            "folder_control_computed_at": folder_control.get("computed_at"),
            "folder_control_summary": folder_summary,
            "payment_postponed_count": payment_postponed_count,
            "folder_move_recommended_count": folder_move_recommended,
            "drilldown_label": "Открыть контроль папок",
        },
        metrics=metrics,
        drilldown_url=_receivables_control_drilldown_url(latest_date),
    )


def _finance_section(
    payload: dict[str, Any] | None,
    key: str,
) -> dict[str, Any]:
    if not payload:
        return {}
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _finance_subsection(section: dict[str, Any], key: str) -> dict[str, Any]:
    value = section.get(key)
    return value if isinstance(value, dict) else {}


def _source_available_for_metric(source_status: str) -> bool:
    return source_status not in {"source_missing", "source_error"}


def _combine_source_status_strings(statuses: list[str]) -> str:
    normalized = {status or "source_missing" for status in statuses}
    if "source_error" in normalized:
        return "source_error"
    if normalized == {"ready"}:
        return "ready"
    if normalized & {"ready", "partial"}:
        return "partial"
    if "stale" in normalized:
        return "stale"
    return "source_missing"


def _build_money_today_block(
    finance_payload: dict[str, Any] | None,
    *,
    access_context: ExecutiveDashboardAuthContext,
) -> ExecutiveDashboardBlock:
    section = _finance_section(finance_payload, "money_today")
    cash_position = _finance_subsection(section, "cash_position")
    cashflow_today = _finance_subsection(section, "cashflow_today")
    if not cashflow_today and section:
        incoming = _decimal(section.get("expected_incoming"))
        outgoing = _decimal(section.get("expected_outgoing"))
        cashflow_today = {
            "source_status": section.get("source_status") or "ready",
            "freshness_status": section.get("freshness_status") or "fresh",
            "as_of": section.get("as_of"),
            "inflow_amount": incoming,
            "outflow_amount": outgoing,
            "net_amount": incoming - outgoing,
            "movement_count": section.get("movement_count") or 0,
            "review_count": section.get("review_count") or 0,
            "internal_transfer_count": section.get("internal_transfer_count") or 0,
            "note": "Legacy cashflow summary converted to cashflow_today.",
        }
    cash_position_status = str(cash_position.get("source_status") or "source_missing")
    cashflow_status = str(cashflow_today.get("source_status") or "source_missing")
    source_status = _combine_source_status_strings([cash_position_status, cashflow_status])
    masked = _mask_finance("money_today", access_context)
    metrics: list[ExecutiveDashboardMetric] = []
    if _source_available_for_metric(cash_position_status):
        total_balance = cash_position.get("total_balance_rub") or cash_position.get("total_balance")
        bank_balance = cash_position.get("bank_balance_total_rub") or cash_position.get(
            "bank_balance_total"
        )
        cashbox_balance = cash_position.get("cashbox_balance_total_rub") or cash_position.get(
            "cashbox_balance_total"
        )
        metrics.extend(
            [
                _metric(
                    "cash_position_total_balance",
                    "Всего в ₽",
                    _decimal(total_balance),
                    unit="RUB",
                    masked=masked,
                    source_status=cash_position_status,
                ),
                _metric(
                    "cash_position_bank_balance_total",
                    "Расчетные счета",
                    _decimal(bank_balance),
                    unit="RUB",
                    masked=masked,
                    source_status=cash_position_status,
                ),
                _metric(
                    "cash_position_cashbox_balance_total",
                    "Кассы",
                    _decimal(cashbox_balance),
                    unit="RUB",
                    masked=masked,
                    source_status=cash_position_status,
                ),
            ]
        )
        card_balance = _decimal(cash_position.get("card_balance_total"))
        if card_balance:
            metrics.append(
                _metric(
                    "cash_position_card_balance_total",
                    "Карты / эквайринг",
                    card_balance,
                    unit="RUB",
                    masked=masked,
                    source_status=cash_position_status,
                )
            )
        other_balance = _decimal(cash_position.get("other_balance_total"))
        if other_balance:
            metrics.append(
                _metric(
                    "cash_position_other_balance_total",
                    "Прочее",
                    other_balance,
                    unit="RUB",
                    masked=masked,
                    source_status=cash_position_status,
                )
            )
        foreign_balance = _decimal(cash_position.get("foreign_balance_total"))
        if foreign_balance:
            metrics.append(
                _metric(
                    "cash_position_foreign_balance_total",
                    "Валюта в ₽",
                    foreign_balance,
                    unit="RUB",
                    masked=masked,
                    tone="info",
                    source_status=cash_position_status,
                )
            )
        negative_balance = _decimal(cash_position.get("negative_balance_total"))
        if negative_balance:
            metrics.append(
                _metric(
                    "cash_position_negative_balance_total",
                    "Минусы / переоценка",
                    negative_balance,
                    unit="RUB",
                    masked=masked,
                    tone="warning",
                    source_status=cash_position_status,
                )
            )
        currency_count = int(cash_position.get("currency_count") or 0)
        if currency_count:
            metrics.append(
                _metric(
                    "cash_position_currency_count",
                    "Валют",
                    currency_count,
                    source_status=cash_position_status,
                )
            )
    if _source_available_for_metric(cashflow_status):
        metrics.extend(
            [
                _metric(
                    "cashflow_inflow_amount",
                    "Поступило",
                    _decimal(cashflow_today.get("inflow_amount")),
                    unit="RUB",
                    masked=masked,
                    tone="info",
                    source_status=cashflow_status,
                ),
                _metric(
                    "cashflow_outflow_amount",
                    "Списано",
                    _decimal(cashflow_today.get("outflow_amount")),
                    unit="RUB",
                    masked=masked,
                    tone="warning",
                    source_status=cashflow_status,
                ),
                _metric(
                    "cashflow_net_amount",
                    "Net ДДС",
                    _decimal(cashflow_today.get("net_amount")),
                    unit="RUB",
                    masked=masked,
                    tone="info",
                    source_status=cashflow_status,
                ),
                _metric(
                    "cashflow_movement_count",
                    "Движений",
                    int(cashflow_today.get("movement_count") or 0),
                    source_status=cashflow_status,
                ),
            ]
        )
        review_count = int(cashflow_today.get("review_count") or 0)
        if review_count:
            metrics.append(
                _metric(
                    "cashflow_review_count",
                    "Строк на проверку",
                    review_count,
                    tone="warning",
                    source_status=cashflow_status,
                )
            )
        internal_transfer_count = int(cashflow_today.get("internal_transfer_count") or 0)
        if internal_transfer_count:
            metrics.append(
                _metric(
                    "cashflow_internal_transfer_count",
                    "Внутренние переводы",
                    internal_transfer_count,
                    tone="warning",
                    source_status=cashflow_status,
                )
            )
    acquiring_pending = _decimal(section.get("acquiring_pending"))
    if acquiring_pending:
        metrics.append(
            _metric(
                "acquiring_pending",
                "Эквайринг в ожидании",
                acquiring_pending,
                unit="RUB",
                masked=masked,
                tone="warning",
                source_status=source_status,
            )
        )
    return ExecutiveDashboardBlock(
        key="money_today",
        title="Деньги / ДДС",
        source_status=source_status,
        freshness_status=_freshness_from_status(source_status),
        as_of=_as_date(
            cash_position.get("as_of")
            or cashflow_today.get("as_of")
            or section.get("as_of")
            or (finance_payload or {}).get("as_of")
        ),
        summary={
            "note": section.get("note")
            or "Источник mm-compensation пока не отдал готовый money snapshot",
            "snapshot_path": section.get("snapshot_path"),
            "cash_position_source_status": cash_position_status,
            "cash_position_freshness_status": cash_position.get("freshness_status"),
            "cash_position_as_of": cash_position.get("as_of"),
            "cash_position_note": cash_position.get("note")
            or cash_position.get("validation_note")
            or "Остатки еще не подключены как validated source.",
            "cash_position_breakdown_by_kind": cash_position.get("breakdown_by_kind") or [],
            "cash_position_breakdown_by_currency": cash_position.get("breakdown_by_currency") or [],
            "cash_position_currency_count": cash_position.get("currency_count"),
            "cash_position_foreign_balance_total": cash_position.get("foreign_balance_total"),
            "cash_position_negative_balance_total": cash_position.get("negative_balance_total"),
            "cashflow_today_source_status": cashflow_status,
            "cashflow_today_freshness_status": cashflow_today.get("freshness_status"),
            "cashflow_today_as_of": cashflow_today.get("as_of"),
            "cashflow_today_note": cashflow_today.get("note"),
            "metric_groups": {
                "cash_position": "Остатки сейчас",
                "cashflow_today": "ДДС сегодня",
                "control": "Контроль",
            },
        },
        metrics=metrics,
        drilldown_url=section.get("drilldown_url"),
    )


def _load_profit_loss_rows(
    session: Session,
    *,
    date_from: date,
    date_to: date,
) -> list[OneCSalesDailyKpi]:
    return (
        session.execute(
            select(OneCSalesDailyKpi)
            .where(
                OneCSalesDailyKpi.sales_date >= date_from,
                OneCSalesDailyKpi.sales_date <= date_to,
            )
            .order_by(OneCSalesDailyKpi.sales_date.asc())
        )
        .scalars()
        .all()
    )


def _load_sales_rows(
    session: Session,
    *,
    date_from: date,
    date_to: date,
    store_ref: str | None = None,
    manager_ref: str | None = None,
) -> list[OneCSalesDailyKpi]:
    query = select(OneCSalesDailyKpi).where(
        OneCSalesDailyKpi.sales_date >= date_from,
        OneCSalesDailyKpi.sales_date <= date_to,
    )
    if store_ref:
        query = query.where(OneCSalesDailyKpi.store_ref == store_ref)
    if manager_ref:
        query = query.where(OneCSalesDailyKpi.manager_ref == manager_ref)
    return session.execute(query.order_by(OneCSalesDailyKpi.sales_date.asc())).scalars().all()


def _profit_loss_margin(revenue: Decimal, gross_profit: Decimal) -> Decimal | None:
    ratio = _safe_ratio(gross_profit, revenue)
    return None if ratio is None else ratio


def _profit_loss_totals(rows: list[OneCSalesDailyKpi]) -> dict[str, Decimal | int | None]:
    revenue = sum((_decimal(row.revenue) for row in rows), Decimal("0"))
    cost_of_sales = sum((_decimal(row.cost_of_sales) for row in rows), Decimal("0"))
    sales_count = sum((_decimal(row.sales_count) for row in rows), Decimal("0"))
    gross_profit = revenue - cost_of_sales
    gross_margin_pct = _profit_loss_margin(revenue, gross_profit)
    return {
        "revenue": revenue,
        "cost_of_sales": cost_of_sales,
        "gross_profit": gross_profit,
        "sales_count": sales_count,
        "row_count": len(rows),
        "gross_margin_pct": gross_margin_pct,
        "missing_expense_line_count": 4,
    }


def _profit_loss_breakdown_row(
    *,
    key: str,
    label: str,
    rows: list[OneCSalesDailyKpi],
    meta: dict[str, Any] | None = None,
) -> ExecutiveProfitLossBreakdownRow:
    totals = _profit_loss_totals(rows)
    return ExecutiveProfitLossBreakdownRow(
        key=key,
        label=label,
        revenue=_decimal(totals["revenue"]),
        cost_of_sales=_decimal(totals["cost_of_sales"]),
        gross_profit=_decimal(totals["gross_profit"]),
        sales_count=_decimal(totals["sales_count"]),
        row_count=int(totals["row_count"] or 0),
        gross_margin_pct=totals["gross_margin_pct"],
        meta=meta or {},
    )


def _profit_loss_daily(rows: list[OneCSalesDailyKpi]) -> list[ExecutiveProfitLossDailyRow]:
    buckets: dict[date, list[OneCSalesDailyKpi]] = defaultdict(list)
    for row in rows:
        buckets[row.sales_date].append(row)
    return [
        ExecutiveProfitLossDailyRow(
            business_date=business_date,
            **_profit_loss_breakdown_row(
                key=business_date.isoformat(),
                label=business_date.isoformat(),
                rows=bucket_rows,
            ).model_dump(),
        )
        for business_date, bucket_rows in sorted(buckets.items())
    ]


def _profit_loss_dimension(
    rows: list[OneCSalesDailyKpi],
    *,
    key_attr: str,
    label_attr: str,
    fallback_label: str,
    limit: int = 12,
) -> list[ExecutiveProfitLossBreakdownRow]:
    buckets: dict[str, list[OneCSalesDailyKpi]] = defaultdict(list)
    labels: dict[str, str] = {}
    for row in rows:
        raw_key = getattr(row, key_attr) or ""
        raw_label = getattr(row, label_attr) or ""
        key = str(raw_key or fallback_label)
        label = str(raw_label or fallback_label)
        buckets[key].append(row)
        labels[key] = label

    result = [
        _profit_loss_breakdown_row(
            key=key,
            label=labels.get(key) or fallback_label,
            rows=bucket_rows,
            meta={key_attr: key},
        )
        for key, bucket_rows in buckets.items()
    ]
    result.sort(key=lambda item: item.gross_profit, reverse=True)
    return result[:limit]


def _profit_loss_lines(
    totals: dict[str, Decimal | int | None],
    *,
    source_status: str,
    expense_source_status: str,
) -> list[ExecutiveProfitLossLineItem]:
    gross_profit = _decimal(totals.get("gross_profit"))
    operating_expenses = totals.get("operating_expenses")
    operating_profit = totals.get("operating_profit")
    expense_open_question_count = int(totals.get("expense_open_question_count") or 0)
    expense_note = "Расходы оплачены в периоде и определены по статьям ДДС."
    if expense_source_status == "partial" and expense_open_question_count:
        expense_note = f"Есть открытые вопросы по {expense_open_question_count} статьям ДДС."
    elif expense_source_status != "ready":
        expense_note = "Расходы по ДДС пока не подключены для выбранного периода."
    return [
        ExecutiveProfitLossLineItem(
            key="revenue",
            label="Выручка",
            amount=_decimal(totals.get("revenue")),
            line_type="income",
            tone="info",
            source_status=source_status,
        ),
        ExecutiveProfitLossLineItem(
            key="cost_of_sales",
            label="Себестоимость продаж",
            amount=-_decimal(totals.get("cost_of_sales")),
            line_type="expense",
            tone="warning",
            source_status=source_status,
        ),
        ExecutiveProfitLossLineItem(
            key="gross_profit",
            label="Валовая прибыль",
            amount=gross_profit,
            line_type="subtotal",
            tone="info" if gross_profit >= 0 else "danger",
            source_status=source_status,
        ),
        ExecutiveProfitLossLineItem(
            key="operating_expenses",
            label="Операционные расходы по ДДС",
            amount=(
                -_decimal(operating_expenses)
                if operating_expenses is not None
                and expense_source_status not in {"source_missing", "source_error"}
                else None
            ),
            line_type="expense",
            tone="warning",
            source_status=expense_source_status,
            note=expense_note,
        ),
        ExecutiveProfitLossLineItem(
            key="operating_profit",
            label="Операционная прибыль",
            amount=(
                _decimal(operating_profit)
                if operating_profit is not None
                and expense_source_status not in {"source_missing", "source_error"}
                else None
            ),
            line_type="subtotal",
            tone=(
                "info"
                if operating_profit is not None and _decimal(operating_profit) >= 0
                else "danger"
            ),
            source_status=expense_source_status,
            note="Валовая прибыль минус расходы по оплатам ДДС.",
        ),
        ExecutiveProfitLossLineItem(
            key="other_income_expenses",
            label="Прочие доходы / расходы",
            amount=None,
            line_type="metric",
            source_status="source_missing",
            note="Не считаем в v1 до утверждения правил по налогам, кредитам и прочим статьям.",
        ),
        ExecutiveProfitLossLineItem(
            key="net_profit",
            label="Чистая прибыль",
            amount=None,
            line_type="total",
            source_status="source_missing",
            note="Пока не считаем без расходов, налогов и прочих статей.",
        ),
    ]


def build_executive_profit_loss_period_response(
    session: Session,
    *,
    date_from: date,
    date_to: date,
) -> ExecutiveProfitLossPeriodResponse:
    rows = _load_profit_loss_rows(session, date_from=date_from, date_to=date_to)
    if not rows:
        return ExecutiveProfitLossPeriodResponse(
            date_from=date_from,
            date_to=date_to,
            generated_at=datetime.now(UTC),
            source_status="source_missing",
            freshness_status="missing",
            note="В onec_sales_daily_kpi нет строк продаж за выбранный период.",
            lines=_profit_loss_lines(
                {
                    "revenue": Decimal("0"),
                    "cost_of_sales": Decimal("0"),
                    "gross_profit": Decimal("0"),
                    "sales_count": Decimal("0"),
                    "row_count": 0,
                    "gross_margin_pct": None,
                    "missing_expense_line_count": 4,
                },
                source_status="source_missing",
                expense_source_status="source_missing",
            ),
            expense_source_status="source_missing",
            filters={"source_table": "onec_sales_daily_kpi"},
        )

    latest_sales_date = max(row.sales_date for row in rows)
    sales_source_status, _ = _apply_date_freshness(
        "ready",
        requested_date=date_to,
        source_as_of=latest_sales_date,
    )
    expense_data = _profit_loss_expenses_from_cashflow_cache(
        session=session,
        date_from=date_from,
        date_to=date_to,
    )
    expense_source_status = str(expense_data["source_status"])
    source_status = _combine_profit_loss_status(sales_source_status, expense_source_status)
    freshness_status = _freshness_from_status(source_status)
    totals = _profit_loss_totals(rows)
    gross_margin_pct = totals["gross_margin_pct"]
    expense_totals = expense_data["totals"]
    operating_expenses = _decimal(expense_totals.get("operating_expenses"))
    operating_profit: Decimal | None = None
    if expense_source_status not in {"source_missing", "source_error"}:
        operating_profit = _decimal(totals.get("gross_profit")) - operating_expenses
    totals.update(
        {
            "operating_expenses": operating_expenses,
            "operating_profit": operating_profit,
            "expense_open_question_count": int(
                expense_totals.get("expense_open_question_count") or 0
            ),
            "expense_open_question_amount": _decimal(
                expense_totals.get("expense_open_question_amount")
            ),
            "operating_expense_movement_count": int(
                expense_totals.get("operating_expense_movement_count") or 0
            ),
            "operating_expense_review_count": int(
                expense_totals.get("operating_expense_review_count") or 0
            ),
            "missing_expense_line_count": 2
            + (1 if expense_source_status in {"source_missing", "source_error"} else 0),
        }
    )
    note = (
        "ОПУ строится по продажам 1С и расходам ДДС. Для договоров с утверждёнными "
        "правилами кассовый расход заменён начислением без двойного учёта."
    )
    if expense_data.get("note"):
        note = f"{note} {expense_data['note']}"
    if sales_source_status == "stale":
        note = f"Последняя строка продаж в периоде: {latest_sales_date.isoformat()}. {note}"

    ratios = [
        ExecutiveProfitLossRatio(
            key="gross_margin_pct",
            label="Валовая маржа",
            value=gross_margin_pct,
            unit="percent",
            tone=(
                "warning"
                if gross_margin_pct is not None and gross_margin_pct < Decimal("0.20")
                else "neutral"
            ),
            note="Валовая прибыль / выручка.",
        ),
    ]
    operating_margin_pct = (
        _profit_loss_margin(_decimal(totals.get("revenue")), operating_profit)
        if operating_profit is not None
        else None
    )
    if operating_profit is not None:
        ratios.append(
            ExecutiveProfitLossRatio(
                key="operating_margin_pct",
                label="Операционная маржа",
                value=operating_margin_pct,
                unit="percent",
                tone=(
                    "warning"
                    if operating_margin_pct is not None and operating_margin_pct < Decimal("0.10")
                    else "neutral"
                ),
                note="Операционная прибыль / выручка.",
            )
        )

    return ExecutiveProfitLossPeriodResponse(
        date_from=date_from,
        date_to=date_to,
        generated_at=datetime.now(UTC),
        source_status=source_status,
        freshness_status=freshness_status,
        note=note,
        totals=totals,
        ratios=ratios,
        lines=_profit_loss_lines(
            totals,
            source_status=sales_source_status,
            expense_source_status=expense_source_status,
        ),
        daily=_profit_loss_daily(rows),
        by_store=_profit_loss_dimension(
            rows,
            key_attr="store_ref",
            label_attr="store_name",
            fallback_label="Без магазина",
        ),
        by_manager=_profit_loss_dimension(
            rows,
            key_attr="manager_ref",
            label_attr="manager_name",
            fallback_label="Без менеджера",
        ),
        expense_source_status=expense_source_status,
        expense_breakdown=expense_data["breakdown"],
        expense_open_questions=expense_data["open_questions"],
        filters={
            "source_table": "onec_sales_daily_kpi",
            "expense_source_table": "cashflow_period_cache.profit_loss_expenses",
            "available_date_from": min(row.sales_date for row in rows).isoformat(),
            "available_date_to": latest_sales_date.isoformat(),
        },
    )


def _month_bounds(value: date) -> tuple[date, date]:
    date_from = value.replace(day=1)
    if date_from.month == 12:
        next_month = date(date_from.year + 1, 1, 1)
    else:
        next_month = date(date_from.year, date_from.month + 1, 1)
    return date_from, next_month - timedelta(days=1)


def _add_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    return date(month_index // 12, month_index % 12 + 1, 1)


def _same_day_last_year(value: date) -> date:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)


def _sales_totals(rows: list[OneCSalesDailyKpi]) -> dict[str, Decimal | int | None]:
    totals = _profit_loss_totals(rows)
    return {
        "revenue": _decimal(totals.get("revenue")),
        "gross_profit": _decimal(totals.get("gross_profit")),
        "gross_margin_pct": totals.get("gross_margin_pct"),
        "sales_count": _decimal(totals.get("sales_count")),
    }


def _sales_rows_by_date(rows: list[OneCSalesDailyKpi]) -> dict[date, Decimal]:
    values: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in rows:
        values[row.sales_date] += _decimal(row.revenue)
    return values


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _sales_breakdown(
    rows: list[OneCSalesDailyKpi],
    *,
    key_attr: str,
    label_attr: str,
    fallback_label: str,
) -> list[ExecutiveSalesBreakdownRow]:
    buckets: dict[str, list[OneCSalesDailyKpi]] = defaultdict(list)
    labels: dict[str, str] = {}
    for row in rows:
        raw_key = getattr(row, key_attr) or ""
        raw_label = getattr(row, label_attr) or ""
        key = str(raw_key or fallback_label)
        buckets[key].append(row)
        labels[key] = str(raw_label or fallback_label)
    result = []
    for key, bucket_rows in buckets.items():
        totals = _sales_totals(bucket_rows)
        result.append(
            ExecutiveSalesBreakdownRow(
                key=key,
                label=labels[key],
                revenue=_decimal(totals["revenue"]),
                gross_profit=_decimal(totals["gross_profit"]),
                sales_count=_decimal(totals["sales_count"]),
                gross_margin_pct=totals["gross_margin_pct"],
                meta={key_attr: key},
            )
        )
    return sorted(result, key=lambda item: item.revenue, reverse=True)


def _sales_filter_options(
    rows: list[OneCSalesDailyKpi],
    *,
    key_attr: str,
    label_attr: str,
    fallback_label: str,
) -> list[ExecutiveSalesFilterOption]:
    values: dict[str, str] = {}
    for row in rows:
        raw_key = getattr(row, key_attr)
        if raw_key in (None, ""):
            continue
        key = str(raw_key)
        values[key] = str(getattr(row, label_attr) or fallback_label)
    return [
        ExecutiveSalesFilterOption(key=key, label=label)
        for key, label in sorted(values.items(), key=lambda item: item[1].casefold())
    ]


def _is_full_calendar_month(date_from: date, date_to: date) -> bool:
    month_from, month_to = _month_bounds(date_from)
    return date_from == month_from and date_to == month_to


def _sales_plan_month_contract(
    *, date_from: date, date_to: date
) -> tuple[dict[str, Any] | None, str, str | None]:
    if not _is_full_calendar_month(date_from, date_to):
        return (
            None,
            "not_applicable",
            "Плановые показатели доступны только в режиме «Месяц».",
        )
    payload, payload_status, source_note = _load_sales_plan_snapshot()
    if payload is None:
        return None, payload_status, source_note
    months = payload.get("months")
    if not isinstance(months, list):
        return None, "source_error", "sales plan snapshot months must be an array"
    month_key = date_from.strftime("%Y-%m")
    matches = [
        item for item in months if isinstance(item, dict) and item.get("period_month") == month_key
    ]
    if not matches:
        return None, "source_missing", f"План на {month_key} не утверждён."
    if len(matches) != 1:
        return None, "source_error", f"В snapshot найдено несколько frozen-планов на {month_key}."
    month = matches[0]
    month_status = str(month.get("source_status") or payload_status or "source_missing")
    note = str(month.get("note") or payload.get("note") or "").strip() or None
    return month, month_status, note


def _sales_plan_contract_rows(
    month: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    network = month.get("network")
    stores = month.get("stores")
    if not isinstance(network, dict) or not isinstance(stores, list):
        raise ValueError("sales plan month must contain network and stores")
    store_map: dict[str, dict[str, Any]] = {}
    for row in stores:
        if not isinstance(row, dict):
            raise ValueError("sales plan store row must be an object")
        key = str(row.get("scope_key") or "").strip()
        if not key or key in store_map:
            raise ValueError("sales plan store scope_key is empty or duplicated")
        store_map[key] = row
    if not store_map:
        raise ValueError("sales plan month has no stores")
    return network, store_map


def _sales_plan_margin_ratio(row: dict[str, Any] | None) -> Decimal | None:
    if not row:
        return None
    value = _optional_decimal(row.get("approved_margin_pct"))
    return None if value is None else value / Decimal("100")


def _sales_plan_revenue(row: dict[str, Any] | None) -> Decimal | None:
    return None if not row else _optional_decimal(row.get("approved_revenue"))


def _sales_plan_gross_profit(row: dict[str, Any] | None) -> Decimal | None:
    return None if not row else _optional_decimal(row.get("approved_gross_profit"))


def _sales_target_for_rows(
    rows: list[OneCSalesDailyKpi],
    store_plans: dict[str, dict[str, Any]],
) -> tuple[Decimal | None, Decimal | None, str, str | None]:
    revenue = sum((_decimal(row.revenue) for row in rows), Decimal("0"))
    if revenue <= 0:
        return None, None, "source_missing", "Нет положительной выручки для расчёта целевой маржи."
    expected_gross_profit = Decimal("0")
    missing_store_refs: set[str] = set()
    for row in rows:
        store_ref = str(row.store_ref or "").strip()
        margin = _sales_plan_margin_ratio(store_plans.get(store_ref))
        if not store_ref or margin is None:
            missing_store_refs.add(store_ref or "Без магазина")
            continue
        expected_gross_profit += _decimal(row.revenue) * margin
    if missing_store_refs:
        labels = ", ".join(sorted(missing_store_refs))
        return (
            None,
            None,
            "partial",
            f"Не все продажи сопоставлены с frozen-планом магазинов: {labels}.",
        )
    return expected_gross_profit / revenue, expected_gross_profit, "ready", None


def _sales_diagnostic(
    key: str,
    *,
    value: Decimal | int | None,
    unit: str,
    source_status: str,
    note: str | None = None,
    meta: dict[str, Any] | None = None,
) -> ExecutiveSalesDiagnosticKpi:
    return ExecutiveSalesDiagnosticKpi(
        key=key,
        value=value,
        unit=unit,
        source_status=source_status,
        note=note,
        meta=meta or {},
    )


def _empty_sales_diagnostics(status: str, note: str | None) -> list[ExecutiveSalesDiagnosticKpi]:
    return [
        _sales_diagnostic(
            "lost_gross_profit_margin_gap",
            value=None,
            unit="RUB",
            source_status=status,
            note=note,
        ),
        _sales_diagnostic(
            "gross_profit_per_unit",
            value=None,
            unit="RUB_PER_UNIT",
            source_status="source_missing",
            note="Нет объёма продаж.",
        ),
        _sales_diagnostic(
            "cost_per_unit",
            value=None,
            unit="RUB_PER_UNIT",
            source_status="source_missing",
            note="Нет объёма продаж.",
        ),
        _sales_diagnostic(
            "margin_gap_pp",
            value=None,
            unit="PERCENTAGE_POINT",
            source_status=status,
            note=note,
        ),
        _sales_diagnostic(
            "stores_below_plan_count",
            value=None,
            unit="COUNT",
            source_status=status,
            note=note,
        ),
        _sales_diagnostic(
            "managers_below_target_margin_count",
            value=None,
            unit="COUNT",
            source_status=status,
            note=note,
        ),
    ]


def _sales_plan_context(
    month: dict[str, Any] | None,
    *,
    plan_status: str,
    plan_note: str | None,
    scope_type: str,
    scope_key: str | None,
    plan_row: dict[str, Any] | None,
    comparison_basis: str = "not_applicable",
    comparison_revenue: Decimal | None = None,
) -> ExecutiveSalesPlanContext | None:
    if month is None:
        return None
    approved_revenue = _sales_plan_revenue(plan_row)
    attainment = (
        None
        if approved_revenue in (None, Decimal("0")) or comparison_revenue is None
        else comparison_revenue / approved_revenue
    )
    return ExecutiveSalesPlanContext(
        source_status=plan_status,
        period_month=str(month.get("period_month") or ""),
        revision_no=int(month.get("revision_no") or 0) or None,
        snapshot_id=str(month.get("snapshot_id") or "") or None,
        frozen_at=month.get("frozen_at"),
        scope_type=scope_type,
        scope_key=scope_key,
        approved_revenue=approved_revenue,
        approved_margin_pct=_sales_plan_margin_ratio(plan_row),
        approved_gross_profit=_sales_plan_gross_profit(plan_row),
        comparison_basis=comparison_basis,
        comparison_revenue=comparison_revenue,
        plan_attainment_pct=attainment,
        note=plan_note,
    )


def _sales_forecast_from_history_rows(
    history_rows: list[OneCSalesDailyKpi],
    *,
    date_to: date,
    as_of: date,
) -> tuple[dict[date, Decimal] | None, str, str | None]:
    if as_of >= date_to:
        return {}, "complete", "Месяц закрыт фактическими данными."
    if not history_rows:
        return None, "insufficient_history", "Для прогноза нужна история продаж за четыре недели."
    history_dates = {row.sales_date for row in history_rows}
    if (max(history_dates) - min(history_dates)).days < 21:
        return None, "insufficient_history", "Для прогноза нужна история продаж за четыре недели."
    history_from = as_of - timedelta(days=28)
    values_by_date = _sales_rows_by_date(history_rows)
    forecast: dict[date, Decimal] = {}
    forecast_date = as_of + timedelta(days=1)
    while forecast_date <= date_to:
        weekday_values = [
            values_by_date.get(history_from + timedelta(days=offset), Decimal("0"))
            for offset in range(28)
            if (history_from + timedelta(days=offset)).weekday() == forecast_date.weekday()
        ]
        if len(weekday_values) < 4:
            return (
                None,
                "insufficient_history",
                "Для прогноза нужна история продаж за четыре недели.",
            )
        forecast[forecast_date] = _median(weekday_values)
        forecast_date += timedelta(days=1)
    return (
        forecast,
        "ready",
        "Прогноз построен по медиане того же дня недели за четыре предыдущие недели.",
    )


def _sales_forecast(
    session: Session,
    *,
    date_to: date,
    as_of: date,
    store_ref: str | None,
    manager_ref: str | None,
) -> tuple[dict[date, Decimal] | None, str, str | None]:
    history_from = as_of - timedelta(days=28)
    history_to = as_of - timedelta(days=1)
    history_rows = _load_sales_rows(
        session,
        date_from=history_from,
        date_to=history_to,
        store_ref=store_ref,
        manager_ref=manager_ref,
    )
    return _sales_forecast_from_history_rows(
        history_rows,
        date_to=date_to,
        as_of=as_of,
    )


def _sales_store_projections(
    session: Session,
    *,
    current_rows: list[OneCSalesDailyKpi],
    store_plans: dict[str, dict[str, Any]],
    date_to: date,
    as_of: date,
    is_open_period: bool,
    selected_store_ref: str | None,
) -> tuple[dict[str, Decimal] | None, str, str | None]:
    target_keys = [selected_store_ref] if selected_store_ref else sorted(store_plans)
    if any(not key or key not in store_plans for key in target_keys):
        return None, "partial", "Не найден frozen-план выбранного магазина."
    if not selected_store_ref:
        unplanned_actual_stores = sorted(
            {
                str(row.store_ref or "Без магазина")
                for row in current_rows
                if str(row.store_ref or "") not in store_plans and _decimal(row.revenue) != 0
            }
        )
        if unplanned_actual_stores:
            return (
                None,
                "partial",
                "Не все магазины факта присутствуют во frozen-плане: "
                + ", ".join(unplanned_actual_stores),
            )
    actual_by_store: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in current_rows:
        actual_by_store[str(row.store_ref or "")] += _decimal(row.revenue)
    if not is_open_period:
        return {key: actual_by_store.get(key, Decimal("0")) for key in target_keys}, "ready", None

    history_from = as_of - timedelta(days=28)
    history_rows = _load_sales_rows(
        session,
        date_from=history_from,
        date_to=as_of - timedelta(days=1),
    )
    history_by_store: dict[str, list[OneCSalesDailyKpi]] = defaultdict(list)
    for row in history_rows:
        history_by_store[str(row.store_ref or "")].append(row)
    projections: dict[str, Decimal] = {}
    missing_forecast: list[str] = []
    for key in target_keys:
        forecast, status, _ = _sales_forecast_from_history_rows(
            history_by_store.get(key, []),
            date_to=date_to,
            as_of=as_of,
        )
        if forecast is None or status not in {"ready", "complete"}:
            missing_forecast.append(key)
            continue
        projections[key] = actual_by_store.get(key, Decimal("0")) + sum(
            forecast.values(), Decimal("0")
        )
    if missing_forecast:
        return (
            None,
            "partial",
            "Не хватает четырёхнедельной истории для прогноза магазинов: "
            + ", ".join(sorted(missing_forecast)),
        )
    return projections, "ready", None


def build_executive_sales_period_response(
    session: Session,
    *,
    date_from: date,
    date_to: date,
    store_ref: str | None = None,
    manager_ref: str | None = None,
    today: date | None = None,
) -> ExecutiveSalesPeriodResponse:
    current_day = today or date.today()
    plan_month, plan_status, plan_note = _sales_plan_month_contract(
        date_from=date_from,
        date_to=date_to,
    )
    network_plan: dict[str, Any] | None = None
    store_plans: dict[str, dict[str, Any]] = {}
    if plan_month is not None:
        try:
            network_plan, store_plans = _sales_plan_contract_rows(plan_month)
        except (ArithmeticError, TypeError, ValueError) as exc:
            plan_status = "source_error"
            plan_note = f"Frozen-план не прошёл проверку: {exc}"
            network_plan = None
            store_plans = {}
    if manager_ref:
        plan_scope_type = "manager"
        plan_scope_key = manager_ref
        scope_plan = None
        scope_plan_note = "Для менеджеров утверждается целевая маржа, но не отдельный план выручки."
    elif store_ref:
        plan_scope_type = "store"
        plan_scope_key = store_ref
        scope_plan = store_plans.get(store_ref)
        scope_plan_note = plan_note
        if plan_month is not None and scope_plan is None:
            plan_status = "partial"
            scope_plan_note = f"В frozen-плане отсутствует магазин {store_ref}."
            plan_note = scope_plan_note
    else:
        plan_scope_type = "network"
        plan_scope_key = str((network_plan or {}).get("scope_key") or "network")
        scope_plan = network_plan
        scope_plan_note = plan_note
    plan_context = _sales_plan_context(
        plan_month,
        plan_status=plan_status,
        plan_note=scope_plan_note,
        scope_type=plan_scope_type,
        scope_key=plan_scope_key,
        plan_row=scope_plan,
    )
    is_open_period = date_from <= current_day <= date_to
    actual_limit = min(current_day, date_to)
    all_rows = _load_sales_rows(session, date_from=date_from, date_to=actual_limit)
    rows = _load_sales_rows(
        session,
        date_from=date_from,
        date_to=actual_limit,
        store_ref=store_ref,
        manager_ref=manager_ref,
    )
    base_response = {
        "month": date_from.strftime("%Y-%m"),
        "date_from": date_from,
        "date_to": date_to,
        "generated_at": datetime.now(UTC),
        "plan_status": plan_status,
        "plan_note": plan_note,
        "plan": plan_context,
        "stores": _sales_filter_options(
            all_rows,
            key_attr="store_ref",
            label_attr="store_name",
            fallback_label="Без магазина",
        ),
        "managers": _sales_filter_options(
            all_rows,
            key_attr="manager_ref",
            label_attr="manager_name",
            fallback_label="Без менеджера",
        ),
        "filters": {
            "source_table": "onec_sales_daily_kpi",
            "store_ref": store_ref,
            "manager_ref": manager_ref,
            "plan_source": "sales_plan_monthly_snapshot",
        },
    }
    if not rows:
        return ExecutiveSalesPeriodResponse(
            **base_response,
            source_status="source_missing",
            freshness_status="missing",
            forecast_status="not_applicable",
            note="В onec_sales_daily_kpi нет строк продаж за выбранный период.",
            diagnostic_kpis=_empty_sales_diagnostics(plan_status, plan_note),
        )

    as_of = max(row.sales_date for row in rows)
    source_status, freshness_status = _apply_date_freshness(
        "ready",
        requested_date=actual_limit,
        source_as_of=as_of,
    )
    totals = _sales_totals(rows)
    elapsed_days = (as_of - date_from).days + 1
    previous_to = date_from - timedelta(days=1)
    previous_from = previous_to - timedelta(days=elapsed_days - 1)
    comparison = _sales_totals(
        _load_sales_rows(
            session,
            date_from=previous_from,
            date_to=previous_to,
            store_ref=store_ref,
            manager_ref=manager_ref,
        )
    )
    actual_by_date = _sales_rows_by_date(rows)
    forecast: dict[date, Decimal] | None = None
    forecast_status = "not_applicable"
    forecast_note: str | None = (
        "Прогноз не пересчитывается для периода, который уже полностью в прошлом."
    )
    if is_open_period:
        forecast, forecast_status, forecast_note = _sales_forecast(
            session,
            date_to=date_to,
            as_of=as_of,
            store_ref=store_ref,
            manager_ref=manager_ref,
        )
    projected_revenue = None
    if forecast is not None:
        projected_revenue = _decimal(totals["revenue"]) + sum(forecast.values(), Decimal("0"))
    totals["forecast_revenue_period_end"] = projected_revenue
    daily = []
    cursor = date_from
    while cursor <= date_to:
        daily.append(
            ExecutiveSalesDailyRow(
                business_date=cursor,
                actual_revenue=(
                    actual_by_date.get(cursor, Decimal("0")) if cursor <= as_of else None
                ),
                forecast_revenue=(forecast or {}).get(cursor),
            )
        )
        cursor += timedelta(days=1)
    monthly_anchor = date_to.replace(day=1)
    year_start = _add_months(monthly_anchor, -11)
    year_rows = _load_sales_rows(
        session,
        date_from=year_start,
        date_to=date_to,
        store_ref=store_ref,
        manager_ref=manager_ref,
    )
    rows_by_month: dict[date, list[OneCSalesDailyKpi]] = defaultdict(list)
    for row in year_rows:
        rows_by_month[row.sales_date.replace(day=1)].append(row)
    comparison_year_rows = _load_sales_rows(
        session,
        date_from=_same_day_last_year(year_start),
        date_to=_same_day_last_year(date_to),
        store_ref=store_ref,
        manager_ref=manager_ref,
    )
    comparison_rows_by_month: dict[date, list[OneCSalesDailyKpi]] = defaultdict(list)
    for row in comparison_year_rows:
        comparison_rows_by_month[row.sales_date.replace(day=1)].append(row)
    monthly = []
    for offset in range(12):
        month_start = _add_months(year_start, offset)
        month_totals = _sales_totals(rows_by_month.get(month_start, []))
        comparison_month_totals = _sales_totals(
            comparison_rows_by_month.get(_same_day_last_year(month_start), [])
        )
        monthly.append(
            ExecutiveSalesMonthlyRow(
                month=month_start.strftime("%Y-%m"),
                revenue=_decimal(month_totals["revenue"]),
                gross_profit=_decimal(month_totals["gross_profit"]),
                sales_count=_decimal(month_totals["sales_count"]),
                gross_margin_pct=month_totals["gross_margin_pct"],
                forecast_revenue=(
                    projected_revenue
                    if month_start == monthly_anchor and projected_revenue is not None
                    else None
                ),
                comparison_sales_count=_decimal(comparison_month_totals["sales_count"]),
            )
        )
    note = "Факт продаж 1С; суммы включают отраженные в витрине возвраты."
    if source_status == "stale":
        note = f"Последняя строка продаж: {as_of.isoformat()}. {note}"

    by_store = _sales_breakdown(
        rows,
        key_attr="store_ref",
        label_attr="store_name",
        fallback_label="Без магазина",
    )
    by_manager = _sales_breakdown(
        rows,
        key_attr="manager_ref",
        label_attr="manager_name",
        fallback_label="Без менеджера",
    )

    comparison_revenue = projected_revenue if is_open_period else _decimal(totals.get("revenue"))
    comparison_basis = "forecast" if is_open_period else "actual"
    if manager_ref:
        comparison_revenue = None
        comparison_basis = "manager_margin_only"
    plan_context = _sales_plan_context(
        plan_month,
        plan_status=plan_status,
        plan_note=scope_plan_note,
        scope_type=plan_scope_type,
        scope_key=plan_scope_key,
        plan_row=scope_plan,
        comparison_basis=comparison_basis,
        comparison_revenue=comparison_revenue,
    )

    target_margin: Decimal | None = None
    expected_gross_profit: Decimal | None = None
    target_status = plan_status
    target_note = plan_note
    if plan_status in {"ready", "partial"} and store_plans:
        target_margin, expected_gross_profit, target_status, target_note = _sales_target_for_rows(
            rows,
            store_plans,
        )
    if plan_context is not None and manager_ref and target_margin is not None:
        plan_context.approved_margin_pct = target_margin
        plan_context.note = scope_plan_note

    sales_count = _decimal(totals.get("sales_count"))
    gross_profit = _decimal(totals.get("gross_profit"))
    revenue = _decimal(totals.get("revenue"))
    cost_of_sales = revenue - gross_profit
    gross_profit_per_unit = None if sales_count == 0 else gross_profit / sales_count
    cost_per_unit = None if sales_count == 0 else cost_of_sales / sales_count
    actual_margin = _optional_decimal(totals.get("gross_margin_pct"))
    lost_gross_profit = (
        None
        if expected_gross_profit is None
        else max(expected_gross_profit - gross_profit, Decimal("0"))
    )
    margin_gap_pp = (
        None
        if actual_margin is None or target_margin is None
        else (actual_margin - target_margin) * Decimal("100")
    )

    store_projections: dict[str, Decimal] | None = None
    store_metric_status = plan_status
    store_metric_note = plan_note
    stores_below_plan: int | None = None
    stores_evaluated = 0
    if manager_ref:
        store_metric_status = "not_applicable"
        store_metric_note = "План выручки не распределён по менеджерам."
    elif plan_status in {"ready", "partial"} and store_plans:
        store_projections, store_metric_status, store_metric_note = _sales_store_projections(
            session,
            current_rows=all_rows,
            store_plans=store_plans,
            date_to=date_to,
            as_of=as_of,
            is_open_period=is_open_period,
            selected_store_ref=store_ref,
        )
        if store_projections is not None:
            selected_store_keys = [store_ref] if store_ref else sorted(store_plans)
            missing_plan_revenue = [
                key
                for key in selected_store_keys
                if _sales_plan_revenue(store_plans.get(key)) is None
            ]
            if missing_plan_revenue:
                store_metric_status = "partial"
                store_metric_note = (
                    "У магазинов отсутствует утверждённый план выручки: "
                    + ", ".join(missing_plan_revenue)
                )
            else:
                stores_evaluated = len(selected_store_keys)
                stores_below_plan = sum(
                    1
                    for key in selected_store_keys
                    if store_projections[key]
                    < (_sales_plan_revenue(store_plans.get(key)) or Decimal("0"))
                )

    managers_by_key: dict[str, list[OneCSalesDailyKpi]] = defaultdict(list)
    for row in rows:
        managers_by_key[str(row.manager_ref or "")].append(row)
    manager_targets: dict[str, tuple[Decimal, Decimal]] = {}
    manager_metric_status = plan_status
    manager_metric_note = plan_note
    managers_below_target: int | None = None
    if plan_status in {"ready", "partial"} and store_plans:
        if not managers_by_key or "" in managers_by_key:
            manager_metric_status = "partial"
            manager_metric_note = "Не у всех продаж заполнен менеджер."
        else:
            manager_failures: list[str] = []
            below_count = 0
            for key, manager_rows in managers_by_key.items():
                manager_target, _, manager_status, manager_note = _sales_target_for_rows(
                    manager_rows,
                    store_plans,
                )
                manager_actual = _optional_decimal(
                    _sales_totals(manager_rows).get("gross_margin_pct")
                )
                if manager_status != "ready" or manager_target is None or manager_actual is None:
                    manager_failures.append(f"{key}: {manager_note or manager_status}")
                    continue
                manager_targets[key] = (
                    manager_target,
                    (manager_actual - manager_target) * Decimal("100"),
                )
                if manager_actual < manager_target:
                    below_count += 1
            if manager_failures:
                manager_metric_status = "partial"
                manager_metric_note = "Не все менеджеры сопоставлены с целевой маржой."
            else:
                manager_metric_status = "ready"
                manager_metric_note = None
                managers_below_target = below_count

    for item in by_store:
        plan_row = store_plans.get(item.key)
        plan_revenue = _sales_plan_revenue(plan_row)
        projected = None if store_projections is None else store_projections.get(item.key)
        item.meta.update(
            {
                "plan_status": (
                    "ready" if plan_row is not None and plan_revenue is not None else plan_status
                ),
                "approved_revenue": plan_revenue,
                "approved_margin_pct": _sales_plan_margin_ratio(plan_row),
                "comparison_revenue": projected,
                "plan_attainment_pct": (
                    None
                    if projected is None or plan_revenue in (None, Decimal("0"))
                    else projected / plan_revenue
                ),
            }
        )
    for item in by_manager:
        manager_target = manager_targets.get(item.key)
        item.meta.update(
            {
                "plan_status": "ready" if manager_target is not None else manager_metric_status,
                "approved_margin_pct": manager_target[0] if manager_target is not None else None,
                "margin_gap_pp": manager_target[1] if manager_target is not None else None,
            }
        )

    unit_status = "ready" if sales_count != 0 else "source_missing"
    unit_note = None if sales_count != 0 else "Нет объёма продаж."
    diagnostic_kpis = [
        _sales_diagnostic(
            "lost_gross_profit_margin_gap",
            value=lost_gross_profit,
            unit="RUB",
            source_status=target_status,
            note=target_note
            or "Разница между целевой и фактической валовой прибылью на фактической выручке.",
        ),
        _sales_diagnostic(
            "gross_profit_per_unit",
            value=gross_profit_per_unit,
            unit="RUB_PER_UNIT",
            source_status=unit_status,
            note=unit_note,
        ),
        _sales_diagnostic(
            "cost_per_unit",
            value=cost_per_unit,
            unit="RUB_PER_UNIT",
            source_status=unit_status,
            note=unit_note,
        ),
        _sales_diagnostic(
            "margin_gap_pp",
            value=margin_gap_pp,
            unit="PERCENTAGE_POINT",
            source_status=target_status,
            note=target_note,
        ),
        _sales_diagnostic(
            "stores_below_plan_count",
            value=stores_below_plan,
            unit="COUNT",
            source_status=store_metric_status,
            note=store_metric_note,
            meta={
                "evaluated_count": stores_evaluated,
                "comparison_basis": comparison_basis,
            },
        ),
        _sales_diagnostic(
            "managers_below_target_margin_count",
            value=managers_below_target,
            unit="COUNT",
            source_status=manager_metric_status,
            note=manager_metric_note,
            meta={"evaluated_count": len(manager_targets)},
        ),
    ]
    base_response["plan_status"] = plan_status
    base_response["plan_note"] = plan_note
    base_response["plan"] = plan_context
    return ExecutiveSalesPeriodResponse(
        **base_response,
        as_of=as_of,
        source_status=source_status,
        freshness_status=freshness_status,
        forecast_status=forecast_status,
        note=note,
        forecast_note=forecast_note,
        totals=totals,
        comparison=comparison,
        daily=daily,
        monthly=monthly,
        diagnostic_kpis=diagnostic_kpis,
        by_store=by_store,
        by_manager=by_manager,
    )


def _build_sales_block(
    session: Session,
    *,
    requested_date: date,
    access_context: ExecutiveDashboardAuthContext,
) -> ExecutiveDashboardBlock:
    _requested_month_from, _requested_month_to = _month_bounds(requested_date)
    period = build_executive_sales_period_response(
        session,
        date_from=_requested_month_from,
        date_to=_requested_month_to,
        today=requested_date,
    )
    masked = _mask_finance("sales", access_context)
    totals = period.totals
    return ExecutiveDashboardBlock(
        key="sales",
        title="Продажи",
        source_status=period.source_status,
        freshness_status=period.freshness_status,
        as_of=period.as_of,
        summary={
            "forecast_status": period.forecast_status,
            "forecast_note": period.forecast_note,
            "source_table": "onec_sales_daily_kpi",
        },
        metrics=[
            _metric(
                "revenue",
                "Выручка с начала месяца",
                totals.get("revenue"),
                unit="RUB",
                masked=masked,
                tone="info",
                source_status=period.source_status,
            ),
            _metric(
                "forecast_revenue_period_end",
                "Прогноз выручки",
                totals.get("forecast_revenue_period_end"),
                unit="RUB",
                masked=masked,
                tone="info",
                source_status=period.source_status,
            ),
            _metric(
                "gross_margin_pct",
                "Валовая маржа",
                totals.get("gross_margin_pct"),
                unit="percent",
                masked=masked,
                tone="info",
                source_status=period.source_status,
            ),
        ],
    )


def _build_profit_loss_block(
    session: Session,
    *,
    requested_date: date,
    access_context: ExecutiveDashboardAuthContext,
) -> ExecutiveDashboardBlock:
    period = build_executive_profit_loss_period_response(
        session,
        date_from=requested_date.replace(day=1),
        date_to=requested_date,
    )
    masked = _mask_finance("profit_loss", access_context)
    metrics: list[ExecutiveDashboardMetric] = [
        _metric(
            "revenue",
            "Выручка",
            _decimal(period.totals.get("revenue")),
            unit="RUB",
            masked=masked,
            tone="info",
            source_status=period.source_status,
        ),
        _metric(
            "cost_of_sales",
            "Себестоимость",
            _decimal(period.totals.get("cost_of_sales")),
            unit="RUB",
            masked=masked,
            tone="warning",
            source_status=period.source_status,
        ),
        _metric(
            "gross_profit",
            "Валовая прибыль",
            _decimal(period.totals.get("gross_profit")),
            unit="RUB",
            masked=masked,
            tone="info",
            source_status=period.source_status,
        ),
        _metric(
            "gross_margin_pct",
            "Валовая маржа",
            period.totals.get("gross_margin_pct"),
            unit="percent",
            masked=masked,
            tone="info",
            source_status=period.source_status,
        ),
        _metric(
            "operating_expenses",
            "Операционные расходы",
            (
                _decimal(period.totals.get("operating_expenses"))
                if period.expense_source_status not in {"source_missing", "source_error"}
                else None
            ),
            unit="RUB",
            masked=masked,
            tone="warning",
            source_status=period.expense_source_status,
        ),
        _metric(
            "operating_profit",
            "Операционная прибыль",
            (
                _decimal(period.totals.get("operating_profit"))
                if period.totals.get("operating_profit") is not None
                else None
            ),
            unit="RUB",
            masked=masked,
            tone=("info" if _decimal(period.totals.get("operating_profit")) >= 0 else "danger"),
            source_status=period.expense_source_status,
        ),
    ]
    return ExecutiveDashboardBlock(
        key="profit_loss",
        title="Отчет о прибылях и убытках",
        source_status=period.source_status,
        freshness_status=period.freshness_status,
        as_of=_as_date(period.filters.get("available_date_to")) or requested_date,
        summary={
            "note": period.note,
            "period": {
                "date_from": period.date_from.isoformat(),
                "date_to": period.date_to.isoformat(),
            },
            "source_anchor": "1C: onec_sales_daily_kpi",
            "source_table": "onec_sales_daily_kpi",
            "expense_source_anchor": (
                "ДДС: cashflow_period_cache.profit_loss_expenses + "
                "pricing-service: executive_service_accrual_entry"
            ),
            "expense_source_status": period.expense_source_status,
            "expense_open_question_count": period.totals.get("expense_open_question_count"),
            "expense_open_question_amount": str(
                period.totals.get("expense_open_question_amount") or Decimal("0")
            ),
            "missing_expense_line_count": period.totals.get("missing_expense_line_count"),
        },
        metrics=metrics,
    )


def _metric_by_key(
    block: ExecutiveDashboardBlock,
    key: str,
) -> ExecutiveDashboardMetric | None:
    return next((metric for metric in block.metrics if metric.key == key), None)


def _settlement_group_amount(
    section: dict[str, Any],
    *,
    group_key: str,
    side: Literal["asset", "liability"],
) -> Decimal | None:
    for group in section.get("groups") or []:
        if isinstance(group, dict) and group.get("key") == group_key:
            # Assets and liabilities are gross balances by counterparty. They
            # must not be netted between different employees, suppliers or
            # organizations. Legacy total_payable is used only as a fallback
            # for contracts that do not yet publish both gross sides.
            liability = group.get("liability_amount", group.get("gross_payable"))
            asset = group.get("asset_amount", group.get("reverse_balance"))
            if liability not in (None, "") and asset not in (None, ""):
                return _decimal(asset if side == "asset" else liability)
            total = group.get("total_payable")
            if total in (None, ""):
                return None
            net_liability = _decimal(total)
            return (
                max(-net_liability, Decimal("0"))
                if side == "asset"
                else max(net_liability, Decimal("0"))
            )
    return None


def _balance_line(
    *,
    key: str,
    label: str,
    amount: Decimal | None,
    source_status: str,
    as_of: date | datetime | None,
    masked: bool,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "amount": None if masked or amount is None else str(amount),
        "source_status": source_status,
        "as_of": as_of.isoformat() if isinstance(as_of, (date, datetime)) else None,
        "masked": masked,
        **extra,
    }


def _load_onec_inventory_cost(as_of: date) -> tuple[OneCInventoryCostSnapshot | None, str]:
    if not get_settings().onec_database_url:
        return None, "Не настроено read-only подключение к 1С УТ 10.3"
    try:
        return fetch_onec_inventory_cost(get_onec_engine(), as_of=as_of), ""
    except DatabaseNotConfiguredError:
        return None, "Не настроено read-only подключение к 1С УТ 10.3"
    except OneCInventoryCostError as exc:
        return None, str(exc)
    except Exception:
        return None, "Не удалось прочитать стоимость товарных остатков из 1С"


def _settlement_group_signed_rows(section: dict[str, Any], *, group_key: str) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for group in section.get("groups") or []:
        if not isinstance(group, dict) or group.get("key") != group_key:
            continue
        for row in group.get("asset_counterparties") or []:
            ref = str(row.get("counterparty_ref") or "").lower()
            if ref:
                result[ref] = result.get(ref, Decimal("0")) + _decimal(row.get("asset_amount"))
        for row in group.get("counterparties") or []:
            ref = str(row.get("counterparty_ref") or "").lower()
            if ref:
                result[ref] = result.get(ref, Decimal("0")) - _decimal(row.get("payable_amount"))
        break
    return result


def _build_management_balance_block(
    session: Session,
    finance_payload: dict[str, Any] | None,
    owner_cash_control_payload: dict[str, Any] | None,
    inventory_cost: OneCInventoryCostSnapshot | None,
    inventory_note: str,
    *,
    requested_date: date,
    money_block: ExecutiveDashboardBlock,
    access_context: ExecutiveDashboardAuthContext,
) -> ExecutiveDashboardBlock:
    section = _finance_section(finance_payload, "creditors_payables")
    payables_status = str(section.get("source_status") or "source_missing")
    cash_metric = _metric_by_key(money_block, "cash_position_total_balance")
    cash_status = (
        cash_metric.source_status if cash_metric is not None else money_block.source_status
    )
    owner_status = str((owner_cash_control_payload or {}).get("source_status") or "source_missing")
    inventory_status = (
        inventory_cost.source_status if inventory_cost is not None else "source_missing"
    )
    source_status = _combine_source_status_strings(
        [cash_status, payables_status, owner_status, inventory_status]
    )
    masked = any(
        _mask_finance(block_key, access_context)
        for block_key in ("money_today", "creditors_payables")
    )

    cash_as_of = _as_date(money_block.summary.get("cash_position_as_of")) or money_block.as_of
    payables_as_of = _as_date(section.get("as_of"))
    cash_amount = (
        _decimal(cash_metric.value)
        if cash_metric is not None and _source_available_for_metric(cash_status)
        else None
    )
    supplier_receivable = _settlement_group_amount(
        section,
        group_key="suppliers",
        side="asset",
    )
    employee_receivable = _settlement_group_amount(
        section,
        group_key="employees",
        side="asset",
    )
    other_receivable = _settlement_group_amount(
        section,
        group_key="other_debtors",
        side="asset",
    )
    supplier_payable = _settlement_group_amount(
        section,
        group_key="suppliers",
        side="liability",
    )
    employee_payable = _settlement_group_amount(
        section,
        group_key="employees",
        side="liability",
    )
    other_payable = _settlement_group_amount(
        section,
        group_key="other_debtors",
        side="liability",
    )
    owner_receivable = _settlement_group_amount(
        section,
        group_key="owners",
        side="asset",
    )
    owner_payable = _settlement_group_amount(
        section,
        group_key="owners",
        side="liability",
    )
    owner_summary = (
        owner_cash_control_payload.get("summary")
        if isinstance(owner_cash_control_payload, dict)
        and isinstance(owner_cash_control_payload.get("summary"), dict)
        else {}
    )
    owner_source_available = owner_cash_control_payload is not None
    owner_as_of = _as_date((owner_cash_control_payload or {}).get("as_of"))
    owner_cash_in_transit = (
        _decimal(owner_summary.get("money_in_transit_asset")) if owner_source_available else None
    )
    owner_related_party_asset = (
        _decimal(owner_summary.get("unresolved_related_party_asset"))
        if owner_source_available
        else None
    )
    owner_related_party_liability = (
        _decimal(owner_summary.get("unresolved_related_party_liability"))
        if owner_source_available
        else None
    )
    owner_unclassified_funds = (
        _decimal(owner_summary.get("unclassified_owner_funds_liability"))
        if owner_source_available
        else None
    )
    dividends_ytd = _decimal(owner_summary.get("dividends_ytd")) if owner_source_available else None
    dividends_month = (
        _decimal(owner_summary.get("dividends_current_month")) if owner_source_available else None
    )
    dividend_warning_count = int(owner_summary.get("dividend_comment_warning_count") or 0)
    legal_entity_rows = _settlement_group_signed_rows(
        section,
        group_key="legal_entities",
    )
    accrual_adjustments = service_accrual_balance_adjustments(
        session,
        as_of=requested_date,
    )
    adjusted_legal_rows = dict(legal_entity_rows)
    for counterparty_ref, amount in accrual_adjustments["by_counterparty"].items():
        adjusted_legal_rows[counterparty_ref] = adjusted_legal_rows.get(
            counterparty_ref, Decimal("0")
        ) - _decimal(amount)
    service_advances_raw = sum(
        (max(amount, Decimal("0")) for amount in legal_entity_rows.values()),
        Decimal("0"),
    )
    service_advances_adjusted = sum(
        (max(amount, Decimal("0")) for amount in adjusted_legal_rows.values()),
        Decimal("0"),
    )
    accrued_service_liability = sum(
        (max(-amount, Decimal("0")) for amount in adjusted_legal_rows.values()),
        Decimal("0"),
    )
    accrual_amount = _decimal(accrual_adjustments["amount"])
    has_service_settlements = (
        any(
            isinstance(group, dict) and group.get("key") == "legal_entities"
            for group in section.get("groups") or []
        )
        or accrual_amount != 0
    )
    assets = [
        _balance_line(
            key="cash",
            label="Денежные средства",
            amount=cash_amount,
            source_status=cash_status,
            as_of=cash_as_of,
            masked=masked,
        ),
        _balance_line(
            key="inventory_cost",
            label="Товарные остатки по себестоимости",
            amount=inventory_cost.amount if inventory_cost is not None else None,
            source_status=inventory_status,
            as_of=inventory_cost.as_of if inventory_cost is not None else None,
            masked=masked,
            note=(inventory_cost.source_title if inventory_cost is not None else inventory_note),
            quantity=(str(inventory_cost.quantity) if inventory_cost is not None else None),
            source_row_count=(
                inventory_cost.source_row_count if inventory_cost is not None else None
            ),
            negative_cost_row_count=(
                inventory_cost.negative_cost_row_count if inventory_cost is not None else 0
            ),
            negative_cost_amount=(
                str(inventory_cost.negative_cost_amount) if inventory_cost is not None else None
            ),
        ),
        _balance_line(
            key="supplier_receivables",
            label="Дебиторка поставщиков",
            amount=supplier_receivable,
            source_status=payables_status,
            as_of=payables_as_of,
            masked=masked,
        ),
        _balance_line(
            key="employee_receivables",
            label="Дебиторка сотрудников",
            amount=employee_receivable,
            source_status=payables_status,
            as_of=payables_as_of,
            masked=masked,
        ),
        _balance_line(
            key="other_receivables",
            label="Прочие дебиторы",
            amount=other_receivable,
            source_status=payables_status,
            as_of=payables_as_of,
            masked=masked,
        ),
        _balance_line(
            key="owner_receivables",
            label="Дебиторка собственников",
            amount=owner_receivable,
            source_status=payables_status,
            as_of=payables_as_of,
            masked=masked,
        ),
        _balance_line(
            key="owner_cash_in_transit",
            label="Деньги в пути через собственника",
            amount=owner_cash_in_transit,
            source_status=owner_status,
            as_of=owner_as_of,
            masked=masked,
            note="Незакрытые исходящие переводы ПП → карта собственника → ПКО",
        ),
        _balance_line(
            key="owner_related_party_unresolved",
            label="Неразобранные расчёты со связанными сторонами",
            amount=owner_related_party_asset,
            source_status=owner_status,
            as_of=owner_as_of,
            masked=masked,
            note="Включает спорный остаток по ИП Ахмедову",
        ),
    ]
    liabilities = [
        _balance_line(
            key="suppliers",
            label="Задолженность поставщикам",
            amount=supplier_payable,
            source_status=payables_status,
            as_of=payables_as_of,
            masked=masked,
        ),
        _balance_line(
            key="employees",
            label="Задолженность сотрудникам",
            amount=employee_payable,
            source_status=payables_status,
            as_of=payables_as_of,
            masked=masked,
        ),
        _balance_line(
            key="other_settlement_liabilities",
            label="Задолженность прочим контрагентам",
            amount=other_payable,
            source_status=payables_status,
            as_of=payables_as_of,
            masked=masked,
        ),
        _balance_line(
            key="owners",
            label="Задолженность собственникам",
            amount=owner_payable,
            source_status=payables_status,
            as_of=payables_as_of,
            masked=masked,
        ),
        _balance_line(
            key="owner_funds_unclassified",
            label="Средства собственника, назначение не определено",
            amount=(
                None
                if owner_unclassified_funds is None or owner_related_party_liability is None
                else owner_unclassified_funds + owner_related_party_liability
            ),
            source_status=owner_status,
            as_of=owner_as_of,
            masked=masked,
            note="Незакрытые входящие ПКО и кредитовые остатки связанных сторон",
        ),
    ]
    if has_service_settlements:
        assets[2:2] = [
            _balance_line(
                key="service_supplier_advances_1c",
                label="Авансы поставщикам услуг по данным 1С",
                amount=service_advances_raw,
                source_status=payables_status,
                as_of=payables_as_of,
                masked=masked,
                include_in_total=False,
            ),
            _balance_line(
                key="service_accruals_without_documents",
                label="Минус: услуги, признанные без документов",
                amount=-accrual_amount,
                source_status=str(accrual_adjustments["source_status"]),
                as_of=requested_date,
                masked=masked,
                include_in_total=False,
                recognition_method="approved_fixed_monthly_rule",
                estimated_count=int(accrual_adjustments["estimated_count"]),
            ),
            _balance_line(
                key="service_supplier_advances",
                label="Остаток авансов поставщикам услуг",
                amount=service_advances_adjusted,
                source_status=str(accrual_adjustments["source_status"]),
                as_of=requested_date,
                masked=masked,
                source_amount=str(service_advances_raw),
                adjustment_amount=str(-accrual_amount),
                adjusted_amount=str(service_advances_adjusted),
                recognition_method="accrual",
                estimated_count=int(accrual_adjustments["estimated_count"]),
            ),
        ]
        liabilities.insert(
            0,
            _balance_line(
                key="accrued_service_liability",
                label="Начисленные услуги к оплате",
                amount=accrued_service_liability,
                source_status=str(accrual_adjustments["source_status"]),
                as_of=requested_date,
                masked=masked,
                recognition_method="accrual",
                estimated_count=int(accrual_adjustments["estimated_count"]),
            ),
        )
    assets_total = sum(
        (
            amount
            for amount in (
                cash_amount,
                inventory_cost.amount if inventory_cost is not None else None,
                service_advances_adjusted,
                supplier_receivable,
                employee_receivable,
                other_receivable,
                owner_receivable,
                owner_cash_in_transit,
                owner_related_party_asset,
            )
            if amount is not None
        ),
        Decimal("0"),
    )
    liabilities_total = sum(
        (
            amount
            for amount in (
                supplier_payable,
                accrued_service_liability,
                employee_payable,
                other_payable,
                owner_payable,
                owner_unclassified_funds,
                owner_related_party_liability,
            )
            if amount is not None
        ),
        Decimal("0"),
    )
    source_dates = [
        value
        for value in (
            cash_as_of,
            payables_as_of,
            owner_as_of,
            inventory_cost.as_of if inventory_cost is not None else None,
        )
        if value
    ]
    as_of = max(source_dates) if source_dates else None
    equity_lines = []
    accounting_includes_dividends = bool(
        get_settings().executive_management_balance_accounting_database_url
    )
    if dividends_ytd or owner_status != "ready":
        warning_note = (
            f"; {dividend_warning_count} РКО имеют комментарий «Зарплата»"
            if dividend_warning_count
            else ""
        )
        equity_lines.append(
            _balance_line(
                key="dividends_paid_ytd",
                label="Выплаченные дивиденды",
                amount=-dividends_ytd if dividends_ytd is not None else None,
                source_status=owner_status,
                as_of=owner_as_of,
                masked=masked,
                include_in_total=not accounting_includes_dividends,
                adjustment_amount=(str(-dividends_month) if dividends_month is not None else None),
                note=(
                    "Накопительно с начала года; выплата месяца "
                    f"{dividends_month or Decimal('0')} ₽{warning_note}"
                    + (
                        "; информационно — капитал уже берётся из КА/БП"
                        if accounting_includes_dividends
                        else ""
                    )
                ),
                recognition_method="equity_distribution",
            )
        )
    if has_service_settlements:
        equity_lines.append(
            _balance_line(
                key="service_accrual_result_adjustment",
                label="Корректировка результата периода по оценочным расходам",
                amount=-accrual_amount,
                source_status=str(accrual_adjustments["source_status"]),
                as_of=requested_date,
                masked=masked,
                recognition_method="accrual",
                estimated_count=int(accrual_adjustments["estimated_count"]),
            )
        )
    equity_total = sum(
        (
            _decimal(item.get("amount"))
            for item in equity_lines
            if item.get("amount") is not None and item.get("include_in_total", True)
        ),
        Decimal("0"),
    )
    liabilities_and_equity_total = liabilities_total + equity_total
    return ExecutiveDashboardBlock(
        key="creditors_payables",
        title="Управленческий баланс",
        source_status=source_status,
        freshness_status=_freshness_from_status(source_status),
        as_of=as_of,
        summary={
            "source_anchor": ("1С: деньги, взаиморасчёты и фактическая стоимость товарных партий"),
            "period_independent": True,
            "selected_month": payables_as_of.strftime("%Y-%m") if payables_as_of else None,
            "monthly_balance_endpoint": ("/api/management/executive-dashboard/management-balance"),
            "note": (
                "Частичный управленческий баланс в рублях. Товарные остатки взяты "
                "по фактической стоимости партий в 1С УТ 10.3. Не включены налоги, "
                "прочие активы и обязательства вне подключенных источников, капитал."
            ),
            "amount_currency": "RUB",
            "balance_assets": assets,
            "balance_liabilities": liabilities,
            "balance_equity": equity_lines,
            "balance_assets_total_label": "Итого подключенные активы",
            "balance_liabilities_total_label": "Итого обязательства и собственные средства",
            "balance_assets_total": None if masked else str(assets_total),
            "balance_liabilities_total": (None if masked else str(liabilities_and_equity_total)),
            "balance_obligations_total": None if masked else str(liabilities_total),
            "balance_equity_total": None if masked else str(equity_total),
        },
        metrics=[
            _metric(
                "balance_assets_total",
                "Активы по подключенным статьям",
                assets_total,
                unit="RUB",
                tone="info",
                masked=masked,
                source_status=source_status,
            ),
            _metric(
                "balance_liabilities_total",
                "Обязательства и собственные средства",
                liabilities_and_equity_total,
                unit="RUB",
                tone="warning",
                masked=masked,
                source_status=source_status,
            ),
        ],
    )


def _build_procurement_block(
    finance_payload: dict[str, Any] | None,
    *,
    access_context: ExecutiveDashboardAuthContext,
) -> ExecutiveDashboardBlock:
    section = _finance_section(finance_payload, "procurement_import")
    source_status = str(section.get("source_status") or "source_missing")
    masked = _mask_finance("procurement_import", access_context)
    return ExecutiveDashboardBlock(
        key="procurement_import",
        title="Закупки / импорт",
        source_status=source_status,
        freshness_status=_freshness_from_status(source_status),
        as_of=_as_date(section.get("as_of")),
        summary={
            "note": section.get("note") or "Нужен compact snapshot из procurement decision contour",
            "risk_items": section.get("risk_items") or [],
        },
        metrics=[
            _metric(
                "open_supplier_orders",
                "Заказы поставщику",
                int(section.get("open_supplier_orders") or 0),
                source_status=source_status,
            ),
            _metric(
                "payment_ready_amount",
                "Готовность к оплате",
                _decimal(section.get("payment_ready_amount")),
                unit="RUB",
                masked=masked,
                source_status=source_status,
            ),
            _metric(
                "cargo_risk_count",
                "Риск задержки",
                int(section.get("cargo_risk_count") or 0),
                tone="warning",
                source_status=source_status,
            ),
            _metric(
                "currency_exposure",
                "Валюта",
                _decimal(section.get("currency_exposure")),
                unit="RUB",
                masked=masked,
                source_status=source_status,
            ),
        ],
        drilldown_url=section.get("drilldown_url"),
    )


def _build_warehouse_operations_block(
    warehouse_payload: dict[str, Any] | None,
) -> ExecutiveDashboardBlock:
    section = _finance_section(warehouse_payload, "warehouse_operations")
    if not section and warehouse_payload:
        section = warehouse_payload
    source_status = str(section.get("source_status") or "source_missing")
    metric_source_status = source_status
    rows_ge_1h = int(section.get("rows_ge_1h") or 0)
    rows_ge_4h = int(section.get("rows_ge_4h") or 0)
    picker_error_count = int(section.get("picker_error_count") or 0)
    quality_issue_count = int(section.get("quality_issue_count") or 0)
    if quality_issue_count == 0:
        quality_issue_count = (
            int(section.get("negative_duration_rows") or 0)
            + rows_ge_4h
            + int(section.get("scanning_error_rows") or 0)
            + int(section.get("confirmed_picker_error_rows") or 0)
            + picker_error_count
        )
    metrics = [
        _metric(
            "transfer_document_count",
            "Перемещений",
            int(section.get("transfer_document_count") or 0),
            tone="info",
            source_status=metric_source_status,
        ),
        _metric(
            "rows_count",
            "Строк сборки",
            int(section.get("rows_count") or 0),
            source_status=metric_source_status,
        ),
        _metric(
            "pieces_picked",
            "Штук собрано",
            _decimal(section.get("pieces_picked")),
            source_status=metric_source_status,
        ),
        _metric(
            "picker_count",
            "Сборщиков",
            int(section.get("picker_count") or 0),
            tone="info",
            source_status=metric_source_status,
        ),
        _metric(
            "avg_need_fact",
            "Средняя потребность",
            _decimal(section.get("avg_need_fact")),
            tone="info",
            source_status=metric_source_status,
        ),
        _metric(
            "practical_max_need_fact",
            "Практический пик",
            _decimal(section.get("practical_max_need_fact") or section.get("max_need_fact")),
            tone="warning" if _decimal(section.get("practical_max_need_fact")) else "neutral",
            source_status=metric_source_status,
        ),
        _metric(
            "rows_ge_1h",
            "Строки > 1 часа",
            rows_ge_1h,
            tone="warning" if rows_ge_1h else "neutral",
            source_status=metric_source_status,
        ),
        _metric(
            "quality_issue_count",
            "Качество / ошибки",
            quality_issue_count,
            tone="danger" if quality_issue_count else "neutral",
            source_status=metric_source_status,
        ),
    ]
    return ExecutiveDashboardBlock(
        key="warehouse_operations",
        title="Склад / сборка",
        source_status=source_status,
        freshness_status=str(
            section.get("freshness_status") or _freshness_from_status(source_status)
        ),
        as_of=_as_date(
            section.get("as_of")
            or section.get("latest_pick_work_date")
            or (warehouse_payload or {}).get("as_of")
        ),
        summary={
            "source_anchor": section.get("source_anchor")
            or "1C: ПеремещениеТоваров -> piecework.fact_transfer_lines",
            "note": section.get("note")
            or (
                "Складской блок строится из витрины piecework: объем сборки, "
                "потребность в людях и качество таймингов."
            ),
            "period": section.get("period") or (warehouse_payload or {}).get("period"),
            "warehouse_count": int(section.get("warehouse_count") or 0),
            "top_warehouses": section.get("top_warehouses") or [],
            "top_item_groups": section.get("top_item_groups") or [],
            "top_item_subjects": section.get("top_item_subjects") or [],
            "quality_breakdown": section.get("quality_breakdown") or [],
            "rows_ge_4h": rows_ge_4h,
            "picker_error_count": picker_error_count,
            "manual_pick_rows": int(section.get("manual_pick_rows") or 0),
            "negative_duration_rows": int(section.get("negative_duration_rows") or 0),
            "drilldown_label": section.get("drilldown_label") or "Открыть складскую аналитику",
        },
        metrics=metrics,
        drilldown_url=section.get("drilldown_url"),
    )


def _build_reconciliation_block(finance_payload: dict[str, Any] | None) -> ExecutiveDashboardBlock:
    section = _finance_section(finance_payload, "reconciliation")
    source_status = str(section.get("source_status") or "source_missing")
    report_delivery = section.get("report_delivery")
    task_count = 0
    if isinstance(report_delivery, dict):
        task_count = int(report_delivery.get("task_count") or 0)
    return ExecutiveDashboardBlock(
        key="reconciliation",
        title="Сверки",
        source_status=source_status,
        freshness_status=_freshness_from_status(source_status),
        as_of=_as_date(section.get("as_of")),
        summary={
            "note": section.get("note")
            or "Sber / CloudPayments / acquiring snapshot еще не подключен к витрине",
            "sber_status": section.get("sber_status"),
            "cloudpayments_status": section.get("cloudpayments_status"),
            "issue_breakdown": section.get("issue_breakdown") or [],
            "issue_examples": section.get("issue_examples") or [],
            "issue_amount_abs": section.get("issue_amount_abs"),
            "report_delivery": report_delivery
            or {"status": "not_sent", "task_count": 0, "task_ids": []},
            "report_workbook": section.get("report_workbook"),
            "dds_issue_breakdown": section.get("dds_issue_breakdown") or [],
            "dds_issue_examples": section.get("dds_issue_examples") or [],
            "dds_issue_amount_abs": section.get("dds_issue_amount_abs"),
            "dds_issue_as_of": section.get("dds_issue_as_of"),
        },
        metrics=[
            _metric(
                "unmatched_count",
                "Не сошлось",
                int(section.get("unmatched_count") or 0),
                tone="warning",
                source_status=source_status,
            ),
            _metric(
                "issue_amount_abs",
                "Сумма расхождений",
                _decimal(section.get("issue_amount_abs")),
                unit="RUB",
                tone="warning",
                source_status=source_status,
            ),
            _metric(
                "unconfirmed_documents",
                "Документы без подтверждения",
                int(section.get("unconfirmed_documents") or 0),
                tone="warning",
                source_status=source_status,
            ),
            _metric(
                "dds_issue_count",
                "Статьи ДДС",
                int(section.get("dds_issue_count") or 0),
                source_status=source_status,
            ),
            _metric(
                "report_task_count",
                "Отчеты в задачах",
                task_count,
                source_status=source_status,
            ),
        ],
        drilldown_url=section.get("drilldown_url"),
    )


def _date_in_range(row: dict[str, Any], *, date_from: date, date_to: date) -> bool:
    business_date = _as_date(row.get("business_date"))
    return business_date is not None and date_from <= business_date <= date_to


def _row_matches_filters(
    row: dict[str, Any],
    *,
    dds_group: set[str],
    cash_account_ref: set[str],
    currency: set[str],
    direction: set[str],
    include_internal: bool,
) -> bool:
    if dds_group and str(row.get("dds_group") or "") not in dds_group:
        return False
    if cash_account_ref and str(row.get("cash_account_ref_hex") or "") not in cash_account_ref:
        return False
    if currency and str(row.get("cash_currency_code") or "") not in currency:
        return False
    if direction and str(row.get("direction") or "") not in direction:
        return False
    if not include_internal and bool(row.get("is_internal_transfer")):
        return False
    return True


def _cashflow_totals(rows: list[dict[str, Any]]) -> dict[str, Decimal | int | None]:
    external_rows = [row for row in rows if not bool(row.get("is_internal_transfer"))]
    internal_rows = [row for row in rows if bool(row.get("is_internal_transfer"))]

    def amount(key: str, source_rows: list[dict[str, Any]]) -> Decimal:
        return sum((_decimal(row.get(key)) for row in source_rows), Decimal("0"))

    movement_count = sum((int(row.get("movement_count") or 0) for row in rows), 0)
    review_count = sum((int(row.get("review_count") or 0) for row in rows), 0)
    return {
        "inflow_amount": amount("inflow_amount", rows),
        "outflow_amount": amount("outflow_amount", rows),
        "net_amount": amount("net_amount", rows),
        "external_inflow_amount": amount("inflow_amount", external_rows),
        "external_outflow_amount": amount("outflow_amount", external_rows),
        "external_net_amount": amount("net_amount", external_rows),
        "internal_inflow_amount": amount("inflow_amount", internal_rows),
        "internal_outflow_amount": amount("outflow_amount", internal_rows),
        "internal_net_amount": amount("net_amount", internal_rows),
        "movement_count": movement_count,
        "review_count": review_count,
    }


def _safe_ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == 0:
        return None
    return (numerator / denominator).quantize(Decimal("0.0001"))


def _cashflow_ratios(
    *,
    totals: dict[str, Decimal | int | None],
    date_from: date,
    date_to: date,
    closing_balance: Decimal | None,
) -> list[ExecutiveCashflowPeriodRatio]:
    days = max((date_to - date_from).days + 1, 1)
    external_outflow = _decimal(totals.get("external_outflow_amount"))
    external_inflow = _decimal(totals.get("external_inflow_amount"))
    external_net = _decimal(totals.get("external_net_amount"))
    total_turnover = _decimal(totals.get("inflow_amount")) + _decimal(totals.get("outflow_amount"))
    internal_turnover = _decimal(totals.get("internal_inflow_amount")) + _decimal(
        totals.get("internal_outflow_amount")
    )
    movement_count = Decimal(str(totals.get("movement_count") or 0))
    review_count = Decimal(str(totals.get("review_count") or 0))
    avg_daily_outflow = (external_outflow / Decimal(days)).quantize(Decimal("0.01"))
    return [
        ExecutiveCashflowPeriodRatio(
            key="cash_days_on_hand",
            label="Дней запаса денег",
            value=_safe_ratio(closing_balance or Decimal("0"), avg_daily_outflow),
            unit="days",
            tone=(
                "warning"
                if avg_daily_outflow
                and closing_balance
                and closing_balance / avg_daily_outflow < 14
                else "neutral"
            ),
            note="Конечный остаток / средний дневной внешний расход.",
        ),
        ExecutiveCashflowPeriodRatio(
            key="average_daily_external_outflow",
            label="Средний дневной расход",
            value=avg_daily_outflow,
            unit="RUB",
            note="Только внешний расход, без внутренних переводов.",
        ),
        ExecutiveCashflowPeriodRatio(
            key="inflow_outflow_coverage",
            label="Покрытие расходов поступлениями",
            value=_safe_ratio(external_inflow, external_outflow),
            unit="ratio",
            tone=(
                "warning" if external_outflow and external_inflow < external_outflow else "neutral"
            ),
        ),
        ExecutiveCashflowPeriodRatio(
            key="internal_turnover_share",
            label="Доля внутренних переводов",
            value=_safe_ratio(internal_turnover, total_turnover),
            unit="percent",
            note="Помогает не путать оборот с реальным cashflow.",
        ),
        ExecutiveCashflowPeriodRatio(
            key="review_share",
            label="Доля строк на проверку",
            value=_safe_ratio(review_count, movement_count),
            unit="percent",
            tone="warning" if review_count else "neutral",
        ),
        ExecutiveCashflowPeriodRatio(
            key="net_cashflow_margin",
            label="Net к поступлениям",
            value=_safe_ratio(external_net, external_inflow),
            unit="percent",
        ),
    ]


def _aggregate_cashflow_rows(
    rows: list[dict[str, Any]],
    keys: tuple[str, ...],
    *,
    label_key: str,
    limit: int = 30,
) -> list[ExecutiveCashflowPeriodBreakdownRow]:
    buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        bucket_key = tuple(row.get(key) for key in keys)
        if bucket_key not in buckets:
            buckets[bucket_key] = {
                "inflow_amount": Decimal("0"),
                "outflow_amount": Decimal("0"),
                "net_amount": Decimal("0"),
                "movement_count": 0,
                "review_count": 0,
                "meta": {key: row.get(key) for key in keys},
                "label": str(row.get(label_key) or row.get(keys[0]) or "Не указано"),
            }
        bucket = buckets[bucket_key]
        bucket["inflow_amount"] += _decimal(row.get("inflow_amount"))
        bucket["outflow_amount"] += _decimal(row.get("outflow_amount"))
        bucket["net_amount"] += _decimal(row.get("net_amount"))
        bucket["movement_count"] += int(row.get("movement_count") or 0)
        bucket["review_count"] += int(row.get("review_count") or 0)
    items = [
        ExecutiveCashflowPeriodBreakdownRow(
            key="|".join(str(part or "") for part in bucket_key),
            label=str(bucket["label"]),
            inflow_amount=bucket["inflow_amount"],
            outflow_amount=bucket["outflow_amount"],
            net_amount=bucket["net_amount"],
            movement_count=int(bucket["movement_count"]),
            review_count=int(bucket["review_count"]),
            meta=dict(bucket["meta"]),
        )
        for bucket_key, bucket in buckets.items()
    ]
    items.sort(key=lambda item: item.inflow_amount + item.outflow_amount, reverse=True)
    return items[:limit]


def _cashflow_daily(rows: list[dict[str, Any]]) -> list[ExecutiveCashflowDailyRow]:
    buckets: dict[date, dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "inflow_amount": Decimal("0"),
            "outflow_amount": Decimal("0"),
            "net_amount": Decimal("0"),
            "external_net_amount": Decimal("0"),
            "internal_net_amount": Decimal("0"),
            "movement_count": 0,
            "review_count": 0,
        }
    )
    for row in rows:
        business_date = _as_date(row.get("business_date"))
        if business_date is None:
            continue
        bucket = buckets[business_date]
        bucket["inflow_amount"] += _decimal(row.get("inflow_amount"))
        bucket["outflow_amount"] += _decimal(row.get("outflow_amount"))
        bucket["net_amount"] += _decimal(row.get("net_amount"))
        if bool(row.get("is_internal_transfer")):
            bucket["internal_net_amount"] += _decimal(row.get("net_amount"))
        else:
            bucket["external_net_amount"] += _decimal(row.get("net_amount"))
        bucket["movement_count"] += int(row.get("movement_count") or 0)
        bucket["review_count"] += int(row.get("review_count") or 0)
    return [
        ExecutiveCashflowDailyRow(business_date=business_date, **bucket)
        for business_date, bucket in sorted(buckets.items())
    ]


def _cash_position_for_period(
    payload: dict[str, Any],
    *,
    date_from: date,
    date_to: date,
) -> dict[str, Any]:
    source = payload.get("cash_position")
    rows = source.get("rows") if isinstance(source, dict) else []
    position_rows = [row for row in rows if isinstance(row, dict)]
    opening_candidates = [
        row for row in position_rows if (_as_date(row.get("snapshot_date")) or date.max) < date_from
    ]
    closing_candidates = [
        row for row in position_rows if (_as_date(row.get("snapshot_date")) or date.min) <= date_to
    ]
    opening = opening_candidates[-1] if opening_candidates else None
    closing = closing_candidates[-1] if closing_candidates else None
    return {
        "opening": opening,
        "closing": closing,
        "source_status": "ready" if opening or closing else "source_missing",
        "note": None if opening or closing else "В кэше нет остатков на границы периода.",
    }


def _quality_issues_for_period(
    payload: dict[str, Any],
    *,
    date_from: date,
    date_to: date,
) -> list[ExecutiveCashflowQualityIssue]:
    raw_issues = payload.get("quality_issues")
    if not isinstance(raw_issues, list):
        raw_issues = (
            (payload.get("quality") or {}).get("examples")
            if isinstance(payload.get("quality"), dict)
            else []
        )
    issues: list[ExecutiveCashflowQualityIssue] = []
    for item in raw_issues if isinstance(raw_issues, list) else []:
        if not isinstance(item, dict):
            continue
        issue_type = str(item.get("issue_type") or "manual_review")
        is_open_owner_control = str(item.get("status") or "open") in {"open", "pending"} and (
            issue_type.startswith("owner_transfer_")
            or issue_type == "owner_related_party_unresolved"
        )
        if not is_open_owner_control and not _date_in_range(
            item, date_from=date_from, date_to=date_to
        ):
            continue
        business_date = _as_date(item.get("business_date"))
        if business_date is None:
            continue
        issues.append(
            ExecutiveCashflowQualityIssue(
                issue_key=str(item.get("issue_key") or ""),
                issue_type=issue_type,
                issue_label=str(
                    item.get("issue_label") or item.get("issue_type") or "Ручная проверка"
                ),
                severity=str(item.get("severity") or "medium"),
                business_date=business_date,
                amount_abs=_decimal(item.get("amount_abs") or item.get("amount_signed")),
                description=item.get("description") or item.get("description_ru"),
                proposed_action=item.get("proposed_action") or item.get("proposed_action_ru"),
                status=str(item.get("status") or "open"),
                document_number=(
                    str(item.get("document_number")) if item.get("document_number") else None
                ),
                bitrix_task_id=(
                    str(item.get("bitrix_task_id")) if item.get("bitrix_task_id") else None
                ),
                task_status=(str(item.get("task_status")) if item.get("task_status") else None),
                drilldown_url=(
                    str(item.get("drilldown_url")) if item.get("drilldown_url") else None
                ),
            )
        )
    return issues


def _quality_totals_for_period(
    payload: dict[str, Any],
    issues: list[ExecutiveCashflowQualityIssue],
    *,
    date_from: date,
    date_to: date,
) -> tuple[int, Decimal]:
    raw_daily = payload.get("quality_daily")
    if isinstance(raw_daily, list):
        issue_count = 0
        amount_abs = Decimal("0")
        for item in raw_daily:
            if not isinstance(item, dict) or not _date_in_range(
                item, date_from=date_from, date_to=date_to
            ):
                continue
            issue_count += int(item.get("issue_count") or item.get("count") or 0)
            amount_abs += _decimal(item.get("amount_abs"))
        return issue_count, amount_abs

    return (
        len(issues),
        sum((_decimal(issue.amount_abs) for issue in issues), Decimal("0")),
    )


def build_executive_cashflow_period_response(
    *,
    date_from: date,
    date_to: date,
    dds_group: list[str] | None = None,
    cash_account_ref: list[str] | None = None,
    currency: list[str] | None = None,
    direction: list[str] | None = None,
    include_internal: bool = True,
) -> ExecutiveCashflowPeriodResponse:
    payload, source_status, note = _load_cashflow_period_cache()
    if not payload:
        return ExecutiveCashflowPeriodResponse(
            date_from=date_from,
            date_to=date_to,
            source_status=source_status,
            freshness_status=_freshness_from_status(source_status),
            note=note,
        )
    rows_source = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    rows = [
        row
        for row in rows_source
        if isinstance(row, dict)
        and _date_in_range(row, date_from=date_from, date_to=date_to)
        and _row_matches_filters(
            row,
            dds_group=set(dds_group or []),
            cash_account_ref=set(cash_account_ref or []),
            currency=set(currency or []),
            direction=set(direction or []),
            include_internal=include_internal,
        )
    ]
    cache_period = payload.get("period") if isinstance(payload.get("period"), dict) else {}
    cache_from = _as_date(cache_period.get("date_from"))
    cache_to = _as_date(cache_period.get("date_to"))
    period_note = None
    period_outside_cache = bool(
        cache_from is None or cache_to is None or date_from < cache_from or date_to > cache_to
    )
    if period_outside_cache:
        period_note = (
            "Запрошенный период выходит за кэш "
            f"{cache_from.isoformat() if cache_from else '?'}.."
            f"{cache_to.isoformat() if cache_to else '?'}."
        )
    if not rows and not period_note:
        period_note = "В кэше нет движений ДДС по выбранным фильтрам."
    totals = _cashflow_totals(rows)
    cash_position = _cash_position_for_period(payload, date_from=date_from, date_to=date_to)
    closing_balance = None
    if isinstance(cash_position.get("closing"), dict):
        closing_balance = _decimal(cash_position["closing"].get("total_balance"))
    issues = _quality_issues_for_period(payload, date_from=date_from, date_to=date_to)
    quality_issue_count, quality_issue_amount_abs = _quality_totals_for_period(
        payload,
        issues,
        date_from=date_from,
        date_to=date_to,
    )
    totals["quality_issue_count"] = quality_issue_count
    totals["quality_issue_amount_abs"] = quality_issue_amount_abs
    effective_status = str(payload.get("source_status") or source_status or "ready")
    if not rows:
        effective_status = "source_missing" if effective_status == "ready" else effective_status
    elif period_outside_cache:
        effective_status = "stale"
    owner_control_issues = [
        issue
        for issue in issues
        if issue.status in {"open", "pending"}
        and (
            issue.issue_type.startswith("owner_transfer_")
            or issue.issue_type == "owner_related_party_unresolved"
            or issue.issue_type == "owner_transfer_control_pending"
        )
    ]
    if effective_status == "ready" and any(
        issue.severity in {"high", "critical"}
        or issue.issue_type == "owner_transfer_control_pending"
        for issue in owner_control_issues
    ):
        effective_status = "partial"
    effective_freshness = (
        "stale" if period_outside_cache and rows else _freshness_from_status(effective_status)
    )
    return ExecutiveCashflowPeriodResponse(
        date_from=date_from,
        date_to=date_to,
        generated_at=_as_datetime(payload.get("generated_at")),
        source_status=effective_status,
        freshness_status=effective_freshness,
        note=period_note or payload.get("note"),
        totals=totals,
        ratios=_cashflow_ratios(
            totals=totals,
            date_from=date_from,
            date_to=date_to,
            closing_balance=closing_balance,
        ),
        cash_position=cash_position,
        daily=_cashflow_daily(rows),
        by_group=_aggregate_cashflow_rows(rows, ("dds_group",), label_key="group_label", limit=20),
        by_article=_aggregate_cashflow_rows(
            rows, ("article_key",), label_key="article_name", limit=20
        ),
        by_cash_account=_aggregate_cashflow_rows(
            rows,
            ("cash_account_ref_hex",),
            label_key="cash_account_name",
            limit=20,
        ),
        by_currency=_aggregate_cashflow_rows(
            rows, ("cash_currency_code",), label_key="currency_name", limit=20
        ),
        quality_issues=issues[:20],
        filters=payload.get("filters") if isinstance(payload.get("filters"), dict) else {},
    )


def _to_action(
    row: ExecutiveActionItem,
    *,
    access_context: ExecutiveDashboardAuthContext,
) -> ExecutiveDashboardAction:
    amount = row.amount
    if row.domain in _MONEY_BLOCK_KEYS and not access_context.can_view_money_block(row.domain):
        amount = None
    return ExecutiveDashboardAction(
        stable_key=row.stable_key,
        business_date=row.business_date,
        domain=row.domain,
        severity=row.severity,
        title=row.title,
        description=row.description,
        amount=amount,
        currency=row.currency,
        responsible_bitrix_user_id=row.responsible_bitrix_user_id,
        deadline_at=row.deadline_at,
        status=row.status,
        source_system=row.source_system,
        source_ref=row.source_ref,
        dedupe_key=row.dedupe_key,
        drilldown_url=row.drilldown_url,
        payload=row.payload or {},
    )


def _action_query(
    *,
    requested_date: date,
    status: str | None,
    domain: str | None,
    access_context: ExecutiveDashboardAuthContext,
) -> Select[tuple[ExecutiveActionItem]]:
    query = select(ExecutiveActionItem).where(ExecutiveActionItem.business_date <= requested_date)
    if status:
        query = query.where(ExecutiveActionItem.status == status)
    if domain:
        query = query.where(ExecutiveActionItem.domain == domain)
    if not access_context.is_full_access:
        allowed_domains = tuple(access_context.allowed_action_domains)
        if not allowed_domains:
            return query.where(false())
        query = query.where(ExecutiveActionItem.domain.in_(allowed_domains))
    if (
        not access_context.is_full_access
        and access_context.personal_actions_only
        and access_context.bitrix_user_id
    ):
        query = query.where(
            or_(
                ExecutiveActionItem.responsible_bitrix_user_id == access_context.bitrix_user_id,
                ExecutiveActionItem.responsible_bitrix_user_id.is_(None),
            )
        )
    return query


def list_executive_actions(
    session: Session,
    *,
    requested_date: date,
    status: str | None = "open",
    domain: str | None = None,
    access_level: AccessLevel = "full",
    bitrix_user_id: str | None = None,
    access_context: ExecutiveDashboardAuthContext | None = None,
    limit: int = 200,
) -> list[ExecutiveDashboardAction]:
    context = _coerce_access_context(
        access_context=access_context,
        access_level=access_level,
        bitrix_user_id=bitrix_user_id,
    )
    rows = (
        session.execute(
            _action_query(
                requested_date=requested_date,
                status=status,
                domain=domain,
                access_context=context,
            )
        )
        .scalars()
        .all()
    )
    rows.sort(
        key=lambda row: (
            _SEVERITY_RANK.get(row.severity, 9),
            row.deadline_at or datetime.max,
            row.created_at or datetime.max,
        )
    )
    return [_to_action(row, access_context=context) for row in rows[:limit]]


def _procurement_attention_actions(
    finance_payload: dict[str, Any] | None,
    *,
    requested_date: date,
    status: str | None,
    domain: str | None,
    access_context: ExecutiveDashboardAuthContext,
) -> list[ExecutiveDashboardAction]:
    if status not in {None, "open"}:
        return []
    if domain not in {None, "procurement_import"}:
        return []
    if not access_context.allows_action_domain("procurement_import"):
        return []

    section = _finance_section(finance_payload, "procurement_import")
    source_date = _as_date(section.get("as_of"))
    if source_date is None or source_date > requested_date:
        return []
    raw_items = section.get("attention_items")
    if not isinstance(raw_items, list):
        return []

    actions: list[ExecutiveDashboardAction] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        onec_ref = str(raw_item.get("onec_ref") or "").strip()
        source_number = str(raw_item.get("onec_source_number") or "").strip()
        if not onec_ref or not source_number:
            continue
        reason_code = str(raw_item.get("reason_code") or "manual_review").strip()
        stable_key = f"procurement_import:{reason_code}:{onec_ref}"
        supplier_title = str(raw_item.get("supplier_title") or "").strip()
        reason = str(raw_item.get("reason") or "Требуется проверка заказа.").strip()
        recommendation = str(raw_item.get("recommendation") or "").strip()
        amount = _decimal(raw_item.get("amount_rub"))
        if not access_context.can_view_money_block("procurement_import"):
            amount = None
        description = " · ".join(part for part in (supplier_title, reason) if part)
        actions.append(
            ExecutiveDashboardAction(
                stable_key=stable_key,
                business_date=source_date,
                domain="procurement_import",
                severity="high",
                title=f"Заказ {source_number}: заполнить «Сдача в карго»",
                description=description,
                amount=amount,
                currency="RUB",
                status="open",
                source_system=str(raw_item.get("source_system") or "1C"),
                source_ref=onec_ref,
                dedupe_key=stable_key,
                payload={
                    **raw_item,
                    "correction_system": "1C",
                    "correction_document": "Заказ поставщику",
                    "correction_field": "Сдача в карго",
                    "recommendation": recommendation,
                },
            )
        )
    return actions


def _action_sort_key(action: ExecutiveDashboardAction) -> tuple[int, float, str]:
    deadline = action.deadline_at
    if deadline is None:
        deadline_value = float("inf")
    else:
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        deadline_value = deadline.timestamp()
    return (_SEVERITY_RANK.get(action.severity, 9), deadline_value, action.stable_key)


def _merge_executive_actions(
    *groups: list[ExecutiveDashboardAction],
    limit: int,
) -> list[ExecutiveDashboardAction]:
    by_stable_key: dict[str, ExecutiveDashboardAction] = {}
    for group in groups:
        for action in group:
            by_stable_key.setdefault(action.stable_key, action)
    return sorted(by_stable_key.values(), key=_action_sort_key)[:limit]


def _latest_persisted_snapshot(
    session: Session,
    *,
    requested_date: date,
) -> ExecutiveDashboardSnapshot | None:
    return (
        session.execute(
            select(ExecutiveDashboardSnapshot)
            .where(ExecutiveDashboardSnapshot.snapshot_date <= requested_date)
            .order_by(
                ExecutiveDashboardSnapshot.snapshot_date.desc(),
                ExecutiveDashboardSnapshot.computed_at.desc(),
                ExecutiveDashboardSnapshot.id.desc(),
            )
            .limit(1)
        )
        .scalars()
        .first()
    )


def _source_freshness(
    session: Session,
    *,
    requested_date: date,
    finance_payload: dict[str, Any] | None,
    warehouse_payload: dict[str, Any] | None,
    blocks: list[ExecutiveDashboardBlock],
    access_context: ExecutiveDashboardAuthContext,
) -> list[ExecutiveSourceStatus]:
    statuses = {
        block.key: ExecutiveSourceStatus(
            source_key=block.key,
            title=block.title,
            source_status=block.source_status,
            freshness_status=block.freshness_status,
            as_of=block.as_of,
            max_lag_days=get_settings().executive_dashboard_source_max_lag_days,
            note=str(block.summary.get("note") or "") or None,
        )
        for block in blocks
    }

    for source_payload in (finance_payload, warehouse_payload):
        source_freshness = (source_payload or {}).get("source_freshness")
        if not isinstance(source_freshness, dict):
            continue
        for key, item in source_freshness.items():
            if not _source_key_allowed(str(key), access_context):
                continue
            if not isinstance(item, dict):
                continue
            source_as_of = _as_datetime(item.get("as_of")) or _as_date(item.get("as_of"))
            item_source_status, item_freshness_status = _apply_date_freshness(
                str(item.get("source_status") or "source_missing"),
                requested_date=requested_date,
                source_as_of=source_as_of,
                max_lag_days=item.get("max_lag_days"),
            )
            statuses[str(key)] = ExecutiveSourceStatus(
                source_key=str(key),
                title=str(item.get("title") or key),
                source_status=item_source_status,
                freshness_status=item_freshness_status,
                as_of=source_as_of,
                max_lag_days=item.get("max_lag_days"),
                note=item.get("note"),
            )

    rows = (
        session.execute(
            select(ExecutiveSourceFreshness).where(
                ExecutiveSourceFreshness.business_date == requested_date
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if not _source_key_allowed(row.source_key, access_context):
            continue
        statuses[row.source_key] = ExecutiveSourceStatus(
            source_key=row.source_key,
            title=str((row.payload or {}).get("title") or row.source_key),
            source_status=row.source_status,
            freshness_status=_freshness_from_status(row.source_status),
            as_of=row.source_as_of,
            max_lag_days=row.max_lag_days,
            note=(row.payload or {}).get("note"),
        )
    return list(statuses.values())


def _tasks_block(actions: list[ExecutiveDashboardAction]) -> ExecutiveDashboardBlock:
    overdue_count = sum(
        1 for item in actions if item.deadline_at and item.deadline_at < datetime.now()
    )
    critical_count = sum(1 for item in actions if item.severity == "critical")
    return ExecutiveDashboardBlock(
        key="tasks",
        title="Задачи",
        source_status="ready" if actions else "source_missing",
        freshness_status="fresh" if actions else "missing",
        summary={
            "note": "В v1 блок строится из executive_action_item; подключение полного task-health Bitrix идет отдельным источником",
        },
        metrics=[
            _metric("open_actions", "Открытые решения", len(actions), tone="info"),
            _metric("critical_actions", "Критичные", critical_count, tone="danger"),
            _metric("overdue_actions", "Просрочено", overdue_count, tone="warning"),
        ],
    )


def _daily_focus_block(actions: list[ExecutiveDashboardAction]) -> ExecutiveDashboardBlock:
    return ExecutiveDashboardBlock(
        key="daily_focus",
        title="Фокус дня",
        source_status="ready" if actions else "source_missing",
        freshness_status="fresh" if actions else "missing",
        summary={
            "action_count": len(actions[:10]),
            "stable_keys": [item.stable_key for item in actions[:10]],
        },
        metrics=[
            _metric("focus_count", "Действий", len(actions[:10]), tone="info"),
        ],
    )


def build_executive_dashboard(
    session: Session,
    *,
    requested_date: date,
    access_level: AccessLevel = "full",
    bitrix_user_id: str | None = None,
    access_context: ExecutiveDashboardAuthContext | None = None,
) -> ExecutiveDashboardResponse:
    context = _coerce_access_context(
        access_context=access_context,
        access_level=access_level,
        bitrix_user_id=bitrix_user_id,
    )
    finance_payload, finance_source_status, finance_note = _load_finance_snapshot()
    warehouse_payload, warehouse_source_status, warehouse_note = _load_warehouse_snapshot()
    owner_payload, owner_source_status, owner_note = _load_owner_cash_control_snapshot()
    inventory_cost, inventory_note = _load_onec_inventory_cost(requested_date)
    persisted = _latest_persisted_snapshot(session, requested_date=requested_date)
    persisted_actions = list_executive_actions(
        session,
        requested_date=requested_date,
        status="open",
        access_context=context,
        limit=10,
    )
    procurement_actions = _procurement_attention_actions(
        finance_payload,
        requested_date=requested_date,
        status="open",
        domain=None,
        access_context=context,
    )
    top_actions = _merge_executive_actions(
        persisted_actions,
        procurement_actions,
        limit=10,
    )

    money_today_block = _build_money_today_block(finance_payload, access_context=context)
    debtors_block = _build_receivables_block(
        session,
        requested_date=requested_date,
        access_context=context,
    )
    blocks = [
        money_today_block,
        _build_profit_loss_block(
            session,
            requested_date=requested_date,
            access_context=context,
        ),
        _build_sales_block(
            session,
            requested_date=requested_date,
            access_context=context,
        ),
        debtors_block,
        _build_receivables_control_block(
            session,
            requested_date=requested_date,
        ),
        _build_management_balance_block(
            session,
            finance_payload,
            owner_payload,
            inventory_cost,
            inventory_note,
            requested_date=requested_date,
            money_block=money_today_block,
            access_context=context,
        ),
        _build_procurement_block(finance_payload, access_context=context),
        _build_warehouse_operations_block(warehouse_payload),
        _build_reconciliation_block(finance_payload),
        _tasks_block(top_actions),
        _daily_focus_block(top_actions),
    ]
    if finance_payload is None:
        for block in blocks:
            if block.key in {
                "money_today",
                "creditors_payables",
                "procurement_import",
                "reconciliation",
            }:
                block.summary["finance_snapshot_status"] = finance_source_status
                block.summary["finance_snapshot_note"] = finance_note
    if owner_payload is None:
        balance = next((block for block in blocks if block.key == "creditors_payables"), None)
        if balance is not None:
            balance.summary["owner_cash_control_status"] = owner_source_status
            balance.summary["owner_cash_control_note"] = owner_note
    if warehouse_payload is None:
        for block in blocks:
            if block.key == "warehouse_operations":
                block.summary["warehouse_snapshot_status"] = warehouse_source_status
                block.summary["warehouse_snapshot_note"] = warehouse_note

    blocks = [block for block in blocks if context.allows_block(block.key)]
    for block in blocks:
        block.source_status, block.freshness_status = _apply_date_freshness(
            block.source_status,
            requested_date=requested_date,
            source_as_of=block.as_of,
        )
    freshness_status, source_status = _source_statuses(blocks)
    source_freshness = _source_freshness(
        session,
        requested_date=requested_date,
        finance_payload=finance_payload,
        warehouse_payload=warehouse_payload,
        blocks=blocks,
        access_context=context,
    )
    generated_at = datetime.now(UTC)
    if persisted is not None and persisted.computed_at is not None:
        generated_at = (
            persisted.computed_at.replace(tzinfo=UTC)
            if persisted.computed_at.tzinfo is None
            else persisted.computed_at
        )

    return ExecutiveDashboardResponse(
        as_of=requested_date,
        generated_at=generated_at,
        freshness_status=freshness_status,
        source_status=source_status,
        access_level=context.access_level,
        roles=list(context.roles),
        allowed_blocks=list(context.allowed_blocks),
        allowed_action_domains=list(context.allowed_action_domains),
        blocks=blocks,
        source_freshness=source_freshness,
        top_actions=top_actions,
        summary={
            "snapshot_revision": persisted.revision if persisted else None,
            "snapshot_status": persisted.status if persisted else "computed_live",
            "finance_snapshot_status": finance_source_status,
            "finance_snapshot_note": finance_note,
            "warehouse_snapshot_status": warehouse_source_status,
            "warehouse_snapshot_note": warehouse_note,
            "top_action_count": len(top_actions),
        },
    )


def build_executive_actions_response(
    session: Session,
    *,
    requested_date: date,
    status: str | None,
    domain: str | None,
    access_level: AccessLevel = "full",
    bitrix_user_id: str | None = None,
    access_context: ExecutiveDashboardAuthContext | None = None,
    limit: int = 200,
) -> ExecutiveDashboardActionsResponse:
    context = _coerce_access_context(
        access_context=access_context,
        access_level=access_level,
        bitrix_user_id=bitrix_user_id,
    )
    finance_payload, _, _ = _load_finance_snapshot()
    persisted_actions = list_executive_actions(
        session,
        requested_date=requested_date,
        status=status,
        domain=domain,
        access_context=context,
        limit=limit,
    )
    procurement_actions = _procurement_attention_actions(
        finance_payload,
        requested_date=requested_date,
        status=status,
        domain=domain,
        access_context=context,
    )
    actions = _merge_executive_actions(
        persisted_actions,
        procurement_actions,
        limit=limit,
    )
    return ExecutiveDashboardActionsResponse(
        as_of=requested_date,
        freshness_status="fresh" if actions else "missing",
        source_status="ready" if actions else "empty",
        total_count=len(actions),
        payload=actions,
    )
