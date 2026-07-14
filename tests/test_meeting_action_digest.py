from __future__ import annotations

from datetime import date

from infra.cron.meeting_action_digest import (
    build_meeting_action_digest,
    classify_candidate,
    render_meeting_action_digest,
)


def test_classify_candidate_detects_return_exchange() -> None:
    candidate = classify_candidate(
        {
            "call_id": "call-1",
            "source": "bitrix",
            "store_id": "bitrix",
            "manager_id": "1",
            "started_at_msk": "2026-03-19 19:41:54",
            "outcome": "pending_review",
            "sentiment": "unknown",
            "summary": "Пришла плата нерабочая. Как можно поменять?",
            "transcript": "Клиент говорит, что товар нерабочий и просит обмен.",
        }
    )

    assert candidate is not None
    assert candidate.topic == "возврат/обмен"
    assert candidate.owner_group == "service_quality"
    assert candidate.priority == "high"


def test_build_meeting_action_digest_splits_new_and_overdue_items() -> None:
    rows = [
        {
            "call_id": "new-1",
            "source": "bitrix",
            "store_id": "bitrix",
            "manager_id": "1",
            "started_at_msk": "2026-03-19 19:41:54",
            "outcome": "pending_review",
            "sentiment": "unknown",
            "summary": "Пришла плата нерабочая. Как можно поменять?",
            "transcript": "Товар нерабочий, нужен обмен.",
        },
        {
            "call_id": "old-1",
            "source": "retail_megafon",
            "store_id": "Лира",
            "manager_id": "31",
            "started_at_msk": "2026-03-16 12:10:00",
            "outcome": "callback",
            "sentiment": "unknown",
            "summary": "Клиент ждёт обратный звонок по заказу.",
            "transcript": "Нужно перезвонить и сообщить статус заказа.",
        },
    ]

    digest = build_meeting_action_digest(rows, anchor_date=date(2026, 3, 20), overdue_days=2)
    rendered = render_meeting_action_digest(digest)

    assert digest["status"] == "ready"
    assert digest["new_items_count"] == 1
    assert digest["overdue_items_count"] == 1
    assert digest["new_items"][0]["topic"] == "возврат/обмен"
    assert digest["overdue_items"][0]["owner_group"] == "retail:Лира"
    assert "Новые action items:" in rendered
    assert "Просроченные/open:" in rendered


def test_build_meeting_action_digest_empty_state() -> None:
    digest = build_meeting_action_digest([], anchor_date=date(2026, 3, 20))

    assert digest["status"] == "empty"
    assert digest["new_items_count"] == 0
    assert digest["overdue_items_count"] == 0
