from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session, aliased

from app.core.config import Settings
from app.models.logistics import LogisticsWarehouse
from app.models.site_order_fulfillment import (
    BitrixChatAction,
    BitrixChatActionCandidate,
    BitrixChatMessage,
    PickupInventoryItem,
    PickupInventorySubmission,
    SiteOrderExecutionCase,
    SiteOrderExecutionEvent,
    SiteOrderFulfillmentOutbox,
)
from app.services import pickup_inventory
from app.services import site_order_fulfillment as fulfillment

CHAT_PICKUP_READY = "pickup_ready"
PARSER_VERSION = "pickup-bot-v1"

ACTION_ARRIVED = "arrived"
ACTION_MOVING = "moving"
ACTION_ISSUED = "issued"
ACTION_UNCLAIMED = "unclaimed"
ACTION_DISMANTLE = "dismantle"
ACTION_FOUND_EXPECTED = "found_expected"
ACTION_FOUND_OTHER = "found_other"
ACTION_RETURNED = "returned"
ACTION_NOT_FOUND = "not_found"
ACTION_REFRESH = "refresh"
ACTION_START_SEARCH = "start_search"
ACTION_OTHER_OUTCOME = "other_outcome"
ACTION_CONFIRM_ARRIVAL = "confirm_arrival"
ACTION_CANCEL = "cancel"
ACTIONS = {
    ACTION_ARRIVED,
    ACTION_MOVING,
    ACTION_ISSUED,
    ACTION_UNCLAIMED,
    ACTION_DISMANTLE,
    ACTION_FOUND_EXPECTED,
    ACTION_FOUND_OTHER,
    ACTION_RETURNED,
    ACTION_NOT_FOUND,
    ACTION_REFRESH,
    ACTION_START_SEARCH,
    ACTION_OTHER_OUTCOME,
    ACTION_CONFIRM_ARRIVAL,
    ACTION_CANCEL,
}
UI_ACTIONS = {
    ACTION_REFRESH,
    ACTION_START_SEARCH,
    ACTION_OTHER_OUTCOME,
}
DANGEROUS_ACTIONS = {
    ACTION_ISSUED,
    ACTION_DISMANTLE,
    ACTION_FOUND_OTHER,
    ACTION_RETURNED,
}

ACTION_LABELS = {
    ACTION_ARRIVED: "Прибыл в точку",
    ACTION_MOVING: "Отправлен на точку",
    ACTION_ISSUED: "Выдан клиенту",
    ACTION_UNCLAIMED: "Не забран",
    ACTION_DISMANTLE: "На расформирование",
    ACTION_FOUND_EXPECTED: "Найден на ожидаемой точке",
    ACTION_FOUND_OTHER: "На другой точке",
    ACTION_RETURNED: "Возвращён / разобран",
    ACTION_NOT_FOUND: "Не найден",
    ACTION_REFRESH: "Обновить",
    ACTION_START_SEARCH: "Начать поиск",
    ACTION_OTHER_OUTCOME: "Другой итог",
    ACTION_CONFIRM_ARRIVAL: "Подтвердить поступление",
    ACTION_CANCEL: "Ошибка / отмена",
}
DECISION_REASON_TEXT = {
    "unsupported_action": "действие не поддерживается",
    "delivery_mismatch": "сделка не относится к внутреннему самовывозу",
    "terminal_crm_stage": "сделка уже закрыта",
    "pickup_point_unresolved": "не удалось определить точку самовывоза",
    "pickup_point_deal_mismatch": "точка в сообщении не совпадает со сделкой",
    "pickup_point_mismatch": "точка не совпадает с ранее подтверждённой",
    "onec_unavailable": "не удалось проверить актуальные данные 1С",
    "onec_return_conflict": "в 1С уже найден проведённый возврат",
    "arrival_transition_not_allowed": "текущая стадия не допускает приёмку",
    "assembly_not_confirmed": "сборка заказа в 1С не подтверждена",
    "second_confirmation_required": "требуется второе подтверждение",
    "issued_transition_not_allowed": "заказ сейчас не ожидает самовывоза",
    "issued_payment_not_confirmed": "оплата или выдача в 1С не подтверждена",
    "unclaimed_transition_not_allowed": "заказ сейчас не ожидает самовывоза",
    "dismantle_transition_not_allowed": "заказ сейчас не ожидает самовывоза",
    "storage_start_missing": "не зафиксирована дата поступления на точку",
    "sla_start_missing": "не подтверждено уведомление клиента",
    "pickup_sla_disabled": "контур SLA и расформирования выключен",
    "dismantle_too_early": "срок хранения ещё не истёк",
    "dismantle_payment_conflict": "оплата или выдача блокирует расформирование",
    "lost_orders_disabled": "контур потерянных заказов выключен",
    "lost_order_stage_not_allowed": "сделка не находится в ожидании самовывоза",
    "lost_order_point_missing": "в заказе не определена ожидаемая точка",
    "lost_order_target_missing": "не выбрана другая точка",
    "lost_order_target_same": "выбрана та же точка",
    "lost_order_target_invalid": "выбранная точка недоступна",
    "return_not_confirmed": "возврат в 1С не подтверждён",
    "return_payment_conflict": "оплата или выдача блокирует закрытие в отказ",
}
ACTION_EVENT_TYPES = {
    ACTION_ARRIVED: fulfillment.EVENT_PICKUP_STORED,
    ACTION_MOVING: fulfillment.EVENT_PICKUP_MOVING,
    ACTION_ISSUED: fulfillment.EVENT_PICKUP_RECEIVED,
    ACTION_UNCLAIMED: fulfillment.EVENT_PICKUP_UNCLAIMED,
    ACTION_DISMANTLE: fulfillment.EVENT_PICKUP_DISMANTLING,
    ACTION_FOUND_EXPECTED: fulfillment.EVENT_PICKUP_STORED,
    ACTION_FOUND_OTHER: fulfillment.EVENT_PICKUP_REDIRECTED,
    ACTION_RETURNED: fulfillment.EVENT_PICKUP_DISMANTLED,
    ACTION_NOT_FOUND: fulfillment.EVENT_PICKUP_EXCEPTION,
}

CANDIDATE_OPEN = "open"
CANDIDATE_CONFIRMATION = "confirmation_pending"
CANDIDATE_QUEUED = "apply_queued"
CANDIDATE_APPLIED = "applied"
CANDIDATE_DRY_RUN = "dry_run_complete"
CANDIDATE_REVIEW = "manual_review"
CANDIDATE_DISMISSED = "dismissed"
CANDIDATE_EXPIRED = "expired"

OUTBOX_PENDING = "pending"
OUTBOX_PROCESSING = "processing"
OUTBOX_COMPLETED = "completed"
OUTBOX_RETRY = "retry"
OUTBOX_FAILED = "failed"

OP_PUBLISH_CARD = "publish_card"
OP_PROCESS_ACTION = "process_action"
OP_PUBLISH_CONFIRMATION = "publish_confirmation"
OP_UPDATE_CARD = "update_card"
OP_UPDATE_CRM_STAGE = "update_crm_stage"
OP_FINALIZE_ACTION = "finalize_action"
OP_START_SMS_WORKFLOW = "start_sms_workflow"
OP_VERIFY_SMS_WORKFLOW = "verify_sms_workflow"
OP_CREATE_TASK = "create_task"
OP_UPDATE_CRM_FIELDS = "update_crm_fields"
OP_FINALIZE_CASE_EVENT = "finalize_case_event"
OP_PUBLISH_INVENTORY_CLARIFICATION = "publish_inventory_clarification"
OP_PROCESS_INVENTORY_CLARIFICATION = "process_inventory_clarification"
OP_UPDATE_INVENTORY_CLARIFICATION = "update_inventory_clarification"
OP_REFRESH_INTERACTIVE_CARD = "refresh_interactive_card"
OP_FINALIZE_STRUCTURED_ARRIVAL = "finalize_structured_arrival"
OP_PUBLISH_MISSING_RECEIPT_PROMPT = "publish_missing_receipt_prompt"
OP_CREATE_MISSING_RECEIPT_TASK = "create_missing_receipt_task"

APPLY_GATED_OUTBOX_OPERATIONS = frozenset(
    {
        OP_UPDATE_CRM_STAGE,
        OP_FINALIZE_ACTION,
        OP_START_SMS_WORKFLOW,
        OP_VERIFY_SMS_WORKFLOW,
        OP_CREATE_TASK,
        OP_UPDATE_CRM_FIELDS,
        OP_FINALIZE_CASE_EVENT,
    }
)

NON_RETRYABLE_EXTERNAL_OPERATIONS = {
    OP_PUBLISH_CARD,
    OP_PUBLISH_INVENTORY_CLARIFICATION,
    OP_UPDATE_INVENTORY_CLARIFICATION,
    OP_REFRESH_INTERACTIVE_CARD,
    OP_FINALIZE_STRUCTURED_ARRIVAL,
    OP_PUBLISH_MISSING_RECEIPT_PROMPT,
    OP_CREATE_MISSING_RECEIPT_TASK,
    OP_START_SMS_WORKFLOW,
    OP_CREATE_TASK,
}
CANDIDATE_CREATE_ADVISORY_LOCK_KEY = 5_584_927_483_672
SLA_TASK_ADVISORY_LOCK_KEY = 5_584_927_483_673
APPLY_ENABLED_ENV_KEY = "ORDER_FULFILLMENT_BOT_APPLY_ENABLED"
MISSING_RECEIPT_ENABLED_ENV_KEY = "ORDER_FULFILLMENT_PICKUP_MISSING_RECEIPT_ENABLED"
TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
DEFAULT_RUNTIME_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

CRM_PICKUP_STORAGE_STARTED_FIELD = "UF_CRM_MM_PICKUP_STORAGE_STARTED_AT"
CRM_PICKUP_SLA_STARTED_FIELD = "UF_CRM_MM_PICKUP_SLA_STARTED_AT"
CRM_PICKUP_HOLD_UNTIL_FIELD = "UF_CRM_MM_PICKUP_HOLD_UNTIL"
CRM_PICKUP_DERIVED_STATUS_FIELD = "UF_CRM_MM_PICKUP_DERIVED_STATUS"
CRM_PICKUP_LAST_EVIDENCE_FIELD = "UF_CRM_MM_PICKUP_LAST_EVIDENCE"
CRM_PICKUP_DATETIME_FIELDS = {
    CRM_PICKUP_STORAGE_STARTED_FIELD,
    CRM_PICKUP_SLA_STARTED_FIELD,
}

ALLOWED_ARRIVAL_STAGES = {
    "PREPARATION",
    "EXECUTING",
    "FINAL_INVOICE",
    "IN_DELIVERY",
    fulfillment.CRM_STAGE_PICKUP_WAITING,
}
TERMINAL_STAGES = {"WON", "LOSE", "DISMANTLING", "APOLOGY"}

ORDER_LIMIT_PER_MESSAGE = 10
STRICT_ARRIVAL_ORDER_LIMIT = 100

STRICT_ARRIVAL_ALLOWED_WORDS = frozenset(
    {
        "в",
        "всем",
        "готов",
        "готова",
        "готовы",
        "выдаче",
        "день",
        "добрый",
        "заказ",
        "заказа",
        "заказы",
        "здравствуйте",
        "к",
        "магазин",
        "магазине",
        "на",
        "прибыл",
        "прибыла",
        "прибыли",
        "прибыло",
        "привезли",
        "получили",
        "приехал",
        "приехала",
        "приехали",
        "принят",
        "принята",
        "приняли",
        "приняты",
        "поступил",
        "поступила",
        "поступили",
        "поступило",
        "точке",
        "точку",
    }
)

INVENTORY_CALLBACK_KIND = "inventory"
INVENTORY_ACTION_SELECT_POINT = "inventory_select_point"
INVENTORY_ACTION_FULL = "inventory_full"
INVENTORY_ACTION_CARRY = "inventory_carry"
INVENTORY_ACTION_ZERO = "inventory_zero"
INVENTORY_ACTION_ERROR = "inventory_error"
INVENTORY_ACTIONS = {
    INVENTORY_ACTION_SELECT_POINT,
    INVENTORY_ACTION_FULL,
    INVENTORY_ACTION_CARRY,
    INVENTORY_ACTION_ZERO,
    INVENTORY_ACTION_ERROR,
}
EXTERNAL_DELIVERY_MARKERS = ("сдэк", "почт", "boxberry", "постамат", "пвз")
PICKUP_CUES = (
    "самовывоз",
    "магазин",
    "точк",
    "митин",
    "савелов",
    "савёлов",
    "горбуш",
    "люблин",
    "тепл",
    "пятигор",
)
ARRIVAL_MARKERS = (
    "прибыл",
    "прибыла",
    "прибыли",
    "приехал",
    "приехала",
    "привезли",
    "получили",
    "поступил",
    "поступила",
    "на точке",
    "готов к выдаче",
    "готова к выдаче",
)
MOVING_MARKERS = (
    "отправили",
    "отправлен",
    "отправлена",
    "отправлены",
    "передали",
    "передан",
    "передана",
    "переданы",
    "отправили на",
    "отправлен на",
    "отправлена на",
    "передали на точку",
    "передан на точку",
    "едет на точку",
)

STRICT_MOVEMENT_ALLOWED_WORDS = frozenset(
    {
        "в",
        "для",
        "добрый",
        "день",
        "заказ",
        "заказа",
        "заказы",
        "магазин",
        "на",
        "с",
        "склад",
        "склада",
        "со",
        "отправил",
        "отправила",
        "отправили",
        "отправлен",
        "отправлена",
        "отправлены",
        "передал",
        "передала",
        "передали",
        "передан",
        "передана",
        "переданы",
        "точка",
        "точке",
        "точку",
    }
)


class BotSecurityError(ValueError):
    """Callback cannot be trusted."""


class RetryableBeforeExternalEffect(RuntimeError):
    """A read-only preflight failed before a non-idempotent external call."""


class ApplyDisabledBeforeSideEffect(RetryableBeforeExternalEffect):
    """The runtime kill-switch disabled an already queued side effect."""


class SmsMarkerNotConfirmed(RuntimeError):
    """The asynchronous SMS workflow did not confirm its marker in time."""


class TaskRouteConfigurationError(RuntimeError):
    """SLA task routing is incomplete and must fail closed."""


class SourceMessageEditedBeforeApply(RuntimeError):
    """A queued external effect no longer has immutable source evidence."""


@dataclass(frozen=True, slots=True)
class PickupCandidateMention:
    site_order_number: str
    detected_action: str
    evidence_text: str


@dataclass(frozen=True, slots=True)
class OneCPickupValidation:
    available: bool
    assembled: bool = False
    payment_confirmed: bool = False
    debt_conflict: bool = False
    issued_confirmed: bool = False
    return_confirmed: bool = False
    evidence: str | None = None


@dataclass(frozen=True, slots=True)
class MissingReceiptSnapshot:
    movement: SiteOrderExecutionEvent
    case: SiteOrderExecutionCase
    deal: fulfillment.BitrixDealSnapshot
    warehouse: LogisticsWarehouse
    onec: OneCPickupValidation


@dataclass(frozen=True, slots=True)
class PickupActionDecision:
    allowed: bool
    target_stage: str | None
    event_type: str | None
    reason: str
    send_sms: bool = False
    create_task: str | None = None


@dataclass(frozen=True, slots=True)
class CallbackToken:
    candidate_id: int
    action: str
    step: int
    nonce: str
    expires_at: datetime
    target_warehouse_id: int | None = None


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def parse_pickup_candidate_text(
    text_value: str,
    *,
    dialog_id: str,
    order_limit: int = ORDER_LIMIT_PER_MESSAGE,
) -> list[PickupCandidateMention]:
    if not text_value or fulfillment._contains_non_authoritative_chat_marker(text_value):
        return []
    normalized = fulfillment._clean_text(text_value)
    if any(marker in normalized for marker in fulfillment.GENERATED_ORDER_REPORT_MARKERS):
        return []
    if "распознано:" in normalized and "точка:" in normalized:
        return []
    order_numbers = fulfillment.extract_order_numbers(text_value)[: max(1, order_limit)]
    if not order_numbers:
        return []
    action = _classify_pickup_action(normalized)
    if action is None and dialog_id == "chat8729":
        action = ACTION_ARRIVED
    if action is None and dialog_id == "chat739":
        action = ACTION_NOT_FOUND
    if action is None:
        return []
    if dialog_id == "chat733":
        if any(marker in normalized for marker in EXTERNAL_DELIVERY_MARKERS):
            return []
        if action == ACTION_ARRIVED and not any(marker in normalized for marker in PICKUP_CUES):
            return []
    evidence = fulfillment._redact_text(text_value)
    return [
        PickupCandidateMention(
            site_order_number=order_number,
            detected_action=action,
            evidence_text=evidence,
        )
        for order_number in order_numbers
    ]


def _classify_pickup_action(normalized: str) -> str | None:
    if any(marker in normalized for marker in ("не найден", "не нашли", "потерян", "потеряли")):
        return ACTION_NOT_FOUND
    if any(marker in normalized for marker in ("расформ", "разобрать", "на разбор")):
        return ACTION_DISMANTLE
    if any(
        marker in normalized for marker in ("не забрал", "не забрали", "не забран", "не забрана")
    ):
        return ACTION_UNCLAIMED
    if any(
        marker in normalized
        for marker in ("выдан клиенту", "выдали клиенту", "клиент забрал", "забрали заказ")
    ):
        return ACTION_ISSUED
    if any(marker in normalized for marker in ARRIVAL_MARKERS):
        return ACTION_ARRIVED
    if any(marker in normalized for marker in MOVING_MARKERS):
        return ACTION_MOVING
    return None


def _strict_pickup_arrival_message(
    text_value: str,
    *,
    mentions: list[PickupCandidateMention],
    resolution: Any,
) -> bool:
    """Accept only the compact, established chat8729 arrival format.

    Point resolution and the later CRM/1C preflight remain mandatory.  This
    grammar deliberately rejects questions, explanations and notification text
    even when they happen to mention a valid order and point.
    """

    if not mentions or resolution.warehouse is None:
        return False
    if any(mention.detected_action != ACTION_ARRIVED for mention in mentions):
        return False
    all_orders = fulfillment.extract_order_numbers(text_value)
    if not all_orders or len(all_orders) > STRICT_ARRIVAL_ORDER_LIMIT:
        return False
    normalized = pickup_inventory._normalize_alias_text(  # noqa: SLF001
        fulfillment._strict_chat_text(text_value)  # noqa: SLF001
    )
    normalized = re.sub(r"[^0-9a-zа-я]+", " ", normalized.replace("ё", "е"))
    for match in resolution.matches:
        alias = pickup_inventory._normalize_alias_text(
            str(match.get("alias") or "")
        )  # noqa: SLF001
        alias = re.sub(r"[^0-9a-zа-я]+", " ", alias.replace("ё", "е")).strip()
        if not alias:
            continue
        normalized = re.sub(
            rf"\b{re.escape(alias)}[а-я]{{0,5}}\b",
            " ",
            normalized,
        )
    normalized = re.sub(r"\b2\d{5}\b", " ", normalized)
    words = [word for word in normalized.split() if word]
    return all(word in STRICT_ARRIVAL_ALLOWED_WORDS for word in words)


def strict_pickup_movement_message(
    text_value: str,
    *,
    mentions: list[PickupCandidateMention],
    resolution: Any,
) -> bool:
    """Accept only an explicit, point-resolved warehouse dispatch message."""

    if not mentions or resolution.warehouse is None:
        return False
    if any(mention.detected_action != ACTION_MOVING for mention in mentions):
        return False
    all_orders = fulfillment.extract_order_numbers(text_value)
    if not all_orders or len(all_orders) > STRICT_ARRIVAL_ORDER_LIMIT:
        return False
    normalized = pickup_inventory._normalize_alias_text(  # noqa: SLF001
        fulfillment._strict_chat_text(text_value)  # noqa: SLF001
    )
    normalized = re.sub(r"[^0-9a-zа-я]+", " ", normalized.replace("ё", "е"))
    for match in resolution.matches:
        alias = pickup_inventory._normalize_alias_text(  # noqa: SLF001
            str(match.get("alias") or "")
        )
        alias = re.sub(r"[^0-9a-zа-я]+", " ", alias.replace("ё", "е")).strip()
        if alias:
            normalized = re.sub(
                rf"\b{re.escape(alias)}[а-я]{{0,5}}\b",
                " ",
                normalized,
            )
    normalized = re.sub(r"\b2\d{5}\b", " ", normalized)
    words = [word for word in normalized.split() if word]
    return all(word in STRICT_MOVEMENT_ALLOWED_WORDS for word in words)


def _source_is_after_cutover(
    source_event_at: datetime | None,
    *,
    settings: Settings,
) -> bool:
    cutover = settings.order_fulfillment_bot_cutover_at
    return bool(
        source_event_at is not None
        and cutover is not None
        and _aware_utc(source_event_at) >= _aware_utc(cutover)
    )


