from __future__ import annotations

import json
import subprocess

import pytest

from app.services import sdek_refund_cancellation as service


class FakeRunner:
    def __init__(self, payloads: list[list[dict]]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append({"command": command, **kwargs})
        payload = self.payloads.pop(0)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=service.REMOTE_JSON_MARKER + json.dumps(payload),
            stderr="",
        )


def _candidate(
    tracking_number: str | None = "10291528413",
    *,
    order_number: str = "229271",
    deal_id: int = 22406,
) -> service.SdekRefundCandidate:
    return service.SdekRefundCandidate(
        site_order_number=order_number,
        bitrix_deal_id=deal_id,
        tracking_number=tracking_number,
    )


def _inspection_row(
    *,
    statuses: list[str],
    order_number: str = "229271",
    candidate: service.SdekRefundCandidate | None = None,
) -> dict:
    candidate = candidate or _candidate()
    return {
        "operation_key": candidate.operation_key,
        "lookup_result": "found",
        "site_status_id": "YR",
        "refund_verified": True,
        "account_id": "3",
        "shipment_order_number": order_number,
        "statuses": statuses,
    }


def test_extract_tracking_numbers_ignores_six_digit_order_and_deduplicates() -> None:
    assert service.extract_tracking_numbers(
        ["Заказ 229271, трек 10291528413", {"second": "10291528413 / 12345678901"}]
    ) == ["10291528413", "12345678901"]


def test_dry_run_marks_only_initial_statuses_cancel_ready() -> None:
    runner = FakeRunner([[_inspection_row(statuses=["ACCEPTED", "CREATED", "ACCEPTED"])]])

    results = service.process_sdek_refund_candidates(
        [_candidate()],
        apply=False,
        runner=runner,
    )

    assert results == [
        service.SdekRefundResult(
            candidate=_candidate(),
            result="cancel_ready",
            refund_verified=True,
            statuses=("ACCEPTED", "CREATED"),
        )
    ]
    assert len(runner.calls) == 1
    assert "deleteOrder(" not in runner.calls[0]["input"]


def test_apply_rechecks_and_cancels_safe_shipment() -> None:
    candidate = _candidate()
    runner = FakeRunner(
        [
            [_inspection_row(statuses=["ACCEPTED", "CREATED"])],
            [
                {
                    "operation_key": candidate.operation_key,
                    "result": "cancelled",
                    "refund_verified": True,
                    "statuses": ["ACCEPTED", "CREATED"],
                    "reason": None,
                }
            ],
        ]
    )

    results = service.process_sdek_refund_candidates(
        [candidate],
        apply=True,
        authorized_order_numbers=None,
        runner=runner,
    )

    assert results[0].result == "cancelled"
    assert results[0].applied is True
    assert len(runner.calls) == 2
    cancellation_php = runner.calls[1]["input"]
    assert "STATUS_ID'] !== 'YR'" in cancellation_php
    assert "shipment_order_mismatch" in cancellation_php
    assert "array_diff($row['statuses'], $safeStatuses)" in cancellation_php
    assert "$controller->deleteOrder" in cancellation_php


def test_apply_rollout_guard_does_not_cancel_without_authorized_order() -> None:
    runner = FakeRunner([[_inspection_row(statuses=["ACCEPTED", "CREATED"])]])

    results = service.process_sdek_refund_candidates(
        [_candidate()],
        apply=True,
        runner=runner,
    )

    assert results == [
        service.SdekRefundResult(
            candidate=_candidate(),
            result="cancel_not_authorized",
            refund_verified=True,
            statuses=("ACCEPTED", "CREATED"),
            reason="rollout_guard",
        )
    ]
    assert len(runner.calls) == 1
    assert "deleteOrder(" not in runner.calls[0]["input"]


def test_apply_allowlist_cancels_only_authorized_order() -> None:
    first = _candidate()
    second = _candidate(
        "10291528414",
        order_number="229272",
        deal_id=22407,
    )
    runner = FakeRunner(
        [
            [
                _inspection_row(
                    statuses=["ACCEPTED", "CREATED"],
                    candidate=first,
                ),
                _inspection_row(
                    statuses=["ACCEPTED", "CREATED"],
                    order_number="229272",
                    candidate=second,
                ),
            ],
            [
                {
                    "operation_key": first.operation_key,
                    "result": "cancelled",
                    "refund_verified": True,
                    "statuses": ["ACCEPTED", "CREATED"],
                    "reason": None,
                }
            ],
        ]
    )

    results = service.process_sdek_refund_candidates(
        [first, second],
        apply=True,
        authorized_order_numbers={"229271"},
        runner=runner,
    )

    assert [(result.candidate.site_order_number, result.result) for result in results] == [
        ("229271", "cancelled"),
        ("229272", "cancel_not_authorized"),
    ]
    assert len(runner.calls) == 2
    cancellation_php = runner.calls[1]["input"]
    assert "10291528413" in cancellation_php
    assert "10291528414" not in cancellation_php


def test_apply_does_not_call_delete_after_handover() -> None:
    runner = FakeRunner(
        [
            [
                _inspection_row(
                    statuses=[
                        "ACCEPTED",
                        "CREATED",
                        "RECEIVED_AT_SHIPMENT_WAREHOUSE",
                    ]
                )
            ]
        ]
    )

    results = service.process_sdek_refund_candidates(
        [_candidate()],
        apply=True,
        runner=runner,
    )

    assert results[0].result == "blocked_after_handover"
    assert results[0].applied is False
    assert len(runner.calls) == 1


def test_apply_does_not_cancel_track_from_another_order() -> None:
    runner = FakeRunner(
        [[_inspection_row(statuses=["ACCEPTED", "CREATED"], order_number="229272")]]
    )

    results = service.process_sdek_refund_candidates(
        [_candidate()],
        apply=True,
        runner=runner,
    )

    assert results[0].result == "shipment_order_mismatch"
    assert len(runner.calls) == 1


def test_missing_track_is_a_verified_non_delete_result() -> None:
    candidate = _candidate(None)
    runner = FakeRunner(
        [
            [
                {
                    "operation_key": candidate.operation_key,
                    "lookup_result": "missing_tracking",
                    "site_status_id": "YR",
                    "refund_verified": True,
                    "statuses": [],
                }
            ]
        ]
    )

    results = service.process_sdek_refund_candidates(
        [candidate],
        apply=True,
        runner=runner,
    )

    assert results[0].result == "missing_tracking"
    assert results[0].refund_verified is True
    assert len(runner.calls) == 1


def test_candidate_rejects_non_numeric_tracking_value() -> None:
    with pytest.raises(ValueError, match="invalid_sdek_tracking_number"):
        service.process_sdek_refund_candidates(
            [_candidate("1029; DROP TABLE")],
            apply=False,
            runner=FakeRunner([]),
        )
