from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ReceivableWorkEvent, ReceivableWorkItem
from app.services.receivable_workflow import STATUS_PAID, stable_key_for_counterparty
from tasks.normalize_receivable_paid_comments_20260805 import (
    EVENT_MANAGER_COMMENT_CLEARED,
    NORMALIZATION_SOURCE,
    normalize_paid_manager_comments,
)


def _paid_item(counterparty_ref: str = "cp-paid") -> ReceivableWorkItem:
    return ReceivableWorkItem(
        stable_key=stable_key_for_counterparty(counterparty_ref),
        counterparty_ref=counterparty_ref,
        status=STATUS_PAID,
        current_balance=Decimal("100"),
        last_contact_comment="Комментарий оплаченного долга",
    )


def test_paid_comment_normalization_dry_run_does_not_mutate(db_session: Session) -> None:
    item = _paid_item()
    db_session.add(item)
    db_session.flush()

    result = normalize_paid_manager_comments(db_session, apply=False)

    assert result["mode"] == "dry_run"
    assert result["candidate_count"] == 1
    assert result["applied_count"] == 0
    assert result["candidates"] == [
        {
            "work_item_id": item.id,
            "counterparty_ref": "cp-paid",
            "comment_length": len("Комментарий оплаченного долга"),
        }
    ]
    assert item.last_contact_comment == "Комментарий оплаченного долга"
    assert db_session.scalars(select(ReceivableWorkEvent)).all() == []


def test_paid_comment_normalization_apply_audits_then_clears(db_session: Session) -> None:
    item = _paid_item()
    db_session.add(item)
    db_session.flush()

    result = normalize_paid_manager_comments(db_session, apply=True)
    second = normalize_paid_manager_comments(db_session, apply=True)
    event = db_session.scalar(select(ReceivableWorkEvent))

    assert result["candidate_count"] == 1
    assert result["applied_count"] == 1
    assert second["candidate_count"] == 0
    assert second["applied_count"] == 0
    assert item.last_contact_comment is None
    assert event is not None
    assert event.event_type == EVENT_MANAGER_COMMENT_CLEARED
    assert event.source == NORMALIZATION_SOURCE
    assert event.comment == "Комментарий оплаченного долга"
    assert event.payload["manager_comment_cleared"] is True