def create_candidates_from_message(
    session: Session,
    *,
    dialog_id: str,
    message_id: str,
    author_id: str | None,
    text_value: str,
    message_at: datetime | None,
    settings: Settings,
    payload: dict[str, Any] | None = None,
    apply_enabled_probe: Callable[[], bool] | None = None,
    now: datetime | None = None,
) -> list[BitrixChatActionCandidate]:
    now = _naive_utc(now or utcnow())
    source_event_at = _naive_utc(message_at) if message_at is not None else None
    if dialog_id not in set(settings.order_fulfillment_bot_source_chat_ids):
        return []
    # Inventory reports are handled as point-in-time snapshots with their own
    # clarification flow. Treating every order in such a report as a regular
    # pickup action would publish one generic card per listed order.
    if dialog_id == settings.order_fulfillment_pickup_inventory_chat_dialog_id:
        return []
    if (
        dialog_id == settings.order_fulfillment_pickup_exception_chat_dialog_id
        and not settings.order_fulfillment_lost_orders_enabled
    ):
        return []
    numeric_chat_id = _numeric_chat_id(dialog_id)
    numeric_message_id = _numeric_message_id(message_id)
    canonical_message_id = str(numeric_message_id)
    excluded_authors = {
        str(item).strip().casefold() for item in settings.order_fulfillment_bot_excluded_user_ids
    }
    if settings.order_fulfillment_bot_id is not None:
        excluded_authors.add(str(settings.order_fulfillment_bot_id))
        excluded_authors.add(f"bot{settings.order_fulfillment_bot_id}")
    if str(author_id or "").strip().casefold() in excluded_authors:
        return []
    mentions = parse_pickup_candidate_text(
        text_value,
        dialog_id=dialog_id,
        order_limit=STRICT_ARRIVAL_ORDER_LIMIT,
    )
    if not mentions:
        return []

    _acquire_advisory_xact_lock(session, CANDIDATE_CREATE_ADVISORY_LOCK_KEY)

    fulfillment._acquire_bitrix_message_lock(  # noqa: SLF001
        session,
        chat_id=numeric_chat_id,
        message_id=numeric_message_id,
    )
    raw_message = session.scalar(
        select(BitrixChatMessage).where(
            BitrixChatMessage.chat_id == numeric_chat_id,
            BitrixChatMessage.message_id == numeric_message_id,
        )
    )
    if raw_message is None:
        chat_code_by_dialog = {
            settings.order_fulfillment_site_chat_dialog_id: (fulfillment.CHAT_SITE_MASTER_MOBILE),
            settings.order_fulfillment_pickup_ready_chat_dialog_id: (fulfillment.CHAT_PICKUP_READY),
            settings.order_fulfillment_pickup_inventory_chat_dialog_id: (
                fulfillment.CHAT_PICKUP_INVENTORY
            ),
            settings.order_fulfillment_pickup_movement_chat_dialog_id: (
                fulfillment.CHAT_PICKUP_MOVEMENT
            ),
            settings.order_fulfillment_pickup_exception_chat_dialog_id: (
                fulfillment.CHAT_PICKUP_EXCEPTION
            ),
        }
        raw_message = BitrixChatMessage(
            chat_code=chat_code_by_dialog.get(
                dialog_id,
                fulfillment.CHAT_SITE_MASTER_MOBILE,
            ),
            dialog_id=dialog_id,
            chat_id=numeric_chat_id,
            message_id=numeric_message_id,
            message_at=source_event_at,
            author_id=author_id,
            raw_text_hash=fulfillment._text_hash(text_value),
            raw_text_redacted=fulfillment._redact_text(text_value),
            parser_version=PARSER_VERSION,
            parse_status="candidate",
            payload=payload or {},
        )
        session.add(raw_message)
        session.flush()
    elif raw_message.parse_status == "edited_manual_review":
        return []

    candidate_count = int(session.scalar(select(func.count(BitrixChatActionCandidate.id))) or 0)
    runtime_apply_enabled = _runtime_apply_enabled(settings, apply_enabled_probe)
    resolution = pickup_inventory.resolve_pickup_inventory_warehouse(
        session,
        text_value,
        pickup_aliases=settings.order_fulfillment_pickup_warehouse_aliases,
    )
    strict_auto_arrival = bool(
        settings.order_fulfillment_pickup_auto_arrival_enabled
        and dialog_id == settings.order_fulfillment_pickup_ready_chat_dialog_id
        and _source_is_after_cutover(source_event_at, settings=settings)
        and _strict_pickup_arrival_message(
            text_value,
            mentions=mentions,
            resolution=resolution,
        )
        and fulfillment._clean_string(author_id)
    )
    compact_arrival_clarification = bool(
        settings.order_fulfillment_pickup_auto_arrival_enabled
        and dialog_id == settings.order_fulfillment_pickup_ready_chat_dialog_id
        and not strict_auto_arrival
        and resolution.warehouse is None
    )
    if (
        settings.order_fulfillment_pickup_auto_arrival_enabled
        and dialog_id == settings.order_fulfillment_pickup_ready_chat_dialog_id
        and not strict_auto_arrival
        and not compact_arrival_clarification
    ):
        session.commit()
        return []
    if not strict_auto_arrival:
        mentions = mentions[:ORDER_LIMIT_PER_MESSAGE]
    created: list[BitrixChatActionCandidate] = []
    for mention in mentions:
        existing = session.scalar(
            select(BitrixChatActionCandidate).where(
                BitrixChatActionCandidate.source_chat_id == dialog_id,
                BitrixChatActionCandidate.source_message_id == canonical_message_id,
                BitrixChatActionCandidate.site_order_number == mention.site_order_number,
            )
        )
        if existing is not None:
            created.append(existing)
            continue
        force_dry_run = candidate_count < settings.order_fulfillment_bot_dry_run_card_limit
        candidate = BitrixChatActionCandidate(
            raw_message_id=raw_message.id,
            source_chat_id=dialog_id,
            source_message_id=canonical_message_id,
            source_author_id=author_id,
            source_event_at=source_event_at,
            site_order_number=mention.site_order_number,
            detected_action=mention.detected_action,
            pickup_point_warehouse_id=(resolution.warehouse.id if resolution.warehouse else None),
            pickup_point_name=(resolution.warehouse.name if resolution.warehouse else None),
            status=(CANDIDATE_QUEUED if strict_auto_arrival else CANDIDATE_OPEN),
            expires_at=now + timedelta(hours=settings.order_fulfillment_bot_card_ttl_hours),
            nonce=secrets.token_hex(16),
            dry_run=bool(force_dry_run or not runtime_apply_enabled),
            payload={
                "evidence": mention.evidence_text,
                "pickup_resolution_reason": resolution.reason,
                "pickup_matches": resolution.matches,
                "automatic_arrival": strict_auto_arrival,
                "interaction": ("structured_arrival" if compact_arrival_clarification else None),
            },
        )
        session.add(candidate)
        session.flush()
        candidate_count += 1
        if strict_auto_arrival:
            actor_id = fulfillment._clean_string(author_id)
            action = BitrixChatAction(
                candidate_id=candidate.id,
                action=ACTION_ARRIVED,
                actor_id=actor_id,
                status="queued",
                confirmation_step=1,
                idempotency_key=f"candidate:{candidate.id}:auto-arrival",
                payload={"automatic": True, "source": "chat8729"},
            )
            session.add(action)
            session.flush()
            candidate.active_action = ACTION_ARRIVED
            candidate.active_actor_id = actor_id
            candidate.action_claimed_at = now
            enqueue_outbox(
                session,
                candidate=candidate,
                action=action,
                operation=OP_PROCESS_ACTION,
                idempotency_key=f"action:{action.id}:process",
                payload={"automatic": True},
                now=now,
            )
        elif not compact_arrival_clarification:
            enqueue_outbox(
                session,
                candidate=candidate,
                operation=OP_PUBLISH_CARD,
                idempotency_key=f"candidate:{candidate.id}:publish",
                payload={},
                now=now,
            )
        created.append(candidate)
    if compact_arrival_clarification and created:
        leader = created[0]
        candidate_ids = [candidate.id for candidate in created]
        order_numbers = [candidate.site_order_number for candidate in created]
        for candidate in created:
            candidate.payload = {
                **(candidate.payload or {}),
                "batch_leader_id": leader.id,
                "batch_candidate_ids": candidate_ids,
                "order_numbers": order_numbers,
                "view": "primary",
            }
        enqueue_outbox(
            session,
            candidate=leader,
            operation=OP_PUBLISH_CARD,
            idempotency_key=f"candidate:{leader.id}:publish",
            payload={"interaction": "structured_arrival"},
            now=now,
        )
    session.commit()
    return created


def create_interactive_candidates(
    session: Session,
    *,
    dialog_id: str,
    source_message_id: str,
    actor_id: str,
    order_numbers: list[str],
    interaction: str,
    settings: Settings,
    apply_enabled_probe: Callable[[], bool] | None = None,
    now: datetime | None = None,
) -> list[BitrixChatActionCandidate]:
    """Persist a public command without treating it as chat evidence."""

    if interaction not in {"search", "structured_arrival"}:
        raise ValueError("unsupported_interaction")
    if dialog_id not in set(settings.order_fulfillment_bot_source_chat_ids):
        raise ValueError("source_chat_not_allowed")
    clean_actor = fulfillment._clean_string(actor_id)
    if not clean_actor or not clean_actor.isdigit():
        raise ValueError("actor_invalid")
    unique_orders = list(dict.fromkeys(order_numbers))
    if not unique_orders or len(unique_orders) > 20:
        raise ValueError("order_count_invalid")
    if any(not re.fullmatch(r"\d{6}", value) for value in unique_orders):
        raise ValueError("order_number_invalid")
    if interaction == "search" and len(unique_orders) != 1:
        raise ValueError("pickup_search_requires_one_order")

    now = _naive_utc(now or utcnow())
    source_key = f"command:{interaction}:{source_message_id}"
    _acquire_advisory_xact_lock(session, CANDIDATE_CREATE_ADVISORY_LOCK_KEY)
    created: list[BitrixChatActionCandidate] = []
    runtime_apply_enabled = _runtime_apply_enabled(settings, apply_enabled_probe)
    for order_number in unique_orders:
        existing = session.scalar(
            select(BitrixChatActionCandidate).where(
                BitrixChatActionCandidate.source_chat_id == dialog_id,
                BitrixChatActionCandidate.source_message_id == source_key,
                BitrixChatActionCandidate.site_order_number == order_number,
            )
        )
        if existing is not None:
            created.append(existing)
            continue
        candidate = BitrixChatActionCandidate(
            source_chat_id=dialog_id,
            source_message_id=source_key,
            source_author_id=clean_actor,
            source_event_at=now,
            site_order_number=order_number,
            detected_action=(ACTION_ARRIVED if interaction == "structured_arrival" else "search"),
            status=CANDIDATE_OPEN,
            expires_at=now + timedelta(hours=settings.order_fulfillment_bot_card_ttl_hours),
            nonce=secrets.token_hex(16),
            dry_run=(interaction == "structured_arrival" and not runtime_apply_enabled),
            payload={"interaction": interaction},
        )
        session.add(candidate)
        session.flush()
        created.append(candidate)
    leader = created[0]
    candidate_ids = [candidate.id for candidate in created]
    for candidate in created:
        candidate.payload = {
            **(candidate.payload or {}),
            "batch_leader_id": leader.id,
            "batch_candidate_ids": candidate_ids,
            "order_numbers": unique_orders,
            "view": "primary",
        }
    enqueue_outbox(
        session,
        candidate=leader,
        operation=OP_PUBLISH_CARD,
        idempotency_key=f"candidate:{leader.id}:publish",
        payload={"interaction": interaction},
        now=now,
    )
    session.commit()
    return created


def sign_callback_token(
    candidate: BitrixChatActionCandidate,
    *,
    action: str,
    step: int,
    secret: str,
    target_warehouse_id: int | None = None,
) -> str:
    if action not in ACTIONS or step not in {1, 2}:
        raise ValueError("unsupported callback action")
    payload = {
        "c": candidate.id,
        "a": action,
        "s": step,
        "n": candidate.nonce,
        "e": int(_aware_utc(candidate.expires_at).timestamp()),
    }
    interaction = fulfillment._clean_string((candidate.payload or {}).get("interaction"))
    if interaction:
        payload["i"] = interaction
    if target_warehouse_id is not None:
        payload["w"] = int(target_warehouse_id)
    encoded = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64encode(signature)}"


def verify_callback_token(
    token: str,
    *,
    secret: str,
    now: datetime | None = None,
    reject_expired: bool = True,
) -> CallbackToken:
    try:
        encoded, raw_signature = token.split(".", 1)
        expected = hmac.new(
            secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _b64decode(raw_signature)):
            raise BotSecurityError("invalid_callback_signature")
        payload = json.loads(_b64decode(encoded).decode("utf-8"))
        action = str(payload["a"])
        step = int(payload["s"])
        expires_at = datetime.fromtimestamp(int(payload["e"]), tz=UTC).replace(tzinfo=None)
        if action not in ACTIONS or step not in {1, 2}:
            raise BotSecurityError("invalid_callback_action")
        if reject_expired and _naive_utc(now or utcnow()) >= expires_at:
            raise BotSecurityError("callback_expired")
        return CallbackToken(
            candidate_id=int(payload["c"]),
            action=action,
            step=step,
            nonce=str(payload["n"]),
            expires_at=expires_at,
            target_warehouse_id=(int(payload["w"]) if payload.get("w") is not None else None),
        )
    except BotSecurityError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BotSecurityError("invalid_callback_token") from exc


