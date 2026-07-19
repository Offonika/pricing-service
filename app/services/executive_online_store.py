from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.schemas.executive_dashboard import (
    ExecutiveOnlineStoreDailyRow,
    ExecutiveOnlineStoreLandingPageRow,
    ExecutiveOnlineStorePeriodResponse,
    ExecutiveOnlineStoreTrafficSourceRow,
)
from app.services.online_demand_metrics import (
    OnlineDemandBreakdownRow,
    OnlineDemandPeriodMetrics,
    fetch_online_store_analytics,
)


def _period_totals(
    metrics: OnlineDemandPeriodMetrics,
    *,
    primary_source: OnlineDemandBreakdownRow | None = None,
) -> dict[str, Decimal | int | str]:
    source_name = primary_source.label if primary_source else metrics.primary_source_name
    source_visits = primary_source.visits if primary_source else metrics.primary_source_visits
    source_purchases = (
        primary_source.purchases if primary_source else metrics.primary_source_purchases
    )
    source_share = (
        Decimal("0")
        if metrics.purchases <= 0
        else (Decimal(source_purchases) * Decimal("100") / Decimal(metrics.purchases)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )
    return {
        "visits": metrics.visits,
        "visitors": metrics.visitors,
        "purchases": metrics.purchases,
        "purchase_conversion_pct": metrics.purchase_conversion_pct,
        "click_buy": metrics.click_buy,
        "begin_checkout": metrics.begin_checkout,
        "phone_clicks": metrics.phone_clicks,
        "site_searches": metrics.site_searches,
        "primary_source_name": source_name,
        "primary_source_visits": source_visits,
        "primary_source_purchases": source_purchases,
        "primary_source_purchase_share_pct": source_share,
    }


def build_executive_online_store_period_response(
    *,
    token: str,
    counter_id: str,
    date_from: date,
    date_to: date,
    timeout: float = 20.0,
) -> ExecutiveOnlineStorePeriodResponse:
    period_days = (date_to - date_from).days + 1
    compare_date_to = date_from - timedelta(days=1)
    compare_date_from = compare_date_to - timedelta(days=period_days - 1)
    analytics = fetch_online_store_analytics(
        token=token,
        counter_id=counter_id,
        date_from=date_from,
        date_to=date_to,
        compare_date_from=compare_date_from,
        compare_date_to=compare_date_to,
        timeout=timeout,
    )
    primary_source = max(
        analytics.traffic_sources,
        key=lambda row: (row.purchases, row.visits),
        default=None,
    )
    return ExecutiveOnlineStorePeriodResponse(
        date_from=analytics.date_from,
        date_to=analytics.date_to,
        compare_date_from=analytics.compare_date_from,
        compare_date_to=analytics.compare_date_to,
        generated_at=datetime.now(UTC),
        counter_id=analytics.counter_id,
        note=(
            "Яндекс Метрика показывает онлайн-спрос и цели сайта; "
            "покупки и цели не являются финансовой выручкой 1С."
        ),
        totals=_period_totals(analytics.current, primary_source=primary_source),
        comparison=_period_totals(analytics.previous),
        daily=[
            ExecutiveOnlineStoreDailyRow(
                business_date=row.business_date,
                visits=row.visits,
                visitors=row.visitors,
                purchases=row.purchases,
                click_buy=row.click_buy,
                begin_checkout=row.begin_checkout,
                phone_clicks=row.phone_clicks,
                site_searches=row.site_searches,
                purchase_conversion_pct=row.purchase_conversion_pct,
            )
            for row in analytics.daily
        ],
        traffic_sources=[
            ExecutiveOnlineStoreTrafficSourceRow(
                key=row.key,
                label=row.label,
                visits=row.visits,
                visitors=row.visitors,
                purchases=row.purchases,
                purchase_conversion_pct=row.purchase_conversion_pct,
            )
            for row in analytics.traffic_sources
        ],
        landing_pages=[
            ExecutiveOnlineStoreLandingPageRow(
                url=row.url,
                visits=row.visits,
                visitors=row.visitors,
                purchases=row.purchases,
                click_buy=row.click_buy,
                begin_checkout=row.begin_checkout,
                purchase_conversion_pct=row.purchase_conversion_pct,
            )
            for row in analytics.landing_pages
        ],
    )
