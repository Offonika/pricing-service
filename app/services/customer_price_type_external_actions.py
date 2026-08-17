"""Delivery worker for approved customer price-type review actions.

The worker is deliberately inert unless the global gate, the action-specific
gate and ``execution_allowed_at_decision`` are all true. Bitrix24 delivery is an
idempotent upsert. A 1C change always follows a persisted preflight -> apply ->
readback state machine and never retries an ambiguous apply automatically.
"""

from __future__ import annotations

import calendar
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.customer_price_type import (
    CustomerPriceTypeCase,
    CustomerPriceTypeCaseEvent,
    CustomerPriceTypeExternalAction,
    CustomerPriceTypeOneCContractAction,
    CustomerPriceTypeProfile,
    CustomerPriceTypeReview,
    CustomerPriceTypeSnapshot,
)
from app.models.telephony import TelephonyUserLineSnapshot
from app.services.customer_price_type_reviews import CLIENT_ACTION_LABELS, PRICE_DIRECTION_KEYS
from app.services.expertise_bitrix import BitrixRestClient
from app.services.exporters.ut103_customer_price_types import (
    CUSTOMER_PRICE_TYPE_UPDATES_SCHEMA_V2,
    CustomerPriceTypeExchangeResult,
    CustomerPriceTypeUpdateMessage,
    CustomerPriceTypeUpdateRow,
    one_c_guid_from_counterparty_ref,
    parse_customer_price_type_exchange_result,
    write_customer_price_type_updates_message,
)

ACTION_STAGE_KEYS = {
    "presignal": "PRECLOSE_SIGNAL",
    "retention": "RETENTION_WORK",
    "isolate": "ISOLATE_1M",
    "recovery": "RECOVERY_CONTROL",
    "quality": "QUALITY_CHECK",
    "credit": "CREDIT_ECONOMICS_CHECK",
    "economics": "CREDIT_ECONOMICS_CHECK",
}


class BitrixCaseGateway(Protocol):
    def upsert_case(
        self,
        *,
        idempotency_key: str,
        fields: dict[str, Any],
    ) -> str: ...

    def read_case(self, *, item_id: str) -> dict[str, Any]: ...


class AmbiguousExternalResult(RuntimeError):
    """An external result cannot be safely retried or interpreted."""


@dataclass(frozen=True, slots=True)
class WorkerSummary:
    scanned: int = 0
    applied: int = 0
    advanced: int = 0
    waiting: int = 0
    skipped: int = 0
    technical_review: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "applied": self.applied,
            "advanced": self.advanced,
            "waiting": self.waiting,
            "skipped": self.skipped,
            "technical_review": self.technical_review,
            "errors": self.errors,
        }