def callback_token_kind(token: str) -> str:
    """Return an untrusted routing hint; the selected verifier still checks HMAC."""

    try:
        encoded, _ = token.split(".", 1)
        payload = json.loads(_b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return fulfillment._clean_string(payload.get("k")) if isinstance(payload, dict) else ""


def callback_token_action(token: str) -> str:
    """Return an untrusted action hint; the selected handler verifies the HMAC."""

    try:
        encoded, _ = token.split(".", 1)
        payload = json.loads(_b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return fulfillment._clean_string(payload.get("a")) if isinstance(payload, dict) else ""


def callback_token_interaction(token: str) -> str:
    """Return an untrusted interaction hint; the selected handler verifies the HMAC."""

    try:
        encoded, _ = token.split(".", 1)
        payload = json.loads(_b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return fulfillment._clean_string(payload.get("i")) if isinstance(payload, dict) else ""


def queue_interactive_callback(
    session: Session,
    *,
    token: str,
    actor_id: str,
    dialog_id: str,
    settings: Settings,
    now: datetime | None = None,
) -> tuple[SiteOrderFulfillmentOutbox, bool]:
    now = _naive_utc(now or utcnow())
    candidate, decoded, clean_actor = _verified_interactive_candidate(
        session,
        token=token,
        actor_id=actor_id,
        dialog_id=dialog_id,
        settings=settings,
        now=now,
    )
    interaction = fulfillment._clean_string((candidate.payload or {}).get("interaction"))
    if interaction not in {"search", "structured_arrival"}:
        raise BotSecurityError("interactive_candidate_required")
    if decoded.action not in UI_ACTIONS | {ACTION_CANCEL}:
        raise BotSecurityError("invalid_interactive_action")
    if interaction != "search" and decoded.action in UI_ACTIONS:
        raise BotSecurityError("invalid_interactive_action")
    if decoded.target_warehouse_id is not None:
        raise BotSecurityError("unexpected_target_warehouse")
    view = fulfillment._clean_string((candidate.payload or {}).get("view")) or "primary"
    next_view = {
        ACTION_START_SEARCH: "search",
        ACTION_OTHER_OUTCOME: "other",
    }.get(decoded.action, view)
    key = f"candidate:{candidate.id}:ui:{decoded.action}:nonce:{candidate.nonce}"
    existing = session.scalar(
        select(SiteOrderFulfillmentOutbox).where(SiteOrderFulfillmentOutbox.idempotency_key == key)
    )
    if existing is not None:
        return existing, True
    candidate.payload = {**(candidate.payload or {}), "view": next_view}
    if decoded.action == ACTION_CANCEL:
        candidate.status = CANDIDATE_DISMISSED
    candidate.nonce = secrets.token_hex(16)
    row = enqueue_outbox(
        session,
        candidate=candidate,
        operation=OP_REFRESH_INTERACTIVE_CARD,
        idempotency_key=key,
        payload={
            "actor_id": clean_actor,
            "status_text": "Отменено" if decoded.action == ACTION_CANCEL else None,
        },
        now=now,
    )
    session.commit()
    return row, False


def queue_structured_arrival(
    session: Session,
    *,
    token: str,
    actor_id: str,
    dialog_id: str,
    settings: Settings,
    now: datetime | None = None,
) -> tuple[BitrixChatAction, bool]:
    now = _naive_utc(now or utcnow())
    leader, decoded, clean_actor = _verified_interactive_candidate(
        session,
        token=token,
        actor_id=actor_id,
        dialog_id=dialog_id,
        settings=settings,
        now=now,
    )
    if (
        fulfillment._clean_string((leader.payload or {}).get("interaction")) != "structured_arrival"
        or decoded.action != ACTION_CONFIRM_ARRIVAL
    ):
        raise BotSecurityError("structured_arrival_required")
    warehouse = session.get(LogisticsWarehouse, decoded.target_warehouse_id)
    allowed_ids = {item.id for item in _selectable_pickup_warehouses(session, settings=settings)}
    if warehouse is None or warehouse.id not in allowed_ids:
        raise BotSecurityError("invalid_target_warehouse")
    candidate_ids = [
        int(value) for value in (leader.payload or {}).get("batch_candidate_ids") or []
    ]
    candidates = session.scalars(
        select(BitrixChatActionCandidate)
        .where(BitrixChatActionCandidate.id.in_(candidate_ids))
        .order_by(BitrixChatActionCandidate.id.asc())
        .with_for_update()
    ).all()
    if not candidates or {item.source_message_id for item in candidates} != {
        leader.source_message_id
    }:
        raise BotSecurityError("structured_arrival_batch_invalid")
    key = (
        f"candidate:{leader.id}:structured-arrival:warehouse:{warehouse.id}:"
        f"nonce:{leader.nonce}"
    )
    existing = session.scalar(
        select(BitrixChatAction).where(BitrixChatAction.idempotency_key == key)
    )
    if existing is not None:
        return existing, True
    first_action: BitrixChatAction | None = None
    action_ids: list[int] = []
    for candidate in candidates:
        if candidate.status != CANDIDATE_OPEN:
            raise BotSecurityError("candidate_already_claimed")
        candidate.pickup_point_warehouse_id = warehouse.id
        candidate.pickup_point_name = warehouse.name
        candidate.active_action = ACTION_ARRIVED
        candidate.active_actor_id = clean_actor
        candidate.action_claimed_at = now
        candidate.status = CANDIDATE_QUEUED
        candidate.payload = {
            **(candidate.payload or {}),
            "structured_batch_leader_id": leader.id,
        }
        action_key = key if candidate.id == leader.id else f"{key}:candidate:{candidate.id}"
        action = BitrixChatAction(
            candidate_id=candidate.id,
            action=ACTION_ARRIVED,
            actor_id=clean_actor,
            status="queued",
            confirmation_step=1,
            idempotency_key=action_key,
            payload={"structured_arrival": True, "warehouse_id": warehouse.id},
        )
        session.add(action)
        session.flush()
        first_action = first_action or action
        action_ids.append(action.id)
        enqueue_outbox(
            session,
            candidate=candidate,
            action=action,
            operation=OP_PROCESS_ACTION,
            idempotency_key=f"action:{action.id}:process",
            payload={"structured_arrival": True},
            now=now,
        )
    leader.nonce = secrets.token_hex(16)
    enqueue_outbox(
        session,
        candidate=leader,
        operation=OP_FINALIZE_STRUCTURED_ARRIVAL,
        idempotency_key=f"{key}:finalize",
        payload={"action_ids": action_ids, "actor_id": clean_actor},
        available_at=now + timedelta(seconds=1),
        now=now,
    )
    session.commit()
    assert first_action is not None
    return first_action, False


def _verified_interactive_candidate(
    session: Session,
    *,
    token: str,
    actor_id: str,
    dialog_id: str,
    settings: Settings,
    now: datetime,
) -> tuple[BitrixChatActionCandidate, CallbackToken, str]:
    secret = settings.order_fulfillment_bot_callback_secret
    if not secret:
        raise BotSecurityError("callback_secret_not_configured")
    decoded = verify_callback_token(token, secret=secret, now=now)
    candidate = session.scalar(
        select(BitrixChatActionCandidate)
        .where(BitrixChatActionCandidate.id == decoded.candidate_id)
        .with_for_update()
    )
    if candidate is None or candidate.nonce != decoded.nonce:
        raise BotSecurityError("candidate_not_found")
    if candidate.source_chat_id != dialog_id:
        raise BotSecurityError("callback_wrong_chat")
    publish_status = session.scalar(
        select(SiteOrderFulfillmentOutbox.status).where(
            SiteOrderFulfillmentOutbox.idempotency_key == f"candidate:{candidate.id}:publish"
        )
    )
    if not candidate.bot_message_id or publish_status != OUTBOX_COMPLETED:
        raise BotSecurityError("candidate_card_not_ready")
    if candidate.status not in {CANDIDATE_OPEN, CANDIDATE_DISMISSED}:
        raise BotSecurityError("candidate_already_claimed")
    clean_actor = fulfillment._clean_string(actor_id)
    if not clean_actor or not clean_actor.isdigit() or len(clean_actor) > 20:
        raise BotSecurityError("actor_invalid")
    return candidate, decoded, clean_actor


def enqueue_inventory_clarification_card(
    session: Session,
    *,
    submission: PickupInventorySubmission,
    settings: Settings,
    now: datetime | None = None,
) -> SiteOrderFulfillmentOutbox:
    now = _naive_utc(now or utcnow())
    state = _inventory_clarification_state(submission)
    if not state.get("nonce"):
        state = {
            **state,
            "nonce": secrets.token_hex(16),
            "expires_at": (
                now + timedelta(hours=settings.order_fulfillment_bot_card_ttl_hours)
            ).isoformat(),
            "status": "open",
            "source_text_hash": submission.source_message.raw_text_hash,
        }
        _set_inventory_clarification_state(submission, state)
    return enqueue_outbox(
        session,
        operation=OP_PUBLISH_INVENTORY_CLARIFICATION,
        idempotency_key=f"inventory-submission:{submission.id}:publish",
        target_type="inventory_submission",
        target_id=str(submission.id),
        payload={},
        now=now,
    )


def sign_inventory_callback_token(
    submission: PickupInventorySubmission,
    *,
    action: str,
    secret: str,
    warehouse_external_id: str | None = None,
) -> str:
    if action not in INVENTORY_ACTIONS:
        raise ValueError("unsupported_inventory_action")
    state = _inventory_clarification_state(submission)
    nonce = fulfillment._clean_string(state.get("nonce"))
    expires_at = _parse_naive_datetime(state.get("expires_at"))
    if not nonce or expires_at is None:
        raise ValueError("inventory_clarification_state_missing")
    payload: dict[str, Any] = {
        "k": INVENTORY_CALLBACK_KIND,
        "i": submission.id,
        "a": action,
        "n": nonce,
        "e": int(_aware_utc(expires_at).timestamp()),
    }
    if warehouse_external_id:
        payload["w"] = warehouse_external_id
    encoded = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64encode(signature)}"


def queue_inventory_clarification_action(
    session: Session,
    *,
    token: str,
    actor_id: str,
    dialog_id: str,
    settings: Settings,
    now: datetime | None = None,
) -> tuple[SiteOrderFulfillmentOutbox, bool]:
    now = _naive_utc(now or utcnow())
    payload = _verify_inventory_callback_token(
        token,
        secret=settings.order_fulfillment_bot_callback_secret,
        now=now,
    )
    submission = session.scalar(
        select(PickupInventorySubmission)
        .where(PickupInventorySubmission.id == int(payload["i"]))
        .with_for_update()
    )
    if submission is None or submission.source_message is None:
        raise BotSecurityError("inventory_submission_not_found")
    if submission.source_message.dialog_id != dialog_id:
        raise BotSecurityError("callback_wrong_chat")
    state = _inventory_clarification_state(submission)
    if state.get("nonce") != payload["n"] or state.get("status") != "open":
        raise BotSecurityError("inventory_clarification_not_open")
    if not fulfillment._clean_string(state.get("bot_message_id")):
        raise BotSecurityError("inventory_clarification_card_not_ready")
    clean_actor = fulfillment._clean_string(actor_id)
    if not clean_actor:
        raise BotSecurityError("actor_missing")
    action = str(payload["a"])
    warehouse_external_id = fulfillment._clean_string(payload.get("w"))
    key = (
        f"inventory-submission:{submission.id}:action:{action}:"
        f"{warehouse_external_id or '-'}:{clean_actor}"
    )
    existing = session.scalar(
        select(SiteOrderFulfillmentOutbox).where(SiteOrderFulfillmentOutbox.idempotency_key == key)
    )
    if existing is not None:
        return existing, True
    row = enqueue_outbox(
        session,
        operation=OP_PROCESS_INVENTORY_CLARIFICATION,
        idempotency_key=key,
        target_type="inventory_submission",
        target_id=str(submission.id),
        payload={
            "action": action,
            "actor_id": clean_actor,
            "warehouse_external_id": warehouse_external_id or None,
            "nonce": payload["n"],
            "source_text_hash": state.get("source_text_hash"),
        },
        now=now,
    )
    session.commit()
    return row, False


def _verify_inventory_callback_token(
    token: str,
    *,
    secret: str,
    now: datetime,
) -> dict[str, Any]:
    if not secret:
        raise BotSecurityError("callback_secret_not_configured")
    try:
        encoded, raw_signature = token.split(".", 1)
        expected = hmac.new(
            secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _b64decode(raw_signature)):
            raise BotSecurityError("invalid_callback_signature")
        payload = json.loads(_b64decode(encoded).decode("utf-8"))
        if payload.get("k") != INVENTORY_CALLBACK_KIND:
            raise BotSecurityError("invalid_callback_kind")
        if str(payload.get("a")) not in INVENTORY_ACTIONS:
            raise BotSecurityError("invalid_callback_action")
        if _naive_utc(now) >= datetime.fromtimestamp(int(payload["e"]), tz=UTC).replace(
            tzinfo=None
        ):
            raise BotSecurityError("callback_expired")
        int(payload["i"])
        if not fulfillment._clean_string(payload.get("n")):
            raise BotSecurityError("invalid_callback_token")
        return payload
    except BotSecurityError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BotSecurityError("invalid_callback_token") from exc


def queue_callback_action(
    session: Session,
    *,
    token: str,
    actor_id: str,
    dialog_id: str,
    settings: Settings,
    now: datetime | None = None,
) -> tuple[BitrixChatAction, bool]:
    now = _naive_utc(now or utcnow())
    if not settings.order_fulfillment_bot_callback_secret:
        raise BotSecurityError("callback_secret_not_configured")
    decoded = verify_callback_token(
        token,
        secret=settings.order_fulfillment_bot_callback_secret,
        now=now,
        reject_expired=False,
    )
    candidate = session.scalar(
        select(BitrixChatActionCandidate)
        .where(BitrixChatActionCandidate.id == decoded.candidate_id)
        .with_for_update()
    )
    if candidate is None or candidate.nonce != decoded.nonce:
        raise BotSecurityError("candidate_not_found")
    if candidate.source_chat_id != dialog_id:
        raise BotSecurityError("callback_wrong_chat")
    publish_status = session.scalar(
        select(SiteOrderFulfillmentOutbox.status).where(
            SiteOrderFulfillmentOutbox.idempotency_key == f"candidate:{candidate.id}:publish"
        )
    )
    if not candidate.bot_message_id or publish_status != OUTBOX_COMPLETED:
        raise BotSecurityError("candidate_card_not_ready")
    if candidate.expires_at <= now:
        candidate.status = CANDIDATE_EXPIRED
        _expire_pending_confirmation(session, candidate=candidate)
        enqueue_outbox(
            session,
            candidate=candidate,
            operation=OP_UPDATE_CARD,
            idempotency_key=f"candidate:{candidate.id}:expired-card",
            payload={"status_text": "Срок действия кнопок истёк"},
            now=now,
        )
        session.commit()
        raise BotSecurityError("callback_expired")
    clean_actor_id = str(actor_id).strip()
    if not clean_actor_id:
        raise BotSecurityError("actor_missing")
    if not clean_actor_id.isdigit() or len(clean_actor_id) > 20:
        raise BotSecurityError("actor_invalid")
    if decoded.action == ACTION_FOUND_OTHER:
        if decoded.step == 1 and decoded.target_warehouse_id is not None:
            raise BotSecurityError("unexpected_target_warehouse")
        if decoded.step == 2:
            warehouse = _selectable_lost_order_warehouse(
                session,
                warehouse_id=decoded.target_warehouse_id,
                settings=settings,
            )
            if warehouse is None:
                raise BotSecurityError("invalid_target_warehouse")
    elif decoded.target_warehouse_id is not None:
        raise BotSecurityError("unexpected_target_warehouse")
    target_suffix = (
        f":warehouse:{decoded.target_warehouse_id}"
        if decoded.target_warehouse_id is not None
        else ""
    )
    idempotency_key = (
        f"candidate:{candidate.id}:action:{decoded.action}:step:{decoded.step}{target_suffix}"
    )
    existing = session.scalar(
        select(BitrixChatAction).where(BitrixChatAction.idempotency_key == idempotency_key)
    )
    if existing is not None:
        if existing.actor_id != clean_actor_id:
            raise BotSecurityError("callback_already_claimed")
        return existing, True
    if candidate.status in {
        CANDIDATE_APPLIED,
        CANDIDATE_DRY_RUN,
        CANDIDATE_DISMISSED,
        CANDIDATE_EXPIRED,
        CANDIDATE_REVIEW,
    }:
        raise BotSecurityError("candidate_closed")
    if decoded.step == 2:
        if decoded.action not in DANGEROUS_ACTIONS:
            raise BotSecurityError("invalid_confirmation_action")
        if (
            candidate.status != CANDIDATE_CONFIRMATION
            or candidate.active_action != decoded.action
            or candidate.active_actor_id != clean_actor_id
        ):
            raise BotSecurityError("confirmation_not_expected")
    elif decoded.action == ACTION_CANCEL and candidate.status == CANDIDATE_CONFIRMATION:
        if candidate.active_actor_id != clean_actor_id:
            raise BotSecurityError("callback_already_claimed")
    elif candidate.status != CANDIDATE_OPEN:
        raise BotSecurityError("candidate_already_claimed")
    action = BitrixChatAction(
        candidate_id=candidate.id,
        action=decoded.action,
        actor_id=clean_actor_id,
        status="queued",
        confirmation_step=decoded.step,
        idempotency_key=idempotency_key,
        payload={"target_warehouse_id": decoded.target_warehouse_id},
    )
    session.add(action)
    session.flush()
    candidate.active_action = decoded.action
    candidate.active_actor_id = clean_actor_id
    candidate.action_claimed_at = now
    candidate.status = CANDIDATE_QUEUED
    candidate.updated_at = now
    enqueue_outbox(
        session,
        candidate=candidate,
        action=action,
        operation=OP_PROCESS_ACTION,
        idempotency_key=f"action:{action.id}:process",
        payload={},
        now=now,
    )
    session.commit()
    return action, False


def enqueue_outbox(
    session: Session,
    *,
    operation: str,
    idempotency_key: str,
    payload: dict[str, Any],
    candidate: BitrixChatActionCandidate | None = None,
    action: BitrixChatAction | None = None,
    depends_on: SiteOrderFulfillmentOutbox | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    available_at: datetime | None = None,
    now: datetime | None = None,
) -> SiteOrderFulfillmentOutbox:
    existing = session.scalar(
        select(SiteOrderFulfillmentOutbox).where(
            SiteOrderFulfillmentOutbox.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        return existing
    row = SiteOrderFulfillmentOutbox(
        candidate_id=candidate.id if candidate is not None else None,
        action_id=action.id if action is not None else None,
        depends_on_id=depends_on.id if depends_on is not None else None,
        operation=operation,
        target_type=target_type,
        target_id=target_id,
        status=OUTBOX_PENDING,
        attempts=0,
        max_attempts=8,
        available_at=_naive_utc(available_at or now or utcnow()),
        idempotency_key=idempotency_key,
        payload=payload,
    )
    session.add(row)
    session.flush()
    return row


def card_text(
    candidate: BitrixChatActionCandidate,
    *,
    status_text: str | None = None,
    details: list[str] | None = None,
) -> str:
    interaction = fulfillment._clean_string((candidate.payload or {}).get("interaction"))
    prefix = "Тест — без изменений\n" if candidate.dry_run and interaction != "search" else ""
    point = candidate.pickup_point_name or "точка не определена"
    if interaction == "search":
        lines = [f"Заказ №{candidate.site_order_number}", *(details or [])]
        if status_text:
            lines.append(status_text)
        return "\n".join(lines)
    if interaction == "structured_arrival":
        order_numbers = list((candidate.payload or {}).get("order_numbers") or [])
        lines = [
            f"{prefix}Фиксация поступления",
            f"Заказы: {', '.join(order_numbers)}",
            f"Точка: {point}",
            *(details or []),
        ]
        if status_text:
            lines.append(status_text)
        return "\n".join(lines)
    lines = [
        f"{prefix}Заказ №{candidate.site_order_number}",
        f"Распознано: {ACTION_LABELS.get(candidate.detected_action, candidate.detected_action)}",
        f"Точка: {point}",
    ]
    if status_text:
        lines.append(status_text)
    return "\n".join(lines)


def pickup_menu_text() -> str:
    return (
        "Самовывоз Master Mobile\n"
        "\n"
        "Выберите действие. После нажатия допишите только номер заказа "
        "или несколько номеров через пробел и отправьте сообщение.\n"
        "\n"
        "Поиск ничего не меняет в CRM. Поступление применяется только после "
        "проверки точки и отдельного подтверждения."
    )


def pickup_menu_keyboard() -> list[dict[str, Any]]:
    return [
        {
            "TEXT": "Найти заказ",
            "ACTION": "PUT",
            "ACTION_VALUE": "Найти заказ ",
            "BG_COLOR": "#2FC6F6",
            "BLOCK": "Y",
        },
        {
            "TEXT": "Зафиксировать поступление",
            "ACTION": "PUT",
            "ACTION_VALUE": "Зафиксировать поступление ",
            "BG_COLOR": "#9DCF00",
            "BLOCK": "Y",
        },
    ]


def card_keyboard(
    candidate: BitrixChatActionCandidate,
    *,
    settings: Settings,
    session: Session | None = None,
) -> list[dict[str, Any]]:
    secret = settings.order_fulfillment_bot_callback_secret
    if not secret:
        return []
    colors = {
        ACTION_ARRIVED: "#2FC6F6",
        ACTION_MOVING: "#2F80ED",
        ACTION_ISSUED: "#9DCF00",
        ACTION_UNCLAIMED: "#F5A623",
        ACTION_DISMANTLE: "#E74C3C",
        ACTION_FOUND_EXPECTED: "#9DCF00",
        ACTION_FOUND_OTHER: "#2F80ED",
        ACTION_RETURNED: "#E74C3C",
        ACTION_NOT_FOUND: "#F5A623",
        ACTION_REFRESH: "#2FC6F6",
        ACTION_START_SEARCH: "#2F80ED",
        ACTION_OTHER_OUTCOME: "#F5A623",
        ACTION_CONFIRM_ARRIVAL: "#9DCF00",
        ACTION_CANCEL: "#A6A6A6",
    }
    interaction = fulfillment._clean_string((candidate.payload or {}).get("interaction"))
    if interaction == "search":
        view = fulfillment._clean_string((candidate.payload or {}).get("view")) or "primary"
        if view == "search":
            actions = (
                ACTION_FOUND_EXPECTED,
                ACTION_FOUND_OTHER,
                ACTION_NOT_FOUND,
                ACTION_OTHER_OUTCOME,
                ACTION_CANCEL,
            )
        elif view == "other":
            actions = (ACTION_ISSUED, ACTION_RETURNED, ACTION_CANCEL)
        else:
            actions = (
                (ACTION_REFRESH, ACTION_START_SEARCH)
                if (candidate.payload or {}).get("search_allowed", True)
                else (ACTION_REFRESH,)
            )
        buttons = [
            {
                "TEXT": (
                    "Найден на своей точке"
                    if action == ACTION_FOUND_EXPECTED
                    else "Отмена" if action == ACTION_CANCEL else ACTION_LABELS[action]
                ),
                "COMMAND": settings.order_fulfillment_bot_command,
                "COMMAND_PARAMS": sign_callback_token(
                    candidate,
                    action=action,
                    step=1,
                    secret=secret,
                ),
                "BG_COLOR": colors[action],
                "BLOCK": "Y",
            }
            for action in actions
        ]
        if view == "primary" and candidate.bitrix_deal_id is not None:
            base_url = fulfillment._clean_string(settings.order_fulfillment_bot_portal_base_url)
            parsed_url = urlparse(base_url)
            if (
                parsed_url.scheme == "https"
                and parsed_url.hostname
                and parsed_url.hostname.casefold()
                in {domain.casefold() for domain in settings.order_fulfillment_bot_allowed_domains}
            ):
                buttons.insert(
                    1,
                    {
                        "TEXT": "Открыть сделку",
                        "LINK": f"{base_url.rstrip('/')}/crm/deal/details/{candidate.bitrix_deal_id}/",
                        "BG_COLOR": "#9DCF00",
                        "BLOCK": "Y",
                    },
                )
        return buttons
    if interaction == "structured_arrival":
        if (candidate.payload or {}).get("arrival_blocked"):
            return [
                {
                    "TEXT": "Отмена",
                    "COMMAND": settings.order_fulfillment_bot_command,
                    "COMMAND_PARAMS": sign_callback_token(
                        candidate,
                        action=ACTION_CANCEL,
                        step=1,
                        secret=secret,
                    ),
                    "BG_COLOR": colors[ACTION_CANCEL],
                    "BLOCK": "Y",
                }
            ]
        if session is None:
            return []
        warehouses = _selectable_pickup_warehouses(session, settings=settings)
        return [
            {
                "TEXT": (
                    f"Подтвердить: {warehouse.name}"
                    if candidate.pickup_point_warehouse_id == warehouse.id
                    else warehouse.name
                ),
                "COMMAND": settings.order_fulfillment_bot_command,
                "COMMAND_PARAMS": sign_callback_token(
                    candidate,
                    action=ACTION_CONFIRM_ARRIVAL,
                    step=1,
                    secret=secret,
                    target_warehouse_id=warehouse.id,
                ),
                "BG_COLOR": (
                    colors[ACTION_CONFIRM_ARRIVAL]
                    if candidate.pickup_point_warehouse_id == warehouse.id
                    else "#2F80ED"
                ),
                "BLOCK": "Y",
            }
            for warehouse in warehouses
        ] + [
            {
                "TEXT": "Отмена",
                "COMMAND": settings.order_fulfillment_bot_command,
                "COMMAND_PARAMS": sign_callback_token(
                    candidate,
                    action=ACTION_CANCEL,
                    step=1,
                    secret=secret,
                ),
                "BG_COLOR": colors[ACTION_CANCEL],
                "BLOCK": "Y",
            }
        ]
    if (
        settings.order_fulfillment_lost_orders_enabled
        and candidate.source_chat_id == settings.order_fulfillment_pickup_exception_chat_dialog_id
    ):
        actions = (
            ACTION_FOUND_EXPECTED,
            ACTION_FOUND_OTHER,
            ACTION_ISSUED,
            ACTION_RETURNED,
            ACTION_NOT_FOUND,
            ACTION_CANCEL,
        )
    else:
        actions = (
            ACTION_ARRIVED,
            ACTION_MOVING,
            ACTION_ISSUED,
            ACTION_UNCLAIMED,
            ACTION_DISMANTLE,
            ACTION_CANCEL,
        )
    return [
        {
            "TEXT": ACTION_LABELS[action],
            "COMMAND": settings.order_fulfillment_bot_command,
            "COMMAND_PARAMS": sign_callback_token(
                candidate,
                action=action,
                step=1,
                secret=secret,
            ),
            "BG_COLOR": colors[action],
            "BLOCK": "Y",
        }
        for action in actions
    ]


def decide_pickup_action(
    *,
    action: str,
    confirmation_step: int,
    deal: fulfillment.BitrixDealSnapshot,
    candidate: BitrixChatActionCandidate,
    case: SiteOrderExecutionCase | None,
    onec: OneCPickupValidation,
    settings: Settings,
    now: datetime | None = None,
) -> PickupActionDecision:
    now = _naive_utc(now or utcnow())
    stage = fulfillment._clean_string(deal.stage_id)
    if action not in ACTIONS:
        return PickupActionDecision(False, None, None, "unsupported_action")
    if action == ACTION_CANCEL:
        return PickupActionDecision(True, None, None, "candidate_dismissed")
    if not fulfillment._is_internal_pickup_deal(deal):
        return PickupActionDecision(False, None, None, "delivery_mismatch")
    if stage in TERMINAL_STAGES and not (action == ACTION_RETURNED and stage == "DISMANTLING"):
        return PickupActionDecision(False, None, None, "terminal_crm_stage")
    if candidate.pickup_point_warehouse_id is None and action == ACTION_ARRIVED:
        return PickupActionDecision(False, None, None, "pickup_point_unresolved")
    if action not in {
        ACTION_MOVING,
        ACTION_FOUND_EXPECTED,
        ACTION_FOUND_OTHER,
    } and _deal_pickup_point_conflicts(candidate, deal=deal):
        return PickupActionDecision(False, None, None, "pickup_point_deal_mismatch")
    if (
        action not in {ACTION_MOVING, ACTION_FOUND_EXPECTED, ACTION_FOUND_OTHER}
        and case is not None
        and case.pickup_point_warehouse_id is not None
        and candidate.pickup_point_warehouse_id is not None
        and case.pickup_point_warehouse_id != candidate.pickup_point_warehouse_id
    ):
        return PickupActionDecision(False, None, None, "pickup_point_mismatch")
    if not onec.available:
        return PickupActionDecision(False, None, None, "onec_unavailable")
    if onec.return_confirmed and action in {
        ACTION_MOVING,
        ACTION_ARRIVED,
        ACTION_ISSUED,
        ACTION_FOUND_EXPECTED,
        ACTION_FOUND_OTHER,
    }:
        return PickupActionDecision(False, None, None, "onec_return_conflict")

    if (
        action
        in {
            ACTION_FOUND_EXPECTED,
            ACTION_FOUND_OTHER,
            ACTION_RETURNED,
            ACTION_NOT_FOUND,
        }
        and not settings.order_fulfillment_lost_orders_enabled
    ):
        return PickupActionDecision(False, None, None, "lost_orders_disabled")

    if action == ACTION_FOUND_EXPECTED:
        if stage != fulfillment.CRM_STAGE_PICKUP_WAITING:
            return PickupActionDecision(False, None, None, "lost_order_stage_not_allowed")
        if case is None or case.pickup_point_warehouse_id is None:
            return PickupActionDecision(False, None, None, "lost_order_point_missing")
        return PickupActionDecision(
            True,
            None,
            fulfillment.EVENT_PICKUP_STORED,
            "lost_order_found_expected_point",
        )

    if action == ACTION_FOUND_OTHER:
        if confirmation_step != 2:
            return PickupActionDecision(False, None, None, "second_confirmation_required")
        if stage != fulfillment.CRM_STAGE_PICKUP_WAITING:
            return PickupActionDecision(False, None, None, "lost_order_stage_not_allowed")
        if candidate.pickup_point_warehouse_id is None:
            return PickupActionDecision(False, None, None, "lost_order_target_missing")
        if (
            case is not None
            and case.pickup_point_warehouse_id == candidate.pickup_point_warehouse_id
        ):
            return PickupActionDecision(False, None, None, "lost_order_target_same")
        return PickupActionDecision(
            True,
            None,
            fulfillment.EVENT_PICKUP_REDIRECTED,
            "lost_order_found_other_point",
        )

    if action == ACTION_NOT_FOUND:
        if stage != fulfillment.CRM_STAGE_PICKUP_WAITING:
            return PickupActionDecision(False, None, None, "lost_order_stage_not_allowed")
        if case is None or case.pickup_point_warehouse_id is None:
            return PickupActionDecision(False, None, None, "lost_order_point_missing")
        return PickupActionDecision(
            True,
            None,
            fulfillment.EVENT_PICKUP_EXCEPTION,
            "lost_order_search_required",
            create_task="lost_search",
        )

    if action == ACTION_RETURNED:
        if confirmation_step != 2:
            return PickupActionDecision(False, None, None, "second_confirmation_required")
        if stage not in {fulfillment.CRM_STAGE_PICKUP_WAITING, "DISMANTLING"}:
            return PickupActionDecision(False, None, None, "lost_order_stage_not_allowed")
        if not onec.return_confirmed:
            return PickupActionDecision(False, None, None, "return_not_confirmed")
        if onec.payment_confirmed or onec.issued_confirmed:
            return PickupActionDecision(False, None, None, "return_payment_conflict")
        return PickupActionDecision(
            True,
            "LOSE",
            fulfillment.EVENT_PICKUP_DISMANTLED,
            "pickup_return_confirmed",
        )

    if action == ACTION_MOVING:
        if stage not in {
            "PREPARATION",
            "EXECUTING",
            "FINAL_INVOICE",
            fulfillment.CRM_STAGE_PICKUP_WAITING,
        }:
            return PickupActionDecision(False, None, None, "arrival_transition_not_allowed")
        if candidate.pickup_point_warehouse_id is None:
            return PickupActionDecision(False, None, None, "pickup_point_unresolved")
        if not onec.assembled:
            return PickupActionDecision(False, None, None, "assembly_not_confirmed")
        if stage == fulfillment.CRM_STAGE_PICKUP_WAITING:
            return PickupActionDecision(
                True,
                None,
                fulfillment.EVENT_PICKUP_REDIRECTED,
                "pickup_redirected_confirmed",
            )
        return PickupActionDecision(
            True,
            "FINAL_INVOICE",
            fulfillment.EVENT_PICKUP_MOVING,
            "pickup_moving_confirmed",
        )

    if action == ACTION_ARRIVED:
        if stage not in ALLOWED_ARRIVAL_STAGES:
            return PickupActionDecision(False, None, None, "arrival_transition_not_allowed")
        if not onec.assembled:
            return PickupActionDecision(False, None, None, "assembly_not_confirmed")
        first_arrival = case is None or case.storage_started_at is None
        return PickupActionDecision(
            True,
            fulfillment.CRM_STAGE_PICKUP_WAITING,
            fulfillment.EVENT_PICKUP_STORED,
            "pickup_arrival_confirmed",
            send_sms=first_arrival,
        )

    if action == ACTION_ISSUED:
        if confirmation_step != 2:
            return PickupActionDecision(False, None, None, "second_confirmation_required")
        if stage != fulfillment.CRM_STAGE_PICKUP_WAITING:
            return PickupActionDecision(False, None, None, "issued_transition_not_allowed")
        if not (onec.payment_confirmed or onec.issued_confirmed):
            return PickupActionDecision(False, None, None, "issued_payment_not_confirmed")
        return PickupActionDecision(
            True,
            "WON",
            fulfillment.EVENT_PICKUP_RECEIVED,
            "pickup_issued_confirmed",
        )

    if action == ACTION_UNCLAIMED:
        if stage != fulfillment.CRM_STAGE_PICKUP_WAITING:
            return PickupActionDecision(False, None, None, "unclaimed_transition_not_allowed")
        due = bool(
            case is not None
            and case.sla_started_at is not None
            and now
            >= case.sla_started_at
            + timedelta(hours=settings.order_fulfillment_bot_call_after_hours)
        )
        return PickupActionDecision(
            True,
            None,
            fulfillment.EVENT_PICKUP_UNCLAIMED,
            "pickup_unclaimed_recorded",
            create_task="call" if due else None,
        )

    if not settings.order_fulfillment_pickup_sla_enabled:
        return PickupActionDecision(False, None, None, "pickup_sla_disabled")
    if confirmation_step != 2:
        return PickupActionDecision(False, None, None, "second_confirmation_required")
    if stage != fulfillment.CRM_STAGE_PICKUP_WAITING:
        return PickupActionDecision(False, None, None, "dismantle_transition_not_allowed")
    if case is None or case.sla_started_at is None:
        return PickupActionDecision(False, None, None, "sla_start_missing")
    if case.hold_until is not None and case.hold_until > _moscow_date(now):
        return PickupActionDecision(False, None, None, "dismantle_too_early")
    if now < case.sla_started_at + timedelta(
        hours=settings.order_fulfillment_bot_dismantle_after_hours
    ):
        return PickupActionDecision(False, None, None, "dismantle_too_early")
    if onec.payment_confirmed or onec.debt_conflict or onec.issued_confirmed:
        return PickupActionDecision(False, None, None, "dismantle_payment_conflict")
    return PickupActionDecision(
        True,
        "DISMANTLING",
        fulfillment.EVENT_PICKUP_DISMANTLING,
        "pickup_dismantle_confirmed",
        create_task="dismantle",
    )


def missing_receipt_movement_is_open(
    session: Session,
    *,
    movement: SiteOrderExecutionEvent,
) -> bool:
    """Return whether a dispatch still lacks a later point-receipt fact."""

    if (
        movement.event_type
        not in {
            fulfillment.EVENT_PICKUP_MOVING,
            fulfillment.EVENT_PICKUP_REDIRECTED,
        }
        or movement.event_at is None
    ):
        return False
    case = session.get(SiteOrderExecutionCase, movement.case_id)
    if case is None or case.delivered_at is not None or case.cancelled_at is not None:
        return False
    latest_movement_id = session.scalar(
        select(SiteOrderExecutionEvent.id)
        .where(
            SiteOrderExecutionEvent.case_id == movement.case_id,
            SiteOrderExecutionEvent.event_type.in_(
                [
                    fulfillment.EVENT_PICKUP_MOVING,
                    fulfillment.EVENT_PICKUP_REDIRECTED,
                ]
            ),
            SiteOrderExecutionEvent.event_at.is_not(None),
        )
        .order_by(
            SiteOrderExecutionEvent.event_at.desc(),
            SiteOrderExecutionEvent.id.desc(),
        )
        .limit(1)
    )
    if latest_movement_id != movement.id:
        return False
    later_receipt = session.scalar(
        select(SiteOrderExecutionEvent.id)
        .where(
            SiteOrderExecutionEvent.case_id == movement.case_id,
            SiteOrderExecutionEvent.event_type.in_(
                [
                    fulfillment.EVENT_PICKUP_STORED,
                    fulfillment.EVENT_PICKUP_RECEIVED,
                    fulfillment.EVENT_PICKUP_UNCLAIMED,
                    fulfillment.EVENT_PICKUP_DISMANTLING,
                    fulfillment.EVENT_PICKUP_DISMANTLED,
                    fulfillment.EVENT_PICKUP_EXCEPTION,
                    fulfillment.EVENT_PICKUP_DISMANTLE_CANDIDATE,
                ]
            ),
            or_(
                SiteOrderExecutionEvent.event_at > movement.event_at,
                (
                    (SiteOrderExecutionEvent.event_at == movement.event_at)
                    & (SiteOrderExecutionEvent.id > movement.id)
                ),
            ),
        )
        .limit(1)
    )
    return later_receipt is None


def pickup_expected_warehouse_ids(
    session: Session,
    *,
    case: SiteOrderExecutionCase | None,
    deal: fulfillment.BitrixDealSnapshot,
    settings: Settings,
) -> set[int]:
    expected_ids: set[int] = set()
    if case is not None and case.pickup_point_warehouse_id is not None:
        expected_ids.add(case.pickup_point_warehouse_id)
    resolution = pickup_inventory.resolve_pickup_inventory_warehouse(
        session,
        " ".join(value for value in (deal.delivery, deal.post_delivery_type) if value),
        pickup_aliases=settings.order_fulfillment_pickup_warehouse_aliases,
    )
    if resolution.warehouse is not None:
        expected_ids.add(resolution.warehouse.id)
    return expected_ids


def _missing_receipt_snapshot(
    session: Session,
    *,
    movement_id: int,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], OneCPickupValidation],
) -> tuple[MissingReceiptSnapshot | None, str]:
    movement = session.get(SiteOrderExecutionEvent, movement_id)
    if movement is None or not missing_receipt_movement_is_open(session, movement=movement):
        return None, "receipt_or_later_movement_recorded"
    case = session.get(SiteOrderExecutionCase, movement.case_id)
    warehouse = (
        session.get(LogisticsWarehouse, movement.warehouse_id)
        if movement.warehouse_id is not None
        else None
    )
    if case is None or warehouse is None or case.bitrix_deal_id is None:
        return None, "movement_context_incomplete"
    try:
        deals = client.list_deals_by_site_order(case.site_order_number)
    except Exception as exc:
        raise RetryableBeforeExternalEffect(str(exc)) from exc
    if len(deals) != 1:
        return None, "deal_not_unique"
    deal = deals[0]
    live_order_number = fulfillment._clean_string(
        (deal.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
    )
    if deal.deal_id != case.bitrix_deal_id or live_order_number != case.site_order_number:
        return None, "deal_identity_changed"
    if fulfillment._clean_string(deal.stage_id) in TERMINAL_STAGES:
        return None, "deal_closed"
    if not fulfillment._is_internal_pickup_deal(deal):  # noqa: SLF001
        return None, "deal_not_internal_pickup"
    expected_ids = pickup_expected_warehouse_ids(
        session,
        case=case,
        deal=deal,
        settings=settings,
    )
    if expected_ids != {warehouse.id}:
        return None, "pickup_point_conflict"
    try:
        onec = onec_validator(case.site_order_number)
    except Exception as exc:
        raise RetryableBeforeExternalEffect(str(exc)) from exc
    if not onec.available:
        raise RetryableBeforeExternalEffect("onec_read_unavailable")
    if not onec.assembled:
        raise RetryableBeforeExternalEffect("onec_assembly_not_confirmed")
    if onec.return_confirmed:
        return None, "onec_return_confirmed"
    if onec.issued_confirmed:
        return None, "onec_issue_confirmed"
    return (
        MissingReceiptSnapshot(
            movement=movement,
            case=case,
            deal=deal,
            warehouse=warehouse,
            onec=onec,
        ),
        "open",
    )


def _suppress_missing_receipt_row(
    row: SiteOrderFulfillmentOutbox,
    *,
    reason: str,
) -> None:
    row.payload = {**(row.payload or {}), "suppressed_reason": reason}


def process_outbox(
    session: Session,
    *,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], OneCPickupValidation],
    apply_enabled_probe: Callable[[], bool] | None = None,
    missing_receipt_enabled_probe: Callable[[], bool] | None = None,
    limit: int = 50,
    now: datetime | None = None,
) -> dict[str, int]:
    now = _naive_utc(now or utcnow())
    stats = {
        "selected": 0,
        "recovered": 0,
        "expired": 0,
        "completed": 0,
        "retry": 0,
        "failed": 0,
    }
    expired_candidates = session.scalars(
        select(BitrixChatActionCandidate)
        .where(
            BitrixChatActionCandidate.status.in_([CANDIDATE_OPEN, CANDIDATE_CONFIRMATION]),
            BitrixChatActionCandidate.expires_at <= now,
            BitrixChatActionCandidate.bot_message_id.is_not(None),
        )
        .order_by(BitrixChatActionCandidate.id.asc())
        .with_for_update(skip_locked=True)
        .limit(limit)
    ).all()
    for candidate in expired_candidates:
        candidate.status = CANDIDATE_EXPIRED
        candidate.updated_at = now
        _expire_pending_confirmation(session, candidate=candidate)
        enqueue_outbox(
            session,
            candidate=candidate,
            operation=OP_UPDATE_CARD,
            idempotency_key=f"candidate:{candidate.id}:expired-card",
            payload={"status_text": "Срок действия кнопок истёк"},
            now=now,
        )
        stats["expired"] += 1
    if expired_candidates:
        session.commit()
    stale_query = (
        select(SiteOrderFulfillmentOutbox)
        .where(
            SiteOrderFulfillmentOutbox.status == OUTBOX_PROCESSING,
            SiteOrderFulfillmentOutbox.updated_at <= now - timedelta(minutes=15),
        )
        .order_by(SiteOrderFulfillmentOutbox.id.asc())
        .with_for_update(skip_locked=True)
        .limit(limit)
    )
    if not _runtime_apply_enabled(settings, apply_enabled_probe):
        stale_query = stale_query.where(
            SiteOrderFulfillmentOutbox.operation.notin_(APPLY_GATED_OUTBOX_OPERATIONS)
        )
    stale_rows = session.scalars(stale_query).all()
    for stale in stale_rows:
        if stale.operation in APPLY_GATED_OUTBOX_OPERATIONS and not _runtime_apply_enabled(
            settings, apply_enabled_probe
        ):
            continue
        stats["recovered"] += 1
        sms_marker_present = False
        if stale.operation == OP_START_SMS_WORKFLOW:
            try:
                sms_marker_present = _sms_workflow_marker_present(
                    session,
                    row=stale,
                    client=client,
                    settings=settings,
                )
            except Exception as exc:
                stale.last_error = (
                    "sms_reconciliation_unavailable:"
                    + fulfillment._safe_error_reason(str(exc))[:900]
                )
                stale.updated_at = now
                stats["retry"] += 1
                continue
        if sms_marker_present:
            _finish_outbox(stale, status=OUTBOX_COMPLETED, error=None, now=now)
            stats["completed"] += 1
        elif stale.operation in NON_RETRYABLE_EXTERNAL_OPERATIONS:
            _finish_outbox(
                stale,
                status=OUTBOX_FAILED,
                error="ambiguous_external_result_manual_review",
                now=now,
            )
            _mark_candidate_manual_review(
                session,
                row=stale,
                reason="ambiguous_external_result_manual_review",
                now=now,
            )
            stats["failed"] += 1
        else:
            stale.status = OUTBOX_RETRY
            stale.available_at = now
            stale.last_error = "worker_interrupted"
            stale.updated_at = now
            stats["retry"] += 1
    if stale_rows:
        session.commit()

    dependency = aliased(SiteOrderFulfillmentOutbox)
    dependency_status = (
        select(dependency.status)
        .where(dependency.id == SiteOrderFulfillmentOutbox.depends_on_id)
        .scalar_subquery()
    )
    deferred_row_ids: set[int] = set()
    for _ in range(limit):
        pending_query = (
            select(SiteOrderFulfillmentOutbox)
            .where(
                SiteOrderFulfillmentOutbox.status.in_([OUTBOX_PENDING, OUTBOX_RETRY]),
                SiteOrderFulfillmentOutbox.available_at <= now,
                or_(
                    SiteOrderFulfillmentOutbox.depends_on_id.is_(None),
                    dependency_status.in_([OUTBOX_COMPLETED, OUTBOX_FAILED]),
                ),
            )
            .order_by(SiteOrderFulfillmentOutbox.id.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if deferred_row_ids:
            pending_query = pending_query.where(
                SiteOrderFulfillmentOutbox.id.notin_(deferred_row_ids)
            )
        if not _runtime_apply_enabled(settings, apply_enabled_probe):
            pending_query = pending_query.where(
                SiteOrderFulfillmentOutbox.operation.notin_(APPLY_GATED_OUTBOX_OPERATIONS)
            )
        row = session.scalar(pending_query)
        if row is None:
            break
        if row.operation in APPLY_GATED_OUTBOX_OPERATIONS and not _runtime_apply_enabled(
            settings, apply_enabled_probe
        ):
            session.rollback()
            continue
        stats["selected"] += 1
        if row.depends_on_id:
            dependency_row = session.get(SiteOrderFulfillmentOutbox, row.depends_on_id)
            if dependency_row is None or dependency_row.status == OUTBOX_FAILED:
                _mark_candidate_manual_review(
                    session,
                    row=row,
                    reason="dependency_failed",
                    now=now,
                )
                if row.operation == OP_UPDATE_CARD:
                    row.payload = {
                        **(row.payload or {}),
                        "status_text": "Нужна ручная проверка: действие завершилось с ошибкой",
                    }
                else:
                    _finish_outbox(
                        row,
                        status=OUTBOX_FAILED,
                        error="dependency_failed",
                        now=now,
                    )
                    stats["failed"] += 1
                    session.commit()
                    continue
        original_status = row.status
        original_attempts = row.attempts
        original_available_at = row.available_at
        original_last_error = row.last_error
        original_updated_at = row.updated_at
        row.status = OUTBOX_PROCESSING
        row.attempts = original_attempts + 1
        row.updated_at = now
        session.commit()
        row_id = row.id
        try:
            _dispatch_outbox(
                session,
                row=row,
                client=client,
                settings=settings,
                onec_validator=onec_validator,
                apply_enabled_probe=apply_enabled_probe,
                missing_receipt_enabled_probe=missing_receipt_enabled_probe,
                now=now,
            )
            _finish_outbox(row, status=OUTBOX_COMPLETED, error=None, now=now)
            stats["completed"] += 1
        except ApplyDisabledBeforeSideEffect:
            session.rollback()
            row = session.get(SiteOrderFulfillmentOutbox, row_id)
            if row is None:
                raise RuntimeError(f"outbox_row_disappeared:{row_id}") from None
            row.status = original_status
            row.attempts = original_attempts
            row.available_at = original_available_at
            row.last_error = original_last_error
            row.updated_at = original_updated_at
            deferred_row_ids.add(row_id)
            stats["selected"] -= 1
        except Exception as exc:  # durable boundary: persist a safe retry state
            session.rollback()
            row = session.get(SiteOrderFulfillmentOutbox, row_id)
            if row is None:
                raise RuntimeError(f"outbox_row_disappeared:{row_id}") from exc
            error = fulfillment._safe_error_reason(str(exc))[:1000]
            if isinstance(exc, SourceMessageEditedBeforeApply):
                _finish_outbox(row, status=OUTBOX_FAILED, error=error, now=now)
                _mark_candidate_manual_review(
                    session,
                    row=row,
                    reason=error,
                    now=now,
                )
                stats["failed"] += 1
            elif isinstance(exc, SmsMarkerNotConfirmed):
                _finish_outbox(row, status=OUTBOX_FAILED, error=error, now=now)
                _mark_candidate_manual_review(
                    session,
                    row=row,
                    reason=error,
                    now=now,
                )
                stats["failed"] += 1
            elif row.operation in NON_RETRYABLE_EXTERNAL_OPERATIONS and not isinstance(
                exc, RetryableBeforeExternalEffect
            ):
                _finish_outbox(row, status=OUTBOX_FAILED, error=error, now=now)
                _mark_candidate_manual_review(
                    session,
                    row=row,
                    reason=error,
                    now=now,
                )
                stats["failed"] += 1
            elif row.attempts >= row.max_attempts:
                _finish_outbox(row, status=OUTBOX_FAILED, error=error, now=now)
                _mark_candidate_manual_review(
                    session,
                    row=row,
                    reason=error,
                    now=now,
                )
                stats["failed"] += 1
            else:
                row.status = OUTBOX_RETRY
                row.last_error = error
                row.available_at = now + timedelta(minutes=min(60, 2**row.attempts))
                row.updated_at = now
                stats["retry"] += 1
        session.commit()
    return stats


def _runtime_apply_enabled(
    settings: Settings,
    apply_enabled_probe: Callable[[], bool] | None,
) -> bool:
    if not settings.order_fulfillment_bot_apply_enabled:
        return False
    return bool(apply_enabled_probe()) if apply_enabled_probe is not None else True


def runtime_apply_enabled_from_env(
    *,
    initial_enabled: bool,
    env_file: Path = DEFAULT_RUNTIME_ENV_FILE,
) -> bool:
    if not initial_enabled:
        return False
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    raw_value: str | None = None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == APPLY_ENABLED_ENV_KEY:
            raw_value = value.strip().strip('"').strip("'")
    return str(raw_value or "").strip().casefold() in TRUE_ENV_VALUES


def runtime_feature_enabled_from_env(
    *,
    initial_enabled: bool,
    env_key: str,
    env_file: Path = DEFAULT_RUNTIME_ENV_FILE,
) -> bool:
    """Re-read an independently gated feature flag immediately before effects."""

    if not initial_enabled:
        return False
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    raw_value: str | None = None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == env_key:
            raw_value = value.strip().strip('"').strip("'")
    return str(raw_value or "").strip().casefold() in TRUE_ENV_VALUES


def _runtime_missing_receipt_enabled(
    settings: Settings,
    enabled_probe: Callable[[], bool] | None,
) -> bool:
    if not settings.order_fulfillment_pickup_missing_receipt_enabled:
        return False
    return bool(enabled_probe()) if enabled_probe is not None else True


def _require_missing_receipt_enabled(
    settings: Settings,
    enabled_probe: Callable[[], bool] | None,
) -> None:
    if not _runtime_missing_receipt_enabled(settings, enabled_probe):
        raise ApplyDisabledBeforeSideEffect("order_fulfillment_pickup_missing_receipt_disabled")


def _require_runtime_apply_enabled(
    settings: Settings,
    apply_enabled_probe: Callable[[], bool] | None,
) -> None:
    if not _runtime_apply_enabled(settings, apply_enabled_probe):
        raise ApplyDisabledBeforeSideEffect("order_fulfillment_bot_apply_disabled")


def _outbox_candidate(
    session: Session,
    row: SiteOrderFulfillmentOutbox,
) -> BitrixChatActionCandidate | None:
    if row.candidate_id is None:
        return None
    return session.get(BitrixChatActionCandidate, row.candidate_id)


def _dispatch_outbox(
    session: Session,
    *,
    row: SiteOrderFulfillmentOutbox,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], OneCPickupValidation],
    apply_enabled_probe: Callable[[], bool] | None = None,
    missing_receipt_enabled_probe: Callable[[], bool] | None = None,
    now: datetime,
) -> None:
    if row.operation in APPLY_GATED_OUTBOX_OPERATIONS and not _runtime_apply_enabled(
        settings, apply_enabled_probe
    ):
        raise ApplyDisabledBeforeSideEffect("order_fulfillment_bot_apply_disabled")
    candidate = _outbox_candidate(session, row)
    action = session.get(BitrixChatAction, row.action_id) if row.action_id else None
    if (
        candidate is not None
        and candidate.raw_message is not None
        and candidate.raw_message.parse_status == "edited_manual_review"
        and row.operation
        in {
            OP_UPDATE_CRM_STAGE,
            OP_FINALIZE_ACTION,
            OP_UPDATE_CRM_FIELDS,
            OP_START_SMS_WORKFLOW,
            OP_CREATE_TASK,
        }
    ):
        raise SourceMessageEditedBeforeApply("source_message_edited_before_apply")
    if row.operation in {
        OP_UPDATE_CRM_STAGE,
        OP_UPDATE_CRM_FIELDS,
        OP_FINALIZE_ACTION,
        OP_FINALIZE_CASE_EVENT,
    }:
        _require_pickup_stage_apply_enabled(settings, apply_enabled_probe)
    if row.operation in {
        OP_PUBLISH_INVENTORY_CLARIFICATION,
        OP_PROCESS_INVENTORY_CLARIFICATION,
        OP_UPDATE_INVENTORY_CLARIFICATION,
    }:
        _require_inventory_enabled(settings, apply_enabled_probe)
    if row.operation in {
        OP_PUBLISH_MISSING_RECEIPT_PROMPT,
        OP_CREATE_MISSING_RECEIPT_TASK,
    }:
        _require_missing_receipt_enabled(settings, missing_receipt_enabled_probe)
    if (
        candidate is not None
        and candidate.source_chat_id == settings.order_fulfillment_pickup_exception_chat_dialog_id
        and row.operation not in {OP_UPDATE_CARD, OP_PUBLISH_CARD, OP_REFRESH_INTERACTIVE_CARD}
    ):
        _require_lost_orders_enabled(settings, apply_enabled_probe)
    feature_guard = fulfillment._clean_string((row.payload or {}).get("feature_guard"))
    if feature_guard == "inventory_won":
        _require_inventory_won_enabled(settings, apply_enabled_probe)
        _require_inventory_won_evidence_current(
            session,
            row=row,
            client=client,
            onec_validator=onec_validator,
            validate_composite=row.operation == OP_UPDATE_CRM_STAGE,
        )
    elif feature_guard == "historical_reconciliation":
        _require_historical_evidence_current(
            session,
            row=row,
            client=client,
            settings=settings,
            onec_validator=onec_validator,
        )
    if row.operation == OP_START_SMS_WORKFLOW:
        _require_sms_enabled(settings, apply_enabled_probe)
    if row.operation == OP_CREATE_TASK:
        task_kind = fulfillment._clean_string((row.payload or {}).get("task_kind"))
        if task_kind in {
            "notify_client",
            "call",
            "hold_call",
            "dismantle_review",
            "dismantle",
        }:
            _require_sla_enabled(settings, apply_enabled_probe)
        elif task_kind == "lost_search":
            _require_lost_orders_enabled(settings, apply_enabled_probe)
        else:
            _require_pickup_stage_apply_enabled(settings, apply_enabled_probe)
    if row.operation == OP_PUBLISH_CARD:
        _publish_card(
            session,
            candidate,
            client=client,
            settings=settings,
            onec_validator=onec_validator,
            now=now,
        )
        return
    if row.operation == OP_REFRESH_INTERACTIVE_CARD:
        _refresh_interactive_card(
            session,
            row=row,
            client=client,
            settings=settings,
            onec_validator=onec_validator,
            now=now,
        )
        return
    if row.operation == OP_FINALIZE_STRUCTURED_ARRIVAL:
        _finalize_structured_arrival(
            session,
            row=row,
            client=client,
            settings=settings,
        )
        return
    if row.operation == OP_PUBLISH_MISSING_RECEIPT_PROMPT:
        _publish_missing_receipt_prompt(
            session,
            row=row,
            client=client,
            settings=settings,
            onec_validator=onec_validator,
            enabled_probe=missing_receipt_enabled_probe,
        )
        return
    if row.operation == OP_CREATE_MISSING_RECEIPT_TASK:
        _create_missing_receipt_task(
            session,
            row=row,
            client=client,
            settings=settings,
            onec_validator=onec_validator,
            enabled_probe=missing_receipt_enabled_probe,
        )
        return
    if row.operation == OP_PUBLISH_INVENTORY_CLARIFICATION:
        _publish_inventory_clarification(
            session,
            row=row,
            client=client,
            settings=settings,
        )
        return
    if row.operation == OP_PROCESS_INVENTORY_CLARIFICATION:
        _process_inventory_clarification(
            session,
            row=row,
            client=client,
            settings=settings,
            now=now,
        )
        return
    if row.operation == OP_UPDATE_INVENTORY_CLARIFICATION:
        _update_inventory_clarification(
            session,
            row=row,
            client=client,
            settings=settings,
        )
        return
    if row.operation == OP_PROCESS_ACTION:
        if candidate is None or action is None:
            raise RuntimeError("action_candidate_missing")
        _process_action(
            session,
            candidate=candidate,
            action=action,
            client=client,
            settings=settings,
            onec_validator=onec_validator,
            apply_enabled_probe=apply_enabled_probe,
            now=now,
        )
        return
    if row.operation == OP_PUBLISH_CONFIRMATION:
        if candidate is None or action is None:
            raise RuntimeError("confirmation_candidate_missing")
        _publish_confirmation(
            session,
            candidate,
            action=action,
            client=client,
            settings=settings,
        )
        return
    if row.operation == OP_UPDATE_CARD:
        if candidate is None:
            raise RuntimeError("card_candidate_missing")
        _update_card(candidate, client=client, settings=settings, payload=row.payload or {})
        return
    if row.operation == OP_UPDATE_CRM_STAGE:
        _apply_crm_stage(
            session,
            row=row,
            client=client,
            settings=settings,
            apply_enabled_probe=apply_enabled_probe,
            now=now,
        )
        return
    if row.operation == OP_UPDATE_CRM_FIELDS:
        _update_crm_fields(
            row=row,
            client=client,
            settings=settings,
            apply_enabled_probe=apply_enabled_probe,
        )
        return
    if row.operation == OP_FINALIZE_ACTION:
        if candidate is None or action is None:
            raise RuntimeError("finalize_action_candidate_missing")
        _finalize_action(
            session,
            candidate=candidate,
            action=action,
            payload=row.payload or {},
            now=now,
        )
        return
    if row.operation == OP_START_SMS_WORKFLOW:
        _start_sms_workflow(
            session,
            row=row,
            client=client,
            settings=settings,
            apply_enabled_probe=apply_enabled_probe,
        )
        return
    if row.operation == OP_VERIFY_SMS_WORKFLOW:
        _verify_sms_workflow(
            session,
            row=row,
            client=client,
            settings=settings,
            now=now,
        )
        return
    if row.operation == OP_CREATE_TASK:
        _create_task(
            session=session,
            row=row,
            client=client,
            settings=settings,
            apply_enabled_probe=apply_enabled_probe,
        )
        return
    if row.operation == OP_FINALIZE_CASE_EVENT:
        _finalize_case_event(session, row=row, now=now)
        return
    raise RuntimeError(f"unsupported_outbox_operation:{row.operation}")


def _publish_missing_receipt_prompt(
    session: Session,
    *,
    row: SiteOrderFulfillmentOutbox,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], OneCPickupValidation],
    enabled_probe: Callable[[], bool] | None,
) -> None:
    raw_ids = list((row.payload or {}).get("movement_event_ids") or [])
    movement_ids = list(
        dict.fromkeys(
            value
            for item in raw_ids[:STRICT_ARRIVAL_ORDER_LIMIT]
            if (value := fulfillment._int_or_none(item)) is not None and value > 0
        )
    )
    if not movement_ids:
        raise RuntimeError("missing_receipt_prompt_payload_invalid")
    snapshots: list[MissingReceiptSnapshot] = []
    suppressed: dict[str, str] = {}
    for movement_id in movement_ids:
        snapshot, reason = _missing_receipt_snapshot(
            session,
            movement_id=movement_id,
            client=client,
            settings=settings,
            onec_validator=onec_validator,
        )
        if snapshot is None:
            suppressed[str(movement_id)] = reason
        else:
            snapshots.append(snapshot)
    if not snapshots:
        _suppress_missing_receipt_row(
            row,
            reason="all_movements_closed_before_prompt",
        )
        row.payload = {**(row.payload or {}), "suppressed_movements": suppressed}
        return
    warehouse_ids = {snapshot.warehouse.id for snapshot in snapshots}
    if len(warehouse_ids) != 1:
        _suppress_missing_receipt_row(row, reason="prompt_warehouse_conflict")
        return
    bot_id = settings.order_fulfillment_bot_id
    if bot_id is None:
        raise RetryableBeforeExternalEffect("bot_id_not_configured")
    warehouse = snapshots[0].warehouse
    order_numbers = list(dict.fromkeys(snapshot.case.site_order_number for snapshot in snapshots))
    orders_text = " ".join(order_numbers)
    message = (
        f"Контроль самовывоза — {warehouse.name}\n"
        "Склад сообщил об отправке более "
        f"{settings.order_fulfillment_pickup_receipt_question_after_hours} часов назад, "
        "но получение точкой не подтверждено:\n"
        f"{orders_text}\n"
        "Если заказы уже получены, ответьте отдельным сообщением:\n"
        f"{warehouse.name}: {orders_text} получили"
    )
    _require_missing_receipt_enabled(settings, enabled_probe)
    message_id = client.add_bot_message(
        dialog_id=settings.order_fulfillment_pickup_ready_chat_dialog_id,
        bot_id=bot_id,
        message=message,
        keyboard=[],
    )
    row.payload = {
        **(row.payload or {}),
        "bot_message_id": message_id,
        "site_order_numbers": order_numbers,
        "warehouse_id": warehouse.id,
        "suppressed_movements": suppressed,
    }


