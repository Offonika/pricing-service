from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services import site_order_fulfillment as service
from infra.cron import order_fulfillment_sync as sync


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


class FakeRefundDealClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call(self, method: str, payload: dict) -> dict:
        assert method == "crm.deal.list"
        self.calls.append(payload)
        order_numbers = payload["filter"][f"@{service.CRM_ORDER_NUMBER_FIELD}"]
        return {
            "result": [
                {
                    "ID": order_number,
                    "STAGE_ID": "EXECUTING" if index == 0 else "WON",
                    service.CRM_ORDER_NUMBER_FIELD: order_number,
                }
                for index, order_number in enumerate(order_numbers)
            ]
        }


class FakeNotifyClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call(self, method: str, payload: dict) -> dict:
        self.calls.append({"method": method, "payload": payload})
        return {"result": 1000 + len(self.calls)}


class FakeTimelineClient:
    def __init__(self) -> None:
        self.comments: list[str] = []
        self.add_calls = 0

    def call(self, method: str, payload: dict) -> dict:
        if method == "crm.timeline.comment.list":
            return {
                "result": [
                    {"ID": str(index + 1), "COMMENT": comment}
                    for index, comment in enumerate(self.comments)
                ]
            }
        if method == "crm.timeline.comment.add":
            self.add_calls += 1
            self.comments.append(payload["fields"]["COMMENT"])
            return {"result": self.add_calls}
        raise AssertionError(method)


def _deal(
    *,
    deal_id: int = 100,
    stage_id: str = "NEW",
    order_number: str = "218001",
    delivery: str = "Самовывоз",
    payment_status: str = "0",
    assembled: str | None = None,
    tracking_number: str | None = None,
) -> service.BitrixDealSnapshot:
    raw = {service.CRM_ORDER_NUMBER_FIELD: order_number}
    if assembled is not None:
        raw[sync.CRM_ASSEMBLED_FIELD] = assembled
    if tracking_number is not None:
        raw[sync.CRM_CDEK_TRACKING_FIELD] = tracking_number
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


def test_decide_delivery_review_completed_order_closes_won() -> None:
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

    assert decision.action == "update_stage"
    assert decision.recommended_stage == "WON"
    assert decision.review_reason == "delivery_review_completed_to_won"


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
    assert assembled_pickup.recommended_stage == "PICKUP_WAITING"
    assert assembled_pickup.review_reason == "delivery_review_pickup_assembled_waiting"
    assert paid_courier.recommended_stage == "FINAL_INVOICE"
    assert paid_courier.review_reason == "delivery_review_paid_carrier_assembled"


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


def test_decide_full_refund_routes_to_dismantling_before_other_rules() -> None:
    decision = sync.decide_new_deal_stage(
        _deal(
            stage_id="FINAL_INVOICE",
            delivery="СДЭК",
            payment_status="0",
            assembled="1",
            tracking_number="10291528413",
        ),
        order_status=sync.SaleOrderStatus(
            order_number="229271",
            canceled=False,
            status_id="YR",
            payed=False,
        ),
    )

    assert decision.recommended_stage == "DISMANTLING"
    assert decision.review_reason == "fully_refunded_to_dismantling"


def test_build_sdek_refund_candidates_keeps_track_or_explicit_missing_track() -> None:
    tracked = _deal(
        deal_id=1,
        order_number="229271",
        delivery="СДЭК",
        tracking_number="10291528413",
    )
    missing = _deal(
        deal_id=2,
        order_number="231004",
        delivery="СДЭК (Доставка курьером)",
    )
    status = sync.SaleOrderStatus(
        order_number="229271",
        canceled=False,
        status_id="YR",
        payed=False,
    )
    missing_status = sync.SaleOrderStatus(
        order_number="231004",
        canceled=False,
        status_id="YR",
        payed=False,
    )
    decisions = [
        sync.decide_new_deal_stage(tracked, order_status=status),
        sync.decide_new_deal_stage(missing, order_status=missing_status),
    ]

    candidates = sync.build_sdek_refund_candidates([tracked, missing], decisions)

    assert [(row.site_order_number, row.tracking_number) for row in candidates] == [
        ("229271", "10291528413"),
        ("231004", None),
    ]


