#!/usr/bin/env python3
"""Build and deliver a monthly online-demand report through the Openclaw/B route."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from app.services.online_demand_metrics import (
    DEFAULT_METRIKA_BASE_URL,
    DEFAULT_METRIKA_COUNTER_ID,
    OnlineDemandLandingPage,
    fetch_online_demand_landing_pages,
    fetch_online_demand_weekly_summary,
    render_online_demand_block,
)

DEFAULT_LOCAL_ENV_FILE = "/opt/MM/pricing-service/.env"
DEFAULT_STATE_PATH = "/home/deploy/.openclaw/workspace/.data/monthly-online-demand/state.json"
DEFAULT_ARTIFACT_DIR = "/home/deploy/.openclaw/workspace/.data/monthly-online-demand/artifacts"
TELEGRAM_MESSAGE_LIMIT = 3900


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send monthly online-demand report to Telegram.")
    parser.add_argument(
        "--month",
        help="Closed month in YYYY-MM format; default is previous calendar month.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Build without Telegram side effects."
    )
    parser.add_argument(
        "--force", action="store_true", help="Send even if this month was delivered."
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable summary.")
    return parser.parse_args()


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


def previous_month(today: date | None = None) -> str:
    anchor = today or date.today()
    return (anchor.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def month_bounds(month: str) -> tuple[date, date]:
    start = datetime.strptime(month, "%Y-%m").date().replace(day=1)
    if start.month == 12:
        next_month = date(start.year + 1, 1, 1)
    else:
        next_month = date(start.year, start.month + 1, 1)
    return start, next_month - timedelta(days=1)


def previous_month_bounds(month: str) -> tuple[date, date]:
    start, _end = month_bounds(month)
    return month_bounds((start - timedelta(days=1)).strftime("%Y-%m"))


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"months": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {"months": {}}
    payload.setdefault("months", {})
    return payload


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _parse_chat_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [chunk.strip() for chunk in raw.split(",") if chunk.strip()]


def _resolve_chat_ids(env: dict[str, str]) -> list[str]:
    return _parse_chat_ids(
        env.get("MONTHLY_ONLINE_DEMAND_TELEGRAM_CHAT_ID")
        or env.get("WEEKLY_MANAGER_SALES_B_TELEGRAM_CHAT_ID_SALES")
        or env.get("WEEKLY_MANAGER_SALES_B_TELEGRAM_CHAT_ID")
        or env.get("WEEKLY_MANAGER_SALES_REPORT_TELEGRAM_CHAT_ID")
        or env.get("WEEKLY_BUYER_DIGEST_ALERT_TELEGRAM_CHAT_ID")
    )


def _resolve_telegram_token(env: dict[str, str]) -> str | None:
    return (
        env.get("MONTHLY_ONLINE_DEMAND_TELEGRAM_TOKEN")
        or env.get("WEEKLY_MANAGER_SALES_B_TELEGRAM_TOKEN")
        or env.get("WEEKLY_MANAGER_SALES_REPORT_TELEGRAM_TOKEN")
        or env.get("WEEKLY_BUYER_DIGEST_ALERT_TELEGRAM_TOKEN")
        or env.get("TELEGRAM_TOKEN_MM")
    )


def _resolve_metrika_token(env: dict[str, str]) -> str | None:
    return (
        env.get("MONTHLY_ONLINE_DEMAND_METRIKA_TOKEN")
        or env.get("WEEKLY_MANAGER_SALES_METRIKA_TOKEN")
        or env.get("YANDEX_METRIKA_TOKEN")
    )


def _send_telegram_message(
    *,
    token: str,
    chat_id: str,
    message: str,
    timeout: int = 60,
) -> None:
    body = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message[:TELEGRAM_MESSAGE_LIMIT],
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def _short_url(url: str) -> str:
    rendered = url.replace("https://master-mobile.ru", "").replace("http://master-mobile.ru", "")
    return rendered or "/"


def _path_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return parsed.path or url or "/"


def _is_business_landing_page(url: str) -> bool:
    path = _path_from_url(url).rstrip("/") or "/"
    if path == "/":
        return False
    return not path.startswith(("/basket", "/order", "/personal", "/auth", "/login"))


def _page_line(index: int, page: OnlineDemandLandingPage, *, include_conversion: bool) -> str:
    base = f"{index}. {_short_url(page.url)} — {page.visits} визитов, {page.purchases} покупок"
    if include_conversion:
        return f"{base}, конв. {str(page.purchase_conversion_pct).replace('.', ',')}%"
    return base


def render_monthly_online_demand_report(
    *,
    month: str,
    base_block: str,
    top_pages: list[OnlineDemandLandingPage],
    no_purchase_pages: list[OnlineDemandLandingPage],
) -> str:
    business_top_pages = [page for page in top_pages if _is_business_landing_page(page.url)]
    if not business_top_pages:
        business_top_pages = top_pages

    lines = [base_block, "", "🏆 Топ посадочных страниц по покупкам:"]
    if business_top_pages:
        lines.extend(
            _page_line(index, page, include_conversion=True)
            for index, page in enumerate(business_top_pages[:5], start=1)
        )
    else:
        lines.append("Нет страниц с покупками в выбранном периоде.")

    lines.extend(["", "⚠️ Страницы с трафиком, но без покупок:"])
    filtered_no_purchase = [page for page in no_purchase_pages if page.purchases == 0]
    if filtered_no_purchase:
        lines.extend(
            _page_line(index, page, include_conversion=False)
            for index, page in enumerate(filtered_no_purchase[:5], start=1)
        )
    else:
        lines.append("Нет явных страниц-кандидатов в выборке.")

    lines.extend(
        [
            "",
            "🎯 Рекомендация на месяц:",
            "усиливать рекламой страницы из топа, а страницы с трафиком без покупок отдать на аудит карточки, цены, наличия и посадочного сценария.",
            "",
            "Данные: Яндекс Метрика, не финансовая выручка 1С.",
        ]
    )
    return "\n".join(lines)


def build_monthly_online_demand_report(
    *,
    env: dict[str, str],
    month: str,
) -> str:
    token = _resolve_metrika_token(env)
    if not token:
        raise RuntimeError("Не задан MONTHLY_ONLINE_DEMAND_METRIKA_TOKEN|YANDEX_METRIKA_TOKEN")
    month_start, month_end = month_bounds(month)
    compare_start, compare_end = previous_month_bounds(month)
    counter_id = (
        env.get("MONTHLY_ONLINE_DEMAND_METRIKA_COUNTER_ID")
        or env.get("WEEKLY_MANAGER_SALES_METRIKA_COUNTER_ID")
        or env.get("YANDEX_METRIKA_COUNTER_ID")
        or DEFAULT_METRIKA_COUNTER_ID
    )
    base_url = env.get("YANDEX_METRIKA_BASE_URL") or DEFAULT_METRIKA_BASE_URL
    timeout = float(env.get("YANDEX_METRIKA_TIMEOUT_SECONDS", "20"))
    page_limit = int(env.get("MONTHLY_ONLINE_DEMAND_PAGE_LIMIT", "20"))
    summary = fetch_online_demand_weekly_summary(
        token=token,
        counter_id=counter_id,
        week_start=month_start,
        week_end=month_end,
        compare_week_start=compare_start,
        compare_week_end=compare_end,
        base_url=base_url,
        timeout=timeout,
    )
    top_pages = fetch_online_demand_landing_pages(
        token=token,
        counter_id=counter_id,
        date_from=month_start,
        date_to=month_end,
        sort="-purchases",
        limit=page_limit,
        base_url=base_url,
        timeout=timeout,
    )
    no_purchase_pages = fetch_online_demand_landing_pages(
        token=token,
        counter_id=counter_id,
        date_from=month_start,
        date_to=month_end,
        sort="-ym:s:visits",
        limit=page_limit,
        base_url=base_url,
        timeout=timeout,
    )
    return render_monthly_online_demand_report(
        month=month,
        base_block=render_online_demand_block(summary).replace(
            "📊 Онлайн-спрос и конверсия", f"📊 Онлайн-спрос и продажи сайта за {month}"
        ),
        top_pages=top_pages,
        no_purchase_pages=no_purchase_pages,
    )


def sync_monthly_online_demand_report(
    *,
    env: dict[str, str],
    month: str,
    state_path: Path,
    artifact_dir: Path,
    deliver_message: Callable[..., dict[str, Any]],
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    report_key = f"monthly-online-demand|{month}"
    state = _load_state(state_path)
    current = (state.get("months") or {}).get(month)
    if not force and isinstance(current, dict) and current.get("delivery_status") == "delivered":
        return {
            "status": "ok",
            "action": "noop",
            "month": month,
            "report_key": report_key,
            "sent_messages": 0,
            "artifact_path": current.get("artifact_path"),
        }

    message = build_monthly_online_demand_report(env=env, month=month)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / month / f"monthly-online-demand-{month}.txt"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(message, encoding="utf-8")

    if dry_run:
        return {
            "status": "ok",
            "action": "dry_run",
            "month": month,
            "report_key": report_key,
            "sent_messages": 0,
            "artifact_path": str(artifact_path),
            "message_preview": message,
        }

    delivery = deliver_message(message=message, artifact_path=artifact_path)
    state.setdefault("months", {})[month] = {
        "report_key": report_key,
        "delivery_status": "delivered",
        "artifact_path": str(artifact_path),
        "sent_messages": int(delivery.get("sent_count") or 0),
        "telegram_chat_ids": delivery.get("chat_ids") or [],
        "delivered_at": _utcnow().isoformat(),
    }
    _save_state(state_path, state)
    return {
        "status": "ok",
        "action": "deliver",
        "month": month,
        "report_key": report_key,
        "sent_messages": int(delivery.get("sent_count") or 0),
        "artifact_path": str(artifact_path),
    }


def render_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"monthly_online_demand_report: {summary.get('status', 'unknown')}",
        f"Месяц: {summary.get('month', '-')}",
        f"Действие: {summary.get('action', '-')}",
        f"Отправлено сообщений: {summary.get('sent_messages', 0)}",
    ]
    if summary.get("artifact_path"):
        lines.append(f"Артефакт: {summary.get('artifact_path')}")
    return "\n".join(lines)


def main() -> None:
    args = _parse_args()
    env = _load_env(
        os.getenv("MONTHLY_ONLINE_DEMAND_ENV_FILE")
        or os.getenv("OPENCLAW_ENV_FILE")
        or os.getenv("PRICING_ENV_FILE")
        or DEFAULT_LOCAL_ENV_FILE
    )
    month = args.month or previous_month()
    telegram_token = _resolve_telegram_token(env)
    chat_ids = _resolve_chat_ids(env)
    if not args.dry_run and (not telegram_token or not chat_ids):
        raise SystemExit(
            "Missing Telegram env: MONTHLY_ONLINE_DEMAND_TELEGRAM_TOKEN/chat id "
            "or weekly-manager-sales Telegram fallback."
        )

    state_path = Path(env.get("MONTHLY_ONLINE_DEMAND_STATE_PATH", DEFAULT_STATE_PATH))
    artifact_dir = Path(env.get("MONTHLY_ONLINE_DEMAND_REPORT_DIR", DEFAULT_ARTIFACT_DIR))

    def _deliver(*, message: str, artifact_path: Path) -> dict[str, Any]:
        del artifact_path
        assert telegram_token is not None
        sent_count = 0
        for chat_id in chat_ids:
            _send_telegram_message(token=telegram_token, chat_id=chat_id, message=message)
            sent_count += 1
            time.sleep(0.2)
        return {"sent_count": sent_count, "chat_ids": chat_ids}

    summary = sync_monthly_online_demand_report(
        env=env,
        month=month,
        state_path=state_path,
        artifact_dir=artifact_dir,
        deliver_message=_deliver,
        dry_run=args.dry_run,
        force=args.force,
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    print(render_summary(summary))


if __name__ == "__main__":
    main()
