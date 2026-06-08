from __future__ import annotations

from datetime import date
from pathlib import Path

import infra.cron.monthly_online_demand_report as monthly_report
from app.services.online_demand_metrics import (
    OnlineDemandLandingPage,
    OnlineDemandPeriodMetrics,
    OnlineDemandWeeklySummary,
)


def test_render_monthly_online_demand_report_includes_pages() -> None:
    rendered = monthly_report.render_monthly_online_demand_report(
        month="2026-04",
        base_block="📊 Онлайн-спрос и продажи сайта за 2026-04\n🛒 Покупки: 100",
        top_pages=[
            OnlineDemandLandingPage(
                url="https://master-mobile.ru/catalog/top/",
                visits=500,
                visitors=300,
                purchases=12,
                click_buy=40,
                begin_checkout=20,
                phone_clicks=2,
                site_searches=10,
            )
        ],
        no_purchase_pages=[
            OnlineDemandLandingPage(
                url="https://master-mobile.ru/catalog/audit/",
                visits=350,
                visitors=250,
                purchases=0,
                click_buy=0,
                begin_checkout=0,
                phone_clicks=0,
                site_searches=15,
            )
        ],
    )

    assert "🏆 Топ посадочных страниц по покупкам:" in rendered
    assert "1. /catalog/top/ — 500 визитов, 12 покупок" in rendered
    assert "⚠️ Страницы с трафиком, но без покупок:" in rendered
    assert "1. /catalog/audit/ — 350 визитов, 0 покупок" in rendered
    assert "Данные: Яндекс Метрика, не финансовая выручка 1С." in rendered


def test_sync_monthly_online_demand_report_delivers_once(monkeypatch, tmp_path: Path) -> None:
    delivered: list[dict[str, object]] = []

    def fake_build(*, env: dict[str, str], month: str) -> str:
        assert env["YANDEX_METRIKA_TOKEN"] == "secret"
        assert month == "2026-04"
        return "месячный отчет"

    def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return {"sent_count": 2, "chat_ids": ["1", "2"]}

    monkeypatch.setattr(monthly_report, "build_monthly_online_demand_report", fake_build)

    summary = monthly_report.sync_monthly_online_demand_report(
        env={"YANDEX_METRIKA_TOKEN": "secret"},
        month="2026-04",
        state_path=tmp_path / "state.json",
        artifact_dir=tmp_path / "artifacts",
        deliver_message=fake_deliver,
    )

    assert summary["action"] == "deliver"
    assert summary["sent_messages"] == 2
    assert Path(str(summary["artifact_path"])).exists()
    assert delivered[0]["message"] == "месячный отчет"

    second = monthly_report.sync_monthly_online_demand_report(
        env={"YANDEX_METRIKA_TOKEN": "secret"},
        month="2026-04",
        state_path=tmp_path / "state.json",
        artifact_dir=tmp_path / "artifacts",
        deliver_message=fake_deliver,
    )

    assert second["action"] == "noop"


def test_build_monthly_online_demand_report_uses_month_boundaries(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_summary(**kwargs):
        calls["summary"] = kwargs
        return OnlineDemandWeeklySummary(
            week_start=date(2026, 4, 1),
            week_end=date(2026, 4, 30),
            compare_week_start=date(2026, 3, 1),
            compare_week_end=date(2026, 3, 31),
            current=OnlineDemandPeriodMetrics(
                visits=1000,
                visitors=700,
                purchases=30,
                click_buy=90,
                begin_checkout=45,
                phone_clicks=8,
                site_searches=120,
                primary_source_name="Переходы из поисковых систем",
                primary_source_visits=600,
                primary_source_purchases=20,
            ),
            previous=OnlineDemandPeriodMetrics(
                visits=900,
                visitors=650,
                purchases=25,
                click_buy=80,
                begin_checkout=35,
                phone_clicks=6,
                site_searches=100,
                primary_source_name="Переходы из поисковых систем",
                primary_source_visits=550,
                primary_source_purchases=18,
            ),
        )

    def fake_pages(**kwargs):
        calls.setdefault("pages", []).append(kwargs)
        return [
            OnlineDemandLandingPage(
                url="https://master-mobile.ru/catalog/top/",
                visits=100,
                visitors=80,
                purchases=5,
                click_buy=10,
                begin_checkout=7,
                phone_clicks=1,
                site_searches=4,
            )
        ]

    monkeypatch.setattr(monthly_report, "fetch_online_demand_weekly_summary", fake_summary)
    monkeypatch.setattr(monthly_report, "fetch_online_demand_landing_pages", fake_pages)

    rendered = monthly_report.build_monthly_online_demand_report(
        env={"YANDEX_METRIKA_TOKEN": "secret"},
        month="2026-04",
    )

    assert "📊 Онлайн-спрос и продажи сайта за 2026-04" in rendered
    assert calls["summary"]["week_start"] == date(2026, 4, 1)
    assert calls["summary"]["week_end"] == date(2026, 4, 30)
    assert calls["summary"]["compare_week_start"] == date(2026, 3, 1)
    assert calls["summary"]["compare_week_end"] == date(2026, 3, 31)