def _create_missing_receipt_task(
    session: Session,
    *,
    row: SiteOrderFulfillmentOutbox,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], OneCPickupValidation],
    enabled_probe: Callable[[], bool] | None,
) -> None:
    movement_id = fulfillment._int_or_none((row.payload or {}).get("movement_event_id"))
    if movement_id is None or movement_id <= 0:
        raise RuntimeError("missing_receipt_task_payload_invalid")
    snapshot, reason = _missing_receipt_snapshot(
        session,
        movement_id=movement_id,
        client=client,
        settings=settings,
        onec_validator=onec_validator,
    )
    if snapshot is None:
        _suppress_missing_receipt_row(row, reason=reason)
        return
    responsible_id, accomplice_ids = _resolve_task_route(
        session,
        case=snapshot.case,
        task_kind="missing_receipt",
        settings=settings,
    )
    try:
        users = [client.get_user_by_id(user_id) for user_id in [responsible_id, *accomplice_ids]]
    except Exception as exc:
        raise RetryableBeforeExternalEffect(str(exc)) from exc
    for user_id, user in zip([responsible_id, *accomplice_ids], users, strict=True):
        if user is None or str(user.get("ACTIVE") or "").upper() not in {
            "Y",
            "TRUE",
            "1",
        }:
            raise RuntimeError(f"task_route_user_inactive:{user_id}")
    order_number = snapshot.case.site_order_number
    _require_missing_receipt_enabled(settings, enabled_probe)
    task_result = client.add_task(
        {
            "TITLE": f"Проверить получение самовывоза №{order_number}",
            "RESPONSIBLE_ID": responsible_id,
            "DESCRIPTION": (
                f"Склад сообщил об отправке заказа №{order_number} на точку "
                f"«{snapshot.warehouse.name}», но получение не подтверждено более "
                f"{settings.order_fulfillment_pickup_receipt_task_after_hours} часов. "
                "Проверьте фактическое наличие и подтвердите получение в рабочем чате."
            ),
            "UF_CRM_TASK": [f"D_{snapshot.deal.deal_id}"],
            **({"ACCOMPLICES": accomplice_ids} if accomplice_ids else {}),
        }
    )
    task_id = _task_result_id(task_result)
    if task_id is None:
        raise RuntimeError("task_api_returned_unrecognized_result")
    row.payload = {
        **(row.payload or {}),
        "task_id": task_id,
        "site_order_number": order_number,
        "warehouse_id": snapshot.warehouse.id,
        "deal_id": snapshot.deal.deal_id,
    }


