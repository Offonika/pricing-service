from __future__ import annotations

import warnings
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SAWarning

from app.core.config import Settings
from app.models.logistics import LogisticsWarehouse
from app.models.site_order_fulfillment import (
    BitrixChatAction,
    BitrixChatActionCandidate,
    BitrixChatMessage,
    SiteOrderExecutionCase,
    SiteOrderExecutionEvent,
    SiteOrderFulfillmentOutbox,
)
from app.services import site_order_fulfillment as fulfillment
from app.services import site_order_fulfillment_bot as bot
from infra.cron import order_fulfillment_sync as fulfillment_sync
from scripts import process_order_fulfillment_bot_outbox as outbox_script


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "order_fulfillment_bot_enabled": True,
        "order_fulfillment_bot_apply_enabled": True,
        "order_fulfillment_pickup_stage_apply_enabled": True,
        "order_fulfillment_pickup_sla_enabled": True,
        "order_fulfillment_pickup_inventory_enabled": True,
        "order_fulfillment_bot_sms_enabled": False,
        "order_fulfillment_bot_source_chat_ids": ["chat8729", "chat733"],
        "order_fulfillment_bot_callback_secret": "test-secret",
        "order_fulfillment_bot_application_token": "app-token",
        "order_fulfillment_bot_allowed_domains": ["crm.example"],
        "order_fulfillment_bot_allowed_member_ids": ["member-1"],
        "order_fulfillment_bot_id": 42,
        "order_fulfillment_bot_client_id": "pickup-bot",
        "order_fulfillment_bot_command_id": 103,
        "order_fulfillment_bot_dry_run_card_limit": 0,
        "order_fulfillment_internet_shop_task_responsible_id": 115204,
        "order_fulfillment_site_return_task_responsible_id": 115204,
        "order_fulfillment_point_task_routes": {"mitino": {"operator": 115204, "senior": 115204}},
    }
    values.update(overrides)
    return Settings(**values)


class FakeBitrixClient:
    def __init__(
        self,
        *,
        order_number: str = "241500",
        stage: str = "FINAL_INVOICE",
        delivery: str = "Самовывоз",
        participants: set[str] | None = None,
    ) -> None:
        self.order_number = order_number
        self.stage = stage
        self.delivery = delivery
        self.participants = participants or {"7"}
        self.bot_messages: list[dict] = []
        self.bot_updates: list[dict] = []
        self.stage_updates: list[str] = []
        self.workflows: list[dict] = []
        self.tasks: list[dict] = []
        self.raw = {
            fulfillment.CRM_ORDER_NUMBER_FIELD: order_number,
            "UF_CRM_MM_PICKUP_READY_SMS_AT": "",
            "UF_CRM_MM_READY_TRACK_SMS_AT": "2026-08-22T12:00:00",
        }

    def _deal(self) -> fulfillment.BitrixDealSnapshot:
        return fulfillment.BitrixDealSnapshot(
            deal_id=500,
            stage_id=self.stage,
            delivery=self.delivery,
            raw=dict(self.raw),
        )

    def list_deals_by_site_order(self, site_order_number: str):
        return [self._deal()] if site_order_number == self.order_number else []

    def get_deal_by_id(self, deal_id: int):
        return self._deal() if deal_id == 500 else None

    def list_dialog_user_ids(self, dialog_id: str) -> set[str]:
        return set(self.participants)

    def add_bot_message(self, **payload):
        self.bot_messages.append(payload)
        return "9001"

    def update_bot_message(self, **payload):
        self.bot_updates.append(payload)
        return True

    def update_deal_stage(self, deal_id: int, target_stage: str):
        assert deal_id == 500
        self.stage_updates.append(target_stage)
        self.stage = target_stage
        return True

    def update_deal_fields(self, deal_id: int, fields: dict):
        assert deal_id == 500
        if "STAGE_ID" in fields:
            self.stage = str(fields["STAGE_ID"])
        self.raw.update(fields)
        return True

    def start_business_process(self, **payload):
        self.workflows.append(payload)
        return "workflow-1"

    def add_task(self, fields):
        self.tasks.append(fields)
        return {"task": {"id": 1}}

    def get_user_by_id(self, user_id: int):
        return {"ID": str(user_id), "ACTIVE": "Y"}


def _warehouse(db_session) -> LogisticsWarehouse:
    warehouse = LogisticsWarehouse(
        external_id="mitino",
        name="Митино магазин",
        kind="retail",
        is_active=True,
        payload={"aliases": ["Митино"]},
    )
    db_session.add(warehouse)
    db_session.commit()
    return warehouse


def _create_candidate(
    db_session,
    *,
    settings: Settings,
    order_number: str = "241500",
    message_id: str = "1",
    message_at: datetime | None = None,
) -> BitrixChatActionCandidate:
    _warehouse(db_session)
    candidates = bot.create_candidates_from_message(
        db_session,
        dialog_id="chat8729",
        message_id=message_id,
        author_id="7",
        text_value=f"Заказ {order_number} прибыл в Митино",
        message_at=message_at or datetime(2026, 8, 23, 12, 0),
        settings=settings,
        now=datetime(2026, 8, 23, 12, 0),
    )
    return candidates[0]


def _mark_card_ready(db_session, candidate: BitrixChatActionCandidate) -> None:
    publish_row = db_session.scalar(
        select(SiteOrderFulfillmentOutbox).where(
            SiteOrderFulfillmentOutbox.idempotency_key == f"candidate:{candidate.id}:publish"
        )
    )
    assert publish_row is not None
    publish_row.status = bot.OUTBOX_COMPLETED
    candidate.bot_message_id = "9001"
    db_session.commit()


def test_pickup_parser_recognizes_actions_and_ignores_quotes() -> None:
    examples = {
        "Заказ 241500 прибыл в Митино": bot.ACTION_ARRIVED,
        "Заказ 241500 выдан клиенту": bot.ACTION_ISSUED,
        "Заказ 241500 не забрали": bot.ACTION_UNCLAIMED,
        "Заказ 241500 на расформирование": bot.ACTION_DISMANTLE,
    }
    for text, expected in examples.items():
        mentions = bot.parse_pickup_candidate_text(text, dialog_id="chat8729")
        assert mentions[0].detected_action == expected

    assert (
        bot.parse_pickup_candidate_text(
            "[QUOTE]Заказ 241500 выдан клиенту[/QUOTE]",
            dialog_id="chat8729",
        )
        == []
    )
    assert (
        bot.parse_pickup_candidate_text(
            "Заказ 241500 прибыл в ПВЗ СДЭК",
            dialog_id="chat733",
        )
        == []
    )
    assert (
        bot.parse_pickup_candidate_text(
            "Заказ №241500\nРаспознано: Прибыл в точку\nТочка: Митино магазин",
            dialog_id="chat8729",
        )
        == []
    )


def test_free_text_creates_candidate_but_no_execution_event(db_session) -> None:
    candidate = _create_candidate(db_session, settings=_settings())

    assert candidate.detected_action == bot.ACTION_ARRIVED
    assert candidate.pickup_point_name == "Митино магазин"
    assert db_session.scalar(select(func.count(SiteOrderExecutionEvent.id))) == 0
    outbox = db_session.scalar(select(SiteOrderFulfillmentOutbox))
    assert outbox is not None
    assert outbox.operation == bot.OP_PUBLISH_CARD


