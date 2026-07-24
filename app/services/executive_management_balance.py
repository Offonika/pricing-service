from __future__ import annotations

import calendar
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.contracts import ContractIntegrityError, read_json_contract
from app.models.executive_dashboard import (
    ExecutiveManagementBalanceAudit,
    ExecutiveManagementBalanceLine,
    ExecutiveManagementBalanceSnapshot,
)
from app.schemas.executive_dashboard import (
    ExecutiveManagementBalanceLineItem,
    ExecutiveManagementBalanceResponse,
    ExecutiveManagementBalanceTurnoverLine,
    ExecutiveManagementBalanceTurnoverResponse,
    ExecutiveManagementBalanceTurnoverTotal,
)
from app.services.bitrix_executive_dashboard_auth import (
    ExecutiveDashboardAuthContext,
    full_executive_dashboard_context,
)
from app.services.executive_dashboard import build_executive_dashboard

BalanceView = Literal["closed", "operational"]
BalanceSection = Literal["asset", "liability", "equity"]
BalanceTrigger = Literal["cron", "manual"]
MONEY = Decimal("0.01")
OPENING_EQUITY_BASELINE_DATE = date(2026, 1, 1)


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


@dataclass(frozen=True)
class ManagementBalanceSnapshotBuildResult:
    snapshot: ExecutiveManagementBalanceSnapshot
    outcome: Literal["inserted", "noop"]


_ACCOUNTING_LINES: tuple[tuple[BalanceSection, str, str, int], ...] = (
    ("asset", "fixed_assets_net", "Основные средства за вычетом амортизации", 70),
    ("asset", "tax_receivables", "Налоги к возмещению", 80),
    ("asset", "other_assets", "Прочие активы", 90),
    ("liability", "taxes_payable", "Налоги к уплате", 40),
    ("liability", "loans_and_interest", "Займы и проценты", 50),
    ("liability", "other_liabilities", "Прочие обязательства", 60),
    ("equity", "owner_capital", "Уставный и добавочный капитал по КА/БП", 10),
    ("equity", "retained_earnings", "Нераспределённая прибыль прошлых лет", 20),
    ("equity", "prior_period_adjustments", "Корректировки прошлых периодов", 25),
    ("equity", "current_period_result", "Результат текущего периода", 30),
)


