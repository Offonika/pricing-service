from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.logistics import LogisticsWarehouse
from app.models.site_order_fulfillment import (
    BitrixChatActionCandidate,
    BitrixChatMessage,
    BitrixChatReaction,
    PickupInventorySubmission,
    SiteOrderExecutionCase,
    SiteOrderExecutionEvent,
    SiteOrderFulfillmentOutbox,
)
from app.services import pickup_inventory
from app.services import site_order_fulfillment as fulfillment
from app.services import site_order_fulfillment_bot as bot


def poll_pickup_control_chats(
    session: Session,
    *,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], bot.OneCPickupValidation] | None = None,
    external_apply_enabled: bool | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = bot._naive_utc(now or bot.utcnow())  # noqa: SLF001
    lookback = now - timedelta(hours=settings.order_fulfillment_reaction_lookback_hours)
    sources = (
        (
            fulfillment.CHAT_SITE_MASTER_MOBILE,
            settings.order_fulfillment_site_chat_dialog_id,
        ),
        (
            fulfillment.CHAT_PICKUP_READY,
            settings.order_fulfillment_pickup_ready_chat_dialog_id,
        ),
        (
            fulfillment.CHAT_PICKUP_INVENTORY,
            settings.order_fulfillment_pickup_inventory_chat_dialog_id,
        ),
        (
            fulfillment.CHAT_PICKUP_MOVEMENT,
            settings.order_fulfillment_pickup_movement_chat_dialog_id,
        ),
        (
            fulfillment.CHAT_PICKUP_EXCEPTION,
            settings.order_fulfillment_pickup_exception_chat_dialog_id,
        ),
    )
    stats: dict[str, Any] = {}
    for chat_code, dialog_id in sources:
        stats[chat_code] = fulfillment.poll_bitrix_chat_pages(
            session,
            client=client,
            chat_code=chat_code,
            dialog_id=dialog_id,
            page_size=50,
            max_pages=settings.order_fulfillment_chat_poll_page_limit,
            lookback_since=lookback,
            run_ocr=False,
            settings=settings,
        )
    if (
        settings.order_fulfillment_pickup_evidence_tracking_enabled
        and settings.order_fulfillment_pickup_evidence_cutover_at is not None
    ):
        if onec_validator is None:
            raise RuntimeError("pickup_evidence_onec_validator_required")
        stats["evidence"] = reconcile_strict_pickup_evidence(
            session,
            client=client,
            settings=settings,
            onec_validator=onec_validator,
            now=now,
        )
    else:
        stats["evidence"] = {"checked": 0, "recorded": 0, "duplicate": 0, "blocked": 0}
    stats["inventory"] = persist_pending_inventory_messages(
        session,
        settings=settings,
        order_exists=build_crm_order_exists_probe(client),
        queue_clarification_cards=bool(
            settings.order_fulfillment_pickup_inventory_enabled
            and (
                external_apply_enabled
                if external_apply_enabled is not None
                else settings.order_fulfillment_bot_apply_enabled
            )
        ),
    )
    stats["candidates"] = create_missing_pickup_candidates(
        session,
        settings=settings,
    )
    stats["reactions"] = reconcile_trusted_notification_reactions(
        session,
        settings=settings,
        now=now,
    )
    session.commit()
    return stats


def build_crm_order_exists_probe(
    client: fulfillment.BitrixChatClient,
) -> Callable[[str], bool]:
    """Build a cached, fail-closed existence check for glued order numbers."""

    cache: dict[str, bool] = {}

    def exists_in_crm(order_number: str) -> bool:
        if order_number in cache:
            return cache[order_number]
        try:
            cache[order_number] = len(client.list_deals_by_site_order(order_number)) == 1
        except Exception:
            cache[order_number] = False
        return cache[order_number]

    return exists_in_crm


