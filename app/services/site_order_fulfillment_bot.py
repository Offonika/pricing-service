from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session, aliased

from app.core.config import Settings
from app.models.site_order_fulfillment import (
    BitrixChatAction,
    BitrixChatActionCandidate,
    BitrixChatMessage,
    SiteOrderExecutionCase,
    SiteOrderFulfillmentOutbox,
)
from app.services import site_order_fulfillment as fulfillment
from app.services.logistics_onec import resolve_target_warehouse

CHAT_PICKUP_READY = "pickup_ready"
PARSER_VERSION = "pickup-bot-v1"

ACTION_ARRIVED = "arrived"
ACTION_ISSUED = "issued"
ACTION_UNCLAIMED = "unclaimed"
ACTION_DISMANTLE = "dismantle"
ACTION_CANCEL = "cancel"
ACTIONS = {
    ACTION_ARRIVED,
    ACTION_ISSUED,
    ACTION_UNCLAIMED,
    ACTION_DISMANTLE,
    ACTION_CANCEL,
}
DANGEROUS_ACTIONS = {ACTION_ISSUED, ACTION_DISMANTLE}

ACTION_LABELS = {
    ACTION_ARRIVED: "Прибыл в точку",
    ACTION_ISSUED: "Выдан клиенту",
    ACTION_UNCLAIMED: "Не забран",
    ACTION_DISMANTLE: "На расформирование",
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
    "dismantle_too_early": "срок хранения ещё не истёк",
    "dismantle_payment_conflict": "оплата или выдача блокирует расформирование",
}
ACTION_EVENT_TYPES = {
    ACTION_ARRIVED: fulfillment.EVENT_PICKUP_STORED,
    ACTION_ISSUED: fulfillment.EVENT_PICKUP_RECEIVED,
    ACTION_UNCLAIMED: fulfillment.EVENT_PICKUP_UNCLAIMED,
    ACTION_DISMANTLE: fulfillment.EVENT_PICKUP_DISMANTLING,
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

APPLY_GATED_OUTBOX_OPERATIONS = frozenset(
    {
        OP_UPDATE_CRM_STAGE,
        OP_FINALIZE_ACTION,
        OP_START_SMS_WORKFLOW,
        OP_VERIFY_SMS_WORKFLOW,
        OP_CREATE_TASK,
    }
)

NON_RETRYABLE_EXTERNAL_OPERATIONS = {
    OP_PUBLISH_CARD,
    OP_START_SMS_WORKFLOW,
    OP_CREATE_TASK,
}
SMS_PILOT_ADVISORY_LOCK_KEY = 5_584_927_483_671
CANDIDATE_CREATE_ADVISORY_LOCK_KEY = 5_584_927_483_672
SLA_TASK_ADVISORY_LOCK_KEY = 5_584_927_483_673
APPLY_ENABLED_ENV_KEY = "ORDER_FULFILLMENT_BOT_APPLY_ENABLED"
TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
DEFAULT_RUNTIME_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

ALLOWED_ARRIVAL_STAGES = {
    "PREPARATION",
    "EXECUTING",
    "FINAL_INVOICE",
    "IN_DELIVERY",
    fulfillment.CRM_STAGE_PICKUP_WAITING,
}
TERMINAL_STAGES = {"WON", "LOSE", "DISMANTLING", "APOLOGY"}

ORDER_LIMIT_PER_MESSAGE = 10
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
    "поступил",
    "поступила",
    "на точке",
    "готов к выдаче",
    "готова к выдаче",
)


class BotSecurityError(ValueError):
    """Callback cannot be trusted."""


class RetryableBeforeExternalEffect(RuntimeError):
    """A read-only preflight failed before a non-idempotent external call."""


class ApplyDisabledBeforeSideEffect(RetryableBeforeExternalEffect):
    """The runtime kill-switch disabled an already queued side effect."""


