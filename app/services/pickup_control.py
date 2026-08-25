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
    messages = session.scalars(
        select(BitrixChatMessage)
        .where(
            BitrixChatMessage.dialog_id.in_(settings.order_fulfillment_bot_source_chat_ids),
            BitrixChatMessage.message_at >= cutoff_naive,
            BitrixChatMessage.parse_status != "edited_manual_review",
            ~exists().where(BitrixChatActionCandidate.raw_message_id == BitrixChatMessage.id),
        )
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
    cutover_naive = bot._naive_utc(cutover) if cutover is not None else None  # noqa: SLF001
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
