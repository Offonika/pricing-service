from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.models.logistics import LogisticsWarehouse
from app.models.site_order_fulfillment import (
    BitrixChatAction,
    BitrixChatActionCandidate,
    BitrixChatMessage,
    BitrixChatReaction,
    PickupInventoryRun,
    PickupInventorySubmission,
    SiteOrderExecutionCase,
    SiteOrderExecutionEvent,
    SiteOrderFulfillmentOutbox,
)
from app.services import pickup_control, pickup_history, pickup_inventory
from app.services import site_order_fulfillment as fulfillment
from app.services import site_order_fulfillment_bot as bot


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "order_fulfillment_bot_enabled": True,
        "order_fulfillment_bot_apply_enabled": True,
        "order_fulfillment_pickup_stage_apply_enabled": True,
        "order_fulfillment_pickup_sla_enabled": True,
        "order_fulfillment_pickup_inventory_enabled": True,
        "order_fulfillment_lost_orders_enabled": True,
        "order_fulfillment_bot_source_chat_ids": [
            "chat8729",
            "chat733",
            "chat8961",
            "chat729",
            "chat739",
        ],
        "order_fulfillment_bot_application_token": "app-token",
        "order_fulfillment_bot_allowed_domains": ["crm.example"],
        "order_fulfillment_bot_allowed_member_ids": ["member-1"],
        "order_fulfillment_bot_callback_secret": "test-secret",
        "order_fulfillment_bot_cutover_at": datetime(2026, 8, 20, tzinfo=UTC),
        "order_fulfillment_bot_id": 42,
        "order_fulfillment_bot_client_id": "pickup-bot",
        "order_fulfillment_bot_command_id": 103,
        "order_fulfillment_bot_dry_run_card_limit": 0,
        "order_fulfillment_internet_shop_task_responsible_id": 100,
        "order_fulfillment_site_return_task_responsible_id": 101,
        "order_fulfillment_point_task_routes": {},
    }
    values.update(overrides)
    return Settings(**values)


def _warehouse(db_session, external_id: str, name: str) -> LogisticsWarehouse:
    row = LogisticsWarehouse(
        external_id=external_id,
        name=name,
        kind="retail",
        is_active=True,
        payload={"aliases": [name]},
    )
    db_session.add(row)
    db_session.flush()
    return row


def _message(
    db_session,
    *,
    message_id: int,
    text_value: str,
    at: datetime,
    chat_code: str = fulfillment.CHAT_PICKUP_INVENTORY,
    dialog_id: str = "chat8961",
) -> BitrixChatMessage:
    row = BitrixChatMessage(
        chat_code=chat_code,
        dialog_id=dialog_id,
        chat_id=int(dialog_id.removeprefix("chat")),
        message_id=message_id,
        message_at=at,
        author_id="7",
        raw_text_hash=fulfillment._text_hash(text_value),  # noqa: SLF001
        raw_text_redacted=text_value,
        parser_version="test",
        parse_status="parsed",
        payload={},
    )
    db_session.add(row)
    db_session.flush()
    return row


class FakeClient:
    def __init__(self, *, stage: str = fulfillment.CRM_STAGE_PICKUP_WAITING) -> None:
        self.stage = stage
        self.raw = {fulfillment.CRM_ORDER_NUMBER_FIELD: "241500"}
        self.bot_messages: list[dict] = []
        self.bot_updates: list[dict] = []
        self.stage_updates: list[str] = []
        self.field_updates: list[dict] = []
        self.tasks: list[dict] = []

    def _deal(self) -> fulfillment.BitrixDealSnapshot:
        return fulfillment.BitrixDealSnapshot(
            deal_id=500,
            stage_id=self.stage,
            delivery="Самовывоз",
            raw=dict(self.raw),
        )

    def list_deals_by_site_order(self, order_number: str):
        return [self._deal()] if order_number == "241500" else []

    def get_deal_by_id(self, deal_id: int):
        return self._deal() if deal_id == 500 else None

    def list_dialog_user_ids(self, dialog_id: str) -> set[str]:
        return {"7"}

    def update_bot_message(self, **payload):
        self.bot_updates.append(payload)
        return True

    def add_bot_message(self, **payload):
        self.bot_messages.append(payload)
        return str(900 + len(self.bot_messages))

    def update_deal_stage(self, deal_id: int, target_stage: str):
        assert deal_id == 500
        self.stage = target_stage
        self.stage_updates.append(target_stage)
        return True

    def update_deal_fields(self, deal_id: int, fields: dict):
        assert deal_id == 500
        self.raw.update(fields)
        self.field_updates.append(fields)
        return True

    def get_user_by_id(self, user_id: int):
        return {"ID": str(user_id), "ACTIVE": "Y"}

    def add_task(self, fields: dict):
        self.tasks.append(fields)
        return {"task": {"id": len(self.tasks)}}


def test_reaction_ingest_detects_add_remove_and_ignores_missing_payload(db_session) -> None:
    base = dict(
        session=db_session,
        chat_code=fulfillment.CHAT_PICKUP_READY,
        dialog_id="chat8729",
        chat_id=8729,
        message_id=10,
        message_at=datetime(2026, 8, 24, 10),
        author_id="7",
        text_value="Заказ 241500 готов к выдаче",
    )
    created = fulfillment.ingest_bitrix_message(
        **base,
        payload={"params": {"LIKE": ["131016"]}},
    )
    assert created.reactions_added == 1
    reaction = db_session.scalar(select(BitrixChatReaction))
    assert reaction is not None and reaction.is_active is True

    unchanged = fulfillment.ingest_bitrix_message(**base, payload={"text": "no reaction data"})
    db_session.refresh(reaction)
    assert unchanged.reactions_removed == 0
    assert reaction.is_active is True

    removed = fulfillment.ingest_bitrix_message(**base, payload={"params": {"LIKE": []}})
    db_session.refresh(reaction)
    assert removed.reactions_removed == 1
    assert reaction.is_active is False
    assert reaction.removed_at is not None


def test_polling_uses_last_id_pagination_and_is_idempotent(db_session) -> None:
    class PagingClient:
        def __init__(self) -> None:
            self.last_ids: list[int | None] = []

        def get_dialog_messages(self, dialog_id: str, *, limit: int, last_id: int | None = None):
            self.last_ids.append(last_id)
            pages = {
                None: [100, 99],
                99: [99, 98],
                98: [],
            }
            return {
                "chat_id": 729,
                "messages": [
                    {
                        "id": value,
                        "chat_id": 729,
                        "author_id": "7",
                        "date": f"2026-08-24T10:{value % 60:02d}:00+00:00",
                        "text": f"Заказ 241500 отправлен на Митино {value}",
                        "params": {"LIKE": []},
                    }
                    for value in pages[last_id]
                ],
            }

    client = PagingClient()
    stats = fulfillment.poll_bitrix_chat_pages(
        db_session,
        client=client,
        chat_code=fulfillment.CHAT_PICKUP_MOVEMENT,
        dialog_id="chat729",
        max_pages=5,
        settings=_settings(),
    )

    assert client.last_ids == [None, 99, 98]
    assert stats["pages"] == 2
    assert stats["duplicates"] == 1
    assert db_session.scalar(select(func.count(BitrixChatMessage.id))) == 3


def test_polling_normalizes_aware_message_dates_for_lookback(db_session) -> None:
    class Client:
        def get_dialog_messages(
            self,
            dialog_id: str,
            *,
            limit: int,
            last_id: int | None = None,
        ):
            assert last_id is None
            return {
                "chat_id": 729,
                "messages": [
                    {
                        "id": 10,
                        "chat_id": 729,
                        "author_id": "7",
                        "date": "2026-08-24T10:00:00+03:00",
                        "text": "Заказ 241500 отправлен на Митино",
                        "params": {"LIKE": []},
                    }
                ],
            }

    stats = fulfillment.poll_bitrix_chat_pages(
        db_session,
        client=Client(),
        chat_code=fulfillment.CHAT_PICKUP_MOVEMENT,
        dialog_id="chat729",
        lookback_since=datetime(2026, 8, 24, 7, tzinfo=UTC),
        settings=_settings(),
    )

    assert stats["reached_lookback"] is True
    assert stats["pages"] == 1


def test_polling_date_backfill_requeues_full_batch_arrival(db_session) -> None:
    settings = _settings(
        order_fulfillment_pickup_auto_arrival_enabled=True,
        order_fulfillment_bot_cutover_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
    )
    _warehouse(db_session, "mitino", "Митино")
    order_numbers = [str(241500 + index) for index in range(12)]
    text_value = f"Митино: {', '.join(order_numbers)}"

    assert (
        bot.create_candidates_from_message(
            db_session,
            dialog_id="chat8729",
            message_id="273088",
            author_id="7",
            text_value=text_value,
            message_at=None,
            settings=settings,
        )
        == []
    )
    raw_message = db_session.scalar(
        select(BitrixChatMessage).where(BitrixChatMessage.message_id == 273088)
    )
    assert raw_message is not None and raw_message.message_at is None

    ingested = fulfillment.ingest_bitrix_message(
        db_session,
        chat_code=fulfillment.CHAT_PICKUP_READY,
        dialog_id="chat8729",
        chat_id=8729,
        message_id=273088,
        message_at=datetime(2026, 8, 25, 14, 31, 33),
        author_id="7",
        text_value=text_value,
        payload={"date": "2026-08-25T17:31:33+03:00"},
    )
    stats = pickup_control.create_missing_pickup_candidates(
        db_session,
        settings=settings,
    )

    assert ingested.message_at_backfilled is True
    assert stats == {"checked": 1, "created": 12}
    candidates = db_session.scalars(
        select(BitrixChatActionCandidate).order_by(BitrixChatActionCandidate.id.asc())
    ).all()
    assert [candidate.site_order_number for candidate in candidates] == order_numbers
    assert {candidate.status for candidate in candidates} == {bot.CANDIDATE_QUEUED}
    assert db_session.scalar(select(func.count(BitrixChatAction.id))) == 12
    assert (
        db_session.scalar(
            select(func.count(SiteOrderFulfillmentOutbox.id)).where(
                SiteOrderFulfillmentOutbox.operation == bot.OP_PROCESS_ACTION
            )
        )
        == 12
    )


