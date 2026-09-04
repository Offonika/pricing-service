from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services import site_order_fulfillment as service
from infra.cron import order_fulfillment_sync as sync


def test_load_env_files_uses_later_files_only_as_fallback(tmp_path: Path) -> None:
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text(
        "DATABASE_URL=postgresql://runtime/pricing\nRUNTIME_ONLY=enabled\n",
        encoding="utf-8",
    )
    legacy_env = tmp_path / "legacy.env"
    legacy_env.write_text(
        "DATABASE_URL=postgresql://legacy/call_analytics\nLEGACY_ONLY=enabled\n",
        encoding="utf-8",
    )

    values = sync.load_env_files((runtime_env, legacy_env))

    assert values == {
        "DATABASE_URL": "postgresql://runtime/pricing",
        "RUNTIME_ONLY": "enabled",
        "LEGACY_ONLY": "enabled",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "0xA01F0025901E48EE11EDD836014C4EDA",
            "014c4eda-d836-11ed-a01f-0025901e48ee",
        ),
        (
            "0x9E79002590803DAF11EFEAC20A909912",
            "0a909912-eac2-11ef-9e79-002590803daf",
        ),
        (
            "0xAC00002590803DAF11EEF8C0276D8052",
            "276d8052-f8c0-11ee-ac00-002590803daf",
        ),
        ("not-a-reference", "not-a-reference"),
    ],
)
def test_normalize_onec_rref_matches_bitrix_xml_id(raw: str, expected: str) -> None:
    assert sync.normalize_onec_rref(raw) == expected


class FakeBitrixClient:
    def __init__(self, deals: dict[int, service.BitrixDealSnapshot]) -> None:
        self.deals = deals
        self.updates: list[tuple[int, str]] = []

    def get_deal_by_id(self, deal_id: int) -> service.BitrixDealSnapshot | None:
        return self.deals.get(deal_id)

    def update_deal_stage(self, deal_id: int, target_stage: str) -> bool:
        self.updates.append((deal_id, target_stage))
        return True


class FakeListClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call(self, method: str, payload: dict) -> dict:
        self.calls.append({"method": method, "payload": payload})
        return {"result": [{"ID": "1"}], "total": 42}


class FlakyListClient:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def call(self, method: str, payload: dict) -> dict:
        del method, payload
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise service.BitrixChatError("crm.deal.list: http_500 Internal Server Error")
        return {"result": [{"ID": "1"}], "total": 42}


class FakeDealListClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call(self, method: str, payload: dict) -> dict:
        self.calls.append({"method": method, "payload": payload})
        stage_id = payload["filter"]["STAGE_ID"]
        return {
            "result": [
                {
                    "ID": str(len(self.calls)),
                    "TITLE": f"Заказ интернет-магазина {stage_id}",
                    "STAGE_ID": stage_id,
                    service.CRM_ORDER_NUMBER_FIELD: str(218000 + len(self.calls)),
                }
            ]
        }


class FakeNotifyClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call(self, method: str, payload: dict) -> dict:
        self.calls.append({"method": method, "payload": payload})
        return {"result": 1000 + len(self.calls)}


class FakeExecutingScanClient:
    def __init__(self, *, recent_ids: list[int], historical_ids: list[int]) -> None:
        self.recent_ids = recent_ids
        self.historical_ids = historical_ids
        self.calls: list[dict] = []

    def call(self, method: str, payload: dict) -> dict:
        assert method == "crm.deal.list"
        self.calls.append(payload)
        ids = self.recent_ids if payload["order"]["ID"] == "DESC" else self.historical_ids
        return {
            "result": [
                {
                    "ID": str(deal_id),
                    "STAGE_ID": "EXECUTING",
                    service.CRM_ORDER_NUMBER_FIELD: str(240000 + deal_id),
                }
                for deal_id in ids
            ]
        }


def _deal(
    *,
    deal_id: int = 100,
    stage_id: str = "NEW",
    order_number: str = "218001",
    delivery: str = "Самовывоз",
    payment_status: str = "0",
    assembled: str | None = None,
) -> service.BitrixDealSnapshot:
    raw = {service.CRM_ORDER_NUMBER_FIELD: order_number}
    if assembled is not None:
        raw[sync.CRM_ASSEMBLED_FIELD] = assembled
    return service.BitrixDealSnapshot(
        deal_id=deal_id,
        stage_id=stage_id,
        delivery=delivery,
        payment_status=payment_status,
        raw=raw,
    )


def test_decide_new_deal_stage_for_safe_v1_rules() -> None:
    assert (
        sync.decide_new_deal_stage(
            _deal(delivery="Самовывоз", payment_status="0")
        ).recommended_stage
        == "EXECUTING"
    )
    assert (
        sync.decide_new_deal_stage(_deal(delivery="Курьер", payment_status="0")).recommended_stage
        == "EXECUTING"
    )
    assert (
        sync.decide_new_deal_stage(_deal(delivery="СДЭК", payment_status="0")).recommended_stage
        == "PREPAYMENT_INVOICE"
    )
    assert (
        sync.decide_new_deal_stage(
            _deal(delivery="СДЭК (Самовывоз)", payment_status="0")
        ).recommended_stage
        == "PREPAYMENT_INVOICE"
    )
    assert (
        sync.decide_new_deal_stage(_deal(delivery="СДЭК", payment_status="1")).recommended_stage
        == "EXECUTING"
    )
    assert (
        sync.decide_new_deal_stage(
            _deal(stage_id="PREPARATION", delivery="Самовывоз", payment_status="0")
        ).recommended_stage
        == "EXECUTING"
    )
    assert (
        sync.decide_new_deal_stage(
            _deal(stage_id="PREPARATION", delivery="Курьер", payment_status="1")
        ).recommended_stage
        == "EXECUTING"
    )
    assert (
        sync.decide_new_deal_stage(
            _deal(stage_id="PREPARATION", delivery="Курьер", payment_status="0")
        ).recommended_stage
        == "EXECUTING"
    )
    assert (
        sync.decide_new_deal_stage(
            _deal(delivery="Доставка курьером", payment_status="0")
        ).recommended_stage
        == "EXECUTING"
    )
    assert (
        sync.decide_new_deal_stage(
            _deal(delivery="Dostavista", payment_status="0")
        ).recommended_stage
        == "EXECUTING"
    )


def test_executing_scan_prioritizes_recent_and_rotates_history(tmp_path: Path) -> None:
    client = FakeExecutingScanClient(
        recent_ids=[500, 499],
        historical_ids=[101, 102, 103],
    )

    scan = sync.fetch_executing_deal_scan(
        client,
        recent_limit=2,
        historical_limit=3,
        cursor=100,
    )
    state_path = tmp_path / "cursor.json"
    sync.save_executing_cursor(state_path, scan)

    assert {deal.deal_id for deal in scan.deals} == {101, 102, 103, 499, 500}
    assert scan.recent_deal_ids == {499, 500}
    assert scan.historical_deal_ids == {101, 102, 103}
    assert scan.cursor_before == 100
    assert scan.cursor_after == 103
    assert scan.cycle_completed is False
    assert sync.load_executing_cursor(state_path) == 103
    historical_call = next(call for call in client.calls if call["order"]["ID"] == "ASC")
    assert historical_call["filter"][">ID"] == 100


def test_executing_scan_resets_cursor_after_full_cycle() -> None:
    client = FakeExecutingScanClient(recent_ids=[500], historical_ids=[499])

    scan = sync.fetch_executing_deal_scan(
        client,
        recent_limit=1,
        historical_limit=3,
        cursor=498,
    )

    assert scan.cursor_after == 0
    assert scan.cycle_completed is True


def test_execution_cutover_not_scan_cohort_controls_historical_apply_gate() -> None:
    deal = _deal(
        deal_id=500,
        stage_id="EXECUTING",
        order_number="242901",
        delivery="Самовывоз",
        assembled="1",
    )
    common = {
        "deals": [deal],
        "order_statuses": {},
        "onec_settlements": {},
        "onec_evidence_available": True,
        "cutover_at": datetime.fromisoformat("2026-08-26T00:00:00+03:00"),
    }

    old = sync.build_execution_snapshots(
        **common,
        rtu_signals={
            "242901": {
                "rtu_count": 1,
                "assembled_rtu_count": 1,
                "latest_assembled_at": datetime(2026, 8, 25, 23, 59),
            }
        },
    )
    new = sync.build_execution_snapshots(
        **common,
        rtu_signals={
            "242901": {
                "rtu_count": 1,
                "assembled_rtu_count": 1,
                "latest_assembled_at": datetime(2026, 8, 26, 0, 1),
            }
        },
    )

    assert old[0].historical is True
    assert new[0].historical is False