def test_strict_pickup_arrival_is_queued_automatically_without_card(db_session) -> None:
    settings = _settings(
        order_fulfillment_pickup_auto_arrival_enabled=True,
        order_fulfillment_bot_cutover_at=datetime(2026, 8, 23, 10, 0),
    )
    warehouse = _warehouse(db_session)

    candidates = bot.create_candidates_from_message(
        db_session,
        dialog_id="chat8729",
        message_id="22001",
        author_id="7",
        text_value="Добрый день, Митино — заказ 241500 поступил",
        message_at=datetime(2026, 8, 23, 12, 0),
        settings=settings,
        now=datetime(2026, 8, 23, 12, 1),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.status == bot.CANDIDATE_QUEUED
    assert candidate.pickup_point_warehouse_id == warehouse.id
    assert candidate.payload["automatic_arrival"] is True
    assert (
        db_session.scalar(
            select(func.count(SiteOrderFulfillmentOutbox.id)).where(
                SiteOrderFulfillmentOutbox.operation == bot.OP_PUBLISH_CARD
            )
        )
        == 0
    )
    action = db_session.scalar(select(BitrixChatAction))
    assert action is not None
    assert action.action == bot.ACTION_ARRIVED
    assert action.actor_id == "7"
    assert action.payload == {"automatic": True, "source": "chat8729"}

    client = FakeBitrixClient()
    for minute in range(1, 8):
        bot.process_outbox(
            db_session,
            client=client,
            settings=settings,
            onec_validator=lambda _: bot.OneCPickupValidation(
                available=True,
                assembled=True,
            ),
            now=datetime(2026, 8, 23, 12, minute),
        )

    db_session.refresh(candidate)
    assert candidate.status == bot.CANDIDATE_APPLIED
    assert client.bot_messages == []
    assert client.bot_updates == []
    assert client.stage_updates == [fulfillment.CRM_STAGE_PICKUP_WAITING]
    case = db_session.scalar(
        select(SiteOrderExecutionCase).where(SiteOrderExecutionCase.site_order_number == "241500")
    )
    assert case is not None
    assert case.storage_started_at == datetime(2026, 8, 23, 12, 0)
    event = db_session.scalar(
        select(SiteOrderExecutionEvent).where(
            SiteOrderExecutionEvent.event_type == fulfillment.EVENT_PICKUP_STORED
        )
    )
    assert event is not None
    assert event.source == "bitrix_chat"
    assert event.actor_ref == "7"


def test_ambiguous_or_discursive_arrival_remains_manual_candidate(db_session) -> None:
    settings = _settings(
        order_fulfillment_pickup_auto_arrival_enabled=True,
        order_fulfillment_bot_cutover_at=datetime(2026, 8, 23, 10, 0),
    )
    _warehouse(db_session)

    unresolved = bot.create_candidates_from_message(
        db_session,
        dialog_id="chat8729",
        message_id="22002",
        author_id="7",
        text_value="Заказ 241500 поступил",
        message_at=datetime(2026, 8, 23, 12, 0),
        settings=settings,
    )[0]
    discussion = bot.create_candidates_from_message(
        db_session,
        dialog_id="chat8729",
        message_id="22003",
        author_id="7",
        text_value="Что сейчас с заказом 241501 в Митино?",
        message_at=datetime(2026, 8, 23, 12, 1),
        settings=settings,
    )[0]

    assert unresolved.status == bot.CANDIDATE_OPEN
    assert unresolved.pickup_point_warehouse_id is None
    assert discussion.status == bot.CANDIDATE_OPEN
    assert (
        db_session.scalar(
            select(func.count(SiteOrderFulfillmentOutbox.id)).where(
                SiteOrderFulfillmentOutbox.operation == bot.OP_PUBLISH_CARD
            )
        )
        == 2
    )


def test_auto_arrival_replay_is_idempotent_and_late_edit_blocks_apply(db_session) -> None:
    settings = _settings(
        order_fulfillment_pickup_auto_arrival_enabled=True,
        order_fulfillment_bot_cutover_at=datetime(2026, 8, 23, 10, 0),
    )
    _warehouse(db_session)
    kwargs = {
        "dialog_id": "chat8729",
        "message_id": "22004",
        "author_id": "7",
        "text_value": "Заказ 241500 прибыл в Митино",
        "message_at": datetime(2026, 8, 23, 12, 0),
        "settings": settings,
        "now": datetime(2026, 8, 23, 12, 1),
    }

    first = bot.create_candidates_from_message(db_session, **kwargs)[0]
    repeated = bot.create_candidates_from_message(db_session, **kwargs)[0]
    assert repeated.id == first.id
    assert db_session.scalar(select(func.count(BitrixChatAction.id))) == 1
    assert db_session.scalar(select(func.count(SiteOrderFulfillmentOutbox.id))) == 1

    first.raw_message.parse_status = "edited_manual_review"
    db_session.commit()
    client = FakeBitrixClient()
    bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True, assembled=True),
        now=datetime(2026, 8, 23, 12, 2),
    )

    db_session.refresh(first)
    assert first.status == bot.CANDIDATE_REVIEW
    assert client.stage_updates == []


def test_auto_arrival_late_edit_before_finalize_blocks_internal_event(db_session) -> None:
    settings = _settings(
        order_fulfillment_pickup_auto_arrival_enabled=True,
        order_fulfillment_bot_cutover_at=datetime(2026, 8, 23, 10, 0),
    )
    _warehouse(db_session)
    candidate = bot.create_candidates_from_message(
        db_session,
        dialog_id="chat8729",
        message_id="22014",
        author_id="7",
        text_value="Заказ 241500 прибыл в Митино",
        message_at=datetime(2026, 8, 23, 12, 0),
        settings=settings,
        now=datetime(2026, 8, 23, 12, 1),
    )[0]
    client = FakeBitrixClient(stage=fulfillment.CRM_STAGE_PICKUP_WAITING)

    first_stats = bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True, assembled=True),
        limit=1,
        now=datetime(2026, 8, 23, 12, 2),
    )
    assert first_stats["completed"] == 1
    assert db_session.scalar(select(func.count(SiteOrderExecutionEvent.id))) == 0

    candidate.raw_message.parse_status = "edited_manual_review"
    db_session.commit()
    second_stats = bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True, assembled=True),
        now=datetime(2026, 8, 23, 12, 3),
    )

    db_session.refresh(candidate)
    assert second_stats["failed"] >= 1
    assert candidate.status == bot.CANDIDATE_REVIEW
    assert db_session.scalar(select(func.count(SiteOrderExecutionEvent.id))) == 0


def test_historical_arrival_never_queues_automatic_action(db_session) -> None:
    settings = _settings(
        order_fulfillment_pickup_auto_arrival_enabled=True,
        order_fulfillment_bot_cutover_at=datetime(2026, 8, 23, 12, 0),
    )
    _warehouse(db_session)

    candidate = bot.create_candidates_from_message(
        db_session,
        dialog_id="chat8729",
        message_id="22005",
        author_id="7",
        text_value="Митино 241500",
        message_at=datetime(2026, 8, 23, 11, 59),
        settings=settings,
    )[0]

    assert candidate.status == bot.CANDIDATE_OPEN
    assert db_session.scalar(select(func.count(BitrixChatAction.id))) == 0
    row = db_session.scalar(select(SiteOrderFulfillmentOutbox))
    assert row is not None and row.operation == bot.OP_PUBLISH_CARD


def test_strict_arrival_keeps_every_order_beyond_manual_card_limit(db_session) -> None:
    settings = _settings(
        order_fulfillment_pickup_auto_arrival_enabled=True,
        order_fulfillment_bot_cutover_at=datetime(2026, 8, 23, 10, 0),
    )
    _warehouse(db_session)
    order_numbers = [str(241500 + index) for index in range(12)]

    candidates = bot.create_candidates_from_message(
        db_session,
        dialog_id="chat8729",
        message_id="22006",
        author_id="7",
        text_value=f"Митино: {', '.join(order_numbers)}",
        message_at=datetime(2026, 8, 23, 12, 0),
        settings=settings,
    )

    assert [candidate.site_order_number for candidate in candidates] == order_numbers
    assert db_session.scalar(select(func.count(BitrixChatAction.id))) == 12
    assert (
        db_session.scalar(
            select(func.count(SiteOrderFulfillmentOutbox.id)).where(
                SiteOrderFulfillmentOutbox.operation == bot.OP_PROCESS_ACTION
            )
        )
        == 12
    )


def test_missing_source_date_can_never_become_sms_eligible(db_session) -> None:
    settings = _settings(
        order_fulfillment_bot_sms_enabled=True,
        order_fulfillment_bot_cutover_at=datetime(2026, 8, 23, 10, 0),
    )
    _warehouse(db_session)

    candidate = bot.create_candidates_from_message(
        db_session,
        dialog_id="chat8729",
        message_id="2001",
        author_id="7",
        text_value="Заказ 241500 прибыл в Митино",
        message_at=None,
        settings=settings,
        now=datetime(2026, 8, 23, 12, 0),
    )[0]

    assert candidate.source_event_at is None
    assert candidate.raw_message is not None
    assert candidate.raw_message.message_at is None
    assert bot._sms_candidate_is_new(candidate, settings=settings) is False  # noqa: SLF001


def test_runtime_apply_switch_marks_new_candidate_as_dry_run(db_session) -> None:
    settings = _settings(order_fulfillment_bot_apply_enabled=True)
    _warehouse(db_session)

    candidate = bot.create_candidates_from_message(
        db_session,
        dialog_id="chat8729",
        message_id="2002",
        author_id="7",
        text_value="Заказ 241500 прибыл в Митино",
        message_at=datetime(2026, 8, 23, 12, 0),
        settings=settings,
        apply_enabled_probe=lambda: False,
        now=datetime(2026, 8, 23, 12, 0),
    )[0]

    assert candidate.dry_run is True
    assert bot.card_text(candidate).startswith("Тест — без изменений")


def test_closed_deal_card_is_published_without_action_buttons(db_session) -> None:
    settings = _settings()
    candidate = _create_candidate(db_session, settings=settings)
    client = FakeBitrixClient(stage="WON")

    bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True),
        now=datetime(2026, 8, 23, 12, 1),
    )

    db_session.refresh(candidate)
    assert candidate.status == bot.CANDIDATE_REVIEW
    assert client.bot_messages[0]["keyboard"] == []
    assert "сделка уже закрыта" in client.bot_messages[0]["message"]


def test_stale_non_retryable_outbox_is_sent_to_manual_review(db_session) -> None:
    settings = _settings()
    _create_candidate(db_session, settings=settings)
    outbox = db_session.scalar(select(SiteOrderFulfillmentOutbox))
    assert outbox is not None
    outbox.status = bot.OUTBOX_PROCESSING
    outbox.updated_at = datetime(2026, 8, 23, 11, 0)
    db_session.commit()

    stats = bot.process_outbox(
        db_session,
        client=FakeBitrixClient(),
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True),
        now=datetime(2026, 8, 23, 12, 0),
    )

    assert stats["recovered"] == 1
    assert stats["completed"] == 0
    assert stats["failed"] == 1
    db_session.refresh(outbox)
    assert outbox.status == bot.OUTBOX_FAILED
    assert outbox.last_error == "ambiguous_external_result_manual_review"