def test_pickup_control_poll_reads_all_five_configured_chats(db_session) -> None:
    class EmptyClient:
        def __init__(self) -> None:
            self.dialog_ids: list[str] = []

        def get_dialog_messages(
            self,
            dialog_id: str,
            *,
            limit: int,
            last_id: int | None = None,
        ):
            self.dialog_ids.append(dialog_id)
            return {
                "chat_id": int(dialog_id.removeprefix("chat")),
                "messages": [],
            }

    client = EmptyClient()
    pickup_control.poll_pickup_control_chats(
        db_session,
        client=client,
        settings=_settings(),
        now=datetime(2026, 8, 24, 12),
    )

    assert client.dialog_ids == ["chat733", "chat8729", "chat8961", "chat729", "chat739"]


def test_inventory_report_does_not_create_generic_pickup_cards(db_session) -> None:
    created = bot.create_candidates_from_message(
        db_session,
        dialog_id="chat8961",
        message_id="101",
        author_id="7",
        text_value="Митино, не забрали: 241500, 241501",
        message_at=datetime(2026, 8, 24, 10),
        settings=_settings(),
        now=datetime(2026, 8, 24, 10),
    )

    assert created == []
    assert db_session.scalar(select(func.count(BitrixChatActionCandidate.id))) == 0
    assert db_session.scalar(select(func.count(SiteOrderFulfillmentOutbox.id))) == 0


def test_inventory_full_carry_zero_and_correction_are_revisioned(db_session) -> None:
    _warehouse(db_session, "mitino", "Митино")
    full = pickup_inventory.persist_inventory_message(
        db_session,
        message=_message(
            db_session,
            message_id=1,
            text_value="Митино полный список: 241500, 241501",
            at=datetime(2026, 8, 24, 10),
        ),
    )
    carry = pickup_inventory.persist_inventory_message(
        db_session,
        message=_message(
            db_session,
            message_id=2,
            text_value="Митино, всё актуально",
            at=datetime(2026, 8, 24, 11),
        ),
    )
    zero = pickup_inventory.persist_inventory_message(
        db_session,
        message=_message(
            db_session,
            message_id=3,
            text_value="Митино: невыданных нет",
            at=datetime(2026, 8, 24, 12),
        ),
    )
    correction = pickup_inventory.persist_inventory_message(
        db_session,
        message=_message(
            db_session,
            message_id=4,
            text_value="Митино исправление: 241500",
            at=datetime(2026, 8, 24, 13),
        ),
    )

    assert [full.revision, carry.revision, zero.revision, correction.revision] == [1, 2, 3, 4]
    assert {item.site_order_number for item in carry.items} == {"241500", "241501"}
    assert zero.items == []
    assert [item.site_order_number for item in correction.items] == ["241500"]
    assert {item.site_order_number for item in full.items} == {"241500", "241501"}
    assert {
        item.site_order_number
        for item in pickup_inventory.disappearance_candidates(
            db_session,
            current_submission=zero,
        )
    } == {"241500", "241501"}
    assert db_session.scalar(select(func.count(PickupInventoryRun.id))) == 1


def test_late_inventory_edit_blocks_previously_confirmed_disappearance(db_session) -> None:
    _warehouse(db_session, "mitino", "Митино")
    previous_message = _message(
        db_session,
        message_id=130,
        text_value="Митино 241500",
        at=datetime(2026, 8, 24, 10),
    )
    previous = pickup_inventory.persist_inventory_message(
        db_session,
        message=previous_message,
    )
    current = pickup_inventory.persist_inventory_message(
        db_session,
        message=_message(
            db_session,
            message_id=131,
            text_value="Митино невыданных нет",
            at=datetime(2026, 8, 24, 11),
        ),
    )
    assert pickup_inventory.disappearance_candidates(
        db_session,
        current_submission=current,
    )

    edited = fulfillment.ingest_bitrix_message(
        db_session,
        chat_code=fulfillment.CHAT_PICKUP_INVENTORY,
        dialog_id="chat8961",
        chat_id=8961,
        message_id=130,
        message_at=datetime(2026, 8, 24, 10),
        author_id="7",
        text_value="Митино исправление: 241501",
        payload={"text": "Митино исправление: 241501"},
    )

    db_session.refresh(previous)
    assert edited.edited_message is True
    assert previous.status == pickup_inventory.STATUS_MANUAL_REVIEW
    assert (
        pickup_inventory.disappearance_candidates(
            db_session,
            current_submission=current,
        )
        == []
    )


def test_glued_order_is_split_only_when_both_orders_exist() -> None:
    unresolved = pickup_inventory.parse_inventory_text(
        "Митино 241500241501",
        order_exists=lambda _: False,
    )
    resolved = pickup_inventory.parse_inventory_text(
        "Митино 241500241501",
        order_exists=lambda value: value in {"241500", "241501"},
    )

    assert unresolved.explicit is False
    assert unresolved.ambiguous_tokens == ("241500241501",)
    assert resolved.explicit is True
    assert resolved.order_numbers == ("241500", "241501")


def test_inventory_language_recognizes_carry_zero_and_non_state_message() -> None:
    carry = pickup_inventory.parse_inventory_text("Савок, все заказы актуальны")
    zero = pickup_inventory.parse_inventory_text("Савок, все заказы выданы")
    instruction = pickup_inventory.parse_inventory_text("Расформируйте заказ 241500 на Садовую")

    assert carry.mode == pickup_inventory.MODE_CARRY and carry.explicit is True
    assert zero.mode == pickup_inventory.MODE_ZERO and zero.explicit is True
    assert instruction.order_numbers == ("241500",)
    assert instruction.explicit is False


def test_pickup_chat_alias_is_scoped_and_manual_inventory_is_reparsed_append_only(
    db_session,
) -> None:
    warehouse = _warehouse(
        db_session,
        "savely",
        "Савеловский Мобильный пав. Т-103 | Т-105",
    )
    original = pickup_inventory.persist_inventory_message(
        db_session,
        message=_message(
            db_session,
            message_id=15,
            text_value="Савок не забрали: 241500, 241501",
            at=datetime(2026, 8, 24, 10),
        ),
    )
    assert original is not None
    assert original.status == pickup_inventory.STATUS_MANUAL_REVIEW
    assert original.warehouse_id is None

    stats = pickup_inventory.reprocess_manual_inventory_submissions(
        db_session,
        pickup_aliases={"savely": ["Савок"]},
        now=datetime(2026, 8, 25, 10),
    )

    db_session.refresh(original)
    confirmed = db_session.scalar(
        select(PickupInventorySubmission).where(
            PickupInventorySubmission.status == pickup_inventory.STATUS_CONFIRMED
        )
    )
    assert stats["confirmed"] == 1
    assert stats["by_warehouse"] == {"savely": 1}
    assert original.status == "superseded"
    assert confirmed is not None and confirmed.warehouse_id == warehouse.id
    assert confirmed.supersedes_submission_id == original.id
    assert {item.site_order_number for item in confirmed.items} == {"241500", "241501"}
    assert (
        pickup_inventory.reprocess_manual_inventory_submissions(
            db_session,
            pickup_aliases={"savely": ["Савок"]},
        )["confirmed"]
        == 0
    )


def test_pickup_alias_contour_rejects_standard_match_outside_scope(db_session) -> None:
    _warehouse(db_session, "real", "Рабочая точка")
    _warehouse(db_session, "store-1", "Магазин 1")

    resolution = pickup_inventory.resolve_pickup_inventory_warehouse(
        db_session,
        "Магазин 1 полный список: 241500",
        pickup_aliases={"real": ["Рабочая"]},
    )

    assert resolution.warehouse is None
    assert resolution.reason == "matched warehouse is outside pickup contour"


def test_pickup_warehouse_settings_parse_json_and_list() -> None:
    settings = Settings(
        _env_file=None,
        order_fulfillment_pickup_warehouse_external_ids="one,two",
        order_fulfillment_pickup_warehouse_aliases='{"one":["Савок","Савок"],"two":"Пресня"}',
    )

    assert settings.order_fulfillment_pickup_warehouse_external_ids == ["one", "two"]
    assert settings.order_fulfillment_pickup_warehouse_aliases == {
        "one": ["Савок"],
        "two": ["Пресня"],
    }