class RestBitrixCaseGateway:
    """Minimal idempotent adapter for the accepted thin smart process."""

    def __init__(self, settings: Settings) -> None:
        if not settings.customer_price_type_bitrix_webhook_url:
            raise RuntimeError("Не настроен адрес Bitrix24 для рабочих кейсов типов цен.")
        if not settings.customer_price_type_bitrix_entity_type_id:
            raise RuntimeError("Не настроен идентификатор смарт-процесса типов цен.")
        stable_field = settings.customer_price_type_bitrix_field_map.get("stable_key")
        if not stable_field:
            raise RuntimeError("Не настроено поле идемпотентного ключа смарт-процесса.")
        self.settings = settings
        self.client = BitrixRestClient(settings.customer_price_type_bitrix_webhook_url)
        self.entity_type_id = settings.customer_price_type_bitrix_entity_type_id
        self.stable_field = stable_field

    def upsert_case(self, *, idempotency_key: str, fields: dict[str, Any]) -> str:
        matches = self.client.list_items_by_ref(
            entity_type_id=self.entity_type_id,
            ref_field=self.stable_field,
            ref_value=idempotency_key,
        )
        if len(matches) > 1:
            raise AmbiguousExternalResult(
                "В Bitrix24 найдено несколько рабочих кейсов с одним техническим ключом."
            )
        if matches:
            item_id = str(matches[0].get("id") or "").strip()
            if not item_id:
                raise AmbiguousExternalResult("Bitrix24 вернул рабочий кейс без идентификатора.")
            self.client.update_smart_process_item(
                entity_type_id=self.entity_type_id,
                item_id=item_id,
                fields=fields,
            )
        else:
            item_id, _ = self.client.add_smart_process_item(
                entity_type_id=self.entity_type_id,
                fields=fields,
            )
        readback = self.client.get_smart_process_item(
            entity_type_id=self.entity_type_id,
            item_id=item_id,
        )
        if str(readback.get("id") or "") != item_id:
            raise AmbiguousExternalResult("Bitrix24 не подтвердил созданный рабочий кейс.")
        readback_matches = self.client.list_items_by_ref(
            entity_type_id=self.entity_type_id,
            ref_field=self.stable_field,
            ref_value=idempotency_key,
        )
        if [str(item.get("id") or "") for item in readback_matches] != [item_id]:
            raise AmbiguousExternalResult(
                "Bitrix24 не подтвердил уникальность технического ключа рабочего кейса."
            )
        if str(readback.get("stageId") or "") != str(fields.get("stageId") or ""):
            raise AmbiguousExternalResult("Bitrix24 не подтвердил стадию рабочего кейса.")
        if str(readback.get("assignedById") or "") != str(fields.get("assignedById") or ""):
            raise AmbiguousExternalResult("Bitrix24 не подтвердил исполнителя рабочего кейса.")
        return item_id

    def read_case(self, *, item_id: str) -> dict[str, Any]:
        return self.client.get_smart_process_item(
            entity_type_id=self.entity_type_id,
            item_id=item_id,
        )