def test_apply_disabled_suspends_existing_side_effect_outbox_without_attempts(
    db_session,
) -> None:
    now = datetime(2026, 8, 23, 12, 0)
    pending_rows = [
        bot.enqueue_outbox(
            db_session,
            operation=operation,
            idempotency_key=f"apply-disabled:{operation}",
            payload={},
            now=now,
        )
        for operation in sorted(bot.APPLY_GATED_OUTBOX_OPERATIONS)
    ]
    stale_row = bot.enqueue_outbox(
        db_session,
        operation=bot.OP_CREATE_TASK,
        idempotency_key="apply-disabled:stale-task",
        payload={},
        now=now - timedelta(hours=1),
    )
    stale_row.status = bot.OUTBOX_PROCESSING
    stale_row.attempts = 1
    stale_row.updated_at = now - timedelta(hours=1)
    db_session.commit()

    stats = bot.process_outbox(
        db_session,
        client=FakeBitrixClient(),
        settings=_settings(order_fulfillment_bot_apply_enabled=False),
        onec_validator=lambda _: bot.OneCPickupValidation(available=True),
        now=now,
    )

    assert stats["selected"] == 0
    assert stats["recovered"] == 0
    for row in pending_rows:
        db_session.refresh(row)
        assert row.status == bot.OUTBOX_PENDING
        assert row.attempts == 0
    db_session.refresh(stale_row)
    assert stale_row.status == bot.OUTBOX_PROCESSING
    assert stale_row.attempts == 1


def test_apply_disabled_still_processes_card_action_as_dry_run(db_session) -> None:
    enabled_settings = _settings(order_fulfillment_bot_apply_enabled=True)
    candidate = _create_candidate(db_session, settings=enabled_settings)
    client = FakeBitrixClient()

    def validator(_: str) -> bot.OneCPickupValidation:
        return bot.OneCPickupValidation(available=True, assembled=True)

    bot.process_outbox(
        db_session,
        client=client,
        settings=enabled_settings,
        onec_validator=validator,
        now=datetime(2026, 8, 23, 12, 1),
    )
    assert candidate.dry_run is False
    token = bot.sign_callback_token(
        candidate,
        action=bot.ACTION_ARRIVED,
        step=1,
        secret="test-secret",
    )
    action, _ = bot.queue_callback_action(
        db_session,
        token=token,
        actor_id="7",
        dialog_id="chat8729",
        settings=enabled_settings,
        now=datetime(2026, 8, 23, 12, 2),
    )
    bot.process_outbox(
        db_session,
        client=client,
        settings=enabled_settings,
        onec_validator=validator,
        apply_enabled_probe=lambda: False,
        now=datetime(2026, 8, 23, 12, 3),
    )

    db_session.refresh(candidate)
    db_session.refresh(action)
    assert candidate.status == bot.CANDIDATE_DRY_RUN
    assert action.status == "dry_run"
    assert client.stage_updates == []


def test_runtime_apply_probe_race_does_not_consume_attempt(db_session) -> None:
    now = datetime(2026, 8, 23, 12, 0)
    row = bot.enqueue_outbox(
        db_session,
        operation=bot.OP_UPDATE_CRM_STAGE,
        idempotency_key="runtime-apply-probe-race",
        payload={},
        now=now,
    )
    db_session.commit()
    probe_values = iter((True, True, True, False))

    stats = bot.process_outbox(
        db_session,
        client=FakeBitrixClient(),
        settings=_settings(order_fulfillment_bot_apply_enabled=True),
        onec_validator=lambda _: bot.OneCPickupValidation(available=True),
        apply_enabled_probe=lambda: next(probe_values, False),
        limit=1,
        now=now,
    )

    db_session.refresh(row)
    assert stats["selected"] == 0
    assert stats["completed"] == 0
    assert row.status == bot.OUTBOX_PENDING
    assert row.attempts == 0


def test_runtime_apply_probe_is_rechecked_after_crm_preflight(db_session) -> None:
    now = datetime(2026, 8, 23, 12, 0)
    row = bot.enqueue_outbox(
        db_session,
        operation=bot.OP_UPDATE_CRM_STAGE,
        idempotency_key="runtime-apply-probe-after-crm-preflight",
        target_type="deal",
        target_id="500",
        payload={
            "site_order_number": "241500",
            "before_stage": "FINAL_INVOICE",
            "target_stage": fulfillment.CRM_STAGE_PICKUP_WAITING,
        },
        now=now,
    )
    db_session.commit()
    probe_values = iter((True, True, True, True, False))
    client = FakeBitrixClient()

    stats = bot.process_outbox(
        db_session,
        client=client,
        settings=_settings(order_fulfillment_bot_apply_enabled=True),
        onec_validator=lambda _: bot.OneCPickupValidation(available=True),
        apply_enabled_probe=lambda: next(probe_values, False),
        limit=1,
        now=now,
    )

    db_session.refresh(row)
    assert stats["selected"] == 0
    assert stats["completed"] == 0
    assert row.status == bot.OUTBOX_PENDING
    assert row.attempts == 0
    assert client.stage_updates == []


def test_runtime_apply_probe_rereads_env_and_is_fail_closed(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ORDER_FULFILLMENT_BOT_APPLY_ENABLED=true\n",
        encoding="utf-8",
    )
    probe = outbox_script.build_runtime_apply_enabled_probe(
        initial_enabled=True,
        env_file=env_file,
    )

    assert probe() is True
    env_file.write_text(
        "ORDER_FULFILLMENT_BOT_APPLY_ENABLED=false\n",
        encoding="utf-8",
    )
    assert probe() is False
    env_file.write_text(
        "ORDER_FULFILLMENT_BOT_APPLY_ENABLED=true\n",
        encoding="utf-8",
    )
    assert probe() is False

    startup_disabled_probe = outbox_script.build_runtime_apply_enabled_probe(
        initial_enabled=False,
        env_file=env_file,
    )
    assert startup_disabled_probe() is False

    missing_probe = outbox_script.build_runtime_apply_enabled_probe(
        initial_enabled=True,
        env_file=tmp_path / "missing.env",
    )
    assert missing_probe() is False

    invalid_env_file = tmp_path / "invalid.env"
    invalid_env_file.write_text(
        "ORDER_FULFILLMENT_BOT_APPLY_ENABLED=unexpected\n",
        encoding="utf-8",
    )
    invalid_probe = outbox_script.build_runtime_apply_enabled_probe(
        initial_enabled=True,
        env_file=invalid_env_file,
    )
    assert invalid_probe() is False


def test_expired_open_candidate_card_is_closed_without_buttons(db_session) -> None:
    settings = _settings()
    candidate = _create_candidate(db_session, settings=settings)
    _mark_card_ready(db_session, candidate)
    candidate.expires_at = datetime(2026, 8, 23, 11, 0)
    db_session.commit()
    client = FakeBitrixClient()

    stats = bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True),
        now=datetime(2026, 8, 23, 12, 0),
    )

    db_session.refresh(candidate)
    assert stats["expired"] == 1
    assert candidate.status == bot.CANDIDATE_EXPIRED
    assert client.bot_updates[-1]["keyboard"] == []
    assert "Срок действия" in client.bot_updates[-1]["message"]


def test_bot_or_service_author_is_ignored(db_session) -> None:
    _warehouse(db_session)
    candidates = bot.create_candidates_from_message(
        db_session,
        dialog_id="chat8729",
        message_id="1",
        author_id="42",
        text_value="Заказ 241500 прибыл в Митино",
        message_at=datetime(2026, 8, 23, 12, 0),
        settings=_settings(order_fulfillment_bot_excluded_user_ids=[42]),
        now=datetime(2026, 8, 23, 12, 0),
    )

    assert candidates == []


def test_first_twenty_candidates_are_forced_to_dry_run(db_session) -> None:
    settings = _settings(order_fulfillment_bot_dry_run_card_limit=20)
    _warehouse(db_session)
    candidates = []
    for index in range(21):
        candidates.extend(
            bot.create_candidates_from_message(
                db_session,
                dialog_id="chat8729",
                message_id=str(index + 1),
                author_id="7",
                text_value=f"Заказ {241500 + index} прибыл в Митино",
                message_at=datetime(2026, 8, 23, 12, 0),
                settings=settings,
                now=datetime(2026, 8, 23, 12, 0),
            )
        )

    assert all(item.dry_run for item in candidates[:20])
    assert candidates[20].dry_run is False
    assert candidates[0].expires_at == datetime(2026, 8, 24, 12, 0)


def test_partial_message_replay_does_not_skip_twentieth_dry_run_slot(db_session) -> None:
    settings = _settings(order_fulfillment_bot_dry_run_card_limit=20)
    _warehouse(db_session)
    for index in range(18):
        bot.create_candidates_from_message(
            db_session,
            dialog_id="chat8729",
            message_id=str(index + 1),
            author_id="7",
            text_value=f"Заказ {250000 + index} прибыл в Митино",
            message_at=datetime(2026, 8, 23, 12, 0),
            settings=settings,
            now=datetime(2026, 8, 23, 12, 0),
        )
    bot.create_candidates_from_message(
        db_session,
        dialog_id="chat8729",
        message_id="99",
        author_id="7",
        text_value="Заказ 260000 прибыл в Митино",
        message_at=datetime(2026, 8, 23, 12, 0),
        settings=settings,
        now=datetime(2026, 8, 23, 12, 0),
    )

    replayed = bot.create_candidates_from_message(
        db_session,
        dialog_id="chat8729",
        message_id="99",
        author_id="7",
        text_value="Заказы 260000 и 260001 прибыли в Митино",
        message_at=datetime(2026, 8, 23, 12, 0),
        settings=settings,
        now=datetime(2026, 8, 23, 12, 0),
    )

    new_candidate = next(item for item in replayed if item.site_order_number == "260001")
    assert new_candidate.dry_run is True
    assert db_session.scalar(select(func.count(BitrixChatActionCandidate.id))) == 20