def test_task_route_preflight_uses_explicit_pickup_contour(db_session) -> None:
    _warehouse(db_session, "real", "Рабочая точка")
    _warehouse(db_session, "store-1", "Магазин 1")
    settings = _settings(
        order_fulfillment_pickup_warehouse_external_ids=["real"],
        order_fulfillment_point_task_routes={
            "real": {"operator": 200, "senior": 201},
        },
    )

    assert bot.task_route_configuration_errors(db_session, settings=settings) == []

    missing = _settings(
        order_fulfillment_pickup_warehouse_external_ids=["real", "missing"],
        order_fulfillment_point_task_routes={
            "real": {"operator": 200, "senior": 201},
            "missing": {"operator": 202, "senior": 203},
        },
    )
    assert bot.task_route_configuration_errors(db_session, settings=missing) == [
        "task_route_missing:pickup_warehouse:missing"
    ]


def test_crm_order_exists_probe_is_cached_and_fails_closed() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def list_deals_by_site_order(self, order_number: str):
            self.calls.append(order_number)
            if order_number == "241500":
                return [object()]
            if order_number == "241501":
                raise RuntimeError("temporary read error")
            return []

    client = Client()
    probe = pickup_control.build_crm_order_exists_probe(client)

    assert probe("241500") is True
    assert probe("241500") is True
    assert probe("241501") is False
    assert probe("241501") is False
    assert client.calls == ["241500", "241501"]


def test_crm_datetime_readback_compares_the_same_instant_across_offsets() -> None:
    assert bot._crm_field_values_equal(  # noqa: SLF001
        bot.CRM_PICKUP_SLA_STARTED_FIELD,
        "2026-08-24T13:00:00+03:00",
        "2026-08-24T10:00:00+00:00",
    )


def test_ambiguous_inventory_requires_point_and_mode_clarification(db_session) -> None:
    warehouse = _warehouse(db_session, "mitino", "Митино")
    _message(
        db_session,
        message_id=20,
        text_value="Полный список 241500",
        at=datetime(2026, 8, 24, 10),
    )
    settings = _settings()
    stats = pickup_control.persist_pending_inventory_messages(
        db_session,
        settings=settings,
        queue_clarification_cards=True,
    )
    submission = db_session.scalar(
        select(PickupInventorySubmission).where(
            PickupInventorySubmission.status == pickup_inventory.STATUS_MANUAL_REVIEW
        )
    )
    assert stats["cards_queued"] == 1
    assert submission is not None and submission.warehouse_id is None

    client = FakeClient()
    bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True),
        apply_enabled_probe=lambda: True,
        limit=10,
    )
    db_session.refresh(submission)
    point_token = bot.sign_inventory_callback_token(
        submission,
        action=bot.INVENTORY_ACTION_SELECT_POINT,
        secret="test-secret",
        warehouse_external_id="mitino",
    )
    bot.queue_inventory_clarification_action(
        db_session,
        token=point_token,
        actor_id="7",
        dialog_id="chat8961",
        settings=settings,
    )
    bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True),
        apply_enabled_probe=lambda: True,
        limit=10,
    )
    selected = db_session.scalar(
        select(PickupInventorySubmission).where(
            PickupInventorySubmission.warehouse_id == warehouse.id,
            PickupInventorySubmission.status == pickup_inventory.STATUS_MANUAL_REVIEW,
        )
    )
    assert selected is not None

    full_token = bot.sign_inventory_callback_token(
        selected,
        action=bot.INVENTORY_ACTION_FULL,
        secret="test-secret",
    )
    bot.queue_inventory_clarification_action(
        db_session,
        token=full_token,
        actor_id="7",
        dialog_id="chat8961",
        settings=settings,
    )
    bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True),
        apply_enabled_probe=lambda: True,
        limit=10,
    )
    confirmed = db_session.scalar(
        select(PickupInventorySubmission)
        .where(PickupInventorySubmission.status == pickup_inventory.STATUS_CONFIRMED)
        .order_by(PickupInventorySubmission.id.desc())
    )
    assert confirmed is not None and confirmed.warehouse_id == warehouse.id
    assert [item.site_order_number for item in confirmed.items] == ["241500"]
    assert client.bot_messages
    assert client.bot_updates[-1]["keyboard"] == []


def test_disabled_inventory_card_waits_without_blocking_other_outbox(db_session) -> None:
    _message(
        db_session,
        message_id=120,
        text_value="Полный список 241500",
        at=datetime(2026, 8, 24, 10),
    )
    settings = _settings(order_fulfillment_pickup_inventory_enabled=False)
    stats = pickup_control.persist_pending_inventory_messages(
        db_session,
        settings=settings,
        queue_clarification_cards=True,
    )
    assert stats["cards_queued"] == 1
    generic_candidates = bot.create_candidates_from_message(
        db_session,
        dialog_id="chat8729",
        message_id="121",
        author_id="7",
        text_value="Заказ 241501 прибыл в Митино",
        message_at=datetime(2026, 8, 24, 10, 1),
        settings=settings,
        now=datetime(2026, 8, 24, 10, 1),
    )
    assert len(generic_candidates) == 1
    client = FakeClient()

    result = bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(available=True),
        apply_enabled_probe=lambda: True,
        limit=10,
    )

    row = db_session.scalar(
        select(SiteOrderFulfillmentOutbox).where(
            SiteOrderFulfillmentOutbox.operation == bot.OP_PUBLISH_INVENTORY_CLARIFICATION
        )
    )
    assert result["selected"] == 1
    assert result["completed"] == 1
    assert row is not None and row.status == bot.OUTBOX_PENDING
    assert len(client.bot_messages) == 1


def test_stage_outbox_waits_when_profile_flag_is_disabled(db_session) -> None:
    warehouse = _warehouse(db_session, "mitino", "Митино")
    case = SiteOrderExecutionCase(
        site_order_number="241500",
        bitrix_deal_id=500,
        delivery_method="Самовывоз",
        current_derived_status=fulfillment.EVENT_PICKUP_STORED,
        current_crm_stage="FINAL_INVOICE",
        pickup_point_warehouse_id=warehouse.id,
        storage_started_at=datetime(2026, 8, 24, 9),
        payload={},
    )
    db_session.add(case)
    db_session.flush()
    row = bot.enqueue_outbox(
        db_session,
        operation=bot.OP_UPDATE_CRM_STAGE,
        idempotency_key="test:stage-flag-off",
        target_type="deal",
        target_id="500",
        payload={
            "site_order_number": "241500",
            "before_stage": "FINAL_INVOICE",
            "target_stage": fulfillment.CRM_STAGE_PICKUP_WAITING,
        },
        now=datetime(2026, 8, 24, 10),
    )
    db_session.commit()
    client = FakeClient(stage="FINAL_INVOICE")

    result = bot.process_outbox(
        db_session,
        client=client,
        settings=_settings(order_fulfillment_pickup_stage_apply_enabled=False),
        onec_validator=lambda _: bot.OneCPickupValidation(available=True),
        apply_enabled_probe=lambda: True,
        limit=10,
        now=datetime(2026, 8, 24, 10),
    )

    db_session.refresh(row)
    assert result["selected"] == 0
    assert row.status == bot.OUTBOX_PENDING
    assert client.stage_updates == []


def test_inventory_won_outbox_waits_when_won_flag_is_disabled(db_session) -> None:
    warehouse = _warehouse(db_session, "mitino", "Митино")
    db_session.add(
        SiteOrderExecutionCase(
            site_order_number="241500",
            bitrix_deal_id=500,
            delivery_method="Самовывоз",
            current_derived_status=fulfillment.EVENT_PICKUP_STORED,
            current_crm_stage=fulfillment.CRM_STAGE_PICKUP_WAITING,
            pickup_point_warehouse_id=warehouse.id,
            storage_started_at=datetime(2026, 8, 24, 9),
            payload={},
        )
    )
    row = bot.enqueue_outbox(
        db_session,
        operation=bot.OP_UPDATE_CRM_STAGE,
        idempotency_key="test:inventory-won-flag-off",
        target_type="deal",
        target_id="500",
        payload={
            "site_order_number": "241500",
            "before_stage": fulfillment.CRM_STAGE_PICKUP_WAITING,
            "target_stage": "WON",
            "feature_guard": "inventory_won",
        },
        now=datetime(2026, 8, 24, 10),
    )
    db_session.commit()
    client = FakeClient()

    result = bot.process_outbox(
        db_session,
        client=client,
        settings=_settings(order_fulfillment_inventory_won_enabled=False),
        onec_validator=lambda _: bot.OneCPickupValidation(available=True),
        apply_enabled_probe=lambda: True,
        limit=10,
        now=datetime(2026, 8, 24, 10),
    )

    db_session.refresh(row)
    assert result["selected"] == 0
    assert row.status == bot.OUTBOX_PENDING
    assert client.stage_updates == []


