import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

DEFAULT_METRIKA_COUNTER_ID = "49993429"
DEFAULT_METRIKA_BASE_URL = "https://api-metrika.yandex.net"
DEFAULT_METRIKA_GOAL_NAMES = (
    "Ecommerce: покупка",
    "Клик по кнопке Купить",
    "Автоцель: Начало оформления заказа",
    "Автоцель: клик по номеру телефона",
    "Автоцель: поиск по сайту",
)
TRAFFIC_SOURCE_DIMENSION = "ym:s:lastsignTrafficSource"
SEARCH_TRAFFIC_SOURCE_LABEL = "Переходы из поисковых систем"
PERCENT_QUANT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class OnlineDemandPeriodMetrics:
    visits: int
    visitors: int
    purchases: int
    click_buy: int
    begin_checkout: int
    phone_clicks: int
    site_searches: int
    primary_source_name: str
    primary_source_visits: int
    primary_source_purchases: int

    @property
    def purchase_conversion_pct(self) -> Decimal:
        if self.visits <= 0:
            return Decimal("0")
        return _quantize_pct(Decimal(self.purchases) * Decimal("100") / Decimal(self.visits))

    @property
    def primary_source_purchase_share_pct(self) -> Decimal:
        if self.purchases <= 0:
            return Decimal("0")
        return _quantize_pct(
            Decimal(self.primary_source_purchases) * Decimal("100") / Decimal(self.purchases)
        )


@dataclass(frozen=True, slots=True)
class OnlineDemandWeeklySummary:
    week_start: date
    week_end: date
    compare_week_start: date
    compare_week_end: date
    current: OnlineDemandPeriodMetrics
    previous: OnlineDemandPeriodMetrics
    counter_id: str = DEFAULT_METRIKA_COUNTER_ID


@dataclass(frozen=True, slots=True)
class OnlineDemandLandingPage:
    url: str
    visits: int
    visitors: int
    purchases: int
    click_buy: int
    begin_checkout: int
    phone_clicks: int
    site_searches: int

    @property
    def purchase_conversion_pct(self) -> Decimal:
        if self.visits <= 0:
            return Decimal("0")
        return _quantize_pct(Decimal(self.purchases) * Decimal("100") / Decimal(self.visits))


@dataclass(frozen=True, slots=True)
class OnlineDemandBreakdownRow:
    key: str
    label: str
    visits: int
    visitors: int
    purchases: int
    click_buy: int
    begin_checkout: int
    phone_clicks: int
    site_searches: int

    @property
    def purchase_conversion_pct(self) -> Decimal:
        if self.visits <= 0:
            return Decimal("0")
        return _quantize_pct(Decimal(self.purchases) * Decimal("100") / Decimal(self.visits))


@dataclass(frozen=True, slots=True)
class OnlineDemandDailyRow:
    business_date: date
    visits: int
    visitors: int
    purchases: int
    click_buy: int
    begin_checkout: int
    phone_clicks: int
    site_searches: int

    @property
    def purchase_conversion_pct(self) -> Decimal:
        if self.visits <= 0:
            return Decimal("0")
        return _quantize_pct(Decimal(self.purchases) * Decimal("100") / Decimal(self.visits))


@dataclass(frozen=True, slots=True)
class OnlineStoreAnalytics:
    date_from: date
    date_to: date
    compare_date_from: date
    compare_date_to: date
    current: OnlineDemandPeriodMetrics
    previous: OnlineDemandPeriodMetrics
    daily: tuple[OnlineDemandDailyRow, ...]
    traffic_sources: tuple[OnlineDemandBreakdownRow, ...]
    landing_pages: tuple[OnlineDemandLandingPage, ...]
    counter_id: str = DEFAULT_METRIKA_COUNTER_ID


def _quantize_pct(value: Decimal) -> Decimal:
    return value.quantize(PERCENT_QUANT, rounding=ROUND_HALF_UP)


