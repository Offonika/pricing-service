from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.executive_dashboard import (
    ExecutiveManagementBalanceAudit,
    ExecutiveManagementBalanceLine,
    ExecutiveManagementBalanceSnapshot,
)
from app.schemas.executive_dashboard import (
    ExecutiveManagementBalanceLineItem,
    ExecutiveManagementBalanceResponse,
)
from app.services.bitrix_executive_dashboard_auth import (
    ExecutiveDashboardAuthContext,
    full_executive_dashboard_context,
)
from app.services.executive_dashboard import build_executive_dashboard

BalanceView = Literal["closed", "operational"]
BalanceSection = Literal["asset", "liability", "equity"]
MONEY = Decimal("0.01")


class ManagementBalanceNotFoundError(LookupError):
    pass


class ManagementBalanceCloseError(ValueError):
    pass


@dataclass(frozen=True)
class BalanceLineDraft:
    section: BalanceSection
    key: str
    label: str
    amount: Decimal | None
    order: int
    source_key: str
    source_status: str
    source_as_of: date | None
    note: str | None = None
    include_in_total: bool = True
    source_amount: Decimal | None = None
    adjustment_amount: Decimal | None = None
    adjusted_amount: Decimal | None = None
    recognition_method: str | None = None
    estimated_count: int = 0


_ACCOUNTING_LINES: tuple[tuple[BalanceSection, str, str, int], ...] = (
    ("asset", "fixed_assets_net", "Основные средства за вычетом амортизации", 70),
    ("asset", "tax_receivables", "Налоги к возмещению", 80),
    ("asset", "other_assets", "Прочие активы", 90),
    ("liability", "taxes_payable", "Налоги к уплате", 40),
    ("liability", "loans_and_interest", "Займы и проценты", 50),
    ("liability", "other_liabilities", "Прочие обязательства", 60),
    ("equity", "owner_capital", "Вклады собственников", 10),
    ("equity", "retained_earnings", "Нераспределённая прибыль прошлых лет", 20),
    ("equity", "current_period_result", "Результат текущего периода", 30),
)


def parse_month(value: str) -> date:
    try:
        parsed = datetime.strptime(value, "%Y-%m").date()
    except ValueError as exc:
        raise ValueError("month must use YYYY-MM format") from exc
    return parsed.replace(day=1)


def month_end(period_month: date) -> date:
    return period_month.replace(day=calendar.monthrange(period_month.year, period_month.month)[1])


def _money(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value)).quantize(MONEY)


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _line_from_compact(
    item: dict[str, Any],
    *,
    section: BalanceSection,
    order: int,
    source_key: str,
) -> BalanceLineDraft:
    return BalanceLineDraft(
        section=section,
        key=str(item.get("key") or "unknown"),
        label=str(item.get("label") or item.get("key") or "Неизвестная статья"),
        amount=_money(item.get("amount")),
        order=order,
        source_key=source_key,
        source_status=str(item.get("source_status") or "source_missing"),
        source_as_of=_as_date(item.get("as_of")),
        note=(
            "Источник не подтверждён"
            if str(item.get("source_status") or "") in {"source_missing", "source_error"}
            else None
        ),
        include_in_total=bool(item.get("include_in_total", True)),
        source_amount=_money(item.get("source_amount")),
        adjustment_amount=_money(item.get("adjustment_amount")),
        adjusted_amount=_money(item.get("adjusted_amount")),
        recognition_method=(
            str(item.get("recognition_method")) if item.get("recognition_method") else None
        ),
        estimated_count=int(item.get("estimated_count") or 0),
    )