def test_decide_delivery_review_completed_order_requires_handoff_confirmation() -> None:
    decision = sync.decide_new_deal_stage(
        _deal(stage_id="DELIVERY_REVIEW", delivery="Самовывоз", payment_status="0"),
        order_status=sync.SaleOrderStatus(
            order_number="218001",
            canceled=False,
            status_id="F",
            payed=False,
            created_at=datetime.now() - timedelta(days=1),
        ),
    )

    assert decision.action == "manual_review"
    assert decision.recommended_stage is None
    assert decision.review_reason == "delivery_review_completed_needs_handoff_confirmation"


def test_decide_delivery_review_routes_non_completed_orders() -> None:
    old_unpaid_carrier = sync.decide_new_deal_stage(
        _deal(stage_id="DELIVERY_REVIEW", delivery="СДЭК", payment_status="0"),
        order_status=sync.SaleOrderStatus(
            order_number="218001",
            canceled=False,
            status_id="N",
            payed=False,
            created_at=datetime.now()
            - timedelta(days=sync.PREPAYMENT_WAITING_MAX_AGE_DAYS, minutes=1),
        ),
    )
    fresh_unpaid_carrier = sync.decide_new_deal_stage(
        _deal(stage_id="DELIVERY_REVIEW", delivery="Почта России", payment_status="0"),
        order_status=sync.SaleOrderStatus(
            order_number="218002",
            canceled=False,
            status_id="N",
            payed=False,
            created_at=datetime.now(),
        ),
    )
    assembled_pickup = sync.decide_new_deal_stage(
        _deal(stage_id="DELIVERY_REVIEW", delivery="Самовывоз", assembled="1"),
        order_status=sync.SaleOrderStatus(
            order_number="218003",
            canceled=False,
            status_id="N",
            payed=False,
            created_at=datetime.now(),
        ),
    )
    paid_pickup = sync.decide_new_deal_stage(
        _deal(stage_id="DELIVERY_REVIEW", delivery="Самовывоз", assembled="1"),
        order_status=sync.SaleOrderStatus(
            order_number="218005",
            canceled=False,
            status_id="P",
            payed=True,
            created_at=datetime.now(),
        ),
    )
    paid_courier = sync.decide_new_deal_stage(
        _deal(stage_id="DELIVERY_REVIEW", delivery="Доставка курьером", assembled="1"),
        order_status=sync.SaleOrderStatus(
            order_number="218004",
            canceled=False,
            status_id="P",
            payed=True,
            created_at=datetime.now(),
        ),
    )

    assert old_unpaid_carrier.recommended_stage == "LOSE"
    assert old_unpaid_carrier.review_reason == "delivery_review_unpaid_expired_to_lost"
    assert fresh_unpaid_carrier.recommended_stage == "PREPAYMENT_INVOICE"
    assert fresh_unpaid_carrier.review_reason == "delivery_review_carrier_unpaid_to_payment_waiting"
    assert assembled_pickup.recommended_stage == "FINAL_INVOICE"
    assert assembled_pickup.review_reason == "delivery_review_pickup_ready_for_dispatch"
    assert paid_pickup.recommended_stage == "FINAL_INVOICE"
    assert paid_pickup.review_reason == "delivery_review_paid_pickup_ready_for_dispatch"
    assert paid_courier.recommended_stage == "FINAL_INVOICE"
    assert paid_courier.review_reason == "delivery_review_paid_carrier_assembled"


def test_delivery_review_does_not_expire_cash_on_delivery_orders_as_prepayment() -> None:
    old_courier_cod = sync.decide_new_deal_stage(
        _deal(stage_id="DELIVERY_REVIEW", delivery="Доставка курьером", payment_status="0"),
        order_status=sync.SaleOrderStatus(
            order_number="218005",
            canceled=False,
            status_id="N",
            payed=False,
            created_at=datetime.now() - timedelta(days=sync.PREPAYMENT_WAITING_MAX_AGE_DAYS + 30),
        ),
    )

    assert old_courier_cod.recommended_stage == "EXECUTING"
    assert old_courier_cod.review_reason == "delivery_review_courier_cod_to_assembly"


def test_decide_new_deal_stage_routes_canceled_orders_by_assembly_state() -> None:
    canceled = sync.SaleOrderStatus(
        order_number="218617",
        canceled=True,
        status_id="N",
        payed=False,
    )

    unassembled = sync.decide_new_deal_stage(
        _deal(stage_id="PREPARATION", delivery="Курьер", assembled="0"),
        order_status=canceled,
    )
    assembled = sync.decide_new_deal_stage(
        _deal(stage_id="PREPARATION", delivery="Курьер", assembled="1"),
        order_status=canceled,
    )
    prepayment = sync.decide_new_deal_stage(
        _deal(stage_id="PREPAYMENT_INVOICE", delivery="СДЭК", assembled="0"),
        order_status=canceled,
    )

    assert unassembled.recommended_stage == "LOSE"
    assert unassembled.review_reason == "canceled_unassembled_to_lost"
    assert assembled.recommended_stage == "DISMANTLING"
    assert assembled.review_reason == "canceled_assembled_to_dismantling"
    assert prepayment.recommended_stage == "LOSE"
    assert prepayment.review_reason == "canceled_unassembled_to_lost"


def test_process_stage_inventory_includes_live_partial_shipment_stage() -> None:
    assert "PARTIALLY_SHIPPED" in sync.PROCESS_STAGES
    assert sync.STAGE_RU_LABELS["PARTIALLY_SHIPPED"] == "Частично отправлен"


def test_decide_new_deal_stage_keeps_active_prepayment_waiting() -> None:
    decision = sync.decide_new_deal_stage(
        _deal(stage_id="PREPAYMENT_INVOICE", delivery="СДЭК", payment_status="0"),
        order_status=sync.SaleOrderStatus(
            order_number="218001",
            canceled=False,
            status_id="N",
            payed=False,
            created_at=datetime.now() - timedelta(days=sync.PREPAYMENT_WAITING_MAX_AGE_DAYS - 1),
        ),
    )

    assert decision.action == "manual_review"
    assert decision.recommended_stage is None
    assert decision.review_reason == "prepayment_waiting_payment"


def test_decide_new_deal_stage_keeps_unconfirmed_expired_prepayment_for_review() -> None:
    decision = sync.decide_new_deal_stage(
        _deal(stage_id="PREPAYMENT_INVOICE", delivery="СДЭК", payment_status="0"),
        order_status=sync.SaleOrderStatus(
            order_number="218001",
            canceled=False,
            status_id="N",
            payed=False,
            created_at=datetime.now()
            - timedelta(days=sync.PREPAYMENT_WAITING_MAX_AGE_DAYS, minutes=1),
        ),
    )

    assert decision.action == "manual_review"
    assert decision.recommended_stage is None
    assert decision.review_reason == "prepayment_unpaid_unconfirmed_in_onec"


def test_decide_new_deal_stage_closes_expired_prepayment_with_posted_sale() -> None:
    decision = sync.decide_new_deal_stage(
        _deal(stage_id="PREPAYMENT_INVOICE", delivery="СДЭК", payment_status="0"),
        order_status=sync.SaleOrderStatus(
            order_number="218001",
            canceled=False,
            status_id="N",
            payed=False,
            created_at=datetime.now()
            - timedelta(days=sync.PREPAYMENT_WAITING_MAX_AGE_DAYS, minutes=1),
        ),
        onec_settlement=sync.OneCOrderSettlement(
            order_number="218001",
            posted_sale_count=1,
            posted_sale_amount=Decimal("250.00"),
            payment_amount=None,
            debt_amount=Decimal("250.00"),
            payment_confirmed=False,
            evidence="onec_payment_not_confirmed",
        ),
    )

    assert decision.action == "update_stage"
    assert decision.recommended_stage == "LOSE"
    assert decision.review_reason == "prepayment_unpaid_expired_to_lost"


def test_quick_onec_candidates_include_expired_prepayment_and_completed_pickup() -> None:
    now = datetime.now()
    deals = [
        _deal(deal_id=1, order_number="218001", stage_id="PREPAYMENT_INVOICE", delivery="СДЭК"),
        _deal(deal_id=2, order_number="218002", stage_id="PICKUP_WAITING", delivery="Самовывоз"),
        _deal(deal_id=3, order_number="218003", stage_id="PREPAYMENT_INVOICE", delivery="СДЭК"),
        _deal(deal_id=4, order_number="218004", stage_id="PREPAYMENT_INVOICE", delivery="СДЭК"),
    ]
    statuses = {
        "218001": sync.SaleOrderStatus(
            order_number="218001",
            canceled=False,
            status_id="N",
            payed=False,
            created_at=now - timedelta(days=sync.PREPAYMENT_WAITING_MAX_AGE_DAYS, minutes=1),
        ),
        "218002": sync.SaleOrderStatus(
            order_number="218002",
            canceled=False,
            status_id="F",
            payed=False,
            created_at=now,
        ),
        "218003": sync.SaleOrderStatus(
            order_number="218003",
            canceled=False,
            status_id="N",
            payed=False,
            created_at=now,
        ),
        "218004": sync.SaleOrderStatus(
            order_number="218004",
            canceled=False,
            status_id="F",
            payed=True,
            created_at=now,
        ),
    }

    assert sync.quick_onec_settlement_candidate_orders(deals, statuses) == [
        "218001",
        "218002",
        "218004",
    ]


