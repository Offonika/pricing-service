from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.procurement_exception import ProcurementException
from app.models.procurement_order_formation import ProcurementOrderFormation as Order
from app.models.procurement_order_formation import ProcurementOrderFormationEvent
from app.services.procurement_supply_scenarios import (
    facts_hash,
    price_confirmed,
    supply_review_valid,
)

MOSCOW = ZoneInfo("Europe/Moscow")
TITLES = {
    "supply_confirmation_required": "Проверить сомнительную поставку и количество закупки",
    "stockout_risk": "Товар может закончиться до поступления",
    "receipt_reconciliation": "Сверить фактическое исполнение заказа",
    "missing_eta": "Указать ожидаемую дату поступления",
    "overdue_eta": "Уточнить прошедший срок поставки",
    "aged_order": "Проверить незавершённый заказ старше 90 дней",
    "product_mapping": "Проверить сопоставление товаров Bitrix",
    "unconfirmed_price": "Согласовать закупочные цены",
    "currency_missing": "Подтвердить валюту договора",
    "price_history": "Проверить доступность сопоставимой истории цен",
}


def utc_naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def reaction_deadline(now: datetime, *, calendar: dict | None = None) -> datetime:
    if calendar is None:
        calendar = json.loads(
            (
                Path(__file__).resolve().parents[2] / "config/procurement-work-calendar.json"
            ).read_text()
        )
    local = (
        now.replace(tzinfo=UTC).astimezone(MOSCOW) if now.tzinfo is None else now.astimezone(MOSCOW)
    )
    day = local.date() + timedelta(days=1)
    while True:
        year = calendar["years"].get(str(day.year))
        if year is None:
            raise ValueError(f"Производственный календарь на {day.year} год не настроен")
        working = day.isoformat() in year.get("working_weekends", []) or (
            day.weekday() < 5 and day.isoformat() not in year["holidays"]
        )
        if working:
            return datetime.combine(day, time(18), MOSCOW).astimezone(UTC).replace(tzinfo=None)
        day += timedelta(days=1)


def exception_facts(order: Order, *, now: datetime) -> Iterable[tuple[str, int | None, dict]]:
    if order.status in {"superseded", "cancelled"}:
        return
    active = [line for line in order.lines if not line.removed]
    open_order = order.origin == "onec_import" and order.lifecycle_status not in {
        "received",
        "cancelled",
    }
    if not open_order and (order.payload or {}).get("receipt_transition_pending"):
        yield "receipt_reconciliation", None, {
            "pending_transition": order.payload["receipt_transition_pending"],
            "evidence": order.payload.get("receipt_evidence"),
        }
    today = (
        now.replace(tzinfo=UTC).astimezone(MOSCOW).date()
        if now.tzinfo is None
        else now.astimezone(MOSCOW).date()
    )
    if open_order:
        if order.expected_receipt_date is None:
            yield "missing_eta", None, {
                "expected_at": None,
                "open_quantity": str(order.onec_open_quantity),
            }
        elif order.expected_receipt_date < today:
            yield "overdue_eta", None, {
                "expected_at": order.expected_receipt_date.isoformat(),
                "open_quantity": str(order.onec_open_quantity),
            }
        if (today - order.order_date).days > 90:
            yield "aged_order", None, {
                "order_date": order.order_date.isoformat(),
                "open_quantity": str(order.onec_open_quantity),
            }
        evidence = (order.payload or {}).get("receipt_evidence") or {}
        if (
            order.lifecycle_status == "reconciliation_required"
            or (order.payload or {}).get("receipt_transition_pending")
            or evidence.get("status") != "exact"
            or evidence.get("stale")
        ):
            yield "receipt_reconciliation", None, {
                "evidence": evidence,
                "open_quantity": str(order.onec_open_quantity),
            }
    if order.bitrix_product_rows_sync_state == "error":
        yield "product_mapping", None, {
            "expected_count": order.bitrix_product_rows_expected_count,
            "error": order.bitrix_product_rows_error,
        }
    if order.origin == "generated" and order.status in {"draft", "approved"}:
        unpriced = [line.id for line in active if not price_confirmed(line)]
        if unpriced:
            yield "unconfirmed_price", None, {"line_ids": sorted(unpriced)}
        if not order.currency:
            yield "currency_missing", None, {"contract_ref": order.contract_ref}
        missing_history = [
            line.id
            for line in active
            if price_confirmed(line)
            and (line.payload or {}).get("price_change_status")
            in {"currency_mismatch", "history_missing"}
        ]
        if missing_history:
            yield "price_history", None, {
                "line_ids": sorted(missing_history),
                "currency": order.currency,
            }
        for line in active:
            payload = line.payload or {}
            scenario = payload.get("supply_scenario") or {}
            if scenario.get("review_required"):
                yield "supply_confirmation_required", line.id, scenario
            if payload.get("stockout_guard_triggered"):
                yield "stockout_risk", line.id, {
                    "days_remaining": payload.get("stockout_guard_days_remaining"),
                    "required_days": payload.get("stockout_guard_required_days"),
                    "nomenclature_code": line.nomenclature_code,
                }