_BP_BALANCE_LINE_LAYOUT: tuple[tuple[BalanceSection, str, str, int], ...] = (
    ("asset", "fixed_assets_net", "Основные средства за вычетом амортизации", 70),
    ("asset", "tax_receivables", "Налоги к возмещению", 80),
    ("liability", "loans_and_interest", "Займы и проценты", 50),
    ("liability", "other_liabilities", "Прочие обязательства", 60),
    ("equity", "owner_capital", "Уставный и добавочный капитал по КА/БП", 10),
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
            str(item.get("note"))
            if item.get("note")
            else (
                "Источник не подтверждён"
                if str(item.get("source_status") or "") in {"source_missing", "source_error"}
                else None
            )
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


def _unavailable_contract_lines(
    *,
    layout: tuple[tuple[BalanceSection, str, str, int], ...],
    source_key: str,
    source_status: str,
    source_as_of: date | None,
    note: str,
) -> list[BalanceLineDraft]:
    return [
        BalanceLineDraft(
            section=section,
            key=key,
            label=label,
            amount=None,
            order=order,
            source_key=source_key,
            source_status=source_status,
            source_as_of=source_as_of,
            note=note,
        )
        for section, key, label, order in layout
    ]


def _load_bp_balance_lines(
    *,
    balance_date: date,
    snapshot_path: str,
) -> tuple[list[BalanceLineDraft], dict[str, Any]]:
    path = Path(snapshot_path)
    source_key = "onec_bp_balance"
    try:
        payload = read_json_contract(path)
    except FileNotFoundError:
        note = "Не опубликован read-only снимок балансовых счетов БП"
        return (
            _unavailable_contract_lines(
                layout=_BP_BALANCE_LINE_LAYOUT,
                source_key=source_key,
                source_status="source_missing",
                source_as_of=None,
                note=note,
            ),
            {"configured": False, "status": "source_missing", "note": note},
        )
    except (ContractIntegrityError, OSError, ValueError, json.JSONDecodeError) as exc:
        note = f"Снимок балансовых счетов БП не прошёл проверку: {type(exc).__name__}"
        return (
            _unavailable_contract_lines(
                layout=_BP_BALANCE_LINE_LAYOUT,
                source_key=source_key,
                source_status="source_error",
                source_as_of=None,
                note=note,
            ),
            {"configured": True, "status": "source_error", "note": note},
        )

    source_as_of = _as_date(payload.get("as_of"))
    raw_lines = payload.get("lines")
    if source_as_of is None or not isinstance(raw_lines, dict):
        note = "Снимок балансовых счетов БП не содержит обязательную дату или строки"
        return (
            _unavailable_contract_lines(
                layout=_BP_BALANCE_LINE_LAYOUT,
                source_key=source_key,
                source_status="source_error",
                source_as_of=source_as_of,
                note=note,
            ),
            {"configured": True, "status": "source_error", "note": note},
        )
    if source_as_of != balance_date:
        status = "stale" if source_as_of < balance_date else "source_error"
        note = (
            f"Снимок балансовых счетов БП рассчитан на {source_as_of.isoformat()}, "
            f"требуется дата {balance_date.isoformat()}"
        )
        return (
            _unavailable_contract_lines(
                layout=_BP_BALANCE_LINE_LAYOUT,
                source_key=source_key,
                source_status=status,
                source_as_of=source_as_of,
                note=note,
            ),
            {
                "configured": True,
                "status": status,
                "as_of": source_as_of.isoformat(),
                "note": note,
            },
        )

    result: list[BalanceLineDraft] = []
    for section, key, label, order in _BP_BALANCE_LINE_LAYOUT:
        raw = raw_lines.get(key)
        if not isinstance(raw, dict):
            result.append(
                BalanceLineDraft(
                    section,
                    key,
                    label,
                    None,
                    order,
                    source_key,
                    "source_error",
                    source_as_of,
                    f"В снимке БП отсутствует строка {key}",
                )
            )
            continue
        result.append(
            BalanceLineDraft(
                section=section,
                key=key,
                label=label,
                amount=_money(raw.get("amount")),
                order=order,
                source_key=str(raw.get("source_key") or source_key),
                source_status=str(raw.get("source_status") or "source_error"),
                source_as_of=source_as_of,
                note=str(raw.get("note")) if raw.get("note") else None,
            )
        )
    return (
        result,
        {
            "configured": True,
            "status": str(payload.get("source_status") or "source_error"),
            "as_of": source_as_of.isoformat(),
            "contract_version": payload.get("contract_version"),
            "excluded": list(payload.get("excluded") or []),
        },
    )


def _load_opening_equity_lines(
    *,
    balance_date: date,
    snapshot_path: str,
) -> tuple[list[BalanceLineDraft], dict[str, Any]]:
    if balance_date < OPENING_EQUITY_BASELINE_DATE:
        return [], {
            "configured": True,
            "status": "not_applicable",
            "baseline_date": OPENING_EQUITY_BASELINE_DATE.isoformat(),
        }
    layout: tuple[tuple[BalanceSection, str, str, int], ...] = (
        ("equity", "retained_earnings", "Входящий управленческий капитал", 20),
        (
            "equity",
            "prior_period_adjustments",
            "Корректировки прошлых периодов",
            25,
        ),
    )
    path = Path(snapshot_path)
    try:
        payload = read_json_contract(path)
    except FileNotFoundError:
        note = "Не опубликован контракт входящего управленческого капитала"
        return (
            _unavailable_contract_lines(
                layout=layout,
                source_key="management_opening_equity",
                source_status="source_missing",
                source_as_of=OPENING_EQUITY_BASELINE_DATE,
                note=note,
            ),
            {"configured": False, "status": "source_missing", "note": note},
        )
    except (ContractIntegrityError, OSError, ValueError, json.JSONDecodeError) as exc:
        note = f"Контракт входящего капитала не прошёл проверку: {type(exc).__name__}"
        return (
            _unavailable_contract_lines(
                layout=layout,
                source_key="management_opening_equity",
                source_status="source_error",
                source_as_of=OPENING_EQUITY_BASELINE_DATE,
                note=note,
            ),
            {"configured": True, "status": "source_error", "note": note},
        )

    baseline_date = _as_date(payload.get("baseline_date"))
    raw_lines = payload.get("lines")
    if baseline_date != OPENING_EQUITY_BASELINE_DATE or not isinstance(raw_lines, dict):
        note = "Контракт входящего капитала содержит неверную базовую дату или строки"
        return (
            _unavailable_contract_lines(
                layout=layout,
                source_key="management_opening_equity",
                source_status="source_error",
                source_as_of=baseline_date,
                note=note,
            ),
            {"configured": True, "status": "source_error", "note": note},
        )

    result: list[BalanceLineDraft] = []
    for section, key, label, order in layout:
        raw = raw_lines.get(key)
        if not isinstance(raw, dict) or _money(raw.get("amount")) is None:
            result.append(
                BalanceLineDraft(
                    section,
                    key,
                    label,
                    None,
                    order,
                    "management_opening_equity",
                    "source_error",
                    baseline_date,
                    f"В контракте входящего капитала отсутствует сумма {key}",
                )
            )
            continue
        result.append(
            BalanceLineDraft(
                section=section,
                key=key,
                label=label,
                amount=_money(raw.get("amount")),
                order=order,
                source_key=str(raw.get("source_key") or "management_opening_equity"),
                source_status=str(
                    raw.get("source_status") or payload.get("source_status") or "ready"
                ),
                source_as_of=baseline_date,
                note=str(raw.get("note")) if raw.get("note") else None,
                recognition_method=(
                    "frozen_opening_equity"
                    if key == "retained_earnings"
                    else "prior_period_adjustment"
                ),
            )
        )
    source_cutoff_date = _as_date(payload.get("source_cutoff_date"))
    if balance_date == baseline_date:
        for index, raw in enumerate(payload.get("components") or [], start=1):
            if not isinstance(raw, dict):
                continue
            section = str(raw.get("section"))
            key = str(raw.get("key") or "")
            if section not in {"asset", "liability", "equity"} or not key:
                continue
            if key in {"retained_earnings", "prior_period_adjustments"}:
                continue
            label = str(raw.get("label") or key)
            raw_order = raw.get("order")
            order = int(raw_order) if isinstance(raw_order, int) else index * 10
            result.append(
                BalanceLineDraft(
                    section=section,  # type: ignore[arg-type]
                    key=key,
                    label=label,
                    amount=_money(raw.get("amount")),
                    order=order,
                    source_key=str(raw.get("source_key") or "management_opening_equity"),
                    source_status=str(raw.get("source_status") or "source_error"),
                    source_as_of=(
                        _as_date(raw.get("as_of")) or source_cutoff_date or baseline_date
                    ),
                    note=str(raw.get("note")) if raw.get("note") else None,
                    include_in_total=bool(raw.get("include_in_total", True)),
                    recognition_method=(
                        str(raw.get("recognition_method"))
                        if raw.get("recognition_method")
                        else None
                    ),
                    estimated_count=int(raw.get("estimated_count") or 0),
                )
            )
    return (
        result,
        {
            "configured": True,
            "status": str(payload.get("source_status") or "source_error"),
            "baseline_date": baseline_date.isoformat(),
            "source_cutoff_date": (source_cutoff_date.isoformat() if source_cutoff_date else None),
            "version": int(payload.get("version") or 0),
            "source_hash": payload.get("source_hash"),
            "contract_version": payload.get("contract_version"),
            "calculation_method": payload.get("calculation_method"),
            "automatic": True,
            "daily_balancing_forbidden": bool(
                (payload.get("control") or {}).get("daily_balancing_forbidden")
            ),
            "unresolved": list(payload.get("unresolved") or []),
            "excluded": list(payload.get("excluded") or []),
            "baseline_bridge": dict(payload.get("bridge") or {}),
        },
    )


def _merge_opening_equity_lines(
    *,
    lines: list[BalanceLineDraft],
    opening_lines: list[BalanceLineDraft],
    balance_date: date,
) -> list[BalanceLineDraft]:
    result = list(lines)
    if balance_date == OPENING_EQUITY_BASELINE_DATE:
        result = [line for line in result if line.amount is None or not line.include_in_total]
    for line in opening_lines:
        result = [existing for existing in result if existing.key != line.key]
        result.append(line)
    return result


def _load_bp_tax_line(
    *,
    balance_date: date,
    snapshot_path: str,
) -> tuple[BalanceLineDraft, dict[str, Any]]:
    path = Path(snapshot_path)
    base = {
        "section": "liability",
        "key": "taxes_payable",
        "label": "Налоги к уплате",
        "order": 40,
        "source_key": "onec_bp_tax_accounting",
    }
    try:
        payload = read_json_contract(path)
    except FileNotFoundError:
        note = "Не опубликован read-only снимок налогов из БП"
        return (
            BalanceLineDraft(
                **base,
                amount=None,
                source_status="source_missing",
                source_as_of=None,
                note=note,
            ),
            {"configured": False, "status": "source_missing", "note": note},
        )
    except (ContractIntegrityError, OSError, ValueError, json.JSONDecodeError) as exc:
        note = f"Снимок налогов из БП не прошёл проверку: {type(exc).__name__}"
        return (
            BalanceLineDraft(
                **base,
                amount=None,
                source_status="source_error",
                source_as_of=None,
                note=note,
            ),
            {"configured": True, "status": "source_error", "note": note},
        )

    raw_line = (payload.get("lines") or {}).get("taxes_payable")
    source_as_of = _as_date(payload.get("as_of"))
    if not isinstance(raw_line, dict) or source_as_of is None:
        note = "Снимок налогов из БП не содержит обязательную строку или дату"
        return (
            BalanceLineDraft(
                **base,
                amount=None,
                source_status="source_error",
                source_as_of=source_as_of,
                note=note,
            ),
            {"configured": True, "status": "source_error", "note": note},
        )
    if source_as_of != balance_date:
        status = "stale" if source_as_of < balance_date else "source_error"
        note = (
            f"Снимок налогов из БП рассчитан на {source_as_of.isoformat()}, "
            f"требуется дата {balance_date.isoformat()}"
        )
        return (
            BalanceLineDraft(
                **base,
                amount=None,
                source_status=status,
                source_as_of=source_as_of,
                note=note,
            ),
            {
                "configured": True,
                "status": status,
                "as_of": source_as_of.isoformat(),
                "note": note,
            },
        )
    amount = _money(raw_line.get("amount"))
    if amount is None or amount < 0 or str(raw_line.get("source_status")) != "ready":
        note = "Снимок налогов из БП содержит неподтверждённую сумму"
        return (
            BalanceLineDraft(
                **base,
                amount=None,
                source_status="source_error",
                source_as_of=source_as_of,
                note=note,
            ),
            {
                "configured": True,
                "status": "source_error",
                "as_of": source_as_of.isoformat(),
                "note": note,
            },
        )
    note = str(raw_line.get("note") or "Кредитовое сальдо счетов 68/69 из 1С БП")
    return (
        BalanceLineDraft(
            **base,
            amount=amount,
            source_status="ready",
            source_as_of=source_as_of,
            note=note,
        ),
        {
            "configured": True,
            "status": "ready",
            "as_of": source_as_of.isoformat(),
            "note": note,
        },
    )


_PAYROLL_LINE_LAYOUT: tuple[tuple[BalanceSection, str, int], ...] = (
    ("asset", "official_salary_advances", 45),
    ("asset", "employee_receivables", 50),
    ("liability", "official_salary_payable", 25),
    ("liability", "management_salary_payable", 26),
    ("liability", "other_employee_settlements", 27),
    ("liability", "service_employee_settlements", 28),
)


def _load_salary_reconciliation_lines(
    *,
    balance_date: date,
    snapshot_path: str,
) -> tuple[list[BalanceLineDraft], dict[str, Any]]:
    path = Path(snapshot_path)

    def unavailable(status: str, note: str) -> tuple[list[BalanceLineDraft], dict[str, Any]]:
        labels = {
            "official_salary_advances": "Авансы/переплата по официальной зарплате",
            "employee_receivables": "Дебиторка сотрудников",
            "official_salary_payable": "Зарплата к выплате — официальная",
            "management_salary_payable": "Зарплата к выплате — управленческая часть",
            "other_employee_settlements": "Прочие расчёты с сотрудниками",
            "service_employee_settlements": "Служебные расчёты с сотрудниками",
        }
        return (
            [
                BalanceLineDraft(
                    section=section,
                    key=key,
                    label=labels[key],
                    amount=None,
                    order=order,
                    source_key="ut_bp_salary_reconciliation",
                    source_status=status,
                    source_as_of=None,
                    note=note,
                )
                for section, key, order in _PAYROLL_LINE_LAYOUT
            ],
            {
                "configured": status != "source_missing",
                "status": status,
                "as_of": None,
                "closing_blocked": True,
                "blockers": [status],
                "note": note,
            },
        )

    try:
        payload = read_json_contract(path)
    except FileNotFoundError:
        return unavailable(
            "source_missing", "Не опубликован единый снимок зарплатной задолженности"
        )
    except (ContractIntegrityError, OSError, ValueError, json.JSONDecodeError) as exc:
        return unavailable(
            "source_error",
            f"Снимок зарплатной задолженности не прошёл проверку: {type(exc).__name__}",
        )

    source_as_of = _as_date(payload.get("as_of"))
    if source_as_of != balance_date:
        status = "stale" if source_as_of and source_as_of < balance_date else "source_error"
        return unavailable(
            status,
            "Снимок зарплатной задолженности рассчитан не на дату баланса",
        )
    raw_lines = payload.get("lines")
    control = payload.get("control")
    if not isinstance(raw_lines, dict) or not isinstance(control, dict):
        return unavailable("source_error", "В зарплатном снимке отсутствуют строки или контроль")

    result: list[BalanceLineDraft] = []
    for section, key, order in _PAYROLL_LINE_LAYOUT:
        raw = raw_lines.get(key)
        if not isinstance(raw, dict):
            return unavailable("source_error", f"В зарплатном снимке отсутствует строка {key}")
        amount = _money(raw.get("amount"))
        status = str(raw.get("source_status") or "source_error")
        if amount is not None and amount < 0:
            return unavailable("source_error", f"В зарплатной строке {key} отрицательная сумма")
        result.append(
            BalanceLineDraft(
                section=section,
                key=key,
                label=str(raw.get("label") or key),
                amount=amount,
                order=order,
                source_key=str(raw.get("source_key") or "ut_bp_salary_reconciliation"),
                source_status=status,
                source_as_of=source_as_of,
                note=str(raw.get("note")) if raw.get("note") else None,
                include_in_total=bool(raw.get("include_in_total", True)),
                recognition_method="gross_employee_balance",
            )
        )
    summary = {
        "configured": True,
        "status": str(payload.get("source_status") or "source_error"),
        "as_of": source_as_of.isoformat() if source_as_of else None,
        "closing_blocked": bool(control.get("closing_blocked", True)),
        "blockers": list(control.get("blockers") or []),
        "mapping": control.get("mapping") or {},
        "unconfirmed_amount": control.get("unconfirmed_amount"),
        "duplicate_payment_amount": control.get("duplicate_payment_amount"),
        "ambiguous_payment_amount": control.get("ambiguous_payment_amount"),
        "ambiguous_duplicate_count": int(control.get("ambiguous_duplicate_count") or 0),
        "account70_reconciliation_difference": control.get("account70_reconciliation_difference"),
        "source_summary": payload.get("source_summary") or {},
    }
    return result, summary


def _load_owner_dividends_line(
    *,
    balance_date: date,
    snapshot_path: str,
    accounting_includes_dividends: bool,
    max_lag_days: int,
) -> BalanceLineDraft:
    path = Path(snapshot_path)
    base = {
        "section": "equity",
        "key": "dividends_paid_ytd",
        "label": "Выплаченные дивиденды",
        "order": 40,
        "source_key": "management_owner_cash_control",
    }
    try:
        payload = read_json_contract(path)
    except FileNotFoundError:
        return BalanceLineDraft(
            **base,
            amount=None,
            source_status="source_missing",
            source_as_of=None,
            note="Не опубликован снимок движения ДДС по дивидендам",
        )
    except (ContractIntegrityError, OSError, ValueError, json.JSONDecodeError) as exc:
        return BalanceLineDraft(
            **base,
            amount=None,
            source_status="source_error",
            source_as_of=None,
            note=f"Снимок движения ДДС по дивидендам не прошёл проверку: {type(exc).__name__}",
        )

    summary = payload.get("summary")
    source_as_of = _as_date(payload.get("as_of"))
    if not isinstance(summary, dict) or source_as_of is None:
        return BalanceLineDraft(
            **base,
            amount=None,
            source_status="source_error",
            source_as_of=source_as_of,
            note="Снимок движения ДДС не содержит итог по дивидендам или дату",
        )

    dividends_ytd = _money(summary.get("dividends_ytd"))
    dividends_month = _money(summary.get("dividends_current_month"))
    if dividends_ytd is None or dividends_ytd < 0:
        return BalanceLineDraft(
            **base,
            amount=None,
            source_status="source_error",
            source_as_of=source_as_of,
            note="Снимок движения ДДС содержит неподтверждённую сумму дивидендов",
        )

    source_status = str(payload.get("source_status") or "ready")
    lag_days = (balance_date - source_as_of).days
    if lag_days < 0:
        source_status = "source_error"
    elif lag_days > max_lag_days:
        source_status = "stale"
    elif source_status not in {"ready", "partial"}:
        source_status = "source_error"

    warning_count = int(summary.get("dividend_comment_warning_count") or 0)
    notes = [
        "Накопительно с начала года; выплата дивидендов уменьшает собственные средства",
    ]
    if dividends_month is not None:
        notes.append(f"за текущий месяц {dividends_month} ₽")
    if warning_count:
        notes.append(f"{warning_count} РКО требуют проверки назначения платежа")
    if accounting_includes_dividends:
        notes.append("информационно — капитал уже берётся из КА/БП")

    return BalanceLineDraft(
        **base,
        amount=-dividends_ytd,
        source_status=source_status,
        source_as_of=source_as_of,
        note="; ".join(notes),
        include_in_total=not accounting_includes_dividends,
        source_amount=dividends_ytd,
        adjustment_amount=(-dividends_month if dividends_month is not None else None),
        recognition_method="equity_distribution",
    )


def _build_draft_lines(
    session: Session,
    *,
    balance_date: date,
    access_context: ExecutiveDashboardAuthContext,
    include_contract_enrichment: bool = True,
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
        if str(item.get("key")) == "employee_receivables":
            continue
        source_key = {
            "cash": "onec_cash_position",
            "receivables": "onec_customer_receivables",
            "inventory_cost": "onec_inventory_cost",
            "owner_cash_in_transit": "management_owner_cash_control",
            "owner_related_party_unresolved": "management_owner_cash_control",
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
        if str(item.get("key")) == "employees":
            continue
        source_key = (
            "management_owner_cash_control"
            if str(item.get("key")) == "owner_funds_unclassified"
            else "onec_counterparty_settlements"
        )
        lines.append(
            _line_from_compact(
                item,
                section="liability",
                order=order * 10,
                source_key=source_key,
            )
        )
    for order, item in enumerate(summary.get("balance_equity") or [], start=1):
        source_key = {
            "dividends_paid_ytd": "management_owner_cash_control",
            "owner_contributed_funds": "onec_counterparty_settlements",
            "current_period_result": "management_profit_loss",
            "service_accrual_result_adjustment": "management_service_accruals",
        }.get(str(item.get("key")), "ka_bp_accounting")
        lines.append(
            _line_from_compact(
                item,
                section="equity",
                order=order * 10,
                source_key=source_key,
            )
        )

    settings = get_settings()
    salary_lines, salary_summary = _load_salary_reconciliation_lines(
        balance_date=balance_date,
        snapshot_path=settings.executive_management_balance_payroll_snapshot_path,
    )
    lines.extend(salary_lines)
    accounting_configured = bool(settings.executive_management_balance_accounting_database_url)
    if not any(line.key == "dividends_paid_ytd" for line in lines):
        lines.append(
            _load_owner_dividends_line(
                balance_date=balance_date,
                snapshot_path=settings.executive_dashboard_owner_cash_control_snapshot_path,
                accounting_includes_dividends=False,
                max_lag_days=settings.executive_dashboard_source_max_lag_days,
            )
        )
    bp_tax_line, bp_tax_summary = _load_bp_tax_line(
        balance_date=balance_date,
        snapshot_path=settings.executive_management_balance_bp_tax_snapshot_path,
    )
    bp_balance_summary: dict[str, Any] = {
        "configured": False,
        "status": "not_loaded",
        "note": "По принятой методике из БП подключаются только начисленные налоги",
    }
    opening_equity_summary: dict[str, Any] = {
        "configured": False,
        "status": "not_loaded",
    }
    if include_contract_enrichment:
        opening_equity_lines, opening_equity_summary = _load_opening_equity_lines(
            balance_date=balance_date,
            snapshot_path=(settings.executive_management_balance_opening_equity_snapshot_path),
        )
        lines = _merge_opening_equity_lines(
            lines=lines,
            opening_lines=opening_equity_lines,
            balance_date=balance_date,
        )
    accounting_status = "source_unverified" if accounting_configured else "source_missing"
    accounting_note = (
        "Read-only КА/БП настроена, но сопоставление счетов ещё не прошло контрольную сверку"
        if accounting_configured
        else "Не настроено read-only подключение к КА/БП"
    )
    for section, key, label, order in _ACCOUNTING_LINES:
        if key == "taxes_payable":
            if any(line.key == key for line in lines):
                continue
            lines.append(bp_tax_line)
            continue
        if any(line.key == key for line in lines):
            continue
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
            "status": next(
                (line.source_status for line in lines if line.source_key == "onec_inventory_cost"),
                "source_missing",
            ),
            "as_of": next(
                (
                    line.source_as_of.isoformat()
                    for line in lines
                    if line.source_key == "onec_inventory_cost" and line.source_as_of is not None
                ),
                None,
            ),
            "note": next(
                (
                    line.note
                    for line in lines
                    if line.source_key == "onec_inventory_cost" and line.note
                ),
                None,
            ),
        },
        "accounting": {
            "configured": (
                accounting_configured
                or bool(bp_tax_summary.get("configured"))
                or bool(bp_balance_summary.get("configured"))
            ),
            "status": (
                bp_balance_summary.get("status")
                if bp_balance_summary.get("status") not in {None, "not_loaded"}
                else (
                    "partial"
                    if bp_tax_summary.get("status") == "ready"
                    else bp_tax_summary.get("status") or accounting_status
                )
            ),
            "as_of": bp_balance_summary.get("as_of") or bp_tax_summary.get("as_of"),
            "note": (
                "Налоги к уплате подтверждены из БП; управленческие остатки " "берутся из УТ 10.3"
                if bp_tax_summary.get("status") == "ready"
                else (bp_tax_summary.get("note") or "Из БП подключаются только начисленные налоги")
            ),
            "bp_balance": bp_balance_summary,
        },
        "owner_cash_control": {
            "status": next(
                (
                    line.source_status
                    for line in lines
                    if line.source_key == "management_owner_cash_control"
                ),
                "source_missing",
            ),
            "as_of": next(
                (
                    line.source_as_of.isoformat()
                    for line in lines
                    if line.source_key == "management_owner_cash_control"
                    and line.source_as_of is not None
                ),
                None,
            ),
        },
        "salary_reconciliation": salary_summary,
        "opening_equity": opening_equity_summary,
    }
    if opening_equity_summary.get("status") not in {
        None,
        "not_loaded",
        "not_applicable",
        "source_missing",
        "source_error",
    }:
        amounts = {line.key: line.amount for line in lines}
        bridge_keys = (
            "retained_earnings",
            "prior_period_adjustments",
            "owner_capital",
            "owner_contributed_funds",
            "current_period_result",
            "dividends_paid_ytd",
        )
        bridge = {key: str(amounts[key]) for key in bridge_keys if amounts.get(key) is not None}
        bridge_total = sum(
            (amounts.get(key) or Decimal("0") for key in bridge_keys),
            Decimal("0"),
        ).quantize(MONEY)
        opening_equity_summary["bridge"] = {
            **bridge,
            "equity_bridge_total": str(bridge_total),
        }
    return lines, source_summary


def _validation_errors(
    lines: list[BalanceLineDraft],
    source_summary: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
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
    owner_control_lines = [
        line for line in lines if line.source_key == "management_owner_cash_control"
    ]
    if not owner_control_lines or any(
        line.source_status
        in {"partial", "source_missing", "source_error", "source_unverified", "stale"}
        for line in owner_control_lines
    ):
        errors.append(
            {
                "code": "owner_cash_control_incomplete",
                "severity": "error",
                "message": (
                    "Сверка переводов через собственника не завершена или содержит "
                    "незакрытые расхождения"
                ),
            }
        )
    salary = (source_summary or {}).get("salary_reconciliation") or {}
    if salary.get("status") != "ready" or bool(salary.get("closing_blocked", True)):
        blockers = ", ".join(str(item) for item in salary.get("blockers") or [])
        errors.append(
            {
                "code": "salary_reconciliation_incomplete",
                "severity": "error",
                "message": (
                    "Сверка зарплаты УТ/БП не закрыта" + (f": {blockers}" if blockers else "")
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


def _canonical_hash_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            str(key): _canonical_hash_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_hash_value(item) for item in value]
    return value


def _management_balance_content_sha256(
    *,
    balance_date: date,
    view: BalanceView,
    lines: list[BalanceLineDraft],
    assets: Decimal,
    liabilities: Decimal,
    equity: Decimal,
    imbalance: Decimal,
    source_summary: dict[str, Any],
    validation_errors: list[dict[str, Any]],
) -> str:
    ordered_lines = sorted(lines, key=lambda line: (line.section, line.order, line.key))
    payload = {
        "balance_date": balance_date,
        "view_mode": view,
        "lines": [
            {
                "section": line.section,
                "key": line.key,
                "label": line.label,
                "amount": line.amount,
                "order": line.order,
                "source_key": line.source_key,
                "source_status": line.source_status,
                "source_as_of": line.source_as_of,
                "note": line.note,
                "include_in_total": line.include_in_total,
                "source_amount": line.source_amount,
                "adjustment_amount": line.adjustment_amount,
                "adjusted_amount": line.adjusted_amount,
                "recognition_method": line.recognition_method,
                "estimated_count": line.estimated_count,
            }
            for line in ordered_lines
        ],
        "totals": {
            "assets": assets,
            "liabilities": liabilities,
            "equity": equity,
            "imbalance": imbalance,
        },
        "source_summary": source_summary,
        "validation_errors": validation_errors,
    }
    canonical = json.dumps(
        _canonical_hash_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_management_balance_components_export(
    session: Session,
    *,
    balance_date: date,
) -> dict[str, Any]:
    """Build historical balance inputs without persistence or opening-equity recursion."""
    lines, source_summary = _build_draft_lines(
        session,
        balance_date=balance_date,
        access_context=full_executive_dashboard_context(),
        include_contract_enrichment=False,
    )
    assets, liabilities, equity, imbalance = _totals(lines)
    serialized_lines = [
        {
            "section": line.section,
            "key": line.key,
            "label": line.label,
            "amount": str(line.amount) if line.amount is not None else None,
            "order": line.order,
            "source_key": line.source_key,
            "source_status": line.source_status,
            "as_of": line.source_as_of.isoformat() if line.source_as_of else None,
            "note": line.note,
            "include_in_total": line.include_in_total,
            "recognition_method": line.recognition_method,
            "estimated_count": line.estimated_count,
        }
        for line in lines
    ]
    canonical = {
        "as_of": balance_date.isoformat(),
        "lines": serialized_lines,
        "source_summary": source_summary,
    }
    source_hash = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "contract_version": "management-balance-components.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        **canonical,
        "source_hash": source_hash,
        "totals": {
            "assets": str(assets),
            "liabilities": str(liabilities),
            "known_equity": str(equity),
            "pre_opening_imbalance": str(imbalance),
        },
    }


def atomic_write_management_balance_components(
    path: Path,
    payload: dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def build_management_balance_snapshot_command(
    session: Session,
    *,
    balance_date: date | None = None,
    view: BalanceView = "operational",
    actor: str = "system:daily",
    trigger: BalanceTrigger = "manual",
) -> ManagementBalanceSnapshotBuildResult:
    today = date.today()
    actual_date = balance_date or today
    period_month = actual_date.replace(day=1)
    if view == "operational" and period_month != today.replace(day=1):
        raise ValueError("operational snapshot can only be built for the current month")
    if (
        view == "closed"
        and actual_date != month_end(period_month)
        and actual_date != OPENING_EQUITY_BASELINE_DATE
    ):
        raise ValueError("closed draft must use the last calendar day of the month")

    lines, source_summary = _build_draft_lines(
        session,
        balance_date=actual_date,
        access_context=full_executive_dashboard_context(),
    )
    assets, liabilities, equity, imbalance = _totals(lines)
    errors = _validation_errors(lines, source_summary)
    tolerance = Decimal(str(get_settings().executive_management_balance_tolerance_rub))
    if abs(imbalance) > tolerance:
        errors.append(
            {
                "code": "balance_mismatch",
                "severity": "error",
                "message": f"Расхождение сторон {imbalance} ₽ превышает допуск {tolerance} ₽",
            }
        )
    content_sha256 = _management_balance_content_sha256(
        balance_date=actual_date,
        view=view,
        lines=lines,
        assets=assets,
        liabilities=liabilities,
        equity=equity,
        imbalance=imbalance,
        source_summary=source_summary,
        validation_errors=errors,
    )
    existing = session.scalar(
        select(ExecutiveManagementBalanceSnapshot).where(
            ExecutiveManagementBalanceSnapshot.balance_date == actual_date,
            ExecutiveManagementBalanceSnapshot.view_mode == view,
            ExecutiveManagementBalanceSnapshot.content_sha256 == content_sha256,
        )
    )
    if existing is not None:
        return ManagementBalanceSnapshotBuildResult(snapshot=existing, outcome="noop")
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
        content_sha256=content_sha256,
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
            payload={
                "view": view,
                "balance_date": actual_date.isoformat(),
                "trigger": trigger,
            },
        )
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(ExecutiveManagementBalanceSnapshot).where(
                ExecutiveManagementBalanceSnapshot.balance_date == actual_date,
                ExecutiveManagementBalanceSnapshot.view_mode == view,
                ExecutiveManagementBalanceSnapshot.content_sha256 == content_sha256,
            )
        )
        if existing is not None:
            return ManagementBalanceSnapshotBuildResult(snapshot=existing, outcome="noop")
        raise
    except BaseException:
        session.rollback()
        raise
    session.refresh(snapshot)
    return ManagementBalanceSnapshotBuildResult(snapshot=snapshot, outcome="inserted")


def build_and_persist_management_balance_snapshot(
    session: Session,
    *,
    balance_date: date | None = None,
    view: BalanceView = "operational",
    actor: str = "system:daily",
    trigger: BalanceTrigger = "manual",
) -> ExecutiveManagementBalanceSnapshot:
    return build_management_balance_snapshot_command(
        session,
        balance_date=balance_date,
        view=view,
        actor=actor,
        trigger=trigger,
    ).snapshot


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
        if line.line_key == "dividends_paid_ytd":
            monthly_dividends = _money((line.payload or {}).get("adjustment_amount"))
            if monthly_dividends is not None:
                delta = monthly_dividends
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
        source_summary=dict(snapshot.source_summary or {}),
        available_months=_available_months(session),
        note=(
            "Полный баланс не подтверждён: до сверки КА/БП и стоимости товара "
            "отчёт остаётся частичным."
            if snapshot.status != "closed"
            else "Закрытый месяц; снимок этой версии неизменяем."
        ),
    )


def _get_management_balance_snapshot(
    session: Session,
    *,
    month: str | None,
    view: BalanceView | None,
    access_context: ExecutiveDashboardAuthContext,
) -> ExecutiveManagementBalanceSnapshot:
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
            return latest_closed

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
    return snapshot


def get_management_balance(
    session: Session,
    *,
    month: str | None,
    view: BalanceView | None,
    access_context: ExecutiveDashboardAuthContext,
) -> ExecutiveManagementBalanceResponse:
    return _response(
        session,
        _get_management_balance_snapshot(
            session,
            month=month,
            view=view,
            access_context=access_context,
        ),
    )


def _snapshot_lines(
    session: Session,
    snapshot: ExecutiveManagementBalanceSnapshot,
) -> list[ExecutiveManagementBalanceLine]:
    return list(
        session.scalars(
            select(ExecutiveManagementBalanceLine)
            .where(ExecutiveManagementBalanceLine.snapshot_id == snapshot.id)
            .order_by(
                ExecutiveManagementBalanceLine.section,
                ExecutiveManagementBalanceLine.display_order,
                ExecutiveManagementBalanceLine.id,
            )
        )
    )


def _opening_management_balance_snapshot(
    session: Session,
) -> ExecutiveManagementBalanceSnapshot:
    snapshot = session.scalar(
        select(ExecutiveManagementBalanceSnapshot)
        .where(
            ExecutiveManagementBalanceSnapshot.balance_date == OPENING_EQUITY_BASELINE_DATE,
            ExecutiveManagementBalanceSnapshot.status == "closed",
        )
        .order_by(ExecutiveManagementBalanceSnapshot.version.desc())
        .limit(1)
    )
    if snapshot is None:
        raise ManagementBalanceNotFoundError("Нет закрытого начального баланса на 01.01.2026")
    return snapshot


def _turnover_amounts(
    *,
    section: BalanceSection,
    opening_balance: Decimal | None,
    closing_balance: Decimal | None,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    if opening_balance is None or closing_balance is None:
        return None, None, None
    change = (closing_balance - opening_balance).quantize(MONEY)
    if section == "asset":
        debit = max(change, Decimal("0.00"))
        credit = max(-change, Decimal("0.00"))
        expected_closing = opening_balance + debit - credit
    else:
        debit = max(-change, Decimal("0.00"))
        credit = max(change, Decimal("0.00"))
        expected_closing = opening_balance + credit - debit
    difference = (closing_balance - expected_closing).quantize(MONEY)
    return debit.quantize(MONEY), credit.quantize(MONEY), difference


def _turnover_line_in_source_scope(line: ExecutiveManagementBalanceLine) -> bool:
    source_key = line.source_key.lower()
    if source_key == "onec_bp_tax_accounting":
        return line.line_key == "taxes_payable"
    return not (
        source_key.startswith("onec_bp_")
        or source_key.startswith("ka_bp_")
        or source_key.startswith("ut_bp_")
        or source_key == "ka_bp_accounting"
    )


def get_management_balance_turnover(
    session: Session,
    *,
    month: str | None,
    view: BalanceView | None,
    access_context: ExecutiveDashboardAuthContext,
) -> ExecutiveManagementBalanceTurnoverResponse:
    opening_snapshot = _opening_management_balance_snapshot(session)
    closing_snapshot = _get_management_balance_snapshot(
        session,
        month=month,
        view=view,
        access_context=access_context,
    )
    if closing_snapshot.balance_date < opening_snapshot.balance_date:
        raise ValueError("Конечная дата ОСВ не может быть раньше 01.01.2026")

    opening_lines = _snapshot_lines(session, opening_snapshot)
    closing_lines = _snapshot_lines(session, closing_snapshot)
    excluded_lines: list[dict[str, Any]] = []
    for snapshot_role, snapshot_lines in (
        ("opening", opening_lines),
        ("closing", closing_lines),
    ):
        for line in snapshot_lines:
            if _turnover_line_in_source_scope(line):
                continue
            excluded_lines.append(
                {
                    "snapshot": snapshot_role,
                    "section": line.section,
                    "key": line.line_key,
                    "label": line.label,
                    "source_key": line.source_key,
                    "reason": "В БП для ОСВ разрешена только строка начисленных налогов",
                }
            )
    opening_by_key = {
        (line.section, line.line_key): line
        for line in opening_lines
        if _turnover_line_in_source_scope(line)
    }
    closing_by_key = {
        (line.section, line.line_key): line
        for line in closing_lines
        if _turnover_line_in_source_scope(line)
    }
    line_keys = set(opening_by_key) | set(closing_by_key)
    section_order = {"asset": 0, "liability": 1, "equity": 2}

    def sort_key(key: tuple[str, str]) -> tuple[int, int, str]:
        line = closing_by_key.get(key) or opening_by_key[key]
        return section_order[line.section], line.display_order, line.line_key

    turnover_lines: list[ExecutiveManagementBalanceTurnoverLine] = []
    for line_key in sorted(line_keys, key=sort_key):
        opening_line = opening_by_key.get(line_key)
        closing_line = closing_by_key.get(line_key)
        anchor = closing_line or opening_line
        assert anchor is not None
        opening_balance = opening_line.amount if opening_line is not None else Decimal("0.00")
        closing_balance = closing_line.amount if closing_line is not None else Decimal("0.00")
        debit, credit, difference = _turnover_amounts(
            section=anchor.section,  # type: ignore[arg-type]
            opening_balance=opening_balance,
            closing_balance=closing_balance,
        )
        notes = [
            note
            for note in (
                opening_line.note if opening_line is not None else None,
                closing_line.note if closing_line is not None else None,
            )
            if note
        ]
        if opening_line is None:
            notes.append("На 01.01.2026 статья отсутствовала; начальное сальдо принято равным 0")
        if closing_line is None:
            notes.append("В конечном снимке статья отсутствует; конечное сальдо принято равным 0")
        missing_side = opening_line is None or closing_line is None
        line_source_statuses = {
            line.source_status for line in (opening_line, closing_line) if line is not None
        }
        line_source_status = anchor.source_status
        if missing_side or line_source_statuses != {"ready"}:
            line_source_status = "partial"
        turnover_lines.append(
            ExecutiveManagementBalanceTurnoverLine(
                key=anchor.line_key,
                label=anchor.label,
                section=anchor.section,  # type: ignore[arg-type]
                opening_balance=opening_balance,
                debit_turnover=debit,
                credit_turnover=credit,
                closing_balance=closing_balance,
                reconciliation_difference=difference,
                source_key=anchor.source_key,
                source_status=line_source_status,
                source_as_of=anchor.source_as_of,
                note="; ".join(dict.fromkeys(notes)) or None,
            )
        )

    totals: list[ExecutiveManagementBalanceTurnoverTotal] = []
    total_labels = {
        "asset": "Итого активы",
        "liability": "Итого обязательства",
        "equity": "Итого собственные средства",
    }
    for section in ("asset", "liability", "equity"):
        section_lines = [line for line in turnover_lines if line.section == section]
        totals.append(
            ExecutiveManagementBalanceTurnoverTotal(
                section=section,  # type: ignore[arg-type]
                label=total_labels[section],
                opening_balance=sum(
                    (line.opening_balance or Decimal("0.00")) for line in section_lines
                ).quantize(MONEY),
                debit_turnover=sum(
                    (line.debit_turnover or Decimal("0.00")) for line in section_lines
                ).quantize(MONEY),
                credit_turnover=sum(
                    (line.credit_turnover or Decimal("0.00")) for line in section_lines
                ).quantize(MONEY),
                closing_balance=sum(
                    (line.closing_balance or Decimal("0.00")) for line in section_lines
                ).quantize(MONEY),
                reconciliation_difference=sum(
                    (line.reconciliation_difference or Decimal("0.00")) for line in section_lines
                ).quantize(MONEY),
                unknown_line_count=sum(
                    1
                    for line in section_lines
                    if line.opening_balance is None or line.closing_balance is None
                ),
            )
        )

    unknown_line_count = sum(
        1 for line in turnover_lines if line.opening_balance is None or line.closing_balance is None
    )
    source_status = closing_snapshot.source_status
    if unknown_line_count or any(line.source_status == "partial" for line in turnover_lines):
        source_status = "partial"
    return ExecutiveManagementBalanceTurnoverResponse(
        month=closing_snapshot.period_month.strftime("%Y-%m"),
        date_from=opening_snapshot.balance_date,
        date_to=closing_snapshot.balance_date,
        view=closing_snapshot.view_mode,  # type: ignore[arg-type]
        opening_version=opening_snapshot.version,
        closing_version=closing_snapshot.version,
        opening_content_sha256=opening_snapshot.content_sha256,
        closing_content_sha256=closing_snapshot.content_sha256,
        source_status=source_status,
        lines=turnover_lines,
        totals=totals,
        excluded_lines=excluded_lines,
        opening_imbalance_amount=opening_snapshot.imbalance_amount,
        closing_imbalance_amount=closing_snapshot.imbalance_amount,
        unknown_line_count=unknown_line_count,
        note=(
            "Управленческий контур: УТ 10.3; из БП включены только начисленные налоги. "
            "Обороты рассчитаны как чистое изменение между сохранёнными снимками; "
            "валовые движения регистров УТ 10.3 ещё не подключены."
        ),
    )


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