class SmsMarkerNotConfirmed(RuntimeError):
    """The asynchronous SMS workflow did not confirm its marker in time."""


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


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def parse_pickup_candidate_text(
    text_value: str,
    *,
    dialog_id: str,
) -> list[PickupCandidateMention]:
    if not text_value or fulfillment._contains_non_authoritative_chat_marker(text_value):
        return []
    normalized = fulfillment._clean_text(text_value)
    if any(marker in normalized for marker in fulfillment.GENERATED_ORDER_REPORT_MARKERS):
        return []
    if "распознано:" in normalized and "точка:" in normalized:
        return []
    order_numbers = fulfillment.extract_order_numbers(text_value)[:ORDER_LIMIT_PER_MESSAGE]
    if not order_numbers:
        return []
    action = _classify_pickup_action(normalized)
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
    return None


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
    mentions = parse_pickup_candidate_text(text_value, dialog_id=dialog_id)
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
        raw_message = BitrixChatMessage(
            chat_code=(
                CHAT_PICKUP_READY
                if dialog_id == "chat8729"
                else fulfillment.CHAT_SITE_MASTER_MOBILE
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

    candidate_count = int(session.scalar(select(func.count(BitrixChatActionCandidate.id))) or 0)
    runtime_apply_enabled = _runtime_apply_enabled(settings, apply_enabled_probe)
    resolution = resolve_target_warehouse(session, [text_value])
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
            status=CANDIDATE_OPEN,
            expires_at=now + timedelta(hours=settings.order_fulfillment_bot_card_ttl_hours),
            nonce=secrets.token_hex(16),
            dry_run=bool(force_dry_run or not runtime_apply_enabled),
            payload={
                "evidence": mention.evidence_text,
                "pickup_resolution_reason": resolution.reason,
                "pickup_matches": resolution.matches,
            },
        )
        session.add(candidate)
        session.flush()
        candidate_count += 1
        enqueue_outbox(
            session,
            candidate=candidate,
            operation=OP_PUBLISH_CARD,
            idempotency_key=f"candidate:{candidate.id}:publish",
            payload={},
            now=now,
        )
        created.append(candidate)
    session.commit()
    return created


def sign_callback_token(
    candidate: BitrixChatActionCandidate,
    *,
    action: str,
    step: int,
    secret: str,
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
        )
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
    idempotency_key = f"candidate:{candidate.id}:action:{decoded.action}:step:{decoded.step}"
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
        payload={},
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


def card_text(candidate: BitrixChatActionCandidate, *, status_text: str | None = None) -> str:
    prefix = "Тест — без изменений\n" if candidate.dry_run else ""
    point = candidate.pickup_point_name or "точка не определена"
    lines = [
        f"{prefix}Заказ №{candidate.site_order_number}",
        f"Распознано: {ACTION_LABELS.get(candidate.detected_action, candidate.detected_action)}",
        f"Точка: {point}",
    ]
    if status_text:
        lines.append(status_text)
    return "\n".join(lines)


def card_keyboard(
    candidate: BitrixChatActionCandidate,
    *,
    settings: Settings,
) -> list[dict[str, Any]]:
    secret = settings.order_fulfillment_bot_callback_secret
    if not secret:
        return []
    colors = {
        ACTION_ARRIVED: "#2FC6F6",
        ACTION_ISSUED: "#9DCF00",
        ACTION_UNCLAIMED: "#F5A623",
        ACTION_DISMANTLE: "#E74C3C",
        ACTION_CANCEL: "#A6A6A6",
    }
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
        for action in (
            ACTION_ARRIVED,
            ACTION_ISSUED,
            ACTION_UNCLAIMED,
            ACTION_DISMANTLE,
            ACTION_CANCEL,
        )
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
    if stage in TERMINAL_STAGES:
        return PickupActionDecision(False, None, None, "terminal_crm_stage")
    if candidate.pickup_point_warehouse_id is None and action == ACTION_ARRIVED:
        return PickupActionDecision(False, None, None, "pickup_point_unresolved")
    if _deal_pickup_point_conflicts(candidate, deal=deal):
        return PickupActionDecision(False, None, None, "pickup_point_deal_mismatch")
    if (
        case is not None
        and case.pickup_point_warehouse_id is not None
        and candidate.pickup_point_warehouse_id is not None
        and case.pickup_point_warehouse_id != candidate.pickup_point_warehouse_id
    ):
        return PickupActionDecision(False, None, None, "pickup_point_mismatch")
    if not onec.available:
        return PickupActionDecision(False, None, None, "onec_unavailable")
    if onec.return_confirmed and action in {ACTION_ARRIVED, ACTION_ISSUED}:
        return PickupActionDecision(False, None, None, "onec_return_conflict")

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
            and case.storage_started_at is not None
            and now
            >= case.storage_started_at
            + timedelta(hours=settings.order_fulfillment_bot_call_after_hours)
        )
        return PickupActionDecision(
            True,
            None,
            fulfillment.EVENT_PICKUP_UNCLAIMED,
            "pickup_unclaimed_recorded",
            create_task="call" if due else None,
        )

    if confirmation_step != 2:
        return PickupActionDecision(False, None, None, "second_confirmation_required")
    if stage != fulfillment.CRM_STAGE_PICKUP_WAITING:
        return PickupActionDecision(False, None, None, "dismantle_transition_not_allowed")
    if case is None or case.storage_started_at is None:
        return PickupActionDecision(False, None, None, "storage_start_missing")
    if now < case.storage_started_at + timedelta(
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


def process_outbox(
    session: Session,
    *,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], OneCPickupValidation],
    apply_enabled_probe: Callable[[], bool] | None = None,
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
            stats["selected"] -= 1
        except Exception as exc:  # durable boundary: persist a safe retry state
            session.rollback()
            row = session.get(SiteOrderFulfillmentOutbox, row_id)
            if row is None:
                raise RuntimeError(f"outbox_row_disappeared:{row_id}") from exc
            error = fulfillment._safe_error_reason(str(exc))[:1000]
            if isinstance(exc, SmsMarkerNotConfirmed):
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
    now: datetime,
) -> None:
    if row.operation in APPLY_GATED_OUTBOX_OPERATIONS and not _runtime_apply_enabled(
        settings, apply_enabled_probe
    ):
        raise ApplyDisabledBeforeSideEffect("order_fulfillment_bot_apply_disabled")
    candidate = _outbox_candidate(session, row)
    action = session.get(BitrixChatAction, row.action_id) if row.action_id else None
    if row.operation == OP_PUBLISH_CARD:
        _publish_card(candidate, client=client, settings=settings, now=now)
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
        _publish_confirmation(candidate, action=action, client=client, settings=settings)
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
            row=row,
            client=client,
            settings=settings,
            apply_enabled_probe=apply_enabled_probe,
        )
        return
    raise RuntimeError(f"unsupported_outbox_operation:{row.operation}")