def _build_draft_lines(
    session: Session,
    *,
    balance_date: date,
    access_context: ExecutiveDashboardAuthContext,
) -> tuple[list[BalanceLineDraft], dict[str, Any]]:
    dashboard = build_executive_dashboard(
        session,
        requested_date=balance_date,
        access_context=access_context,
    )
    compact = next(
        (block for block in dashboard.blocks if block.key == "creditors_payables"),
        None,
    )
    summary = compact.summary if compact is not None else {}
    lines: list[BalanceLineDraft] = []
    for order, item in enumerate(summary.get("balance_assets") or [], start=1):
        source_key = {
            "cash": "onec_cash_position",
            "receivables": "onec_customer_receivables",
            "inventory_cost": "onec_inventory_cost",
        }.get(str(item.get("key")), "onec_counterparty_settlements")
        lines.append(
            _line_from_compact(
                item,
                section="asset",
                order=order * 10,
                source_key=source_key,
            )
        )
    for order, item in enumerate(summary.get("balance_liabilities") or [], start=1):
        lines.append(
            _line_from_compact(
                item,
                section="liability",
                order=order * 10,
                source_key="onec_counterparty_settlements",
            )
        )
    for order, item in enumerate(summary.get("balance_equity") or [], start=1):
        lines.append(
            _line_from_compact(
                item,
                section="equity",
                order=order * 10,
                source_key="management_service_accruals",
            )
        )

    settings = get_settings()
    accounting_configured = bool(settings.executive_management_balance_accounting_database_url)
    accounting_status = "source_unverified" if accounting_configured else "source_missing"
    accounting_note = (
        "Read-only КА/БП настроена, но сопоставление счетов ещё не прошло контрольную сверку"
        if accounting_configured
        else "Не настроено read-only подключение к КА/БП"
    )
    for section, key, label, order in _ACCOUNTING_LINES:
        lines.append(
            BalanceLineDraft(
                section=section,
                key=key,
                label=label,
                amount=None,
                order=order,
                source_key="ka_bp_accounting",
                source_status=accounting_status,
                source_as_of=None,
                note=accounting_note,
            )
        )

    source_summary = {
        "trade_detail": {
            "status": compact.source_status if compact is not None else "source_missing",
            "as_of": compact.as_of.isoformat() if compact and compact.as_of else None,
        },
        "inventory": {
            "status": "source_unverified",
            "note": "СтоимостьОстаток не прошла сверку с типовым отчётом 1С",
        },
        "accounting": {
            "configured": accounting_configured,
            "status": accounting_status,
            "note": accounting_note,
        },
    }
    return lines, source_summary