def _publish_card(
    session: Session,
    candidate: BitrixChatActionCandidate | None,
    *,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], OneCPickupValidation],
    now: datetime,
) -> None:
    if candidate is None:
        raise RuntimeError("candidate_missing")
    if settings.order_fulfillment_bot_id is None:
        raise RetryableBeforeExternalEffect("bot_id_not_configured")
    if not fulfillment._clean_string(settings.order_fulfillment_bot_client_id):
        raise RetryableBeforeExternalEffect("bot_client_id_not_configured")
    if not fulfillment._clean_string(settings.order_fulfillment_bot_callback_secret):
        raise RetryableBeforeExternalEffect("callback_secret_not_configured")
    if not fulfillment._clean_string(settings.order_fulfillment_bot_application_token):
        raise RetryableBeforeExternalEffect("application_token_not_configured")
    if not settings.order_fulfillment_bot_allowed_domains:
        raise RetryableBeforeExternalEffect("allowed_domains_not_configured")
    if not settings.order_fulfillment_bot_allowed_member_ids:
        raise RetryableBeforeExternalEffect("allowed_member_ids_not_configured")
    if settings.order_fulfillment_bot_command_id is None:
        raise RetryableBeforeExternalEffect("bot_command_id_not_configured")
    if not fulfillment._clean_string(settings.order_fulfillment_bot_command):
        raise RetryableBeforeExternalEffect("bot_command_not_configured")
    interaction = fulfillment._clean_string((candidate.payload or {}).get("interaction"))
    if interaction in {"search", "structured_arrival"}:
        actor_id = fulfillment._clean_string(candidate.source_author_id)
        participants = client.list_dialog_user_ids(candidate.source_chat_id)
        excluded = {str(item) for item in settings.order_fulfillment_bot_excluded_user_ids}
        if actor_id not in participants or actor_id in excluded:
            candidate.status = CANDIDATE_REVIEW
            candidate.updated_at = now
            return
        details, status_text = _interactive_card_snapshot(
            session,
            candidate=candidate,
            client=client,
            settings=settings,
            onec_validator=onec_validator,
        )
        candidate.bot_message_id = client.add_bot_message(
            dialog_id=candidate.source_chat_id,
            bot_id=settings.order_fulfillment_bot_id,
            message=card_text(candidate, status_text=status_text, details=details),
            keyboard=card_keyboard(candidate, settings=settings, session=session),
        )
        return
    try:
        deals = client.list_deals_by_site_order(candidate.site_order_number)
    except Exception as exc:
        raise RetryableBeforeExternalEffect(str(exc)) from exc
    status_text: str | None = None
    if candidate.expires_at <= now:
        candidate.status = CANDIDATE_EXPIRED
        status_text = "Срок действия карточки истёк"
    if len(deals) == 1:
        deal = deals[0]
        candidate.bitrix_deal_id = deal.deal_id
        if candidate.status == CANDIDATE_EXPIRED:
            pass
        elif fulfillment._clean_string(deal.stage_id) in TERMINAL_STAGES:
            candidate.status = CANDIDATE_DISMISSED
            candidate.updated_at = now
            return
        elif not fulfillment._is_internal_pickup_deal(deal):
            candidate.status = CANDIDATE_REVIEW
            status_text = "Проверка: сделка не относится к внутреннему самовывозу"
    elif candidate.status != CANDIDATE_EXPIRED and not deals:
        candidate.status = CANDIDATE_REVIEW
        status_text = "Проверка: сделка не найдена"
    elif candidate.status != CANDIDATE_EXPIRED:
        candidate.status = CANDIDATE_REVIEW
        status_text = "Проверка: найдено несколько сделок"
    candidate.bot_message_id = client.add_bot_message(
        dialog_id=candidate.source_chat_id,
        bot_id=settings.order_fulfillment_bot_id,
        message=card_text(candidate, status_text=status_text),
        keyboard=(
            card_keyboard(candidate, settings=settings, session=session)
            if candidate.status == CANDIDATE_OPEN
            else []
        ),
    )


def _interactive_card_snapshot(
    session: Session,
    *,
    candidate: BitrixChatActionCandidate,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], OneCPickupValidation],
) -> tuple[list[str], str | None]:
    interaction = fulfillment._clean_string((candidate.payload or {}).get("interaction"))
    if interaction == "search":
        return _search_card_snapshot(
            session,
            candidate=candidate,
            client=client,
            settings=settings,
            onec_validator=onec_validator,
        )
    return _structured_arrival_snapshot(
        session,
        candidate=candidate,
        client=client,
        settings=settings,
        onec_validator=onec_validator,
    )


def _search_card_snapshot(
    session: Session,
    *,
    candidate: BitrixChatActionCandidate,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], OneCPickupValidation],
) -> tuple[list[str], str | None]:
    try:
        deals = client.list_deals_by_site_order(candidate.site_order_number)
    except Exception as exc:
        raise RetryableBeforeExternalEffect(str(exc)) from exc
    case = session.scalar(
        select(SiteOrderExecutionCase).where(
            SiteOrderExecutionCase.site_order_number == candidate.site_order_number
        )
    )
    point: LogisticsWarehouse | None = None
    if case is not None and case.pickup_point_warehouse_id is not None:
        point = session.get(LogisticsWarehouse, case.pickup_point_warehouse_id)
    candidate.pickup_point_warehouse_id = point.id if point is not None else None
    candidate.pickup_point_name = point.name if point is not None else None
    deal = deals[0] if len(deals) == 1 else None
    candidate.bitrix_deal_id = deal.deal_id if deal is not None else None
    onec = onec_validator(candidate.site_order_number)
    event = (
        session.scalar(
            select(SiteOrderExecutionEvent)
            .where(SiteOrderExecutionEvent.case_id == case.id)
            .order_by(
                SiteOrderExecutionEvent.event_at.desc(),
                SiteOrderExecutionEvent.id.desc(),
            )
        )
        if case is not None
        else None
    )
    inventory = session.execute(
        select(LogisticsWarehouse.name, PickupInventorySubmission.submitted_at)
        .join(
            PickupInventorySubmission,
            PickupInventorySubmission.warehouse_id == LogisticsWarehouse.id,
        )
        .join(
            PickupInventoryItem,
            PickupInventoryItem.submission_id == PickupInventorySubmission.id,
        )
        .where(
            PickupInventoryItem.site_order_number == candidate.site_order_number,
            PickupInventorySubmission.status == pickup_inventory.STATUS_CONFIRMED,
        )
        .order_by(
            PickupInventorySubmission.submitted_at.desc(),
            PickupInventorySubmission.id.desc(),
        )
        .limit(1)
    ).first()
    stage = fulfillment._clean_string(deal.stage_id) if deal is not None else "не найдена"
    sms_value = (
        fulfillment._clean_string(
            (deal.raw or {}).get(settings.order_fulfillment_bot_pickup_sms_field)
        )
        if deal is not None
        else ""
    )
    onec_parts = []
    if not onec.available:
        onec_parts.append("недоступна")
    else:
        onec_parts.extend(
            label
            for label, present in (
                ("собран", onec.assembled),
                ("выдан", onec.issued_confirmed),
                ("возврат", onec.return_confirmed),
                ("оплата", onec.payment_confirmed),
            )
            if present
        )
        if not onec_parts:
            onec_parts.append("подтверждающих фактов нет")
    details = [
        f"CRM: {stage}",
        f"Ожидаемая точка: {point.name if point is not None else 'не определена'}",
        (
            f"Последний факт: {event.event_type}"
            if event is not None
            else "Последний факт: нет подтверждённых событий"
        ),
        f"1С: {', '.join(onec_parts)}",
        f"SMS: {'отправлена' if sms_value else 'не подтверждена'}",
        (
            f"Последний остаток: {inventory[0]}, {inventory[1].isoformat()}"
            if inventory is not None
            else "Последний остаток: заказ не найден в подтверждённых списках"
        ),
    ]
    search_allowed = bool(
        deal is not None
        and fulfillment._is_internal_pickup_deal(deal)
        and fulfillment._clean_string(deal.stage_id) not in TERMINAL_STAGES
        and candidate.source_chat_id == settings.order_fulfillment_pickup_exception_chat_dialog_id
        and settings.order_fulfillment_lost_orders_enabled
    )
    candidate.payload = {**(candidate.payload or {}), "search_allowed": search_allowed}
    if not deals:
        return details, "Вывод: сделка не найдена; CRM не изменялась"
    if len(deals) != 1:
        return details, "Вывод: найдено несколько сделок; нужна ручная проверка"
    if fulfillment._clean_string(deal.stage_id) in TERMINAL_STAGES:
        return details, "Вывод: сделка закрыта; доступен только просмотр"
    if not fulfillment._is_internal_pickup_deal(deal):
        return details, "Вывод: сделка не относится к внутреннему самовывозу"
    if not onec.available:
        return details, "Вывод: 1С недоступна; действия заблокированы"
    return details, "Вывод: данные обновлены, CRM не изменялась"


def _structured_arrival_snapshot(
    session: Session,
    *,
    candidate: BitrixChatActionCandidate,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], OneCPickupValidation],
) -> tuple[list[str], str | None]:
    order_numbers = list((candidate.payload or {}).get("order_numbers") or [])
    expected_ids: set[int] = set()
    problems: list[str] = []
    for order_number in order_numbers:
        try:
            deals = client.list_deals_by_site_order(order_number)
        except Exception as exc:
            raise RetryableBeforeExternalEffect(str(exc)) from exc
        if len(deals) != 1:
            problems.append(f"№{order_number}: сделка не уникальна")
            continue
        deal = deals[0]
        if not fulfillment._is_internal_pickup_deal(deal):
            problems.append(f"№{order_number}: не внутренний самовывоз")
        if fulfillment._clean_string(deal.stage_id) in TERMINAL_STAGES:
            problems.append(f"№{order_number}: сделка закрыта")
        onec = onec_validator(order_number)
        if not onec.available:
            problems.append(f"№{order_number}: 1С недоступна")
        elif not onec.assembled:
            problems.append(f"№{order_number}: сборка в 1С не подтверждена")
        case = session.scalar(
            select(SiteOrderExecutionCase).where(
                SiteOrderExecutionCase.site_order_number == order_number
            )
        )
        if case is not None and case.pickup_point_warehouse_id is not None:
            expected_ids.add(case.pickup_point_warehouse_id)
        else:
            resolution = pickup_inventory.resolve_pickup_inventory_warehouse(
                session,
                " ".join(value for value in (deal.delivery, deal.post_delivery_type) if value),
                pickup_aliases=settings.order_fulfillment_pickup_warehouse_aliases,
            )
            if resolution.warehouse is not None:
                expected_ids.add(resolution.warehouse.id)
    if len(expected_ids) == 1:
        point = session.get(LogisticsWarehouse, next(iter(expected_ids)))
        if point is not None:
            candidate.pickup_point_warehouse_id = point.id
            candidate.pickup_point_name = point.name
    elif len(expected_ids) > 1:
        problems.append("заказы ожидаются в разных точках; разделите команды")
    candidate.payload = {**(candidate.payload or {}), "arrival_blocked": bool(problems)}
    details = [f"Проверено заказов: {len(order_numbers)}"]
    if problems:
        details.extend(f"Проверка: {problem}" for problem in problems[:8])
        return details, "Поступление не зафиксировано"
    if candidate.pickup_point_name:
        return details, "Проверьте предложенную точку и подтвердите поступление"
    return details, "Выберите точку; автор сообщения не используется для определения"


def _refresh_interactive_card(
    session: Session,
    *,
    row: SiteOrderFulfillmentOutbox,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], OneCPickupValidation],
    now: datetime,
) -> None:
    candidate = _outbox_candidate(session, row)
    if (
        candidate is None
        or not candidate.bot_message_id
        or settings.order_fulfillment_bot_id is None
    ):
        raise RuntimeError("interactive_card_missing")
    actor_id = fulfillment._clean_string((row.payload or {}).get("actor_id"))
    participants = client.list_dialog_user_ids(candidate.source_chat_id)
    excluded = {str(item) for item in settings.order_fulfillment_bot_excluded_user_ids}
    if actor_id not in participants or actor_id in excluded:
        raise BotSecurityError("actor_not_active_chat_participant")
    status_text = fulfillment._clean_string((row.payload or {}).get("status_text")) or None
    details, snapshot_status = _interactive_card_snapshot(
        session,
        candidate=candidate,
        client=client,
        settings=settings,
        onec_validator=onec_validator,
    )
    client.update_bot_message(
        message_id=candidate.bot_message_id,
        bot_id=settings.order_fulfillment_bot_id,
        message=card_text(
            candidate,
            status_text=status_text or snapshot_status,
            details=details,
        ),
        keyboard=(
            []
            if candidate.status == CANDIDATE_DISMISSED
            else card_keyboard(candidate, settings=settings, session=session)
        ),
    )


def _finalize_structured_arrival(
    session: Session,
    *,
    row: SiteOrderFulfillmentOutbox,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
) -> None:
    leader = _outbox_candidate(session, row)
    if leader is None or not leader.bot_message_id or settings.order_fulfillment_bot_id is None:
        raise RuntimeError("structured_arrival_card_missing")
    action_ids = [int(value) for value in (row.payload or {}).get("action_ids") or []]
    actions = session.scalars(
        select(BitrixChatAction)
        .where(BitrixChatAction.id.in_(action_ids))
        .order_by(BitrixChatAction.id.asc())
    ).all()
    if len(actions) != len(action_ids):
        raise RuntimeError("structured_arrival_actions_missing")
    pending = {"queued", "processing", "awaiting_confirmation"}
    if any(action.status in pending for action in actions):
        raise RetryableBeforeExternalEffect("structured_arrival_still_processing")
    successful = sum(action.status in {"accepted", "dry_run"} for action in actions)
    review = len(actions) - successful
    status = (
        f"Готово: {successful}; нужна проверка: {review}" if review else f"Готово: {successful}"
    )
    leader.status = (
        CANDIDATE_REVIEW
        if review
        else (
            CANDIDATE_DRY_RUN
            if any(action.status == "dry_run" for action in actions)
            else CANDIDATE_APPLIED
        )
    )
    client.update_bot_message(
        message_id=leader.bot_message_id,
        bot_id=settings.order_fulfillment_bot_id,
        message=card_text(leader, status_text=status),
        keyboard=[],
    )


def _publish_inventory_clarification(
    session: Session,
    *,
    row: SiteOrderFulfillmentOutbox,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
) -> None:
    submission = _outbox_inventory_submission(session, row)
    if submission is None or submission.source_message is None:
        raise RuntimeError("inventory_submission_missing")
    _require_bot_card_configuration(settings)
    state = _inventory_clarification_state(submission)
    existing_bot_message_id = fulfillment._clean_string(state.get("bot_message_id"))
    if existing_bot_message_id:
        return
    bot_message_id = client.add_bot_message(
        dialog_id=submission.source_message.dialog_id,
        bot_id=int(settings.order_fulfillment_bot_id or 0),
        message=_inventory_clarification_text(submission),
        keyboard=_inventory_clarification_keyboard(
            session,
            submission=submission,
            settings=settings,
        ),
    )
    _set_inventory_clarification_state(
        submission,
        {**state, "bot_message_id": bot_message_id},
    )