def _event(
    db: Session,
    item: ProcurementException,
    kind: str,
    actor: str,
    before: dict,
    *,
    after_extra: dict | None = None,
) -> None:
    db.add(
        ProcurementOrderFormationEvent(
            order_id=item.order_id,
            entity_type="procurement_exception",
            entity_id=str(item.id),
            event_type=kind,
            actor=actor,
            idempotency_key=f"proc-exception:{item.id}:v{item.version}:{kind}",
            before=before,
            after={
                "status": item.status,
                "facts_hash": item.facts_hash,
                "facts": item.facts,
                **(after_extra or {}),
            },
            payload={
                "reason": item.resolution,
                "next_action": item.next_action,
                "assigned_user_id": item.assigned_user_id,
                "next_action_due_at": (
                    item.next_action_due_at.isoformat() if item.next_action_due_at else None
                ),
            },
        )
    )


def sync_exceptions(
    db: Session, *, orders: Iterable[Order] | None = None, now: datetime | None = None
) -> dict[str, int]:
    now = utc_naive(now or datetime.now(UTC))
    if orders is None:
        orders = db.scalars(select(Order).options(selectinload(Order.lines))).all()
    orders_by_id = {order.id: order for order in orders}
    existing = {
        item.stable_key: item
        for item in db.scalars(select(ProcurementException).with_for_update()).all()
    }
    counts = Counter()
    lines_by_id = {line.id: line for order in orders_by_id.values() for line in order.lines}
    prior_supply = defaultdict(list)
    for item in existing.values():
        old_line = lines_by_id.get(item.line_id)
        if (
            item.reason_code == "supply_confirmation_required"
            and item.status != "resolved"
            and old_line is not None
        ):
            prior_supply[
                str(old_line.nomenclature_ref or old_line.nomenclature_code).lower()
            ].append(item)
    observed = set()
    for order in sorted(orders_by_id.values(), key=lambda order: order.id, reverse=True):
        for reason, line_id, facts in exception_facts(order, now=now):
            key = f"order:{order.id}:line:{line_id or 0}:{reason}"
            if line_id is not None and order.origin == "generated" and reason == "stockout_risk":
                line = next(line for line in order.lines if line.id == line_id)
                identity = str(
                    line.nomenclature_ref or line.nomenclature_code or line.stable_key
                ).lower()
                key = f"sku:{identity}:{reason}"
            if key in observed:
                continue
            observed.add(key)
            # The scenario already has an exact decision hash; omit its observation date.
            digest = facts.get("facts_hash") or facts_hash(facts)
            item = existing.get(key)
            line = lines_by_id.get(line_id)
            decision_invalidated = (
                reason == "supply_confirmation_required"
                and line is not None
                and not supply_review_valid(line)
            )
            if item is None:
                predecessors = (
                    prior_supply.get(
                        str(line.nomenclature_ref or line.nomenclature_code).lower(), []
                    )
                    if reason == "supply_confirmation_required" and line is not None
                    else []
                )
                first_seen = min((prior.first_seen_at for prior in predecessors), default=now)
                response_due = min((prior.response_due_at for prior in predecessors), default=None)
                item = ProcurementException(
                    stable_key=key,
                    order_id=order.id,
                    line_id=line_id,
                    reason_code=reason,
                    title=f"{TITLES[reason]} — {order.onec_document_number or order.supplier_name}",
                    status="new",
                    version=1,
                    facts=facts,
                    facts_hash=digest,
                    first_seen_at=first_seen,
                    last_seen_at=now,
                    response_due_at=response_due or reaction_deadline(now),
                )
                db.add(item)
                db.flush()
                existing[key] = item
                _event(db, item, "exception_detected", "system:procurement-exceptions", {})
                counts["created"] += 1
            elif (
                item.facts_hash != digest
                or item.order_id != order.id
                or item.line_id != line_id
                or (item.status == "resolved" and decision_invalidated)
            ):
                before = {"status": item.status, "facts_hash": item.facts_hash, "facts": item.facts}
                item.order_id = order.id
                item.line_id = line_id
                item.title = (
                    f"{TITLES[reason]} — {order.onec_document_number or order.supplier_name}"
                )
                item.facts = facts
                item.facts_hash = digest
                item.version += 1
                if item.status == "resolved":
                    item.status = "new"
                    item.resolved_at = None
                    item.acknowledged_at = None
                # Updated facts invalidate the version, not an unfinished assigned action.
                _event(db, item, "exception_facts_changed", "system:procurement-exceptions", before)
                counts["changed"] += 1
            item.last_seen_at = now
    # Resolve only after observing all orders, including a SKU moved to another draft.
    for item in existing.values():
        order = orders_by_id.get(item.order_id)
        if order is None or item.stable_key in observed or item.status == "resolved":
            continue
        stale = ((order.payload or {}).get("receipt_evidence") or {}).get("stale")
        if stale:
            continue
        if item.reason_code == "product_mapping" and order.bitrix_product_rows_sync_state not in {
            "synced",
            "ready",
        }:
            continue
        line = next((line for line in order.lines if line.id == item.line_id), None)
        if (
            item.reason_code in {"stockout_risk", "supply_confirmation_required"}
            and line is not None
            and not line.removed
            and line.blockers
        ):
            continue
        before = {"status": item.status}
        item.status = "resolved"
        item.resolution = "Причина устранена; подтверждено повторным чтением источника"
        item.resolved_at = now
        item.resolved_facts_hash = item.facts_hash
        item.version += 1
        _event(db, item, "exception_readback_resolved", "system:procurement-exceptions", before)
        counts["resolved_by_source"] += 1
    db.flush()
    return dict(counts)