def test_decide_pickup_waiting_never_closes_without_handoff_confirmation() -> None:
    completed_unpaid = sync.decide_new_deal_stage(
        _deal(stage_id="PICKUP_WAITING", delivery="Самовывоз", payment_status="0"),
        order_status=sync.SaleOrderStatus(
            order_number="218001",
            canceled=False,
            status_id="F",
            payed=False,
            created_at=datetime.now() - timedelta(days=1),
        ),
    )
    completed_paid = sync.decide_new_deal_stage(
        _deal(stage_id="PICKUP_WAITING", delivery="Самовывоз", payment_status="1"),
        order_status=sync.SaleOrderStatus(
            order_number="218001",
            canceled=False,
            status_id="F",
            payed=False,
            created_at=datetime.now() - timedelta(days=1),
        ),
    )
    completed_onec_paid = sync.decide_new_deal_stage(
        _deal(stage_id="PICKUP_WAITING", delivery="Самовывоз", payment_status="0"),
        order_status=sync.SaleOrderStatus(
            order_number="218001",
            canceled=False,
            status_id="F",
            payed=False,
            created_at=datetime.now() - timedelta(days=1),
        ),
        onec_settlement=sync.OneCOrderSettlement(
            order_number="218001",
            posted_sale_count=1,
            posted_sale_amount=Decimal("250.00"),
            payment_amount=Decimal("250.00"),
            debt_amount=Decimal("0.00"),
            payment_confirmed=True,
            evidence="onec_no_debt",
        ),
    )

    assert completed_unpaid.action == "manual_review"
    assert completed_unpaid.recommended_stage is None
    assert completed_unpaid.review_reason == "pickup_waiting_completed_needs_handoff_confirmation"
    assert completed_paid.action == "manual_review"
    assert completed_paid.recommended_stage is None
    assert completed_paid.review_reason == "pickup_waiting_completed_needs_handoff_confirmation"
    assert completed_onec_paid.action == "manual_review"
    assert completed_onec_paid.recommended_stage is None
    assert completed_onec_paid.review_reason == (
        "pickup_waiting_completed_needs_handoff_confirmation"
    )


def test_onec_payment_confirmation_requires_closed_debt_or_full_payment() -> None:
    confirmed_by_no_debt = sync._onec_payment_confirmation(  # noqa: SLF001
        posted_sale_count=1,
        posted_sale_amount=Decimal("250.00"),
        payment_amount=None,
        debt_amount=Decimal("0.00"),
    )
    confirmed_by_full_payment = sync._onec_payment_confirmation(  # noqa: SLF001
        posted_sale_count=1,
        posted_sale_amount=Decimal("250.00"),
        payment_amount=Decimal("250.00"),
        debt_amount=None,
    )
    not_confirmed = sync._onec_payment_confirmation(  # noqa: SLF001
        posted_sale_count=1,
        posted_sale_amount=Decimal("250.00"),
        payment_amount=Decimal("100.00"),
        debt_amount=Decimal("150.00"),
    )

    assert confirmed_by_no_debt == (True, "onec_no_debt")
    assert confirmed_by_full_payment == (True, "onec_full_payment")
    assert not_confirmed == (False, "onec_payment_not_confirmed")


def test_decide_new_deal_stage_moves_paid_prepayment_to_assembly() -> None:
    decision = sync.decide_new_deal_stage(
        _deal(stage_id="PREPAYMENT_INVOICE", delivery="СДЭК", payment_status="0"),
        order_status=sync.SaleOrderStatus(
            order_number="218001",
            canceled=False,
            status_id="N",
            payed=True,
            created_at=datetime.now() - timedelta(days=sync.PREPAYMENT_WAITING_MAX_AGE_DAYS + 10),
        ),
    )

    assert decision.action == "update_stage"
    assert decision.recommended_stage == "EXECUTING"
    assert decision.review_reason == "prepayment_paid_to_assembly"


def test_decide_new_deal_stage_does_not_reassemble_completed_order_with_posted_sale() -> None:
    decision = sync.decide_new_deal_stage(
        _deal(stage_id="PREPAYMENT_INVOICE", delivery="СДЭК", payment_status="0"),
        order_status=sync.SaleOrderStatus(
            order_number="218001",
            canceled=False,
            status_id="F",
            payed=True,
            created_at=datetime.now() - timedelta(days=10),
        ),
        onec_settlement=sync.OneCOrderSettlement(
            order_number="218001",
            posted_sale_count=1,
            posted_sale_amount=Decimal("250.00"),
            payment_amount=Decimal("250.00"),
            debt_amount=Decimal("0.00"),
            payment_confirmed=True,
            evidence="onec_no_debt",
        ),
    )

    assert decision.action == "manual_review"
    assert decision.recommended_stage is None
    assert decision.review_reason == "historical_completed_with_rtu_needs_delivery_check"


def test_fetch_new_deals_includes_prepayment_stage_per_stage_limit() -> None:
    client = FakeDealListClient()

    deals = sync.fetch_new_deals(client, limit=1)  # type: ignore[arg-type]

    assert [deal.stage_id for deal in deals] == list(sync.QUICK_STAGE_IDS)
    assert [call["payload"]["filter"]["STAGE_ID"] for call in client.calls] == list(
        sync.QUICK_STAGE_IDS
    )


def test_new_deal_outbox_excludes_manual_review_and_won_targets() -> None:
    decisions = [
        sync.decide_new_deal_stage(_deal(deal_id=1, delivery="Самовывоз")),
        sync.decide_new_deal_stage(_deal(deal_id=2, delivery="СДЭК", payment_status="0")),
    ]

    rows = sync.build_new_deal_outbox_rows(
        decisions,
        available_stage_ids={"NEW", "PREPARATION", "PREPAYMENT_INVOICE", "EXECUTING"},
    )

    assert len(rows) == 2
    assert rows[0].bitrix_deal_id == 1
    assert rows[0].target_stage == "EXECUTING"
    assert rows[1].bitrix_deal_id == 2
    assert rows[1].target_stage == "PREPAYMENT_INVOICE"


def test_new_deal_outbox_blocks_won_without_handoff_confirmation() -> None:
    decision = sync.decide_new_deal_stage(
        _deal(deal_id=1, stage_id="DELIVERY_REVIEW", delivery="Самовывоз"),
        order_status=sync.SaleOrderStatus(
            order_number="218001",
            canceled=False,
            status_id="F",
            payed=False,
            created_at=datetime.now(),
        ),
    )

    rows = sync.build_new_deal_outbox_rows(
        [decision],
        available_stage_ids={"WON"},
    )

    assert rows == []


def test_apply_outbox_by_target_handles_mixed_targets() -> None:
    rows = [
        service.OrderFulfillmentStageOutboxRow(
            idempotency_key="key-1",
            site_order_number="218001",
            bitrix_deal_id=1,
            current_stage="NEW",
            target_stage="PREPARATION",
            operation="update_stage",
            state="ready",
            chat_event="new_deal",
            event_confidence="medium",
            evidence_redacted=None,
            payload_json="{}",
            block_reason=None,
        ),
        service.OrderFulfillmentStageOutboxRow(
            idempotency_key="key-2",
            site_order_number="218002",
            bitrix_deal_id=2,
            current_stage="NEW",
            target_stage="EXECUTING",
            operation="update_stage",
            state="ready",
            chat_event="new_deal",
            event_confidence="medium",
            evidence_redacted=None,
            payload_json="{}",
            block_reason=None,
        ),
    ]
    client = FakeBitrixClient(
        {
            1: _deal(deal_id=1, order_number="218001"),
            2: _deal(deal_id=2, order_number="218002"),
        }
    )

    results = sync.apply_outbox_by_target(rows, client=client, apply=True)

    assert [result.result for result in results] == ["applied", "applied"]
    assert sorted(client.updates) == [(1, "PREPARATION"), (2, "EXECUTING")]


def test_apply_outbox_by_target_applies_only_allowed_targets() -> None:
    rows = [
        service.OrderFulfillmentStageOutboxRow(
            idempotency_key="key-pickup",
            site_order_number="218001",
            bitrix_deal_id=1,
            current_stage="EXECUTING",
            target_stage="PICKUP_WAITING",
            operation="update_stage",
            state="ready",
            chat_event=service.EVENT_PICKUP_UNCLAIMED,
            event_confidence="medium",
            evidence_redacted="не забрали",
            payload_json="{}",
            block_reason=None,
        ),
        service.OrderFulfillmentStageOutboxRow(
            idempotency_key="key-won",
            site_order_number="218002",
            bitrix_deal_id=2,
            current_stage="PICKUP_WAITING",
            target_stage="WON",
            operation="update_stage",
            state="ready",
            chat_event=service.EVENT_PICKUP_RECEIVED,
            event_confidence="strong",
            evidence_redacted="выдали",
            payload_json="{}",
            block_reason=None,
        ),
    ]
    client = FakeBitrixClient(
        {
            1: _deal(
                deal_id=1,
                stage_id="EXECUTING",
                order_number="218001",
                delivery="Самовывоз",
            ),
            2: _deal(
                deal_id=2,
                stage_id="PICKUP_WAITING",
                order_number="218002",
                delivery="Самовывоз",
            ),
        }
    )

    results = sync.apply_outbox_by_target(
        rows,
        client=client,
        apply=True,
        allowed_target_stages={"PICKUP_WAITING"},
    )

    assert [result.result for result in results] == ["applied", "dry_run_ready"]
    assert client.updates == [(1, "PICKUP_WAITING")]