def test_filter_unverified_sdek_refund_blocks_stale_stage_transition() -> None:
    deal = _deal(deal_id=1, order_number="229271", delivery="СДЭК")
    decision = sync.decide_new_deal_stage(
        deal,
        order_status=sync.SaleOrderStatus(
            order_number="229271",
            canceled=False,
            status_id="YR",
            payed=False,
        ),
    )
    candidate = sync.sdek_refunds.SdekRefundCandidate("229271", 1, None)
    stale = sync.sdek_refunds.SdekRefundResult(
        candidate=candidate,
        result="order_not_refunded",
        refund_verified=False,
    )

    assert sync.filter_unverified_sdek_refund_decisions([decision], [stale]) == []


def test_sdek_refund_rollout_defaults_off_and_parses_allowlist() -> None:
    assert sync.resolve_sdek_refund_cancellation_rollout({}) == (
        "off",
        frozenset(),
    )
    assert sync.resolve_sdek_refund_cancellation_rollout(
        {
            sync.SDEK_REFUND_CANCELLATION_MODE_ENV: "allowlist",
            sync.SDEK_REFUND_CANCELLATION_ALLOWLIST_ENV: "225281, 229002;225281",
        }
    ) == ("allowlist", frozenset({"225281", "229002"}))
    assert sync.resolve_sdek_refund_cancellation_rollout(
        {
            sync.SDEK_REFUND_CANCELLATION_MODE_ENV: "all",
            sync.SDEK_REFUND_CANCELLATION_ALLOWLIST_ENV: "ignored",
        }
    ) == ("all", None)


def test_sdek_refund_rollout_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="invalid_sdek_refund_cancellation_mode"):
        sync.resolve_sdek_refund_cancellation_rollout(
            {sync.SDEK_REFUND_CANCELLATION_MODE_ENV: "enabled"}
        )
    with pytest.raises(ValueError, match="invalid_sdek_refund_cancellation_allowlist"):
        sync.resolve_sdek_refund_cancellation_rollout(
            {
                sync.SDEK_REFUND_CANCELLATION_MODE_ENV: "allowlist",
                sync.SDEK_REFUND_CANCELLATION_ALLOWLIST_ENV: "225281,DROP",
            }
        )


def test_order_fulfillment_shell_preserves_sdek_rollout_overrides() -> None:
    shell_source = Path(sync.__file__).with_suffix(".sh").read_text(encoding="utf-8")

    assert "SDEK_REFUND_MODE_OVERRIDE_SET" in shell_source
    assert (
        'export ORDER_FULFILLMENT_SDEK_REFUND_CANCELLATION_MODE="${SDEK_REFUND_MODE_OVERRIDE}"'
        in shell_source
    )
    assert "SDEK_REFUND_ALLOWLIST_OVERRIDE_SET" in shell_source
    assert (
        'export ORDER_FULFILLMENT_SDEK_REFUND_CANCELLATION_ALLOWLIST="${SDEK_REFUND_ALLOWLIST_OVERRIDE}"'
        in shell_source
    )


def test_sdek_refund_rollout_guard_blocks_stage_until_cancellation() -> None:
    deal = _deal(deal_id=1, order_number="229271", delivery="СДЭК")
    decision = sync.decide_new_deal_stage(
        deal,
        order_status=sync.SaleOrderStatus(
            order_number="229271",
            canceled=False,
            status_id="YR",
            payed=False,
        ),
    )
    candidate = sync.sdek_refunds.SdekRefundCandidate("229271", 1, "10291528413")
    guarded = sync.sdek_refunds.SdekRefundResult(
        candidate=candidate,
        result="cancel_not_authorized",
        refund_verified=True,
        statuses=("ACCEPTED", "CREATED"),
        reason="rollout_guard",
    )
    cancelled = sync.sdek_refunds.SdekRefundResult(
        candidate=candidate,
        result="cancelled",
        refund_verified=True,
        statuses=("ACCEPTED", "CREATED"),
        applied=True,
    )

    assert sync.filter_unverified_sdek_refund_decisions([decision], [guarded]) == []
    assert sync.filter_unverified_sdek_refund_decisions([decision], [cancelled]) == [decision]
    assert (
        sync.filter_unverified_sdek_refund_decisions(
            [decision],
            [cancelled],
            automation_enabled=False,
        )
        == []
    )