def serialize_exception(item: ProcurementException, *, now: datetime | None = None) -> dict:
    now = utc_naive(now or datetime.now(UTC))
    overdue = item.status != "resolved" and (
        (item.acknowledged_at is None and item.response_due_at < now)
        or (item.next_action_due_at is not None and item.next_action_due_at < now)
    )
    return {
        key: (
            getattr(item, key).replace(tzinfo=UTC)
            if isinstance(getattr(item, key), datetime)
            else getattr(item, key)
        )
        for key in (
            "id",
            "order_id",
            "line_id",
            "reason_code",
            "title",
            "status",
            "version",
            "facts_hash",
            "facts",
            "first_seen_at",
            "last_seen_at",
            "response_due_at",
            "acknowledged_at",
            "assigned_user_id",
            "next_action",
            "next_action_due_at",
            "resolution",
            "resolved_at",
        )
    } | {"overdue": bool(overdue)}


def decide_exception(
    db: Session,
    exception_id: int,
    *,
    values: dict,
    user_id: str,
    actor: str,
    now: datetime | None = None,
) -> ProcurementException:
    from app.services.procurement_order_formation import (
        VersionConflictError,
        invalidate_order_approval,
    )

    now = utc_naive(now or datetime.now(UTC))
    item = db.scalar(
        select(ProcurementException)
        .where(ProcurementException.id == exception_id)
        .with_for_update()
    )
    if item is None:
        raise ValueError("Исключение не найдено")
    if item.version != values["expected_version"] or item.facts_hash != values["facts_hash"]:
        raise VersionConflictError("Факты изменились; обновите очередь")
    status = values["status"]
    if status not in {"in_progress", "waiting", "resolved"}:
        raise ValueError("Недопустимое состояние исключения")
    reason = str(values.get("reason") or "").strip()
    before = {"status": item.status, "facts_hash": item.facts_hash}
    after_extra = {}
    if status == "resolved":
        if not reason or not values.get("evidence"):
            raise ValueError("Нужны основание и результат проверки")
        if item.reason_code not in {
            "supply_confirmation_required",
            "stockout_risk",
            "aged_order",
            "price_history",
        }:
            raise ValueError(
                "Это исключение закрывается только повторным чтением исправленного источника"
            )
        order = db.scalar(select(Order).where(Order.id == item.order_id).with_for_update())
        current = {
            (code, line_id): facts for code, line_id, facts in exception_facts(order, now=now)
        }.get((item.reason_code, item.line_id))
        if current is None or (current.get("facts_hash") or facts_hash(current)) != item.facts_hash:
            raise VersionConflictError("Источник изменился; требуется повторная проверка")
        if item.reason_code == "supply_confirmation_required":
            from app.services.procurement_order_formation import ensure_order_editable

            ensure_order_editable(order)
            if values.get("expected_order_version") != order.version:
                raise VersionConflictError("Версия заказа изменилась")
            line = next(line for line in order.lines if line.id == item.line_id)
            if values.get("expected_line_version") != line.version:
                raise VersionConflictError("Версия строки изменилась")
            try:
                quantity = Decimal(str(values.get("final_quantity")))
            except InvalidOperation as exc:
                raise ValueError("Укажите итоговое количество") from exc
            if not quantity.is_finite() or quantity <= 0:
                raise ValueError(
                    "Укажите положительное итоговое количество либо исключите строку в заказе"
                )
            before["final_quantity"] = str(line.final_quantity)
            line.final_quantity = quantity
            after_extra["final_quantity"] = str(quantity)
            line.amount = (quantity * line.purchase_price).quantize(Decimal("0.01"))
            line.payload = {
                **(line.payload or {}),
                "manual_overrides": {
                    **((line.payload or {}).get("manual_overrides") or {}),
                    "final_quantity": True,
                },
                "supply_review": {
                    "facts_hash": item.facts_hash,
                    "final_quantity": str(quantity),
                    "reason": reason,
                    "evidence": values["evidence"],
                    "actor": actor,
                    "reviewed_at": now.isoformat(),
                },
            }
            line.version += 1
            invalidate_order_approval(order)
        item.resolution = reason + "\n" + str(values["evidence"])
        item.resolved_at = now
        item.resolved_facts_hash = item.facts_hash
    else:
        action = str(values.get("next_action") or "").strip()
        due = values.get("next_action_due_at")
        if not action or due is None or utc_naive(due) <= now:
            raise ValueError("Укажите следующее действие и его будущий срок")
        item.next_action = action
        item.next_action_due_at = utc_naive(due)
    item.assigned_user_id = user_id
    item.acknowledged_at = item.acknowledged_at or now
    item.status = status
    item.version += 1
    _event(db, item, "exception_decided", actor, before, after_extra=after_extra)
    db.flush()
    return item