def reconcile_strict_pickup_evidence(
    session: Session,
    *,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], bot.OneCPickupValidation],
    limit: int = 1000,
    now: datetime | None = None,
) -> dict[str, int]:
    """Persist strict dispatch/receipt evidence without changing CRM or sending messages."""

    stats = {"checked": 0, "recorded": 0, "duplicate": 0, "blocked": 0}
    cutover = settings.order_fulfillment_pickup_evidence_cutover_at
    if not settings.order_fulfillment_pickup_evidence_tracking_enabled or cutover is None:
        return stats
    now = bot._naive_utc(now or bot.utcnow())  # noqa: SLF001
    cutoff_naive = bot._naive_utc(cutover)  # noqa: SLF001
    messages = session.scalars(
        select(BitrixChatMessage)
        .where(
            BitrixChatMessage.chat_code.in_(
                [
                    fulfillment.CHAT_SITE_MASTER_MOBILE,
                    fulfillment.CHAT_PICKUP_READY,
                ]
            ),
            BitrixChatMessage.message_at >= cutoff_naive,
            BitrixChatMessage.parse_status != "edited_manual_review",
        )
        .order_by(BitrixChatMessage.message_at.asc(), BitrixChatMessage.id.asc())
        .limit(max(1, min(limit, 5000)))
    ).all()
    excluded_authors = {
        str(item).strip().casefold() for item in settings.order_fulfillment_bot_excluded_user_ids
    }
    if settings.order_fulfillment_bot_id is not None:
        excluded_authors.add(str(settings.order_fulfillment_bot_id))
        excluded_authors.add(f"bot{settings.order_fulfillment_bot_id}")
    for message in messages:
        stats["checked"] += 1
        if str(message.author_id or "").strip().casefold() in excluded_authors:
            continue
        text_value = fulfillment.bitrix_message_text_for_parsing(message)
        mentions = bot.parse_pickup_candidate_text(
            text_value,
            dialog_id=message.dialog_id,
            order_limit=bot.STRICT_ARRIVAL_ORDER_LIMIT,
        )
        resolution = pickup_inventory.resolve_pickup_inventory_warehouse(
            session,
            text_value,
            pickup_aliases=settings.order_fulfillment_pickup_warehouse_aliases,
        )
        if message.chat_code == fulfillment.CHAT_SITE_MASTER_MOBILE:
            event_type = fulfillment.EVENT_PICKUP_MOVING
            strict = bot.strict_pickup_movement_message(
                text_value,
                mentions=mentions,
                resolution=resolution,
            )
        else:
            event_type = fulfillment.EVENT_PICKUP_STORED
            strict = bot._strict_pickup_arrival_message(  # noqa: SLF001
                text_value,
                mentions=mentions,
                resolution=resolution,
            )
        if not strict or resolution.warehouse is None:
            continue
        for mention in mentions:
            existing = session.scalar(
                select(SiteOrderExecutionEvent.id)
                .join(
                    SiteOrderExecutionCase,
                    SiteOrderExecutionCase.id == SiteOrderExecutionEvent.case_id,
                )
                .where(
                    SiteOrderExecutionEvent.raw_message_id == message.id,
                    SiteOrderExecutionEvent.event_type == event_type,
                    SiteOrderExecutionCase.site_order_number == mention.site_order_number,
                )
            )
            if existing is not None:
                stats["duplicate"] += 1
                continue
            try:
                deals = client.list_deals_by_site_order(mention.site_order_number)
            except Exception:
                stats["blocked"] += 1
                continue
            if len(deals) != 1:
                stats["blocked"] += 1
                continue
            deal = deals[0]
            if fulfillment._clean_string(
                deal.stage_id
            ) in bot.TERMINAL_STAGES or not fulfillment._is_internal_pickup_deal(
                deal
            ):  # noqa: SLF001
                stats["blocked"] += 1
                continue
            case = session.scalar(
                select(SiteOrderExecutionCase).where(
                    SiteOrderExecutionCase.site_order_number == mention.site_order_number
                )
            )
            expected_ids = bot.pickup_expected_warehouse_ids(
                session,
                case=case,
                deal=deal,
                settings=settings,
            )
            if expected_ids != {resolution.warehouse.id}:
                stats["blocked"] += 1
                continue
            onec = onec_validator(mention.site_order_number)
            if not onec.available or not onec.assembled or onec.return_confirmed:
                stats["blocked"] += 1
                continue
            event = fulfillment.upsert_execution_event(
                session,
                site_order_number=mention.site_order_number,
                event_type=event_type,
                event_at=message.message_at or now,
                source="bitrix_chat",
                source_ref=f"pickup_evidence:{message.id}",
                confidence="strong",
                raw_message_id=message.id,
                warehouse_id=resolution.warehouse.id,
                actor_ref=message.author_id,
                payload={"strict": True, "silent": True},
            )
            if event is None:
                stats["duplicate"] += 1
                continue
            case = session.scalar(
                select(SiteOrderExecutionCase).where(
                    SiteOrderExecutionCase.site_order_number == mention.site_order_number
                )
            )
            assert case is not None
            case.bitrix_deal_id = deal.deal_id
            case.delivery_method = deal.delivery
            case.current_crm_stage = deal.stage_id
            case.pickup_point_warehouse_id = resolution.warehouse.id
            if event_type == fulfillment.EVENT_PICKUP_STORED:
                event_at = message.message_at or now
                if case.storage_started_at is None or event_at > case.storage_started_at:
                    case.storage_started_at = event_at
            case.updated_at = now
            stats["recorded"] += 1
    session.commit()
    return stats