def run_customer_price_type_external_actions_once(
    db: Session,
    *,
    exchange_root: str | Path | None = None,
    settings: Settings | None = None,
    bitrix_gateway: BitrixCaseGateway | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    settings = settings or get_settings()
    current_time = _naive_utc(now)
    if not settings.customer_price_type_external_actions_enabled:
        return WorkerSummary().as_dict()
    processed_ids: set[int] = set()
    counters = WorkerSummary().as_dict()
    gateway = bitrix_gateway
    for _ in range(settings.customer_price_type_external_action_batch_size):
        statement = (
            select(CustomerPriceTypeExternalAction)
            .where(
                CustomerPriceTypeExternalAction.execution_allowed_at_decision.is_(True),
                CustomerPriceTypeExternalAction.status.in_(
                    ("pending", "preflight", "ready_to_apply", "applying")
                ),
            )
            .order_by(
                CustomerPriceTypeExternalAction.created_at, CustomerPriceTypeExternalAction.id
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if processed_ids:
            statement = statement.where(CustomerPriceTypeExternalAction.id.not_in(processed_ids))
        action = db.scalar(statement)
        if action is None:
            break
        processed_ids.add(action.id)
        counters["scanned"] += 1
        try:
            if action.action_kind == "bitrix_case":
                if not settings.customer_price_type_bitrix_case_actions_enabled:
                    counters["skipped"] += 1
                    db.rollback()
                    continue
                gateway = gateway or RestBitrixCaseGateway(settings)
                _deliver_bitrix_case(
                    db,
                    action=action,
                    settings=settings,
                    gateway=gateway,
                    now=current_time,
                )
                counters["applied"] += 1
                continue
            if not settings.customer_price_type_onec_actions_enabled or exchange_root is None:
                counters["skipped"] += 1
                db.rollback()
                continue
            outcome = _advance_onec_action(
                db,
                action=action,
                exchange_root=Path(exchange_root),
                settings=settings,
                now=current_time,
            )
            counters[outcome] += 1
            if outcome in {"waiting", "skipped"}:
                db.rollback()
        except AmbiguousExternalResult as exc:
            db.rollback()
            fresh_action = db.get(CustomerPriceTypeExternalAction, action.id)
            if fresh_action is not None:
                _mark_technical_review(db, fresh_action, str(exc), now=current_time)
            counters["technical_review"] += 1
        except Exception as exc:  # noqa: BLE001 - durable worker keeps the row retryable
            db.rollback()
            fresh_action = db.get(CustomerPriceTypeExternalAction, action.id)
            if fresh_action is not None and fresh_action.action_kind == "bitrix_case":
                fresh_action.attempt_count += 1
                fresh_action.technical_message = _safe_message(
                    exc, "Не удалось доставить рабочий кейс в Bitrix24."
                )
                fresh_action.updated_at = current_time
                db.commit()
            counters["errors"] += 1
    return counters


def sync_customer_price_type_bitrix_completions_once(
    db: Session,
    *,
    settings: Settings | None = None,
    bitrix_gateway: BitrixCaseGateway | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Persist trusted completion evidence after an explicit Bitrix24 readback."""

    settings = settings or get_settings()
    summary = {"scanned": 0, "completed": 0, "waiting": 0, "errors": 0}
    if not (
        settings.customer_price_type_external_actions_enabled
        and settings.customer_price_type_bitrix_case_actions_enabled
        and settings.customer_price_type_bitrix_completed_stage_ids
    ):
        return summary
    gateway = bitrix_gateway or RestBitrixCaseGateway(settings)
    current_time = _naive_utc(now)
    completed_stages = set(settings.customer_price_type_bitrix_completed_stage_ids)
    actions = list(
        db.scalars(
            select(CustomerPriceTypeExternalAction)
            .where(
                CustomerPriceTypeExternalAction.action_kind == "bitrix_case",
                CustomerPriceTypeExternalAction.status == "applied",
                CustomerPriceTypeExternalAction.external_ref.is_not(None),
            )
            .order_by(
                CustomerPriceTypeExternalAction.updated_at, CustomerPriceTypeExternalAction.id
            )
            .limit(settings.customer_price_type_external_action_batch_size)
        )
    )
    for action in actions:
        summary["scanned"] += 1
        try:
            review, snapshot, profile, case = _action_context(db, action)
            if case is None:
                summary["waiting"] += 1
                continue
            current = dict(case.manager_action_completeness or {})
            if (
                current.get("status") == "completed"
                and current.get("external_action_id") == action.id
            ):
                summary["completed"] += 1
                continue
            item = gateway.read_case(item_id=str(action.external_ref))
            stage_id = str(item.get("stageId") or "").strip()
            if str(item.get("id") or "").strip() != str(action.external_ref) or not stage_id:
                raise AmbiguousExternalResult(
                    "Bitrix24 не подтвердил состояние рабочего кейса для нового расчёта."
                )
            if stage_id not in completed_stages:
                summary["waiting"] += 1
                continue
            client_action = str(review.final_value or "")
            if client_action not in {"retention", "isolate", "recovery"}:
                summary["waiting"] += 1
                continue
            case.manager_action_completeness = {
                "status": "completed",
                "source": "bitrix_readback",
                "action": client_action,
                "current_price_type": snapshot.current_price_type,
                "snapshot_month": snapshot.snapshot_month.isoformat(),
                "snapshot_hash": snapshot.snapshot_hash,
                "bitrix_item_id": str(action.external_ref),
                "bitrix_stage_id": stage_id,
                "completed_at": current_time.isoformat(),
                "external_action_id": action.id,
            }
            case.stage = "CLOSED_KEEP"
            case.version += 1
            _append_event(
                db,
                case,
                action,
                event_type="bitrix_client_action_completed",
                source="bitrix",
                before=ACTION_STAGE_KEYS[client_action],
                after="CLOSED_KEEP",
                comment=(
                    "Bitrix24 подтвердил завершение действия. Возможность изменения "
                    "типа определит только новый месячный расчёт."
                ),
                metadata={"bitrix_item_id": action.external_ref, "bitrix_stage_id": stage_id},
            )
            db.commit()
            summary["completed"] += 1
        except Exception:  # noqa: BLE001 - readback is retried without changing external state
            db.rollback()
            summary["errors"] += 1
    return summary


def _deliver_bitrix_case(
    db: Session,
    *,
    action: CustomerPriceTypeExternalAction,
    settings: Settings,
    gateway: BitrixCaseGateway,
    now: datetime,
) -> None:
    review, snapshot, profile, case = _action_context(db, action)
    _ensure_current(action, review, snapshot, profile)
    client_action = str(review.final_value or "")
    stage_code = ACTION_STAGE_KEYS.get(client_action)
    stage_id = settings.customer_price_type_bitrix_stage_map.get(client_action)
    if stage_code is None or not stage_id:
        raise AmbiguousExternalResult("Для рекомендованного действия не настроена стадия Bitrix24.")
    responsible_id = _responsible_bitrix_user(
        db,
        profile=profile,
        client_action=client_action,
        settings=settings,
    )
    if responsible_id is None:
        raise AmbiguousExternalResult(
            "Не определён исполнитель рабочего кейса и не настроена внутренняя команда."
        )
    due_at = _action_due_at(client_action, now)
    bitrix_case_key = (
        case.case_key
        if case is not None
        else f"customer-price-type:{profile.counterparty_ref}:{snapshot.snapshot_month.isoformat()}"
    )
    logical_fields: dict[str, Any] = {
        "stable_key": bitrix_case_key,
        "counterparty_ref": profile.counterparty_ref,
        "counterparty_code": profile.counterparty_code or "",
        "counterparty_name": profile.counterparty_name or "",
        "snapshot_month": snapshot.snapshot_month.isoformat(),
        "action": client_action,
        "review_id": review.id,
    }
    fields: dict[str, Any] = {
        "title": f"{CLIENT_ACTION_LABELS.get(client_action, 'Действие')} · "
        f"{profile.counterparty_code or profile.counterparty_name or profile.counterparty_ref}",
        "stageId": stage_id,
        "assignedById": responsible_id,
        "begindate": now.isoformat(),
        "closedate": due_at.isoformat(),
    }
    if settings.customer_price_type_bitrix_category_id is not None:
        fields["categoryId"] = settings.customer_price_type_bitrix_category_id
    for key, value in logical_fields.items():
        mapped = settings.customer_price_type_bitrix_field_map.get(key)
        if mapped:
            fields[mapped] = value
    if bitrix_case_key not in {str(value) for value in fields.values()}:
        raise AmbiguousExternalResult(
            "В настройке Bitrix24 отсутствует поле технического ключа рабочего кейса."
        )
    item_id = gateway.upsert_case(idempotency_key=bitrix_case_key, fields=fields)
    before = action.status
    action.status = "applied"
    action.external_ref = item_id
    action.attempt_count += 1
    action.started_at = action.started_at or now
    action.completed_at = now
    action.technical_message = None
    action.version += 1
    action.updated_at = now
    if case is not None:
        case.bitrix_entity_id = item_id
        case.bitrix_sync_version = (case.bitrix_sync_version or 0) + 1
        case.stage = stage_code
        case.due_at = due_at
        case.version += 1
        _append_event(
            db,
            case,
            action,
            event_type="bitrix_case_applied",
            source="bitrix",
            before=before,
            after="applied",
            comment="Рабочий кейс Bitrix24 создан или обновлён и проверен.",
            metadata={"bitrix_item_id": item_id, "client_action": client_action},
        )
    db.commit()


def _advance_onec_action(
    db: Session,
    *,
    action: CustomerPriceTypeExternalAction,
    exchange_root: Path,
    settings: Settings,
    now: datetime,
) -> str:
    review, snapshot, profile, case = _action_context(db, action)
    _ensure_current(action, review, snapshot, profile)
    direction = PRICE_DIRECTION_KEYS.get(
        (snapshot.current_price_type, str(review.final_value or ""))
    )
    if direction not in set(settings.customer_price_type_onec_enabled_directions):
        return "skipped"
    lines = list(
        db.scalars(
            select(CustomerPriceTypeOneCContractAction)
            .where(CustomerPriceTypeOneCContractAction.external_action_id == action.id)
            .order_by(CustomerPriceTypeOneCContractAction.id)
        )
    )
    if not lines:
        raise AmbiguousExternalResult("У изменения 1С отсутствуют точные строки договоров.")
    if action.status == "pending":
        message = _onec_message(action, review, snapshot, profile, lines, mode="dry_run")
        action.status = "preflight"
        action.attempt_count += 1
        action.started_at = action.started_at or now
        action.technical_message = "Предварительная проверка подготовлена; запись не выполнялась."
        action.version += 1
        action.updated_at = now
        if case is not None:
            case.approval_status = "approved"
            case.approver_ref = review.reviewed_by
            case.approver_name = review.reviewed_by
            case.approved_at = review.reviewed_at
            case.approved_snapshot_hash = review.snapshot_hash
            case.onec_export_status = "ready"
            case.onec_readback_status = "pending"
            case.stage = "READY_FOR_1C"
            case.due_at = _add_business_days(now, 1)
            case.version += 1
        db.commit()
        try:
            write_customer_price_type_updates_message(exchange_root, message)
        except FileExistsError:
            pass
        return "advanced"
    if action.status == "preflight":
        result = _find_result(exchange_root, _message_id(action, "dry-run"))
        if result is None:
            ready_path = _ready_path(exchange_root, _message_id(action, "dry-run"))
            if not ready_path.exists() and now - (action.updated_at or now) >= timedelta(
                seconds=settings.customer_price_type_onec_result_timeout_seconds
            ):
                message = _onec_message(action, review, snapshot, profile, lines, mode="dry_run")
                try:
                    write_customer_price_type_updates_message(exchange_root, message)
                except FileExistsError:
                    pass
            return "waiting"
        parsed = parse_customer_price_type_exchange_result(result)
        try:
            _verify_onec_result(
                parsed,
                action=action,
                lines=lines,
                expected_results={"ready", "already_actual"},
                require_all_ready=True,
                require_atomic=False,
            )
        finally:
            _archive_result(result, exchange_root)
        if all(
            item.result == "already_actual" and item.readback_price_type == line.target_price_type
            for item, line in _match_result_lines(parsed, action, lines)
        ):
            _mark_onec_applied(db, action, case, lines, parsed, now=now)
            return "applied"
        for line in lines:
            line.status = "ready"
            line.updated_at = now
        action.status = "ready_to_apply"
        action.technical_message = "Предварительная проверка пройдена для всех договоров."
        action.version += 1
        action.updated_at = now
        db.commit()
        return "advanced"
    if action.status == "ready_to_apply":
        # The row is re-read and its cancellation/current-snapshot guards are
        # checked immediately before entering the no-retry apply state.
        db.refresh(action)
        if action.status != "ready_to_apply":
            return "skipped"
        _ensure_current(action, review, snapshot, profile)
        message = _onec_message(action, review, snapshot, profile, lines, mode="apply")
        action.status = "applying"
        action.attempt_count += 1
        action.technical_message = (
            "Применение начато; автоматическая повторная запись этого решения запрещена."
        )
        action.version += 1
        action.updated_at = now
        for line in lines:
            line.status = "applying"
            line.updated_at = now
        db.commit()
        try:
            write_customer_price_type_updates_message(exchange_root, message)
        except FileExistsError:
            pass
        return "advanced"
    result = _find_result(exchange_root, _message_id(action, "apply"))
    if result is None:
        if now - (action.updated_at or action.started_at or now) >= timedelta(
            seconds=settings.customer_price_type_onec_result_timeout_seconds
        ):
            raise AmbiguousExternalResult(
                "Ответ применения 1С не получен вовремя; повторная запись заблокирована."
            )
        return "waiting"
    parsed = parse_customer_price_type_exchange_result(result)
    try:
        _verify_onec_result(
            parsed,
            action=action,
            lines=lines,
            expected_results={"applied", "already_actual"},
            require_all_ready=False,
            require_atomic=True,
        )
    finally:
        _archive_result(result, exchange_root)
    _mark_onec_applied(db, action, case, lines, parsed, now=now)
    return "applied"


def _verify_onec_result(
    result: CustomerPriceTypeExchangeResult,
    *,
    action: CustomerPriceTypeExternalAction,
    lines: list[CustomerPriceTypeOneCContractAction],
    expected_results: set[str],
    require_all_ready: bool,
    require_atomic: bool,
) -> None:
    if result.message_id not in {
        _message_id(action, "dry-run"),
        _message_id(action, "apply"),
    }:
        raise AmbiguousExternalResult("1С вернула результат для другого сообщения.")
    if result.schema != CUSTOMER_PRICE_TYPE_UPDATES_SCHEMA_V2:
        raise AmbiguousExternalResult("1С вернула результат неподдерживаемой версии.")
    if result.requires_technical_review is True or not result.ok:
        raise AmbiguousExternalResult(
            result.errors or "1С направила карточку на техническую сверку."
        )
    if require_all_ready and result.all_ready is not True:
        raise AmbiguousExternalResult("1С не подтвердила готовность всех договоров.")
    if require_atomic and result.applied_atomically is not True:
        raise AmbiguousExternalResult("1С не подтвердила атомарное применение карточки.")
    matched = _match_result_lines(result, action, lines)
    for item, line in matched:
        if item.result not in expected_results:
            raise AmbiguousExternalResult(
                item.message or "Один из договоров не прошёл проверку 1С."
            )
        expected_readback = (
            line.expected_price_type if item.result == "ready" else line.target_price_type
        )
        if item.readback_price_type != expected_readback:
            raise AmbiguousExternalResult(
                "Фактический тип цены договора не совпал с согласованным значением."
            )


def _match_result_lines(
    result: CustomerPriceTypeExchangeResult,
    action: CustomerPriceTypeExternalAction,
    lines: list[CustomerPriceTypeOneCContractAction],
) -> list[tuple[Any, CustomerPriceTypeOneCContractAction]]:
    if len(result.item_results) != len(lines):
        raise AmbiguousExternalResult("1С вернула неполный состав договоров карточки.")
    by_key = {item.idempotency_key: item for item in result.item_results}
    if len(by_key) != len(result.item_results):
        raise AmbiguousExternalResult("В результате 1С повторяется ключ строки договора.")
    matched: list[tuple[Any, CustomerPriceTypeOneCContractAction]] = []
    for line in lines:
        item = by_key.get(line.idempotency_key)
        if item is None:
            raise AmbiguousExternalResult("В результате 1С отсутствует точный договор карточки.")
        if (
            item.decision_id != str(action.review_id)
            or item.contract_ref.lower() != line.contract_ref
        ):
            raise AmbiguousExternalResult("1С вернула результат для другого решения или договора.")
        matched.append((item, line))
    return matched


def _mark_onec_applied(
    db: Session,
    action: CustomerPriceTypeExternalAction,
    case: CustomerPriceTypeCase | None,
    lines: list[CustomerPriceTypeOneCContractAction],
    result: CustomerPriceTypeExchangeResult,
    *,
    now: datetime,
) -> None:
    matched = _match_result_lines(result, action, lines)
    for item, line in matched:
        line.status = "applied"
        line.actual_price_type = item.readback_price_type
        line.result_message = item.message or "Тип цены подтверждён сверкой 1С."
        line.updated_at = now
    before = action.status
    action.status = "applied"
    action.external_ref = result.message_id
    action.completed_at = now
    action.technical_message = None
    action.version += 1
    action.updated_at = now
    if case is not None:
        case.stage = "CLOSED_CHANGED"
        case.human_final_decision = "changed_in_1c"
        case.onec_export_status = "exported"
        case.onec_readback_status = "confirmed"
        case.version += 1
        _append_event(
            db,
            case,
            action,
            event_type="onec_change_readback_confirmed",
            source="onec",
            before=before,
            after="applied",
            comment="Изменение типа цены выполнено и подтверждено сверкой 1С.",
            metadata={"message_id": result.message_id, "contracts": len(lines)},
        )
    db.commit()


def _mark_technical_review(
    db: Session,
    action: CustomerPriceTypeExternalAction,
    message: str,
    *,
    now: datetime,
) -> None:
    before = action.status
    action.status = "technical_review"
    action.technical_message = message[:2000]
    action.version += 1
    action.updated_at = now
    lines = list(
        db.scalars(
            select(CustomerPriceTypeOneCContractAction).where(
                CustomerPriceTypeOneCContractAction.external_action_id == action.id
            )
        )
    )
    for line in lines:
        line.status = "technical_review"
        line.result_message = message[:2000]
        line.updated_at = now
    case = db.get(CustomerPriceTypeCase, action.case_id) if action.case_id else None
    if case is not None:
        case.stage = "READY_FOR_1C"
        case.onec_export_status = "error"
        case.onec_readback_status = "error"
        case.version += 1
        _append_event(
            db,
            case,
            action,
            event_type="external_action_technical_review",
            source="system",
            before=before,
            after="technical_review",
            comment=message[:2000],
            metadata={"action_kind": action.action_kind},
        )
    db.commit()


def _action_context(db: Session, action: CustomerPriceTypeExternalAction) -> tuple[
    CustomerPriceTypeReview,
    CustomerPriceTypeSnapshot,
    CustomerPriceTypeProfile,
    CustomerPriceTypeCase | None,
]:
    review = db.get(CustomerPriceTypeReview, action.review_id)
    snapshot = db.get(CustomerPriceTypeSnapshot, action.snapshot_id)
    if review is None or snapshot is None:
        raise AmbiguousExternalResult("Решение или расчёт карточки больше не существует.")
    profile = db.get(CustomerPriceTypeProfile, snapshot.profile_id)
    if profile is None:
        raise AmbiguousExternalResult("Профиль клиента больше не существует.")
    case = db.get(CustomerPriceTypeCase, action.case_id) if action.case_id else None
    return review, snapshot, profile, case


def _ensure_current(
    action: CustomerPriceTypeExternalAction,
    review: CustomerPriceTypeReview,
    snapshot: CustomerPriceTypeSnapshot,
    profile: CustomerPriceTypeProfile,
) -> None:
    if not action.execution_allowed_at_decision or review.decision_mode != "live":
        raise AmbiguousExternalResult("Проверочное решение не может запускать внешнее действие.")
    if (
        action.snapshot_hash != snapshot.snapshot_hash
        or review.snapshot_hash != snapshot.snapshot_hash
    ):
        raise AmbiguousExternalResult("Расчёт изменился после решения; требуется новая проверка.")
    if profile.latest_snapshot_id != snapshot.id:
        raise AmbiguousExternalResult(
            "После решения появился новый расчёт; старое действие остановлено."
        )


def _onec_message(
    action: CustomerPriceTypeExternalAction,
    review: CustomerPriceTypeReview,
    snapshot: CustomerPriceTypeSnapshot,
    profile: CustomerPriceTypeProfile,
    lines: list[CustomerPriceTypeOneCContractAction],
    *,
    mode: str,
) -> CustomerPriceTypeUpdateMessage:
    approved_at = review.reviewed_at.replace(tzinfo=UTC).isoformat()
    return CustomerPriceTypeUpdateMessage(
        message_id=_message_id(action, "dry-run" if mode == "dry_run" else "apply"),
        schema=CUSTOMER_PRICE_TYPE_UPDATES_SCHEMA_V2,
        mode=mode,
        approved_by=review.reviewed_by if mode == "apply" else "",
        rows=tuple(
            CustomerPriceTypeUpdateRow(
                idempotency_key=line.idempotency_key,
                decision_id=str(review.id),
                counterparty_ref=profile.counterparty_ref,
                counterparty_guid=one_c_guid_from_counterparty_ref(profile.counterparty_ref),
                counterparty_name=profile.counterparty_name or profile.counterparty_ref,
                contract_ref=line.contract_ref,
                contract_guid=one_c_guid_from_counterparty_ref(line.contract_ref),
                expected_current_price_type=line.expected_price_type,
                target_price_type=line.target_price_type,
                snapshot_hash=snapshot.snapshot_hash,
                reason=review.comment or "Согласовано руководителем сети",
                approved_at=approved_at if mode == "apply" else "",
            )
            for line in lines
        ),
    )


def _responsible_bitrix_user(
    db: Session,
    *,
    profile: CustomerPriceTypeProfile,
    client_action: str,
    settings: Settings,
) -> int | None:
    if client_action == "quality":
        return settings.customer_price_type_bitrix_quality_user_id or (
            settings.customer_price_type_bitrix_internal_user_id
        )
    if client_action in {"credit", "economics"}:
        return settings.customer_price_type_bitrix_finance_user_id or (
            settings.customer_price_type_bitrix_internal_user_id
        )
    owner_ref = str(profile.owner_ref or "").strip().lower()
    if owner_ref:
        latest = db.scalar(
            select(func.max(TelephonyUserLineSnapshot.snapshot_date)).where(
                func.lower(TelephonyUserLineSnapshot.user_ref_hex) == owner_ref,
                TelephonyUserLineSnapshot.is_marked.is_(False),
            )
        )
        if latest is not None:
            ids = {
                str(value).strip()
                for value in db.scalars(
                    select(TelephonyUserLineSnapshot.bitrix_user_id).where(
                        TelephonyUserLineSnapshot.snapshot_date == latest,
                        func.lower(TelephonyUserLineSnapshot.user_ref_hex) == owner_ref,
                        TelephonyUserLineSnapshot.is_marked.is_(False),
                        TelephonyUserLineSnapshot.bitrix_user_id.is_not(None),
                    )
                )
                if value and str(value).strip()
            }
            if len(ids) == 1:
                return int(next(iter(ids)))
    return settings.customer_price_type_bitrix_internal_user_id


def _action_due_at(client_action: str, now: datetime) -> datetime:
    if client_action == "isolate":
        year = now.year + (1 if now.month == 12 else 0)
        month = 1 if now.month == 12 else now.month + 1
        day = calendar.monthrange(year, month)[1]
        return datetime(year, month, day, 23, 59, 59)
    days = 2 if client_action in {"presignal", "retention", "recovery"} else 3
    return _add_business_days(now, days)


def _add_business_days(now: datetime, days: int) -> datetime:
    result = now
    added = 0
    while added < days:
        result += timedelta(days=1)
        if result.weekday() < 5:
            added += 1
    return result


def _append_event(
    db: Session,
    case: CustomerPriceTypeCase,
    action: CustomerPriceTypeExternalAction,
    *,
    event_type: str,
    source: str,
    before: str,
    after: str,
    comment: str,
    metadata: dict[str, Any],
) -> None:
    key = f"external-action:{action.id}:{event_type}"
    existing = db.scalar(
        select(CustomerPriceTypeCaseEvent.id).where(
            CustomerPriceTypeCaseEvent.case_id == case.id,
            CustomerPriceTypeCaseEvent.idempotency_key == key,
        )
    )
    if existing is None:
        db.add(
            CustomerPriceTypeCaseEvent(
                case_id=case.id,
                event_type=event_type,
                actor="system:customer-price-type-worker",
                source=source,
                before_status=before,
                after_status=after,
                comment=comment,
                metadata_json=metadata,
                idempotency_key=key,
            )
        )


def _message_id(action: CustomerPriceTypeExternalAction, phase: str) -> str:
    return f"customer-price-type-review-{action.review_id}-{phase}"


def _find_result(exchange_root: Path, message_id: str) -> Path | None:
    path = exchange_root / "from_1c" / "new" / f"customer_price_types_{message_id}.result.xml"
    return path if path.exists() else None


def _ready_path(exchange_root: Path, message_id: str) -> Path:
    return exchange_root / "to_1c" / "new" / f"customer_price_types_{message_id}.ready.xml"


def _archive_result(path: Path, exchange_root: Path) -> Path:
    target_dir = exchange_root / "from_1c" / "archive"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    if target.exists():
        path.unlink()
        return target
    shutil.move(str(path), str(target))
    return target


def _naive_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is not None:
        current = current.astimezone(UTC).replace(tzinfo=None)
    return current


def _safe_message(exc: Exception, fallback: str) -> str:
    message = str(exc).strip()
    return (message or fallback)[:2000]