def control_summary(db: Session, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    today = now.astimezone(MOSCOW).date()
    orders = db.scalars(
        select(Order)
        .options(selectinload(Order.lines))
        .where(Order.status.not_in(("superseded", "cancelled")))
    ).all()
    opened = [
        order
        for order in orders
        if order.origin == "onec_import" and order.lifecycle_status not in {"received", "cancelled"}
    ]
    uncertain = [
        order
        for order in opened
        if order.expected_receipt_date is None or order.expected_receipt_date < today
    ]
    exceptions = [
        serialize_exception(item, now=now)
        for item in db.scalars(
            select(ProcurementException).where(ProcurementException.status != "resolved")
        )
    ]
    generated = [
        line
        for order in orders
        if order.origin == "generated" and order.status in {"draft", "approved"}
        for line in order.lines
        if not line.removed
    ]
    confirmed = 0
    changed = 0
    changed_reasons = Counter()
    for line in generated:
        payload = line.payload or {}
        has_review = supply_review_valid(line)
        has_manual = bool((payload.get("manual_overrides") or {}).get("final_quantity"))
        if has_review or has_manual:
            if line.final_quantity != line.recommended_quantity:
                changed += 1
                decision = (
                    payload.get("supply_review") if has_review else payload.get("quantity_decision")
                )
                changed_reasons[str((decision or {}).get("reason") or "Основание не указано")] += 1
            else:
                confirmed += 1
    amounts: dict[str, Decimal] = {}
    unpriced = 0
    for order in orders:
        if order.origin != "generated" or order.status not in {"draft", "approved"}:
            continue
        for line in order.lines:
            if line.removed:
                continue
            if not price_confirmed(line) or not order.currency:
                unpriced += 1
            else:
                amounts[order.currency] = amounts.get(order.currency, Decimal(0)) + line.amount
    sync_times = [o.last_onec_sync_at for o in opened if o.last_onec_sync_at]
    return {
        "generated_at": now,
        "open_orders": len(opened),
        "without_eta": sum(o.expected_receipt_date is None for o in opened),
        "past_eta": sum(
            o.expected_receipt_date is not None and o.expected_receipt_date < today for o in opened
        ),
        "unconfirmed_incoming_quantity": sum(
            (o.onec_open_quantity for o in uncertain if o.onec_open_quantity is not None),
            Decimal(0),
        ),
        "unknown_incoming_order_count": sum(o.onec_open_quantity is None for o in uncertain),
        "synchronization_errors": sum(
            bool(
                o.sync_conflict
                or o.bitrix_link_error
                or o.bitrix_product_rows_sync_state == "error"
                or ((o.payload or {}).get("receipt_evidence") or {}).get("stale")
            )
            for o in opened
        ),
        "last_onec_sync_at": max(sync_times).replace(tzinfo=UTC) if sync_times else None,
        "oldest_onec_sync_at": min(sync_times).replace(tzinfo=UTC) if sync_times else None,
        "stale_receipt_sources": sum(
            bool(((o.payload or {}).get("receipt_evidence") or {}).get("stale")) for o in orders
        ),
        "unknown_freshness_count": sum(o.last_onec_sync_at is None for o in opened),
        "exceptions_open": len(exceptions),
        "exceptions_overdue": sum(e["overdue"] for e in exceptions),
        "stockout_risks": sum(e["reason_code"] == "stockout_risk" for e in exceptions),
        "recommendation_decisions": {
            "denominator": len(generated),
            "confirmed": confirmed,
            "changed": changed,
            "unreviewed": len(generated) - confirmed - changed,
        },
        "recommendation_change_reasons": dict(changed_reasons),
        "confirmed_amount_by_currency": amounts,
        "confirmed_amount_scope": "active_generated_drafts",
        "unpriced_lines": unpriced,
    }