def _process_inventory_clarification(
    session: Session,
    *,
    row: SiteOrderFulfillmentOutbox,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    now: datetime,
) -> None:
    submission = _outbox_inventory_submission(session, row)
    if submission is None or submission.source_message is None:
        raise RuntimeError("inventory_submission_missing")
    state = _inventory_clarification_state(submission)
    payload = row.payload or {}
    if submission.status != pickup_inventory.STATUS_MANUAL_REVIEW or state.get("status") != "open":
        return
    if payload.get("nonce") != state.get("nonce"):
        raise RuntimeError("inventory_clarification_nonce_changed")
    if state.get("source_text_hash") != submission.source_message.raw_text_hash:
        submission.status = pickup_inventory.STATUS_MANUAL_REVIEW
        _set_inventory_clarification_state(
            submission,
            {**state, "status": "conflict", "reason": "source_message_edited"},
        )
        _enqueue_inventory_card_update(
            session,
            submission=submission,
            depends_on=row,
            status_text="Сообщение изменено — отправьте исправленный список заново",
            now=now,
        )
        return
    actor_id = fulfillment._clean_string(payload.get("actor_id"))
    participants = client.list_dialog_user_ids(submission.source_message.dialog_id)
    excluded = {str(value) for value in settings.order_fulfillment_bot_excluded_user_ids}
    if not actor_id or actor_id not in participants or actor_id in excluded:
        raise RuntimeError("actor_not_active_chat_participant")
    action = fulfillment._clean_string(payload.get("action"))
    if action == INVENTORY_ACTION_ERROR:
        submission.status = "dismissed"
        _set_inventory_clarification_state(
            submission,
            {**state, "status": "dismissed", "actor_id": actor_id},
        )
        _enqueue_inventory_card_update(
            session,
            submission=submission,
            depends_on=row,
            status_text="Сообщение отмечено как ошибочное; состояние точки не изменено",
            now=now,
        )
        return
    if action == INVENTORY_ACTION_SELECT_POINT:
        external_id = fulfillment._clean_string(payload.get("warehouse_external_id"))
        warehouse = session.scalar(
            select(LogisticsWarehouse).where(
                LogisticsWarehouse.external_id == external_id,
                LogisticsWarehouse.is_active.is_(True),
            )
        )
        if warehouse is None:
            raise RuntimeError("inventory_warehouse_not_available")
        selected = pickup_inventory.create_point_selected_submission(
            session,
            submission=submission,
            warehouse_id=warehouse.id,
            actor_id=actor_id,
            now=now,
        )
        _set_inventory_clarification_state(
            submission,
            {**state, "status": "superseded", "actor_id": actor_id},
        )
        _set_inventory_clarification_state(
            selected,
            {
                "nonce": secrets.token_hex(16),
                "expires_at": state.get("expires_at"),
                "status": "open",
                "source_text_hash": submission.source_message.raw_text_hash,
                "bot_message_id": state.get("bot_message_id"),
            },
        )
        _enqueue_inventory_card_update(
            session,
            submission=selected,
            depends_on=row,
            status_text="Точка выбрана; теперь уточните смысл сообщения",
            now=now,
        )
        return
    mode_by_action = {
        INVENTORY_ACTION_FULL: pickup_inventory.MODE_FULL,
        INVENTORY_ACTION_CARRY: pickup_inventory.MODE_CARRY,
        INVENTORY_ACTION_ZERO: pickup_inventory.MODE_ZERO,
    }
    mode = mode_by_action.get(action)
    if mode is None or submission.warehouse_id is None:
        raise RuntimeError("inventory_clarification_action_invalid")
    try:
        confirmed = pickup_inventory.create_clarified_submission(
            session,
            submission=submission,
            warehouse_id=submission.warehouse_id,
            mode=mode,
            actor_id=actor_id,
            now=now,
        )
    except ValueError as exc:
        reason = str(exc)
        status_text = (
            "Номер заказа неоднозначен — отправьте исправленный полный список"
            if reason == "inventory_order_numbers_ambiguous"
            else "Нет предыдущего подтверждённого списка этой точки"
        )
        _enqueue_inventory_card_update(
            session,
            submission=submission,
            depends_on=row,
            status_text=status_text,
            now=now,
        )
        return
    _set_inventory_clarification_state(
        submission,
        {**state, "status": "superseded", "actor_id": actor_id},
    )
    _set_inventory_clarification_state(
        confirmed,
        {
            "status": "completed",
            "actor_id": actor_id,
            "bot_message_id": state.get("bot_message_id"),
        },
    )
    _enqueue_inventory_card_update(
        session,
        submission=confirmed,
        depends_on=row,
        status_text="Состояние точки подтверждено",
        now=now,
    )


def _update_inventory_clarification(
    session: Session,
    *,
    row: SiteOrderFulfillmentOutbox,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
) -> None:
    submission = _outbox_inventory_submission(session, row)
    if submission is None:
        raise RuntimeError("inventory_submission_missing")
    _require_bot_card_configuration(settings)
    state = _inventory_clarification_state(submission)
    bot_message_id = fulfillment._clean_string(state.get("bot_message_id"))
    if not bot_message_id:
        raise RuntimeError("inventory_bot_message_missing")
    is_open = (
        submission.status == pickup_inventory.STATUS_MANUAL_REVIEW and state.get("status") == "open"
    )
    client.update_bot_message(
        message_id=bot_message_id,
        bot_id=int(settings.order_fulfillment_bot_id or 0),
        message=_inventory_clarification_text(
            submission,
            status_text=fulfillment._clean_string((row.payload or {}).get("status_text")),
        ),
        keyboard=(
            _inventory_clarification_keyboard(
                session,
                submission=submission,
                settings=settings,
            )
            if is_open
            else []
        ),
    )


def _inventory_clarification_text(
    submission: PickupInventorySubmission,
    *,
    status_text: str | None = None,
) -> str:
    warehouse = submission.warehouse.name if submission.warehouse is not None else "не определена"
    lines = [
        "Уточнение инвентаризации",
        f"Точка: {warehouse}",
        f"Распознано заказов: {len(submission.items)}",
    ]
    if status_text:
        lines.append(status_text)
    elif submission.warehouse_id is None:
        lines.append("Сначала выберите точку")
    else:
        lines.append("Выберите смысл сообщения")
    return "\n".join(lines)


def _inventory_clarification_keyboard(
    session: Session,
    *,
    submission: PickupInventorySubmission,
    settings: Settings,
) -> list[dict[str, Any]]:
    secret = settings.order_fulfillment_bot_callback_secret
    if not secret:
        return []
    buttons: list[dict[str, Any]] = []
    if submission.warehouse_id is None:
        warehouse_filters = [
            LogisticsWarehouse.is_active.is_(True),
            LogisticsWarehouse.kind.in_(["store", "retail"]),
        ]
        if settings.order_fulfillment_pickup_warehouse_external_ids:
            warehouse_filters.append(
                LogisticsWarehouse.external_id.in_(
                    settings.order_fulfillment_pickup_warehouse_external_ids
                )
            )
        warehouses = session.scalars(
            select(LogisticsWarehouse)
            .where(*warehouse_filters)
            .order_by(LogisticsWarehouse.name.asc(), LogisticsWarehouse.id.asc())
        ).all()
        buttons.extend(
            {
                "TEXT": warehouse.name,
                "COMMAND": settings.order_fulfillment_bot_command,
                "COMMAND_PARAMS": sign_inventory_callback_token(
                    submission,
                    action=INVENTORY_ACTION_SELECT_POINT,
                    secret=secret,
                    warehouse_external_id=warehouse.external_id,
                ),
                "BG_COLOR": "#2F80ED",
                "BLOCK": "Y",
            }
            for warehouse in warehouses
        )
    else:
        for action, label, color in (
            (INVENTORY_ACTION_FULL, "Полный список", "#2FC6F6"),
            (INVENTORY_ACTION_CARRY, "Всё актуально", "#2F80ED"),
            (INVENTORY_ACTION_ZERO, "Нулевой остаток", "#9DCF00"),
        ):
            buttons.append(
                {
                    "TEXT": label,
                    "COMMAND": settings.order_fulfillment_bot_command,
                    "COMMAND_PARAMS": sign_inventory_callback_token(
                        submission,
                        action=action,
                        secret=secret,
                    ),
                    "BG_COLOR": color,
                    "BLOCK": "Y",
                }
            )
    buttons.append(
        {
            "TEXT": "Ошибка",
            "COMMAND": settings.order_fulfillment_bot_command,
            "COMMAND_PARAMS": sign_inventory_callback_token(
                submission,
                action=INVENTORY_ACTION_ERROR,
                secret=secret,
            ),
            "BG_COLOR": "#A6A6A6",
            "BLOCK": "Y",
        }
    )
    return buttons


def _require_bot_card_configuration(settings: Settings) -> None:
    if settings.order_fulfillment_bot_id is None:
        raise RetryableBeforeExternalEffect("bot_id_not_configured")
    if not fulfillment._clean_string(settings.order_fulfillment_bot_client_id):
        raise RetryableBeforeExternalEffect("bot_client_id_not_configured")
    if not fulfillment._clean_string(settings.order_fulfillment_bot_callback_secret):
        raise RetryableBeforeExternalEffect("callback_secret_not_configured")
    if settings.order_fulfillment_bot_command_id is None:
        raise RetryableBeforeExternalEffect("bot_command_id_not_configured")
    if not fulfillment._clean_string(settings.order_fulfillment_bot_command):
        raise RetryableBeforeExternalEffect("bot_command_not_configured")
    if not fulfillment._clean_string(settings.order_fulfillment_bot_application_token):
        raise RetryableBeforeExternalEffect("application_token_not_configured")
    if not settings.order_fulfillment_bot_allowed_domains:
        raise RetryableBeforeExternalEffect("allowed_domains_not_configured")
    if not settings.order_fulfillment_bot_allowed_member_ids:
        raise RetryableBeforeExternalEffect("allowed_member_ids_not_configured")


def _outbox_inventory_submission(
    session: Session,
    row: SiteOrderFulfillmentOutbox,
) -> PickupInventorySubmission | None:
    if row.target_type != "inventory_submission":
        return None
    submission_id = fulfillment._int_or_none(row.target_id)
    return session.get(PickupInventorySubmission, submission_id) if submission_id else None


def _inventory_clarification_state(
    submission: PickupInventorySubmission,
) -> dict[str, Any]:
    value = (submission.payload or {}).get("clarification")
    return dict(value) if isinstance(value, dict) else {}


def _set_inventory_clarification_state(
    submission: PickupInventorySubmission,
    state: dict[str, Any],
) -> None:
    submission.payload = {**(submission.payload or {}), "clarification": state}


def _enqueue_inventory_card_update(
    session: Session,
    *,
    submission: PickupInventorySubmission,
    depends_on: SiteOrderFulfillmentOutbox,
    status_text: str,
    now: datetime,
) -> None:
    enqueue_outbox(
        session,
        depends_on=depends_on,
        operation=OP_UPDATE_INVENTORY_CLARIFICATION,
        idempotency_key=f"{depends_on.idempotency_key}:card-update:{submission.id}",
        target_type="inventory_submission",
        target_id=str(submission.id),
        payload={"status_text": status_text},
        now=now,
    )


def _process_action(
    session: Session,
    *,
    candidate: BitrixChatActionCandidate,
    action: BitrixChatAction,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], OneCPickupValidation],
    apply_enabled_probe: Callable[[], bool] | None = None,
    now: datetime,
) -> None:
    if candidate.active_action != action.action or candidate.active_actor_id != action.actor_id:
        action.status = "rejected"
        action.reason = "stale_action_claim"
        return
    if (
        candidate.raw_message is not None
        and candidate.raw_message.parse_status == "edited_manual_review"
    ):
        action.status = "manual_review"
        action.reason = "source_message_edited"
        candidate.status = CANDIDATE_REVIEW
        candidate.updated_at = now
        _reject_pending_confirmation(
            session,
            candidate=candidate,
            reason=action.reason,
        )
        _queue_card_update(
            session,
            candidate,
            action,
            "Сообщение изменено — нужна ручная проверка",
            now,
        )
        return
    if candidate.expires_at <= now:
        candidate.status = CANDIDATE_EXPIRED
        candidate.updated_at = now
        action.status = "rejected"
        action.reason = "callback_expired"
        _queue_card_update(session, candidate, action, "Срок действия кнопок истёк", now)
        return
    _acquire_order_action_lock(session, candidate.site_order_number)
    active_owner_id = session.scalar(
        select(BitrixChatActionCandidate.id)
        .where(
            BitrixChatActionCandidate.site_order_number == candidate.site_order_number,
            BitrixChatActionCandidate.status.in_([CANDIDATE_QUEUED, CANDIDATE_CONFIRMATION]),
        )
        .order_by(
            BitrixChatActionCandidate.action_claimed_at.asc().nulls_last(),
            BitrixChatActionCandidate.id.asc(),
        )
        .limit(1)
    )
    if active_owner_id != candidate.id:
        candidate.status = CANDIDATE_REVIEW
        candidate.updated_at = now
        action.status = "manual_review"
        action.reason = "order_action_already_in_progress"
        _reject_pending_confirmation(
            session,
            candidate=candidate,
            reason=action.reason,
        )
        _queue_card_update(
            session,
            candidate,
            action,
            "Нужна ручная проверка: по заказу уже выполняется другое действие",
            now,
        )
        return
    participant_ids = client.list_dialog_user_ids(candidate.source_chat_id)
    excluded = {str(item) for item in settings.order_fulfillment_bot_excluded_user_ids}
    if action.actor_id not in participant_ids or action.actor_id in excluded:
        candidate.status = CANDIDATE_REVIEW
        candidate.updated_at = now
        action.status = "rejected"
        action.reason = "actor_not_active_chat_participant"
        _reject_pending_confirmation(
            session,
            candidate=candidate,
            reason=action.reason,
        )
        _queue_card_update(session, candidate, action, "Действие отклонено: нет доступа", now)
        return
    if action.action == ACTION_CANCEL:
        pending_confirmation = session.scalar(
            select(BitrixChatAction)
            .where(
                BitrixChatAction.candidate_id == candidate.id,
                BitrixChatAction.confirmation_step == 1,
                BitrixChatAction.status == "awaiting_confirmation",
            )
            .order_by(BitrixChatAction.id.desc())
        )
        if pending_confirmation is not None:
            pending_confirmation.status = "cancelled"
            pending_confirmation.reason = "confirmation_cancelled"
        candidate.status = CANDIDATE_DISMISSED
        candidate.updated_at = now
        action.status = "completed"
        action.reason = "candidate_dismissed"
        _queue_card_update(session, candidate, action, "Карточка отменена", now)
        return
    if action.action in DANGEROUS_ACTIONS and action.confirmation_step == 1:
        candidate.status = CANDIDATE_CONFIRMATION
        candidate.updated_at = now
        action.status = "awaiting_confirmation"
        action.reason = "second_confirmation_required"
        enqueue_outbox(
            session,
            candidate=candidate,
            action=action,
            operation=OP_PUBLISH_CONFIRMATION,
            idempotency_key=f"action:{action.id}:confirmation",
            payload={},
            now=now,
        )
        return
    if action.confirmation_step == 2:
        first = session.scalar(
            select(BitrixChatAction)
            .where(
                BitrixChatAction.candidate_id == candidate.id,
                BitrixChatAction.action == action.action,
                BitrixChatAction.confirmation_step == 1,
                BitrixChatAction.status == "awaiting_confirmation",
            )
            .order_by(BitrixChatAction.id.desc())
        )
        if first is None or first.actor_id != action.actor_id:
            action.status = "rejected"
            action.reason = "confirmation_actor_mismatch"
            candidate.status = CANDIDATE_REVIEW
            candidate.updated_at = now
            _reject_pending_confirmation(
                session,
                candidate=candidate,
                reason=action.reason,
            )
            _queue_card_update(session, candidate, action, "Подтверждение отклонено", now)
            return
        first.status = "confirmed"
        first.reason = "second_confirmation_received"

    deals = client.list_deals_by_site_order(candidate.site_order_number)
    if len(deals) != 1:
        action.status = "rejected"
        action.reason = "deal_not_unique"
        candidate.status = CANDIDATE_REVIEW
        candidate.updated_at = now
        _queue_card_update(session, candidate, action, "Нужна ручная проверка сделки", now)
        return
    deal = deals[0]
    if candidate.bitrix_deal_id is not None and candidate.bitrix_deal_id != deal.deal_id:
        action.status = "rejected"
        action.reason = "deal_changed"
        candidate.status = CANDIDATE_REVIEW
        candidate.updated_at = now
        _queue_card_update(session, candidate, action, "Сделка изменилась — нужна проверка", now)
        return
    candidate.bitrix_deal_id = deal.deal_id
    case = session.scalar(
        select(SiteOrderExecutionCase).where(
            SiteOrderExecutionCase.site_order_number == candidate.site_order_number
        )
    )
    if action.action == ACTION_FOUND_OTHER:
        target_warehouse_id = _action_target_warehouse_id(action)
        warehouse = _selectable_lost_order_warehouse(
            session,
            warehouse_id=target_warehouse_id,
            settings=settings,
        )
        if warehouse is None:
            action.status = "manual_review"
            action.reason = "lost_order_target_invalid"
            candidate.status = CANDIDATE_REVIEW
            candidate.updated_at = now
            _queue_card_update(
                session,
                candidate,
                action,
                "Нужна ручная проверка: выбранная точка недоступна",
                now,
            )
            return
        candidate.payload = {
            **(candidate.payload or {}),
            "previous_pickup_point_warehouse_id": (
                case.pickup_point_warehouse_id if case is not None else None
            ),
            "target_pickup_point_warehouse_id": warehouse.id,
        }
        candidate.pickup_point_warehouse_id = warehouse.id
        candidate.pickup_point_name = warehouse.name
    decision = decide_pickup_action(
        action=action.action,
        confirmation_step=action.confirmation_step,
        deal=deal,
        candidate=candidate,
        case=case,
        onec=onec_validator(candidate.site_order_number),
        settings=settings,
        now=now,
    )
    action.before_stage = deal.stage_id
    action.after_stage = decision.target_stage
    action.reason = decision.reason
    if not decision.allowed:
        action.status = "manual_review"
        candidate.status = CANDIDATE_REVIEW
        candidate.updated_at = now
        _queue_card_update(
            session,
            candidate,
            action,
            f"Нужна ручная проверка: {_decision_reason_text(decision.reason)}",
            now,
        )
        return
    if (
        candidate.dry_run
        or not settings.order_fulfillment_pickup_stage_apply_enabled
        or not _runtime_apply_enabled(settings, apply_enabled_probe)
    ):
        action.status = "dry_run"
        candidate.status = CANDIDATE_DRY_RUN
        candidate.updated_at = now
        _queue_card_update(
            session,
            candidate,
            action,
            f"Тест пройден: {ACTION_LABELS[action.action]}; изменений нет",
            now,
        )
        return

    case = _ensure_case(session, candidate=candidate, deal=deal)
    action.status = "processing"
    candidate.status = CANDIDATE_QUEUED
    candidate.updated_at = now

    event_at = candidate.source_event_at or now
    dependency: SiteOrderFulfillmentOutbox | None = None
    if decision.target_stage and decision.target_stage != fulfillment._clean_string(deal.stage_id):
        dependency = enqueue_outbox(
            session,
            candidate=candidate,
            action=action,
            operation=OP_UPDATE_CRM_STAGE,
            idempotency_key=f"action:{action.id}:crm:{decision.target_stage}",
            target_type="deal",
            target_id=str(deal.deal_id),
            payload={
                "site_order_number": candidate.site_order_number,
                "before_stage": deal.stage_id,
                "target_stage": decision.target_stage,
            },
            now=now,
        )
    dependency = enqueue_outbox(
        session,
        candidate=candidate,
        action=action,
        depends_on=dependency,
        operation=OP_FINALIZE_ACTION,
        idempotency_key=f"action:{action.id}:finalize",
        payload={
            "event_type": decision.event_type,
            "event_at": event_at.isoformat(),
            "current_crm_stage": decision.target_stage or deal.stage_id,
            "warehouse_id": _decision_warehouse_id(
                candidate=candidate,
                case=case,
                action=action.action,
            ),
            "dismantle_after_hours": settings.order_fulfillment_bot_dismantle_after_hours,
        },
        now=now,
    )
    crm_fields: dict[str, Any] = {
        CRM_PICKUP_DERIVED_STATUS_FIELD: decision.event_type or "",
        CRM_PICKUP_LAST_EVIDENCE_FIELD: decision.reason,
    }
    if action.action == ACTION_ARRIVED:
        crm_fields[CRM_PICKUP_STORAGE_STARTED_FIELD] = crm_datetime_iso(event_at)
    dependency = enqueue_outbox(
        session,
        candidate=candidate,
        action=action,
        depends_on=dependency,
        operation=OP_UPDATE_CRM_FIELDS,
        idempotency_key=f"action:{action.id}:crm-fields:{decision.event_type or 'none'}",
        target_type="deal",
        target_id=str(deal.deal_id),
        payload={
            "site_order_number": candidate.site_order_number,
            "fields": crm_fields,
        },
        now=now,
    )
    if (
        decision.send_sms
        and settings.order_fulfillment_bot_sms_enabled
        and _sms_candidate_is_new(candidate, settings=settings)
    ):
        sms_start = enqueue_outbox(
            session,
            candidate=candidate,
            action=action,
            depends_on=dependency,
            operation=OP_START_SMS_WORKFLOW,
            idempotency_key=f"deal:{deal.deal_id}:pickup-ready-sms",
            target_type="deal",
            target_id=str(deal.deal_id),
            payload={"site_order_number": candidate.site_order_number},
            now=now,
        )
        dependency = enqueue_outbox(
            session,
            candidate=candidate,
            action=action,
            depends_on=sms_start,
            operation=OP_VERIFY_SMS_WORKFLOW,
            idempotency_key=f"deal:{deal.deal_id}:pickup-ready-sms-verify",
            target_type="deal",
            target_id=str(deal.deal_id),
            payload={"site_order_number": candidate.site_order_number},
            available_at=now + timedelta(minutes=2),
            now=now,
        )
    if decision.create_task:
        dependency = enqueue_outbox(
            session,
            candidate=candidate,
            action=action,
            depends_on=dependency,
            operation=OP_CREATE_TASK,
            idempotency_key=f"case:{case.id}:task:{decision.create_task}",
            target_type="deal",
            target_id=str(deal.deal_id),
            payload={
                "task_kind": decision.create_task,
                "site_order_number": candidate.site_order_number,
                "expected_stage": decision.target_stage or deal.stage_id,
            },
            now=now,
        )
    _queue_card_update(
        session,
        candidate,
        action,
        "Действие выполнено",
        now,
        depends_on=dependency,
    )


def _publish_confirmation(
    session: Session,
    candidate: BitrixChatActionCandidate,
    *,
    action: BitrixChatAction,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
) -> None:
    if not candidate.bot_message_id or settings.order_fulfillment_bot_id is None:
        raise RuntimeError("bot_message_not_configured")
    if (
        candidate.status != CANDIDATE_CONFIRMATION
        or candidate.active_action != action.action
        or candidate.active_actor_id != action.actor_id
    ):
        return
    secret = settings.order_fulfillment_bot_callback_secret
    if not secret:
        raise RuntimeError("callback_secret_not_configured")
    if action.action == ACTION_FOUND_OTHER:
        warehouses = _selectable_lost_order_warehouses(session, settings=settings)
        if not warehouses:
            raise RuntimeError("lost_order_warehouse_routes_missing")
        case = session.scalar(
            select(SiteOrderExecutionCase).where(
                SiteOrderExecutionCase.site_order_number == candidate.site_order_number
            )
        )
        current_warehouse_id = case.pickup_point_warehouse_id if case is not None else None
        keyboard = [
            {
                "TEXT": warehouse.name,
                "COMMAND": settings.order_fulfillment_bot_command,
                "COMMAND_PARAMS": sign_callback_token(
                    candidate,
                    action=action.action,
                    step=2,
                    secret=secret,
                    target_warehouse_id=warehouse.id,
                ),
                "BG_COLOR": "#2F80ED",
                "BLOCK": "Y",
            }
            for warehouse in warehouses
            if warehouse.id != current_warehouse_id
        ]
        if not keyboard:
            raise RuntimeError("lost_order_alternative_warehouse_missing")
        status_text = "Выберите точку, на которой найден заказ"
    else:
        keyboard = [
            {
                "TEXT": f"Подтвердить: {ACTION_LABELS[action.action]}",
                "COMMAND": settings.order_fulfillment_bot_command,
                "COMMAND_PARAMS": sign_callback_token(
                    candidate,
                    action=action.action,
                    step=2,
                    secret=secret,
                ),
                "BG_COLOR": "#E74C3C",
                "BLOCK": "Y",
            }
        ]
        status_text = "Требуется второе подтверждение"
    keyboard.append(
        {
            "TEXT": ACTION_LABELS[ACTION_CANCEL],
            "COMMAND": settings.order_fulfillment_bot_command,
            "COMMAND_PARAMS": sign_callback_token(
                candidate,
                action=ACTION_CANCEL,
                step=1,
                secret=secret,
            ),
            "BG_COLOR": "#A6A6A6",
            "BLOCK": "Y",
        }
    )
    client.update_bot_message(
        message_id=candidate.bot_message_id,
        bot_id=settings.order_fulfillment_bot_id,
        message=card_text(candidate, status_text=status_text),
        keyboard=keyboard,
    )


