from __future__ import annotations

import csv
from pathlib import Path

from app.services import site_order_fulfillment as service


class FakeBitrixClient:
    def __init__(
        self,
        deals: dict[int, service.BitrixDealSnapshot],
        *,
        update_errors: dict[int, Exception | list[Exception | None]] | None = None,
    ) -> None:
        self.deals = deals
        self.update_errors = update_errors or {}
        self.updates: list[tuple[int, str]] = []
        self.update_attempts: dict[int, int] = {}

    def get_deal_by_id(self, deal_id: int) -> service.BitrixDealSnapshot | None:
        return self.deals.get(deal_id)

    def update_deal_stage(self, deal_id: int, target_stage: str) -> bool:
        self.update_attempts[deal_id] = self.update_attempts.get(deal_id, 0) + 1
        if deal_id in self.update_errors:
            configured_error = self.update_errors[deal_id]
            if isinstance(configured_error, list):
                if configured_error:
                    error = configured_error.pop(0)
                    if error is not None:
                        raise error
            else:
                raise configured_error
        self.updates.append((deal_id, target_stage))
        return True


def _outbox_row(
    *,
    state: str = "ready",
    operation: str = "update_stage",
    target_stage: str = "PICKUP_WAITING",
    current_stage: str = "PREPARATION",
    deal_id: int = 11412,
    order_number: str = "218014",
) -> service.OrderFulfillmentStageOutboxRow:
    return service.OrderFulfillmentStageOutboxRow(
        idempotency_key="key-1",
        site_order_number=order_number,
        bitrix_deal_id=deal_id,
        current_stage=current_stage,
        target_stage=target_stage,
        operation=operation,
        state=state,
        chat_event=service.EVENT_PICKUP_UNCLAIMED,
        event_confidence="medium",
        evidence_redacted="<order> не забрали",
        payload_json='{"id":11412,"fields":{"STAGE_ID":"PICKUP_WAITING"}}',
        block_reason=None,
    )


def _deal(
    *,
    deal_id: int = 11412,
    stage_id: str = "PREPARATION",
    order_number: str = "218014",
) -> service.BitrixDealSnapshot:
    return service.BitrixDealSnapshot(
        deal_id=deal_id,
        stage_id=stage_id,
        delivery="Самовывоз",
        payment_status="0",
        raw={service.CRM_ORDER_NUMBER_FIELD: order_number, "STAGE_ID": stage_id},
    )


def test_stage_apply_dry_run_does_not_update_bitrix() -> None:
    client = FakeBitrixClient({11412: _deal()})

    results = service.apply_stage_outbox_rows(
        [_outbox_row()],
        client=client,
        apply=False,
    )

    assert results[0].result == "dry_run_ready"
    assert results[0].applied is False
    assert results[0].dry_run is True
    assert client.updates == []


def test_stage_apply_updates_ready_row_with_live_guards() -> None:
    client = FakeBitrixClient({11412: _deal()})

    results = service.apply_stage_outbox_rows(
        [_outbox_row()],
        client=client,
        apply=True,
    )

    assert results[0].result == "applied"
    assert results[0].applied is True
    assert results[0].dry_run is False
    assert client.updates == [(11412, "PICKUP_WAITING")]


def test_stage_apply_skips_not_ready_and_wrong_target() -> None:
    client = FakeBitrixClient({11412: _deal()})

    results = service.apply_stage_outbox_rows(
        [
            _outbox_row(state="blocked_missing_target_stage"),
            _outbox_row(target_stage="WON"),
        ],
        client=client,
        apply=True,
    )

    assert [item.result for item in results] == [
        "skipped_not_ready",
        "skipped_target_stage",
    ]
    assert client.updates == []


def test_stage_apply_blocks_terminal_stage_and_stage_mismatch() -> None:
    client = FakeBitrixClient(
        {
            1: _deal(deal_id=1, stage_id="WON"),
            2: _deal(deal_id=2, stage_id="NEW"),
            3: _deal(deal_id=3, stage_id="PREPARATION", order_number="999999"),
        }
    )

    results = service.apply_stage_outbox_rows(
        [
            _outbox_row(deal_id=1),
            _outbox_row(deal_id=2),
            _outbox_row(deal_id=3),
            _outbox_row(deal_id=4),
        ],
        client=client,
        apply=True,
    )

    assert [item.result for item in results] == [
        "terminal_live_stage",
        "current_stage_mismatch",
        "order_mismatch",
        "deal_not_found",
    ]
    assert client.updates == []


def test_stage_apply_limit_and_csv_are_secret_safe(tmp_path: Path) -> None:
    client = FakeBitrixClient({11412: _deal(), 11413: _deal(deal_id=11413)})
    rows = [_outbox_row(), _outbox_row(deal_id=11413)]

    results = service.apply_stage_outbox_rows(rows, client=client, apply=True, limit=1)
    path = service.write_stage_apply_result_csv(tmp_path / "result.csv", results)

    assert len(results) == 1
    assert client.updates == [(11412, "PICKUP_WAITING")]
    content = path.read_text(encoding="utf-8-sig")
    assert "https://crm.master-mobile.ru/rest/1/secret" not in content
    with path.open(encoding="utf-8-sig", newline="") as file_obj:
        written = list(csv.DictReader(file_obj))
    assert written[0]["result"] == "applied"


def test_stage_apply_routes_product_shipment_errors_to_technical_review() -> None:
    client = FakeBitrixClient(
        {11412: _deal()},
        update_errors={
            11412: service.BitrixStageTechnicalReviewError(
                "Товар распределен по отгрузкам в количестве 2 шт."
            )
        },
    )

    results = service.apply_stage_outbox_rows(
        [_outbox_row()],
        client=client,
        apply=True,
    )

    assert results[0].result == "technical_review"
    assert results[0].applied is False
    assert "распределен по отгрузкам" in (results[0].reason or "")
    assert client.updates == []


def test_stage_apply_retries_transient_bitrix_update_errors() -> None:
    client = FakeBitrixClient(
        {11412: _deal()},
        update_errors={11412: [RuntimeError("crm.deal.update: http_500 temporary"), None]},
    )

    results = service.apply_stage_outbox_rows(
        [_outbox_row()],
        client=client,
        apply=True,
        attempts=2,
        retry_delay_seconds=0,
    )

    assert results[0].result == "applied"
    assert client.update_attempts[11412] == 2
    assert client.updates == [(11412, "PICKUP_WAITING")]


def test_stage_apply_logs_transient_bitrix_update_error_after_retries() -> None:
    client = FakeBitrixClient(
        {11412: _deal()},
        update_errors={
            11412: [
                RuntimeError("crm.deal.update: http_500 temporary"),
                RuntimeError("crm.deal.update: http_500 temporary"),
            ]
        },
    )

    results = service.apply_stage_outbox_rows(
        [_outbox_row()],
        client=client,
        apply=True,
        attempts=2,
        retry_delay_seconds=0,
    )

    assert results[0].result == "update_error"
    assert results[0].applied is False
    assert results[0].reason is not None
    assert results[0].reason.startswith("transient_bitrix_error")
    assert client.update_attempts[11412] == 2
