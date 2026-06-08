from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.site_order_fulfillment import (
    BitrixChatMention,
    BitrixChatMessage,
    SiteOrderExecutionCase,
    SiteOrderExecutionEvent,
)
from app.services import site_order_fulfillment as service

SITE_ORDER_TABLES = [
    SiteOrderExecutionCase.__table__,
    BitrixChatMessage.__table__,
    BitrixChatMention.__table__,
    SiteOrderExecutionEvent.__table__,
]


def test_site_chat_parser_extracts_many_orders_without_word_order() -> None:
    mentions = service.parse_site_chat_text(
        "218014\n217624\nне забрали спб садовая [USER=7]Ариф Рахманов[/USER]"
    )

    assert [mention.site_order_number for mention in mentions] == ["218014", "217624"]
    assert {mention.event_type for mention in mentions} == {service.EVENT_PICKUP_UNCLAIMED}
    assert {mention.confidence for mention in mentions} == {"medium"}
    assert "<order>" in mentions[0].evidence_text
    assert "218014" not in mentions[0].evidence_text
    assert "Ариф Рахманов" not in mentions[0].evidence_text


def test_site_chat_parser_does_not_infer_pickup_from_absence_in_list() -> None:
    mentions = service.parse_site_chat_text("218014\n217624\nспб садовая")

    assert mentions == []


def test_courier_ocr_classifier_distinguishes_payment_state() -> None:
    pending = service.parse_courier_ocr_payload(
        {
            "site_order_number": "218530",
            "delivery_status": "delivered",
            "payment_collected": False,
            "confidence": 0.92,
        }
    )
    paid = service.parse_courier_ocr_payload(
        {
            "orders": ["218531"],
            "status": "доставлено",
            "amount": "120 руб.",
            "confidence": 0.8,
        }
    )
    failed = service.parse_courier_ocr_payload(
        {
            "order_number": "218532",
            "status": "не доставлен, перенос",
            "paid": False,
            "confidence": 0.7,
        }
    )

    assert pending[0].event_type == service.EVENT_COURIER_DELIVERED_PENDING
    assert paid[0].event_type == service.EVENT_COURIER_DELIVERED_PAID
    assert failed[0].event_type in {
        service.EVENT_COURIER_FAILED,
        service.EVENT_COURIER_RESCHEDULED,
    }


def test_delivery_method_report_flags_unknown_watch_and_empty_values() -> None:
    report = service.build_delivery_method_report_from_rows(
        [
            ("Самовывоз", 10),
            ("Самовывоз Самовывоз", 20),
            ("Савелово Магазин", 30),
            ("Теплый Стан", 31),
            ("Горбушка", 32),
            ("Без доставки", 33),
            ("СДЭК (Доставка курьером)", 40),
            ("Доставка курьером (от 400 руб.)", 50),
            ("Dostavista", 60),
            ("Такси", 2),
            ("Достависта", 3),
            ("EMS", 4),
            ("", 1),
        ]
    )

    by_method = {row.raw_delivery_method: row for row in report}
    assert set(by_method) == {"EMS", "<empty>"}
    assert by_method["EMS"].status == "watch"
    assert by_method["<empty>"].status == "unknown"


def test_delivery_method_classifier_normalizes_site_variants() -> None:
    assert service.classify_delivery_method("Самовывоз Самовывоз") == "pickup"
    assert service.classify_delivery_method("Горбушкин Двор Магазин") == "pickup"
    assert service.classify_delivery_method("Теплый Стан") == "pickup"
    assert service.classify_delivery_method("Люблино") == "pickup"
    assert service.classify_delivery_method("Без доставки") == "pickup"
    assert service.classify_delivery_method("СДЭК (Самовывоз)") == "carrier"
    assert service.classify_delivery_method("Почта России (Доставка в отделение)") == "carrier"
    assert service.classify_delivery_method("Доставка курьером (от 400 руб.)") == "courier"
    assert service.classify_delivery_method("Dostavista") == "courier"


