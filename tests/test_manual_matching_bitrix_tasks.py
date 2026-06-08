from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.models import CompetitorItem, Product
from app.models.competitor_item_match import (
    CompetitorItemMatch,
    CompetitorItemMatchStatus,
)
from app.services.manual_matching_bitrix_tasks import (
    DEFAULT_CREATED_BY_ID,
    DEFAULT_GROUP_ID,
    build_manual_matching_bitrix_task_drafts,
)
from app.services.manual_matching_control import build_manual_matching_control_report
from tasks import manual_matching_bitrix_tasks

REPORT_DATE = date(2026, 5, 26)


def _product() -> Product:
    return Product(
        article="P-1",
        name="Дисплей для Apple iPhone 11 + тачскрин (черный) (OLED)",
        subject="дисплей",
        subject_1c="дисплей",
    )


def _display_item() -> CompetitorItem:
    return CompetitorItem(
        competitor="moba",
        external_id="LCD-1",
        name="Дисплей для iPhone 11 в сборе Черный OLED",
        item_type="display",
    )


def test_bitrix_task_drafts_assign_displays_to_omar_and_general_queue_to_buyers(
    db_session: Session,
) -> None:
    product = _product()
    item = _display_item()
    db_session.add_all([product, item])
    db_session.flush()
    db_session.add(
        CompetitorItemMatch(
            competitor_item_id=item.id,
            product_id=product.id,
            status=CompetitorItemMatchStatus.NEEDS_REVIEW,
        )
    )
    db_session.commit()

    report = build_manual_matching_control_report(db_session, report_date=REPORT_DATE)
    drafts = build_manual_matching_bitrix_task_drafts(report)

    assert [draft.responsible_id for draft in drafts] == [130757, 130756, 130917, 130747]
    assert [draft.plan for draft in drafts] == [10, 10, 10, 10]
    assert [draft.task_focus for draft in drafts] == ["display", "general", "general", "general"]
    assert all(draft.created_by_id == DEFAULT_CREATED_BY_ID for draft in drafts)
    assert all(draft.group_id == DEFAULT_GROUP_ID for draft in drafts)
    assert all(draft.auditors == () for draft in drafts)
    assert all(draft.deadline == "2026-05-26T18:00:00+03:00" for draft in drafts)
    assert "всего: 1" in drafts[0].description
    assert "дисплеи для Омара: 1" in drafts[0].description
    assert "обычные товары не брать" in drafts[0].description
    assert "по обычной очереди без дисплеев" in drafts[1].description
    assert "Дисплеи не брать: их разбирает Омар" in drafts[1].description

    fields = drafts[0].bitrix_fields()
    assert fields["CREATED_BY"] == 115204
    assert fields["RESPONSIBLE_ID"] == 130757
    assert fields["GROUP_ID"] == 0
    assert "AUDITORS" not in fields
    assert "закрытие этой задачи само по себе не засчитывает план" in fields["DESCRIPTION"]


def test_manual_matching_bitrix_tasks_dry_run_does_not_call_bitrix(
    db_session: Session,
    sqlite_engine,
    tmp_path,
    capsys,
) -> None:
    state_path = tmp_path / "state.json"

    def fail_call(_base: str, method: str, _payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"unexpected Bitrix call: {method}")

    result = manual_matching_bitrix_tasks.main(
        [
            "--date",
            REPORT_DATE.isoformat(),
            "--database-url",
            str(sqlite_engine.url),
            "--state-path",
            str(state_path),
            "--json",
        ],
        bitrix_call_func=fail_call,
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert result["mode"] == "dry_run"
    assert payload["summary"]["would_create"] == 4
    assert payload["tasks"][0]["responsible_id"] == 130757
    assert payload["tasks"][0]["task_focus"] == "display"
    assert not state_path.exists()
    assert "postgresql://" not in output
    assert "/rest/" not in output


def test_manual_matching_bitrix_tasks_apply_is_idempotent_with_state(
    db_session: Session,
    sqlite_engine,
    tmp_path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "state.json"
    calls: list[tuple[str, dict[str, Any]]] = []
    created_ids = iter([501, 502, 503, 504])

    monkeypatch.setattr(
        manual_matching_bitrix_tasks,
        "resolve_webhook",
        lambda: "https://bitrix.example/rest/1/token",
    )

    def fake_call(_base: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, payload))
        if method == "tasks.task.list":
            return {"result": {"tasks": []}}
        if method == "tasks.task.add":
            return {"result": {"task": {"id": next(created_ids)}}}
        raise AssertionError(f"unexpected Bitrix method: {method}")

    first = manual_matching_bitrix_tasks.main(
        [
            "--date",
            REPORT_DATE.isoformat(),
            "--database-url",
            str(sqlite_engine.url),
            "--state-path",
            str(state_path),
            "--apply",
        ],
        bitrix_call_func=fake_call,
    )

    assert first["summary"]["created"] == 4
    assert [method for method, _payload in calls].count("tasks.task.list") == 4
    assert [method for method, _payload in calls].count("tasks.task.add") == 4
    assert state_path.exists()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(state["tasks"]) == 4
    assert any(item["task_focus"] == "display" for item in state["tasks"].values())

    calls.clear()
    second = manual_matching_bitrix_tasks.main(
        [
            "--date",
            REPORT_DATE.isoformat(),
            "--database-url",
            str(sqlite_engine.url),
            "--state-path",
            str(state_path),
            "--apply",
        ],
        bitrix_call_func=fake_call,
    )

    assert second["summary"]["created"] == 0
    assert second["summary"]["skipped"] == 4
    assert calls == []