def _validation_errors(lines: list[BalanceLineDraft]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    missing = [line.label for line in lines if line.amount is None]
    if missing:
        errors.append(
            {
                "code": "mandatory_sources_incomplete",
                "severity": "error",
                "message": "Не подтверждены обязательные статьи: " + ", ".join(missing),
            }
        )
    inventory = next((line for line in lines if line.key == "inventory_cost"), None)
    if inventory is None or inventory.amount is None:
        errors.append(
            {
                "code": "inventory_source_unverified",
                "severity": "error",
                "message": "Стоимость товара не сверена с отчётом по партиям и КА/БП",
            }
        )
    elif inventory.amount < 0:
        errors.append(
            {
                "code": "negative_inventory_cost",
                "severity": "error",
                "message": "Отрицательная стоимость товара блокирует закрытие месяца",
            }
        )
    accrual_lines = [
        line
        for line in lines
        if line.source_key == "management_service_accruals"
        or line.recognition_method in {"accrual", "approved_fixed_monthly_rule"}
    ]
    if any(
        line.source_status in {"partial", "source_missing", "source_error", "source_unverified"}
        for line in accrual_lines
    ):
        errors.append(
            {
                "code": "service_accrual_source_incomplete",
                "severity": "error",
                "message": (
                    "Начисления услуг не закрыты: источник договоров или закрывающих "
                    "документов не прошёл контроль"
                ),
            }
        )
    return errors


def _totals(
    lines: list[BalanceLineDraft],
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    def total(section: BalanceSection) -> Decimal:
        return sum(
            (
                line.amount
                for line in lines
                if line.section == section and line.amount is not None and line.include_in_total
            ),
            # Informational reconciliation rows are visible in the response,
            # but only the adjusted line participates in balance totals.
            Decimal("0"),
        ).quantize(MONEY)

    assets = total("asset")
    liabilities = total("liability")
    equity = total("equity")
    return assets, liabilities, equity, (assets - liabilities - equity).quantize(MONEY)


def build_and_persist_management_balance_snapshot(
    session: Session,
    *,
    balance_date: date | None = None,
    view: BalanceView = "operational",
    actor: str = "system:daily",
) -> ExecutiveManagementBalanceSnapshot:
    today = date.today()
    actual_date = balance_date or today
    period_month = actual_date.replace(day=1)
    if view == "operational" and period_month != today.replace(day=1):
        raise ValueError("operational snapshot can only be built for the current month")
    if view == "closed" and actual_date != month_end(period_month):
        raise ValueError("closed draft must use the last calendar day of the month")

    lines, source_summary = _build_draft_lines(
        session,
        balance_date=actual_date,
        access_context=full_executive_dashboard_context(),
    )
    assets, liabilities, equity, imbalance = _totals(lines)
    errors = _validation_errors(lines)
    tolerance = Decimal(str(get_settings().executive_management_balance_tolerance_rub))
    if abs(imbalance) > tolerance:
        errors.append(
            {
                "code": "balance_mismatch",
                "severity": "error",
                "message": f"Расхождение сторон {imbalance} ₽ превышает допуск {tolerance} ₽",
            }
        )
    version = (
        session.scalar(
            select(func.max(ExecutiveManagementBalanceSnapshot.version)).where(
                ExecutiveManagementBalanceSnapshot.period_month == period_month,
                ExecutiveManagementBalanceSnapshot.view_mode == view,
            )
        )
        or 0
    ) + 1
    generated_at = datetime.now(UTC).replace(tzinfo=None)
    snapshot = ExecutiveManagementBalanceSnapshot(
        period_month=period_month,
        balance_date=actual_date,
        view_mode=view,
        version=version,
        status="draft",
        source_status="partial" if errors else "ready",
        freshness_status="fresh",
        assets_total=assets,
        liabilities_total=liabilities,
        equity_total=equity,
        imbalance_amount=imbalance,
        source_summary=source_summary,
        validation_errors=errors,
        generated_at=generated_at,
    )
    session.add(snapshot)
    session.flush()
    for line in lines:
        session.add(
            ExecutiveManagementBalanceLine(
                snapshot_id=snapshot.id,
                section=line.section,
                line_key=line.key,
                label=line.label,
                amount=line.amount,
                display_order=line.order,
                source_key=line.source_key,
                source_status=line.source_status,
                source_as_of=line.source_as_of,
                note=line.note,
                payload={
                    "include_in_total": line.include_in_total,
                    "source_amount": (
                        str(line.source_amount) if line.source_amount is not None else None
                    ),
                    "adjustment_amount": (
                        str(line.adjustment_amount) if line.adjustment_amount is not None else None
                    ),
                    "adjusted_amount": (
                        str(line.adjusted_amount) if line.adjusted_amount is not None else None
                    ),
                    "recognition_method": line.recognition_method,
                    "estimated_count": line.estimated_count,
                },
            )
        )
    session.add(
        ExecutiveManagementBalanceAudit(
            snapshot_id=snapshot.id,
            action="generated",
            actor=actor,
            payload={"view": view, "balance_date": actual_date.isoformat()},
        )
    )
    session.commit()
    session.refresh(snapshot)
    return snapshot


def _latest_snapshot(
    session: Session,
    *,
    period_month: date,
    view: BalanceView,
    closed_only: bool = False,
) -> ExecutiveManagementBalanceSnapshot | None:
    statement = select(ExecutiveManagementBalanceSnapshot).where(
        ExecutiveManagementBalanceSnapshot.period_month == period_month,
        ExecutiveManagementBalanceSnapshot.view_mode == view,
    )
    if closed_only:
        statement = statement.where(ExecutiveManagementBalanceSnapshot.status == "closed")
    return session.scalar(
        statement.order_by(ExecutiveManagementBalanceSnapshot.version.desc()).limit(1)
    )


def _available_months(session: Session) -> list[str]:
    values = list(
        session.scalars(
            select(ExecutiveManagementBalanceSnapshot.period_month)
            .distinct()
            .order_by(ExecutiveManagementBalanceSnapshot.period_month.desc())
        )
    )
    current = date.today().replace(day=1)
    if current not in values:
        values.insert(0, current)
    return [value.strftime("%Y-%m") for value in values]


def _previous_snapshot(
    session: Session,
    snapshot: ExecutiveManagementBalanceSnapshot,
) -> ExecutiveManagementBalanceSnapshot | None:
    return session.scalar(
        select(ExecutiveManagementBalanceSnapshot)
        .where(
            ExecutiveManagementBalanceSnapshot.period_month < snapshot.period_month,
            ExecutiveManagementBalanceSnapshot.status == "closed",
        )
        .order_by(
            ExecutiveManagementBalanceSnapshot.period_month.desc(),
            ExecutiveManagementBalanceSnapshot.version.desc(),
        )
        .limit(1)
    )


def _response(
    session: Session,
    snapshot: ExecutiveManagementBalanceSnapshot,
) -> ExecutiveManagementBalanceResponse:
    lines = list(
        session.scalars(
            select(ExecutiveManagementBalanceLine)
            .where(ExecutiveManagementBalanceLine.snapshot_id == snapshot.id)
            .order_by(
                ExecutiveManagementBalanceLine.section,
                ExecutiveManagementBalanceLine.display_order,
            )
        )
    )
    previous = _previous_snapshot(session, snapshot)
    previous_amounts: dict[tuple[str, str], Decimal | None] = {}
    if previous is not None:
        previous_amounts = {
            (line.section, line.line_key): line.amount
            for line in session.scalars(
                select(ExecutiveManagementBalanceLine).where(
                    ExecutiveManagementBalanceLine.snapshot_id == previous.id
                )
            )
        }

    def item(line: ExecutiveManagementBalanceLine) -> ExecutiveManagementBalanceLineItem:
        old_amount = previous_amounts.get((line.section, line.line_key))
        delta = None
        if line.amount is not None and old_amount is not None:
            delta = (line.amount - old_amount).quantize(MONEY)
        return ExecutiveManagementBalanceLineItem(
            key=line.line_key,
            label=line.label,
            section=line.section,  # type: ignore[arg-type]
            amount=line.amount,
            delta_previous=delta,
            source_key=line.source_key,
            source_status=line.source_status,
            source_as_of=line.source_as_of,
            note=line.note,
            source_amount=_money((line.payload or {}).get("source_amount")),
            adjustment_amount=_money((line.payload or {}).get("adjustment_amount")),
            adjusted_amount=_money((line.payload or {}).get("adjusted_amount")),
            recognition_method=(line.payload or {}).get("recognition_method"),
            estimated_count=int((line.payload or {}).get("estimated_count") or 0),
        )

    items = [item(line) for line in lines]
    tolerance = Decimal(str(get_settings().executive_management_balance_tolerance_rub))
    can_close = (
        snapshot.view_mode == "closed"
        and snapshot.status == "draft"
        and not snapshot.validation_errors
        and abs(snapshot.imbalance_amount) <= tolerance
    )
    return ExecutiveManagementBalanceResponse(
        month=snapshot.period_month.strftime("%Y-%m"),
        balance_date=snapshot.balance_date,
        view=snapshot.view_mode,  # type: ignore[arg-type]
        version=snapshot.version,
        status=snapshot.status,
        source_status=snapshot.source_status,
        freshness_status=snapshot.freshness_status,
        generated_at=snapshot.generated_at,
        closed_at=snapshot.closed_at,
        closed_by=snapshot.closed_by,
        assets=[entry for entry in items if entry.section == "asset"],
        liabilities=[entry for entry in items if entry.section == "liability"],
        equity=[entry for entry in items if entry.section == "equity"],
        assets_total=snapshot.assets_total,
        liabilities_total=snapshot.liabilities_total,
        equity_total=snapshot.equity_total,
        liabilities_and_equity_total=(snapshot.liabilities_total + snapshot.equity_total).quantize(
            MONEY
        ),
        imbalance_amount=snapshot.imbalance_amount,
        can_close=can_close,
        validation_errors=list(snapshot.validation_errors or []),
        available_months=_available_months(session),
        note=(
            "Полный баланс не подтверждён: до сверки КА/БП и стоимости товара "
            "отчёт остаётся частичным."
            if snapshot.status != "closed"
            else "Закрытый месяц; снимок этой версии неизменяем."
        ),
    )


def get_management_balance(
    session: Session,
    *,
    month: str | None,
    view: BalanceView | None,
    access_context: ExecutiveDashboardAuthContext,
) -> ExecutiveManagementBalanceResponse:
    if month is None and view in (None, "closed"):
        latest_closed = session.scalar(
            select(ExecutiveManagementBalanceSnapshot)
            .where(ExecutiveManagementBalanceSnapshot.status == "closed")
            .order_by(
                ExecutiveManagementBalanceSnapshot.period_month.desc(),
                ExecutiveManagementBalanceSnapshot.version.desc(),
            )
            .limit(1)
        )
        if latest_closed is not None:
            return _response(session, latest_closed)

    period_month = parse_month(month) if month else date.today().replace(day=1)
    requested_view: BalanceView = view or "operational"
    snapshot = _latest_snapshot(
        session,
        period_month=period_month,
        view=requested_view,
    )
    if (
        snapshot is None
        and requested_view == "operational"
        and period_month == date.today().replace(day=1)
    ):
        snapshot = build_and_persist_management_balance_snapshot(
            session,
            balance_date=date.today(),
            view="operational",
            actor=access_context.actor,
        )
    if snapshot is None:
        raise ManagementBalanceNotFoundError(
            f"Нет снимка баланса за {period_month:%Y-%m} в режиме {requested_view}"
        )
    return _response(session, snapshot)


def close_management_balance(
    session: Session,
    *,
    month: str,
    actor: str,
    confirm: bool,
    note: str | None,
) -> ExecutiveManagementBalanceResponse:
    if not confirm:
        raise ManagementBalanceCloseError("Для закрытия требуется явное подтверждение")
    period_month = parse_month(month)
    if period_month >= date.today().replace(day=1):
        raise ManagementBalanceCloseError("Текущий или будущий месяц закрыть нельзя")
    already_closed = _latest_snapshot(
        session,
        period_month=period_month,
        view="closed",
        closed_only=True,
    )
    if already_closed is not None:
        return _response(session, already_closed)
    snapshot = _latest_snapshot(
        session,
        period_month=period_month,
        view="closed",
    )
    if snapshot is None:
        raise ManagementBalanceNotFoundError(f"Нет подготовленного снимка за {period_month:%Y-%m}")
    if snapshot.validation_errors:
        raise ManagementBalanceCloseError(
            "Месяц не закрыт: есть ошибки источников или контрольных сверок"
        )
    tolerance = Decimal(str(get_settings().executive_management_balance_tolerance_rub))
    if abs(snapshot.imbalance_amount) > tolerance:
        raise ManagementBalanceCloseError("Месяц не закрыт: стороны баланса не равны")

    snapshot.status = "closed"
    snapshot.closed_at = datetime.now(UTC).replace(tzinfo=None)
    snapshot.closed_by = actor
    snapshot.close_note = note
    session.add(
        ExecutiveManagementBalanceAudit(
            snapshot_id=snapshot.id,
            action="closed",
            actor=actor,
            payload={"note": note},
        )
    )
    session.commit()
    session.refresh(snapshot)
    return _response(session, snapshot)