def test_movement_from_waiting_redirects_without_leaving_pickup_stage(db_session) -> None:
    expected = _warehouse(db_session, "mitino", "Митино")
    target = _warehouse(db_session, "lublino", "Люблино")
    case = SiteOrderExecutionCase(
        site_order_number="241500",
        bitrix_deal_id=500,
        delivery_method="Самовывоз",
        current_derived_status=fulfillment.EVENT_PICKUP_STORED,
        current_crm_stage=fulfillment.CRM_STAGE_PICKUP_WAITING,
        pickup_point_warehouse_id=expected.id,
        storage_started_at=datetime(2026, 8, 24, 9),
        payload={},
    )
    candidate = BitrixChatActionCandidate(
        source_chat_id="chat729",
        source_message_id="121",
        source_event_at=datetime(2026, 8, 24, 10),
        site_order_number="241500",
        detected_action=bot.ACTION_MOVING,
        pickup_point_warehouse_id=target.id,
        pickup_point_name=target.name,
        status=bot.CANDIDATE_OPEN,
        expires_at=datetime(2026, 8, 25, 10),
        nonce="redirect",
        dry_run=False,
        payload={},
    )
    deal = fulfillment.BitrixDealSnapshot(
        deal_id=500,
        stage_id=fulfillment.CRM_STAGE_PICKUP_WAITING,
        delivery="Самовывоз Митино",
        raw={fulfillment.CRM_ORDER_NUMBER_FIELD: "241500"},
    )

    decision = bot.decide_pickup_action(
        action=bot.ACTION_MOVING,
        confirmation_step=1,
        deal=deal,
        candidate=candidate,
        case=case,
        onec=bot.OneCPickupValidation(available=True, assembled=True),
        settings=_settings(),
        now=datetime(2026, 8, 24, 10),
    )

    assert decision.allowed is True
    assert decision.target_stage is None
    assert decision.event_type == fulfillment.EVENT_PICKUP_REDIRECTED


def test_operational_metrics_exclude_historical_cases_from_new_sla(db_session) -> None:
    db_session.add_all(
        [
            SiteOrderExecutionCase(
                site_order_number="241500",
                current_derived_status=fulfillment.EVENT_PICKUP_STORED,
                current_crm_stage=fulfillment.CRM_STAGE_PICKUP_WAITING,
                storage_started_at=datetime(2026, 8, 19, 9),
                payload={},
            ),
            SiteOrderExecutionCase(
                site_order_number="241501",
                current_derived_status=fulfillment.EVENT_PICKUP_STORED,
                current_crm_stage=fulfillment.CRM_STAGE_PICKUP_WAITING,
                storage_started_at=datetime(2026, 8, 21, 9),
                payload={},
            ),
        ]
    )
    db_session.commit()

    metrics = pickup_control.pickup_operational_metrics(
        db_session,
        settings=_settings(order_fulfillment_bot_cutover_at=datetime(2026, 8, 20, tzinfo=UTC)),
        now=datetime(2026, 8, 24, 10),
    )

    assert metrics["pickup_without_notification"] == 1


def test_disappearance_uses_latest_point_state_and_newer_events(db_session) -> None:
    _warehouse(db_session, "mitino", "Митино")
    _warehouse(db_session, "lub lino", "Люблино")
    previous = pickup_inventory.persist_inventory_message(
        db_session,
        message=_message(
            db_session,
            message_id=11,
            text_value="Митино 241500",
            at=datetime(2026, 8, 24, 10),
        ),
    )
    pickup_inventory.persist_inventory_message(
        db_session,
        message=_message(
            db_session,
            message_id=12,
            text_value="Люблино 241500",
            at=datetime(2026, 8, 24, 10, 30),
        ),
    )
    current = pickup_inventory.persist_inventory_message(
        db_session,
        message=_message(
            db_session,
            message_id=13,
            text_value="Митино невыданных нет",
            at=datetime(2026, 8, 24, 11),
        ),
    )
    candidate = pickup_inventory.disappearance_candidates(
        db_session,
        current_submission=current,
    )[0]
    assert candidate.previous_submission_id == previous.id
    assert pickup_inventory.disappearance_is_uncontested(
        db_session,
        candidate=candidate,
    ) == (False, "present_in_newer_inventory")

    pickup_inventory.persist_inventory_message(
        db_session,
        message=_message(
            db_session,
            message_id=14,
            text_value="Люблино невыданных нет",
            at=datetime(2026, 8, 24, 12),
        ),
    )
    assert pickup_inventory.disappearance_is_uncontested(
        db_session,
        candidate=candidate,
    ) == (True, "confirmed_disappearance")

    fulfillment.upsert_execution_event(
        db_session,
        site_order_number="241500",
        event_type=fulfillment.EVENT_PICKUP_REDIRECTED,
        event_at=datetime(2026, 8, 24, 10, 30),
        source="test",
        source_ref="late-move",
        confidence="strong",
        raw_message_id=None,
        payload={},
    )
    assert pickup_inventory.disappearance_is_uncontested(
        db_session,
        candidate=candidate,
    ) == (False, "newer_blocking_event")


def test_trusted_reaction_starts_sla_and_revocation_is_recorded_once(db_session) -> None:
    warehouse = _warehouse(db_session, "mitino", "Митино")
    case = SiteOrderExecutionCase(
        site_order_number="241500",
        bitrix_deal_id=500,
        delivery_method="Самовывоз",
        current_derived_status=fulfillment.EVENT_PICKUP_STORED,
        current_crm_stage=fulfillment.CRM_STAGE_PICKUP_WAITING,
        pickup_point_warehouse_id=warehouse.id,
        storage_started_at=datetime(2026, 8, 24, 9),
        payload={},
    )
    db_session.add(case)
    ingested = fulfillment.ingest_bitrix_message(
        db_session,
        chat_code=fulfillment.CHAT_PICKUP_READY,
        dialog_id="chat8729",
        chat_id=8729,
        message_id=20,
        message_at=datetime(2026, 8, 24, 10),
        author_id="7",
        text_value="Заказ 241500 готов к выдаче",
        payload={"params": {"LIKE": ["131016"]}},
    )
    reaction = db_session.scalar(select(BitrixChatReaction))

    stats = pickup_control.reconcile_trusted_notification_reactions(
        db_session,
        settings=_settings(),
        now=datetime(2026, 8, 24, 10, 5),
    )
    db_session.refresh(case)
    assert stats["confirmed"] == 1
    assert case.notification_confirmed_at == reaction.first_seen_at
    assert case.sla_started_at == max(case.storage_started_at, reaction.first_seen_at)
    assert (
        db_session.scalar(
            select(func.count(SiteOrderFulfillmentOutbox.id)).where(
                SiteOrderFulfillmentOutbox.operation == bot.OP_UPDATE_CRM_FIELDS
            )
        )
        == 1
    )

    reaction.is_active = False
    reaction.removed_at = datetime(2026, 8, 24, 11)
    db_session.commit()
    first_revoke = pickup_control.reconcile_trusted_notification_reactions(
        db_session,
        settings=_settings(),
        now=datetime(2026, 8, 24, 11),
    )
    second_revoke = pickup_control.reconcile_trusted_notification_reactions(
        db_session,
        settings=_settings(),
        now=datetime(2026, 8, 24, 11, 5),
    )
    db_session.refresh(case)
    assert ingested.message.id > 0
    assert first_revoke["revoked"] == 1
    assert second_revoke["revoked"] == 0
    assert case.current_derived_status == "manual_review"
    assert (
        db_session.scalar(
            select(func.count(SiteOrderExecutionEvent.id)).where(
                SiteOrderExecutionEvent.event_type == fulfillment.EVENT_PICKUP_NOTIFICATION_REVOKED
            )
        )
        == 1
    )


def test_sla_creates_72_and_96_tasks_once_without_stage_change(db_session) -> None:
    warehouse = _warehouse(db_session, "mitino", "Митино")
    settings = _settings(
        order_fulfillment_point_task_routes={"mitino": {"operator": 200, "senior": 201}}
    )
    started = datetime(2026, 8, 20, 12)
    db_session.add(
        SiteOrderExecutionCase(
            site_order_number="241500",
            bitrix_deal_id=500,
            delivery_method="Самовывоз",
            current_derived_status=fulfillment.EVENT_PICKUP_STORED,
            current_crm_stage=fulfillment.CRM_STAGE_PICKUP_WAITING,
            pickup_point_warehouse_id=warehouse.id,
            storage_started_at=started,
            notification_confirmed_at=started,
            sla_started_at=started,
            payload={},
        )
    )
    db_session.commit()

    assert (
        bot.enqueue_due_sla_tasks(
            db_session,
            settings=settings,
            now=started + timedelta(hours=73),
        )
        == 1
    )
    assert (
        bot.enqueue_due_sla_tasks(
            db_session,
            settings=settings,
            now=started + timedelta(hours=97),
        )
        == 1
    )
    assert (
        bot.enqueue_due_sla_tasks(
            db_session,
            settings=settings,
            now=started + timedelta(hours=98),
        )
        == 0
    )
    task_kinds = [
        (row.payload or {}).get("task_kind")
        for row in db_session.scalars(
            select(SiteOrderFulfillmentOutbox).where(
                SiteOrderFulfillmentOutbox.operation == bot.OP_CREATE_TASK
            )
        ).all()
    ]
    assert task_kinds == ["call", "dismantle_review"]
    assert (
        db_session.scalar(
            select(func.count(SiteOrderFulfillmentOutbox.id)).where(
                SiteOrderFulfillmentOutbox.operation == bot.OP_UPDATE_CRM_STAGE
            )
        )
        == 0
    )
    assert (
        db_session.scalar(
            select(func.count(SiteOrderExecutionEvent.id)).where(
                SiteOrderExecutionEvent.event_type == fulfillment.EVENT_PICKUP_DISMANTLE_CANDIDATE
            )
        )
        == 1
    )