def enqueue_missing_receipt_followups(
    session: Session,
    *,
    settings: Settings,
    limit: int = 1000,
    now: datetime | None = None,
) -> dict[str, int]:
    """Find dispatches without a later receipt and queue one question plus later tasks."""

    stats = {
        "checked": 0,
        "due": 0,
        "prompt_queued": 0,
        "task_queued": 0,
        "dry_run": 0,
    }
    cutover = settings.order_fulfillment_pickup_evidence_cutover_at
    if not settings.order_fulfillment_pickup_evidence_tracking_enabled or cutover is None:
        return stats
    now = bot._naive_utc(now or bot.utcnow())  # noqa: SLF001
    question_hours = settings.order_fulfillment_pickup_receipt_question_after_hours
    task_hours = max(
        question_hours,
        settings.order_fulfillment_pickup_receipt_task_after_hours,
    )
    cutoff_naive = bot._naive_utc(cutover)  # noqa: SLF001
    movements = session.scalars(
        select(SiteOrderExecutionEvent)
        .where(
            SiteOrderExecutionEvent.event_type.in_(
                [fulfillment.EVENT_PICKUP_MOVING, fulfillment.EVENT_PICKUP_REDIRECTED]
            ),
            SiteOrderExecutionEvent.event_at >= cutoff_naive,
            SiteOrderExecutionEvent.event_at <= now - timedelta(hours=question_hours),
        )
        .order_by(SiteOrderExecutionEvent.event_at.asc(), SiteOrderExecutionEvent.id.asc())
        .limit(max(1, min(limit, 5000)))
    ).all()
    prompt_groups: dict[tuple[int, int], list[SiteOrderExecutionEvent]] = {}
    task_movements: list[SiteOrderExecutionEvent] = []
    for movement in movements:
        stats["checked"] += 1
        if not bot.missing_receipt_movement_is_open(session, movement=movement):
            continue
        case = session.get(SiteOrderExecutionCase, movement.case_id)
        if (
            case is None
            or case.bitrix_deal_id is None
            or movement.warehouse_id is None
            or case.delivered_at is not None
            or case.cancelled_at is not None
        ):
            continue
        stats["due"] += 1
        fulfillment.upsert_execution_event(
            session,
            site_order_number=case.site_order_number,
            event_type=fulfillment.EVENT_PICKUP_RECEIPT_OVERDUE,
            event_at=(movement.event_at or now) + timedelta(hours=question_hours),
            source="system",
            source_ref=f"movement:{movement.id}",
            confidence="strong",
            raw_message_id=movement.raw_message_id,
            warehouse_id=movement.warehouse_id,
            payload={"movement_event_id": movement.id},
        )
        if not settings.order_fulfillment_pickup_missing_receipt_enabled:
            stats["dry_run"] += 1
            continue
        group_source_id = movement.raw_message_id or movement.id
        prompt_groups.setdefault((group_source_id, movement.warehouse_id), []).append(movement)
        if movement.event_at is not None and movement.event_at <= now - timedelta(hours=task_hours):
            task_movements.append(movement)
    for (source_id, warehouse_id), grouped_movements in prompt_groups.items():
        key = f"movement-source:{source_id}:warehouse:{warehouse_id}:missing-receipt:prompt"
        if (
            session.scalar(
                select(SiteOrderFulfillmentOutbox.id).where(
                    SiteOrderFulfillmentOutbox.idempotency_key == key
                )
            )
            is not None
        ):
            continue
        bot.enqueue_outbox(
            session,
            operation=bot.OP_PUBLISH_MISSING_RECEIPT_PROMPT,
            idempotency_key=key,
            target_type="pickup_movement_batch",
            target_id=str(source_id),
            payload={"movement_event_ids": [item.id for item in grouped_movements]},
            now=now,
        )
        stats["prompt_queued"] += 1
    for movement in task_movements:
        key = f"movement:{movement.id}:missing-receipt:task"
        if (
            session.scalar(
                select(SiteOrderFulfillmentOutbox.id).where(
                    SiteOrderFulfillmentOutbox.idempotency_key == key
                )
            )
            is not None
        ):
            continue
        bot.enqueue_outbox(
            session,
            operation=bot.OP_CREATE_MISSING_RECEIPT_TASK,
            idempotency_key=key,
            target_type="pickup_movement_event",
            target_id=str(movement.id),
            payload={"movement_event_id": movement.id},
            now=now,
        )
        stats["task_queued"] += 1
    session.commit()
    return stats