def test_ingest_is_idempotent_and_delivery_without_payment_is_not_won() -> None:
    engine = create_engine("sqlite:///:memory:")
    for table in SITE_ORDER_TABLES:
        table.create(engine)

    with Session(engine) as session:
        first = service.ingest_bitrix_message(
            session,
            chat_code=service.CHAT_COURIER_SPB,
            dialog_id="chat727",
            chat_id=727,
            message_id=1001,
            ocr_payloads=[
                {
                    "orders": ["218530"],
                    "delivery_status": "delivered",
                    "payment_collected": False,
                    "confidence": 0.9,
                }
            ],
        )
        second = service.ingest_bitrix_message(
            session,
            chat_code=service.CHAT_COURIER_SPB,
            dialog_id="chat727",
            chat_id=727,
            message_id=1001,
            ocr_payloads=[
                {
                    "orders": ["218530"],
                    "delivery_status": "delivered",
                    "payment_collected": False,
                    "confidence": 0.9,
                }
            ],
        )

        case = session.scalar(
            select(SiteOrderExecutionCase).where(
                SiteOrderExecutionCase.site_order_number == "218530"
            )
        )
        recommendations = service.build_recommendations(session)

    assert first.duplicate_message is False
    assert second.duplicate_message is True
    assert len(first.events) == 1
    assert case is not None
    assert case.current_derived_status == service.EVENT_COURIER_DELIVERED_PENDING
    assert recommendations[0]["recommended_stage"] == "IN_DELIVERY"
    assert recommendations[0]["recommended_stage"] != "WON"


def test_review_enrichment_merges_case_bitrix_and_onec() -> None:
    engine = create_engine("sqlite:///:memory:")
    for table in SITE_ORDER_TABLES:
        table.create(engine)

    with Session(engine) as session:
        service.ingest_bitrix_message(
            session,
            chat_code=service.CHAT_SITE_MASTER_MOBILE,
            dialog_id="chat733",
            chat_id=733,
            message_id=2001,
            text_value="218014 не забрали спб садовая",
        )
        rows = service.build_review_rows(
            session,
            deals_by_order={
                "218014": [
                    service.BitrixDealSnapshot(
                        deal_id=11412,
                        stage_id="NEW",
                        delivery="Самовывоз",
                        payment_status="0",
                    )
                ]
            },
            onec_by_order={
                "218014": service.OneCOrderSnapshot(
                    site_order_number="218014",
                    order_date=datetime(2026, 5, 21, 12, 0, 0),
                    raw_delivery="Самовывоз",
                    courier="",
                    delivery_cost=Decimal("0.00"),
                )
            },
        )

    assert len(rows) == 1
    row = rows[0]
    assert row.bitrix_deal_id == 11412
    assert row.crm_stage == "NEW"
    assert row.crm_delivery == "Самовывоз"
    assert row.onec_raw_delivery == "Самовывоз"
    assert row.recommended_stage == "PICKUP_WAITING"
    assert row.action == "update_stage"
    assert row.manual_review_reason is None
    assert row.recommended_stage != "LOSE"


def test_review_marks_multiple_bitrix_deals_manual_review() -> None:
    engine = create_engine("sqlite:///:memory:")
    for table in SITE_ORDER_TABLES:
        table.create(engine)

    with Session(engine) as session:
        service.ingest_bitrix_message(
            session,
            chat_code=service.CHAT_SITE_MASTER_MOBILE,
            dialog_id="chat733",
            chat_id=733,
            message_id=2002,
            text_value="218015 не забрали",
        )
        rows = service.build_review_rows(
            session,
            deals_by_order={
                "218015": [
                    service.BitrixDealSnapshot(deal_id=1, delivery="Самовывоз"),
                    service.BitrixDealSnapshot(deal_id=2, delivery="Самовывоз"),
                ]
            },
            onec_by_order={},
    )

    assert rows[0].action == "manual_review"
    assert rows[0].recommended_stage == service.CRM_STAGE_MANUAL_REVIEW
    assert rows[0].manual_review_reason == "multiple_bitrix_deals"