@pytest.mark.parametrize(
    ("result_name", "allows_stage"),
    [
        ("cancelled", True),
        ("already_removed", True),
        ("missing_tracking", True),
        ("cancel_ready", False),
        ("cancel_not_authorized", False),
        ("shipment_not_found", False),
        ("shipment_order_mismatch", False),
        ("blocked_after_handover", False),
        ("blocked_unknown_status", False),
        ("cancel_error", False),
    ],
)
def test_sdek_refund_stage_requires_safe_terminal_result(
    result_name: str,
    allows_stage: bool,
) -> None:
    deal = _deal(deal_id=1, order_number="229271", delivery="СДЭК")
    decision = sync.decide_new_deal_stage(
        deal,
        order_status=sync.SaleOrderStatus(
            order_number="229271",
            canceled=False,
            status_id="YR",
            payed=False,
        ),
    )
    result = sync.sdek_refunds.SdekRefundResult(
        candidate=sync.sdek_refunds.SdekRefundCandidate(
            "229271",
            1,
            "10291528413",
        ),
        result=result_name,
        refund_verified=True,
    )

    filtered = sync.filter_unverified_sdek_refund_decisions([decision], [result])

    assert filtered == ([decision] if allows_stage else [])


def test_sdek_refund_rollout_guard_does_not_write_timeline_comment() -> None:
    client = FakeTimelineClient()
    result = sync.sdek_refunds.SdekRefundResult(
        candidate=sync.sdek_refunds.SdekRefundCandidate(
            site_order_number="229271",
            bitrix_deal_id=22406,
            tracking_number="10291528413",
        ),
        result="cancel_not_authorized",
        refund_verified=True,
        statuses=("ACCEPTED", "CREATED"),
        reason="rollout_guard",
    )

    outcomes = sync.record_sdek_refund_timeline_comments(
        client=client,  # type: ignore[arg-type]
        results=[result],
        apply=True,
    )

    assert outcomes[result.candidate.operation_key] == "rollout_guarded"
    assert client.add_calls == 0


def test_sdek_refund_timeline_comment_is_idempotent_and_keeps_track() -> None:
    client = FakeTimelineClient()
    result = sync.sdek_refunds.SdekRefundResult(
        candidate=sync.sdek_refunds.SdekRefundCandidate(
            site_order_number="229271",
            bitrix_deal_id=22406,
            tracking_number="10291528413",
        ),
        result="cancelled",
        refund_verified=True,
        statuses=("ACCEPTED", "CREATED"),
        applied=True,
    )

    first = sync.record_sdek_refund_timeline_comments(
        client=client,  # type: ignore[arg-type]
        results=[result],
        apply=True,
    )
    second = sync.record_sdek_refund_timeline_comments(
        client=client,  # type: ignore[arg-type]
        results=[result],
        apply=True,
    )

    assert first[result.candidate.operation_key] == "added"
    assert second[result.candidate.operation_key] == "already_present"
    assert client.add_calls == 1
    assert "10291528413" in client.comments[0]
    assert "Трек сохранён" in client.comments[0]


def test_terminal_sdek_state_prevents_second_delete_and_retries_comment() -> None:
    candidate = sync.sdek_refunds.SdekRefundCandidate(
        site_order_number="229271",
        bitrix_deal_id=22406,
        tracking_number="10291528413",
    )
    result = sync.sdek_refunds.SdekRefundResult(
        candidate=candidate,
        result="cancelled",
        refund_verified=True,
        statuses=("ACCEPTED", "CREATED"),
        applied=True,
    )
    state: dict = {}

    changed = sync.update_sdek_refund_terminal_state(
        state,
        [result],
        {candidate.operation_key: "timeline_add_error"},
    )
    pending, comment_retry = sync.split_terminal_sdek_refund_candidates([candidate], state)

    assert changed is True
    assert pending == []
    assert comment_retry[0].result == "cancelled"
    assert comment_retry[0].reason == "terminal_state_comment_retry"

    sync.update_sdek_refund_terminal_state(
        state,
        comment_retry,
        {candidate.operation_key: "added"},
    )
    pending, skipped = sync.split_terminal_sdek_refund_candidates([candidate], state)

    assert pending == []
    assert skipped[0].result == "skipped_terminal_state"


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
    }

    assert sync.quick_onec_settlement_candidate_orders(deals, statuses) == [
        "218001",
        "218002",
    ]