def test_polling_and_callback_share_dialog_message_identity(db_session) -> None:
    settings = _settings()
    _warehouse(db_session)

    class PollingClient:
        def get_dialog_messages(self, dialog_id: str, *, limit: int):
            assert dialog_id == "chat8729"
            assert limit == 50
            return {
                "messages": [
                    {
                        "id": "1001",
                        "author_id": "7",
                        "date": "2026-08-23T12:00:00+03:00",
                        "text": "Заказ 241500 прибыл в Митино",
                    }
                ],
                "files": [],
            }

    stats = fulfillment.ingest_bitrix_chat(
        db_session,
        client=PollingClient(),  # type: ignore[arg-type]
        chat_code=fulfillment.CHAT_SITE_MASTER_MOBILE,
        dialog_id="chat8729",
        limit=50,
        run_ocr=False,
        settings=settings,
    )
    candidate = bot.create_candidates_from_message(
        db_session,
        dialog_id="chat8729",
        message_id="1001",
        author_id="7",
        text_value="Заказ 241500 прибыл в Митино",
        message_at=datetime(2026, 8, 23, 9, 0),
        settings=settings,
        now=datetime(2026, 8, 23, 9, 0),
    )[0]
    raw_message = db_session.scalar(select(BitrixChatMessage))

    assert stats["messages"] == 1
    assert raw_message is not None
    assert raw_message.chat_id == 8729
    assert candidate.raw_message_id == raw_message.id
    assert db_session.scalar(select(func.count(BitrixChatMessage.id))) == 1


def test_polling_preserves_legacy_site_events_before_bot_cutover(db_session) -> None:
    settings = _settings(order_fulfillment_bot_enabled=False)

    class PollingClient:
        def get_dialog_messages(self, dialog_id: str, *, limit: int):
            return {
                "messages": [
                    {
                        "id": "1002",
                        "chat_id": "733",
                        "author_id": "7",
                        "date": "2026-08-23T12:00:00+03:00",
                        "text": "Заказ 241501 не забрали",
                    }
                ],
                "files": [],
            }

    stats = fulfillment.ingest_bitrix_chat(
        db_session,
        client=PollingClient(),  # type: ignore[arg-type]
        chat_code=fulfillment.CHAT_SITE_MASTER_MOBILE,
        dialog_id="chat733",
        limit=50,
        run_ocr=False,
        settings=settings,
    )

    assert stats["events"] == 1
    assert db_session.scalar(select(func.count(SiteOrderExecutionEvent.id))) == 1


def test_callback_token_is_signed_expires_and_replay_is_idempotent(db_session) -> None:
    settings = _settings()
    candidate = _create_candidate(db_session, settings=settings)
    _mark_card_ready(db_session, candidate)
    token = bot.sign_callback_token(
        candidate,
        action=bot.ACTION_ARRIVED,
        step=1,
        secret="test-secret",
    )

    action, duplicate = bot.queue_callback_action(
        db_session,
        token=token,
        actor_id="7",
        dialog_id="chat8729",
        settings=settings,
        now=datetime(2026, 8, 23, 13, 0),
    )
    repeated, repeated_duplicate = bot.queue_callback_action(
        db_session,
        token=token,
        actor_id="7",
        dialog_id="chat8729",
        settings=settings,
        now=datetime(2026, 8, 23, 13, 0),
    )

    assert duplicate is False
    assert repeated_duplicate is True
    assert repeated.id == action.id
    with pytest.raises(bot.BotSecurityError, match="invalid_callback_signature"):
        bot.verify_callback_token(
            token + "x",
            secret="test-secret",
            now=datetime(2026, 8, 23, 13, 0),
        )
    with pytest.raises(bot.BotSecurityError, match="callback_expired"):
        bot.verify_callback_token(
            token,
            secret="test-secret",
            now=datetime(2026, 8, 24, 13, 0),
        )


def test_callback_is_rejected_until_card_publication_is_committed(db_session) -> None:
    settings = _settings()
    candidate = _create_candidate(db_session, settings=settings)
    token = bot.sign_callback_token(
        candidate,
        action=bot.ACTION_ARRIVED,
        step=1,
        secret="test-secret",
    )

    with pytest.raises(bot.BotSecurityError, match="candidate_card_not_ready"):
        bot.queue_callback_action(
            db_session,
            token=token,
            actor_id="7",
            dialog_id="chat8729",
            settings=settings,
            now=datetime(2026, 8, 23, 13, 0),
        )


def test_second_employee_cannot_take_over_claimed_candidate(db_session) -> None:
    settings = _settings()
    candidate = _create_candidate(db_session, settings=settings)
    _mark_card_ready(db_session, candidate)
    token = bot.sign_callback_token(
        candidate,
        action=bot.ACTION_ARRIVED,
        step=1,
        secret="test-secret",
    )
    bot.queue_callback_action(
        db_session,
        token=token,
        actor_id="7",
        dialog_id="chat8729",
        settings=settings,
        now=datetime(2026, 8, 23, 13, 0),
    )

    with pytest.raises(bot.BotSecurityError, match="callback_already_claimed"):
        bot.queue_callback_action(
            db_session,
            token=token,
            actor_id="8",
            dialog_id="chat8729",
            settings=settings,
            now=datetime(2026, 8, 23, 13, 1),
        )


def test_cancel_cannot_bypass_an_action_already_queued(db_session) -> None:
    settings = _settings()
    candidate = _create_candidate(db_session, settings=settings)
    _mark_card_ready(db_session, candidate)
    arrived_token = bot.sign_callback_token(
        candidate,
        action=bot.ACTION_ARRIVED,
        step=1,
        secret="test-secret",
    )
    cancel_token = bot.sign_callback_token(
        candidate,
        action=bot.ACTION_CANCEL,
        step=1,
        secret="test-secret",
    )
    bot.queue_callback_action(
        db_session,
        token=arrived_token,
        actor_id="7",
        dialog_id="chat8729",
        settings=settings,
        now=datetime(2026, 8, 23, 13, 0),
    )

    with pytest.raises(bot.BotSecurityError, match="candidate_already_claimed"):
        bot.queue_callback_action(
            db_session,
            token=cancel_token,
            actor_id="7",
            dialog_id="chat8729",
            settings=settings,
            now=datetime(2026, 8, 23, 13, 1),
        )


def test_second_employee_cannot_cancel_confirmation_claim(db_session) -> None:
    settings = _settings()
    candidate = _create_candidate(db_session, settings=settings)
    _mark_card_ready(db_session, candidate)
    candidate.status = bot.CANDIDATE_CONFIRMATION
    candidate.active_action = bot.ACTION_ISSUED
    candidate.active_actor_id = "7"
    db_session.commit()
    cancel_token = bot.sign_callback_token(
        candidate,
        action=bot.ACTION_CANCEL,
        step=1,
        secret="test-secret",
    )

    with pytest.raises(bot.BotSecurityError, match="callback_already_claimed"):
        bot.queue_callback_action(
            db_session,
            token=cancel_token,
            actor_id="8",
            dialog_id="chat8729",
            settings=settings,
            now=datetime(2026, 8, 23, 13, 1),
        )


def test_second_candidate_cannot_run_parallel_action_for_same_order(db_session) -> None:
    settings = _settings()
    _warehouse(db_session)
    candidates = []
    for index, message_id in enumerate(("3001", "3002")):
        candidate = bot.create_candidates_from_message(
            db_session,
            dialog_id="chat8729",
            message_id=message_id,
            author_id="7",
            text_value="Заказ 241500 прибыл в Митино",
            message_at=datetime(2026, 8, 23, 12, index),
            settings=settings,
            now=datetime(2026, 8, 23, 12, index),
        )[0]
        candidate.status = bot.CANDIDATE_QUEUED
        candidate.active_action = bot.ACTION_ARRIVED
        candidate.active_actor_id = "7"
        candidate.action_claimed_at = datetime(2026, 8, 23, 12, index)
        candidates.append(candidate)
    second_action = BitrixChatAction(
        candidate_id=candidates[1].id,
        action=bot.ACTION_ARRIVED,
        actor_id="7",
        status="queued",
        confirmation_step=1,
        idempotency_key="parallel-order-action",
        payload={},
    )
    db_session.add(second_action)
    db_session.commit()
    client = FakeBitrixClient()

    bot._process_action(  # noqa: SLF001
        db_session,
        candidate=candidates[1],
        action=second_action,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(
            available=True,
            assembled=True,
        ),
        now=datetime(2026, 8, 23, 12, 2),
    )

    assert candidates[1].status == bot.CANDIDATE_REVIEW
    assert second_action.status == "manual_review"
    assert second_action.reason == "order_action_already_in_progress"
    assert client.stage_updates == []


