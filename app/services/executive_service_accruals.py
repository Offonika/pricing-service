from __future__ import annotations

import calendar
import json
import os
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.executive_dashboard import (
    ExecutiveServiceAccrualAudit,
    ExecutiveServiceAccrualEntry,
    ExecutiveServiceAccrualRule,
    ExecutiveSourceFreshness,
)

MONEY = Decimal("0.01")
FIRST_PERIOD = date(2026, 7, 1)


class ServiceAccrualSourceError(ValueError):
    pass


def _money(value: Any) -> Decimal:
    return Decimal(str(value or "0")).quantize(MONEY)


def _month_end(month: date) -> date:
    return month.replace(day=calendar.monthrange(month.year, month.month)[1])


def _next_month(month: date) -> date:
    return date(month.year + (month.month == 12), 1 if month.month == 12 else month.month + 1, 1)


def _resolve_source_path() -> Path:
    configured = Path(get_settings().executive_service_accrual_source_path)
    if configured.is_absolute():
        return configured
    workspace = Path(os.getenv("MM_WORKSPACE_ROOT", "/opt/MM"))
    return (workspace / configured).resolve()


def load_service_accrual_source(path: Path | None = None) -> dict[str, Any]:
    source_path = path or _resolve_source_path()
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ServiceAccrualSourceError(f"Источник начислений не найден: {source_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ServiceAccrualSourceError(f"Источник начислений повреждён: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ServiceAccrualSourceError("Источник начислений имеет неизвестную схему")
    if payload.get("source_status") != "ready":
        raise ServiceAccrualSourceError("Источник начислений не готов")
    if not isinstance(payload.get("rules"), list) or not isinstance(payload.get("payments"), list):
        raise ServiceAccrualSourceError("Источник начислений не содержит rules/payments")
    return payload


def _sync_rules(session: Session, payload: dict[str, Any]) -> list[ExecutiveServiceAccrualRule]:
    source_hash = str(payload.get("rules_hash") or "")
    if len(source_hash) != 64:
        raise ServiceAccrualSourceError("Источник начислений не содержит rules_hash")
    result: list[ExecutiveServiceAccrualRule] = []
    active_contracts: dict[str, list[tuple[date, date | None]]] = defaultdict(list)
    for item in payload["rules"]:
        if not isinstance(item, dict):
            raise ServiceAccrualSourceError("Правило начисления должно быть объектом")
        rule_key = str(item.get("rule_key") or "").strip()
        version = int(item.get("version") or 0)
        effective_from = date.fromisoformat(str(item.get("effective_from")))
        effective_to = (
            date.fromisoformat(str(item["effective_to"])) if item.get("effective_to") else None
        )
        active = bool(item.get("active"))
        contract_ref = str(item.get("contract_ref") or "").lower()
        if active:
            for old_from, old_to in active_contracts[contract_ref]:
                if effective_from <= (old_to or date.max) and old_from <= (
                    effective_to or date.max
                ):
                    raise ServiceAccrualSourceError(
                        f"Пересекаются активные правила договора {contract_ref}"
                    )
            active_contracts[contract_ref].append((effective_from, effective_to))
        existing = session.scalar(
            select(ExecutiveServiceAccrualRule).where(
                ExecutiveServiceAccrualRule.rule_key == rule_key,
                ExecutiveServiceAccrualRule.version == version,
            )
        )
        values = {
            "counterparty_ref": str(item.get("counterparty_ref") or "").lower(),
            "counterparty_name": str(item.get("counterparty_name") or ""),
            "contract_ref": contract_ref,
            "contract_name": str(item.get("contract_name") or ""),
            "effective_from": effective_from,
            "effective_to": effective_to,
            "expense_line_key": str(item.get("expense_line_key") or ""),
            "expense_line_label": str(item.get("expense_line_label") or ""),
            "monthly_amount_rub": _money(item.get("monthly_amount_rub")),
            "recognition_day": int(item.get("recognition_day") or 1),
            "balance_scope_verified": bool(item.get("balance_scope_verified")),
            "active": active,
            "approved_by": str(item.get("approved_by") or ""),
            "approval_note": item.get("approval_note"),
            "source_hash": source_hash,
        }
        if not values["approved_by"] and active:
            raise ServiceAccrualSourceError(f"Активное правило {rule_key} не утверждено")
        if active and not values["balance_scope_verified"]:
            raise ServiceAccrualSourceError(
                f"Для активного правила {rule_key} не подтверждён договорный остаток"
            )
        if existing is None:
            existing = ExecutiveServiceAccrualRule(rule_key=rule_key, version=version, **values)
            session.add(existing)
            session.flush()
        else:
            immutable = (
                existing.counterparty_ref,
                existing.contract_ref,
                existing.effective_from,
                existing.effective_to,
                existing.expense_line_key,
                existing.monthly_amount_rub,
                existing.recognition_day,
                existing.balance_scope_verified,
            )
            incoming = (
                values["counterparty_ref"],
                values["contract_ref"],
                values["effective_from"],
                values["effective_to"],
                values["expense_line_key"],
                values["monthly_amount_rub"],
                values["recognition_day"],
                values["balance_scope_verified"],
            )
            if immutable != incoming:
                raise ServiceAccrualSourceError(
                    f"Правило {rule_key}:{version} изменено без новой версии"
                )
            for key in ("active", "approved_by", "approval_note", "source_hash"):
                setattr(existing, key, values[key])
        result.append(existing)
    return result


def _payments_by_contract_month(
    payload: dict[str, Any],
) -> dict[tuple[str, date], list[dict[str, Any]]]:
    grouped: dict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
    seen: set[str] = set()
    for item in payload.get("payments") or []:
        movement_id = str(item.get("movement_id") or "")
        if not movement_id or movement_id in seen:
            raise ServiceAccrualSourceError("Платежи содержат пустой или повторный movement_id")
        seen.add(movement_id)
        business_date = date.fromisoformat(str(item.get("business_date")))
        contract_ref = str(item.get("contract_ref") or "").lower()
        grouped[(contract_ref, business_date.replace(day=1))].append(item)
    return grouped


def _documents_by_contract_month(
    payload: dict[str, Any],
) -> dict[tuple[str, date], list[dict[str, Any]]]:
    grouped: dict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
    seen: set[str] = set()
    for item in payload.get("closing_documents") or []:
        document_ref = str(item.get("document_ref") or "")
        if not document_ref or document_ref in seen:
            raise ServiceAccrualSourceError(
                "Закрывающие документы содержат пустую или повторную ссылку"
            )
        seen.add(document_ref)
        service_month = date.fromisoformat(str(item.get("service_month"))).replace(day=1)
        grouped[(str(item.get("contract_ref") or "").lower(), service_month)].append(item)
    return grouped


def _sync_service_accruals(
    session: Session,
    *,
    as_of: date,
    actor: str = "system:daily",
    source_path: Path | None = None,
) -> dict[str, Any]:
    payload = load_service_accrual_source(source_path)
    source_as_of = date.fromisoformat(str(payload.get("as_of")))
    if source_as_of > as_of:
        raise ServiceAccrualSourceError("Источник начислений датирован будущим")
    rules = _sync_rules(session, payload)
    payments = _payments_by_contract_month(payload)
    documents = _documents_by_contract_month(payload)
    documents_ready = payload.get("closing_documents_status") == "ready"
    generated_at = datetime.now(UTC).replace(tzinfo=None)
    inserted = updated = unchanged = 0
    active_entries: list[ExecutiveServiceAccrualEntry] = []
    for rule in rules:
        if not rule.active:
            continue
        month = max(FIRST_PERIOD, rule.effective_from.replace(day=1))
        last_month = as_of.replace(day=1)
        if rule.effective_to:
            last_month = min(last_month, rule.effective_to.replace(day=1))
        while month <= last_month:
            recognition_day = min(
                rule.recognition_day, calendar.monthrange(month.year, month.month)[1]
            )
            recognition_date = month.replace(day=recognition_day)
            if recognition_date > as_of:
                break
            payment_rows = payments.get((rule.contract_ref, month), [])
            document_rows = documents.get((rule.contract_ref, month), [])
            payment_amount = sum(
                (_money(row.get("amount_rub")) for row in payment_rows), Decimal("0")
            )
            if document_rows:
                recognized = sum(
                    (_money(row.get("amount_rub")) for row in document_rows), Decimal("0")
                )
                status = "actual_document"
                recognition_method = "onec_closing_document"
                source_status = "ready"
                source_document_ref = str(document_rows[0].get("document_ref"))
            else:
                recognized = rule.monthly_amount_rub
                status = "estimated_without_document"
                recognition_method = "approved_fixed_monthly_rule"
                source_status = "ready" if documents_ready else "partial"
                source_document_ref = None
            existing = session.scalar(
                select(ExecutiveServiceAccrualEntry).where(
                    ExecutiveServiceAccrualEntry.rule_id == rule.id,
                    ExecutiveServiceAccrualEntry.period_month == month,
                )
            )
            values = {
                "recognition_date": recognition_date,
                "counterparty_ref": rule.counterparty_ref,
                "counterparty_name": rule.counterparty_name,
                "contract_ref": rule.contract_ref,
                "contract_name": rule.contract_name,
                "expense_line_key": rule.expense_line_key,
                "expense_line_label": rule.expense_line_label,
                "status": status,
                "recognition_method": recognition_method,
                "recognized_amount_rub": recognized,
                "payment_amount_rub": payment_amount,
                "cashflow_expense_replaced_rub": payment_amount,
                "source_document_ref": source_document_ref,
                "source_status": source_status,
                "source_as_of": source_as_of,
                "payload": {"payments": payment_rows, "closing_documents": document_rows},
                "generated_at": generated_at,
            }
            if existing is None:
                existing = ExecutiveServiceAccrualEntry(
                    rule_id=rule.id, period_month=month, **values
                )
                session.add(existing)
                session.flush()
                session.add(
                    ExecutiveServiceAccrualAudit(
                        entry_id=existing.id,
                        action="generated",
                        actor=actor,
                        payload={"status": status, "recognized_amount_rub": str(recognized)},
                    )
                )
                inserted += 1
            else:
                before = (
                    existing.status,
                    existing.recognized_amount_rub,
                    existing.payment_amount_rub,
                    existing.source_document_ref,
                    existing.source_status,
                )
                after = (status, recognized, payment_amount, source_document_ref, source_status)
                if before == after:
                    unchanged += 1
                else:
                    action = (
                        "replaced_by_actual"
                        if existing.status == "estimated_without_document"
                        and status == "actual_document"
                        else "recalculated"
                    )
                    for key, value in values.items():
                        setattr(existing, key, value)
                    session.add(
                        ExecutiveServiceAccrualAudit(
                            entry_id=existing.id,
                            action=action,
                            actor=actor,
                            payload={
                                "before": [str(value) for value in before],
                                "after": [str(value) for value in after],
                            },
                        )
                    )
                    updated += 1
            active_entries.append(existing)
            month = _next_month(month)
    active_rule_count = sum(1 for rule in rules if rule.active)
    # An empty rule set means the contour has not been configured yet, not that
    # all regular services have been checked. Likewise, estimates remain
    # degraded until the closing-document extractor is verified.
    source_status = "ready" if active_rule_count > 0 and documents_ready else "partial"
    freshness = session.scalar(
        select(ExecutiveSourceFreshness).where(
            ExecutiveSourceFreshness.source_key == "finance.service_accruals",
            ExecutiveSourceFreshness.business_date == as_of,
        )
    )
    if freshness is None:
        freshness = ExecutiveSourceFreshness(
            source_key="finance.service_accruals",
            business_date=as_of,
            source_status=source_status,
            source_as_of=datetime.combine(source_as_of, datetime.min.time()),
            max_lag_days=1,
            payload={},
        )
        session.add(freshness)
    freshness.source_status = source_status
    freshness.source_as_of = datetime.combine(source_as_of, datetime.min.time())
    freshness.payload = {
        "title": "Начисления регулярных услуг",
        "rule_count": len(rules),
        "active_rule_count": active_rule_count,
        "entry_count": len(active_entries),
        "closing_documents_status": payload.get("closing_documents_status"),
        "configuration_status": "ready" if active_rule_count > 0 else "rules_missing",
    }
    session.commit()
    return {
        "as_of": as_of.isoformat(),
        "source_as_of": source_as_of.isoformat(),
        "rule_count": len(rules),
        "entry_count": len(active_entries),
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "recognized_amount_rub": str(
            sum((entry.recognized_amount_rub for entry in active_entries), Decimal("0")).quantize(
                MONEY
            )
        ),
        "source_status": source_status,
    }


def sync_service_accruals(
    session: Session,
    *,
    as_of: date,
    actor: str = "system:daily",
    source_path: Path | None = None,
) -> dict[str, Any]:
    try:
        return _sync_service_accruals(
            session,
            as_of=as_of,
            actor=actor,
            source_path=source_path,
        )
    except BaseException:
        session.rollback()
        raise


def service_accrual_entries(
    session: Session,
    *,
    month: date,
    counterparty_ref: str | None = None,
    contract_ref: str | None = None,
    expense_line_key: str | None = None,
    status: str | None = None,
) -> list[ExecutiveServiceAccrualEntry]:
    query = select(ExecutiveServiceAccrualEntry).where(
        ExecutiveServiceAccrualEntry.period_month == month.replace(day=1)
    )
    if counterparty_ref:
        query = query.where(
            ExecutiveServiceAccrualEntry.counterparty_ref == counterparty_ref.lower()
        )
    if contract_ref:
        query = query.where(ExecutiveServiceAccrualEntry.contract_ref == contract_ref.lower())
    if expense_line_key:
        query = query.where(ExecutiveServiceAccrualEntry.expense_line_key == expense_line_key)
    if status:
        query = query.where(ExecutiveServiceAccrualEntry.status == status)
    return list(
        session.scalars(query.order_by(ExecutiveServiceAccrualEntry.recognized_amount_rub.desc()))
    )


def service_accrual_source_status(session: Session, *, as_of: date) -> str:
    row = session.scalar(
        select(ExecutiveSourceFreshness)
        .where(
            ExecutiveSourceFreshness.source_key == "finance.service_accruals",
            ExecutiveSourceFreshness.business_date <= as_of,
        )
        .order_by(ExecutiveSourceFreshness.business_date.desc())
        .limit(1)
    )
    return row.source_status if row is not None else "source_missing"


def service_accrual_profit_loss_summary(
    session: Session, *, date_from: date, date_to: date
) -> dict[str, Any]:
    entries = list(
        session.scalars(
            select(ExecutiveServiceAccrualEntry).where(
                ExecutiveServiceAccrualEntry.recognition_date >= date_from,
                ExecutiveServiceAccrualEntry.recognition_date <= date_to,
                ExecutiveServiceAccrualEntry.status.in_(
                    ("estimated_without_document", "actual_document")
                ),
            )
        )
    )
    buckets: dict[str, dict[str, Any]] = {}
    for entry in entries:
        bucket = buckets.setdefault(
            entry.expense_line_key,
            {
                "key": entry.expense_line_key,
                "label": entry.expense_line_label,
                "recognized_amount": Decimal("0"),
                "cashflow_replaced_amount": Decimal("0"),
                "estimated_count": 0,
                "entry_count": 0,
                "source_status": "ready",
            },
        )
        bucket["recognized_amount"] += entry.recognized_amount_rub
        bucket["cashflow_replaced_amount"] += entry.cashflow_expense_replaced_rub
        bucket["entry_count"] += 1
        if entry.status == "estimated_without_document":
            bucket["estimated_count"] += 1
        if entry.source_status != "ready":
            bucket["source_status"] = "partial"
    return {
        "entry_count": len(entries),
        "recognized_amount": sum(
            (entry.recognized_amount_rub for entry in entries), Decimal("0")
        ).quantize(MONEY),
        "cashflow_replaced_amount": sum(
            (entry.cashflow_expense_replaced_rub for entry in entries), Decimal("0")
        ).quantize(MONEY),
        "estimated_count": sum(
            1 for entry in entries if entry.status == "estimated_without_document"
        ),
        "source_status": (
            "ready"
            if entries and all(entry.source_status == "ready" for entry in entries)
            else ("partial" if entries else service_accrual_source_status(session, as_of=date_to))
        ),
        "by_line": list(buckets.values()),
    }


def service_accrual_balance_adjustments(session: Session, *, as_of: date) -> dict[str, Any]:
    entries = list(
        session.scalars(
            select(ExecutiveServiceAccrualEntry).where(
                ExecutiveServiceAccrualEntry.recognition_date <= as_of,
                ExecutiveServiceAccrualEntry.status == "estimated_without_document",
            )
        )
    )
    by_counterparty: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for entry in entries:
        by_counterparty[entry.counterparty_ref] += entry.recognized_amount_rub
    return {
        "amount": sum((entry.recognized_amount_rub for entry in entries), Decimal("0")).quantize(
            MONEY
        ),
        "estimated_count": len(entries),
        "source_status": (
            "ready"
            if entries and all(entry.source_status == "ready" for entry in entries)
            else ("partial" if entries else service_accrual_source_status(session, as_of=as_of))
        ),
        "by_counterparty": dict(by_counterparty),
    }