def test_apply_outbox_by_target_dry_runs_target_outside_chat_allowlist() -> None:
    row = service.OrderFulfillmentStageOutboxRow(
        idempotency_key="key-chat-won",
        site_order_number="218003",
        bitrix_deal_id=3,
        current_stage="PICKUP_WAITING",
        target_stage="WON",
        operation="update_stage",
        state="ready",
        chat_event=service.EVENT_PICKUP_RECEIVED,
        event_confidence="strong",
        evidence_redacted=None,
        payload_json="{}",
        block_reason=None,
    )
    client = FakeBitrixClient(
        {3: _deal(deal_id=3, order_number="218003", stage_id="PICKUP_WAITING")}
    )

    results = sync.apply_outbox_by_target(
        [row],
        client=client,
        apply=True,
        allowed_target_stages={service.CRM_STAGE_PICKUP_WAITING},
    )

    assert results[0].result == "dry_run_ready"
    assert client.updates == []


def test_bot_cutover_blocks_only_legacy_pickup_auto_apply() -> None:
    rows = [
        service.OrderFulfillmentStageOutboxRow(
            idempotency_key="pickup-won",
            site_order_number="218003",
            bitrix_deal_id=3,
            current_stage="PICKUP_WAITING",
            target_stage="WON",
            operation="update_stage",
            state="ready",
            chat_event=service.EVENT_PICKUP_RECEIVED,
            event_confidence="strong",
            evidence_redacted=None,
            payload_json="{}",
            block_reason=None,
        ),
        service.OrderFulfillmentStageOutboxRow(
            idempotency_key="courier-won",
            site_order_number="218004",
            bitrix_deal_id=4,
            current_stage="IN_DELIVERY",
            target_stage="WON",
            operation="update_stage",
            state="ready",
            chat_event=service.EVENT_COURIER_DELIVERED_PAID,
            event_confidence="strong",
            evidence_redacted=None,
            payload_json="{}",
            block_reason=None,
        ),
    ]
    client = FakeBitrixClient(
        {
            3: _deal(
                deal_id=3,
                order_number="218003",
                stage_id="PICKUP_WAITING",
                delivery="Самовывоз",
            ),
            4: _deal(
                deal_id=4,
                order_number="218004",
                stage_id="IN_DELIVERY",
                delivery="Доставка курьером",
            ),
        }
    )

    results = sync.apply_outbox_by_target(
        rows,
        client=client,
        apply=True,
        allowed_target_stages={"WON"},
        blocked_event_prefixes=("pickup_",),
    )

    assert [result.result for result in results] == ["dry_run_ready", "applied"]
    assert client.updates == [(4, "WON")]


def test_build_operational_monitoring_rows_flags_stage_errors_and_rtu_gap() -> None:
    decisions = [
        sync.decide_new_deal_stage(
            _deal(stage_id="PREPAYMENT_INVOICE", delivery="СДЭК", payment_status="0"),
            order_status=sync.SaleOrderStatus(
                order_number="218001",
                canceled=False,
                status_id="N",
                payed=False,
                created_at=datetime.now()
                - timedelta(days=sync.PREPAYMENT_WAITING_MAX_AGE_DAYS, minutes=1),
            ),
            onec_settlement=sync.OneCOrderSettlement(
                order_number="218001",
                posted_sale_count=1,
                posted_sale_amount=Decimal("250.00"),
                payment_amount=None,
                debt_amount=Decimal("250.00"),
                payment_confirmed=False,
                evidence="onec_payment_not_confirmed",
            ),
        ),
        sync.decide_new_deal_stage(
            _deal(
                deal_id=3,
                stage_id="PICKUP_WAITING",
                order_number="218003",
                delivery="Самовывоз",
                payment_status="0",
            ),
            order_status=sync.SaleOrderStatus(
                order_number="218003",
                canceled=False,
                status_id="F",
                payed=False,
                created_at=datetime.now() - timedelta(days=1),
            ),
        ),
    ]
    apply_results = [
        service.OrderFulfillmentStageApplyResult(
            idempotency_key="key-1",
            site_order_number="218001",
            bitrix_deal_id=1,
            current_stage="PREPAYMENT_INVOICE",
            live_stage="PREPAYMENT_INVOICE",
            live_order_number="218001",
            target_stage="LOSE",
            operation="update_stage",
            input_state="ready",
            result="update_error",
            applied=False,
            dry_run=False,
            reason="transient_bitrix_error:HTTPError: http_500",
        )
    ]
    rows = sync.build_operational_monitoring_rows(
        decisions=decisions,
        apply_results=apply_results,
        stage_summary=[
            {"stage_id": "NEW", "internet_order_count": 0},
            {"stage_id": "PREPARATION", "internet_order_count": 2},
            {"stage_id": "DELIVERY_REVIEW", "internet_order_count": 1},
        ],
        rtu_signal_rows=[
            {
                "site_order_number": "218002",
                "bitrix_deal_id": 2,
                "crm_stage": "EXECUTING",
                "rtu_count": 1,
                "latest_rtu_number": "РБГУ000001",
                "assembled_rtu_count": 0,
            }
        ],
        artifacts={"review": "review.csv", "apply_result": "apply.csv"},
    )

    by_type = {row["alert_type"]: row for row in rows}
    assert by_type["stage_count"]["stage_id"] == "DELIVERY_REVIEW"
    assert by_type["overdue_prepayment"]["site_order_number"] == "218001"
    assert by_type["pickup_waiting_close_candidate"]["site_order_number"] == "218003"
    assert by_type["outbox_error"]["reason"].startswith("transient_bitrix_error")
    assert by_type["rtu_without_assembled"]["site_order_number"] == "218002"


def test_build_rtu_without_assembled_rows_keeps_only_missing_signal() -> None:
    deals = [
        _deal(deal_id=1, stage_id="EXECUTING", order_number="218001"),
        _deal(deal_id=2, stage_id="EXECUTING", order_number="218002"),
    ]

    rows = sync.build_rtu_without_assembled_rows(
        deals,
        {
            "218001": {
                "rtu_count": 1,
                "assembled_rtu_count": 0,
                "latest_rtu_number": "РБГУ000001",
                "latest_rtu_date": datetime(2026, 5, 26, 10, 0),
            },
            "218002": {
                "rtu_count": 1,
                "assembled_rtu_count": 1,
                "latest_rtu_number": "РБГУ000002",
                "latest_rtu_date": datetime(2026, 5, 26, 11, 0),
            },
        },
    )

    assert len(rows) == 1
    assert rows[0]["site_order_number"] == "218001"
    assert rows[0]["latest_rtu_date"] == "2026-05-26T10:00:00"


def test_rtu_signal_requires_print_and_scan_on_same_rtu_and_reads_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResult:
        def fetchall(self):
            return [
                {
                    "site_order_number": "218001",
                    "rtu_count": 2,
                    "assembled_rtu_count": 1,
                    "printed_rtu_count": 1,
                    "scanned_rtu_count": 1,
                    "issued_rtu_count": 0,
                    "returned_rtu_count": 1,
                    "latest_rtu_number": "РБГУ000002",
                    "latest_rtu_date": datetime(2026, 8, 23, 12, 0),
                }
            ]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement, params):
            captured["sql"] = str(statement)
            captured["params"] = params
            return FakeResult()

    class FakeEngine:
        disposed = False

        def connect(self):
            return FakeConnection()

        def dispose(self):
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(
        sync,
        "get_settings",
        lambda: Settings(_env_file=None, onec_database_url="mssql://placeholder"),
    )
    monkeypatch.setattr(sync, "build_engine", lambda *args, **kwargs: engine)

    result = sync.query_rtu_signal_by_orders(["218001"])

    sql = str(captured["sql"])
    assert result["218001"]["issued_rtu_count"] == 0
    assert result["218001"]["returned_rtu_count"] == 1
    assert "CASE WHEN has_print = 1 AND has_scan = 1" in sql
    assert "assembled_event._Fld9449_TYPE = 0x08" in sql
    assert "assembled_event._Fld9449_RTRef = 0x000000CB" in sql
    assert "print_event._Fld9449_TYPE = 0x08" in sql
    assert "print_event._Fld9449_RTRef = 0x000000CB" in sql
    assert "scan_event._Fld9449_TYPE = 0x08" in sql
    assert "scan_event._Fld9449_RTRef = 0x000000CB" in sql
    assert "return_doc._Fld1684_TYPE = 0x08" in sql
    assert "return_doc._Fld1684_RRRef = rtu._IDRRef" in sql
    assert "return_line._Fld1712_TYPE = 0x08" in sql
    assert "return_line._Fld1712_RRRef = rtu._IDRRef" in sql
    assert engine.disposed is True