def test_parallel_action_rejection_closes_pending_confirmation(db_session) -> None:
    settings = _settings()
    first = _create_candidate(db_session, settings=settings, message_id="3101")
    second = bot.create_candidates_from_message(
        db_session,
        dialog_id="chat8729",
        message_id="3102",
        author_id="7",
        text_value="Заказ 241500 выдан клиенту",
        message_at=datetime(2026, 8, 23, 12, 1),
        settings=settings,
        now=datetime(2026, 8, 23, 12, 1),
    )[0]
    first.status = bot.CANDIDATE_QUEUED
    first.active_action = bot.ACTION_ARRIVED
    first.active_actor_id = "7"
    first.action_claimed_at = datetime(2026, 8, 23, 12, 0)
    second.status = bot.CANDIDATE_QUEUED
    second.active_action = bot.ACTION_ISSUED
    second.active_actor_id = "7"
    second.action_claimed_at = datetime(2026, 8, 23, 12, 1)
    pending = BitrixChatAction(
        candidate_id=second.id,
        action=bot.ACTION_ISSUED,
        actor_id="7",
        status="awaiting_confirmation",
        confirmation_step=1,
        idempotency_key="parallel-pending-confirmation",
        payload={},
    )
    confirmation = BitrixChatAction(
        candidate_id=second.id,
        action=bot.ACTION_ISSUED,
        actor_id="7",
        status="queued",
        confirmation_step=2,
        idempotency_key="parallel-second-confirmation",
        payload={},
    )
    db_session.add_all([pending, confirmation])
    db_session.commit()

    bot._process_action(  # noqa: SLF001
        db_session,
        candidate=second,
        action=confirmation,
        client=FakeBitrixClient(),
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True),
        now=datetime(2026, 8, 23, 12, 2),
    )

    assert second.status == bot.CANDIDATE_REVIEW
    assert confirmation.status == "manual_review"
    assert pending.status == "rejected"
    assert pending.reason == "order_action_already_in_progress"


def test_arrival_apply_moves_stage_only_after_button(db_session) -> None:
    settings = _settings()
    candidate = _create_candidate(db_session, settings=settings)
    client = FakeBitrixClient()

    def validator(_: str) -> bot.OneCPickupValidation:
        return bot.OneCPickupValidation(available=True, assembled=True)

    bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=validator,
        now=datetime(2026, 8, 23, 12, 1),
    )
    assert client.stage_updates == []
    token = bot.sign_callback_token(
        candidate,
        action=bot.ACTION_ARRIVED,
        step=1,
        secret="test-secret",
    )
    bot.queue_callback_action(
        db_session,
        token=token,
        actor_id="7",
        dialog_id="chat8729",
        settings=settings,
        now=datetime(2026, 8, 23, 12, 2),
    )
    bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=validator,
        now=datetime(2026, 8, 23, 12, 3),
    )
    bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=validator,
        now=datetime(2026, 8, 23, 12, 4),
    )

    case = db_session.scalar(select(SiteOrderExecutionCase))
    assert client.stage_updates == ["PICKUP_WAITING"]
    assert case is not None
    assert case.storage_started_at == datetime(2026, 8, 23, 12, 0)
    assert case.notification_confirmed_at is None
    assert case.sla_started_at is None
    assert case.storage_deadline_at is None
    assert case.current_derived_status == fulfillment.EVENT_PICKUP_STORED


def test_non_participant_cannot_apply_action(db_session) -> None:
    settings = _settings()
    candidate = _create_candidate(db_session, settings=settings)
    client = FakeBitrixClient(participants={"8"})
    bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True, assembled=True),
        now=datetime(2026, 8, 23, 12, 1),
    )
    token = bot.sign_callback_token(
        candidate,
        action=bot.ACTION_ARRIVED,
        step=1,
        secret="test-secret",
    )
    action, _ = bot.queue_callback_action(
        db_session,
        token=token,
        actor_id="7",
        dialog_id="chat8729",
        settings=settings,
        now=datetime(2026, 8, 23, 12, 2),
    )
    bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True, assembled=True),
        now=datetime(2026, 8, 23, 12, 3),
    )

    assert action.status == "rejected"
    assert action.reason == "actor_not_active_chat_participant"
    assert client.stage_updates == []


def test_issued_requires_second_confirmation_and_payment(db_session) -> None:
    settings = _settings()
    warehouse = _warehouse(db_session)
    case = SiteOrderExecutionCase(
        site_order_number="241500",
        bitrix_deal_id=500,
        delivery_method="Самовывоз",
        current_derived_status=fulfillment.EVENT_PICKUP_STORED,
        current_crm_stage="PICKUP_WAITING",
        pickup_point_warehouse_id=warehouse.id,
        storage_started_at=datetime(2026, 8, 20, 12, 0),
        notification_confirmed_at=datetime(2026, 8, 20, 12, 30),
        sla_started_at=datetime(2026, 8, 20, 12, 30),
        payload={},
    )
    db_session.add(case)
    db_session.commit()
    candidate = bot.BitrixChatActionCandidate(
        source_chat_id="chat8729",
        source_message_id="10",
        source_author_id="7",
        source_event_at=datetime(2026, 8, 23, 12, 0),
        site_order_number="241500",
        bitrix_deal_id=500,
        detected_action=bot.ACTION_ISSUED,
        pickup_point_warehouse_id=warehouse.id,
        pickup_point_name=warehouse.name,
        status=bot.CANDIDATE_OPEN,
        expires_at=datetime(2026, 8, 24, 12, 0),
        nonce="nonce-issued",
        dry_run=False,
        payload={},
    )
    db_session.add(candidate)
    db_session.commit()
    deal = fulfillment.BitrixDealSnapshot(
        deal_id=500,
        stage_id="PICKUP_WAITING",
        delivery="Самовывоз",
    )

    first = bot.decide_pickup_action(
        action=bot.ACTION_ISSUED,
        confirmation_step=1,
        deal=deal,
        candidate=candidate,
        case=case,
        onec=bot.OneCPickupValidation(available=True, payment_confirmed=True),
        settings=settings,
        now=datetime(2026, 8, 23, 12, 0),
    )
    unpaid = bot.decide_pickup_action(
        action=bot.ACTION_ISSUED,
        confirmation_step=2,
        deal=deal,
        candidate=candidate,
        case=case,
        onec=bot.OneCPickupValidation(available=True, payment_confirmed=False),
        settings=settings,
        now=datetime(2026, 8, 23, 12, 0),
    )

    assert first.reason == "second_confirmation_required"
    assert unpaid.reason == "issued_payment_not_confirmed"
    assert unpaid.target_stage is None


def test_dismantle_is_blocked_before_96_hours(db_session) -> None:
    settings = _settings()
    warehouse = _warehouse(db_session)
    case = SiteOrderExecutionCase(
        site_order_number="241500",
        bitrix_deal_id=500,
        delivery_method="Самовывоз",
        current_derived_status=fulfillment.EVENT_PICKUP_STORED,
        current_crm_stage="PICKUP_WAITING",
        pickup_point_warehouse_id=warehouse.id,
        storage_started_at=datetime(2026, 8, 20, 12, 0),
        notification_confirmed_at=datetime(2026, 8, 20, 12, 30),
        sla_started_at=datetime(2026, 8, 20, 12, 30),
        payload={},
    )
    candidate = BitrixChatActionCandidate(
        source_chat_id="chat8729",
        source_message_id="11",
        source_event_at=datetime(2026, 8, 23, 12, 0),
        site_order_number="241500",
        bitrix_deal_id=500,
        detected_action=bot.ACTION_DISMANTLE,
        pickup_point_warehouse_id=warehouse.id,
        pickup_point_name=warehouse.name,
        status=bot.CANDIDATE_OPEN,
        expires_at=datetime(2026, 8, 24, 12, 0),
        nonce="nonce-dismantle",
        dry_run=False,
        payload={},
    )
    db_session.add_all([case, candidate])
    db_session.commit()

    decision = bot.decide_pickup_action(
        action=bot.ACTION_DISMANTLE,
        confirmation_step=2,
        deal=fulfillment.BitrixDealSnapshot(
            deal_id=500,
            stage_id="PICKUP_WAITING",
            delivery="Самовывоз",
        ),
        candidate=candidate,
        case=case,
        onec=bot.OneCPickupValidation(available=True),
        settings=settings,
        now=datetime(2026, 8, 23, 12, 0),
    )

    assert decision.allowed is False
    assert decision.reason == "dismantle_too_early"
    assert (
        bot._decision_reason_text(decision.reason) == "срок хранения ещё не истёк"
    )  # noqa: SLF001


@pytest.mark.parametrize("action", [bot.ACTION_ARRIVED, bot.ACTION_ISSUED])
def test_confirmed_onec_return_blocks_arrival_and_issue(db_session, action: str) -> None:
    settings = _settings()
    warehouse = _warehouse(db_session)
    candidate = BitrixChatActionCandidate(
        source_chat_id="chat8729",
        source_message_id=f"return-{action}",
        source_event_at=datetime(2026, 8, 23, 12, 0),
        site_order_number="241500",
        bitrix_deal_id=500,
        detected_action=action,
        pickup_point_warehouse_id=warehouse.id,
        pickup_point_name=warehouse.name,
        status=bot.CANDIDATE_OPEN,
        expires_at=datetime(2026, 8, 24, 12, 0),
        nonce=f"nonce-{action}",
        dry_run=False,
        payload={},
    )
    db_session.add(candidate)
    db_session.commit()

    decision = bot.decide_pickup_action(
        action=action,
        confirmation_step=2 if action == bot.ACTION_ISSUED else 1,
        deal=fulfillment.BitrixDealSnapshot(
            deal_id=500,
            stage_id=(
                fulfillment.CRM_STAGE_PICKUP_WAITING
                if action == bot.ACTION_ISSUED
                else "FINAL_INVOICE"
            ),
            delivery="Самовывоз",
        ),
        candidate=candidate,
        case=None,
        onec=bot.OneCPickupValidation(
            available=True,
            assembled=True,
            payment_confirmed=True,
            return_confirmed=True,
        ),
        settings=settings,
        now=datetime(2026, 8, 23, 12, 0),
    )

    assert decision.allowed is False
    assert decision.reason == "onec_return_conflict"


