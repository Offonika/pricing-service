from datetime import date
from decimal import Decimal

from tasks.report_display_auto_order_backtest import build_backtest_rows

AS_OF = date(2026, 6, 10)


def _row(code: str, **extra: str) -> dict[str, str]:
    base = {
        "nomenclature_code": code,
        "name": f"Дисплей {code}",
        "status_label": "Растим",
        "speed_tier": "normal",
        "dry_run_decision": "order",
        "avg_daily_sales_qty": "0",
        "base_avg_daily_sales_qty": "0",
        "days_in_sale_long": "",
        "latest_purchase_price": "1000",
    }
    base.update(extra)
    return base


def test_over_forecast_detected_with_money() -> None:
    # Предсказано 0.5 шт/день -> 30 шт за 60 дней; фактически продано 5 шт
    # при полном наличии. Разрыв 25 шт >= порога 5, отношение 6x >= 2x.
    rows, summary = build_backtest_rows(
        [_row("RB-OVER", avg_daily_sales_qty="0.5", base_avg_daily_sales_qty="0.5")],
        {"RB-OVER": {"sales_qty_window": Decimal("5")}},
        {"RB-OVER": {60: Decimal("60")}},
        horizon_days=60,
        as_of_past=AS_OF,
        observable_cap_days=0,
    )

    assert rows[0]["verdict"] == "over_forecast"
    assert summary.verdict_over == 1
    assert summary.over_order_qty_total == Decimal("25")
    assert summary.over_order_rub_total == Decimal("25000")


def test_under_forecast_and_lost_sales_on_starved_card() -> None:
    # Формула сказала "не заказывать", товар был на полке 20 дней из 60 и всё
    # равно продал 10 шт. Мягкая фактическая скорость: base=10/60,
    # virtual=40*base, qty_soft=(10+6.67)=16.7 -> недозаказ. Упущенные продажи:
    # скорость в дни наличия 0.5 * 40 дней отсутствия = 20 шт по 1000 руб.
    rows, summary = build_backtest_rows(
        [_row("RB-LOST", dry_run_decision="do_not_order")],
        {"RB-LOST": {"sales_qty_window": Decimal("10")}},
        {"RB-LOST": {60: Decimal("20")}},
        horizon_days=60,
        as_of_past=AS_OF,
        observable_cap_days=0,
    )

    assert rows[0]["verdict"] == "under_forecast"
    assert rows[0]["lost_sales_qty"] == "20.0"
    assert summary.lost_sales_rub_total == Decimal("20000")


def test_coverage_cap_switches_prediction_to_calendar_base() -> None:
    # Карточка была на полке всю наблюдаемую историю витрины (100 дней при
    # колпаке 100): поправка прошлой даты перегрета обрезанным окном, поэтому
    # предсказанием берётся календарная скорость, а не мягкая.
    rows, summary = build_backtest_rows(
        [
            _row(
                "RB-CAP",
                avg_daily_sales_qty="0.30",
                base_avg_daily_sales_qty="0.20",
                days_in_sale_long="100",
            )
        ],
        {"RB-CAP": {"sales_qty_window": Decimal("12")}},
        {"RB-CAP": {60: Decimal("60")}},
        horizon_days=60,
        as_of_past=AS_OF,
        observable_cap_days=100,
    )

    assert rows[0]["predicted_rate_source"] == "base_coverage_cap"
    assert rows[0]["predicted_rate"] == "0.2000"
    assert summary.cards_capped_by_coverage == 1
    assert rows[0]["verdict"] == "ok"


def test_short_actual_availability_uses_calendar_for_actual_rate() -> None:
    # Фактических дней наличия за горизонт меньше 15 - тот же предохранитель,
    # что в расчёте: фактическую скорость не дорисовываем, берём календарную.
    rows, _summary = build_backtest_rows(
        [_row("RB-SHORT", avg_daily_sales_qty="0.1", base_avg_daily_sales_qty="0.1")],
        {"RB-SHORT": {"sales_qty_window": Decimal("6")}},
        {"RB-SHORT": {60: Decimal("10")}},
        horizon_days=60,
        as_of_past=AS_OF,
        observable_cap_days=0,
    )

    assert rows[0]["actual_rate_soft"] == "0.1000"
    assert rows[0]["actual_qty_soft"] == "6.0"


def test_no_signal_rows_do_not_enter_score() -> None:
    rows, summary = build_backtest_rows(
        [_row("RB-EMPTY")],
        {},
        {},
        horizon_days=60,
        as_of_past=AS_OF,
        observable_cap_days=0,
    )

    assert rows[0]["verdict"] == "no_signal"
    assert summary.cards_scored == 0
    assert summary.verdict_no_signal == 1