def test_review_courier_delivered_unpaid_is_not_won() -> None:
    engine = create_engine("sqlite:///:memory:")
    for table in SITE_ORDER_TABLES:
        table.create(engine)

    with Session(engine) as session:
        service.ingest_bitrix_message(
            session,
            chat_code=service.CHAT_COURIER_SPB,
            dialog_id="chat727",
            chat_id=727,
            message_id=2003,
            ocr_payloads=[
                {
                    "orders": ["218530"],
                    "delivery_status": "delivered",
                    "payment_collected": False,
                    "confidence": 0.9,
                }
            ],
        )
        rows = service.build_review_rows(
            session,
            deals_by_order={
                "218530": [
                    service.BitrixDealSnapshot(
                        deal_id=11624,
                        stage_id="NEW",
                        delivery="Доставка курьером",
                        payment_status="0",
                    )
                ]
            },
            onec_by_order={
                "218530": service.OneCOrderSnapshot(
                    site_order_number="218530",
                    raw_delivery="Доставка курьером",
                )
            },
        )

    assert rows[0].chat_event == service.EVENT_COURIER_DELIVERED_PENDING
    assert rows[0].recommended_stage == "IN_DELIVERY"
    assert rows[0].recommended_stage != "WON"


def test_review_pickup_received_closes_internal_pickup_without_site_payment_flag() -> None:
    engine = create_engine("sqlite:///:memory:")
    for table in SITE_ORDER_TABLES:
        table.create(engine)

    with Session(engine) as session:
        service.ingest_bitrix_message(
            session,
            chat_code=service.CHAT_SITE_MASTER_MOBILE,
            dialog_id="chat733",
            chat_id=733,
            message_id=2004,
            text_value="218016 забрали",
        )
        rows = service.build_review_rows(
            session,
            deals_by_order={
                "218016": [
                    service.BitrixDealSnapshot(
                        deal_id=3,
                        delivery="Самовывоз",
                        payment_status="0",
                    )
                ]
            },
            onec_by_order={},
        )

    assert rows[0].action == "update_stage"
    assert rows[0].recommended_stage == "WON"
    assert rows[0].manual_review_reason is None


def test_review_pickup_received_for_non_pickup_requires_manual_review() -> None:
    engine = create_engine("sqlite:///:memory:")
    for table in SITE_ORDER_TABLES:
        table.create(engine)

    with Session(engine) as session:
        service.ingest_bitrix_message(
            session,
            chat_code=service.CHAT_SITE_MASTER_MOBILE,
            dialog_id="chat733",
            chat_id=733,
            message_id=20040,
            text_value="218016 забрали",
        )
        rows = service.build_review_rows(
            session,
            deals_by_order={
                "218016": [
                    service.BitrixDealSnapshot(
                        deal_id=3,
                        delivery="СДЭК",
                        payment_status="0",
                    )
                ]
            },
            onec_by_order={},
        )

    assert rows[0].action == "manual_review"
    assert rows[0].recommended_stage == service.CRM_STAGE_MANUAL_REVIEW
    assert "delivery_conflict" in rows[0].manual_review_reason


def test_review_does_not_update_terminal_crm_stage() -> None:
    engine = create_engine("sqlite:///:memory:")
    for table in SITE_ORDER_TABLES:
        table.create(engine)

    with Session(engine) as session:
        service.ingest_bitrix_message(
            session,
            chat_code=service.CHAT_SITE_MASTER_MOBILE,
            dialog_id="chat733",
            chat_id=733,
            message_id=2005,
            text_value="218017 не забрали",
        )
        rows = service.build_review_rows(
            session,
            deals_by_order={
                "218017": [
                    service.BitrixDealSnapshot(
                        deal_id=4,
                        stage_id="WON",
                        delivery="Самовывоз",
                        payment_status="1",
                    )
                ]
            },
            onec_by_order={},
        )

    assert rows[0].action == "manual_review"
    assert rows[0].recommended_stage == "WON"
    assert rows[0].manual_review_reason == "terminal_crm_stage"