def _update_card(
    candidate: BitrixChatActionCandidate,
    *,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    payload: dict[str, Any],
) -> None:
    if not candidate.bot_message_id and (candidate.payload or {}).get("automatic_arrival"):
        if candidate.status == CANDIDATE_QUEUED:
            candidate.status = CANDIDATE_APPLIED
            candidate.updated_at = utcnow()
        return
    if not candidate.bot_message_id or settings.order_fulfillment_bot_id is None:
        raise RuntimeError("bot_message_not_configured")
    client.update_bot_message(
        message_id=candidate.bot_message_id,
        bot_id=settings.order_fulfillment_bot_id,
        message=card_text(candidate, status_text=str(payload.get("status_text") or "Готово")),
        keyboard=[],
    )
    if candidate.status == CANDIDATE_QUEUED:
        candidate.status = CANDIDATE_APPLIED
        candidate.updated_at = utcnow()


def _finalize_action(
    session: Session,
    *,
    candidate: BitrixChatActionCandidate,
    action: BitrixChatAction,
    payload: dict[str, Any],
    now: datetime,
) -> None:
    event_at = _parse_naive_datetime(payload.get("event_at")) or now
    event_type = fulfillment._clean_string(payload.get("event_type"))
    automatic = bool((action.payload or {}).get("automatic"))
    if event_type:
        fulfillment.upsert_execution_event(
            session,
            site_order_number=candidate.site_order_number,
            event_type=event_type,
            event_at=event_at,
            source=("bitrix_chat" if automatic else "manual"),
            source_ref=(
                f"pickup_evidence:{candidate.raw_message_id}"
                if automatic
                else f"pickup_bot_action:{action.id}"
            ),
            confidence="strong",
            raw_message_id=candidate.raw_message_id,
            warehouse_id=fulfillment._int_or_none(payload.get("warehouse_id")),
            actor_ref=action.actor_id,
            payload={
                "candidate_id": candidate.id,
                "actor_id": action.actor_id,
                "automatic": automatic,
                "previous_warehouse_id": (candidate.payload or {}).get(
                    "previous_pickup_point_warehouse_id"
                ),
            },
        )
    case = session.scalar(
        select(SiteOrderExecutionCase).where(
            SiteOrderExecutionCase.site_order_number == candidate.site_order_number
        )
    )
    if case is None:
        raise RuntimeError("execution_case_missing")
    current_crm_stage = fulfillment._clean_string(payload.get("current_crm_stage"))
    if current_crm_stage:
        case.current_crm_stage = current_crm_stage
    if action.action == ACTION_ARRIVED:
        if case.storage_started_at is None:
            case.storage_started_at = event_at
        if candidate.pickup_point_warehouse_id is not None:
            case.pickup_point_warehouse_id = candidate.pickup_point_warehouse_id
    elif action.action == ACTION_MOVING:
        if candidate.pickup_point_warehouse_id is not None:
            case.pickup_point_warehouse_id = candidate.pickup_point_warehouse_id
    elif action.action == ACTION_FOUND_EXPECTED:
        case.current_derived_status = fulfillment.EVENT_PICKUP_STORED
        case.confidence = "strong"
    elif action.action == ACTION_FOUND_OTHER:
        if candidate.pickup_point_warehouse_id is not None:
            case.pickup_point_warehouse_id = candidate.pickup_point_warehouse_id
        case.current_derived_status = fulfillment.EVENT_PICKUP_REDIRECTED
        case.confidence = "strong"
    elif action.action == ACTION_ISSUED:
        case.delivered_at = event_at
    elif action.action == ACTION_RETURNED:
        case.cancelled_at = event_at
    elif action.action == ACTION_NOT_FOUND:
        case.current_derived_status = "manual_review"
        case.confidence = "weak"
    case.updated_at = now
    action.status = "accepted"