def test_onec_validator_treats_unknown_debt_as_conflict(monkeypatch) -> None:
    settlement = fulfillment_sync.OneCOrderSettlement(
        order_number="241500",
        posted_sale_count=1,
        posted_sale_amount=None,
        payment_amount=None,
        debt_amount=None,
        payment_confirmed=False,
        evidence="onec_payment_not_confirmed",
    )
    monkeypatch.setattr(
        fulfillment_sync,
        "fetch_onec_order_settlements",
        lambda _: {"241500": settlement},
    )
    monkeypatch.setattr(
        fulfillment_sync,
        "query_rtu_signal_by_orders",
        lambda _: {
            "241500": {
                "assembled_rtu_count": 1,
                "issued_rtu_count": 0,
                "returned_rtu_count": 0,
            }
        },
    )

    result = outbox_script.build_onec_validator()("241500")

    assert result.available is True
    assert result.debt_conflict is True


def test_historical_sms_is_blocked_and_track_marker_is_not_reused(db_session) -> None:
    settings = _settings(
        order_fulfillment_bot_sms_enabled=True,
        order_fulfillment_bot_sms_workflow_template_id=77,
        order_fulfillment_bot_cutover_at=datetime(2026, 8, 23, 10, 0, tzinfo=UTC),
    )
    candidate = _create_candidate(
        db_session,
        settings=settings,
        message_at=datetime(2026, 8, 23, 9, 59, tzinfo=UTC),
    )
    assert bot._sms_candidate_is_new(candidate, settings=settings) is False  # noqa: SLF001

    candidate.source_event_at = datetime(2026, 8, 23, 12, 1)
    db_session.commit()
    client = FakeBitrixClient()
    bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True, assembled=True),
        now=datetime(2026, 8, 23, 12, 2),
    )
    token = bot.sign_callback_token(
        candidate,
        action=bot.ACTION_ARRIVED,
        step=1,
        secret="test-secret",
    )
    bot.queue_callback_action(
        db_session,
        token=token,
        actor_id="7",
        dialog_id="chat8729",
        settings=settings,
        now=datetime(2026, 8, 23, 12, 3),
    )
    for minute in range(4, 10):
        bot.process_outbox(
            db_session,
            client=client,
            settings=settings,
            onec_validator=lambda _: bot.OneCPickupValidation(available=True, assembled=True),
            now=datetime(2026, 8, 23, 12, minute),
        )

    assert client.workflows[0]["parameters"]["MARKER_FIELD"] == ("UF_CRM_MM_PICKUP_READY_SMS_AT")
    assert client.raw["UF_CRM_MM_READY_TRACK_SMS_AT"] == "2026-08-22T12:00:00"


def test_first_arrival_reserves_sms_when_stage_is_already_pickup_waiting(db_session) -> None:
    settings = _settings(
        order_fulfillment_bot_sms_enabled=True,
        order_fulfillment_bot_sms_workflow_template_id=77,
        order_fulfillment_bot_cutover_at=datetime(2026, 8, 23, 10, 0),
    )
    candidate = _create_candidate(
        db_session,
        settings=settings,
        message_at=datetime(2026, 8, 23, 12, 0),
    )
    action = BitrixChatAction(
        candidate_id=candidate.id,
        action=bot.ACTION_ARRIVED,
        actor_id="7",
        status="queued",
        confirmation_step=1,
        idempotency_key="arrival-already-waiting",
        payload={},
    )
    db_session.add(action)
    db_session.flush()
    candidate.active_action = bot.ACTION_ARRIVED
    candidate.active_actor_id = "7"
    candidate.status = bot.CANDIDATE_QUEUED

    bot._process_action(  # noqa: SLF001
        db_session,
        candidate=candidate,
        action=action,
        client=FakeBitrixClient(stage=fulfillment.CRM_STAGE_PICKUP_WAITING),
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True, assembled=True),
        now=datetime(2026, 8, 23, 12, 1),
    )
    db_session.flush()

    operations = db_session.scalars(
        select(SiteOrderFulfillmentOutbox.operation).where(
            SiteOrderFulfillmentOutbox.action_id == action.id
        )
    ).all()
    assert bot.OP_UPDATE_CRM_STAGE not in operations
    assert bot.OP_START_SMS_WORKFLOW in operations
    assert bot.OP_VERIFY_SMS_WORKFLOW in operations


def test_sms_outbox_has_no_quantitative_pilot_limit(db_session) -> None:
    for index in range(10):
        bot.enqueue_outbox(
            db_session,
            operation=bot.OP_START_SMS_WORKFLOW,
            idempotency_key=f"sms:{index}",
            payload={},
            now=datetime(2026, 8, 23, 12, 0),
        )
    eleventh = bot.enqueue_outbox(
        db_session,
        operation=bot.OP_START_SMS_WORKFLOW,
        idempotency_key="sms:10",
        payload={},
        now=datetime(2026, 8, 23, 12, 0),
    )
    db_session.commit()

    assert eleventh.id is not None
    assert (
        db_session.scalar(
            select(func.count(SiteOrderFulfillmentOutbox.id)).where(
                SiteOrderFulfillmentOutbox.operation == bot.OP_START_SMS_WORKFLOW
            )
        )
        == 11
    )


def test_two_candidates_for_one_deal_reserve_only_one_pickup_sms(db_session) -> None:
    settings = _settings(
        order_fulfillment_bot_sms_enabled=True,
        order_fulfillment_bot_sms_workflow_template_id=77,
        order_fulfillment_bot_cutover_at=datetime(2026, 8, 23, 10, 0),
    )
    _warehouse(db_session)
    candidates = []
    for message_id in ("2001", "2002"):
        candidates.extend(
            bot.create_candidates_from_message(
                db_session,
                dialog_id="chat8729",
                message_id=message_id,
                author_id="7",
                text_value="Заказ 241500 прибыл в Митино",
                message_at=datetime(2026, 8, 23, 12, 0),
                settings=settings,
                now=datetime(2026, 8, 23, 12, 0),
            )
        )
    actions = []
    for index, candidate in enumerate(candidates, start=1):
        action = BitrixChatAction(
            candidate_id=candidate.id,
            action=bot.ACTION_ARRIVED,
            actor_id="7",
            status="queued",
            confirmation_step=1,
            idempotency_key=f"sms-race-action:{index}",
            payload={},
        )
        db_session.add(action)
        db_session.flush()
        candidate.active_action = bot.ACTION_ARRIVED
        candidate.active_actor_id = "7"
        candidate.status = bot.CANDIDATE_QUEUED
        actions.append(action)

    client = FakeBitrixClient()
    for index, (candidate, action) in enumerate(zip(candidates, actions, strict=True)):
        bot._process_action(  # noqa: SLF001
            db_session,
            candidate=candidate,
            action=action,
            client=client,
            settings=settings,
            onec_validator=lambda _: bot.OneCPickupValidation(
                available=True,
                assembled=True,
            ),
            now=datetime(2026, 8, 23, 12, 1),
        )
        if index == 0:
            candidate.status = bot.CANDIDATE_APPLIED
    db_session.flush()

    sms_rows = db_session.scalars(
        select(SiteOrderFulfillmentOutbox).where(
            SiteOrderFulfillmentOutbox.operation == bot.OP_START_SMS_WORKFLOW
        )
    ).all()
    assert len(sms_rows) == 1
    assert sms_rows[0].idempotency_key == "deal:500:pickup-ready-sms"


def test_failed_sms_attempts_do_not_block_unrelated_new_sms(db_session) -> None:
    for index in range(10):
        row = bot.enqueue_outbox(
            db_session,
            operation=bot.OP_START_SMS_WORKFLOW,
            idempotency_key=f"ambiguous-sms:{index}",
            payload={},
            now=datetime(2026, 8, 23, 12, 0),
        )
        row.status = bot.OUTBOX_FAILED
    next_row = bot.enqueue_outbox(
        db_session,
        operation=bot.OP_START_SMS_WORKFLOW,
        idempotency_key="ambiguous-sms:next-deal",
        payload={},
        now=datetime(2026, 8, 23, 12, 1),
    )
    db_session.commit()

    assert next_row.status == bot.OUTBOX_PENDING