def test_sla_fails_closed_when_route_map_is_incomplete(db_session) -> None:
    warehouse = _warehouse(db_session, "mitino", "Митино")
    started = datetime(2026, 8, 20, 12)
    db_session.add(
        SiteOrderExecutionCase(
            site_order_number="241500",
            bitrix_deal_id=500,
            delivery_method="Самовывоз",
            current_derived_status=fulfillment.EVENT_PICKUP_STORED,
            current_crm_stage=fulfillment.CRM_STAGE_PICKUP_WAITING,
            pickup_point_warehouse_id=warehouse.id,
            storage_started_at=started,
            notification_confirmed_at=started,
            sla_started_at=started,
            payload={},
        )
    )
    db_session.commit()

    with pytest.raises(bot.TaskRouteConfigurationError, match="mitino:operator"):
        bot.enqueue_due_sla_tasks(
            db_session,
            settings=_settings(order_fulfillment_point_task_routes={}),
            now=started + timedelta(hours=73),
        )
    assert db_session.scalar(select(func.count(SiteOrderFulfillmentOutbox.id))) == 0


def test_marker_and_hold_readback_start_sla_and_revision_tasks(db_session) -> None:
    warehouse = _warehouse(db_session, "mitino", "Митино")
    settings = _settings(
        order_fulfillment_point_task_routes={"mitino": {"operator": 200, "senior": 201}}
    )
    case = SiteOrderExecutionCase(
        site_order_number="241500",
        bitrix_deal_id=500,
        delivery_method="Самовывоз",
        current_derived_status=fulfillment.EVENT_PICKUP_STORED,
        current_crm_stage=fulfillment.CRM_STAGE_PICKUP_WAITING,
        pickup_point_warehouse_id=warehouse.id,
        storage_started_at=datetime(2026, 8, 24, 9),
        payload={},
    )
    db_session.add(case)
    db_session.commit()
    client = FakeClient()
    client.raw[settings.order_fulfillment_bot_pickup_sms_field] = "2026-08-24T10:00:00"
    client.raw[bot.CRM_PICKUP_HOLD_UNTIL_FIELD] = "2026-08-27"

    first = bot.reconcile_pickup_case_fields(
        db_session,
        client=client,
        settings=settings,
        now=datetime(2026, 8, 24, 10, 5),
    )
    db_session.refresh(case)
    assert first["notification_confirmed"] == 1
    assert first["hold_changed"] == 1
    assert case.sla_started_at == datetime(2026, 8, 24, 10)
    assert case.hold_until == date(2026, 8, 27)
    assert (case.payload or {})["hold_revision"] == 1

    client.raw[bot.CRM_PICKUP_HOLD_UNTIL_FIELD] = ""
    bot.reconcile_pickup_case_fields(
        db_session,
        client=client,
        settings=settings,
        now=datetime(2026, 8, 24, 11),
    )
    client.raw[bot.CRM_PICKUP_HOLD_UNTIL_FIELD] = "2026-08-27"
    bot.reconcile_pickup_case_fields(
        db_session,
        client=client,
        settings=settings,
        now=datetime(2026, 8, 24, 12),
    )
    db_session.refresh(case)
    assert (case.payload or {})["hold_revision"] == 3
    assert (
        db_session.scalar(
            select(func.count(SiteOrderExecutionEvent.id)).where(
                SiteOrderExecutionEvent.event_type == "pickup_hold_changed"
            )
        )
        == 3
    )

    assert (
        bot.enqueue_due_sla_tasks(
            db_session,
            settings=settings,
            now=datetime(2026, 8, 27, 9),
        )
        == 1
    )
    hold_task = db_session.scalar(
        select(SiteOrderFulfillmentOutbox).where(
            SiteOrderFulfillmentOutbox.idempotency_key.like("%task:hold_call:%")
        )
    )
    assert hold_task is not None
    assert ":hold_call:3:2026-08-27" in hold_task.idempotency_key

    bot.reconcile_pickup_case_fields(
        db_session,
        client=client,
        settings=settings,
        now=datetime(2026, 8, 28, 9),
    )
    db_session.refresh(case)
    assert case.current_derived_status == "manual_review"


def test_inventory_won_requires_allowlisted_point(db_session) -> None:
    warehouse = _warehouse(db_session, "mitino", "Митино")
    previous = pickup_inventory.persist_inventory_message(
        db_session,
        message=_message(
            db_session,
            message_id=31,
            text_value="Митино 241500",
            at=datetime(2026, 8, 24, 10),
        ),
    )
    current = pickup_inventory.persist_inventory_message(
        db_session,
        message=_message(
            db_session,
            message_id=32,
            text_value="Митино невыданных нет",
            at=datetime(2026, 8, 24, 11),
        ),
    )
    db_session.add(
        SiteOrderExecutionCase(
            site_order_number="241500",
            bitrix_deal_id=500,
            delivery_method="Самовывоз",
            current_derived_status=fulfillment.EVENT_PICKUP_STORED,
            current_crm_stage=fulfillment.CRM_STAGE_PICKUP_WAITING,
            pickup_point_warehouse_id=warehouse.id,
            storage_started_at=datetime(2026, 8, 24, 9),
            payload={},
        )
    )
    db_session.commit()
    assert previous.id == current.supersedes_submission_id
    client = FakeClient()

    def onec(_: str) -> bot.OneCPickupValidation:
        return bot.OneCPickupValidation(available=True, assembled=True)

    dry_run = pickup_control.enqueue_inventory_won_candidates(
        db_session,
        client=client,
        settings=_settings(order_fulfillment_inventory_won_enabled=False),
        onec_validator=onec,
    )
    assert dry_run["dry_run_ready"] == 1

    blocked = pickup_control.enqueue_inventory_won_candidates(
        db_session,
        client=client,
        settings=_settings(order_fulfillment_inventory_won_enabled=True),
        onec_validator=onec,
    )
    assert blocked["queued"] == 0

    enabled_settings = _settings(
        order_fulfillment_inventory_won_enabled=True,
        order_fulfillment_inventory_won_warehouse_external_ids=["mitino"],
    )
    queued = pickup_control.enqueue_inventory_won_candidates(
        db_session,
        client=client,
        settings=enabled_settings,
        onec_validator=onec,
        now=datetime(2026, 8, 24, 11),
    )
    assert queued["queued"] == 1
    assert (
        db_session.scalar(
            select(func.count(SiteOrderFulfillmentOutbox.id)).where(
                SiteOrderFulfillmentOutbox.operation == bot.OP_UPDATE_CRM_STAGE
            )
        )
        == 1
    )
    fulfillment.upsert_execution_event(
        db_session,
        site_order_number="241500",
        event_type=fulfillment.EVENT_PICKUP_REDIRECTED,
        event_at=datetime(2026, 8, 24, 12),
        source="test",
        source_ref="movement-after-inventory-enqueue",
        confidence="strong",
        raw_message_id=None,
        payload={},
    )
    db_session.commit()

    result = bot.process_outbox(
        db_session,
        client=client,
        settings=enabled_settings,
        onec_validator=onec,
        apply_enabled_probe=lambda: True,
        limit=10,
        now=datetime(2026, 8, 24, 12),
    )
    stage_row = db_session.scalar(
        select(SiteOrderFulfillmentOutbox).where(
            SiteOrderFulfillmentOutbox.operation == bot.OP_UPDATE_CRM_STAGE
        )
    )
    assert result["failed"] >= 1
    assert stage_row is not None and stage_row.status == bot.OUTBOX_FAILED
    assert client.stage_updates == []


def test_historical_inventory_disappearance_builds_approved_won_batch(db_session) -> None:
    warehouse = _warehouse(db_session, "mitino", "Митино")
    previous = pickup_inventory.persist_inventory_message(
        db_session,
        message=_message(
            db_session,
            message_id=70,
            text_value="Митино 241500",
            at=datetime(2026, 8, 1, 10),
        ),
    )
    current = pickup_inventory.persist_inventory_message(
        db_session,
        message=_message(
            db_session,
            message_id=71,
            text_value="Митино невыданных нет",
            at=datetime(2026, 8, 2, 10),
        ),
    )
    assert previous is not None and current is not None
    db_session.add(
        SiteOrderExecutionCase(
            site_order_number="241500",
            bitrix_deal_id=500,
            delivery_method="Самовывоз",
            current_derived_status=fulfillment.EVENT_PICKUP_STORED,
            current_crm_stage=fulfillment.CRM_STAGE_PICKUP_WAITING,
            pickup_point_warehouse_id=warehouse.id,
            storage_started_at=datetime(2026, 8, 1, 10),
            payload={},
        )
    )
    db_session.commit()

    rows = pickup_history.assess_historical_pickup_cases(
        db_session,
        client=FakeClient(),
        settings=_settings(),
        onec_validator=lambda _: bot.OneCPickupValidation(available=True, assembled=True),
    )
    assert len(rows) == 1
    assert rows[0].queue == pickup_history.QUEUE_WON
    assert rows[0].target_stage == "WON"
    batch_id = pickup_history.approved_batch_id(rows)
    with pytest.raises(ValueError, match="historical_batch_approval_mismatch"):
        pickup_history.enqueue_approved_batch(
            db_session,
            rows=rows,
            approved_id="wrong",
            settings=_settings(),
        )
    result = pickup_history.enqueue_approved_batch(
        db_session,
        rows=rows,
        approved_id=batch_id,
        settings=_settings(),
        now=datetime(2026, 8, 2, 11),
    )
    assert result["queued"] == 1
    assert db_session.scalar(select(func.count(SiteOrderFulfillmentOutbox.id))) == 3
    pickup_inventory.persist_inventory_message(
        db_session,
        message=_message(
            db_session,
            message_id=72,
            text_value="Митино 241500",
            at=datetime(2026, 8, 3, 10),
        ),
    )
    db_session.commit()
    client = FakeClient()

    apply_result = bot.process_outbox(
        db_session,
        client=client,
        settings=_settings(),
        onec_validator=lambda _: bot.OneCPickupValidation(available=True, assembled=True),
        apply_enabled_probe=lambda: True,
        limit=10,
        now=datetime(2026, 8, 3, 11),
    )
    stage_row = db_session.scalar(
        select(SiteOrderFulfillmentOutbox).where(
            SiteOrderFulfillmentOutbox.operation == bot.OP_UPDATE_CRM_STAGE
        )
    )
    assert apply_result["failed"] >= 1
    assert stage_row is not None and stage_row.status == bot.OUTBOX_FAILED
    assert client.stage_updates == []