def test_review_csv_does_not_include_raw_secret_payload(tmp_path) -> None:
    row = service.OrderFulfillmentReviewRow(
        site_order_number="218014",
        bitrix_deal_id=11412,
        crm_stage="NEW",
        crm_delivery="Самовывоз",
        crm_payment_status="0",
        onec_raw_delivery="Самовывоз",
        onec_order_date=None,
        onec_courier=None,
        onec_delivery_cost=None,
        chat_event=service.EVENT_PICKUP_UNCLAIMED,
        event_confidence="medium",
        evidence_redacted="<order> не забрали",
        recommended_stage="PICKUP_WAITING",
        action="update_stage",
        manual_review_reason=None,
    )

    path = service.write_review_csv(tmp_path / "review.csv", [row])
    content = path.read_text(encoding="utf-8-sig")

    assert "https://crm.master-mobile.ru/rest/1/secret" not in content
    assert "<order> не забрали" in content


def test_stage_outbox_builds_ready_update_rows(tmp_path) -> None:
    review_row = service.OrderFulfillmentReviewRow(
        site_order_number="218014",
        bitrix_deal_id=11412,
        crm_stage="PREPARATION",
        crm_delivery="Самовывоз",
        crm_payment_status="0",
        onec_raw_delivery="Самовывоз",
        onec_order_date=None,
        onec_courier=None,
        onec_delivery_cost=None,
        chat_event=service.EVENT_PICKUP_UNCLAIMED,
        event_confidence="medium",
        evidence_redacted="<order> не забрали",
        recommended_stage="PICKUP_WAITING",
        action="update_stage",
        manual_review_reason=None,
    )

    rows = service.build_stage_outbox_rows(
        [review_row],
        available_stage_ids={"PREPARATION", "PICKUP_WAITING"},
    )
    path = service.write_stage_outbox_csv(tmp_path / "stage-outbox.csv", rows)
    content = path.read_text(encoding="utf-8-sig")

    assert len(rows) == 1
    assert rows[0].state == "ready"
    assert rows[0].operation == "update_stage"
    assert rows[0].target_stage == "PICKUP_WAITING"
    assert json.loads(rows[0].payload_json) == {
        "id": 11412,
        "fields": {"STAGE_ID": "PICKUP_WAITING"},
    }
    assert "https://crm.master-mobile.ru/rest/1/secret" not in content


def test_stage_outbox_blocks_missing_target_stage_and_skips_manual_review() -> None:
    update_row = service.OrderFulfillmentReviewRow(
        site_order_number="218014",
        bitrix_deal_id=11412,
        crm_stage="PREPARATION",
        crm_delivery="Самовывоз",
        crm_payment_status="0",
        onec_raw_delivery="Самовывоз",
        onec_order_date=None,
        onec_courier=None,
        onec_delivery_cost=None,
        chat_event=service.EVENT_PICKUP_UNCLAIMED,
        event_confidence="medium",
        evidence_redacted="<order> не забрали",
        recommended_stage="PICKUP_WAITING",
        action="update_stage",
        manual_review_reason=None,
    )
    manual_row = service.OrderFulfillmentReviewRow(
        site_order_number="224236",
        bitrix_deal_id=None,
        crm_stage=None,
        crm_delivery=None,
        crm_payment_status=None,
        onec_raw_delivery=None,
        onec_order_date=None,
        onec_courier=None,
        onec_delivery_cost=None,
        chat_event=service.EVENT_COURIER_IN_PROGRESS,
        event_confidence="medium",
        evidence_redacted=None,
        recommended_stage=service.CRM_STAGE_MANUAL_REVIEW,
        action="manual_review",
        manual_review_reason="bitrix_deal_not_found",
    )

    rows = service.build_stage_outbox_rows(
        [update_row, manual_row],
        available_stage_ids={"PREPARATION"},
    )

    assert len(rows) == 1
    assert rows[0].state == "blocked_missing_target_stage"
    assert rows[0].block_reason == "target_stage_not_found:PICKUP_WAITING"
