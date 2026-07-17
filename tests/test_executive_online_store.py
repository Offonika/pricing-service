from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from app.api import management
from app.core.config import Settings
from app.main import app
from app.services.bitrix_executive_dashboard_auth import (
    ExecutiveDashboardAuthContext,
    full_executive_dashboard_context,
    require_executive_dashboard_access,
)
from app.services.executive_online_store import (
    build_executive_online_store_period_response,
)
from app.services.online_demand_metrics import (
    OnlineDemandBreakdownRow,
    OnlineDemandDailyRow,
    OnlineDemandLandingPage,
    OnlineDemandPeriodMetrics,
    OnlineStoreAnalytics,
)


def _period_metrics(*, visits: int, purchases: int) -> OnlineDemandPeriodMetrics:
    return OnlineDemandPeriodMetrics(
        visits=visits,
        visitors=visits // 2,
        purchases=purchases,
        click_buy=purchases * 4,
        begin_checkout=purchases * 2,
        phone_clicks=3,
        site_searches=12,
        primary_source_name="Переходы из поисковых систем",
        primary_source_visits=visits // 2,
        primary_source_purchases=purchases - 1,
    )


def _analytics() -> OnlineStoreAnalytics:
    return OnlineStoreAnalytics(
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 7),
        compare_date_from=date(2026, 6, 24),
        compare_date_to=date(2026, 6, 30),
        current=_period_metrics(visits=1000, purchases=25),
        previous=_period_metrics(visits=800, purchases=16),
        daily=(
            OnlineDemandDailyRow(
                business_date=date(2026, 7, 1),
                visits=120,
                visitors=80,
                purchases=4,
                click_buy=15,
                begin_checkout=8,
                phone_clicks=1,
                site_searches=10,
            ),
        ),
        traffic_sources=(
            OnlineDemandBreakdownRow(
                key="organic",
                label="Переходы из поисковых систем",
                visits=600,
                visitors=350,
                purchases=20,
                click_buy=70,
                begin_checkout=35,
                phone_clicks=6,
                site_searches=100,
            ),
        ),
        landing_pages=(
            OnlineDemandLandingPage(
                url="https://master-mobile.ru/catalog/item/",
                visits=120,
                visitors=90,
                purchases=4,
                click_buy=12,
                begin_checkout=5,
                phone_clicks=1,
                site_searches=8,
            ),
        ),
        counter_id="49993429",
    )


def test_build_online_store_period_maps_metrika_analytics(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.executive_online_store.fetch_online_store_analytics",
        lambda **_: _analytics(),
    )

    result = build_executive_online_store_period_response(
        token="hidden-token",
        counter_id="49993429",
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 7),
    )

    assert result.compare_date_from == date(2026, 6, 24)
    assert result.compare_date_to == date(2026, 6, 30)
    assert result.totals["visits"] == 1000
    assert result.totals["purchase_conversion_pct"] == Decimal("2.50")
    assert result.comparison["purchases"] == 16
    assert result.traffic_sources[0].purchases == 20
    assert result.landing_pages[0].url.endswith("/catalog/item/")
    assert "не являются финансовой выручкой 1С" in (result.note or "")


def test_online_store_period_api_uses_configured_read_only_source(
    client: TestClient,
    monkeypatch,
) -> None:
    settings = Settings(
        management_internal_api_token="secret-token",
        yandex_metrika_token="hidden-token",
        yandex_metrika_counter_id="49993429",
        yandex_metrika_timeout_seconds=7,
    )
    captured: dict[str, object] = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        monkeypatch.setattr(
            "app.services.executive_online_store.fetch_online_store_analytics",
            lambda **_: _analytics(),
        )
        return build_executive_online_store_period_response(**kwargs)

    monkeypatch.setattr(management, "get_settings", lambda: settings)
    monkeypatch.setattr(management, "build_executive_online_store_period_response", fake_build)
    app.dependency_overrides[require_executive_dashboard_access] = full_executive_dashboard_context
    try:
        response = client.get(
            "/api/management/executive-dashboard/online-store-period"
            "?date_from=2026-07-01&date_to=2026-07-07"
        )
    finally:
        app.dependency_overrides.pop(require_executive_dashboard_access, None)

    assert response.status_code == 200
    assert response.json()["totals"]["purchases"] == 25
    assert captured["token"] == "hidden-token"
    assert captured["timeout"] == 7


def test_online_store_period_api_forbids_unrelated_domain_role(
    client: TestClient,
) -> None:
    access = ExecutiveDashboardAuthContext(
        actor="bitrix:202",
        source="bitrix",
        access_level="domain",
        roles=("receivables",),
        allowed_blocks=("debtors", "receivables_control"),
    )
    app.dependency_overrides[require_executive_dashboard_access] = lambda: access
    try:
        response = client.get(
            "/api/management/executive-dashboard/online-store-period"
            "?date_from=2026-07-01&date_to=2026-07-07"
        )
    finally:
        app.dependency_overrides.pop(require_executive_dashboard_access, None)

    assert response.status_code == 403