def test_onec_order_states_require_single_unposted_marked_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResult:
        def fetchall(self):
            return [
                {
                    "site_order_number": "226030",
                    "onec_order_count": 1,
                    "onec_inactive_marked_order_count": 1,
                },
                {
                    "site_order_number": "242704",
                    "onec_order_count": 1,
                    "onec_inactive_marked_order_count": 0,
                },
            ]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement, params):
            captured["sql"] = str(statement)
            captured["params"] = params
            return FakeResult()

    class FakeEngine:
        disposed = False

        def connect(self):
            return FakeConnection()

        def dispose(self):
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(
        sync,
        "get_settings",
        lambda: Settings(_env_file=None, onec_database_url="mssql://placeholder"),
    )
    monkeypatch.setattr(sync, "build_engine", lambda *args, **kwargs: engine)

    result = sync.query_onec_order_states_by_orders(["226030", "242704"])

    assert result["226030"] == {
        "onec_order_count": 1,
        "onec_inactive_marked_order_count": 1,
    }
    assert result["242704"] == {
        "onec_order_count": 1,
        "onec_inactive_marked_order_count": 0,
    }
    assert "ord._Posted = 0x00 AND ord._Marked = 0x01" in str(captured["sql"])
    assert engine.disposed is True


def test_stage_summary_uses_fast_totals_without_pagination() -> None:
    client = FakeListClient()

    rows = sync.fetch_stage_summary(client)  # type: ignore[arg-type]

    assert rows[0]["deal_count"] == 42
    assert rows[0]["internet_order_count"] == 42
    assert len(client.calls) == len(sync.PROCESS_STAGES) * 2
    assert all(call["payload"]["start"] == 0 for call in client.calls)


def test_stage_summary_retries_transient_bitrix_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync, "BITRIX_READ_RETRY_DELAYS", (0, 0))
    client = FlakyListClient(failures=1)

    rows = sync.fetch_stage_summary(client)  # type: ignore[arg-type]

    assert client.calls == len(sync.PROCESS_STAGES) * 2 + 1
    assert all(not row["error"] for row in rows)
    assert rows[0]["internet_order_count"] == 42


def test_stage_summary_failure_does_not_abort_daily_contour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync, "BITRIX_READ_RETRY_DELAYS", (0, 0))
    client = FlakyListClient(failures=100)

    rows = sync.fetch_stage_summary(client)  # type: ignore[arg-type]

    assert len(rows) == len(sync.PROCESS_STAGES)
    assert all(row["deal_count"] is None for row in rows)
    assert all("http_500" in row["error"] for row in rows)
    assert sync.stage_summary_counts(rows) == {}