def _publish_card(
    candidate: BitrixChatActionCandidate | None,
    *,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
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
            candidate.status = CANDIDATE_REVIEW
            status_text = "Проверка: сделка уже закрыта"
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
            card_keyboard(candidate, settings=settings)
            if candidate.status == CANDIDATE_OPEN
            else []
        ),
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
    if candidate.dry_run or not _runtime_apply_enabled(settings, apply_enabled_probe):
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
            "event_at": now.isoformat(),
            "dismantle_after_hours": settings.order_fulfillment_bot_dismantle_after_hours,
        },
        now=now,
    )
    if (
        decision.send_sms
        and settings.order_fulfillment_bot_sms_enabled
        and _sms_candidate_is_new(candidate, settings=settings)
    ):
        _acquire_advisory_xact_lock(session, SMS_PILOT_ADVISORY_LOCK_KEY)
        if _sms_pilot_has_capacity(session, settings=settings):
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
        },
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
        },
    ]
    client.update_bot_message(
        message_id=candidate.bot_message_id,
        bot_id=settings.order_fulfillment_bot_id,
        message=card_text(candidate, status_text="Требуется второе подтверждение"),
        keyboard=keyboard,
    )


def _update_card(
    candidate: BitrixChatActionCandidate,
    *,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    payload: dict[str, Any],
) -> None:
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
    if event_type:
        fulfillment.upsert_execution_event(
            session,
            site_order_number=candidate.site_order_number,
            event_type=event_type,
            event_at=event_at,
            source="manual",
            source_ref=f"pickup_bot_action:{action.id}",
            confidence="strong",
            raw_message_id=candidate.raw_message_id,
            payload={"candidate_id": candidate.id, "actor_id": action.actor_id},
        )
    case = session.scalar(
        select(SiteOrderExecutionCase).where(
            SiteOrderExecutionCase.site_order_number == candidate.site_order_number
        )
    )
    if case is None:
        raise RuntimeError("execution_case_missing")
    if action.action == ACTION_ARRIVED:
        if case.storage_started_at is None:
            case.storage_started_at = event_at
            case.storage_deadline_at = event_at + timedelta(
                hours=int(payload.get("dismantle_after_hours") or 96)
            )
        if candidate.pickup_point_warehouse_id is not None:
            case.pickup_point_warehouse_id = candidate.pickup_point_warehouse_id
    elif action.action == ACTION_ISSUED:
        case.delivered_at = event_at
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
    _require_runtime_apply_enabled(settings, apply_enabled_probe)
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


