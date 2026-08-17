from __future__ import annotations

import json
import urllib.error
from datetime import date
from pathlib import Path

import infra.cron.weekly_manager_sales_reports_from_a as weekly_adapter
from app.services.online_demand_metrics import (
    OnlineDemandPeriodMetrics,
    OnlineDemandWeeklySummary,
)
from infra.cron.weekly_manager_sales_reports_from_a import (
    _resolve_chat_ids_for_artifact,
    render_summary,
    sync_weekly_manager_sales_report,
)


def _manifest(*, revision: str = "bundle-r1") -> dict[str, object]:
    return {
        "report_key": "weekly-manager-sales|2026-04-05",
        "revision": revision,
        "generated_at": "2026-04-07T06:10:00",
        "period": {
            "week_start": "2026-03-30",
            "week_end": "2026-04-05",
            "compare_week_start": "2026-03-23",
            "compare_week_end": "2026-03-29",
            "employee_snapshot_date": "2026-04-05",
            "employee_previous_date": "2026-04-03",
        },
        "manager_count": 2,
        "attention_count": 1,
        "employee_case_count": 2,
        "cash_order_count": 0,
        "artifacts": [
            {
                "artifact_type": "sales",
                "title": "Личные продажи менеджеров",
                "filename": "weekly-sales.xlsx",
                "artifact_url": "/api/management/weekly-manager-sales-report/sales?week_end=2026-04-05",
                "sha256": "1c980b22bca31941462855e560db3053e7772efb4b8bc7c4d5d8bb799a1abc6c",
                "size_bytes": 4,
                "message": "Личные продажи менеджеров",
            },
            {
                "artifact_type": "employee",
                "title": "Долги сотрудников",
                "filename": "employee-debt.xlsx",
                "artifact_url": "/api/management/weekly-manager-sales-report/employee?week_end=2026-04-05",
                "sha256": "652d75f9bafb25e55c0e8db77c3a9ea11f87c5167431c08f827375741d1b0c2f",
                "size_bytes": 4,
                "message": "Долги сотрудников",
            },
        ],
    }