def test_order_fulfillment_cron_avoids_daily_collision() -> None:
    cron_source = (sync.REPO_ROOT / "infra/cron/order_fulfillment_sync.cron").read_text(
        encoding="utf-8"
    )
    wrapper_source = (sync.REPO_ROOT / "infra/cron/order_fulfillment_sync.sh").read_text(
        encoding="utf-8"
    )
    bot_wrapper_source = (sync.REPO_ROOT / "infra/cron/order_fulfillment_bot_outbox.sh").read_text(
        encoding="utf-8"
    )
    env_example = (sync.REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "25,55 0,2-23 * * *" in cron_source
    assert "25 1 * * *" in cron_source
    assert "ORDER_FULFILLMENT_SYNC_MODE=nightly" in cron_source
    assert "0 11 * * *" in cron_source
    assert "ORDER_FULFILLMENT_SYNC_MODE=daily" in cron_source
    assert "ORDER_FULFILLMENT_SYNC_MODE=shipments" in cron_source
    shipment_line = next(line for line in cron_source.splitlines() if "SYNC_MODE=shipments" in line)
    assert "ORDER_FULFILLMENT_NOTIFY_ENABLED=false" in shipment_line
    assert "ORDER_FULFILLMENT_SYNC_APPLY=true" in shipment_line
    daily_line = next(line for line in cron_source.splitlines() if "SYNC_MODE=daily" in line)
    assert "ORDER_FULFILLMENT_NOTIFY_ENABLED=false" in daily_line
    assert "ORDER_FULFILLMENT_SYNC_MODE=all" not in cron_source
    assert (
        "/opt/MM/pricing-service-task43-current/infra/cron/order_fulfillment_sync.sh" in cron_source
    )
    assert "REPO_DIR=/opt/MM/pricing-service-task43-current" in cron_source
    assert "flock -w 600" in wrapper_source
    assert "flock -n" in wrapper_source
    assert '"${PYTHON_BIN}" -m tasks.order_fulfillment_sync' in wrapper_source
    assert 'source "${REPO_DIR}/.env"' not in bot_wrapper_source
    assert "ORDER_FULFILLMENT_BOT_WORKER_TIMEOUT_SECONDS=//p" in bot_wrapper_source
    assert 'WORKER_TIMEOUT_SECONDS="${WORKER_TIMEOUT_SECONDS:-600}"' in bot_wrapper_source
    assert "--signal=TERM" in bot_wrapper_source
    assert "--kill-after=30s" in bot_wrapper_source
    assert '"${WORKER_TIMEOUT_SECONDS}s"' in bot_wrapper_source
    assert "ORDER_FULFILLMENT_BOT_WORKER_TIMEOUT_SECONDS=600" in env_example


def test_nightly_heavy_window_is_limited_to_moscow_midnight_through_six() -> None:
    moscow = sync.ZoneInfo("Europe/Moscow")

    assert sync.nightly_heavy_window_is_open(datetime(2026, 8, 31, 0, 0, tzinfo=moscow))
    assert sync.nightly_heavy_window_is_open(datetime(2026, 8, 31, 5, 59, tzinfo=moscow))
    assert not sync.nightly_heavy_window_is_open(datetime(2026, 8, 31, 6, 0, tzinfo=moscow))
    assert not sync.nightly_heavy_window_is_open(datetime(2026, 8, 31, 23, 59, tzinfo=moscow))


def test_nightly_heavy_window_guard_fails_closed_during_day() -> None:
    with pytest.raises(SystemExit, match="allowed only from 00:00 to 06:00"):
        sync.require_nightly_heavy_window(datetime(2026, 8, 31, 12, 0))


def test_daytime_quick_sync_skips_heavy_onec_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Client:
        @staticmethod
        def list_deal_stage_ids() -> set[str]:
            return set(sync.PROCESS_STAGES)

    settings = Settings(
        _env_file=None,
        order_fulfillment_execution_master_enabled=True,
        order_fulfillment_execution_reconciliation_enabled=True,
    )
    monkeypatch.setattr(sync, "get_settings", lambda: settings)
    monkeypatch.setattr(sync, "fetch_new_deals", lambda *args, **kwargs: [])
    monkeypatch.setattr(sync, "fetch_sale_order_statuses", lambda *args, **kwargs: {})
    monkeypatch.setattr(sync, "fetch_onec_order_settlements", lambda *args, **kwargs: {})
    monkeypatch.setattr(sync, "fetch_stage_summary", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        sync,
        "fetch_executing_deal_scan",
        lambda *args, **kwargs: pytest.fail("daytime quick started the executing-deal scan"),
    )
    monkeypatch.setattr(
        sync,
        "query_rtu_without_assembled_for_deals",
        lambda *args, **kwargs: pytest.fail("daytime quick started the wide RTU query"),
    )

    summary = sync.run_quick_sync(
        client=Client(),  # type: ignore[arg-type]
        output_dir=tmp_path,
        stamp="20260831-120000",
        apply=False,
        limit=200,
        include_heavy_onec=False,
    )

    assert summary["mode"] == "quick"
    assert summary["executing_reconciliation"]["enabled"] is False
    assert summary["executing_reconciliation"]["configured"] is True
    assert summary["executing_reconciliation"]["deferred_to_nightly"] is True


def test_nightly_sync_runs_heavy_onec_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Client:
        @staticmethod
        def list_deal_stage_ids() -> set[str]:
            return set(sync.PROCESS_STAGES)

    calls = {"scan": 0, "rtu": 0, "states": 0, "monitoring_rtu": 0}
    settings = Settings(
        _env_file=None,
        onec_database_url="mssql://placeholder",
        order_fulfillment_execution_master_enabled=True,
        order_fulfillment_execution_reconciliation_enabled=True,
        order_fulfillment_execution_ingest_enabled=False,
    )
    scan = sync.ExecutingDealScan([], set(), set(), 0, 0, True)

    def fetch_scan(*args, **kwargs):
        calls["scan"] += 1
        return scan

    def query_rtu(*args, **kwargs):
        calls["rtu"] += 1
        return {}

    def query_states(*args, **kwargs):
        calls["states"] += 1
        return {}

    def query_monitoring_rtu(*args, **kwargs):
        calls["monitoring_rtu"] += 1
        return []

    monkeypatch.setattr(sync, "get_settings", lambda: settings)
    monkeypatch.setattr(sync, "fetch_new_deals", lambda *args, **kwargs: [])
    monkeypatch.setattr(sync, "fetch_executing_deal_scan", fetch_scan)
    monkeypatch.setattr(sync, "fetch_sale_order_statuses", lambda *args, **kwargs: {})
    monkeypatch.setattr(sync, "fetch_onec_order_settlements", lambda *args, **kwargs: {})
    monkeypatch.setattr(sync, "query_rtu_signal_by_orders", query_rtu)
    monkeypatch.setattr(sync, "query_onec_order_states_by_orders", query_states)
    monkeypatch.setattr(sync, "query_rtu_without_assembled_for_deals", query_monitoring_rtu)
    monkeypatch.setattr(sync, "fetch_stage_summary", lambda *args, **kwargs: [])

    summary = sync.run_quick_sync(
        client=Client(),  # type: ignore[arg-type]
        output_dir=tmp_path,
        stamp="20260831-012500",
        apply=False,
        limit=200,
        include_heavy_onec=True,
    )

    assert summary["mode"] == "nightly"
    assert summary["executing_reconciliation"]["enabled"] is True
    assert calls == {"scan": 1, "rtu": 1, "states": 1, "monitoring_rtu": 1}


def test_shipment_poller_uses_cursor_and_overlapping_recent_window(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cursor_path = tmp_path / "shipment-cursor.json"
    cursor_path.write_text(json.dumps({"last_deal_id": 2}), encoding="utf-8")
    settings = Settings(
        _env_file=None,
        order_fulfillment_shipments_poller_enabled=True,
        order_fulfillment_shipments_poller_limit=1,
        order_fulfillment_shipments_poller_overlap=1,
        order_fulfillment_shipments_poller_cursor_path=str(cursor_path),
        order_fulfillment_shipments_master_enabled=False,
        order_fulfillment_shipments_ingest_enabled=False,
        order_fulfillment_shipments_gateway_url="https://example.invalid",
        order_fulfillment_shipments_gateway_token="secret",
    )
    monkeypatch.setattr(sync, "get_settings", lambda: settings)

    class Client:
        def list_deals_by_stages(self, stage_ids, *, limit):
            assert "PARTIALLY_SHIPPED" in stage_ids
            assert limit >= 2
            return [
                service.BitrixDealSnapshot(
                    deal_id=deal_id,
                    stage_id="FINAL_INVOICE",
                    delivery="СДЭК",
                    raw={service.CRM_ORDER_NUMBER_FIELD: str(242800 + deal_id)},
                )
                for deal_id in (1, 2, 3, 4)
            ]

    order_snapshots = {
        str(242800 + deal_id): {
            "source_revision": f"onec-{deal_id}",
            "expected_items": [{"product_ref": "product-a", "quantity": "1"}],
            "rtus": [
                {
                    "external_id": f"rtu-{deal_id}",
                    "posted": True,
                    "assembled_at": datetime(2026, 8, 29, 10, 0),
                    "items": [{"product_ref": "product-a", "quantity": "1"}],
                }
            ],
        }
        for deal_id in (3, 4)
    }
    monkeypatch.setattr(
        sync,
        "query_multi_shipment_onec_snapshots",
        lambda order_numbers: {
            order: order_snapshots[order] for order in order_numbers if order in order_snapshots
        },
    )

    class Gateway:
        def __init__(self, **kwargs):
            assert kwargs["token"] == "secret"

        def get_order_snapshot(self, *, site_order_number):
            return {
                "order_id": int(site_order_number),
                "revision": f"bitrix-{site_order_number}",
                "shipments": [],
            }

    monkeypatch.setattr(sync.shipment_service, "BitrixSaleShipmentGatewayClient", Gateway)
    captured: list[dict] = []

    def fake_sync_order_shipments(session, **kwargs):
        del session
        captured.append(kwargs)
        return sync.shipment_service.ShipmentSyncResult(
            snapshot_id=kwargs["snapshot_id"],
            site_order_number=kwargs["site_order_number"],
            coverage_status="complete",
            full_assembly=True,
            shipment_count=0,
            target_stage="FINAL_INVOICE",
            action="noop",
            reason="already_final_invoice",
        )

    monkeypatch.setattr(
        sync.shipment_service,
        "sync_order_shipments",
        fake_sync_order_shipments,
    )

    class Session:
        def rollback(self):
            raise AssertionError("poller must not roll back a successful shadow cycle")

    class SessionScope:
        def __enter__(self):
            return Session()

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(sync, "session_scope", SessionScope)

    summary = sync.run_shipment_poller_sync(client=Client(), apply=True)

    assert summary["persist"] is False
    assert summary["processed"] == 2
    assert [item["site_order_number"] for item in captured] == ["242803", "242804"]
    assert all(item["enqueue_gateway"] is False for item in captured)
    assert json.loads(cursor_path.read_text(encoding="utf-8"))["last_deal_id"] == 3


def test_shipment_poller_advances_cursor_past_failed_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cursor_path = tmp_path / "shipment-cursor.json"
    settings = Settings(
        _env_file=None,
        order_fulfillment_shipments_poller_enabled=True,
        order_fulfillment_shipments_poller_limit=1,
        order_fulfillment_shipments_poller_overlap=1,
        order_fulfillment_shipments_poller_cursor_path=str(cursor_path),
        order_fulfillment_shipments_master_enabled=False,
        order_fulfillment_shipments_ingest_enabled=False,
        order_fulfillment_shipments_gateway_url="https://example.invalid",
        order_fulfillment_shipments_gateway_token="secret",
    )
    monkeypatch.setattr(sync, "get_settings", lambda: settings)

    class Client:
        def list_deals_by_stages(self, stage_ids, *, limit):
            del stage_ids, limit
            return [
                service.BitrixDealSnapshot(
                    deal_id=7,
                    stage_id="FINAL_INVOICE",
                    delivery="СДЭК",
                    raw={service.CRM_ORDER_NUMBER_FIELD: "242807"},
                )
            ]

    monkeypatch.setattr(sync, "query_multi_shipment_onec_snapshots", lambda order_numbers: {})

    class Gateway:
        def __init__(self, **kwargs):
            assert kwargs["token"] == "secret"

    monkeypatch.setattr(sync.shipment_service, "BitrixSaleShipmentGatewayClient", Gateway)

    class Session:
        def rollback(self):
            return None

    class SessionScope:
        def __enter__(self):
            return Session()

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(sync, "session_scope", SessionScope)

    summary = sync.run_shipment_poller_sync(client=Client(), apply=False)

    assert summary["processed"] == 0
    assert summary["errors"][0]["site_order_number"] == "242807"
    assert json.loads(cursor_path.read_text(encoding="utf-8"))["last_deal_id"] == 7


def _notify_settings(
    tmp_path: Path,
    *,
    enabled: bool = True,
    business_user_ids: str = "10",
    tech_user_ids: str = "20",
    site_dialog_id: str = "-",
) -> Settings:
    return Settings(
        _env_file=None,
        order_fulfillment_notify_enabled=enabled,
        order_fulfillment_notify_business_user_ids=business_user_ids,
        order_fulfillment_notify_tech_user_ids=tech_user_ids,
        order_fulfillment_notify_site_dialog_id=site_dialog_id,
        order_fulfillment_notify_state_path=str(tmp_path / "notify-state.json"),
    )


def _summary(
    *,
    mode: str,
    item: dict,
    stamp: str = "20260522-090000",
    dry_run: bool = True,
) -> dict:
    return {"stamp": stamp, "mode": mode, "dry_run": dry_run, "items": [item]}


def test_order_fulfillment_notify_user_ids_parse_csv_and_json(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        order_fulfillment_notify_business_user_ids="10, 20",
        order_fulfillment_notify_tech_user_ids="[30, 40]",
        order_fulfillment_site_chat_apply_author_ids="50, 60",
        order_fulfillment_courier_chat_apply_author_ids="[70, 80]",
        order_fulfillment_notify_state_path=str(tmp_path / "state.json"),
    )

    assert settings.order_fulfillment_notify_business_user_ids == [10, 20]
    assert settings.order_fulfillment_notify_tech_user_ids == [30, 40]
    assert settings.order_fulfillment_site_chat_apply_author_ids == [50, 60]
    assert settings.order_fulfillment_courier_chat_apply_author_ids == [70, 80]
    assert settings.order_fulfillment_chat_auto_apply_enabled is False


def test_order_fulfillment_notifications_disabled_do_not_send(tmp_path: Path) -> None:
    client = FakeNotifyClient()
    summary = _summary(
        mode="chat",
        item={
            "mode": "chat",
            "manual_review_keys": ["manual-1"],
            "manual_review_examples": [{"key": "manual-1", "reason": "bitrix_deal_not_found"}],
        },
    )

    result = sync.deliver_order_fulfillment_notifications(
        client=client,
        summary=summary,
        summary_path=tmp_path / "summary.json",
        output_dir=tmp_path,
        settings=_notify_settings(tmp_path, enabled=False),
    )

    assert result["enabled"] is False
    assert client.calls == []


def test_quick_sync_does_not_send_business_manual_review(tmp_path: Path) -> None:
    client = FakeNotifyClient()
    summary = _summary(
        mode="quick",
        item={
            "mode": "quick",
            "manual_review_keys": ["manual-quick"],
            "manual_review_examples": [{"key": "manual-quick", "reason": "carrier"}],
        },
    )

    sync.deliver_order_fulfillment_notifications(
        client=client,
        summary=summary,
        summary_path=tmp_path / "summary.json",
        output_dir=tmp_path,
        settings=_notify_settings(tmp_path),
    )

    assert client.calls == []


def test_chat_manual_review_sends_once_to_business_users(tmp_path: Path) -> None:
    client = FakeNotifyClient()
    summary = _summary(
        mode="chat",
        item={
            "mode": "chat",
            "review": str(tmp_path / "chat-review.csv"),
            "manual_review_keys": ["manual-1"],
            "manual_review_examples": [
                {
                    "key": "manual-1",
                    "site_order_number": "224236",
                    "bitrix_deal_id": "-",
                    "reason": "bitrix_deal_not_found",
                }
            ],
        },
    )
    settings = _notify_settings(
        tmp_path,
        business_user_ids="10,11",
        tech_user_ids="20",
        site_dialog_id="chat733",
    )

    first = sync.deliver_order_fulfillment_notifications(
        client=client,
        summary=summary,
        summary_path=tmp_path / "summary.json",
        output_dir=tmp_path,
        settings=settings,
    )
    second = sync.deliver_order_fulfillment_notifications(
        client=client,
        summary=summary,
        summary_path=tmp_path / "summary.json",
        output_dir=tmp_path,
        settings=settings,
    )

    assert first["sent"] == [{"kind": "manual_review", "count": 1}]
    assert second["sent"] == []
    assert [call["payload"]["USER_ID"] for call in client.calls] == ["10", "11"]
    message = client.calls[0]["payload"]["MESSAGE"]
    assert "требуется ручная проверка" in message
    assert "для заказа не найдена сделка" in message
    assert "manual_review" not in message
    assert "/opt/MM" not in message
    assert all(call["method"] != "im.message.add" for call in client.calls)


def test_technical_review_sends_only_to_tech_users(tmp_path: Path) -> None:
    client = FakeNotifyClient()
    summary = _summary(
        mode="quick",
        item={
            "mode": "quick",
            "apply_result": str(tmp_path / "apply.csv"),
            "technical_review_keys": ["tech-1"],
            "technical_review_examples": [
                {
                    "key": "tech-1",
                    "site_order_number": "213486",
                    "bitrix_deal_id": "6563",
                    "result": "technical_review",
                    "reason": "shipment cleanup required",
                }
            ],
        },
    )

    sync.deliver_order_fulfillment_notifications(
        client=client,
        summary=summary,
        summary_path=tmp_path / "summary.json",
        output_dir=tmp_path,
        settings=_notify_settings(
            tmp_path,
            business_user_ids="10",
            tech_user_ids="20,21",
            site_dialog_id="chat733",
        ),
    )

    assert [call["payload"]["USER_ID"] for call in client.calls] == ["20", "21"]
    assert "technical_review" in client.calls[0]["payload"]["MESSAGE"]
    assert "213486" in client.calls[0]["payload"]["MESSAGE"]
    assert all(call["method"] != "im.message.add" for call in client.calls)


def test_manual_review_message_skips_empty_order_examples(tmp_path: Path) -> None:
    message = sync.build_manual_review_message(
        {"dry_run": True},
        tmp_path / "summary.json",
        [
            {
                "key": "empty",
                "example": {
                    "site_order_number": "-",
                    "bitrix_deal_id": "-",
                    "reason": "manual_review",
                },
            }
        ],
    )

    assert "недостаточно данных" in message
    assert "заказ - / сделка -" not in message
    assert "Заказы:" not in message
    assert "/opt/MM" not in message


def test_operational_alert_sends_to_site_group_dialog(tmp_path: Path) -> None:
    client = FakeNotifyClient()
    summary = _summary(
        mode="quick",
        item={
            "mode": "quick",
            "monitoring": str(tmp_path / "monitoring.csv"),
            "operational_alert_keys": ["stage_count|NEW|1"],
            "operational_alert_examples": [
                {
                    "key": "stage_count|NEW|1",
                    "alert_type": "stage_count",
                    "site_order_number": "-",
                    "bitrix_deal_id": "-",
                    "stage_id": "NEW",
                    "count": "1",
                    "reason": "NEW has 1 internet orders",
                }
            ],
        },
    )

    first = sync.deliver_order_fulfillment_notifications(
        client=client,
        summary=summary,
        summary_path=tmp_path / "summary.json",
        output_dir=tmp_path,
        settings=_notify_settings(
            tmp_path,
            business_user_ids="",
            tech_user_ids="",
            site_dialog_id="chat733",
        ),
    )
    second = sync.deliver_order_fulfillment_notifications(
        client=client,
        summary=summary,
        summary_path=tmp_path / "summary.json",
        output_dir=tmp_path,
        settings=_notify_settings(
            tmp_path,
            business_user_ids="",
            tech_user_ids="",
            site_dialog_id="chat733",
        ),
    )

    assert first["sent"] == [{"kind": "operational_alert", "count": 1}]
    assert second["sent"] == []
    assert len(client.calls) == 1
    assert client.calls[0]["method"] == "im.message.add"
    assert client.calls[0]["payload"]["DIALOG_ID"] == "chat733"
    message = client.calls[0]["payload"]["MESSAGE"]
    assert "Интернет-заказы: требуется действие" in message
    assert "зависшие заказы в очередях" in message
    assert "Новых сигналов: 1 сигнал" in message
    assert "стадия «Новые»: 1 заказ требует проверки" in message
    assert "Ответственный: менеджер сделки" in message
    assert "Автоизменение CRM" not in message
    assert "Подробный отчет" not in message
    assert "/opt/MM" not in message
    assert "stage_count" not in message
    assert "Auto-apply" not in message


def test_daily_digest_deduplicates_repeated_cron_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeNotifyClient()
    apply_path = tmp_path / "apply.csv"
    apply_path.write_text(
        "site_order_number,bitrix_deal_id,target_stage,result,applied,reason\n"
        "235002,28002,EXECUTING,applied,1,\n",
        encoding="utf-8",
    )
    quick_item = {
        "mode": "quick",
        "dry_run": False,
        "apply_result": str(apply_path),
        "deal_keys": ["deal:1"],
        "ready_keys": ["ready-1"],
        "manual_review_keys": ["manual-1"],
        "manual_review_reason_counts": {"carrier": 1},
        "technical_review_keys": [],
        "operational_alert_keys": ["overdue_prepayment|235001|28001"],
    }
    for stamp in ("20260522-080000", "20260522-081500"):
        path = tmp_path / f"order-fulfillment-sync-summary-{stamp}.json"
        payload = _summary(mode="quick", item=quick_item, stamp=stamp)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    daily_summary = _summary(
        mode="daily",
        stamp="20260522-083000",
        item={
            "mode": "daily",
            "unknown_delivery_rows": 2,
            "unknown_delivery": str(tmp_path / "unknown.csv"),
        },
    )
    daily_path = tmp_path / "order-fulfillment-sync-summary-20260522-083000.json"
    daily_path.write_text(json.dumps(daily_summary, ensure_ascii=False), encoding="utf-8")

    settings = _notify_settings(tmp_path, business_user_ids="10", tech_user_ids="20")
    monkeypatch.setenv("ONEC_ASSEMBLY_CRM_STATE_PATH", str(tmp_path / "missing.sqlite3"))
    sync.deliver_order_fulfillment_notifications(
        client=client,
        summary=daily_summary,
        summary_path=daily_path,
        output_dir=tmp_path,
        settings=settings,
    )
    sync.deliver_order_fulfillment_notifications(
        client=client,
        summary=daily_summary,
        summary_path=daily_path,
        output_dir=tmp_path,
        settings=settings,
    )

    assert len(client.calls) == 1
    message = client.calls[0]["payload"]["MESSAGE"]
    assert "MASTER-MOBILE.RU: контроль интернет-заказов" in message
    assert "подтверждена сборка: 0 заказов" in message
    assert "подтверждена выдача: 0 заказов" in message
    assert "выполнено переходов CRM: 1 переход" in message
    assert "Требуется вмешательство: 1 заказ." in message
    assert "заказ 235001 / сделка 28001: проверить просроченную оплату" in message
    assert "dry-run" not in message
    assert "Ручной разбор" not in message
    assert "Технические ошибки" not in message
    assert "Неизвестные доставки" not in message
    assert "/opt/MM" not in message


def test_daily_digest_sends_to_site_dialog_without_business_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeNotifyClient()
    daily_summary = _summary(
        mode="daily",
        stamp="20260810-110000",
        item={
            "mode": "daily",
            "unknown_delivery_rows": 0,
        },
    )
    daily_path = tmp_path / "order-fulfillment-sync-summary-20260810-110000.json"
    daily_path.write_text(json.dumps(daily_summary, ensure_ascii=False), encoding="utf-8")
    settings = _notify_settings(
        tmp_path,
        business_user_ids="",
        tech_user_ids="",
        site_dialog_id="chat733",
    )
    monkeypatch.setenv("ONEC_ASSEMBLY_CRM_STATE_PATH", str(tmp_path / "missing.sqlite3"))

    first = sync.deliver_order_fulfillment_notifications(
        client=client,
        summary=daily_summary,
        summary_path=daily_path,
        output_dir=tmp_path,
        settings=settings,
    )
    second = sync.deliver_order_fulfillment_notifications(
        client=client,
        summary=daily_summary,
        summary_path=daily_path,
        output_dir=tmp_path,
        settings=settings,
    )

    assert first["sent"] == [{"kind": "daily_digest", "count": 1}]
    assert second["sent"] == []
    assert second["skipped"] == ["daily_digest_already_sent"]
    assert len(client.calls) == 1
    assert client.calls[0]["method"] == "im.message.add"
    assert client.calls[0]["payload"]["DIALOG_ID"] == "chat733"
    message = client.calls[0]["payload"]["MESSAGE"]
    assert message == (
        "MASTER-MOBILE.RU: контроль интернет-заказов\n"
        "Автоматически обработано за период:\n"
        "- подтверждена сборка: 0 заказов;\n"
        "- подтверждена выдача: 0 заказов;\n"
        "- выполнено переходов CRM: 0 переходов.\n"
        "Ручных действий сегодня не требуется."
    )


def test_daily_digest_marks_partial_bitrix_stage_statistics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEC_ASSEMBLY_CRM_STATE_PATH", str(tmp_path / "missing.sqlite3"))
    daily_summary = _summary(
        mode="daily",
        stamp="20260820-110000",
        item={
            "mode": "daily",
            "stage_summary_error_count": 3,
        },
    )
    daily_path = tmp_path / "order-fulfillment-sync-summary-20260820-110000.json"
    daily_path.write_text(json.dumps(daily_summary), encoding="utf-8")

    digest = sync.build_daily_digest(tmp_path, daily_summary, daily_path, {})

    assert "Ручных действий сегодня не требуется" in digest["message"]
    assert "Bitrix временно не отдал статистику" in digest["message"]
    assert "операционная сводка сформирована" in digest["message"]


def test_daily_digest_includes_pickup_control_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEC_ASSEMBLY_CRM_STATE_PATH", str(tmp_path / "missing.sqlite3"))
    daily_summary = _summary(
        mode="daily",
        stamp="20260824-110000",
        item={
            "mode": "daily",
            "pickup_metrics": {
                "chat_freshness": {
                    "pickup_ready": "2026-08-24T10:59:00",
                    "pickup_inventory": None,
                },
                "active_reactions": 3,
                "inventory_confirmed": 11,
                "inventory_manual_review": 2,
                "pickup_without_notification": 1,
                "sla_72_due": 4,
                "sla_96_due": 1,
                "active_holds": 2,
                "lost_orders": 1,
                "missing_receipt_due": 3,
                "task_routing_errors": 1,
                "task_route_configuration_errors": ["warehouse:mitino:senior_missing"],
                "outbox": {"pending": 2, "retry": 1, "failed": 1},
            },
        },
    )
    daily_path = tmp_path / "order-fulfillment-sync-summary-20260824-110000.json"
    daily_path.write_text(json.dumps(daily_summary), encoding="utf-8")

    digest = sync.build_daily_digest(tmp_path, daily_summary, daily_path, {})

    message = digest["message"]
    assert "Контроль самовывоза" in message
    assert "подтверждено 11, нужно уточнить 2" in message
    assert "без подтверждённой SMS 1" in message
    assert "96 часов 1" in message
    assert "потерянных заказов 1" in message
    assert "получение точкой не подтверждено: 3" in message
    assert "pickup_inventory=нет данных" in message


def test_daily_digest_reports_onec_activity_and_current_technical_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_db = tmp_path / "onec.sqlite3"
    with sqlite3.connect(state_db) as connection:
        connection.execute("""
            CREATE TABLE processed_events (
                event_key TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL,
                site_order_number TEXT NOT NULL,
                rtu_number TEXT NOT NULL,
                crm_response TEXT
            )
            """)
        connection.executemany(
            "INSERT INTO processed_events VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "assembled:1",
                    "2026-08-12 10:00:00",
                    "238500",
                    "РТУ1",
                    json.dumps(
                        {
                            "action": "moved_to_pickup_waiting",
                            "deal_id": 31664,
                            "stage_from": "EXECUTING",
                            "stage_to": "PICKUP_WAITING",
                        }
                    ),
                ),
                (
                    "issued-scan:2",
                    "2026-08-12 10:10:00",
                    "238500",
                    "РТУ1",
                    json.dumps(
                        {
                            "action": "moved_to_won_issued",
                            "deal_id": 31664,
                            "stage_from": "PICKUP_WAITING",
                            "stage_to": "WON",
                        }
                    ),
                ),
            ],
        )
    monkeypatch.setenv("ONEC_ASSEMBLY_CRM_STATE_PATH", str(state_db))
    apply_path = tmp_path / "apply.csv"
    apply_path.write_text(
        "site_order_number,bitrix_deal_id,target_stage,result,applied,reason\n"
        "226255,19368,EXECUTING,technical_review,0,Товар распределен по отгрузкам\n",
        encoding="utf-8",
    )
    quick_summary = _summary(
        mode="quick",
        stamp="20260812-103000",
        item={
            "mode": "quick",
            "dry_run": False,
            "apply_result": str(apply_path),
            "operational_alert_keys": [],
        },
    )
    quick_path = tmp_path / "order-fulfillment-sync-summary-20260812-103000.json"
    quick_path.write_text(json.dumps(quick_summary), encoding="utf-8")
    daily_summary = _summary(
        mode="daily",
        stamp="20260812-110000",
        item={"mode": "daily"},
    )
    daily_path = tmp_path / "order-fulfillment-sync-summary-20260812-110000.json"
    daily_path.write_text(json.dumps(daily_summary), encoding="utf-8")

    digest = sync.build_daily_digest(
        tmp_path,
        daily_summary,
        daily_path,
        {"last_daily_digest_stamp": "20260811-110000"},
    )

    message = digest["message"]
    assert "подтверждена сборка: 1 заказ" in message
    assert "подтверждена выдача: 1 заказ" in message
    assert "выполнено переходов CRM: 2 перехода" in message
    assert "Требуется вмешательство: 1 заказ" in message
    assert "заказ 226255 / сделка 19368" in message
    assert "конфликт количества товаров в заказе и отгрузках" in message
    assert "technical_review" not in message