@pytest.mark.parametrize(
    ("marker", "expected_status", "expected_candidate_status"),
    [
        ("2026-08-23T12:00:00", bot.OUTBOX_COMPLETED, bot.CANDIDATE_OPEN),
        ("", bot.OUTBOX_FAILED, bot.CANDIDATE_REVIEW),
    ],
)
def test_stale_sms_is_reconciled_without_second_send(
    db_session,
    marker: str,
    expected_status: str,
    expected_candidate_status: str,
) -> None:
    settings = _settings(
        order_fulfillment_bot_sms_enabled=True,
        order_fulfillment_bot_sms_workflow_template_id=77,
        order_fulfillment_bot_cutover_at=datetime(2026, 8, 23, 10, 0),
    )
    candidate = _create_candidate(
        db_session,
        settings=settings,
        message_at=datetime(2026, 8, 23, 12, 0),
    )
    sms_row = bot.enqueue_outbox(
        db_session,
        candidate=candidate,
        operation=bot.OP_START_SMS_WORKFLOW,
        idempotency_key="deal:500:pickup-ready-sms",
        target_type="deal",
        target_id="500",
        payload={"site_order_number": candidate.site_order_number},
        now=datetime(2026, 8, 23, 11, 0),
    )
    sms_row.status = bot.OUTBOX_PROCESSING
    sms_row.updated_at = datetime(2026, 8, 23, 11, 0)
    db_session.commit()
    client = FakeBitrixClient()
    client.raw[settings.order_fulfillment_bot_pickup_sms_field] = marker

    bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True),
        limit=1,
        now=datetime(2026, 8, 23, 12, 0),
    )

    db_session.refresh(sms_row)
    db_session.refresh(candidate)
    assert sms_row.status == expected_status
    assert candidate.status == expected_candidate_status
    assert client.workflows == []


@pytest.mark.parametrize(
    ("marker", "check_at", "expected_status", "expected_candidate_status"),
    [
        (
            "2026-08-23T12:03:00",
            datetime(2026, 8, 23, 12, 5),
            bot.OUTBOX_COMPLETED,
            bot.CANDIDATE_OPEN,
        ),
        (
            "",
            datetime(2026, 8, 23, 12, 5),
            bot.OUTBOX_RETRY,
            bot.CANDIDATE_OPEN,
        ),
        (
            "",
            datetime(2026, 8, 23, 12, 16),
            bot.OUTBOX_FAILED,
            bot.CANDIDATE_REVIEW,
        ),
    ],
)
def test_started_sms_is_completed_only_after_marker_confirmation(
    db_session,
    marker: str,
    check_at: datetime,
    expected_status: str,
    expected_candidate_status: str,
) -> None:
    settings = _settings(
        order_fulfillment_bot_sms_enabled=True,
        order_fulfillment_bot_sms_workflow_template_id=77,
        order_fulfillment_bot_cutover_at=datetime(2026, 8, 23, 10, 0),
    )
    candidate = _create_candidate(
        db_session,
        settings=settings,
        message_at=datetime(2026, 8, 23, 12, 0),
    )
    db_session.add(
        SiteOrderExecutionCase(
            site_order_number=candidate.site_order_number,
            bitrix_deal_id=500,
            delivery_method="Самовывоз",
            current_derived_status=fulfillment.EVENT_PICKUP_STORED,
            current_crm_stage=fulfillment.CRM_STAGE_PICKUP_WAITING,
            pickup_point_warehouse_id=candidate.pickup_point_warehouse_id,
            storage_started_at=datetime(2026, 8, 23, 12, 0),
            payload={},
        )
    )
    _mark_card_ready(db_session, candidate)
    start_row = bot.enqueue_outbox(
        db_session,
        candidate=candidate,
        operation=bot.OP_START_SMS_WORKFLOW,
        idempotency_key="deal:500:pickup-ready-sms",
        target_type="deal",
        target_id="500",
        payload={"site_order_number": candidate.site_order_number, "workflow_id": "77"},
        now=datetime(2026, 8, 23, 12, 0),
    )
    start_row.status = bot.OUTBOX_COMPLETED
    start_row.processed_at = datetime(2026, 8, 23, 12, 0)
    verify_row = bot.enqueue_outbox(
        db_session,
        candidate=candidate,
        depends_on=start_row,
        operation=bot.OP_VERIFY_SMS_WORKFLOW,
        idempotency_key="deal:500:pickup-ready-sms-verify",
        target_type="deal",
        target_id="500",
        payload={"site_order_number": candidate.site_order_number},
        now=datetime(2026, 8, 23, 12, 0),
    )
    db_session.commit()
    client = FakeBitrixClient(stage=fulfillment.CRM_STAGE_PICKUP_WAITING)
    client.raw[settings.order_fulfillment_bot_pickup_sms_field] = marker

    bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True),
        limit=1,
        now=check_at,
    )

    db_session.refresh(verify_row)
    db_session.refresh(candidate)
    assert verify_row.status == expected_status
    assert candidate.status == expected_candidate_status
    assert client.workflows == []


def test_failed_dependency_closes_card_as_manual_review(db_session) -> None:
    settings = _settings()
    candidate = _create_candidate(db_session, settings=settings)
    publish_row = db_session.scalar(
        select(SiteOrderFulfillmentOutbox).where(
            SiteOrderFulfillmentOutbox.operation == bot.OP_PUBLISH_CARD
        )
    )
    assert publish_row is not None
    publish_row.status = bot.OUTBOX_COMPLETED
    candidate.bot_message_id = "9001"
    candidate.status = bot.CANDIDATE_QUEUED
    failed = bot.enqueue_outbox(
        db_session,
        candidate=candidate,
        operation=bot.OP_START_SMS_WORKFLOW,
        idempotency_key="failed-sms",
        target_type="deal",
        target_id="500",
        payload={},
        now=datetime(2026, 8, 23, 12, 0),
    )
    failed.status = bot.OUTBOX_FAILED
    card = bot.enqueue_outbox(
        db_session,
        candidate=candidate,
        depends_on=failed,
        operation=bot.OP_UPDATE_CARD,
        idempotency_key="failed-sms-card",
        payload={"status_text": "Действие выполнено"},
        now=datetime(2026, 8, 23, 12, 0),
    )
    db_session.commit()
    client = FakeBitrixClient()

    bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True),
        now=datetime(2026, 8, 23, 12, 1),
    )

    db_session.refresh(candidate)
    db_session.refresh(card)
    assert candidate.status == bot.CANDIDATE_REVIEW
    assert card.status == bot.OUTBOX_COMPLETED
    assert client.bot_updates[-1]["keyboard"] == []
    assert "ручная проверка" in client.bot_updates[-1]["message"]


def test_failed_action_processing_closes_original_card(db_session) -> None:
    settings = _settings()
    candidate = _create_candidate(db_session, settings=settings)
    _mark_card_ready(db_session, candidate)
    token = bot.sign_callback_token(
        candidate,
        action=bot.ACTION_ARRIVED,
        step=1,
        secret="test-secret",
    )
    action, _ = bot.queue_callback_action(
        db_session,
        token=token,
        actor_id="7",
        dialog_id="chat8729",
        settings=settings,
        now=datetime(2026, 8, 23, 12, 0),
    )
    process_row = db_session.scalar(
        select(SiteOrderFulfillmentOutbox).where(
            SiteOrderFulfillmentOutbox.operation == bot.OP_PROCESS_ACTION
        )
    )
    assert process_row is not None
    process_row.max_attempts = 1
    db_session.commit()
    client = FakeBitrixClient()

    def unavailable_participants(_: str) -> set[str]:
        raise fulfillment.BitrixChatError("participant lookup failed")

    client.list_dialog_user_ids = unavailable_participants  # type: ignore[method-assign]

    bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True),
        now=datetime(2026, 8, 23, 12, 1),
    )

    db_session.refresh(candidate)
    db_session.refresh(action)
    assert candidate.status == bot.CANDIDATE_REVIEW
    assert action.status == "manual_review"
    assert client.bot_updates[-1]["keyboard"] == []
    assert "ручная проверка" in client.bot_updates[-1]["message"]


def test_sms_is_blocked_if_deal_left_pickup_waiting_before_send(db_session) -> None:
    settings = _settings(
        order_fulfillment_bot_sms_enabled=True,
        order_fulfillment_bot_sms_workflow_template_id=77,
        order_fulfillment_bot_cutover_at=datetime(2026, 8, 23, 10, 0),
    )
    candidate = _create_candidate(
        db_session,
        settings=settings,
        message_at=datetime(2026, 8, 23, 12, 0),
    )
    row = bot.enqueue_outbox(
        db_session,
        candidate=candidate,
        operation=bot.OP_START_SMS_WORKFLOW,
        idempotency_key="stage-changed-sms",
        target_type="deal",
        target_id="500",
        payload={"site_order_number": candidate.site_order_number},
        now=datetime(2026, 8, 23, 12, 0),
    )
    db_session.commit()
    client = FakeBitrixClient(stage="WON")

    with pytest.raises(RuntimeError, match="pickup_sms_stage_changed"):
        bot._start_sms_workflow(  # noqa: SLF001
            db_session,
            row=row,
            client=client,
            settings=settings,
        )

    assert client.workflows == []


def test_runtime_apply_probe_is_rechecked_before_sms_start(db_session) -> None:
    settings = _settings(
        order_fulfillment_bot_sms_enabled=True,
        order_fulfillment_bot_sms_workflow_template_id=77,
        order_fulfillment_bot_cutover_at=datetime(2026, 8, 23, 10, 0),
    )
    candidate = _create_candidate(
        db_session,
        settings=settings,
        message_at=datetime(2026, 8, 23, 12, 0),
    )
    row = bot.enqueue_outbox(
        db_session,
        candidate=candidate,
        operation=bot.OP_START_SMS_WORKFLOW,
        idempotency_key="runtime-apply-before-sms-start",
        target_type="deal",
        target_id="500",
        payload={"site_order_number": candidate.site_order_number},
        now=datetime(2026, 8, 23, 12, 0),
    )
    db_session.commit()
    client = FakeBitrixClient(stage=fulfillment.CRM_STAGE_PICKUP_WAITING)
    probe_values = iter((True, False))

    with pytest.raises(bot.ApplyDisabledBeforeSideEffect):
        bot._dispatch_outbox(  # noqa: SLF001
            db_session,
            row=row,
            client=client,
            settings=settings,
            onec_validator=lambda _: bot.OneCPickupValidation(available=True),
            apply_enabled_probe=lambda: next(probe_values, False),
            now=datetime(2026, 8, 23, 12, 1),
        )

    assert client.workflows == []