def test_sync_weekly_manager_sales_report_deduplicates(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "reports": {
                    "weekly-manager-sales|2026-04-05|rbundle-r1": {
                        "report_key": "weekly-manager-sales|2026-04-05",
                        "revision": "bundle-r1",
                        "delivery_status": "delivered",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fetch_json(path: str, params: dict[str, str]) -> dict[str, object]:
        assert params == {"week_end": "2026-04-05"}
        if path.endswith("/health"):
            return {"status": "ready"}
        return {"payload": _manifest()}

    summary = sync_weekly_manager_sales_report(
        fetch_json=fetch_json,
        download_artifact=lambda url: b"xlsx",
        deliver_artifact=lambda **kwargs: {"sent_count": 2},
        week_end=date(2026, 4, 5),
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
    )

    assert summary["status"] == "ok"
    assert summary["delivered"] == 0
    assert summary["noop"] == 1
    assert summary["actions"][0]["action"] == "noop"


def test_sync_weekly_manager_sales_report_delivers_correction_once(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "reports": {
                    "weekly-manager-sales|2026-04-05|rbundle-r1": {
                        "report_key": "weekly-manager-sales|2026-04-05",
                        "revision": "bundle-r1",
                        "delivery_status": "delivered",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    delivered: list[dict[str, object]] = []

    def fetch_json(path: str, params: dict[str, str]) -> dict[str, object]:
        if path.endswith("/health"):
            return {"status": "ready"}
        return {"payload": _manifest(revision="bundle-r2")}

    def download_artifact(url: str) -> bytes:
        if "/sales?" in url:
            return b"xlsx"
        return b"debt"

    def deliver_artifact(**kwargs):
        delivered.append(kwargs)
        return {"sent_count": 2}

    summary = sync_weekly_manager_sales_report(
        fetch_json=fetch_json,
        download_artifact=download_artifact,
        deliver_artifact=deliver_artifact,
        week_end=date(2026, 4, 5),
        state_path=state_path,
        artifact_dir=tmp_path / "artifacts",
    )

    assert summary["status"] == "ok"
    assert summary["delivered"] == 1
    assert summary["failed"] == 0
    assert summary["sent_documents"] == 4
    assert len(delivered) == 2
    assert all(item["is_correction"] is True for item in delivered)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    saved = state["reports"]["weekly-manager-sales|2026-04-05|rbundle-r2"]
    assert saved["delivery_status"] == "delivered"
    assert saved["sent_documents"] == 4


def test_sync_weekly_manager_sales_report_returns_error_when_source_unavailable(
    tmp_path: Path,
) -> None:
    def fetch_json(path: str, params: dict[str, str]) -> dict[str, object]:
        raise urllib.error.URLError("server-a down")

    summary = sync_weekly_manager_sales_report(
        fetch_json=fetch_json,
        download_artifact=lambda url: b"xlsx",
        deliver_artifact=lambda **kwargs: {"sent_count": 2},
        week_end=date(2026, 4, 5),
        state_path=tmp_path / "state.json",
        artifact_dir=tmp_path / "artifacts",
    )

    assert summary["status"] == "error"
    assert summary["health_status"] == "unavailable"
    assert summary["failed"] == 1


def test_resolve_chat_ids_for_artifact_prefers_scoped_override() -> None:
    env = {
        "WEEKLY_MANAGER_SALES_B_TELEGRAM_CHAT_ID": "1287954453,911475089",
        "WEEKLY_MANAGER_SALES_B_TELEGRAM_CHAT_ID_SALES": "911475089",
    }

    assert _resolve_chat_ids_for_artifact(env, artifact_type="sales") == ["911475089"]
    assert _resolve_chat_ids_for_artifact(env, artifact_type="employee") == [
        "1287954453",
        "911475089",
    ]


def test_render_summary_includes_status_line() -> None:
    summary = {
        "status": "ok",
        "week_end": "2026-04-05",
        "health_status": "ready",
        "fetched": 1,
        "delivered": 1,
        "noop": 0,
        "failed": 0,
        "sent_documents": 4,
        "actions": [
            {
                "action": "deliver",
                "report_key": "weekly-manager-sales|2026-04-05",
                "revision": "bundle-r2",
                "is_correction": True,
            }
        ],
    }

    rendered = render_summary(summary)

    assert "weekly_manager_sales_reports_from_a: ok" in rendered
    assert "sent_documents: 4" in rendered
    assert "correction=True" in rendered


def test_append_online_demand_to_caption_adds_sales_block(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_fetch(**kwargs):
        calls.append(kwargs)
        return OnlineDemandWeeklySummary(
            week_start=date(2026, 3, 30),
            week_end=date(2026, 4, 5),
            compare_week_start=date(2026, 3, 23),
            compare_week_end=date(2026, 3, 29),
            current=OnlineDemandPeriodMetrics(
                visits=1000,
                visitors=700,
                purchases=25,
                click_buy=80,
                begin_checkout=40,
                phone_clicks=7,
                site_searches=120,
                primary_source_name="Переходы из поисковых систем",
                primary_source_visits=600,
                primary_source_purchases=20,
            ),
            previous=OnlineDemandPeriodMetrics(
                visits=900,
                visitors=650,
                purchases=20,
                click_buy=70,
                begin_checkout=35,
                phone_clicks=5,
                site_searches=100,
                primary_source_name="Переходы из поисковых систем",
                primary_source_visits=550,
                primary_source_purchases=15,
            ),
        )

    monkeypatch.setattr(weekly_adapter, "fetch_online_demand_weekly_summary", fake_fetch)

    caption = weekly_adapter._append_online_demand_to_caption(
        {"WEEKLY_MANAGER_SALES_METRIKA_TOKEN": "secret-token"},
        artifact_type="sales",
        caption="Личные продажи менеджеров",
        manifest=_manifest(),
    )

    assert "Личные продажи менеджеров" in caption
    assert "📊 Онлайн-спрос и конверсия" in caption
    assert "🛒 Покупки: 25 | +25,00%" in caption
    assert calls[0]["week_start"] == date(2026, 3, 30)
    assert calls[0]["counter_id"] == "49993429"


def test_append_online_demand_to_caption_skips_without_token() -> None:
    caption = weekly_adapter._append_online_demand_to_caption(
        {},
        artifact_type="sales",
        caption="Личные продажи менеджеров",
        manifest=_manifest(),
    )

    assert caption == "Личные продажи менеджеров"