def test_historical_current_inventory_blocks_false_won(db_session) -> None:
    warehouse = _warehouse(db_session, "mitino", "Митино")
    pickup_inventory.persist_inventory_message(
        db_session,
        message=_message(
            db_session,
            message_id=72,
            text_value="Митино 241500",
            at=datetime(2026, 8, 2, 10),
        ),
    )
    db_session.add(
        SiteOrderExecutionCase(
            site_order_number="241500",
            bitrix_deal_id=500,
            delivery_method="Самовывоз",
            current_derived_status=fulfillment.EVENT_PICKUP_STORED,
            current_crm_stage=fulfillment.CRM_STAGE_PICKUP_WAITING,
            pickup_point_warehouse_id=warehouse.id,
            storage_started_at=datetime(2026, 8, 1, 10),
            payload={},
        )
    )
    db_session.commit()

    rows = pickup_history.assess_historical_pickup_cases(
        db_session,
        client=FakeClient(),
        settings=_settings(),
        onec_validator=lambda _: bot.OneCPickupValidation(
            available=True,
            assembled=True,
            issued_confirmed=True,
        ),
    )
    assert rows[0].queue == pickup_history.QUEUE_MANUAL
    assert rows[0].reason == "current_inventory_conflicts_with_closure"


def test_twenty_historical_composite_checks_produce_no_false_won(db_session) -> None:
    warehouse = _warehouse(db_session, "mitino", "Митино")
    order_numbers = [str(241500 + index) for index in range(20)]
    pickup_inventory.persist_inventory_message(
        db_session,
        message=_message(
            db_session,
            message_id=73,
            text_value=f"Митино полный список: {', '.join(order_numbers)}",
            at=datetime(2026, 8, 2, 10),
        ),
    )
    for index, order_number in enumerate(order_numbers, start=1):
        db_session.add(
            SiteOrderExecutionCase(
                site_order_number=order_number,
                bitrix_deal_id=500 + index,
                delivery_method="Самовывоз",
                current_derived_status=fulfillment.EVENT_PICKUP_STORED,
                current_crm_stage=fulfillment.CRM_STAGE_PICKUP_WAITING,
                pickup_point_warehouse_id=warehouse.id,
                storage_started_at=datetime(2026, 8, 1, 10),
                payload={},
            )
        )
    db_session.commit()

    class MultiDealClient:
        def list_deals_by_site_order(self, order_number: str):
            index = order_numbers.index(order_number) + 1
            return [
                fulfillment.BitrixDealSnapshot(
                    deal_id=500 + index,
                    stage_id=fulfillment.CRM_STAGE_PICKUP_WAITING,
                    delivery="Самовывоз",
                    raw={fulfillment.CRM_ORDER_NUMBER_FIELD: order_number},
                )
            ]

    rows = pickup_history.assess_historical_pickup_cases(
        db_session,
        client=MultiDealClient(),
        settings=_settings(),
        onec_validator=lambda _: bot.OneCPickupValidation(
            available=True,
            assembled=True,
            issued_confirmed=True,
        ),
    )
    assert len(rows) == 20
    assert all(row.queue == pickup_history.QUEUE_MANUAL for row in rows)
    assert not any(row.queue == pickup_history.QUEUE_WON for row in rows)


def test_lost_order_found_other_uses_signed_warehouse_selection(db_session) -> None:
    expected = _warehouse(db_session, "mitino", "Митино")
    other = _warehouse(db_session, "lublino", "Люблино")
    settings = _settings(
        order_fulfillment_point_task_routes={
            "mitino": {"operator": 200, "senior": 201},
            "lublino": {"operator": 202, "senior": 203},
        }
    )
    case = SiteOrderExecutionCase(
        site_order_number="241500",
        bitrix_deal_id=500,
        delivery_method="Самовывоз",
        current_derived_status="manual_review",
        current_crm_stage=fulfillment.CRM_STAGE_PICKUP_WAITING,
        pickup_point_warehouse_id=expected.id,
        storage_started_at=datetime(2026, 8, 24, 9),
        payload={},
    )
    candidate = BitrixChatActionCandidate(
        source_chat_id="chat739",
        source_message_id="40",
        source_event_at=datetime(2026, 8, 24, 10),
        site_order_number="241500",
        bitrix_deal_id=500,
        detected_action=bot.ACTION_NOT_FOUND,
        pickup_point_warehouse_id=expected.id,
        pickup_point_name=expected.name,
        status=bot.CANDIDATE_CONFIRMATION,
        expires_at=datetime(2026, 8, 25, 10),
        nonce="lost-order-nonce",
        active_action=bot.ACTION_FOUND_OTHER,
        active_actor_id="7",
        bot_message_id="9001",
        dry_run=False,
        payload={},
    )
    db_session.add_all([case, candidate])
    db_session.flush()
    first = BitrixChatAction(
        candidate_id=candidate.id,
        action=bot.ACTION_FOUND_OTHER,
        actor_id="7",
        status="awaiting_confirmation",
        confirmation_step=1,
        idempotency_key="lost-found-other-step-1",
        payload={},
    )
    db_session.add(first)
    publish = bot.enqueue_outbox(
        db_session,
        candidate=candidate,
        operation=bot.OP_PUBLISH_CARD,
        idempotency_key=f"candidate:{candidate.id}:publish",
        payload={},
        now=datetime(2026, 8, 24, 10),
    )
    publish.status = bot.OUTBOX_COMPLETED
    db_session.commit()
    client = FakeClient()

    bot._publish_confirmation(  # noqa: SLF001
        db_session,
        candidate,
        action=first,
        client=client,
        settings=settings,
    )
    warehouse_button = next(
        item for item in client.bot_updates[-1]["keyboard"] if item["TEXT"] == "Люблино"
    )
    decoded = bot.verify_callback_token(
        warehouse_button["COMMAND_PARAMS"],
        secret="test-secret",
        now=datetime(2026, 8, 24, 10, 1),
    )
    assert decoded.target_warehouse_id == other.id

    bot.queue_callback_action(
        db_session,
        token=warehouse_button["COMMAND_PARAMS"],
        actor_id="7",
        dialog_id="chat739",
        settings=settings,
        now=datetime(2026, 8, 24, 10, 1),
    )
    for minute in range(2, 6):
        bot.process_outbox(
            db_session,
            client=client,
            settings=settings,
            onec_validator=lambda _: bot.OneCPickupValidation(available=True),
            now=datetime(2026, 8, 24, 10, minute),
        )
    db_session.refresh(case)
    assert case.pickup_point_warehouse_id == other.id
    assert case.current_crm_stage == fulfillment.CRM_STAGE_PICKUP_WAITING
    assert client.stage_updates == []


def test_returned_action_requires_onec_return_without_payment(db_session) -> None:
    warehouse = _warehouse(db_session, "mitino", "Митино")
    case = SiteOrderExecutionCase(
        site_order_number="241500",
        bitrix_deal_id=500,
        delivery_method="Самовывоз",
        current_derived_status=fulfillment.EVENT_PICKUP_DISMANTLING,
        current_crm_stage="DISMANTLING",
        pickup_point_warehouse_id=warehouse.id,
        storage_started_at=datetime(2026, 8, 20, 9),
        payload={},
    )
    candidate = BitrixChatActionCandidate(
        source_chat_id="chat739",
        source_message_id="41",
        source_event_at=datetime(2026, 8, 24, 10),
        site_order_number="241500",
        detected_action=bot.ACTION_RETURNED,
        pickup_point_warehouse_id=warehouse.id,
        pickup_point_name=warehouse.name,
        status=bot.CANDIDATE_OPEN,
        expires_at=datetime(2026, 8, 25, 10),
        nonce="returned-nonce",
        dry_run=False,
        payload={},
    )
    deal = fulfillment.BitrixDealSnapshot(
        deal_id=500,
        stage_id="DISMANTLING",
        delivery="Самовывоз",
    )

    missing = bot.decide_pickup_action(
        action=bot.ACTION_RETURNED,
        confirmation_step=2,
        deal=deal,
        candidate=candidate,
        case=case,
        onec=bot.OneCPickupValidation(available=True),
        settings=_settings(),
    )
    paid = bot.decide_pickup_action(
        action=bot.ACTION_RETURNED,
        confirmation_step=2,
        deal=deal,
        candidate=candidate,
        case=case,
        onec=bot.OneCPickupValidation(
            available=True,
            return_confirmed=True,
            payment_confirmed=True,
        ),
        settings=_settings(),
    )
    allowed = bot.decide_pickup_action(
        action=bot.ACTION_RETURNED,
        confirmation_step=2,
        deal=deal,
        candidate=candidate,
        case=case,
        onec=bot.OneCPickupValidation(available=True, return_confirmed=True),
        settings=_settings(),
    )

    assert missing.reason == "return_not_confirmed"
    assert paid.reason == "return_payment_conflict"
    assert allowed.allowed is True
    assert allowed.target_stage == "LOSE"


