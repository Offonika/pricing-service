from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.online_demand_metrics import (
    OnlineDemandWeeklySummary,
    _landing_pages_from_payload,
    _period_metrics_from_payload,
    render_online_demand_block,
)


def test_period_metrics_from_payload_uses_search_as_primary_source() -> None:
    payload = {
        "totals": [1000, 700, 25, 80, 40, 7, 120],
        "data": [
            {
                "dimensions": [{"name": "Прямые заходы"}],
                "metrics": [300, 250, 2, 5, 1, 0, 10],
            },
            {
                "dimensions": [{"name": "Переходы из поисковых систем"}],
                "metrics": [600, 350, 20, 70, 35, 6, 100],
            },
        ],
    }

    metrics = _period_metrics_from_payload(payload)

    assert metrics.visits == 1000
    assert metrics.purchases == 25
    assert metrics.click_buy == 80
    assert metrics.purchase_conversion_pct == Decimal("2.50")
    assert metrics.primary_source_name == "Переходы из поисковых систем"
    assert metrics.primary_source_purchases == 20
    assert metrics.primary_source_purchase_share_pct == Decimal("80.00")


def test_render_online_demand_block_includes_management_kpis() -> None:
    summary = OnlineDemandWeeklySummary(
        week_start=date(2026, 5, 3),
        week_end=date(2026, 5, 9),
        compare_week_start=date(2026, 4, 26),
        compare_week_end=date(2026, 5, 2),
        current=_period_metrics_from_payload(
            {
                "totals": [65278, 38713, 912, 2175, 1410, 176, 10775],
                "data": [
                    {
                        "dimensions": [{"name": "Переходы из поисковых систем"}],
                        "metrics": [39659, 19153, 705, 1699, 1103, 129, 8377],
                    }
                ],
            }
        ),
        previous=_period_metrics_from_payload(
            {
                "totals": [63905, 36843, 871, 2143, 1382, 154, 10808],
                "data": [
                    {
                        "dimensions": [{"name": "Переходы из поисковых систем"}],
                        "metrics": [41852, 20747, 687, 1768, 1119, 119, 8462],
                    }
                ],
            }
        ),
    )

    rendered = render_online_demand_block(summary)

    assert "📊 Онлайн-спрос и конверсия" in rendered
    assert "🌐 Визиты: 65 278 | +2,15%" in rendered
    assert "🛒 Покупки: 912 | +4,71%" in rendered
    assert "📈 Конверсия в покупку: 1,40%" in rendered
    assert "Данные: Яндекс Метрика, не финансовая выручка 1С." in rendered


def test_landing_pages_from_payload_maps_goal_metrics() -> None:
    pages = _landing_pages_from_payload(
        {
            "data": [
                {
                    "dimensions": [{"name": "https://master-mobile.ru/catalog/item/"}],
                    "metrics": [120, 90, 4, 12, 5, 1, 8],
                }
            ]
        }
    )

    assert len(pages) == 1
    assert pages[0].url == "https://master-mobile.ru/catalog/item/"
    assert pages[0].visits == 120
    assert pages[0].purchases == 4
    assert pages[0].click_buy == 12
    assert pages[0].purchase_conversion_pct == Decimal("3.33")
