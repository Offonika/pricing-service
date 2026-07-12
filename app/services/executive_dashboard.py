from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import Select, false, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
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
    ExecutiveSourceStatus,
)
from app.services.bitrix_executive_dashboard_auth import (
    EXECUTIVE_DASHBOARD_MONEY_BLOCK_KEYS,
    ExecutiveDashboardAuthContext,
    full_executive_dashboard_context,
    legacy_domain_executive_dashboard_context,
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
    cwd_candidate = Path.cwd() / configured
    if cwd_candidate.exists():
        return cwd_candidate
    if configured.parts and configured.parts[0] == "..":
        workspace_root = Path(os.getenv("MM_WORKSPACE_ROOT", "/opt/MM"))
        workspace_candidate = workspace_root.joinpath(*configured.parts[1:])
        if workspace_candidate.exists():
            return workspace_candidate
    return cwd_candidate


def _resolve_snapshot_path() -> Path:
    return _resolve_shared_path(get_settings().executive_dashboard_finance_snapshot_path)


def _load_finance_snapshot() -> tuple[dict[str, Any] | None, str, str]:
    path = _resolve_snapshot_path()
    if not path.exists():
        return None, "source_missing", f"finance snapshot is not found: {path}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
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
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
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
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, "source_error", f"warehouse snapshot is not readable: {exc}"
    if not isinstance(payload, dict):
        return None, "source_error", "warehouse snapshot root must be an object"
    return payload, str(payload.get("source_status") or "ready"), str(path)


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
    if period_outside_cache:
        expense_status = "stale"

    return {
        "source_status": expense_status,
        "freshness_status": _freshness_from_status(expense_status),
        "note": (
            "Расходы определены по исходящим оплатам ДДС. "
            "Это cash-based управленческий срез, не бухгалтерское начисление."
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
        "ОПУ v1 строится по продажам 1С и расходам по оплатам ДДС. "
        "Расходы являются cash-based управленческим срезом, не бухгалтерским начислением."
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
            "Расходы по ДДС",
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
            "expense_source_anchor": "ДДС: cashflow_period_cache.profit_loss_expenses",
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
            value = group.get(f"{side}_amount")
            if value in (None, ""):
                # Compatibility with the old snapshot format. All currently
                # supported settlement groups use positive balances for
                # receivables/prepayments and negative balances for debts.
                fallback = "gross_payable" if side == "asset" else "reverse_balance"
                value = group.get(fallback)
            return _decimal(value) if value not in (None, "") else None
    return None


def _balance_line(
    *,
    key: str,
    label: str,
    amount: Decimal | None,
    source_status: str,
    as_of: date | datetime | None,
    masked: bool,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "amount": None if masked or amount is None else str(amount),
        "source_status": source_status,
        "as_of": as_of.isoformat() if isinstance(as_of, (date, datetime)) else None,
        "masked": masked,
    }


def _build_management_balance_block(
    finance_payload: dict[str, Any] | None,
    *,
    money_block: ExecutiveDashboardBlock,
    debtors_block: ExecutiveDashboardBlock,
    access_context: ExecutiveDashboardAuthContext,
) -> ExecutiveDashboardBlock:
    section = _finance_section(finance_payload, "creditors_payables")
    payables_status = str(section.get("source_status") or "source_missing")
    cash_metric = _metric_by_key(money_block, "cash_position_total_balance")
    receivable_metric = _metric_by_key(debtors_block, "total_receivable")
    cash_status = (
        cash_metric.source_status if cash_metric is not None else money_block.source_status
    )
    receivables_status = debtors_block.source_status
    source_status = _combine_source_status_strings(
        [cash_status, receivables_status, payables_status, "source_missing"]
    )
    masked = any(
        _mask_finance(block_key, access_context)
        for block_key in ("money_today", "debtors", "creditors_payables")
    )

    cash_as_of = _as_date(money_block.summary.get("cash_position_as_of")) or money_block.as_of
    receivables_as_of = debtors_block.as_of
    payables_as_of = _as_date(section.get("as_of"))
    cash_amount = (
        _decimal(cash_metric.value)
        if cash_metric is not None and _source_available_for_metric(cash_status)
        else None
    )
    receivables_amount = (
        _decimal(receivable_metric.value)
        if receivable_metric is not None and _source_available_for_metric(receivables_status)
        else None
    )
    supplier_advance = _settlement_group_amount(
        section,
        group_key="suppliers",
        side="asset",
    )
    employee_receivable = _settlement_group_amount(
        section,
        group_key="employees",
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
            key="receivables",
            label="Дебиторка покупателей",
            amount=receivables_amount,
            source_status=receivables_status,
            as_of=receivables_as_of,
            masked=masked,
        ),
        _balance_line(
            key="inventory_cost",
            label="Товарные остатки по себестоимости",
            amount=None,
            source_status="source_missing",
            as_of=None,
            masked=masked,
        ),
        _balance_line(
            key="supplier_advances",
            label="Предоплата поставщикам",
            amount=supplier_advance,
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
            key="owner_receivables",
            label="Дебиторка собственников",
            amount=owner_receivable,
            source_status=payables_status,
            as_of=payables_as_of,
            masked=masked,
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
            key="owners",
            label="Задолженность собственникам",
            amount=owner_payable,
            source_status=payables_status,
            as_of=payables_as_of,
            masked=masked,
        ),
    ]
    assets_total = sum(
        (
            amount
            for amount in (
                cash_amount,
                receivables_amount,
                supplier_advance,
                employee_receivable,
                owner_receivable,
            )
            if amount is not None
        ),
        Decimal("0"),
    )
    liabilities_total = sum(
        (
            amount
            for amount in (supplier_payable, employee_payable, owner_payable)
            if amount is not None
        ),
        Decimal("0"),
    )
    source_dates = [value for value in (cash_as_of, receivables_as_of, payables_as_of) if value]
    as_of = max(source_dates) if source_dates else None
    return ExecutiveDashboardBlock(
        key="creditors_payables",
        title="Управленческий баланс",
        source_status=source_status,
        freshness_status=_freshness_from_status(source_status),
        as_of=as_of,
        summary={
            "source_anchor": "1С: остатки денежных средств, дебиторка покупателей и взаиморасчеты",
            "note": (
                "Частичный управленческий баланс в рублях. Себестоимость товарных "
                "остатков ещё не подключена: найденная SQL totals-таблица не прошла "
                "проверку суммы. Не включены налоги, прочие активы и обязательства "
                "вне подключенных источников, капитал."
            ),
            "amount_currency": "RUB",
            "balance_assets": assets,
            "balance_liabilities": liabilities,
            "balance_assets_total_label": "Итого подключенные активы",
            "balance_liabilities_total_label": "Итого подключенные пассивы",
            "balance_assets_total": None if masked else str(assets_total),
            "balance_liabilities_total": None if masked else str(liabilities_total),
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
                "Пассивы по подключенным статьям",
                liabilities_total,
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
        if not isinstance(item, dict) or not _date_in_range(
            item, date_from=date_from, date_to=date_to
        ):
            continue
        business_date = _as_date(item.get("business_date"))
        if business_date is None:
            continue
        issues.append(
            ExecutiveCashflowQualityIssue(
                issue_key=str(item.get("issue_key") or ""),
                issue_type=str(item.get("issue_type") or "manual_review"),
                issue_label=str(
                    item.get("issue_label") or item.get("issue_type") or "Ручная проверка"
                ),
                severity=str(item.get("severity") or "medium"),
                business_date=business_date,
                amount_abs=_decimal(item.get("amount_abs") or item.get("amount_signed")),
                description=item.get("description") or item.get("description_ru"),
                proposed_action=item.get("proposed_action") or item.get("proposed_action_ru"),
                status=str(item.get("status") or "open"),
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
        debtors_block,
        _build_receivables_control_block(
            session,
            requested_date=requested_date,
        ),
        _build_management_balance_block(
            finance_payload,
            money_block=money_today_block,
            debtors_block=debtors_block,
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