def _apply_crm_stage(
    session: Session,
    *,
    row: SiteOrderFulfillmentOutbox,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    apply_enabled_probe: Callable[[], bool] | None = None,
    now: datetime,
) -> None:
    payload = row.payload or {}
    deal_id = int(row.target_id or 0)
    target_stage = fulfillment._clean_string(payload.get("target_stage"))
    if deal_id <= 0 or not target_stage:
        raise RuntimeError("invalid_crm_stage_outbox_payload")
    try:
        live = client.get_deal_by_id(deal_id)
    except Exception as exc:
        raise RetryableBeforeExternalEffect(str(exc)) from exc
    if live is None:
        raise RuntimeError("deal_not_found")
    order_number = fulfillment._clean_string(
        (live.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
    )
    if order_number != str(payload.get("site_order_number") or ""):
        raise RuntimeError("deal_order_changed")
    live_stage = fulfillment._clean_string(live.stage_id)
    _require_pickup_stage_apply_enabled(settings, apply_enabled_probe)
    if live_stage != target_stage:
        if live_stage != fulfillment._clean_string(payload.get("before_stage")):
            raise RuntimeError(f"deal_stage_changed:{live_stage or '-'}")
        client.update_deal_stage(deal_id, target_stage)
        try:
            live = client.get_deal_by_id(deal_id)
        except Exception as exc:
            raise RuntimeError("deal_stage_readback_failed") from exc
        if live is None:
            raise RuntimeError("deal_stage_readback_unavailable")
        readback_order_number = fulfillment._clean_string(
            (live.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
        )
        if readback_order_number != order_number:
            raise RuntimeError("deal_order_changed_after_stage_update")
        if fulfillment._clean_string(live.stage_id) != target_stage:
            raise RuntimeError("deal_stage_update_not_confirmed")
    case = session.scalar(
        select(SiteOrderExecutionCase).where(
            SiteOrderExecutionCase.site_order_number == order_number
        )
    )
    if case is not None:
        case.current_crm_stage = target_stage
        case.updated_at = now


def _update_crm_fields(
    *,
    row: SiteOrderFulfillmentOutbox,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    apply_enabled_probe: Callable[[], bool] | None = None,
) -> None:
    payload = row.payload or {}
    deal_id = int(row.target_id or 0)
    order_number = fulfillment._clean_string(payload.get("site_order_number"))
    fields = payload.get("fields") or {}
    if deal_id <= 0 or not order_number or not isinstance(fields, dict) or not fields:
        raise RuntimeError("invalid_crm_fields_outbox_payload")
    try:
        live = client.get_deal_by_id(deal_id)
    except Exception as exc:
        raise RetryableBeforeExternalEffect(str(exc)) from exc
    if live is None:
        raise RuntimeError("deal_not_found")
    live_order_number = fulfillment._clean_string(
        (live.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
    )
    if live_order_number != order_number:
        raise RuntimeError("deal_order_changed")
    _require_pickup_stage_apply_enabled(settings, apply_enabled_probe)
    changed = {
        key: value
        for key, value in fields.items()
        if not _crm_field_values_equal(
            key,
            (live.raw or {}).get(key),
            value,
        )
    }
    if changed:
        client.update_deal_fields(deal_id, changed)
        readback = client.get_deal_by_id(deal_id)
        if readback is None:
            raise RuntimeError("deal_fields_readback_unavailable")
        for key, expected in changed.items():
            if not _crm_field_values_equal(
                key,
                (readback.raw or {}).get(key),
                expected,
            ):
                raise RuntimeError(f"deal_field_update_not_confirmed:{key}")


def _finalize_case_event(
    session: Session,
    *,
    row: SiteOrderFulfillmentOutbox,
    now: datetime,
) -> None:
    payload = row.payload or {}
    order_number = fulfillment._clean_string(payload.get("site_order_number"))
    event_type = fulfillment._clean_string(payload.get("event_type"))
    if not order_number or not event_type:
        raise RuntimeError("invalid_finalize_case_event_payload")
    event_at = _parse_naive_datetime(payload.get("event_at")) or now
    event = fulfillment.upsert_execution_event(
        session,
        site_order_number=order_number,
        event_type=event_type,
        event_at=event_at,
        source=fulfillment._clean_string(payload.get("source")) or "system",
        source_ref=fulfillment._clean_string(payload.get("source_ref")) or row.idempotency_key,
        confidence=fulfillment._clean_string(payload.get("confidence")) or "strong",
        raw_message_id=None,
        warehouse_id=fulfillment._int_or_none(payload.get("warehouse_id")),
        actor_ref=fulfillment._clean_string(payload.get("actor_ref")) or None,
        payload=payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {},
    )
    case = session.scalar(
        select(SiteOrderExecutionCase).where(
            SiteOrderExecutionCase.site_order_number == order_number
        )
    )
    if case is not None and event_type == fulfillment.EVENT_PICKUP_RECEIVED:
        case.delivered_at = event_at
        case.updated_at = now
    if event is None:
        return


def _start_sms_workflow(
    session: Session,
    *,
    row: SiteOrderFulfillmentOutbox,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    apply_enabled_probe: Callable[[], bool] | None = None,
) -> None:
    _require_sms_enabled(settings, apply_enabled_probe)
    if settings.order_fulfillment_bot_sms_workflow_template_id is None:
        raise RuntimeError("pickup_sms_workflow_not_configured")
    candidate = _outbox_candidate(session, row)
    if candidate is None or not _sms_candidate_is_new(candidate, settings=settings):
        raise RuntimeError("historical_pickup_sms_blocked")
    deal_id = int(row.target_id or 0)
    marker_field = fulfillment._clean_string(settings.order_fulfillment_bot_pickup_sms_field)
    if deal_id <= 0 or not marker_field:
        raise RuntimeError("invalid_pickup_sms_outbox_payload")
    try:
        live = client.get_deal_by_id(deal_id)
    except Exception as exc:
        raise RetryableBeforeExternalEffect(str(exc)) from exc
    if live is None:
        raise RuntimeError("deal_not_found")
    live_order_number = fulfillment._clean_string(
        (live.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
    )
    if live_order_number != candidate.site_order_number:
        raise RuntimeError("deal_order_changed")
    if fulfillment._clean_string((live.raw or {}).get(marker_field)):
        return
    if fulfillment._clean_string(live.stage_id) != fulfillment.CRM_STAGE_PICKUP_WAITING:
        raise RuntimeError("pickup_sms_stage_changed")
    _require_runtime_apply_enabled(settings, apply_enabled_probe)
    workflow_id = client.start_business_process(
        template_id=settings.order_fulfillment_bot_sms_workflow_template_id,
        deal_id=deal_id,
        parameters={
            "ORDER_NUMBER": candidate.site_order_number,
            "SMS_TEXT": (f"Ваш заказ №{candidate.site_order_number} готов к выдаче. Master Mobile"),
            "MARKER_FIELD": marker_field,
        },
    )
    if not workflow_id:
        raise RuntimeError("pickup_sms_workflow_returned_empty_id")
    row.payload = {**(row.payload or {}), "workflow_id": str(workflow_id or "")}


def _create_task(
    *,
    session: Session,
    row: SiteOrderFulfillmentOutbox,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    apply_enabled_probe: Callable[[], bool] | None = None,
) -> None:
    payload = row.payload or {}
    order_number = str(payload.get("site_order_number") or "")
    task_kind = str(payload.get("task_kind") or "")
    deal_id = int(row.target_id or 0)
    allowed_task_kinds = {
        "notify_client",
        "call",
        "hold_call",
        "dismantle_review",
        "dismantle",
        "onec_return",
        "lost_search",
    }
    if deal_id <= 0 or not order_number or task_kind not in allowed_task_kinds:
        raise RuntimeError("invalid_task_outbox_payload")
    case = session.scalar(
        select(SiteOrderExecutionCase).where(
            SiteOrderExecutionCase.site_order_number == order_number
        )
    )
    responsible_id, accomplice_ids = _resolve_task_route(
        session,
        case=case,
        task_kind=task_kind,
        settings=settings,
    )
    try:
        live = client.get_deal_by_id(deal_id)
    except Exception as exc:
        raise RetryableBeforeExternalEffect(str(exc)) from exc
    if live is None:
        raise RuntimeError("deal_not_found")
    live_order_number = fulfillment._clean_string(
        (live.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
    )
    if live_order_number != order_number:
        raise RuntimeError("deal_order_changed")
    expected_stage = fulfillment._clean_string(payload.get("expected_stage"))
    if expected_stage and fulfillment._clean_string(live.stage_id) != expected_stage:
        raise RuntimeError("deal_stage_changed_before_task")
    titles = {
        "notify_client": f"Проверить телефон и уведомить клиента по заказу №{order_number}",
        "call": f"Позвонить клиенту по самовывозу №{order_number}",
        "hold_call": f"Повторно связаться с клиентом по самовывозу №{order_number}",
        "dismantle_review": f"Проверить самовывоз №{order_number} на расформирование",
        "dismantle": f"Физически разобрать самовывоз №{order_number}",
        "onec_return": f"Оформить возврат самовывоза №{order_number} в 1С",
        "lost_search": f"Найти потерянный самовывоз №{order_number}",
    }
    title = titles[task_kind]
    for user_id in [responsible_id, *accomplice_ids]:
        user = client.get_user_by_id(user_id)
        if user is None or str(user.get("ACTIVE") or "").upper() not in {"Y", "TRUE", "1"}:
            raise RuntimeError(f"task_route_user_inactive:{user_id}")
    _require_runtime_apply_enabled(settings, apply_enabled_probe)
    fields: dict[str, Any] = {
        "TITLE": title,
        "RESPONSIBLE_ID": responsible_id,
        "DESCRIPTION": "Создано подтверждённым сценарием самовывоза.",
        "UF_CRM_TASK": [f"D_{row.target_id}"],
    }
    if accomplice_ids:
        fields["ACCOMPLICES"] = accomplice_ids
    task_result = client.add_task(fields)
    task_id = _task_result_id(task_result)
    if task_id is None:
        raise RuntimeError("task_api_returned_unrecognized_result")
    row.payload = {**payload, "task_id": task_id}


def _resolve_task_route(
    session: Session,
    *,
    case: SiteOrderExecutionCase | None,
    task_kind: str,
    settings: Settings,
) -> tuple[int, list[int]]:
    if task_kind in {"notify_client", "call", "hold_call"}:
        responsible = settings.order_fulfillment_internet_shop_task_responsible_id
        if responsible is None:
            raise RuntimeError("task_route_missing:internet_shop")
        return responsible, []
    if task_kind == "onec_return":
        responsible = settings.order_fulfillment_site_return_task_responsible_id
        if responsible is None:
            raise RuntimeError("task_route_missing:site_return")
        return responsible, []
    if case is None or case.pickup_point_warehouse_id is None:
        raise RuntimeError("task_route_missing:pickup_point")
    warehouse = session.get(LogisticsWarehouse, case.pickup_point_warehouse_id)
    if warehouse is None:
        raise RuntimeError("task_route_missing:warehouse")
    if (
        settings.order_fulfillment_pickup_warehouse_external_ids
        and warehouse.external_id
        not in set(settings.order_fulfillment_pickup_warehouse_external_ids)
    ):
        raise RuntimeError(f"task_route_out_of_scope:{warehouse.external_id}")
    route = settings.order_fulfillment_point_task_routes.get(warehouse.external_id) or {}
    if task_kind in {"dismantle_review", "lost_search"}:
        senior = route.get("senior")
        if senior is None:
            raise RuntimeError(f"task_route_missing:{warehouse.external_id}:senior")
        return senior, []
    operator = route.get("operator")
    senior = route.get("senior")
    if operator is None:
        raise RuntimeError(f"task_route_missing:{warehouse.external_id}:operator")
    return operator, ([senior] if senior is not None and senior != operator else [])


def _action_target_warehouse_id(action: BitrixChatAction) -> int | None:
    return fulfillment._int_or_none((action.payload or {}).get("target_warehouse_id"))


def _selectable_lost_order_warehouse(
    session: Session,
    *,
    warehouse_id: int | None,
    settings: Settings,
) -> LogisticsWarehouse | None:
    if warehouse_id is None:
        return None
    warehouse = session.get(LogisticsWarehouse, warehouse_id)
    route = (
        settings.order_fulfillment_point_task_routes.get(warehouse.external_id) or {}
        if warehouse is not None
        else {}
    )
    if (
        warehouse is None
        or not warehouse.is_active
        or warehouse.kind not in {"store", "retail"}
        or not fulfillment._int_or_none(route.get("operator"))
        or not fulfillment._int_or_none(route.get("senior"))
    ):
        return None
    return warehouse


def _selectable_lost_order_warehouses(
    session: Session,
    *,
    settings: Settings,
) -> list[LogisticsWarehouse]:
    external_ids = {
        external_id
        for external_id, route in settings.order_fulfillment_point_task_routes.items()
        if fulfillment._int_or_none(route.get("operator"))
        and fulfillment._int_or_none(route.get("senior"))
    }
    if not external_ids:
        return []
    return session.scalars(
        select(LogisticsWarehouse)
        .where(
            LogisticsWarehouse.is_active.is_(True),
            LogisticsWarehouse.kind.in_(["store", "retail"]),
            LogisticsWarehouse.external_id.in_(external_ids),
        )
        .order_by(LogisticsWarehouse.name.asc(), LogisticsWarehouse.id.asc())
    ).all()


def _selectable_pickup_warehouses(
    session: Session,
    *,
    settings: Settings,
) -> list[LogisticsWarehouse]:
    filters = [
        LogisticsWarehouse.is_active.is_(True),
        LogisticsWarehouse.kind.in_(["store", "retail"]),
    ]
    if settings.order_fulfillment_pickup_warehouse_external_ids:
        filters.append(
            LogisticsWarehouse.external_id.in_(
                settings.order_fulfillment_pickup_warehouse_external_ids
            )
        )
    return session.scalars(
        select(LogisticsWarehouse)
        .where(*filters)
        .order_by(LogisticsWarehouse.name.asc(), LogisticsWarehouse.id.asc())
    ).all()


def _decision_warehouse_id(
    *,
    candidate: BitrixChatActionCandidate,
    case: SiteOrderExecutionCase,
    action: str,
) -> int | None:
    if action in {ACTION_MOVING, ACTION_ARRIVED, ACTION_FOUND_OTHER}:
        return candidate.pickup_point_warehouse_id
    return case.pickup_point_warehouse_id


def _verify_sms_workflow(
    session: Session,
    *,
    row: SiteOrderFulfillmentOutbox,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    now: datetime,
) -> None:
    candidate = _outbox_candidate(session, row)
    start_row = (
        session.get(SiteOrderFulfillmentOutbox, row.depends_on_id)
        if row.depends_on_id is not None
        else None
    )
    if candidate is None or start_row is None or start_row.operation != OP_START_SMS_WORKFLOW:
        raise SmsMarkerNotConfirmed("pickup_sms_verification_context_missing")
    deal_id = int(row.target_id or 0)
    if deal_id <= 0:
        raise SmsMarkerNotConfirmed("pickup_sms_verification_deal_missing")
    try:
        live = client.get_deal_by_id(deal_id)
    except Exception as exc:
        raise RetryableBeforeExternalEffect(str(exc)) from exc
    if live is None:
        raise RetryableBeforeExternalEffect("pickup_sms_verification_deal_unavailable")
    live_order_number = fulfillment._clean_string(
        (live.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
    )
    if live_order_number != candidate.site_order_number:
        raise SmsMarkerNotConfirmed("pickup_sms_verification_order_changed")
    marker_field = fulfillment._clean_string(settings.order_fulfillment_bot_pickup_sms_field)
    marker_value = (live.raw or {}).get(marker_field) if marker_field else None
    if fulfillment._clean_string(marker_value):
        notification_at = _naive_utc(fulfillment.parse_datetime(marker_value) or now)
        case = session.scalar(
            select(SiteOrderExecutionCase).where(
                SiteOrderExecutionCase.site_order_number == candidate.site_order_number
            )
        )
        if case is None or case.storage_started_at is None:
            raise SmsMarkerNotConfirmed("pickup_sms_case_storage_missing")
        if case.notification_confirmed_at is None:
            case.notification_confirmed_at = notification_at
        case.sla_started_at = max(case.storage_started_at, case.notification_confirmed_at)
        case.storage_deadline_at = case.sla_started_at + timedelta(
            hours=settings.order_fulfillment_bot_dismantle_after_hours
        )
        case.payload = {
            **(case.payload or {}),
            "notification_source": "sms_marker",
        }
        case.updated_at = now
        fulfillment.upsert_execution_event(
            session,
            site_order_number=case.site_order_number,
            event_type=fulfillment.EVENT_PICKUP_NOTIFICATION_CONFIRMED,
            event_at=case.notification_confirmed_at,
            source="bitrix",
            source_ref=f"deal:{deal_id}:{marker_field}",
            confidence="strong",
            raw_message_id=candidate.raw_message_id,
            payload={"marker_field": marker_field},
        )
        enqueue_outbox(
            session,
            candidate=candidate,
            depends_on=row,
            operation=OP_UPDATE_CRM_FIELDS,
            idempotency_key=f"case:{case.id}:crm-fields:sla:{case.sla_started_at.isoformat()}",
            target_type="deal",
            target_id=str(deal_id),
            payload={
                "site_order_number": case.site_order_number,
                "fields": {
                    CRM_PICKUP_SLA_STARTED_FIELD: crm_datetime_iso(case.sla_started_at),
                    CRM_PICKUP_DERIVED_STATUS_FIELD: fulfillment.EVENT_PICKUP_STORED,
                    CRM_PICKUP_LAST_EVIDENCE_FIELD: "pickup_sms_marker_confirmed",
                },
            },
            now=now,
        )
        return
    started_at = start_row.processed_at or start_row.updated_at
    if now < started_at + timedelta(minutes=15):
        raise RetryableBeforeExternalEffect("pickup_sms_marker_pending")
    raise SmsMarkerNotConfirmed("pickup_sms_marker_not_confirmed")


def reconcile_pickup_case_fields(
    session: Session,
    *,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    limit: int = 200,
    now: datetime | None = None,
) -> dict[str, int]:
    now = _naive_utc(now or utcnow())
    stats = {
        "checked": 0,
        "notification_confirmed": 0,
        "hold_changed": 0,
        "invalid_hold": 0,
        "errors": 0,
    }
    cases = session.scalars(
        select(SiteOrderExecutionCase)
        .where(
            SiteOrderExecutionCase.current_crm_stage == fulfillment.CRM_STAGE_PICKUP_WAITING,
            SiteOrderExecutionCase.bitrix_deal_id.is_not(None),
        )
        .order_by(SiteOrderExecutionCase.updated_at.desc())
        .limit(max(1, min(limit, 1000)))
    ).all()
    for case in cases:
        stats["checked"] += 1
        try:
            live = client.get_deal_by_id(int(case.bitrix_deal_id or 0))
        except Exception:
            stats["errors"] += 1
            continue
        if live is None:
            stats["errors"] += 1
            continue
        live_order = fulfillment._clean_string(
            (live.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
        )
        if live_order != case.site_order_number:
            stats["errors"] += 1
            continue
        marker_field = fulfillment._clean_string(settings.order_fulfillment_bot_pickup_sms_field)
        marker_value = (live.raw or {}).get(marker_field) if marker_field else None
        marker_at = fulfillment.parse_datetime(marker_value)
        if marker_at is not None and case.storage_started_at is not None:
            marker_at = _naive_utc(marker_at)
            if case.notification_confirmed_at != marker_at:
                case.notification_confirmed_at = marker_at
                case.sla_started_at = max(case.storage_started_at, marker_at)
                case.storage_deadline_at = case.sla_started_at + timedelta(
                    hours=settings.order_fulfillment_bot_dismantle_after_hours
                )
                case.payload = {
                    **(case.payload or {}),
                    "notification_source": "sms_marker",
                }
                fulfillment.upsert_execution_event(
                    session,
                    site_order_number=case.site_order_number,
                    event_type=fulfillment.EVENT_PICKUP_NOTIFICATION_CONFIRMED,
                    event_at=marker_at,
                    source="bitrix",
                    source_ref=f"deal:{case.bitrix_deal_id}:{marker_field}",
                    confidence="strong",
                    raw_message_id=None,
                    payload={"marker_field": marker_field},
                )
                enqueue_outbox(
                    session,
                    operation=OP_UPDATE_CRM_FIELDS,
                    idempotency_key=(f"case:{case.id}:marker:{marker_at.isoformat()}:crm-fields"),
                    target_type="deal",
                    target_id=str(case.bitrix_deal_id),
                    payload={
                        "site_order_number": case.site_order_number,
                        "fields": {
                            CRM_PICKUP_SLA_STARTED_FIELD: crm_datetime_iso(case.sla_started_at),
                            CRM_PICKUP_DERIVED_STATUS_FIELD: fulfillment.EVENT_PICKUP_STORED,
                            CRM_PICKUP_LAST_EVIDENCE_FIELD: "pickup_sms_marker_confirmed",
                        },
                    },
                    now=now,
                )
                stats["notification_confirmed"] += 1
        raw_hold = (live.raw or {}).get(CRM_PICKUP_HOLD_UNTIL_FIELD)
        hold_value = _parse_hold_date(raw_hold)
        if raw_hold and hold_value is None:
            stats["invalid_hold"] += 1
            case.current_derived_status = "manual_review"
            case.confidence = "weak"
        elif hold_value != case.hold_until:
            previous = case.hold_until
            case.hold_until = hold_value
            hold_revision = int((case.payload or {}).get("hold_revision") or 0) + 1
            case.payload = {**(case.payload or {}), "hold_revision": hold_revision}
            fulfillment.upsert_execution_event(
                session,
                site_order_number=case.site_order_number,
                event_type="pickup_hold_changed",
                event_at=now,
                source="bitrix",
                source_ref=(
                    f"deal:{case.bitrix_deal_id}:hold:"
                    f"{previous.isoformat() if previous else '-'}:"
                    f"{hold_value.isoformat() if hold_value else '-'}:{now.isoformat()}"
                ),
                confidence="strong",
                raw_message_id=None,
                payload={
                    "previous": previous.isoformat() if previous else None,
                    "current": hold_value.isoformat() if hold_value else None,
                },
            )
            stats["hold_changed"] += 1
        if case.hold_until is not None and case.hold_until < _moscow_date(now):
            stats["invalid_hold"] += 1
            case.current_derived_status = "manual_review"
            case.confidence = "weak"
        case.updated_at = now
    session.commit()
    return stats


def _parse_hold_date(value: Any) -> date | None:
    if value in (None, "", "0000-00-00"):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    cleaned = fulfillment._clean_string(value)
    try:
        return date.fromisoformat(cleaned[:10])
    except ValueError:
        return None


def crm_datetime_iso(value: datetime) -> str:
    """Serialize service UTC-naive datetimes with an explicit offset for Bitrix."""

    return _aware_utc(value).isoformat()


def _crm_field_values_equal(field_name: str, actual: Any, expected: Any) -> bool:
    if field_name in CRM_PICKUP_DATETIME_FIELDS:
        actual_dt = fulfillment.parse_datetime(actual)
        expected_dt = fulfillment.parse_datetime(expected)
        if actual_dt is not None and expected_dt is not None:
            return _naive_utc(actual_dt) == _naive_utc(expected_dt)
    return str(actual or "") == str(expected or "")


def enqueue_due_sla_tasks(
    session: Session,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> int:
    now = _naive_utc(now or utcnow())
    if not settings.order_fulfillment_pickup_sla_enabled:
        return 0
    route_errors = task_route_configuration_errors(session, settings=settings)
    if route_errors:
        raise TaskRouteConfigurationError(";".join(route_errors))
    _acquire_advisory_xact_lock(session, SLA_TASK_ADVISORY_LOCK_KEY)
    cases = session.scalars(
        select(SiteOrderExecutionCase).where(
            SiteOrderExecutionCase.current_crm_stage == fulfillment.CRM_STAGE_PICKUP_WAITING,
            SiteOrderExecutionCase.storage_started_at.is_not(None),
            SiteOrderExecutionCase.delivered_at.is_(None),
            SiteOrderExecutionCase.bitrix_deal_id.is_not(None),
        )
    ).all()
    created = 0
    for case in cases:
        if not _case_is_after_cutover(case, settings=settings):
            continue
        if case.notification_confirmed_at is None or case.sla_started_at is None:
            created += _enqueue_case_task_once(
                session,
                case=case,
                task_kind="notify_client",
                key=f"case:{case.id}:task:notify_client",
                now=now,
            )
            continue
        today_moscow = _moscow_date(now)
        if case.hold_until is not None:
            if case.hold_until <= today_moscow:
                hold_revision = int((case.payload or {}).get("hold_revision") or 0)
                created += _enqueue_case_task_once(
                    session,
                    case=case,
                    task_kind="hold_call",
                    key=(
                        f"case:{case.id}:task:hold_call:{hold_revision}:"
                        f"{case.hold_until.isoformat()}"
                    ),
                    now=now,
                )
            continue
        if now >= case.sla_started_at + timedelta(
            hours=settings.order_fulfillment_bot_call_after_hours
        ):
            created += _enqueue_case_task_once(
                session,
                case=case,
                task_kind="call",
                key=f"case:{case.id}:task:call:initial",
                now=now,
            )
        if now >= case.sla_started_at + timedelta(
            hours=settings.order_fulfillment_bot_dismantle_after_hours
        ):
            created_now = _enqueue_case_task_once(
                session,
                case=case,
                task_kind="dismantle_review",
                key=f"case:{case.id}:task:dismantle_review:initial",
                now=now,
            )
            if created_now:
                fulfillment.upsert_execution_event(
                    session,
                    site_order_number=case.site_order_number,
                    event_type=fulfillment.EVENT_PICKUP_DISMANTLE_CANDIDATE,
                    event_at=now,
                    source="system",
                    source_ref=f"case:{case.id}:sla:96",
                    confidence="strong",
                    raw_message_id=None,
                    payload={"sla_started_at": case.sla_started_at.isoformat()},
                )
                created += 1
    session.commit()
    return created


def task_route_configuration_errors(
    session: Session,
    *,
    settings: Settings,
) -> list[str]:
    errors: list[str] = []
    if settings.order_fulfillment_internet_shop_task_responsible_id is None:
        errors.append("task_route_missing:internet_shop")
    if settings.order_fulfillment_site_return_task_responsible_id is None:
        errors.append("task_route_missing:site_return")
    warehouse_filters = [
        LogisticsWarehouse.is_active.is_(True),
        LogisticsWarehouse.kind.in_(["store", "retail"]),
    ]
    if settings.order_fulfillment_pickup_warehouse_external_ids:
        warehouse_filters.append(
            LogisticsWarehouse.external_id.in_(
                settings.order_fulfillment_pickup_warehouse_external_ids
            )
        )
    warehouses = session.scalars(
        select(LogisticsWarehouse)
        .where(*warehouse_filters)
        .order_by(LogisticsWarehouse.external_id.asc())
    ).all()
    if not warehouses:
        errors.append("task_route_missing:pickup_warehouse_catalog")
    configured_warehouse_ids = set(settings.order_fulfillment_pickup_warehouse_external_ids)
    if configured_warehouse_ids:
        found_warehouse_ids = {warehouse.external_id for warehouse in warehouses}
        errors.extend(
            f"task_route_missing:pickup_warehouse:{external_id}"
            for external_id in sorted(configured_warehouse_ids - found_warehouse_ids)
        )
    for warehouse in warehouses:
        route = settings.order_fulfillment_point_task_routes.get(warehouse.external_id) or {}
        for role in ("operator", "senior"):
            if not fulfillment._int_or_none(route.get(role)):
                errors.append(f"task_route_missing:{warehouse.external_id}:{role}")
    return errors


def _enqueue_case_task_once(
    session: Session,
    *,
    case: SiteOrderExecutionCase,
    task_kind: str,
    key: str,
    now: datetime,
) -> int:
    if session.scalar(
        select(SiteOrderFulfillmentOutbox.id).where(
            SiteOrderFulfillmentOutbox.idempotency_key == key
        )
    ):
        return 0
    enqueue_outbox(
        session,
        operation=OP_CREATE_TASK,
        idempotency_key=key,
        target_type="deal",
        target_id=str(case.bitrix_deal_id),
        payload={
            "task_kind": task_kind,
            "site_order_number": case.site_order_number,
            "expected_stage": fulfillment.CRM_STAGE_PICKUP_WAITING,
        },
        now=now,
    )
    return 1


def _case_is_after_cutover(case: SiteOrderExecutionCase, *, settings: Settings) -> bool:
    cutover = settings.order_fulfillment_bot_cutover_at
    if cutover is None or case.storage_started_at is None:
        return False
    return _aware_utc(case.storage_started_at) >= _aware_utc(cutover)


def _require_pickup_stage_apply_enabled(
    settings: Settings,
    apply_enabled_probe: Callable[[], bool] | None,
) -> None:
    _require_runtime_apply_enabled(settings, apply_enabled_probe)
    if not settings.order_fulfillment_pickup_stage_apply_enabled:
        raise ApplyDisabledBeforeSideEffect("order_fulfillment_pickup_stage_apply_disabled")


def _require_inventory_enabled(
    settings: Settings,
    apply_enabled_probe: Callable[[], bool] | None,
) -> None:
    _require_runtime_apply_enabled(settings, apply_enabled_probe)
    if not settings.order_fulfillment_pickup_inventory_enabled:
        raise ApplyDisabledBeforeSideEffect("order_fulfillment_pickup_inventory_disabled")


def _require_inventory_won_enabled(
    settings: Settings,
    apply_enabled_probe: Callable[[], bool] | None,
) -> None:
    _require_inventory_enabled(settings, apply_enabled_probe)
    if not settings.order_fulfillment_inventory_won_enabled:
        raise ApplyDisabledBeforeSideEffect("order_fulfillment_inventory_won_disabled")


def _require_inventory_won_evidence_current(
    session: Session,
    *,
    row: SiteOrderFulfillmentOutbox,
    client: fulfillment.BitrixChatClient,
    onec_validator: Callable[[str], OneCPickupValidation],
    validate_composite: bool,
) -> None:
    payload = row.payload or {}
    order_number = fulfillment._clean_string(payload.get("site_order_number"))
    previous_id = fulfillment._int_or_none(payload.get("inventory_previous_submission_id"))
    current_id = fulfillment._int_or_none(payload.get("inventory_current_submission_id"))
    previous = session.get(PickupInventorySubmission, previous_id) if previous_id else None
    current = session.get(PickupInventorySubmission, current_id) if current_id else None
    if (
        previous is None
        or current is None
        or previous.status != pickup_inventory.STATUS_CONFIRMED
        or current.status != pickup_inventory.STATUS_CONFIRMED
        or current.supersedes_submission_id != previous.id
        or current.warehouse_id is None
    ):
        raise SourceMessageEditedBeforeApply("inventory_evidence_changed_before_apply")
    if not validate_composite:
        return
    previous_orders = {item.site_order_number for item in previous.items}
    current_orders = {item.site_order_number for item in current.items}
    if not order_number or order_number not in previous_orders or order_number in current_orders:
        raise SourceMessageEditedBeforeApply("inventory_disappearance_changed_before_apply")
    disappearance = pickup_inventory.InventoryDisappearance(
        site_order_number=order_number,
        warehouse_id=current.warehouse_id,
        previous_submission_id=previous.id,
        current_submission_id=current.id,
        previous_at=previous.submitted_at,
        current_at=current.submitted_at,
    )
    uncontested, reason = pickup_inventory.disappearance_is_uncontested(
        session,
        candidate=disappearance,
    )
    if not uncontested:
        raise SourceMessageEditedBeforeApply(f"inventory_won_blocked:{reason}")
    onec = onec_validator(order_number)
    if not onec.available or not onec.assembled or onec.return_confirmed:
        raise SourceMessageEditedBeforeApply("inventory_onec_changed_before_apply")
    deals = client.list_deals_by_site_order(order_number)
    target_id = fulfillment._int_or_none(row.target_id)
    if (
        len(deals) != 1
        or deals[0].deal_id != target_id
        or fulfillment._clean_string(deals[0].stage_id) != fulfillment.CRM_STAGE_PICKUP_WAITING
        or not fulfillment._is_internal_pickup_deal(deals[0])
    ):
        raise SourceMessageEditedBeforeApply("inventory_crm_changed_before_apply")


def _require_historical_evidence_current(
    session: Session,
    *,
    row: SiteOrderFulfillmentOutbox,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], OneCPickupValidation],
) -> None:
    from app.services import pickup_history

    payload = row.payload or {}
    order_number = fulfillment._clean_string(payload.get("site_order_number"))
    if not order_number:
        raise SourceMessageEditedBeforeApply("historical_order_missing_before_apply")
    assessment = pickup_history.reassess_historical_order(
        session,
        site_order_number=order_number,
        client=client,
        settings=settings,
        onec_validator=onec_validator,
    )
    expected_warehouses = tuple(
        sorted(
            value
            for item in payload.get("historical_warehouse_ids") or []
            if (value := fulfillment._int_or_none(item)) is not None
        )
    )
    if (
        assessment.bitrix_deal_id != fulfillment._int_or_none(row.target_id)
        or assessment.current_stage != fulfillment._clean_string(payload.get("before_stage"))
        or assessment.queue != fulfillment._clean_string(payload.get("historical_queue"))
        or assessment.target_stage != fulfillment._clean_string(payload.get("target_stage"))
        or assessment.reason != fulfillment._clean_string(payload.get("historical_reason"))
        or tuple(sorted(assessment.warehouse_ids)) != expected_warehouses
    ):
        raise SourceMessageEditedBeforeApply("historical_evidence_changed_before_apply")


def _require_sms_enabled(
    settings: Settings,
    apply_enabled_probe: Callable[[], bool] | None,
) -> None:
    _require_runtime_apply_enabled(settings, apply_enabled_probe)
    if not settings.order_fulfillment_bot_sms_enabled:
        raise ApplyDisabledBeforeSideEffect("order_fulfillment_bot_sms_disabled")


def _require_lost_orders_enabled(
    settings: Settings,
    apply_enabled_probe: Callable[[], bool] | None,
) -> None:
    _require_runtime_apply_enabled(settings, apply_enabled_probe)
    if not settings.order_fulfillment_lost_orders_enabled:
        raise ApplyDisabledBeforeSideEffect("order_fulfillment_lost_orders_disabled")


def _require_sla_enabled(
    settings: Settings,
    apply_enabled_probe: Callable[[], bool] | None,
) -> None:
    _require_runtime_apply_enabled(settings, apply_enabled_probe)
    if not settings.order_fulfillment_pickup_sla_enabled:
        raise ApplyDisabledBeforeSideEffect("order_fulfillment_pickup_sla_disabled")


def _moscow_date(value: datetime) -> date:
    return _aware_utc(value).astimezone(ZoneInfo("Europe/Moscow")).date()


def _ensure_case(
    session: Session,
    *,
    candidate: BitrixChatActionCandidate,
    deal: fulfillment.BitrixDealSnapshot,
) -> SiteOrderExecutionCase:
    case = session.scalar(
        select(SiteOrderExecutionCase).where(
            SiteOrderExecutionCase.site_order_number == candidate.site_order_number
        )
    )
    if case is None:
        case = SiteOrderExecutionCase(
            site_order_number=candidate.site_order_number,
            bitrix_deal_id=deal.deal_id,
            delivery_method=deal.delivery,
            current_derived_status="manual_review",
            current_crm_stage=deal.stage_id,
            confidence="weak",
            payload={},
        )
        session.add(case)
        session.flush()
    else:
        case.bitrix_deal_id = deal.deal_id
        case.delivery_method = deal.delivery
    return case


def _queue_card_update(
    session: Session,
    candidate: BitrixChatActionCandidate,
    action: BitrixChatAction,
    status_text: str,
    now: datetime,
    *,
    depends_on: SiteOrderFulfillmentOutbox | None = None,
) -> None:
    if (candidate.payload or {}).get("structured_batch_leader_id"):
        return
    if not candidate.bot_message_id and not (candidate.payload or {}).get("automatic_arrival"):
        return
    enqueue_outbox(
        session,
        candidate=candidate,
        action=action,
        depends_on=depends_on,
        operation=OP_UPDATE_CARD,
        idempotency_key=f"action:{action.id}:card-update",
        payload={"status_text": status_text},
        now=now,
    )


def _sms_candidate_is_new(
    candidate: BitrixChatActionCandidate,
    *,
    settings: Settings,
) -> bool:
    cutover = settings.order_fulfillment_bot_cutover_at
    if cutover is None or candidate.source_event_at is None:
        return False
    return _aware_utc(candidate.source_event_at) >= _aware_utc(cutover)


def _deal_pickup_point_conflicts(
    candidate: BitrixChatActionCandidate,
    *,
    deal: fulfillment.BitrixDealSnapshot,
) -> bool:
    candidate_text = fulfillment._clean_text(candidate.pickup_point_name or "")
    deal_text = fulfillment._clean_text(
        " ".join(value for value in (deal.delivery, deal.post_delivery_type) if value)
    )
    candidate_cues = {cue for cue in PICKUP_CUES if cue != "самовывоз" and cue in candidate_text}
    deal_cues = {cue for cue in PICKUP_CUES if cue != "самовывоз" and cue in deal_text}
    return bool(candidate_cues and deal_cues and candidate_cues.isdisjoint(deal_cues))


def _sms_workflow_marker_present(
    session: Session,
    *,
    row: SiteOrderFulfillmentOutbox,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
) -> bool:
    candidate = _outbox_candidate(session, row)
    if candidate is None:
        return False
    deal_id = int(row.target_id or 0)
    live = client.get_deal_by_id(deal_id)
    if live is None:
        return False
    order_number = fulfillment._clean_string(
        (live.raw or {}).get(fulfillment.CRM_ORDER_NUMBER_FIELD)
    )
    if order_number != candidate.site_order_number:
        return False
    return bool(
        fulfillment._clean_string(
            (live.raw or {}).get(settings.order_fulfillment_bot_pickup_sms_field)
        )
    )


def _mark_candidate_manual_review(
    session: Session,
    *,
    row: SiteOrderFulfillmentOutbox,
    reason: str,
    now: datetime,
) -> None:
    candidate = _outbox_candidate(session, row)
    if candidate is not None and candidate.status not in {
        CANDIDATE_APPLIED,
        CANDIDATE_DRY_RUN,
        CANDIDATE_DISMISSED,
        CANDIDATE_EXPIRED,
    }:
        candidate.status = CANDIDATE_REVIEW
        candidate.updated_at = now
        _reject_pending_confirmation(
            session,
            candidate=candidate,
            reason=reason,
        )
    action = session.get(BitrixChatAction, row.action_id) if row.action_id else None
    if action is not None and action.status not in {"completed", "dry_run"}:
        action.status = "manual_review"
        action.reason = reason[:255]
    if (
        candidate is not None
        and candidate.bot_message_id
        and row.operation in {OP_PROCESS_ACTION, OP_PUBLISH_CONFIRMATION}
    ):
        enqueue_outbox(
            session,
            candidate=candidate,
            action=action,
            operation=OP_UPDATE_CARD,
            idempotency_key=f"outbox:{row.id}:manual-review-card",
            payload={"status_text": "Нужна ручная проверка: действие завершилось с ошибкой"},
            now=now,
        )


def _expire_pending_confirmation(
    session: Session,
    *,
    candidate: BitrixChatActionCandidate,
) -> None:
    _reject_pending_confirmation(
        session,
        candidate=candidate,
        reason="callback_expired",
    )


def _reject_pending_confirmation(
    session: Session,
    *,
    candidate: BitrixChatActionCandidate,
    reason: str,
) -> None:
    pending = session.scalar(
        select(BitrixChatAction)
        .where(
            BitrixChatAction.candidate_id == candidate.id,
            BitrixChatAction.status == "awaiting_confirmation",
        )
        .order_by(BitrixChatAction.id.desc())
    )
    if pending is not None:
        pending.status = "rejected"
        pending.reason = reason[:255]


def _acquire_advisory_xact_lock(session: Session, lock_key: int) -> None:
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )


def _acquire_order_action_lock(session: Session, site_order_number: str) -> None:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    digest = hashlib.sha256(f"pickup-order-action:{site_order_number}".encode()).digest()
    lock_key = int.from_bytes(digest[:8], "big", signed=True)
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": lock_key},
    )


def _parse_naive_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _naive_utc(value)
    if not value:
        return None
    try:
        return _naive_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _task_result_id(value: Any) -> str | None:
    if isinstance(value, dict):
        task = value.get("task") or value.get("TASK")
        if isinstance(task, dict):
            value = task.get("id") or task.get("ID")
        else:
            value = value.get("id") or value.get("ID")
    clean_value = fulfillment._clean_string(value)
    return clean_value or None


def _decision_reason_text(reason: str) -> str:
    return DECISION_REASON_TEXT.get(reason, "условия действия не подтверждены")


def _finish_outbox(
    row: SiteOrderFulfillmentOutbox,
    *,
    status: str,
    error: str | None,
    now: datetime,
) -> None:
    row.status = status
    row.last_error = error
    row.processed_at = now if status in {OUTBOX_COMPLETED, OUTBOX_FAILED} else None
    row.updated_at = now


def _numeric_chat_id(dialog_id: str) -> int:
    match = re.search(r"(\d+)$", dialog_id)
    if match is None:
        raise ValueError("chat_id_missing")
    return int(match.group(1))


def _numeric_message_id(message_id: str) -> int:
    normalized = str(message_id).strip()
    if not normalized.isdigit():
        raise ValueError("message id must be numeric")
    value = int(normalized)
    if value <= 0 or value > 2_147_483_647:
        raise ValueError("message id is outside the supported range")
    return value


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