def create_missing_pickup_candidates(
    session: Session,
    *,
    settings: Settings,
    limit: int = 500,
) -> dict[str, int]:
    cutover = settings.order_fulfillment_bot_cutover_at
    stats = {"checked": 0, "created": 0}
    if cutover is None:
        return stats
    cutoff_naive = bot._naive_utc(cutover)  # noqa: SLF001
    message_filters = [
        BitrixChatMessage.dialog_id.in_(settings.order_fulfillment_bot_source_chat_ids),
        BitrixChatMessage.message_at >= cutoff_naive,
        BitrixChatMessage.parse_status != "edited_manual_review",
        ~exists().where(BitrixChatActionCandidate.raw_message_id == BitrixChatMessage.id),
    ]
    if (
        settings.order_fulfillment_pickup_evidence_tracking_enabled
        and settings.order_fulfillment_pickup_evidence_cutover_at is not None
    ):
        message_filters.append(
            ~exists().where(SiteOrderExecutionEvent.raw_message_id == BitrixChatMessage.id)
        )
    messages = session.scalars(
        select(BitrixChatMessage)
        .where(*message_filters)
        .order_by(BitrixChatMessage.message_at.asc(), BitrixChatMessage.id.asc())
        .limit(max(1, min(limit, 5000)))
    ).all()
    for message in messages:
        stats["checked"] += 1
        created = bot.create_candidates_from_message(
            session,
            dialog_id=message.dialog_id,
            message_id=str(message.message_id),
            author_id=message.author_id,
            text_value=fulfillment.bitrix_message_text_for_parsing(message),
            message_at=message.message_at,
            settings=settings,
            payload=message.payload or {},
        )
        stats["created"] += len(created)
    return stats


def persist_pending_inventory_messages(
    session: Session,
    *,
    limit: int = 1000,
    order_exists: Callable[[str], bool] | None = None,
    settings: Settings | None = None,
    queue_clarification_cards: bool = False,
) -> dict[str, int]:
    messages = session.scalars(
        select(BitrixChatMessage)
        .where(
            BitrixChatMessage.chat_code == fulfillment.CHAT_PICKUP_INVENTORY,
            ~exists().where(PickupInventorySubmission.source_message_id == BitrixChatMessage.id),
            BitrixChatMessage.parse_status.not_in(
                ["inventory_manual_review", "edited_manual_review"]
            ),
        )
        .order_by(BitrixChatMessage.message_at.asc(), BitrixChatMessage.id.asc())
        .limit(max(1, min(limit, 5000)))
    ).all()
    stats = {"checked": 0, "confirmed": 0, "manual_review": 0, "cards_queued": 0}
    for message in messages:
        stats["checked"] += 1
        submission = pickup_inventory.persist_inventory_message(
            session,
            message=message,
            order_exists=order_exists,
            pickup_aliases=(
                settings.order_fulfillment_pickup_warehouse_aliases
                if settings is not None
                else None
            ),
        )
        if submission is None:
            message.parse_status = "inventory_manual_review"
            stats["manual_review"] += 1
            continue
        if submission.status == pickup_inventory.STATUS_CONFIRMED:
            message.parse_status = "inventory_confirmed"
            stats["confirmed"] += 1
        else:
            message.parse_status = "inventory_manual_review"
            stats["manual_review"] += 1
            if queue_clarification_cards and settings is not None:
                bot.enqueue_inventory_clarification_card(
                    session,
                    submission=submission,
                    settings=settings,
                )
                stats["cards_queued"] += 1
    session.flush()
    return stats


