#!/usr/bin/env python3
"""Read management snapshots and render a compact digest for Openclaw."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

DEFAULT_LOCAL_SOURCE_URL = "http://127.0.0.1:18080"
DEFAULT_LOCAL_ENV_FILE = "/opt/MM/pricing-service/.env"
DETAIL_RECEIVABLE_ROLE_CODES = {"cfo", "finance"}
FINANCE_CONTROL_ROLE_CODES = {"cfo", "finance"}
EXCHANGE_COUNTERPARTY_CODE = "РБ002085"
RETAIL_DIRECTOR_MONTHLY_ROLE_CODES = {
    "ceo",
    "cco",
    "coo",
    "cfo",
    "development_director",
    "retail_director",
    "retail_network_head",
}
OPTIONAL_FRESHNESS_COMPONENTS = {"staffing_daily"}


def _load_env(path: str | None) -> dict[str, str]:
    env = os.environ.copy()
    if not path:
        return env
    env_path = Path(path)
    if not env_path.exists():
        return env
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pull management snapshots for Openclaw morning reports."
    )
    parser.add_argument("--date", dest="anchor_date", help="Anchor date in YYYY-MM-DD format")
    parser.add_argument("--role-code", default="", help="Optional role code for role-aware hints")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of compact text")
    return parser.parse_args()


def _http_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int,
) -> Any:
    request = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        if not body:
            return {}
        return json.loads(body)


def _build_fetcher(
    *,
    source_url: str,
    token: str,
    timeout: int,
    retries: int,
    retry_delay: float,
) -> Callable[[str, dict[str, str]], Any]:
    base = source_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}

    def _fetch(path: str, params: dict[str, str]) -> Any:
        query = urllib.parse.urlencode(params)
        url = f"{base}{path}"
        if query:
            url = f"{url}?{query}"

        attempts = max(1, retries + 1)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return _http_json(url, headers=headers, timeout=timeout)
            except (
                urllib.error.HTTPError,
                urllib.error.URLError,
                TimeoutError,
                ValueError,
            ) as error:
                last_error = error
                if attempt + 1 >= attempts:
                    break
                time.sleep(retry_delay)
        assert last_error is not None
        raise last_error

    return _fetch


def _to_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        normalized = value.replace(" ", "").replace(",", ".")
        try:
            return float(normalized)
        except ValueError:
            return 0.0
    return 0.0


def _format_money(value: float) -> str:
    return f"{value:,.0f} ₽".replace(",", " ")


def _format_rub_precise(value: Any) -> str:
    return f"{_to_float(value):,.2f} ₽".replace(",", " ").replace(".", ",")


def _format_decimal_amount(value: Any) -> str:
    rendered = f"{_to_float(value):,.2f}".replace(",", " ").replace(".", ",")
    if rendered.endswith(",00"):
        return rendered[:-3]
    return rendered


def _format_currency_amount(value: Any, currency: str | None) -> str:
    label = (currency or "вал.").strip()
    return f"{_format_decimal_amount(value)} {label}"


def _format_rate_value(value: Any) -> str:
    rendered = f"{_to_float(value):,.6f}".replace(",", " ").replace(".", ",")
    return rendered.rstrip("0").rstrip(",") or "0"


def _format_short_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).strftime("%d.%m")
    except ValueError:
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            return f"{text[8:10]}.{text[5:7]}"
        return text[:10]


def _format_qty(value: float) -> str:
    rendered = f"{value:,.3f}".replace(",", " ")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _format_pct(value: float) -> str:
    return f"{value * 100:.1f}%".replace(".", ",")


def _format_pct_points(value: float) -> str:
    rendered = f"{value * 100:.1f}".replace(".", ",")
    return f"{rendered} п.п."


def _format_percent_value(value: float | None, *, decimals: int = 2) -> str:
    if value is None:
        return "н/д"
    rendered = f"{value:.{decimals}f}".replace(".", ",")
    return f"{rendered}%"


def _avg_ticket(revenue: float, sales_count: float) -> float:
    if abs(sales_count) < 1e-9:
        return 0.0
    return revenue / sales_count


def _safe_ratio(numerator: float, denominator: float) -> float:
    if abs(denominator) < 1e-9:
        return 0.0
    return numerator / denominator


def _sum_case_balance(items: list[dict[str, Any]]) -> float:
    return sum(_to_float(item.get("current_balance")) for item in items)


def _buyers_snapshot_quality(items: list[dict[str, Any]]) -> dict[str, float]:
    signed_total = 0.0
    positive_total = 0.0
    unknown_positive_total = 0.0
    for item in items:
        balance = _to_float(item.get("current_balance"))
        signed_total += balance
        if balance <= 0:
            continue
        positive_total += balance
        if str(item.get("aged_bucket") or "").strip().lower() == "unknown":
            unknown_positive_total += balance

    unknown_positive_share = (
        0.0 if abs(positive_total) < 1e-9 else unknown_positive_total / positive_total
    )
    return {
        "signed_total": signed_total,
        "positive_total": positive_total,
        "unknown_positive_total": unknown_positive_total,
        "unknown_positive_share": unknown_positive_share,
    }


def _sum_sales_metric(items: list[dict[str, Any]], key: str) -> float:
    return sum(_to_float(item.get(key)) for item in items)


def _sales_metric_present(item: dict[str, Any], key: str) -> bool:
    if key not in item:
        return False
    value = item.get(key)
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _sum_present_sales_metric(items: list[dict[str, Any]], key: str) -> tuple[float, bool]:
    total = 0.0
    present = False
    for item in items:
        if _sales_metric_present(item, key):
            total += _to_float(item.get(key))
            present = True
    return total, present


def _sales_totals(items: list[dict[str, Any]]) -> dict[str, float | None]:
    revenue = _sum_sales_metric(items, "revenue")
    sales_count = _sum_sales_metric(items, "sales_count")
    cost_of_sales_sum, has_cost_of_sales = _sum_present_sales_metric(items, "cost_of_sales")
    gross_profit_sum, has_gross_profit = _sum_present_sales_metric(items, "gross_profit")
    profitability_pct_value, has_profitability_pct = _sum_present_sales_metric(
        items,
        "profitability_pct",
    )
    margin_pct_value, has_margin_pct = _sum_present_sales_metric(items, "margin_pct")

    cost_of_sales: float | None = None
    gross_profit: float | None = None
    profitability_pct: float | None = None

    if has_gross_profit:
        gross_profit = gross_profit_sum
        if has_cost_of_sales:
            cost_of_sales = cost_of_sales_sum
        else:
            cost_of_sales = revenue - gross_profit
    elif has_cost_of_sales:
        cost_of_sales = cost_of_sales_sum
        gross_profit = revenue - cost_of_sales
    elif len(items) == 1 and has_margin_pct:
        gross_profit = revenue * margin_pct_value
        cost_of_sales = revenue - gross_profit

    if gross_profit is not None:
        profitability_pct = _safe_ratio(gross_profit, revenue)
    elif len(items) == 1 and has_profitability_pct:
        profitability_pct = profitability_pct_value

    return {
        "revenue": revenue,
        "sales_count": sales_count,
        "cost_of_sales": cost_of_sales,
        "gross_profit": gross_profit,
        "profitability_pct": profitability_pct,
        "avg_ticket": _avg_ticket(revenue, sales_count),
    }


def _sales_period_section(
    current_items: list[dict[str, Any]],
    previous_items: list[dict[str, Any]],
) -> dict[str, float | None]:
    current = _sales_totals(current_items)
    previous = _sales_totals(previous_items)
    return {
        **current,
        "revenue_delta": current["revenue"] - previous["revenue"],
        "gross_profit_delta": (
            None
            if current["gross_profit"] is None or previous["gross_profit"] is None
            else current["gross_profit"] - previous["gross_profit"]
        ),
        "profitability_pct_delta": (
            None
            if current["profitability_pct"] is None or previous["profitability_pct"] is None
            else current["profitability_pct"] - previous["profitability_pct"]
        ),
        "sales_count_delta": current["sales_count"] - previous["sales_count"],
        "avg_ticket_delta": current["avg_ticket"] - previous["avg_ticket"],
    }


def _render_money_metric(
    label: str,
    value: float | None,
    delta: float | None,
    delta_label: str,
) -> str:
    if value is None:
        return f"{label} н/д"
    if delta is None:
        return f"{label} {_format_money(value)}"
    delta_sign = "+" if delta >= 0 else ""
    return f"{label} {_format_money(value)} ({delta_sign}{_format_money(delta)} {delta_label})"


def _render_pct_metric(
    label: str,
    value: float | None,
    delta: float | None,
    delta_label: str,
) -> str:
    if value is None:
        return f"{label} н/д"
    if delta is None:
        return f"{label} {_format_pct(value)}"
    delta_sign = "+" if delta >= 0 else ""
    return f"{label} {_format_pct(value)} ({delta_sign}{_format_pct_points(delta)} {delta_label})"


def _freshness_from_payload(payload: Any) -> dict[str, str]:
    if isinstance(payload, dict):
        freshness_status = payload.get("freshness_status")
        source_status = payload.get("source_status")
        if freshness_status or source_status:
            return {
                "freshness_status": freshness_status or "unknown",
                "source_status": source_status or "unknown",
            }
        if str(payload.get("status") or "").strip().lower() == "ready":
            return {
                "freshness_status": "fresh",
                "source_status": "ready",
            }
        items = payload.get("payload")
        if isinstance(items, list) and items:
            return {
                "freshness_status": "fresh",
                "source_status": "ready",
            }
    if isinstance(payload, list) and payload:
        return {
            "freshness_status": "fresh",
            "source_status": "ready",
        }
    return {
        "freshness_status": "unknown",
        "source_status": "unknown",
    }


def _section_source_status(payload_names: list[str], freshness: dict[str, dict[str, str]]) -> str:
    statuses = [freshness.get(name, {}).get("source_status", "unknown") for name in payload_names]
    if any(status == "error" for status in statuses):
        return "error"
    if any(status == "ready" for status in statuses):
        return "ready"
    if all(status == "empty" for status in statuses):
        return "empty"
    return "unknown"


def _is_blocking_freshness_issue(
    *,
    name: str,
    component: dict[str, str],
    health_components: dict[str, Any],
) -> bool:
    freshness_status = component.get("freshness_status")
    source_status = component.get("source_status")
    has_issue = freshness_status in {"stale", "missing"} or source_status in {
        "partial",
        "empty",
        "error",
    }
    if not has_issue:
        return False
    if name in OPTIONAL_FRESHNESS_COMPONENTS:
        return False
    if name == "new_daily" and freshness_status == "missing" and source_status == "empty":
        return False
    if name == "health":
        return any(
            item.get("component") != "staffing"
            and not (
                item.get("component") == "task_payloads"
                and item.get("source_status") == "partial"
                and int((item.get("metrics") or {}).get("task_payload_count") or 0) > 0
            )
            and (
                item.get("freshness_status") in {"stale", "missing"}
                or item.get("source_status") in {"partial", "empty", "error"}
            )
            for item in health_components.values()
            if isinstance(item, dict)
        )
    return True


def _payload_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        items = payload.get("payload", [])
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _previous_month_start(value: date) -> date:
    return (value.replace(day=1) - timedelta(days=1)).replace(day=1)


def _previous_month_same_day(value: date) -> date:
    previous_month_last_day = value.replace(day=1) - timedelta(days=1)
    previous_month_day = min(value.day, previous_month_last_day.day)
    return previous_month_last_day.replace(day=previous_month_day)


def _period_end_with_same_span(start: date, anchor: date, comparison_start: date) -> date:
    span_days = (anchor - start).days
    return comparison_start + timedelta(days=span_days)


def _iso_date_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    rendered = str(value).strip()
    if not rendered:
        return None
    if len(rendered) >= 10 and rendered[4] == "-" and rendered[7] == "-":
        return rendered[:10]
    return rendered


def _top_counterparties(items: list[dict[str, Any]], *, limit: int = 3) -> list[str]:
    ranked = sorted(items, key=lambda item: _to_float(item.get("current_balance")), reverse=True)
    lines: list[str] = []
    for item in ranked[:limit]:
        name = item.get("counterparty_name") or item.get("counterparty_ref") or "без названия"
        lines.append(f"{name} ({_format_money(_to_float(item.get('current_balance')))})")
    return lines


def _top_managers(items: list[dict[str, Any]], *, limit: int = 3) -> list[str]:
    ranked = sorted(items, key=lambda item: _to_float(item.get("total_balance")), reverse=True)
    lines: list[str] = []
    for item in ranked[:limit]:
        name = item.get("manager_name") or item.get("manager_ref") or "не назначен"
        total_balance = _format_money(_to_float(item.get("total_balance")))
        counterparty_count = int(item.get("counterparty_count") or 0)
        lines.append(f"{name}: {total_balance}, {counterparty_count} контр.")
    return lines


def _is_unassigned_manager_bucket(item: dict[str, Any]) -> bool:
    return not (
        str(item.get("manager_name") or "").strip() or str(item.get("manager_ref") or "").strip()
    )


def _unassigned_manager_totals(items: list[dict[str, Any]]) -> dict[str, float | int]:
    total_balance = 0.0
    counterparty_count = 0
    for item in items:
        if not isinstance(item, dict) or not _is_unassigned_manager_bucket(item):
            continue
        total_balance += _to_float(item.get("total_balance"))
        counterparty_count += int(item.get("counterparty_count") or 0)
    return {
        "total_balance": total_balance,
        "counterparty_count": counterparty_count,
    }


def _top_staffing_points(items: list[dict[str, Any]], *, limit: int = 3) -> list[str]:
    ranked = sorted(
        items,
        key=lambda item: (
            item.get("criticality") != "critical",
            -int(item.get("deficit_count") or 0),
            str(item.get("store_name") or item.get("store_ref") or ""),
        ),
    )
    lines: list[str] = []
    for item in ranked[:limit]:
        if int(item.get("deficit_count") or 0) <= 0:
            continue
        store_name = item.get("store_name") or item.get("store_ref") or "без магазина"
        shift_code = item.get("shift_code") or "shift"
        deficit = int(item.get("deficit_count") or 0)
        criticality = item.get("criticality") or "unknown"
        lines.append(f"{store_name}/{shift_code}: -{deficit}, {criticality}")
    return lines


def _task_efficiency_employee_line(item: dict[str, Any]) -> str:
    name = item.get("employee_name") or item.get("employee_key") or item.get("employee_bitrix_id")
    if not name:
        name = "сотрудник без имени"
    share_value = item.get("bitrix_effectiveness_pct")
    if share_value is None:
        share_value = item.get("personal_tasks_on_time_share")
    share = _to_float(share_value) if share_value is not None else None
    total = int(
        item.get("bitrix_total_in_work_count")
        if item.get("bitrix_total_in_work_count") is not None
        else item.get("total_personal_tasks_with_deadline") or 0
    )
    completed = int(
        item.get("bitrix_completed_tasks_count")
        if item.get("bitrix_completed_tasks_count") is not None
        else (
            int(item.get("closed_on_time_personal_tasks") or 0)
            + int(item.get("late_closed_personal_tasks") or 0)
        )
    )
    remarks = int(
        item.get("bitrix_task_remarks_count")
        if item.get("bitrix_task_remarks_count") is not None
        else (
            int(item.get("late_closed_personal_tasks") or 0)
            + int(item.get("open_overdue_personal_tasks") or 0)
        )
    )
    if share is None and total == 0 and completed == 0 and remarks == 0:
        return f"{name}: н/д (в Bitrix нет учитываемых задач за период)"
    return (
        f"{name}: {_format_percent_value(share, decimals=1)} "
        f"(в работе {total}; завершено {completed}; замечаний {remarks})"
    )


def _render_task_efficiency_lines(task_efficiency: dict[str, Any]) -> list[str]:
    if not isinstance(task_efficiency, dict):
        return []

    summary = task_efficiency.get("summary") or {}
    employees = task_efficiency.get("employees") or []
    low_employees = task_efficiency.get("low_efficiency_employees") or []
    if task_efficiency.get("status") == "ready" and isinstance(summary, dict):
        average = summary.get("bitrix_average_effectiveness_pct")
        if average is None:
            average = summary.get("average_on_time_share")
        threshold = summary.get("low_efficiency_threshold")
        total_tasks = int(
            summary.get("bitrix_total_in_work_count")
            if summary.get("bitrix_total_in_work_count") is not None
            else summary.get("total_personal_tasks_with_deadline") or 0
        )
        completed_tasks = int(
            summary.get("bitrix_completed_tasks_count")
            if summary.get("bitrix_completed_tasks_count") is not None
            else (
                int(summary.get("closed_on_time_personal_tasks") or 0)
                + int(summary.get("late_closed_personal_tasks") or 0)
            )
        )
        remarks = int(
            summary.get("bitrix_task_remarks_count")
            if summary.get("bitrix_task_remarks_count") is not None
            else (
                int(summary.get("late_closed_personal_tasks") or 0)
                + int(summary.get("open_overdue_personal_tasks") or 0)
            )
        )
        employee_count = int(summary.get("employee_count") or len(employees))
        applicable_count = int(summary.get("applicable_count") or 0)
        low_count = int(summary.get("low_efficiency_count") or len(low_employees))
        lines = [
            (
                "Эффективность задач Bitrix "
                f"({task_efficiency.get('month') or ''}): "
                f"средняя {_format_percent_value(_to_float(average) if average is not None else None, decimals=1)}; "
                f"в работе {total_tasks}; "
                f"завершено {completed_tasks}; "
                f"замечаний {remarks}; "
                f"ниже {_format_percent_value(_to_float(threshold) if threshold is not None else None, decimals=1)} - "
                f"{low_count} из {applicable_count} применимых, всего сотрудников {employee_count}."
            )
        ]
        employee_lines = [
            _task_efficiency_employee_line(item) for item in employees if isinstance(item, dict)
        ]
        if employee_lines:
            lines.append("По всем сотрудникам: " + "; ".join(employee_lines) + ".")
        return lines

    note = task_efficiency.get("note") or task_efficiency.get("status") or "данные не готовы"
    return [f"Эффективность задач Bitrix: {note}."]


def _currency_label(item: dict[str, Any], *, prefix: str = "") -> str:
    name_key = f"{prefix}currency_name"
    code_key = f"{prefix}currency_code"
    name = str(item.get(name_key) or "").strip()
    code = str(item.get(code_key) or "").strip()
    return name or code or "вал."


def _is_rub_currency_item(item: dict[str, Any], *, prefix: str = "") -> bool:
    label = _currency_label(item, prefix=prefix).strip().lower()
    code = str(item.get(f"{prefix}currency_code") or "").strip()
    return code == "643" or label in {"руб", "rub", "rur"}


def _render_exchange_counterparty_lines(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return []
    if payload.get("status") != "ready":
        note = payload.get("note") or payload.get("source_status") or "данные не готовы"
        return [f"Обменник: {note}."]

    control = payload.get("rub_control") or {}
    if not isinstance(control, dict):
        control = {}
    control_status = str(
        payload.get("control_status") or control.get("status") or "unknown"
    ).lower()
    status_label = "OK" if control_status == "ok" else "ВНИМАНИЕ"
    lines = [
        (
            f"Обменник {payload.get('counterparty_code') or ''}: {status_label}; "
            f"приход рублей {_format_rub_precise(control.get('rub_inflow'))}; "
            "расход валюты в руб. эквиваленте "
            f"{_format_rub_precise(control.get('foreign_outflow_rub'))}; "
            f"разница {_format_rub_precise(control.get('movement_diff_rub'))}; "
            f"рублевый хвост {_format_rub_precise(control.get('closing_balance_rub'))}."
        )
    ]

    rate_control = payload.get("rate_mismatch_control") or {}
    if isinstance(rate_control, dict):
        mismatch_count = int(_to_float(rate_control.get("mismatch_count")))
        rate_status = str(rate_control.get("status") or "unknown").lower()
        if rate_status != "ok" and mismatch_count > 0:
            total = rate_control.get("total_abs_diff_rub") or rate_control.get("total_diff_rub")
            items = [item for item in rate_control.get("items") or [] if isinstance(item, dict)][:5]
            details = []
            for item in items:
                currency = str(item.get("currency_name") or "вал.").strip()
                number = str(item.get("document_number") or "без номера").strip()
                document_date = _format_short_date(item.get("document_at"))
                multiplicity = _to_float(item.get("document_multiplicity"))
                rate = _format_rate_value(item.get("document_rate"))
                if multiplicity and multiplicity != 1:
                    rate = f"{rate}/{_format_rate_value(multiplicity)}"
                details.append(
                    f"{number} {document_date} {currency}: "
                    f"{_format_currency_amount(item.get('document_amount'), currency)} x {rate} "
                    f"-> {_format_rub_precise(item.get('expected_rub'))}, "
                    f"регистр {_format_rub_precise(item.get('movement_rub'))}, "
                    f"разница {_format_rub_precise(item.get('diff_rub'))}"
                )
            line = f"Ошибки курса Обменник: {mismatch_count} док. на {_format_rub_precise(total)}"
            if details:
                line = f"{line}; {'; '.join(details)}"
            lines.append(f"{line}.")

    summary = [item for item in payload.get("summary_by_currency") or [] if isinstance(item, dict)]
    if summary:
        summary.sort(
            key=lambda item: (
                _is_rub_currency_item(item, prefix="contract_"),
                _currency_label(item, prefix="contract_"),
            )
        )
        parts = []
        for item in summary:
            currency = _currency_label(item, prefix="contract_")
            native = _format_currency_amount(item.get("current_balance"), currency)
            if _is_rub_currency_item(item, prefix="contract_"):
                parts.append(native)
            else:
                parts.append(
                    f"{native} (экв. {_format_rub_precise(item.get('current_balance_rub'))})"
                )
        lines.append("Обменник остатки по валютам договора: " + "; ".join(parts) + ".")
    return lines


def _render_cash_position_lines(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return []
    if payload.get("status") != "ready":
        note = payload.get("note") or payload.get("source_status") or "данные не готовы"
        return [f"Остатки денег: {note}."]

    summary = [
        item for item in payload.get("summary_by_category_currency") or [] if isinstance(item, dict)
    ]
    if not summary:
        return ["Остатки денег: активных остатков в 1С не найдено."]

    category_order = ["bank_accounts", "cashboxes", "cards", "other"]
    category_parts = []
    for category in category_order:
        items = [item for item in summary if item.get("category") == category]
        if not items:
            continue
        items.sort(key=lambda item: _currency_label(item))
        category_name = str(items[0].get("category_name") or category).strip()
        values = [
            _format_currency_amount(item.get("current_balance"), _currency_label(item))
            for item in items
        ]
        category_parts.append(f"{category_name}: {', '.join(values)}")

    if not category_parts:
        return ["Остатки денег: активных остатков в 1С не найдено."]
    return ["Остатки денег по 1С без смешивания валют: " + "; ".join(category_parts) + "."]


def _format_error(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}: {error.reason}"
    if isinstance(error, urllib.error.URLError):
        return f"URL error: {error.reason}"
    return str(error)


def build_management_digest(
    *,
    fetch_json: Callable[[str, dict[str, str]], Any],
    anchor_date: date,
    role_code: str = "",
) -> dict[str, Any]:
    receivables_date = anchor_date
    staffing_date = anchor_date

    result: dict[str, Any] = {
        "anchor_date": anchor_date.isoformat(),
        "receivables_date": receivables_date.isoformat(),
        "staffing_date": staffing_date.isoformat(),
        "role_code": role_code,
        "status": "ready",
        "errors": [],
        "freshness": {},
        "sections": {},
    }

    responses: dict[str, Any] = {}
    previous_day = anchor_date - timedelta(days=1)
    previous_week = anchor_date - timedelta(days=7)
    previous_month = _previous_month_same_day(anchor_date)
    closed_month = _previous_month_start(anchor_date)
    calls = [
        ("health", "/api/management/health", {"date": anchor_date.isoformat()}),
        ("new_daily", "/api/receivables/new-daily", {"date": receivables_date.isoformat()}),
        (
            "overdue_cases",
            "/api/receivables/cases",
            {"date": receivables_date.isoformat(), "segment": "overdue"},
        ),
        (
            "inactive_cases",
            "/api/receivables/cases",
            {"date": receivables_date.isoformat(), "segment": "inactive"},
        ),
        (
            "fired_manager_cases",
            "/api/receivables/cases",
            {"date": receivables_date.isoformat(), "segment": "fired_manager"},
        ),
        (
            "adjustment_candidates",
            "/api/receivables/cases",
            {"date": receivables_date.isoformat(), "segment": "adjustment_candidates"},
        ),
        (
            "employee_cases",
            "/api/receivables/employee-cases",
            {"date": receivables_date.isoformat()},
        ),
        (
            "manager_summary",
            "/api/receivables/manager-summary",
            {"date": receivables_date.isoformat()},
        ),
        (
            "buyers_balance_current",
            "/api/bi/receivables-contract-balances",
            {"date": anchor_date.isoformat(), "buyers_rub_only": "true"},
        ),
        (
            "buyers_snapshot_current",
            "/api/bi/receivables-current",
            {"date": anchor_date.isoformat()},
        ),
        (
            "buyers_balance_previous_day",
            "/api/bi/receivables-contract-balances",
            {"date": previous_day.isoformat(), "buyers_rub_only": "true"},
        ),
        (
            "buyers_balance_previous_week",
            "/api/bi/receivables-contract-balances",
            {"date": previous_week.isoformat(), "buyers_rub_only": "true"},
        ),
        (
            "buyers_balance_previous_month",
            "/api/bi/receivables-contract-balances",
            {"date": previous_month.isoformat(), "buyers_rub_only": "true"},
        ),
        (
            "sales_daily_current",
            "/api/bi/sales-daily-kpi",
            {"date_from": anchor_date.isoformat(), "date_to": anchor_date.isoformat()},
        ),
        (
            "sales_daily_previous",
            "/api/bi/sales-daily-kpi",
            {
                "date_from": (anchor_date.fromordinal(anchor_date.toordinal() - 1)).isoformat(),
                "date_to": (anchor_date.fromordinal(anchor_date.toordinal() - 1)).isoformat(),
            },
        ),
        (
            "sales_weekly_current",
            "/api/bi/sales-weekly-kpi",
            {
                "date_from": _week_start(anchor_date).isoformat(),
                "date_to": anchor_date.isoformat(),
            },
        ),
        (
            "sales_weekly_previous",
            "/api/bi/sales-weekly-kpi",
            {
                "date_from": (_week_start(anchor_date) - timedelta(days=7)).isoformat(),
                "date_to": _period_end_with_same_span(
                    _week_start(anchor_date),
                    anchor_date,
                    _week_start(anchor_date) - timedelta(days=7),
                ).isoformat(),
            },
        ),
        (
            "sales_monthly_current",
            "/api/bi/sales-daily-kpi",
            {
                "date_from": _month_start(anchor_date).isoformat(),
                "date_to": anchor_date.isoformat(),
            },
        ),
        (
            "sales_monthly_previous",
            "/api/bi/sales-daily-kpi",
            {
                "date_from": _previous_month_start(anchor_date).isoformat(),
                "date_to": _period_end_with_same_span(
                    _month_start(anchor_date),
                    anchor_date,
                    _previous_month_start(anchor_date),
                ).isoformat(),
            },
        ),
        ("staffing_daily", "/api/staffing/daily", {"date": staffing_date.isoformat()}),
        ("task_payloads", "/api/management/task-payloads", {"date": staffing_date.isoformat()}),
        (
            "task_efficiency",
            "/api/management/task-efficiency",
            {"month": anchor_date.strftime("%Y-%m")},
        ),
    ]
    normalized_role_code = str(role_code or "").strip().lower()
    if normalized_role_code in FINANCE_CONTROL_ROLE_CODES:
        calls.extend(
            [
                (
                    "exchange_counterparty",
                    "/api/management/exchange-counterparty-settlements",
                    {"counterparty_code": EXCHANGE_COUNTERPARTY_CODE},
                ),
                (
                    "cash_position",
                    "/api/management/cash-position",
                    {"top": "15"},
                ),
            ]
        )
    if normalized_role_code in RETAIL_DIRECTOR_MONTHLY_ROLE_CODES:
        calls.append(
            (
                "retail_director_monthly_kpi",
                "/api/management/retail-director-monthly-kpi",
                {"month": closed_month.strftime("%Y-%m")},
            )
        )

    for name, path, params in calls:
        try:
            payload = fetch_json(path, params)
            responses[name] = payload
            result["freshness"][name] = _freshness_from_payload(payload)
        except Exception as error:
            result["status"] = "degraded"
            result["errors"].append(
                {
                    "component": name,
                    "path": path,
                    "message": _format_error(error),
                }
            )
            responses[name] = {
                "payload": [],
                "freshness_status": "missing",
                "source_status": "error",
            }
            result["freshness"][name] = {
                "freshness_status": "missing",
                "source_status": "error",
            }

    health_payload = responses["health"]
    result["sections"]["health"] = {
        "status": health_payload.get("status", "degraded" if result["errors"] else "unknown"),
        "components": health_payload.get("components", []),
    }

    health_components = {
        item.get("component"): item
        for item in health_payload.get("components", [])
        if isinstance(item, dict) and item.get("component")
    }

    new_daily = _payload_items(responses["new_daily"])
    overdue_cases = _payload_items(responses["overdue_cases"])
    inactive_cases = _payload_items(responses["inactive_cases"])
    fired_manager_cases = _payload_items(responses["fired_manager_cases"])
    adjustment_candidates = _payload_items(responses["adjustment_candidates"])
    employee_cases = _payload_items(responses["employee_cases"])
    manager_summary = _payload_items(responses["manager_summary"])
    assigned_manager_summary = [
        item
        for item in manager_summary
        if isinstance(item, dict) and not _is_unassigned_manager_bucket(item)
    ]
    unassigned_manager_totals = _unassigned_manager_totals(manager_summary)
    buyers_balance_current = _payload_items(responses["buyers_balance_current"])
    buyers_snapshot_current = _payload_items(responses["buyers_snapshot_current"])
    buyers_balance_previous_day = _payload_items(responses["buyers_balance_previous_day"])
    buyers_balance_previous_week = _payload_items(responses["buyers_balance_previous_week"])
    buyers_balance_previous_month = _payload_items(responses["buyers_balance_previous_month"])
    sales_daily_current = _payload_items(responses["sales_daily_current"])
    sales_daily_previous = _payload_items(responses["sales_daily_previous"])
    sales_weekly_current = _payload_items(responses["sales_weekly_current"])
    sales_weekly_previous = _payload_items(responses["sales_weekly_previous"])
    sales_monthly_current = _payload_items(responses["sales_monthly_current"])
    sales_monthly_previous = _payload_items(responses["sales_monthly_previous"])
    staffing_daily = _payload_items(responses["staffing_daily"])
    task_payloads = _payload_items(responses["task_payloads"])
    task_efficiency_payload = responses.get("task_efficiency", {})
    task_efficiency_items = _payload_items(task_efficiency_payload)
    task_efficiency_summary: dict[str, Any] = {}
    task_efficiency_note = ""
    task_efficiency_month = anchor_date.strftime("%Y-%m")
    if isinstance(task_efficiency_payload, dict):
        summary = task_efficiency_payload.get("summary") or {}
        if isinstance(summary, dict):
            task_efficiency_summary = summary
        task_efficiency_note = str(task_efficiency_payload.get("note") or "").strip()
        task_efficiency_month = str(task_efficiency_payload.get("month") or task_efficiency_month)
    task_payloads_date = staffing_date.isoformat()

    task_component = health_components.get("task_payloads") or {}
    receivables_component = health_components.get("receivables") or {}
    receivables_metrics = receivables_component.get("metrics", {})
    if not isinstance(receivables_metrics, dict):
        receivables_metrics = {}
    latest_task_date = task_component.get("latest_snapshot_date")
    if not task_payloads and latest_task_date and latest_task_date != staffing_date.isoformat():
        try:
            fallback_payload = fetch_json(
                "/api/management/task-payloads",
                {"date": latest_task_date},
            )
            fallback_items = _payload_items(fallback_payload)
            if fallback_items:
                task_payloads = fallback_items
                task_payloads_date = latest_task_date
                responses["task_payloads"] = fallback_payload
                result["freshness"]["task_payloads"] = {
                    "freshness_status": fallback_payload.get("freshness_status", "unknown"),
                    "source_status": fallback_payload.get("source_status", "unknown"),
                }
        except Exception as error:
            result["status"] = "degraded"
            result["errors"].append(
                {
                    "component": "task_payloads_fallback",
                    "path": "/api/management/task-payloads",
                    "message": _format_error(error),
                }
            )

    buyers_total_balance = _sum_case_balance(buyers_balance_current)
    buyers_previous_day_balance = _sum_case_balance(buyers_balance_previous_day)
    buyers_previous_week_balance = _sum_case_balance(buyers_balance_previous_week)
    buyers_previous_month_balance = _sum_case_balance(buyers_balance_previous_month)
    buyers_latest_snapshot_date = _iso_date_value(
        receivables_metrics.get("latest_balance_snapshot_date")
        or receivables_component.get("latest_snapshot_date")
    )
    buyers_balance_current_status = (
        result["freshness"].get("buyers_balance_current", {}).get("source_status", "unknown")
    )
    buyers_total_status = "ready"
    buyers_total_note = ""
    if buyers_balance_current_status == "error":
        buyers_total_status = "source_error"
        buyers_total_note = "источник дебиторки покупателей недоступен"
    elif buyers_balance_current or buyers_latest_snapshot_date == anchor_date.isoformat():
        buyers_total_status = "ready"
    elif buyers_latest_snapshot_date != anchor_date.isoformat():
        buyers_total_status = "snapshot_pending"
        if buyers_latest_snapshot_date:
            buyers_total_note = (
                f"актуальный срез за {anchor_date.isoformat()} ещё не готов; "
                f"последний snapshot {buyers_latest_snapshot_date}"
            )
        else:
            buyers_total_note = (
                f"актуальный срез за {anchor_date.isoformat()} ещё не готов; "
                "snapshot по дебиторке пока отсутствует"
            )
    buyers_snapshot_quality = _buyers_snapshot_quality(buyers_snapshot_current)
    receivables_source_status = str(receivables_component.get("source_status") or "unknown").strip()
    buyer_case_total = receivables_metrics.get("buyer_case_total_balance")
    buyer_case_total_value = _to_float(buyer_case_total) if buyer_case_total is not None else None
    if (
        buyers_total_status == "ready"
        and buyer_case_total_value is not None
        and abs(buyers_total_balance - buyer_case_total_value) > 0.01
    ):
        buyers_total_status = "degraded"
        buyers_total_note = (
            "buyers-срезы расходятся: BI "
            f"{_format_money(buyers_total_balance)}, cases "
            f"{_format_money(buyer_case_total_value)}; число временно не публикую"
        )
        result["status"] = "degraded"
    if (
        buyers_total_status == "ready"
        and receivables_source_status
        and receivables_source_status != "ready"
    ):
        buyers_total_status = "degraded"
        quality_issues = receivables_metrics.get("quality_issues") or []
        if quality_issues:
            buyers_total_note = (
                "authoritative snapshot дебиторки не прошёл контроль качества "
                f"({', '.join(str(item) for item in quality_issues)}); "
                "число покупателей временно не публикую"
            )
        else:
            buyers_total_note = (
                "authoritative snapshot дебиторки не готов; "
                "число покупателей временно не публикую"
            )
        result["status"] = "degraded"
    if (
        buyers_total_status == "ready"
        and buyers_snapshot_quality["signed_total"] < 0
        and buyers_snapshot_quality["unknown_positive_share"] >= 0.8
    ):
        buyers_total_status = "degraded"
        buyers_total_note = (
            "текущий buyers-срез противоречив: signed-остаток отрицательный, "
            f"{buyers_snapshot_quality['unknown_positive_share'] * 100:.0f}% "
            "положительной суммы сидит в unknown-bucket; до exact-сверки 1С "
            "на этот блок опираться нельзя"
        )
        result["status"] = "degraded"
    buyers_reportable_balance: float | None = buyers_total_balance
    buyers_reportable_day_delta: float | None = buyers_total_balance - buyers_previous_day_balance
    buyers_reportable_week_delta: float | None = buyers_total_balance - buyers_previous_week_balance
    buyers_reportable_month_delta: float | None = (
        buyers_total_balance - buyers_previous_month_balance
    )
    if buyers_total_status != "ready":
        buyers_reportable_balance = None
        buyers_reportable_day_delta = None
        buyers_reportable_week_delta = None
        buyers_reportable_month_delta = None

    result["sections"]["receivables"] = {
        "buyers_total": {
            "as_of": anchor_date.isoformat(),
            "status": buyers_total_status,
            "note": buyers_total_note,
            "latest_snapshot_date": buyers_latest_snapshot_date,
            "total_balance": buyers_reportable_balance,
            "snapshot_signed_total": buyers_snapshot_quality["signed_total"],
            "snapshot_unknown_positive_total": buyers_snapshot_quality["unknown_positive_total"],
            "snapshot_unknown_positive_share": buyers_snapshot_quality["unknown_positive_share"],
            "day_compare_date": previous_day.isoformat(),
            "day_delta": buyers_reportable_day_delta,
            "week_compare_date": previous_week.isoformat(),
            "week_delta": buyers_reportable_week_delta,
            "month_compare_date": previous_month.isoformat(),
            "month_delta": buyers_reportable_month_delta,
        },
        "new_daily_count": len(new_daily),
        "new_daily_total_balance": _sum_case_balance(new_daily),
        "overdue_count": len(overdue_cases),
        "overdue_total_balance": _sum_case_balance(overdue_cases),
        "inactive_count": len(inactive_cases),
        "inactive_total_balance": _sum_case_balance(inactive_cases),
        "unassigned_counterparty_count": int(unassigned_manager_totals["counterparty_count"]),
        "unassigned_total_balance": float(unassigned_manager_totals["total_balance"]),
        "fired_manager_count": len(fired_manager_cases),
        "fired_manager_total_balance": _sum_case_balance(fired_manager_cases),
        "employee_count": len(employee_cases),
        "employee_total_balance": _sum_case_balance(employee_cases),
        "adjustment_candidates_count": len(adjustment_candidates),
        "adjustment_candidates_total_balance": _sum_case_balance(adjustment_candidates),
        "top_new_daily": _top_counterparties(new_daily),
        "top_managers": _top_managers(assigned_manager_summary),
    }

    sales_payload_names = [
        "sales_daily_current",
        "sales_daily_previous",
        "sales_weekly_current",
        "sales_weekly_previous",
        "sales_monthly_current",
        "sales_monthly_previous",
    ]
    sales_source_status = _section_source_status(sales_payload_names, result["freshness"])
    sales_current_row_count = len(sales_daily_current)
    sales_status = (
        "ready" if sales_source_status == "ready" and sales_current_row_count else "empty"
    )
    sales_note = ""
    if sales_source_status == "error":
        sales_status = "source_error"
        sales_note = "источник продаж недоступен"
    elif sales_current_row_count == 0:
        sales_note = f"нет строк продаж за {anchor_date.isoformat()}"

    result["sections"]["sales"] = {
        "as_of": anchor_date.isoformat(),
        "status": sales_status,
        "freshness_status": "fresh" if sales_status == "ready" else "unknown",
        "source_status": sales_source_status,
        "note": sales_note,
        "row_counts": {
            "day": sales_current_row_count,
            "day_previous": len(sales_daily_previous),
            "week": len(sales_weekly_current),
            "week_previous": len(sales_weekly_previous),
            "month": len(sales_monthly_current),
            "month_previous": len(sales_monthly_previous),
        },
        "previous_date": date.fromordinal(anchor_date.toordinal() - 1).isoformat(),
        "day": {
            **_sales_period_section(sales_daily_current, sales_daily_previous),
        },
        "week": {
            "date_from": _week_start(anchor_date).isoformat(),
            "date_to": anchor_date.isoformat(),
            "compare_from": (_week_start(anchor_date) - timedelta(days=7)).isoformat(),
            "compare_to": _period_end_with_same_span(
                _week_start(anchor_date),
                anchor_date,
                _week_start(anchor_date) - timedelta(days=7),
            ).isoformat(),
            **_sales_period_section(sales_weekly_current, sales_weekly_previous),
        },
        "month": {
            "date_from": _month_start(anchor_date).isoformat(),
            "date_to": anchor_date.isoformat(),
            "compare_from": _previous_month_start(anchor_date).isoformat(),
            "compare_to": _period_end_with_same_span(
                _month_start(anchor_date),
                anchor_date,
                _previous_month_start(anchor_date),
            ).isoformat(),
            **_sales_period_section(sales_monthly_current, sales_monthly_previous),
        },
    }

    critical_staffing = [
        item for item in staffing_daily if (item.get("criticality") or "") == "critical"
    ]
    deficit_staffing = [item for item in staffing_daily if int(item.get("deficit_count") or 0) > 0]
    total_deficit = sum(int(item.get("deficit_count") or 0) for item in staffing_daily)
    result["sections"]["staffing"] = {
        "deficit_shift_count": len(deficit_staffing),
        "critical_shift_count": len(critical_staffing),
        "total_deficit_count": total_deficit,
        "top_points": _top_staffing_points(deficit_staffing),
    }

    rule_counter = Counter(item.get("rule_code") or "unknown" for item in task_payloads)
    result["sections"]["task_payloads"] = {
        "as_of": task_payloads_date,
        "total_count": len(task_payloads),
        "by_rule": dict(sorted(rule_counter.items())),
    }

    low_threshold_value = task_efficiency_summary.get("low_efficiency_threshold")
    low_threshold_pct = _to_float(low_threshold_value) if low_threshold_value is not None else 80.0
    low_efficiency_items = [
        item
        for item in task_efficiency_items
        if item.get("is_metric_applicable", True)
        and (
            item.get("bitrix_effectiveness_pct") is not None
            or item.get("personal_tasks_on_time_share") is not None
        )
        and _to_float(
            item.get("bitrix_effectiveness_pct")
            if item.get("bitrix_effectiveness_pct") is not None
            else item.get("personal_tasks_on_time_share")
        )
        < low_threshold_pct
    ]
    result["sections"]["task_efficiency"] = {
        "month": task_efficiency_month,
        "status": result["freshness"].get("task_efficiency", {}).get("source_status", "unknown"),
        "freshness_status": result["freshness"]
        .get("task_efficiency", {})
        .get("freshness_status", "unknown"),
        "note": task_efficiency_note,
        "summary": task_efficiency_summary,
        "employees": task_efficiency_items,
        "low_efficiency_employees": low_efficiency_items,
    }

    result["sections"]["meetings"] = {
        "status": "not_configured",
        "note": "AI action items по встречам на сервере B пока не подключены в этот digest.",
    }

    if normalized_role_code in FINANCE_CONTROL_ROLE_CODES:
        exchange_payload = responses.get("exchange_counterparty", {})
        result["sections"]["exchange_counterparty"] = (
            exchange_payload
            if isinstance(exchange_payload, dict)
            else {"status": "source_error", "note": "неожиданный формат ответа"}
        )
        cash_payload = responses.get("cash_position", {})
        result["sections"]["cash_position"] = (
            cash_payload
            if isinstance(cash_payload, dict)
            else {"status": "source_error", "note": "неожиданный формат ответа"}
        )

    if normalized_role_code in RETAIL_DIRECTOR_MONTHLY_ROLE_CODES:
        result["sections"]["retail_director_open_month"] = {
            "month": anchor_date.strftime("%Y-%m"),
            "date_from": result["sections"]["sales"]["month"]["date_from"],
            "date_to": result["sections"]["sales"]["month"]["date_to"],
            "compare_from": result["sections"]["sales"]["month"]["compare_from"],
            "compare_to": result["sections"]["sales"]["month"]["compare_to"],
            **result["sections"]["sales"]["month"],
        }
        retail_director_monthly = responses.get("retail_director_monthly_kpi", {})
        payload = {}
        if isinstance(retail_director_monthly, dict) and isinstance(
            retail_director_monthly.get("payload"), dict
        ):
            payload = retail_director_monthly["payload"]
        result["sections"]["retail_director_monthly_kpi"] = {
            "month": closed_month.strftime("%Y-%m"),
            "status": "ready" if payload else "missing",
            "note": (
                ""
                if payload
                else f"monthly KPI retail_director за {closed_month.strftime('%Y-%m')} пока не опубликован."
            ),
            "payload": payload,
        }

    if any(
        _is_blocking_freshness_issue(
            name=name,
            component=component,
            health_components=health_components,
        )
        for name, component in result["freshness"].items()
    ):
        result["status"] = "degraded"

    return result


def render_management_digest(digest: dict[str, Any]) -> str:
    lines = [
        "Management Control Tower",
        (
            f"Даты: дебиторка {digest['receivables_date']}, "
            f"staffing {digest['staffing_date']}, anchor {digest['anchor_date']}."
        ),
        f"Статус интеграции: {digest['status']}.",
    ]

    health = digest["sections"]["health"]
    health_components = {
        item.get("component"): f"{item.get('freshness_status')}/{item.get('source_status')}"
        for item in health.get("components", [])
    }
    if health_components:
        lines.append(
            "Свежесть витрин: "
            + ", ".join(f"{name}={status}" for name, status in sorted(health_components.items()))
            + "."
        )

    receivables = digest["sections"]["receivables"]
    buyers_total = receivables["buyers_total"]
    if buyers_total["status"] == "ready":
        buyers_day_delta_sign = "+" if buyers_total["day_delta"] >= 0 else ""
        buyers_week_delta_sign = "+" if buyers_total["week_delta"] >= 0 else ""
        buyers_month_delta_sign = "+" if buyers_total["month_delta"] >= 0 else ""
        lines.append(
            "Дебиторка покупателей: "
            f"{_format_money(buyers_total['total_balance'])} "
            f"({buyers_day_delta_sign}{_format_money(buyers_total['day_delta'])} д/д; "
            f"{buyers_week_delta_sign}{_format_money(buyers_total['week_delta'])} н/н; "
            f"{buyers_month_delta_sign}{_format_money(buyers_total['month_delta'])} м/м)."
        )
    else:
        lines.append(f"Дебиторка покупателей: {buyers_total['note']}.")
    role_code = str(digest.get("role_code") or "").strip().lower()
    if role_code in DETAIL_RECEIVABLE_ROLE_CODES and buyers_total["status"] == "ready":
        lines.append(
            "Детали дебиторки: "
            f"новые долги {receivables['new_daily_count']} на "
            f"{_format_money(receivables['new_daily_total_balance'])}; "
            f"просрочка {receivables['overdue_count']} на "
            f"{_format_money(receivables['overdue_total_balance'])}; "
            f"inactive {receivables['inactive_count']} на "
            f"{_format_money(receivables['inactive_total_balance'])}; "
            f"без владельца {receivables['unassigned_counterparty_count']} на "
            f"{_format_money(receivables['unassigned_total_balance'])}; "
            f"уволенный менеджер {receivables['fired_manager_count']} на "
            f"{_format_money(receivables['fired_manager_total_balance'])}; "
            f"сотрудники {receivables['employee_count']} на "
            f"{_format_money(receivables['employee_total_balance'])}; "
            f"кандидаты на корректировку {receivables['adjustment_candidates_count']} на "
            f"{_format_money(receivables['adjustment_candidates_total_balance'])}."
        )
        if receivables["top_new_daily"]:
            lines.append("Топ новых долгов: " + "; ".join(receivables["top_new_daily"]) + ".")
        if receivables["top_managers"]:
            lines.append("Менеджеры по портфелю: " + "; ".join(receivables["top_managers"]) + ".")

    sales = digest["sections"]["sales"]
    if sales.get("status") != "ready":
        lines.append(f"Продажи: {sales.get('note') or 'данные не готовы'}.")
    else:
        day_sales = sales["day"]
        week_sales = sales["week"]
        month_sales = sales["month"]
        day_revenue_delta_sign = "+" if day_sales["revenue_delta"] >= 0 else ""
        day_qty_delta_sign = "+" if day_sales["sales_count_delta"] >= 0 else ""
        day_ticket_delta_sign = "+" if day_sales["avg_ticket_delta"] >= 0 else ""
        week_revenue_delta_sign = "+" if week_sales["revenue_delta"] >= 0 else ""
        week_qty_delta_sign = "+" if week_sales["sales_count_delta"] >= 0 else ""
        week_ticket_delta_sign = "+" if week_sales["avg_ticket_delta"] >= 0 else ""
        month_revenue_delta_sign = "+" if month_sales["revenue_delta"] >= 0 else ""
        month_qty_delta_sign = "+" if month_sales["sales_count_delta"] >= 0 else ""
        month_ticket_delta_sign = "+" if month_sales["avg_ticket_delta"] >= 0 else ""
        lines.append(
            "Продажи день: "
            f"выручка {_format_money(day_sales['revenue'])} "
            f"({day_revenue_delta_sign}{_format_money(day_sales['revenue_delta'])} д/д); "
            f"{_render_money_metric('валовая прибыль', day_sales['gross_profit'], day_sales['gross_profit_delta'], 'д/д')}; "
            f"{_render_pct_metric('рентабельность продаж', day_sales['profitability_pct'], day_sales['profitability_pct_delta'], 'д/д')}; "
            f"продано {_format_qty(day_sales['sales_count'])} шт. "
            f"({day_qty_delta_sign}{_format_qty(day_sales['sales_count_delta'])} д/д); "
            f"ср. чек {_format_money(day_sales['avg_ticket'])} "
            f"({day_ticket_delta_sign}{_format_money(day_sales['avg_ticket_delta'])} д/д)."
        )
        lines.append(
            "Продажи неделя: "
            f"выручка {_format_money(week_sales['revenue'])} "
            f"({week_revenue_delta_sign}{_format_money(week_sales['revenue_delta'])} н/н); "
            f"{_render_money_metric('валовая прибыль', week_sales['gross_profit'], week_sales['gross_profit_delta'], 'н/н')}; "
            f"{_render_pct_metric('рентабельность продаж', week_sales['profitability_pct'], week_sales['profitability_pct_delta'], 'н/н')}; "
            f"продано {_format_qty(week_sales['sales_count'])} шт. "
            f"({week_qty_delta_sign}{_format_qty(week_sales['sales_count_delta'])} н/н); "
            f"ср. чек {_format_money(week_sales['avg_ticket'])} "
            f"({week_ticket_delta_sign}{_format_money(week_sales['avg_ticket_delta'])} н/н)."
        )
        lines.append(
            "Продажи месяц: "
            f"выручка {_format_money(month_sales['revenue'])} "
            f"({month_revenue_delta_sign}{_format_money(month_sales['revenue_delta'])} м/м); "
            f"{_render_money_metric('валовая прибыль', month_sales['gross_profit'], month_sales['gross_profit_delta'], 'м/м')}; "
            f"{_render_pct_metric('рентабельность продаж', month_sales['profitability_pct'], month_sales['profitability_pct_delta'], 'м/м')}; "
            f"продано {_format_qty(month_sales['sales_count'])} шт. "
            f"({month_qty_delta_sign}{_format_qty(month_sales['sales_count_delta'])} м/м); "
            f"ср. чек {_format_money(month_sales['avg_ticket'])} "
            f"({month_ticket_delta_sign}{_format_money(month_sales['avg_ticket_delta'])} м/м)."
        )

    if role_code in FINANCE_CONTROL_ROLE_CODES:
        lines.extend(
            _render_exchange_counterparty_lines(digest["sections"].get("exchange_counterparty", {}))
        )
        lines.extend(_render_cash_position_lines(digest["sections"].get("cash_position", {})))

    lines.extend(_render_task_efficiency_lines(digest["sections"].get("task_efficiency", {})))

    staffing = digest["sections"]["staffing"]
    lines.append(
        "Staffing: "
        f"смен с дефицитом {staffing['deficit_shift_count']}, "
        f"критичных {staffing['critical_shift_count']}, "
        f"суммарный дефицит {staffing['total_deficit_count']}."
    )
    if staffing["top_points"]:
        lines.append("Критичные точки: " + "; ".join(staffing["top_points"]) + ".")

    task_payloads = digest["sections"]["task_payloads"]
    if task_payloads["total_count"] > 0:
        by_rule = ", ".join(f"{rule}={count}" for rule, count in task_payloads["by_rule"].items())
        lines.append(
            f"Payload'ы задач ({task_payloads['as_of']}): "
            f"всего {task_payloads['total_count']}; {by_rule}."
        )
    else:
        lines.append("Payload'ы задач: пусто или источник недоступен.")

    meetings = digest["sections"]["meetings"]
    lines.append(f"Встречи/AI: {meetings['note']}")

    open_month = digest["sections"].get("retail_director_open_month")
    if isinstance(open_month, dict):
        open_month_revenue_delta_sign = (
            "+" if _to_float(open_month.get("revenue_delta")) >= 0 else ""
        )
        open_month_qty_delta_sign = (
            "+" if _to_float(open_month.get("sales_count_delta")) >= 0 else ""
        )
        open_month_ticket_delta_sign = (
            "+" if _to_float(open_month.get("avg_ticket_delta")) >= 0 else ""
        )
        lines.append(
            "Розница, открытый месяц "
            f"{open_month.get('month') or ''} "
            f"({open_month.get('date_from')}..{open_month.get('date_to')} "
            f"vs {open_month.get('compare_from')}..{open_month.get('compare_to')}): "
            f"выручка {_format_money(_to_float(open_month.get('revenue')))} "
            f"({open_month_revenue_delta_sign}{_format_money(_to_float(open_month.get('revenue_delta')))}); "
            f"{_render_money_metric('валовая прибыль', open_month.get('gross_profit'), open_month.get('gross_profit_delta'), 'к тому же периоду')}; "
            f"{_render_pct_metric('рентабельность продаж', open_month.get('profitability_pct'), open_month.get('profitability_pct_delta'), 'к тому же периоду')}; "
            f"продано {_format_qty(_to_float(open_month.get('sales_count')))} шт. "
            f"({open_month_qty_delta_sign}{_format_qty(_to_float(open_month.get('sales_count_delta')))}); "
            f"ср. чек {_format_money(_to_float(open_month.get('avg_ticket')))} "
            f"({open_month_ticket_delta_sign}{_format_money(_to_float(open_month.get('avg_ticket_delta')))})."
        )

    monthly_kpi = digest["sections"].get("retail_director_monthly_kpi")
    if isinstance(monthly_kpi, dict):
        if monthly_kpi.get("status") == "ready":
            payload = monthly_kpi.get("payload") or {}
            lines.append(
                "Розница, закрытый месяц "
                f"{monthly_kpi['month']}: "
                f"списания {_format_money(_to_float(payload.get('writeoff_amount')))}; "
                f"оприходования {_format_money(_to_float(payload.get('receipt_amount')))}; "
                f"чистые потери {_format_money(_to_float(payload.get('shrinkage_amount')))}; "
                f"уровень потерь {_format_percent_value(_to_float(payload.get('shrinkage_pct')), decimals=4)}."
            )
            lines.append(
                "Премия retail_director: "
                f"индекс KPI {_to_float(payload.get('kpi_index_sum')):.4f}; "
                f"бонус {_format_money(_to_float(payload.get('kpi_bonus_amount')))}; "
                f"к выплате {_format_money(_to_float(payload.get('to_pay')))}."
            )
        else:
            lines.append(
                "Розница, закрытый месяц "
                f"{monthly_kpi['month']}: {monthly_kpi.get('note') or 'данные не готовы'}."
            )

    if digest["errors"]:
        lines.append("Деградация:")
        for item in digest["errors"]:
            lines.append(f"- {item['component']} ({item['path']}): {item['message']}")

    return "\n".join(lines)


def main() -> None:
    args = _parse_args()
    env = _load_env(
        os.getenv("OPENCLAW_ENV_FILE") or os.getenv("PRICING_ENV_FILE") or DEFAULT_LOCAL_ENV_FILE
    )
    source_url = (
        env.get("MANAGEMENT_SOURCE_URL")
        or env.get("RETURN_SCHEME_SOURCE_URL")
        or DEFAULT_LOCAL_SOURCE_URL
    )
    source_token = (
        env.get("MANAGEMENT_SOURCE_TOKEN")
        or env.get("RETURN_SCHEME_SOURCE_TOKEN")
        or env.get("MANAGEMENT_INTERNAL_API_TOKEN")
        or env.get("RETURN_SCHEME_INTERNAL_API_TOKEN")
    )
    if not source_token:
        raise SystemExit(
            "Missing required env: MANAGEMENT_SOURCE_TOKEN|RETURN_SCHEME_SOURCE_TOKEN|"
            "MANAGEMENT_INTERNAL_API_TOKEN|RETURN_SCHEME_INTERNAL_API_TOKEN"
        )

    timeout = int(env.get("MANAGEMENT_ADAPTER_TIMEOUT_SECONDS", "20") or 20)
    retries = int(env.get("MANAGEMENT_ADAPTER_RETRIES", "2") or 2)
    retry_delay = float(env.get("MANAGEMENT_ADAPTER_RETRY_DELAY_SECONDS", "1.0") or 1.0)
    anchor_date = (
        datetime.strptime(args.anchor_date, "%Y-%m-%d").date() if args.anchor_date else date.today()
    )

    fetch_json = _build_fetcher(
        source_url=source_url,
        token=source_token,
        timeout=timeout,
        retries=retries,
        retry_delay=retry_delay,
    )
    digest = build_management_digest(
        fetch_json=fetch_json,
        anchor_date=anchor_date,
        role_code=args.role_code,
    )

    if args.json:
        print(json.dumps(digest, ensure_ascii=False, indent=2))
    else:
        print(render_management_digest(digest))


if __name__ == "__main__":
    main()