def test_decide_pickup_waiting_closes_only_when_payment_is_confirmed() -> None:
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
    assert completed_unpaid.review_reason == "pickup_waiting_completed_needs_payment_check"
    assert completed_paid.action == "update_stage"
    assert completed_paid.recommended_stage == "WON"
    assert completed_paid.review_reason == "pickup_waiting_completed_paid_to_won"
    assert completed_onec_paid.action == "update_stage"
    assert completed_onec_paid.recommended_stage == "WON"
    assert completed_onec_paid.review_reason == (
        "pickup_waiting_completed_onec_confirmed_to_won:onec_no_debt"
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


def test_fetch_new_deals_includes_prepayment_stage_per_stage_limit() -> None:
    client = FakeDealListClient()

    deals = sync.fetch_new_deals(client, limit=1)  # type: ignore[arg-type]

    assert [deal.stage_id for deal in deals] == list(sync.QUICK_STAGE_IDS)
    assert [call["payload"]["filter"]["STAGE_ID"] for call in client.calls] == list(
        sync.QUICK_STAGE_IDS
    )


def test_fetch_full_refund_order_numbers_uses_targeted_site_status_query() -> None:
    calls: list[dict] = []

    def runner(command: list[str], **kwargs):
        calls.append({"command": command, **kwargs})
        return type(
            "Completed",
            (),
            {"returncode": 0, "stdout": '["236532","231004","bad"]'},
        )()

    result = sync.fetch_full_refund_order_numbers(limit=250, runner=runner)

    assert result == ["231004", "236532"]
    assert "STATUS_ID = 'YR'" in calls[0]["input"]
    assert "LIMIT 250" in calls[0]["input"]


def test_fetch_deals_by_site_orders_chunks_and_excludes_closed_deals() -> None:
    client = FakeRefundDealClient()

    deals = sync.fetch_deals_by_site_orders(
        client,  # type: ignore[arg-type]
        ["231004", "233441", "236532"],
        chunk_size=2,
    )

    assert [deal.raw[service.CRM_ORDER_NUMBER_FIELD] for deal in deals] == [
        "231004",
        "236532",
    ]
    assert len(client.calls) == 2


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


def test_new_deal_outbox_allows_won_with_payment_flag() -> None:
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

    assert len(rows) == 1
    assert rows[0].target_stage == "WON"
    payload = json.loads(rows[0].payload_json)
    assert payload["fields"]["STAGE_ID"] == "WON"
    assert payload["fields"][service.CRM_PAYMENT_FIELD] == "1"


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


def test_stage_summary_uses_fast_totals_without_pagination() -> None:
    client = FakeListClient()

    rows = sync.fetch_stage_summary(client)  # type: ignore[arg-type]

    assert rows[0]["deal_count"] == 42
    assert rows[0]["internet_order_count"] == 42
    assert len(client.calls) == len(sync.PROCESS_STAGES) * 2
    assert all(call["payload"]["start"] == 0 for call in client.calls)


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
        order_fulfillment_notify_state_path=str(tmp_path / "state.json"),
    )

    assert settings.order_fulfillment_notify_business_user_ids == [10, 20]
    assert settings.order_fulfillment_notify_tech_user_ids == [30, 40]


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


def test_daily_digest_deduplicates_repeated_cron_summaries(tmp_path: Path) -> None:
    client = FakeNotifyClient()
    quick_item = {
        "mode": "quick",
        "deal_keys": ["deal:1"],
        "ready_keys": ["ready-1"],
        "manual_review_keys": ["manual-1"],
        "manual_review_reason_counts": {"carrier": 1},
        "technical_review_keys": [],
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
    assert "Проверено уникальных сделок: 1" in message
    assert "Готово к dry-run/apply: 1" in message
    assert "Ручной разбор: 1" in message
    assert "Неизвестные доставки: 2" in message