class MissingReceiptClient:
    def __init__(self, order_numbers: list[str], *, point_name: str = "Митино") -> None:
        self.order_numbers = set(order_numbers)
        self.point_name = point_name
        self.stage_by_order = {order_number: "FINAL_INVOICE" for order_number in order_numbers}
        self.bot_messages: list[dict] = []
        self.tasks: list[dict] = []
        self.stage_updates: list[str] = []

    def _deal(self, order_number: str) -> fulfillment.BitrixDealSnapshot:
        return fulfillment.BitrixDealSnapshot(
            deal_id=int(order_number),
            stage_id=self.stage_by_order[order_number],
            delivery="Самовывоз",
            post_delivery_type=self.point_name,
            raw={fulfillment.CRM_ORDER_NUMBER_FIELD: order_number},
        )

    def list_deals_by_site_order(self, order_number: str):
        return [self._deal(order_number)] if order_number in self.order_numbers else []

    def add_bot_message(self, **payload):
        self.bot_messages.append(payload)
        return str(9500 + len(self.bot_messages))

    def get_user_by_id(self, user_id: int):
        return {"ID": str(user_id), "ACTIVE": "Y"}

    def add_task(self, fields: dict):
        self.tasks.append(fields)
        return {"task": {"id": len(self.tasks)}}


def _missing_receipt_settings(**overrides) -> Settings:
    values = {
        "order_fulfillment_bot_apply_enabled": False,
        "order_fulfillment_pickup_stage_apply_enabled": False,
        "order_fulfillment_pickup_evidence_tracking_enabled": True,
        "order_fulfillment_pickup_evidence_cutover_at": datetime(2026, 8, 20, tzinfo=UTC),
        "order_fulfillment_pickup_missing_receipt_enabled": True,
        "order_fulfillment_pickup_receipt_question_after_hours": 24,
        "order_fulfillment_pickup_receipt_task_after_hours": 48,
        "order_fulfillment_point_task_routes": {"mitino": {"operator": 11, "senior": 12}},
    }
    values.update(overrides)
    return _settings(**values)


def _record_movement(
    db_session,
    *,
    order_number: str,
    warehouse: LogisticsWarehouse,
    raw_message: BitrixChatMessage,
    event_at: datetime,
) -> SiteOrderExecutionEvent:
    event = fulfillment.upsert_execution_event(
        db_session,
        site_order_number=order_number,
        event_type=fulfillment.EVENT_PICKUP_MOVING,
        event_at=event_at,
        source="bitrix_chat",
        source_ref=f"pickup_evidence:{raw_message.id}",
        confidence="strong",
        raw_message_id=raw_message.id,
        warehouse_id=warehouse.id,
        payload={"strict": True, "silent": True},
    )
    assert event is not None
    case = db_session.get(SiteOrderExecutionCase, event.case_id)
    assert case is not None
    case.bitrix_deal_id = int(order_number)
    case.delivery_method = "Самовывоз"
    case.current_crm_stage = "FINAL_INVOICE"
    case.pickup_point_warehouse_id = warehouse.id
    db_session.commit()
    return event


def test_strict_dispatch_and_receipt_are_recorded_silently_and_idempotently(
    db_session,
) -> None:
    warehouse = _warehouse(db_session, "mitino", "Митино")
    _message(
        db_session,
        message_id=501,
        text_value="Митино: заказы 241500 241501 отправили",
        at=datetime(2026, 8, 24, 8),
        chat_code=fulfillment.CHAT_SITE_MASTER_MOBILE,
        dialog_id="chat733",
    )
    _message(
        db_session,
        message_id=502,
        text_value="Митино: 241500 получили",
        at=datetime(2026, 8, 24, 12),
        chat_code=fulfillment.CHAT_PICKUP_READY,
        dialog_id="chat8729",
    )
    client = MissingReceiptClient(["241500", "241501"])
    settings = _missing_receipt_settings()

    first = pickup_control.reconcile_strict_pickup_evidence(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(
            available=True,
            assembled=True,
        ),
        now=datetime(2026, 8, 24, 13),
    )
    candidate_stats = pickup_control.create_missing_pickup_candidates(
        db_session,
        settings=settings,
    )
    second = pickup_control.reconcile_strict_pickup_evidence(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(
            available=True,
            assembled=True,
        ),
        now=datetime(2026, 8, 24, 13),
    )

    events = db_session.scalars(
        select(SiteOrderExecutionEvent).order_by(SiteOrderExecutionEvent.id)
    ).all()
    assert first == {"checked": 2, "recorded": 3, "duplicate": 0, "blocked": 0}
    assert candidate_stats["created"] == 0
    assert second == {"checked": 2, "recorded": 0, "duplicate": 3, "blocked": 0}
    assert [event.event_type for event in events] == [
        fulfillment.EVENT_PICKUP_MOVING,
        fulfillment.EVENT_PICKUP_MOVING,
        fulfillment.EVENT_PICKUP_STORED,
    ]
    assert all(event.warehouse_id == warehouse.id for event in events)
    assert client.bot_messages == []
    assert client.tasks == []
    assert client.stage_updates == []


def test_strict_receipt_from_bot_itself_is_never_evidence(db_session) -> None:
    _warehouse(db_session, "mitino", "Митино")
    message = _message(
        db_session,
        message_id=508,
        text_value="Митино: 241500 получили",
        at=datetime(2026, 8, 24, 12),
        chat_code=fulfillment.CHAT_PICKUP_READY,
        dialog_id="chat8729",
    )
    message.author_id = "42"
    db_session.commit()

    stats = pickup_control.reconcile_strict_pickup_evidence(
        db_session,
        client=MissingReceiptClient(["241500"]),
        settings=_missing_receipt_settings(),
        onec_validator=lambda _: bot.OneCPickupValidation(
            available=True,
            assembled=True,
        ),
        now=datetime(2026, 8, 24, 13),
    )

    assert stats["recorded"] == 0
    assert db_session.scalar(select(func.count(SiteOrderExecutionEvent.id))) == 0


def test_evidence_tracking_requires_a_dedicated_cutover(db_session) -> None:
    _warehouse(db_session, "mitino", "Митино")
    _message(
        db_session,
        message_id=510,
        text_value="Митино: заказ 241500 отправили",
        at=datetime(2026, 8, 24, 8),
        chat_code=fulfillment.CHAT_SITE_MASTER_MOBILE,
        dialog_id="chat733",
    )
    onec_calls: list[str] = []
    settings = _missing_receipt_settings(order_fulfillment_pickup_evidence_cutover_at=None)

    evidence = pickup_control.reconcile_strict_pickup_evidence(
        db_session,
        client=MissingReceiptClient(["241500"]),
        settings=settings,
        onec_validator=lambda order_number: (
            onec_calls.append(order_number)
            or bot.OneCPickupValidation(available=True, assembled=True)
        ),
        now=datetime(2026, 8, 25, 8),
    )
    followups = pickup_control.enqueue_missing_receipt_followups(
        db_session,
        settings=settings,
        now=datetime(2026, 8, 25, 8),
    )

    assert evidence == {"checked": 0, "recorded": 0, "duplicate": 0, "blocked": 0}
    assert followups["checked"] == 0
    assert onec_calls == []
    assert db_session.scalar(select(func.count(SiteOrderExecutionEvent.id))) == 0


def test_strict_evidence_blocks_conflicting_case_and_deal_points(db_session) -> None:
    mitino = _warehouse(db_session, "mitino", "Митино")
    other = _warehouse(db_session, "other", "Другая точка")
    message = _message(
        db_session,
        message_id=503,
        text_value="Митино: заказ 241500 отправили",
        at=datetime(2026, 8, 24, 8),
        chat_code=fulfillment.CHAT_SITE_MASTER_MOBILE,
        dialog_id="chat733",
    )
    db_session.add(
        SiteOrderExecutionCase(
            site_order_number="241500",
            bitrix_deal_id=241500,
            delivery_method="Самовывоз",
            current_derived_status="manual_review",
            current_crm_stage="FINAL_INVOICE",
            pickup_point_warehouse_id=other.id,
            payload={},
        )
    )
    db_session.commit()
    onec_calls: list[str] = []

    stats = pickup_control.reconcile_strict_pickup_evidence(
        db_session,
        client=MissingReceiptClient(["241500"]),
        settings=_missing_receipt_settings(),
        onec_validator=lambda order_number: (
            onec_calls.append(order_number)
            or bot.OneCPickupValidation(available=True, assembled=True)
        ),
        now=datetime(2026, 8, 24, 9),
    )

    assert message.id is not None and mitino.id != other.id
    assert stats["blocked"] == 1
    assert onec_calls == []
    assert db_session.scalar(select(func.count(SiteOrderExecutionEvent.id))) == 0