def test_task_is_blocked_if_live_deal_stage_changed(db_session) -> None:
    settings = _settings()
    row = bot.enqueue_outbox(
        db_session,
        operation=bot.OP_CREATE_TASK,
        idempotency_key="stage-changed-task",
        target_type="deal",
        target_id="500",
        payload={
            "task_kind": "call",
            "site_order_number": "241500",
            "expected_stage": fulfillment.CRM_STAGE_PICKUP_WAITING,
        },
        now=datetime(2026, 8, 23, 12, 0),
    )
    db_session.commit()
    client = FakeBitrixClient(stage="WON")

    with pytest.raises(RuntimeError, match="deal_stage_changed_before_task"):
        bot._create_task(  # noqa: SLF001
            session=db_session,
            row=row,
            client=client,
            settings=settings,
        )

    assert client.tasks == []


def test_runtime_apply_probe_is_rechecked_before_task_creation(db_session) -> None:
    settings = _settings()
    row = bot.enqueue_outbox(
        db_session,
        operation=bot.OP_CREATE_TASK,
        idempotency_key="runtime-apply-before-task-create",
        target_type="deal",
        target_id="500",
        payload={
            "task_kind": "call",
            "site_order_number": "241500",
            "expected_stage": fulfillment.CRM_STAGE_PICKUP_WAITING,
        },
        now=datetime(2026, 8, 23, 12, 0),
    )
    db_session.commit()
    client = FakeBitrixClient(stage=fulfillment.CRM_STAGE_PICKUP_WAITING)
    probe_values = iter((True, False))

    with pytest.raises(bot.ApplyDisabledBeforeSideEffect):
        bot._dispatch_outbox(  # noqa: SLF001
            db_session,
            row=row,
            client=client,
            settings=settings,
            onec_validator=lambda _: bot.OneCPickupValidation(available=True),
            apply_enabled_probe=lambda: next(probe_values, False),
            now=datetime(2026, 8, 23, 12, 1),
        )

    assert client.tasks == []


def test_sla_task_without_candidate_does_not_emit_sqlalchemy_warning(db_session) -> None:
    now = datetime(2026, 8, 23, 12, 0)
    row = bot.enqueue_outbox(
        db_session,
        operation=bot.OP_CREATE_TASK,
        idempotency_key="sla-task-without-candidate",
        target_type="deal",
        target_id="500",
        payload={
            "task_kind": "call",
            "site_order_number": "241500",
            "expected_stage": fulfillment.CRM_STAGE_PICKUP_WAITING,
        },
        now=now,
    )
    db_session.commit()
    client = FakeBitrixClient(stage=fulfillment.CRM_STAGE_PICKUP_WAITING)

    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        stats = bot.process_outbox(
            db_session,
            client=client,
            settings=_settings(),
            onec_validator=lambda _: bot.OneCPickupValidation(available=True),
            now=now,
        )

    db_session.refresh(row)
    assert stats["completed"] == 1
    assert row.status == bot.OUTBOX_COMPLETED
    assert len(client.tasks) == 1


def test_task_api_falls_back_only_when_modern_method_is_unavailable() -> None:
    client = fulfillment.BitrixChatClient(
        "https://crm.example/rest/1/token",
        bot_client_id="pickup-bot",
    )
    calls: list[str] = []

    def fake_call(method: str, params: dict | None = None) -> dict:
        del params
        calls.append(method)
        if method == "tasks.task.add":
            raise fulfillment.BitrixChatError(
                "tasks.task.add: ERROR_METHOD_NOT_FOUND Method not found"
            )
        return {"result": 55}

    client.call = fake_call  # type: ignore[method-assign]

    assert client.add_task({"TITLE": "Проверка"}) == 55
    assert calls == ["tasks.task.add", "task.item.add"]


def test_dialog_participants_are_loaded_from_all_bitrix_pages() -> None:
    client = fulfillment.BitrixChatClient(
        "https://crm.example/rest/1/token",
        bot_client_id="pickup-bot",
    )
    calls: list[dict] = []

    def fake_call(method: str, params: dict | None = None) -> dict:
        assert method == "im.dialog.users.list"
        calls.append(params or {})
        if not params or "start" not in params:
            return {
                "result": [{"id": index} for index in range(1, 51)],
                "next": 50,
                "total": 60,
            }
        assert params["start"] == 50
        return {
            "result": [{"id": index} for index in range(51, 61)],
            "total": 60,
        }

    client.call = fake_call  # type: ignore[method-assign]

    assert client.list_dialog_user_ids("chat8729") == {str(index) for index in range(1, 61)}
    assert calls == [
        {"DIALOG_ID": "chat8729"},
        {"DIALOG_ID": "chat8729", "start": 50},
    ]


def test_dialog_participant_pagination_fails_closed_on_repeated_page() -> None:
    client = fulfillment.BitrixChatClient(
        "https://crm.example/rest/1/token",
        bot_client_id="pickup-bot",
    )
    client.call = lambda *args, **kwargs: {  # type: ignore[method-assign]
        "result": [{"id": 1}],
        "next": 50,
    }

    with pytest.raises(fulfillment.BitrixChatError, match="repeated next page"):
        client.list_dialog_user_ids("chat8729")


def test_task_api_does_not_fallback_after_ambiguous_modern_api_error() -> None:
    client = fulfillment.BitrixChatClient(
        "https://crm.example/rest/1/token",
        bot_client_id="pickup-bot",
    )
    calls: list[str] = []

    def fake_call(method: str, params: dict | None = None) -> dict:
        del params
        calls.append(method)
        raise fulfillment.BitrixChatError("tasks.task.add: http_504 gateway timeout")

    client.call = fake_call  # type: ignore[method-assign]

    with pytest.raises(fulfillment.BitrixChatError, match="http_504"):
        client.add_task({"TITLE": "Проверка"})
    assert calls == ["tasks.task.add"]


@pytest.mark.parametrize(
    ("operation", "expected_error"),
    [
        ("message", "imbot.message.update returned empty result"),
        ("deal", "crm.deal.update returned empty result"),
    ],
)
def test_bitrix_updates_reject_empty_success_result(
    operation: str,
    expected_error: str,
) -> None:
    client = fulfillment.BitrixChatClient(
        "https://crm.example/rest/1/token",
        bot_client_id="pickup-bot",
    )
    client.call = lambda *args, **kwargs: {"result": False}  # type: ignore[method-assign]

    with pytest.raises(fulfillment.BitrixChatError, match=expected_error):
        if operation == "message":
            client.update_bot_message(
                message_id="9001",
                bot_id=42,
                message="Готово",
                keyboard=[],
            )
        else:
            client.update_deal_stage(500, fulfillment.CRM_STAGE_PICKUP_WAITING)


def test_old_execution_event_cannot_regress_case_state(db_session) -> None:
    newer = datetime(2026, 8, 23, 12, 0)
    older = newer - timedelta(days=1)
    fulfillment.upsert_execution_event(
        db_session,
        site_order_number="241500",
        event_type=fulfillment.EVENT_PICKUP_RECEIVED,
        event_at=newer,
        source="manual",
        source_ref="new",
        confidence="strong",
        raw_message_id=None,
        payload={},
    )
    fulfillment.upsert_execution_event(
        db_session,
        site_order_number="241500",
        event_type=fulfillment.EVENT_PICKUP_UNCLAIMED,
        event_at=older,
        source="manual",
        source_ref="old",
        confidence="strong",
        raw_message_id=None,
        payload={},
    )
    db_session.commit()
    case = db_session.scalar(select(SiteOrderExecutionCase))

    assert case is not None
    assert case.current_derived_status == fulfillment.EVENT_PICKUP_RECEIVED


def test_execution_event_times_are_normalized_before_ordering(db_session) -> None:
    fulfillment.upsert_execution_event(
        db_session,
        site_order_number="241501",
        event_type=fulfillment.EVENT_PICKUP_RECEIVED,
        event_at=datetime(2026, 8, 23, 15, 0, tzinfo=UTC),
        source="manual",
        source_ref="utc-new",
        confidence="strong",
        raw_message_id=None,
        payload={},
    )
    fulfillment.upsert_execution_event(
        db_session,
        site_order_number="241501",
        event_type=fulfillment.EVENT_PICKUP_UNCLAIMED,
        event_at=datetime(2026, 8, 23, 14, 0),
        source="manual",
        source_ref="naive-old",
        confidence="strong",
        raw_message_id=None,
        payload={},
    )
    db_session.commit()
    case = db_session.scalar(
        select(SiteOrderExecutionCase).where(SiteOrderExecutionCase.site_order_number == "241501")
    )

    assert case is not None
    assert case.current_derived_status == fulfillment.EVENT_PICKUP_RECEIVED