def test_daily_digest_does_not_include_summaries_after_report_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEC_ASSEMBLY_CRM_STATE_PATH", str(tmp_path / "missing.sqlite3"))
    before_apply = tmp_path / "before.csv"
    before_apply.write_text(
        "site_order_number,bitrix_deal_id,target_stage,result,applied,reason\n"
        "238500,31664,EXECUTING,applied,1,\n",
        encoding="utf-8",
    )
    after_apply = tmp_path / "after.csv"
    after_apply.write_text(
        "site_order_number,bitrix_deal_id,target_stage,result,applied,reason\n"
        "238501,31665,EXECUTING,applied,1,\n",
        encoding="utf-8",
    )
    for stamp, apply_path in (
        ("20260812-103000", before_apply),
        ("20260812-113000", after_apply),
    ):
        payload = _summary(
            mode="quick",
            stamp=stamp,
            item={
                "mode": "quick",
                "dry_run": False,
                "apply_result": str(apply_path),
                "operational_alert_keys": [],
            },
        )
        (tmp_path / f"order-fulfillment-sync-summary-{stamp}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    daily_summary = _summary(
        mode="daily",
        stamp="20260812-110000",
        item={"mode": "daily"},
    )
    daily_path = tmp_path / "order-fulfillment-sync-summary-20260812-110000.json"
    daily_path.write_text(json.dumps(daily_summary), encoding="utf-8")

    digest = sync.build_daily_digest(
        tmp_path,
        daily_summary,
        daily_path,
        {"last_daily_digest_stamp": "20260811-110000"},
    )

    assert "выполнено переходов CRM: 1 переход" in digest["message"]