def test_missing_receipt_queues_one_grouped_prompt_at_24h_and_tasks_at_48h(
    db_session,
) -> None:
    warehouse = _warehouse(db_session, "mitino", "Митино")
    source = _message(
        db_session,
        message_id=504,
        text_value="Митино: заказы 241500 241501 отправили",
        at=datetime(2026, 8, 24, 8),
        chat_code=fulfillment.CHAT_SITE_MASTER_MOBILE,
        dialog_id="chat733",
    )
    _record_movement(
        db_session,
        order_number="241500",
        warehouse=warehouse,
        raw_message=source,
        event_at=datetime(2026, 8, 24, 8),
    )
    _record_movement(
        db_session,
        order_number="241501",
        warehouse=warehouse,
        raw_message=source,
        event_at=datetime(2026, 8, 24, 8),
    )
    settings = _missing_receipt_settings()
    client = MissingReceiptClient(["241500", "241501"])

    early = pickup_control.enqueue_missing_receipt_followups(
        db_session,
        settings=settings,
        now=datetime(2026, 8, 25, 7, 59),
    )
    due = pickup_control.enqueue_missing_receipt_followups(
        db_session,
        settings=settings,
        now=datetime(2026, 8, 25, 8),
    )
    metrics = pickup_control.pickup_operational_metrics(
        db_session,
        settings=settings,
        now=datetime(2026, 8, 25, 8),
    )
    repeated = pickup_control.enqueue_missing_receipt_followups(
        db_session,
        settings=settings,
        now=datetime(2026, 8, 25, 8, 1),
    )
    prompt_result = bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(
            available=True,
            assembled=True,
        ),
        missing_receipt_enabled_probe=lambda: True,
        now=datetime(2026, 8, 25, 8, 2),
    )
    tasks_due = pickup_control.enqueue_missing_receipt_followups(
        db_session,
        settings=settings,
        now=datetime(2026, 8, 26, 8),
    )
    task_result = bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(
            available=True,
            assembled=True,
        ),
        missing_receipt_enabled_probe=lambda: True,
        now=datetime(2026, 8, 26, 8, 1),
    )

    assert early["due"] == 0
    assert due["prompt_queued"] == 1 and due["task_queued"] == 0
    assert metrics["missing_receipt_due"] == 2
    assert repeated["prompt_queued"] == 0
    assert prompt_result["completed"] == 1
    assert len(client.bot_messages) == 1
    assert "241500 241501" in client.bot_messages[0]["message"]
    assert client.bot_messages[0]["dialog_id"] == "chat8729"
    assert tasks_due["prompt_queued"] == 0 and tasks_due["task_queued"] == 2
    assert task_result["completed"] == 2
    assert len(client.tasks) == 2
    assert {task["RESPONSIBLE_ID"] for task in client.tasks} == {11}
    assert {tuple(task["ACCOMPLICES"]) for task in client.tasks} == {(12,)}
    assert client.stage_updates == []


def test_late_receipt_before_dispatch_suppresses_prompt_and_task(db_session) -> None:
    warehouse = _warehouse(db_session, "mitino", "Митино")
    source = _message(
        db_session,
        message_id=505,
        text_value="Митино: заказ 241500 отправили",
        at=datetime(2026, 8, 24, 8),
        chat_code=fulfillment.CHAT_SITE_MASTER_MOBILE,
        dialog_id="chat733",
    )
    movement = _record_movement(
        db_session,
        order_number="241500",
        warehouse=warehouse,
        raw_message=source,
        event_at=datetime(2026, 8, 24, 8),
    )
    settings = _missing_receipt_settings()
    pickup_control.enqueue_missing_receipt_followups(
        db_session,
        settings=settings,
        now=datetime(2026, 8, 25, 8),
    )
    receipt = fulfillment.upsert_execution_event(
        db_session,
        site_order_number="241500",
        event_type=fulfillment.EVENT_PICKUP_STORED,
        event_at=datetime(2026, 8, 25, 8, 1),
        source="bitrix_chat",
        source_ref="pickup_evidence:late",
        confidence="strong",
        raw_message_id=None,
        warehouse_id=warehouse.id,
        payload={},
    )
    assert receipt is not None and movement.id is not None
    db_session.commit()
    client = MissingReceiptClient(["241500"])

    result = bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(
            available=True,
            assembled=True,
        ),
        missing_receipt_enabled_probe=lambda: True,
        now=datetime(2026, 8, 25, 8, 2),
    )
    later = pickup_control.enqueue_missing_receipt_followups(
        db_session,
        settings=settings,
        now=datetime(2026, 8, 26, 8),
    )
    row = db_session.scalar(select(SiteOrderFulfillmentOutbox))

    assert result["completed"] == 1
    assert row is not None
    assert (row.payload or {}).get("suppressed_reason") == ("all_movements_closed_before_prompt")
    assert later["task_queued"] == 0
    assert client.bot_messages == []
    assert client.tasks == []


def test_missing_receipt_flag_false_records_overdue_without_external_outbox(
    db_session,
) -> None:
    warehouse = _warehouse(db_session, "mitino", "Митино")
    source = _message(
        db_session,
        message_id=506,
        text_value="Митино: заказ 241500 отправили",
        at=datetime(2026, 8, 24, 8),
        chat_code=fulfillment.CHAT_SITE_MASTER_MOBILE,
        dialog_id="chat733",
    )
    _record_movement(
        db_session,
        order_number="241500",
        warehouse=warehouse,
        raw_message=source,
        event_at=datetime(2026, 8, 24, 8),
    )

    stats = pickup_control.enqueue_missing_receipt_followups(
        db_session,
        settings=_missing_receipt_settings(order_fulfillment_pickup_missing_receipt_enabled=False),
        now=datetime(2026, 8, 25, 8),
    )

    assert stats["due"] == 1 and stats["dry_run"] == 1
    assert db_session.scalar(select(func.count(SiteOrderFulfillmentOutbox.id))) == 0
    assert (
        db_session.scalar(
            select(func.count(SiteOrderExecutionEvent.id)).where(
                SiteOrderExecutionEvent.event_type == fulfillment.EVENT_PICKUP_RECEIPT_OVERDUE
            )
        )
        == 1
    )


def test_missing_receipt_runtime_kill_switch_leaves_queued_prompt_pending(
    db_session,
) -> None:
    warehouse = _warehouse(db_session, "mitino", "Митино")
    source = _message(
        db_session,
        message_id=507,
        text_value="Митино: заказ 241500 отправили",
        at=datetime(2026, 8, 24, 8),
        chat_code=fulfillment.CHAT_SITE_MASTER_MOBILE,
        dialog_id="chat733",
    )
    _record_movement(
        db_session,
        order_number="241500",
        warehouse=warehouse,
        raw_message=source,
        event_at=datetime(2026, 8, 24, 8),
    )
    settings = _missing_receipt_settings()
    pickup_control.enqueue_missing_receipt_followups(
        db_session,
        settings=settings,
        now=datetime(2026, 8, 25, 8),
    )
    client = MissingReceiptClient(["241500"])

    result = bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(
            available=True,
            assembled=True,
        ),
        missing_receipt_enabled_probe=lambda: False,
        now=datetime(2026, 8, 25, 8, 1),
    )
    row = db_session.scalar(select(SiteOrderFulfillmentOutbox))

    assert result["selected"] == 0
    assert row is not None and row.status == bot.OUTBOX_PENDING and row.attempts == 0
    assert client.bot_messages == []


def test_closed_deal_suppresses_queued_missing_receipt_prompt(db_session) -> None:
    warehouse = _warehouse(db_session, "mitino", "Митино")
    source = _message(
        db_session,
        message_id=509,
        text_value="Митино: заказ 241500 отправили",
        at=datetime(2026, 8, 24, 8),
        chat_code=fulfillment.CHAT_SITE_MASTER_MOBILE,
        dialog_id="chat733",
    )
    _record_movement(
        db_session,
        order_number="241500",
        warehouse=warehouse,
        raw_message=source,
        event_at=datetime(2026, 8, 24, 8),
    )
    settings = _missing_receipt_settings()
    pickup_control.enqueue_missing_receipt_followups(
        db_session,
        settings=settings,
        now=datetime(2026, 8, 25, 8),
    )
    client = MissingReceiptClient(["241500"])
    client.stage_by_order["241500"] = "WON"

    result = bot.process_outbox(
        db_session,
        client=client,
        settings=settings,
        onec_validator=lambda _: bot.OneCPickupValidation(
            available=True,
            assembled=True,
        ),
        missing_receipt_enabled_probe=lambda: True,
        now=datetime(2026, 8, 25, 8, 1),
    )
    row = db_session.scalar(select(SiteOrderFulfillmentOutbox))

    assert result["completed"] == 1
    assert row is not None
    assert (row.payload or {}).get("suppressed_movements") == {
        str((row.payload or {})["movement_event_ids"][0]): "deal_closed"
    }
    assert client.bot_messages == []