def _start_sms_workflow(
    session: Session,
    *,
    row: SiteOrderFulfillmentOutbox,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    apply_enabled_probe: Callable[[], bool] | None = None,
) -> None:
    if not settings.order_fulfillment_bot_sms_enabled:
        raise RuntimeError("pickup_sms_disabled")
    if settings.order_fulfillment_bot_sms_workflow_template_id is None:
        raise RuntimeError("pickup_sms_workflow_not_configured")
    candidate = _outbox_candidate(session, row)
    if candidate is None or not _sms_candidate_is_new(candidate, settings=settings):
        raise RuntimeError("historical_pickup_sms_blocked")
    active_count = int(
        session.scalar(
            select(func.count(SiteOrderFulfillmentOutbox.id)).where(
                SiteOrderFulfillmentOutbox.operation == OP_START_SMS_WORKFLOW,
                SiteOrderFulfillmentOutbox.id != row.id,
            )
        )
        or 0
    )
    if active_count >= settings.order_fulfillment_bot_sms_pilot_limit:
        raise RuntimeError("pickup_sms_pilot_limit_reached")
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
    row: SiteOrderFulfillmentOutbox,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    apply_enabled_probe: Callable[[], bool] | None = None,
) -> None:
    if settings.order_fulfillment_bot_task_responsible_id is None:
        raise RuntimeError("task_responsible_not_configured")
    payload = row.payload or {}
    order_number = str(payload.get("site_order_number") or "")
    task_kind = str(payload.get("task_kind") or "")
    deal_id = int(row.target_id or 0)
    if deal_id <= 0 or not order_number or task_kind not in {"call", "dismantle"}:
        raise RuntimeError("invalid_task_outbox_payload")
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
    title = (
        f"Позвонить клиенту по самовывозу №{order_number}"
        if task_kind == "call"
        else f"Расформировать самовывоз №{order_number}"
    )
    _require_runtime_apply_enabled(settings, apply_enabled_probe)
    task_result = client.add_task(
        {
            "TITLE": title,
            "RESPONSIBLE_ID": settings.order_fulfillment_bot_task_responsible_id,
            "DESCRIPTION": "Создано подтверждённым сценарием самовывоза.",
            "UF_CRM_TASK": [f"D_{row.target_id}"],
        }
    )
    task_id = _task_result_id(task_result)
    if task_id is None:
        raise RuntimeError("task_api_returned_unrecognized_result")
    row.payload = {**payload, "task_id": task_id}


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
    if marker_field and fulfillment._clean_string((live.raw or {}).get(marker_field)):
        return
    started_at = start_row.processed_at or start_row.updated_at
    if now < started_at + timedelta(minutes=15):
        raise RetryableBeforeExternalEffect("pickup_sms_marker_pending")
    raise SmsMarkerNotConfirmed("pickup_sms_marker_not_confirmed")


def enqueue_due_sla_tasks(
    session: Session,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> int:
    now = _naive_utc(now or utcnow())
    _acquire_advisory_xact_lock(session, SLA_TASK_ADVISORY_LOCK_KEY)
    threshold = now - timedelta(hours=settings.order_fulfillment_bot_call_after_hours)
    cases = session.scalars(
        select(SiteOrderExecutionCase).where(
            SiteOrderExecutionCase.current_crm_stage == fulfillment.CRM_STAGE_PICKUP_WAITING,
            SiteOrderExecutionCase.storage_started_at.is_not(None),
            SiteOrderExecutionCase.storage_started_at <= threshold,
            SiteOrderExecutionCase.delivered_at.is_(None),
            SiteOrderExecutionCase.bitrix_deal_id.is_not(None),
        )
    ).all()
    created = 0
    for case in cases:
        key = f"case:{case.id}:task:call"
        if session.scalar(
            select(SiteOrderFulfillmentOutbox.id).where(
                SiteOrderFulfillmentOutbox.idempotency_key == key
            )
        ):
            continue
        enqueue_outbox(
            session,
            operation=OP_CREATE_TASK,
            idempotency_key=key,
            target_type="deal",
            target_id=str(case.bitrix_deal_id),
            payload={
                "task_kind": "call",
                "site_order_number": case.site_order_number,
                "expected_stage": fulfillment.CRM_STAGE_PICKUP_WAITING,
            },
            now=now,
        )
        created += 1
    session.commit()
    return created


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


def _sms_pilot_has_capacity(session: Session, *, settings: Settings) -> bool:
    reserved = int(
        session.scalar(
            select(func.count(SiteOrderFulfillmentOutbox.id)).where(
                SiteOrderFulfillmentOutbox.operation == OP_START_SMS_WORKFLOW,
            )
        )
        or 0
    )
    return reserved < settings.order_fulfillment_bot_sms_pilot_limit


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