def _safe_int(value: Any) -> int:
    try:
        return int(Decimal(str(value or 0)).to_integral_value(rounding=ROUND_HALF_UP))
    except Exception:
        return 0


def _delta_pct(current: int, previous: int) -> Decimal | None:
    if previous == 0:
        return None
    return _quantize_pct(
        (Decimal(current) - Decimal(previous)) * Decimal("100") / Decimal(previous)
    )


def _format_int(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _format_pct(value: Decimal) -> str:
    rendered = f"{_quantize_pct(value):,.2f}".replace(",", " ").replace(".", ",")
    return f"{rendered}%"


def _format_delta(current: int, previous: int) -> str:
    delta = _delta_pct(current, previous)
    if delta is None:
        if current == 0:
            return "0,0%"
        return "новый"
    sign = "+" if delta > 0 else ""
    return f"{sign}{_format_pct(delta)}"


def _request_json(
    url: str,
    *,
    token: str,
    timeout: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"OAuth {token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read().decode("utf-8")
            payload = json.loads(body)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Metrika API HTTP {error.code}: {body[:300]}") from error
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise RuntimeError(f"Metrika API недоступна: {error}") from error

    if payload.get("errors"):
        raise RuntimeError(
            f"Metrika API error: {json.dumps(payload['errors'], ensure_ascii=False)}"
        )
    return payload


def _goal_ids_by_name(
    *,
    token: str,
    counter_id: str,
    goal_names: tuple[str, ...],
    base_url: str,
    timeout: float,
) -> dict[str, int]:
    url = f"{base_url.rstrip('/')}/management/v1/counter/{counter_id}/goals"
    payload = _request_json(url, token=token, timeout=timeout)
    wanted = set(goal_names)
    result: dict[str, int] = {}
    for goal in payload.get("goals") or []:
        name = str(goal.get("name") or "")
        goal_id = goal.get("id")
        if name in wanted and goal_id:
            result[name] = int(goal_id)
    missing = sorted(wanted.difference(result))
    if missing:
        raise RuntimeError(f"В Метрике не найдены цели: {', '.join(missing)}")
    return result


def _metric_names(goal_ids: dict[str, int]) -> tuple[str, ...]:
    return (
        "ym:s:visits",
        "ym:s:users",
        f"ym:s:goal{goal_ids.get('Ecommerce: покупка', 0)}visits",
        f"ym:s:goal{goal_ids.get('Клик по кнопке Купить', 0)}visits",
        f"ym:s:goal{goal_ids.get('Автоцель: Начало оформления заказа', 0)}visits",
        f"ym:s:goal{goal_ids.get('Автоцель: клик по номеру телефона', 0)}visits",
        f"ym:s:goal{goal_ids.get('Автоцель: поиск по сайту', 0)}visits",
    )


def _build_report_url(
    *,
    counter_id: str,
    date_from: date,
    date_to: date,
    goal_ids: dict[str, int],
    base_url: str,
) -> str:
    params = {
        "ids": counter_id,
        "date1": date_from.isoformat(),
        "date2": date_to.isoformat(),
        "dimensions": TRAFFIC_SOURCE_DIMENSION,
        "metrics": ",".join(_metric_names(goal_ids)),
        "sort": "-ym:s:visits",
        "limit": "20",
        "accuracy": "full",
        "lang": "ru",
    }
    return f"{base_url.rstrip('/')}/stat/v1/data?{urllib.parse.urlencode(params)}"


def _landing_pages_report_url(
    *,
    counter_id: str,
    date_from: date,
    date_to: date,
    goal_ids: dict[str, int],
    base_url: str,
    sort: str,
    limit: int,
) -> str:
    params = {
        "ids": counter_id,
        "date1": date_from.isoformat(),
        "date2": date_to.isoformat(),
        "dimensions": "ym:s:startURL",
        "metrics": ",".join(_metric_names(goal_ids)),
        "sort": sort,
        "limit": str(limit),
        "accuracy": "full",
        "lang": "ru",
    }
    return f"{base_url.rstrip('/')}/stat/v1/data?{urllib.parse.urlencode(params)}"


def _dimension_report_url(
    *,
    counter_id: str,
    date_from: date,
    date_to: date,
    goal_ids: dict[str, int],
    dimension: str,
    sort: str,
    limit: int,
    base_url: str,
) -> str:
    params = {
        "ids": counter_id,
        "date1": date_from.isoformat(),
        "date2": date_to.isoformat(),
        "dimensions": dimension,
        "metrics": ",".join(_metric_names(goal_ids)),
        "sort": sort,
        "limit": str(limit),
        "accuracy": "full",
        "lang": "ru",
    }
    return f"{base_url.rstrip('/')}/stat/v1/data?{urllib.parse.urlencode(params)}"


def _period_metrics_from_payload(payload: dict[str, Any]) -> OnlineDemandPeriodMetrics:
    totals = payload.get("totals") or []
    visits = _safe_int(totals[0] if len(totals) > 0 else 0)
    visitors = _safe_int(totals[1] if len(totals) > 1 else 0)
    purchases = _safe_int(totals[2] if len(totals) > 2 else 0)
    click_buy = _safe_int(totals[3] if len(totals) > 3 else 0)
    begin_checkout = _safe_int(totals[4] if len(totals) > 4 else 0)
    phone_clicks = _safe_int(totals[5] if len(totals) > 5 else 0)
    site_searches = _safe_int(totals[6] if len(totals) > 6 else 0)

    primary_source_name = ""
    primary_source_visits = 0
    primary_source_purchases = 0
    best_source_purchases = -1
    for row in payload.get("data") or []:
        dimensions = row.get("dimensions") or []
        metrics = row.get("metrics") or []
        name = str((dimensions[0] if dimensions else {}).get("name") or "")
        row_visits = _safe_int(metrics[0] if len(metrics) > 0 else 0)
        row_purchases = _safe_int(metrics[2] if len(metrics) > 2 else 0)
        if name == SEARCH_TRAFFIC_SOURCE_LABEL:
            primary_source_name = name
            primary_source_visits = row_visits
            primary_source_purchases = row_purchases
            break
        if row_purchases > best_source_purchases:
            best_source_purchases = row_purchases
            primary_source_name = name
            primary_source_visits = row_visits
            primary_source_purchases = row_purchases

    return OnlineDemandPeriodMetrics(
        visits=visits,
        visitors=visitors,
        purchases=purchases,
        click_buy=click_buy,
        begin_checkout=begin_checkout,
        phone_clicks=phone_clicks,
        site_searches=site_searches,
        primary_source_name=primary_source_name or "Не определено",
        primary_source_visits=primary_source_visits,
        primary_source_purchases=primary_source_purchases,
    )


def _landing_pages_from_payload(payload: dict[str, Any]) -> list[OnlineDemandLandingPage]:
    pages: list[OnlineDemandLandingPage] = []
    for row in payload.get("data") or []:
        dimensions = row.get("dimensions") or []
        metrics = row.get("metrics") or []
        url = str((dimensions[0] if dimensions else {}).get("name") or "")
        pages.append(
            OnlineDemandLandingPage(
                url=url,
                visits=_safe_int(metrics[0] if len(metrics) > 0 else 0),
                visitors=_safe_int(metrics[1] if len(metrics) > 1 else 0),
                purchases=_safe_int(metrics[2] if len(metrics) > 2 else 0),
                click_buy=_safe_int(metrics[3] if len(metrics) > 3 else 0),
                begin_checkout=_safe_int(metrics[4] if len(metrics) > 4 else 0),
                phone_clicks=_safe_int(metrics[5] if len(metrics) > 5 else 0),
                site_searches=_safe_int(metrics[6] if len(metrics) > 6 else 0),
            )
        )
    return pages


def _breakdown_rows_from_payload(payload: dict[str, Any]) -> list[OnlineDemandBreakdownRow]:
    rows: list[OnlineDemandBreakdownRow] = []
    for row in payload.get("data") or []:
        dimensions = row.get("dimensions") or []
        metrics = row.get("metrics") or []
        dimension = dimensions[0] if dimensions else {}
        label = str(dimension.get("name") or "Не определено")
        rows.append(
            OnlineDemandBreakdownRow(
                key=str(dimension.get("id") or label),
                label=label,
                visits=_safe_int(metrics[0] if len(metrics) > 0 else 0),
                visitors=_safe_int(metrics[1] if len(metrics) > 1 else 0),
                purchases=_safe_int(metrics[2] if len(metrics) > 2 else 0),
                click_buy=_safe_int(metrics[3] if len(metrics) > 3 else 0),
                begin_checkout=_safe_int(metrics[4] if len(metrics) > 4 else 0),
                phone_clicks=_safe_int(metrics[5] if len(metrics) > 5 else 0),
                site_searches=_safe_int(metrics[6] if len(metrics) > 6 else 0),
            )
        )
    return rows


def _daily_rows_from_payload(payload: dict[str, Any]) -> list[OnlineDemandDailyRow]:
    rows: list[OnlineDemandDailyRow] = []
    for row in payload.get("data") or []:
        dimensions = row.get("dimensions") or []
        metrics = row.get("metrics") or []
        raw_date = str((dimensions[0] if dimensions else {}).get("name") or "")
        try:
            business_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        rows.append(
            OnlineDemandDailyRow(
                business_date=business_date,
                visits=_safe_int(metrics[0] if len(metrics) > 0 else 0),
                visitors=_safe_int(metrics[1] if len(metrics) > 1 else 0),
                purchases=_safe_int(metrics[2] if len(metrics) > 2 else 0),
                click_buy=_safe_int(metrics[3] if len(metrics) > 3 else 0),
                begin_checkout=_safe_int(metrics[4] if len(metrics) > 4 else 0),
                phone_clicks=_safe_int(metrics[5] if len(metrics) > 5 else 0),
                site_searches=_safe_int(metrics[6] if len(metrics) > 6 else 0),
            )
        )
    rows.sort(key=lambda item: item.business_date)
    return rows


def fetch_online_demand_weekly_summary(
    *,
    token: str,
    counter_id: str,
    week_start: date,
    week_end: date,
    compare_week_start: date,
    compare_week_end: date,
    base_url: str = DEFAULT_METRIKA_BASE_URL,
    timeout: float = 20.0,
    goal_names: tuple[str, ...] = DEFAULT_METRIKA_GOAL_NAMES,
) -> OnlineDemandWeeklySummary:
    goal_ids = _goal_ids_by_name(
        token=token,
        counter_id=counter_id,
        goal_names=goal_names,
        base_url=base_url,
        timeout=timeout,
    )
    current_payload = _request_json(
        _build_report_url(
            counter_id=counter_id,
            date_from=week_start,
            date_to=week_end,
            goal_ids=goal_ids,
            base_url=base_url,
        ),
        token=token,
        timeout=timeout,
    )
    previous_payload = _request_json(
        _build_report_url(
            counter_id=counter_id,
            date_from=compare_week_start,
            date_to=compare_week_end,
            goal_ids=goal_ids,
            base_url=base_url,
        ),
        token=token,
        timeout=timeout,
    )
    return OnlineDemandWeeklySummary(
        week_start=week_start,
        week_end=week_end,
        compare_week_start=compare_week_start,
        compare_week_end=compare_week_end,
        current=_period_metrics_from_payload(current_payload),
        previous=_period_metrics_from_payload(previous_payload),
        counter_id=counter_id,
    )


def fetch_online_demand_landing_pages(
    *,
    token: str,
    counter_id: str,
    date_from: date,
    date_to: date,
    sort: str,
    limit: int = 20,
    base_url: str = DEFAULT_METRIKA_BASE_URL,
    timeout: float = 20.0,
    goal_names: tuple[str, ...] = DEFAULT_METRIKA_GOAL_NAMES,
) -> list[OnlineDemandLandingPage]:
    goal_ids = _goal_ids_by_name(
        token=token,
        counter_id=counter_id,
        goal_names=goal_names,
        base_url=base_url,
        timeout=timeout,
    )
    resolved_sort = sort
    if sort == "-purchases":
        resolved_sort = f"-ym:s:goal{goal_ids['Ecommerce: покупка']}visits"
    payload = _request_json(
        _landing_pages_report_url(
            counter_id=counter_id,
            date_from=date_from,
            date_to=date_to,
            goal_ids=goal_ids,
            base_url=base_url,
            sort=resolved_sort,
            limit=limit,
        ),
        token=token,
        timeout=timeout,
    )
    return _landing_pages_from_payload(payload)


def fetch_online_store_analytics(
    *,
    token: str,
    counter_id: str,
    date_from: date,
    date_to: date,
    compare_date_from: date,
    compare_date_to: date,
    landing_page_limit: int = 15,
    traffic_source_limit: int = 20,
    base_url: str = DEFAULT_METRIKA_BASE_URL,
    timeout: float = 20.0,
    goal_names: tuple[str, ...] = DEFAULT_METRIKA_GOAL_NAMES,
) -> OnlineStoreAnalytics:
    goal_ids = _goal_ids_by_name(
        token=token,
        counter_id=counter_id,
        goal_names=goal_names,
        base_url=base_url,
        timeout=timeout,
    )
    current_payload = _request_json(
        _dimension_report_url(
            counter_id=counter_id,
            date_from=date_from,
            date_to=date_to,
            goal_ids=goal_ids,
            dimension=TRAFFIC_SOURCE_DIMENSION,
            sort="-ym:s:visits",
            limit=traffic_source_limit,
            base_url=base_url,
        ),
        token=token,
        timeout=timeout,
    )
    previous_payload = _request_json(
        _dimension_report_url(
            counter_id=counter_id,
            date_from=compare_date_from,
            date_to=compare_date_to,
            goal_ids=goal_ids,
            dimension=TRAFFIC_SOURCE_DIMENSION,
            sort="-ym:s:visits",
            limit=traffic_source_limit,
            base_url=base_url,
        ),
        token=token,
        timeout=timeout,
    )
    daily_payload = _request_json(
        _dimension_report_url(
            counter_id=counter_id,
            date_from=date_from,
            date_to=date_to,
            goal_ids=goal_ids,
            dimension="ym:s:date",
            sort="ym:s:date",
            limit=366,
            base_url=base_url,
        ),
        token=token,
        timeout=timeout,
    )
    landing_pages_payload = _request_json(
        _landing_pages_report_url(
            counter_id=counter_id,
            date_from=date_from,
            date_to=date_to,
            goal_ids=goal_ids,
            base_url=base_url,
            sort="-ym:s:visits",
            limit=landing_page_limit,
        ),
        token=token,
        timeout=timeout,
    )
    return OnlineStoreAnalytics(
        date_from=date_from,
        date_to=date_to,
        compare_date_from=compare_date_from,
        compare_date_to=compare_date_to,
        current=_period_metrics_from_payload(current_payload),
        previous=_period_metrics_from_payload(previous_payload),
        daily=tuple(_daily_rows_from_payload(daily_payload)),
        traffic_sources=tuple(_breakdown_rows_from_payload(current_payload)),
        landing_pages=tuple(_landing_pages_from_payload(landing_pages_payload)),
        counter_id=counter_id,
    )


def online_demand_rows(summary: OnlineDemandWeeklySummary) -> list[list[Any]]:
    current = summary.current
    previous = summary.previous
    return [
        [
            "Визиты на сайт",
            current.visits,
            previous.visits,
            _format_delta(current.visits, previous.visits),
        ],
        [
            "Посетители",
            current.visitors,
            previous.visitors,
            _format_delta(current.visitors, previous.visitors),
        ],
        [
            "Покупки на сайте",
            current.purchases,
            previous.purchases,
            _format_delta(current.purchases, previous.purchases),
        ],
        [
            "Конверсия в покупку",
            float(current.purchase_conversion_pct) / 100,
            float(previous.purchase_conversion_pct) / 100,
            "",
        ],
        [
            "Клики Купить",
            current.click_buy,
            previous.click_buy,
            _format_delta(current.click_buy, previous.click_buy),
        ],
        [
            "Начали оформление заказа",
            current.begin_checkout,
            previous.begin_checkout,
            _format_delta(current.begin_checkout, previous.begin_checkout),
        ],
        [
            "Клики по телефону",
            current.phone_clicks,
            previous.phone_clicks,
            _format_delta(current.phone_clicks, previous.phone_clicks),
        ],
        [
            "Поиск по сайту",
            current.site_searches,
            previous.site_searches,
            _format_delta(current.site_searches, previous.site_searches),
        ],
        [
            "Основной источник продаж",
            current.primary_source_name,
            previous.primary_source_name,
            "",
        ],
        [
            "Покупки основного источника",
            current.primary_source_purchases,
            previous.primary_source_purchases,
            _format_delta(current.primary_source_purchases, previous.primary_source_purchases),
        ],
    ]


def render_online_demand_block(summary: OnlineDemandWeeklySummary) -> str:
    current = summary.current
    previous = summary.previous
    source_name = current.primary_source_name.replace("Переходы из ", "").lower()
    return "\n".join(
        [
            "📊 Онлайн-спрос и конверсия",
            (
                f"Период: {summary.week_start.strftime('%d.%m')}-"
                f"{summary.week_end.strftime('%d.%m.%Y')}"
            ),
            f"🌐 Визиты: {_format_int(current.visits)} | {_format_delta(current.visits, previous.visits)}",
            (
                f"👥 Посетители: {_format_int(current.visitors)} | "
                f"{_format_delta(current.visitors, previous.visitors)}"
            ),
            (
                f"🛒 Покупки: {_format_int(current.purchases)} | "
                f"{_format_delta(current.purchases, previous.purchases)}"
            ),
            f"📈 Конверсия в покупку: {_format_pct(current.purchase_conversion_pct)}",
            (
                f"👆 Клики Купить: {_format_int(current.click_buy)} | "
                f"{_format_delta(current.click_buy, previous.click_buy)}"
            ),
            (
                f"🧾 Начали оформление: {_format_int(current.begin_checkout)} | "
                f"{_format_delta(current.begin_checkout, previous.begin_checkout)}"
            ),
            (
                f"📞 Клики по телефону: {_format_int(current.phone_clicks)} | "
                f"{_format_delta(current.phone_clicks, previous.phone_clicks)}"
            ),
            (
                f"🔎 Поиск по сайту: {_format_int(current.site_searches)} | "
                f"{_format_delta(current.site_searches, previous.site_searches)}"
            ),
            (
                f"🔍 Основной источник: {source_name} — "
                f"{_format_int(current.primary_source_visits)} визитов, "
                f"{_format_int(current.primary_source_purchases)} покупок "
                f"({_format_pct(current.primary_source_purchase_share_pct)} покупок сайта)."
            ),
            (
                "📌 Вывод: онлайн-спрос "
                f"{'растёт' if current.purchases >= previous.purchases else 'просел'}, "
                "главный канал для усиления рекламы — страницы, уже конвертирующие из поиска."
            ),
            "Данные: Яндекс Метрика, не финансовая выручка 1С.",
        ]
    )