def reconcile_trusted_notification_reactions(
    session: Session,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> dict[str, int]:
    now = bot._naive_utc(now or bot.utcnow())  # noqa: SLF001
    cutover = settings.order_fulfillment_bot_cutover_at
    trusted = {str(value) for value in settings.order_fulfillment_pickup_notification_confirmer_ids}
    stats = {"checked": 0, "confirmed": 0, "revoked": 0, "ignored": 0}
    if cutover is None or not trusted:
        return stats
    cutoff_naive = bot._naive_utc(cutover)  # noqa: SLF001
    reactions = session.scalars(
        select(BitrixChatReaction)
        .join(BitrixChatMessage, BitrixChatMessage.id == BitrixChatReaction.message_id)
        .where(
            BitrixChatMessage.chat_code == fulfillment.CHAT_PICKUP_READY,
            BitrixChatMessage.message_at >= cutoff_naive,
            BitrixChatReaction.actor_id.in_(trusted),
            BitrixChatReaction.reaction == "like",
            BitrixChatReaction.is_active.is_(True),
        )
        .order_by(BitrixChatReaction.first_seen_at.asc())
    ).all()
    active_message_ids: set[int] = set()
    for reaction in reactions:
        stats["checked"] += 1
        active_message_ids.add(reaction.message_id)
        message = session.get(BitrixChatMessage, reaction.message_id)
        if message is None:
            stats["ignored"] += 1
            continue
        orders = fulfillment.bitrix_message_order_numbers(message)
        if not orders:
            stats["ignored"] += 1
            continue
        for order_number in orders:
            case = session.scalar(
                select(SiteOrderExecutionCase).where(
                    SiteOrderExecutionCase.site_order_number == order_number
                )
            )
            if case is None or case.storage_started_at is None:
                stats["ignored"] += 1
                continue
            if (case.payload or {}).get("notification_source") == "sms_marker":
                continue
            confirmed_at = reaction.first_seen_at
            if case.notification_confirmed_at is None:
                case.notification_confirmed_at = confirmed_at
                case.sla_started_at = max(case.storage_started_at, confirmed_at)
                case.storage_deadline_at = case.sla_started_at + timedelta(
                    hours=settings.order_fulfillment_bot_dismantle_after_hours
                )
                case.payload = {
                    **(case.payload or {}),
                    "notification_source": "chat_reaction",
                    "notification_message_id": message.id,
                    "notification_actor_id": reaction.actor_id,
                }
                case.updated_at = now
                fulfillment.upsert_execution_event(
                    session,
                    site_order_number=order_number,
                    event_type=fulfillment.EVENT_PICKUP_NOTIFICATION_CONFIRMED,
                    event_at=confirmed_at,
                    source="bitrix_chat",
                    source_ref=f"reaction:{reaction.id}",
                    confidence="strong",
                    raw_message_id=message.id,
                    actor_ref=reaction.actor_id,
                    payload={"reaction": reaction.reaction},
                )
                if case.bitrix_deal_id is not None:
                    bot.enqueue_outbox(
                        session,
                        operation=bot.OP_UPDATE_CRM_FIELDS,
                        idempotency_key=f"case:{case.id}:reaction:{reaction.id}:crm-fields",
                        target_type="deal",
                        target_id=str(case.bitrix_deal_id),
                        payload={
                            "site_order_number": case.site_order_number,
                            "fields": {
                                bot.CRM_PICKUP_SLA_STARTED_FIELD: (
                                    bot.crm_datetime_iso(case.sla_started_at)
                                    if case.sla_started_at is not None
                                    else ""
                                ),
                                bot.CRM_PICKUP_DERIVED_STATUS_FIELD: (
                                    fulfillment.EVENT_PICKUP_STORED
                                ),
                                bot.CRM_PICKUP_LAST_EVIDENCE_FIELD: (
                                    "trusted_notification_reaction"
                                ),
                            },
                        },
                        now=now,
                    )
                stats["confirmed"] += 1
    reaction_cases = session.scalars(
        select(SiteOrderExecutionCase).where(
            SiteOrderExecutionCase.notification_confirmed_at.is_not(None)
        )
    ).all()
    for case in reaction_cases:
        if (case.payload or {}).get("notification_source") != "chat_reaction":
            continue
        if (case.payload or {}).get("notification_reaction_revoked_at"):
            continue
        message_id = int((case.payload or {}).get("notification_message_id") or 0)
        if message_id <= 0 or message_id in active_message_ids:
            continue
        case.payload = {
            **(case.payload or {}),
            "notification_reaction_revoked_at": now.isoformat(),
        }
        fulfillment.upsert_execution_event(
            session,
            site_order_number=case.site_order_number,
            event_type=fulfillment.EVENT_PICKUP_NOTIFICATION_REVOKED,
            event_at=now,
            source="bitrix_chat",
            source_ref=f"reaction-revoked:{message_id}",
            confidence="strong",
            raw_message_id=message_id,
            payload={},
        )
        case.current_derived_status = "manual_review"
        case.confidence = "weak"
        if case.bitrix_deal_id is not None:
            confirmation_row = session.scalar(
                select(SiteOrderFulfillmentOutbox)
                .where(
                    SiteOrderFulfillmentOutbox.idempotency_key.like(
                        f"case:{case.id}:reaction:%:crm-fields"
                    )
                )
                .order_by(SiteOrderFulfillmentOutbox.id.desc())
            )
            bot.enqueue_outbox(
                session,
                depends_on=confirmation_row,
                operation=bot.OP_UPDATE_CRM_FIELDS,
                idempotency_key=f"case:{case.id}:reaction-revoked:{message_id}:crm-fields",
                target_type="deal",
                target_id=str(case.bitrix_deal_id),
                payload={
                    "site_order_number": case.site_order_number,
                    "fields": {
                        bot.CRM_PICKUP_DERIVED_STATUS_FIELD: "manual_review",
                        bot.CRM_PICKUP_LAST_EVIDENCE_FIELD: (
                            "trusted_notification_reaction_revoked"
                        ),
                    },
                },
                now=now,
            )
        stats["revoked"] += 1
    session.flush()
    return stats


def pickup_operational_metrics(
    session: Session,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = bot._naive_utc(now or bot.utcnow())  # noqa: SLF001
    expected_chat_codes = (
        fulfillment.CHAT_SITE_MASTER_MOBILE,
        fulfillment.CHAT_PICKUP_READY,
        fulfillment.CHAT_PICKUP_INVENTORY,
        fulfillment.CHAT_PICKUP_MOVEMENT,
        fulfillment.CHAT_PICKUP_EXCEPTION,
        fulfillment.CHAT_COURIER_SPB,
    )
    stored_freshness = {
        chat_code: message_at.isoformat() if isinstance(message_at, datetime) else None
        for chat_code, message_at in session.execute(
            select(BitrixChatMessage.chat_code, func.max(BitrixChatMessage.message_at)).group_by(
                BitrixChatMessage.chat_code
            )
        ).all()
    }
    chat_freshness = {
        chat_code: stored_freshness.get(chat_code) for chat_code in expected_chat_codes
    }
    cutover = settings.order_fulfillment_bot_cutover_at
    evidence_cutover = settings.order_fulfillment_pickup_evidence_cutover_at
    cutover_naive = bot._naive_utc(cutover) if cutover is not None else None  # noqa: SLF001
    evidence_cutover_naive = (
        bot._naive_utc(evidence_cutover) if evidence_cutover is not None else None  # noqa: SLF001
    )
    current_case_filters = [
        SiteOrderExecutionCase.current_crm_stage == fulfillment.CRM_STAGE_PICKUP_WAITING,
    ]
    if cutover_naive is None:
        current_case_filters.append(SiteOrderExecutionCase.id < 0)
    else:
        current_case_filters.extend(
            [
                SiteOrderExecutionCase.storage_started_at.is_not(None),
                SiteOrderExecutionCase.storage_started_at >= cutover_naive,
            ]
        )
    inventory_confirmed = int(
        session.scalar(
            select(func.count(PickupInventorySubmission.id)).where(
                PickupInventorySubmission.status == pickup_inventory.STATUS_CONFIRMED
            )
        )
        or 0
    )
    inventory_manual_review = int(
        session.scalar(
            select(func.count(PickupInventorySubmission.id)).where(
                PickupInventorySubmission.status == pickup_inventory.STATUS_MANUAL_REVIEW
            )
        )
        or 0
    )
    pickup_without_notification = int(
        session.scalar(
            select(func.count(SiteOrderExecutionCase.id)).where(
                *current_case_filters,
                SiteOrderExecutionCase.storage_started_at.is_not(None),
                SiteOrderExecutionCase.notification_confirmed_at.is_(None),
            )
        )
        or 0
    )
    active_holds = int(
        session.scalar(
            select(func.count(SiteOrderExecutionCase.id)).where(
                *current_case_filters,
                SiteOrderExecutionCase.hold_until.is_not(None),
                SiteOrderExecutionCase.hold_until > bot._moscow_date(now),  # noqa: SLF001
            )
        )
        or 0
    )
    sla_72_due = int(
        session.scalar(
            select(func.count(SiteOrderExecutionCase.id)).where(
                *current_case_filters,
                SiteOrderExecutionCase.sla_started_at.is_not(None),
                SiteOrderExecutionCase.sla_started_at
                <= now - timedelta(hours=settings.order_fulfillment_bot_call_after_hours),
            )
        )
        or 0
    )
    sla_96_due = int(
        session.scalar(
            select(func.count(SiteOrderExecutionCase.id)).where(
                *current_case_filters,
                SiteOrderExecutionCase.sla_started_at.is_not(None),
                SiteOrderExecutionCase.sla_started_at
                <= now - timedelta(hours=settings.order_fulfillment_bot_dismantle_after_hours),
                SiteOrderExecutionCase.hold_until.is_(None),
            )
        )
        or 0
    )
    lost_orders = int(
        session.scalar(
            select(func.count(func.distinct(SiteOrderExecutionEvent.case_id)))
            .join(
                SiteOrderExecutionCase,
                SiteOrderExecutionCase.id == SiteOrderExecutionEvent.case_id,
            )
            .where(
                SiteOrderExecutionEvent.event_type == fulfillment.EVENT_PICKUP_EXCEPTION,
                SiteOrderExecutionCase.current_crm_stage == fulfillment.CRM_STAGE_PICKUP_WAITING,
                SiteOrderExecutionCase.current_derived_status == "manual_review",
            )
        )
        or 0
    )
    missing_receipt_due = 0
    if evidence_cutover_naive is not None:
        due_movements = session.scalars(
            select(SiteOrderExecutionEvent).where(
                SiteOrderExecutionEvent.event_type.in_(
                    [
                        fulfillment.EVENT_PICKUP_MOVING,
                        fulfillment.EVENT_PICKUP_REDIRECTED,
                    ]
                ),
                SiteOrderExecutionEvent.event_at >= evidence_cutover_naive,
                SiteOrderExecutionEvent.event_at
                <= now
                - timedelta(hours=settings.order_fulfillment_pickup_receipt_question_after_hours),
            )
        ).all()
        missing_receipt_due = sum(
            1
            for movement in due_movements
            if bot.missing_receipt_movement_is_open(session, movement=movement)
        )
    outbox_counts = {
        status: int(count_value or 0)
        for status, count_value in session.execute(
            select(
                SiteOrderFulfillmentOutbox.status,
                func.count(SiteOrderFulfillmentOutbox.id),
            ).group_by(SiteOrderFulfillmentOutbox.status)
        ).all()
    }
    routing_errors = int(
        session.scalar(
            select(func.count(SiteOrderFulfillmentOutbox.id)).where(
                SiteOrderFulfillmentOutbox.status == bot.OUTBOX_FAILED,
                SiteOrderFulfillmentOutbox.last_error.like("%task_route_%"),
            )
        )
        or 0
    )
    return {
        "chat_freshness": chat_freshness,
        "active_reactions": int(
            session.scalar(
                select(func.count(BitrixChatReaction.id)).where(
                    BitrixChatReaction.is_active.is_(True)
                )
            )
            or 0
        ),
        "inventory_confirmed": inventory_confirmed,
        "inventory_manual_review": inventory_manual_review,
        "pickup_without_notification": pickup_without_notification,
        "sla_72_due": sla_72_due,
        "sla_96_due": sla_96_due,
        "active_holds": active_holds,
        "lost_orders": lost_orders,
        "missing_receipt_due": missing_receipt_due,
        "task_routing_errors": routing_errors,
        "task_route_configuration_errors": bot.task_route_configuration_errors(
            session,
            settings=settings,
        ),
        "outbox": outbox_counts,
    }


def enqueue_inventory_won_candidates(
    session: Session,
    *,
    client: fulfillment.BitrixChatClient,
    settings: Settings,
    onec_validator: Callable[[str], bot.OneCPickupValidation],
    limit: int = 200,
    now: datetime | None = None,
) -> dict[str, int]:
    now = bot._naive_utc(now or bot.utcnow())  # noqa: SLF001
    stats = {"checked": 0, "dry_run_ready": 0, "queued": 0, "blocked": 0}
    submissions = session.scalars(
        select(PickupInventorySubmission)
        .where(PickupInventorySubmission.status == pickup_inventory.STATUS_CONFIRMED)
        .order_by(PickupInventorySubmission.submitted_at.desc())
        .limit(max(1, min(limit, 1000)))
    ).all()
    for submission in submissions:
        for candidate in pickup_inventory.disappearance_candidates(
            session, current_submission=submission
        ):
            stats["checked"] += 1
            uncontested, _ = pickup_inventory.disappearance_is_uncontested(
                session,
                candidate=candidate,
            )
            if not uncontested:
                stats["blocked"] += 1
                continue
            case = session.scalar(
                select(SiteOrderExecutionCase).where(
                    SiteOrderExecutionCase.site_order_number == candidate.site_order_number
                )
            )
            if (
                case is None
                or case.current_crm_stage != fulfillment.CRM_STAGE_PICKUP_WAITING
                or case.pickup_point_warehouse_id != candidate.warehouse_id
                or not bot._case_is_after_cutover(case, settings=settings)  # noqa: SLF001
            ):
                stats["blocked"] += 1
                continue
            warehouse = session.get(LogisticsWarehouse, candidate.warehouse_id)
            if warehouse is None:
                stats["blocked"] += 1
                continue
            onec = onec_validator(candidate.site_order_number)
            if not onec.available or not onec.assembled or onec.return_confirmed:
                stats["blocked"] += 1
                continue
            deals = client.list_deals_by_site_order(candidate.site_order_number)
            if len(deals) != 1:
                stats["blocked"] += 1
                continue
            deal = deals[0]
            if (
                deal.deal_id != case.bitrix_deal_id
                or deal.stage_id != fulfillment.CRM_STAGE_PICKUP_WAITING
                or not fulfillment._is_internal_pickup_deal(deal)  # noqa: SLF001
            ):
                stats["blocked"] += 1
                continue
            if (
                not settings.order_fulfillment_pickup_inventory_enabled
                or not settings.order_fulfillment_inventory_won_enabled
            ):
                stats["dry_run_ready"] += 1
                continue
            if warehouse.external_id not in set(
                settings.order_fulfillment_inventory_won_warehouse_external_ids
            ):
                stats["blocked"] += 1
                continue
            source_key = (
                f"inventory:{candidate.previous_submission_id}:"
                f"{candidate.current_submission_id}:{candidate.site_order_number}"
            )
            stage_row = bot.enqueue_outbox(
                session,
                operation=bot.OP_UPDATE_CRM_STAGE,
                idempotency_key=f"{source_key}:crm:WON",
                target_type="deal",
                target_id=str(deal.deal_id),
                payload={
                    "site_order_number": candidate.site_order_number,
                    "before_stage": fulfillment.CRM_STAGE_PICKUP_WAITING,
                    "target_stage": "WON",
                    "feature_guard": "inventory_won",
                    "inventory_previous_submission_id": candidate.previous_submission_id,
                    "inventory_current_submission_id": candidate.current_submission_id,
                },
                now=now,
            )
            final_row = bot.enqueue_outbox(
                session,
                depends_on=stage_row,
                operation=bot.OP_FINALIZE_CASE_EVENT,
                idempotency_key=f"{source_key}:finalize",
                target_type="deal",
                target_id=str(deal.deal_id),
                payload={
                    "site_order_number": candidate.site_order_number,
                    "event_type": fulfillment.EVENT_PICKUP_RECEIVED,
                    "event_at": candidate.current_at.isoformat(),
                    "source": "pickup_inventory",
                    "source_ref": source_key,
                    "confidence": "strong",
                    "warehouse_id": candidate.warehouse_id,
                    "feature_guard": "inventory_won",
                    "inventory_previous_submission_id": candidate.previous_submission_id,
                    "inventory_current_submission_id": candidate.current_submission_id,
                    "evidence": {
                        "previous_submission_id": candidate.previous_submission_id,
                        "current_submission_id": candidate.current_submission_id,
                    },
                },
                now=now,
            )
            bot.enqueue_outbox(
                session,
                depends_on=final_row,
                operation=bot.OP_UPDATE_CRM_FIELDS,
                idempotency_key=f"{source_key}:crm-fields",
                target_type="deal",
                target_id=str(deal.deal_id),
                payload={
                    "site_order_number": candidate.site_order_number,
                    "feature_guard": "inventory_won",
                    "inventory_previous_submission_id": candidate.previous_submission_id,
                    "inventory_current_submission_id": candidate.current_submission_id,
                    "fields": {
                        bot.CRM_PICKUP_DERIVED_STATUS_FIELD: fulfillment.EVENT_PICKUP_RECEIVED,
                        bot.CRM_PICKUP_LAST_EVIDENCE_FIELD: "confirmed_inventory_disappearance",
                    },
                },
                now=now,
            )
            stats["queued"] += 1
    session.commit()
    return stats
