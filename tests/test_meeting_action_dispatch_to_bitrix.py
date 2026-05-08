from __future__ import annotations

import json
import sys

from infra.cron import meeting_action_dispatch_to_bitrix


def _payload() -> dict:
    return {
        "owner_group": "procurement",
        "owner_label": "Отдел закупки",
        "report_date": "2026-05-06",
        "new_count": 1,
        "overdue_count": 0,
        "oldest_overdue_date": None,
        "top_topics": [{"topic": "наличие", "count": 1}],
        "new_items": [
            {
                "started_at_msk": "2026-05-06 10:00",
                "topic": "наличие",
                "priority": "средний",
                "manager_id": "31",
                "summary": "Клиент ждёт наличие позиции",
                "next_step": "Подтвердить наличие или предложить замену.",
            }
        ],
        "overdue_items": [],
    }


def test_delivery_state_helpers_find_sent_delivery(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state = meeting_action_dispatch_to_bitrix._load_delivery_state(state_path)
    meeting_action_dispatch_to_bitrix._record_delivery(
        state,
        dedupe_key="box|chat1|procurement|2026-05-06|sha256:test",
        contour="box",
        dialog_id="chat1",
        owner_group="procurement",
        report_date="2026-05-06",
        message_hash="sha256:test",
        message_id="42",
    )
    meeting_action_dispatch_to_bitrix._save_delivery_state(state_path, state)

    loaded = meeting_action_dispatch_to_bitrix._load_delivery_state(state_path)
    found = meeting_action_dispatch_to_bitrix._find_delivery(
        loaded, "box|chat1|procurement|2026-05-06|sha256:test"
    )

    assert found is not None
    assert found["message_id"] == "42"


def test_main_returns_noop_for_seeded_state(monkeypatch, tmp_path, capsys) -> None:
    payload = _payload()
    message = meeting_action_dispatch_to_bitrix.render_owner_group_message(payload)
    message_hash = meeting_action_dispatch_to_bitrix._message_hash(message)
    dedupe_key = meeting_action_dispatch_to_bitrix._delivery_dedupe_key(
        contour="box",
        dialog_id="chat1",
        owner_group="procurement",
        report_date="2026-05-06",
        message_hash=message_hash,
    )
    state_path = tmp_path / "state.json"
    state = {"version": 1, "deliveries": []}
    meeting_action_dispatch_to_bitrix._record_delivery(
        state,
        dedupe_key=dedupe_key,
        contour="box",
        dialog_id="chat1",
        owner_group="procurement",
        report_date="2026-05-06",
        message_hash=message_hash,
        message_id="42",
    )
    meeting_action_dispatch_to_bitrix._save_delivery_state(state_path, state)

    monkeypatch.setattr(
        meeting_action_dispatch_to_bitrix,
        "_load_env",
        lambda _path: {
            "DATABASE_URL": "postgresql://example/masked",
            "BITRIX24_BOX_WEBHOOK_URL": "https://example.invalid/rest/1/masked",
        },
    )
    monkeypatch.setattr(
        meeting_action_dispatch_to_bitrix, "_query_rows", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        meeting_action_dispatch_to_bitrix,
        "build_owner_group_digest",
        lambda *_args, **_kwargs: {"procurement": payload},
    )
    monkeypatch.setattr(
        meeting_action_dispatch_to_bitrix,
        "_load_routing",
        lambda _env: {
            "procurement": meeting_action_dispatch_to_bitrix.ChatRoute(
                owner_group="procurement", dialog_id="chat1"
            )
        },
    )
    monkeypatch.setattr(
        meeting_action_dispatch_to_bitrix,
        "_resolve_chat",
        lambda *_args, **_kwargs: {"dialog_id": "chat1"},
    )
    monkeypatch.setattr(
        meeting_action_dispatch_to_bitrix,
        "_send_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("send must not run for noop")
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meeting_action_dispatch_to_bitrix.py",
            "--date",
            "2026-05-07",
            "--owner-group",
            "procurement",
            "--delivery-state-path",
            str(state_path),
            "--json",
        ],
    )

    meeting_action_dispatch_to_bitrix.main()

    output = json.loads(capsys.readouterr().out)
    assert output["results"][0]["status"] == "noop"
    assert output["results"][0]["message_id"] == "42"
