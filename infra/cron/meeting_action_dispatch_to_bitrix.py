#!/usr/bin/env python3
"""Route AI follow-up digests into department chats in Bitrix Messenger."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from .meeting_action_digest import (
        DEFAULT_CACHE_DIR,
        DEFAULT_CACHE_TTL_SECONDS,
        OPEN_STATUSES,
        _default_anchor_date,
        _load_cached_rows,
        _load_env,
        _query_rows,
        _rows_cache_key,
        _write_cached_rows,
        classify_candidate,
    )
except ImportError:  # pragma: no cover - script execution path
    from meeting_action_digest import (
        DEFAULT_CACHE_DIR,
        DEFAULT_CACHE_TTL_SECONDS,
        OPEN_STATUSES,
        _default_anchor_date,
        _load_cached_rows,
        _load_env,
        _query_rows,
        _rows_cache_key,
        _write_cached_rows,
        classify_candidate,
    )


OWNER_LABELS = {
    "sales_support": "Отдел продаж",
    "service_quality": "Сервис и качество",
    "logistics_sales": "Логистика продаж",
    "procurement": "Отдел закупки",
}

PRIORITY_LABELS = {
    "high": "высокий",
    "medium": "средний",
    "low": "низкий",
}

TOPIC_LABELS = {
    "callback/дозвон": "дозвон",
    "возврат/обмен": "возвраты/обмены",
    "наличие/замена": "наличие",
    "общее follow-up": "общие кейсы",
    "статус заказа": "статусы заказов",
    "цена/промокод": "цены/промокоды",
    "отмена/коррекция заказа": "отмена/коррекция",
}

SUMMARY_NOISE_PATTERNS = [
    r"напоминаем, наш магазин .*?новом месте[!. ,]*",
    r"здравствуйте[!. ,]*",
    r"добрый день[!. ,]*",
    r"добрый вечер[!. ,]*",
    r"оставайтесь на линии[!. ,]*",
    r"вам ответит первый свободный сотрудник[!. ,]*",
    r"спасибо за ожидание[!. ,]*",
    r"к сожалению, сейчас все сотрудники заняты[!. ,]*",
    r"пожалуйста, оставайтесь на линии[!. ,]*",
    r"компания мастер мобайл[!. ,]*",
    r"company master mobile[!. ,]*",
    r"master mobile[!. ,]*",
    r"мастер[- ]?мобайл[а-яa-z\s-]*[,.!?]*",
    r"мастер[- ]?мобил[а-яa-z\s-]*[,.!?]*",
    r"мастер ввпд[,.!?]*",
    r"мастер омс[,.!?]*",
    r"меня зовут [а-яёa-z-]+[!. ,]*",
    r"удобно говорить вам[?.! ,]*",
    r"слушаю вас[!. ,]*",
    r"звонок[!. ,]*",
    r"алло[!. ,]*",
]


@dataclass(frozen=True)
class ChatRoute:
    owner_group: str
    dialog_id: str | None = None
    chat_name: str | None = None


def _message_hash(message: str) -> str:
    digest = hashlib.sha256((message or "").encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _delivery_dedupe_key(
    *,
    contour: str,
    dialog_id: str,
    owner_group: str,
    report_date: str,
    message_hash: str,
) -> str:
    return "|".join([contour, dialog_id, owner_group, report_date, message_hash])


def _load_delivery_state(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"version": 1, "deliveries": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "deliveries": []}
    if not isinstance(payload, dict):
        return {"version": 1, "deliveries": []}
    deliveries = payload.get("deliveries")
    if not isinstance(deliveries, list):
        payload["deliveries"] = []
    payload["version"] = 1
    return payload


def _save_delivery_state(path: Path | None, state: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    tmp.replace(path)


def _find_delivery(state: dict[str, Any], dedupe_key: str) -> dict[str, Any] | None:
    for item in state.get("deliveries") or []:
        if (
            isinstance(item, dict)
            and item.get("dedupe_key") == dedupe_key
            and item.get("status") == "sent"
        ):
            return item
    return None


def _record_delivery(
    state: dict[str, Any],
    *,
    dedupe_key: str,
    contour: str,
    dialog_id: str,
    owner_group: str,
    report_date: str,
    message_hash: str,
    message_id: str | None,
) -> None:
    state.setdefault("deliveries", []).append(
        {
            "dedupe_key": dedupe_key,
            "contour": contour,
            "dialog_id": dialog_id,
            "owner_group": owner_group,
            "report_date": report_date,
            "message_hash": message_hash,
            "status": "sent",
            "message_id": str(message_id or "") or None,
            "sent_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send meeting action digests into Bitrix department chats."
    )
    parser.add_argument("--date", dest="anchor_date", help="Anchor date in YYYY-MM-DD format")
    parser.add_argument(
        "--contour",
        choices=["cloud", "box"],
        default=(os.environ.get("BITRIX24_CHAT_CONTOUR") or "box").strip().lower(),
        help="Which Bitrix24 contour to use for chat delivery: cloud=old Bitrix24, box=new Bitrix box (default)",
    )
    parser.add_argument(
        "--owner-group",
        action="append",
        default=[],
        help="Dispatch only selected owner_group (repeat flag for multiple)",
    )
    parser.add_argument(
        "--new-limit",
        type=int,
        default=5,
        help="How many newest cases to include per owner group",
    )
    parser.add_argument(
        "--overdue-limit",
        type=int,
        default=5,
        help="How many overdue cases to include per owner group",
    )
    parser.add_argument(
        "--overdue-days",
        type=int,
        default=int(os.environ.get("MEETING_ACTION_OVERDUE_DAYS", "2")),
        help="Case age in days to treat as overdue",
    )
    parser.add_argument(
        "--cache-ttl-seconds",
        type=int,
        default=DEFAULT_CACHE_TTL_SECONDS,
        help="How long to reuse cached meeting action rows",
    )
    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_CACHE_DIR),
        help="Directory for cached meeting action rows",
    )
    parser.add_argument("--no-cache", action="store_true", help="Disable row cache")
    parser.add_argument("--dry-run", action="store_true", help="Render payloads without sending")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output")
    parser.add_argument(
        "--delivery-state-path",
        default=os.environ.get("MEETING_ACTION_DELIVERY_STATE_PATH", ""),
        help="Durable JSON state for Bitrix chat delivery dedupe",
    )
    return parser.parse_args()


def _normalize_dialog_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    if value.startswith("chat"):
        return value
    if value.isdigit():
        return f"chat{value}"
    return value


def _resolve_bitrix_base(env: dict[str, str], contour: str) -> str:
    if contour == "box":
        base = (env.get("BITRIX24_BOX_WEBHOOK_URL") or "").rstrip("/")
        if not base:
            raise SystemExit("BITRIX24_BOX_WEBHOOK_URL is missing")
        return base
    base = (env.get("BITRIX24_WEBHOOK_URL") or "").rstrip("/")
    if not base:
        raise SystemExit("BITRIX24_WEBHOOK_URL is missing")
    return base


def _b24_call(
    base: str, method: str, params: list[tuple[str, str]] | None = None, timeout: int = 60
) -> Any:
    query = urllib.parse.urlencode(params or [], doseq=True)
    url = f"{base}/{method}.json"
    if query:
        url = f"{url}?{query}"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _chat_title(item: dict[str, Any]) -> str:
    return (item.get("title") or item.get("name") or "").strip()


def _resolve_chat(base: str, route: ChatRoute, timeout: int = 60) -> dict[str, str]:
    if route.dialog_id:
        return {"dialog_id": route.dialog_id, "title": route.chat_name or route.dialog_id}
    if not route.chat_name:
        raise ValueError(f"Missing chat target for owner_group={route.owner_group}")

    exact_name = route.chat_name.strip().lower()
    try:
        data = _b24_call(base, "im.search.chat.list", [("FIND", route.chat_name)], timeout=timeout)
        items = data.get("result") or []
        rows: list[dict[str, str]] = []
        for item in items:
            title = _chat_title(item)
            dialog_id = _normalize_dialog_id(item.get("dialog_id") or item.get("id"))
            if title and dialog_id:
                rows.append({"dialog_id": dialog_id, "title": title})
        exact = [row for row in rows if row["title"].lower() == exact_name]
        if exact:
            return exact[0]
        if rows:
            return rows[0]
    except Exception:
        pass

    data = _b24_call(base, "im.recent.list", timeout=timeout)
    items = ((data.get("result") or {}).get("items")) or []
    exact: list[dict[str, str]] = []
    partial: list[dict[str, str]] = []
    for item in items:
        if item.get("type") != "chat":
            continue
        title = _chat_title(item)
        dialog_id = _normalize_dialog_id(item.get("id"))
        if not title or not dialog_id:
            continue
        row = {"dialog_id": dialog_id, "title": title}
        lowered = title.lower()
        if lowered == exact_name:
            exact.append(row)
        elif exact_name in lowered:
            partial.append(row)
    if exact:
        return exact[0]
    if partial:
        return partial[0]
    raise ValueError(
        f"Bitrix chat not found for owner_group={route.owner_group}: {route.chat_name}"
    )


def _load_routing(env: dict[str, str]) -> dict[str, ChatRoute]:
    routes: dict[str, ChatRoute] = {}

    raw_json = (env.get("MEETING_ACTION_BITRIX_CHAT_MAP") or "").strip()
    if raw_json:
        payload = json.loads(raw_json)
        if not isinstance(payload, dict):
            raise ValueError("MEETING_ACTION_BITRIX_CHAT_MAP must be a JSON object")
        for owner_group, item in payload.items():
            if isinstance(item, str):
                routes[str(owner_group)] = ChatRoute(
                    owner_group=str(owner_group),
                    dialog_id=_normalize_dialog_id(item),
                )
                continue
            if not isinstance(item, dict):
                raise ValueError(
                    f"Invalid route payload for owner_group={owner_group}: expected string or object"
                )
            routes[str(owner_group)] = ChatRoute(
                owner_group=str(owner_group),
                dialog_id=_normalize_dialog_id(item.get("dialog_id") or item.get("dialogId")),
                chat_name=str(item.get("chat_name") or item.get("chatName") or "").strip() or None,
            )

    prefixes = ("MEETING_ACTION_BITRIX_CHAT_",)

    def _owner_group_from_env_key(raw_owner: str) -> str:
        normalized = re.sub(r"_+", "_", raw_owner).strip("_").lower()
        if normalized.startswith("retail_"):
            tail = normalized[len("retail_") :]
            if tail:
                return f"retail:{tail}"
        return normalized

    for key, value in env.items():
        if not value:
            continue
        prefix = next((item for item in prefixes if key.startswith(item)), None)
        if not prefix:
            continue
        suffix = key[len(prefix) :]
        if suffix.endswith("_DIALOG_ID"):
            owner_group = _owner_group_from_env_key(suffix[: -len("_DIALOG_ID")])
            if owner_group:
                previous = routes.get(owner_group)
                routes[owner_group] = ChatRoute(
                    owner_group=owner_group,
                    dialog_id=_normalize_dialog_id(value),
                    chat_name=previous.chat_name if previous else None,
                )
        elif suffix.endswith("_NAME"):
            owner_group = _owner_group_from_env_key(suffix[: -len("_NAME")])
            if owner_group:
                previous = routes.get(owner_group)
                routes[owner_group] = ChatRoute(
                    owner_group=owner_group,
                    dialog_id=previous.dialog_id if previous else None,
                    chat_name=value.strip(),
                )

    normalized_routes: dict[str, ChatRoute] = {}
    for owner_group, route in routes.items():
        canonical = owner_group.lower()
        normalized_routes[canonical] = ChatRoute(
            owner_group=canonical,
            dialog_id=route.dialog_id,
            chat_name=route.chat_name,
        )
    return normalized_routes


def _load_disabled_owner_groups(env: dict[str, str]) -> set[str]:
    raw = (env.get("MEETING_ACTION_DISABLED_OWNER_GROUPS") or "").strip()
    if not raw:
        return set()
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _owner_label(owner_group: str) -> str:
    return OWNER_LABELS.get(owner_group, owner_group.replace("_", " "))


def _priority_rank(priority: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(priority, 3)


def _priority_label(priority: str) -> str:
    return PRIORITY_LABELS.get(priority, priority or "не указан")


def _topic_label(topic: str) -> str:
    return TOPIC_LABELS.get(topic, topic)


def _clean_case_summary(text: str, *, limit: int = 120) -> str:
    value = re.sub(r"\s+", " ", (text or "")).strip()
    lowered = value.lower()
    if "продолжение следует" in lowered:
        return "Нужен ручной разбор: в расшифровке недостаточно содержания."

    for pattern in SUMMARY_NOISE_PATTERNS:
        value = re.sub(pattern, " ", value, flags=re.IGNORECASE)

    value = re.sub(r"\s+", " ", value).strip(" ,.;:-")
    if not value:
        return "Нужен ручной разбор: AI не выделил короткую суть кейса."
    if len(value) <= limit:
        return value

    shortened = value[: limit - 1].rstrip(" ,.;:-")
    boundary = max(shortened.rfind("."), shortened.rfind(","), shortened.rfind(" "), 0)
    if boundary >= max(40, limit // 2):
        shortened = shortened[:boundary].rstrip(" ,.;:-")
    return f"{shortened}…"


def _is_low_signal_summary(text: str) -> bool:
    value = re.sub(r"\s+", " ", (text or "")).strip()
    if not value:
        return True
    lowered = value.lower()
    if lowered.startswith("нужен ручной разбор:"):
        return True
    if len(value) < 24:
        return True

    tokens = [token for token in re.split(r"[^а-яёa-z0-9]+", lowered) if token]
    if len(tokens) <= 3:
        return True
    unique_tokens = len(set(tokens))
    if unique_tokens <= 2:
        return True

    letters = re.findall(r"[а-яёa-z]", lowered)
    if len(letters) < 15:
        return True
    return False


def _select_case_summary(item: Any, *, owner_group: str) -> str:
    primary = _clean_case_summary(item.summary)
    if not _is_low_signal_summary(primary):
        return primary

    transcript = _clean_case_summary(item.transcript, limit=140)
    if not _is_low_signal_summary(transcript):
        return transcript

    if owner_group == "sales_support":
        return "Нужен ручной разбор: AI не выделил внятную суть клиентского обращения."
    return primary


def _rank_items_for_display(items: list[Any], *, owner_group: str) -> list[Any]:
    def _score(item: Any) -> tuple[int, int, int, int, datetime]:
        summary = _select_case_summary(item, owner_group=owner_group)
        low_signal = 1 if _is_low_signal_summary(summary) else 0
        if item.started_at_msk:
            timestamp = item.started_at_msk
        else:
            timestamp = datetime.min
        return (
            low_signal,
            _priority_rank(item.priority),
            0 if item.manager_id else 1,
            len(summary),
            timestamp,
        )

    return sorted(items, key=_score)


def build_owner_group_digest(
    rows: list[dict[str, str]],
    *,
    anchor_date: date,
    overdue_days: int,
    new_limit: int,
    overdue_limit: int,
) -> dict[str, dict[str, Any]]:
    topic_counts = defaultdict(int)
    report_date = anchor_date - timedelta(days=1)
    candidates = [item for item in (classify_candidate(row) for row in rows) if item is not None]

    grouped: dict[str, dict[str, list[Any]]] = defaultdict(
        lambda: {"new": [], "overdue": [], "all": []}
    )
    for item in candidates:
        bucket = grouped[item.owner_group]
        bucket["all"].append(item)
        topic_counts[(item.owner_group, item.topic)] += 1
        if item.started_at_msk.date() == report_date:
            bucket["new"].append(item)
        if (
            item.outcome in OPEN_STATUSES
            and (report_date - item.started_at_msk.date()).days >= overdue_days
        ):
            bucket["overdue"].append(item)

    digest: dict[str, dict[str, Any]] = {}
    for owner_group, bucket in grouped.items():
        new_items = _rank_items_for_display(bucket["new"], owner_group=owner_group)
        overdue_items = _rank_items_for_display(bucket["overdue"], owner_group=owner_group)
        digest[owner_group] = {
            "owner_group": owner_group,
            "owner_label": _owner_label(owner_group),
            "report_date": report_date.isoformat(),
            "new_count": len(bucket["new"]),
            "overdue_count": len(bucket["overdue"]),
            "oldest_overdue_date": (
                overdue_items[0].started_at_msk.date().isoformat() if overdue_items else None
            ),
            "top_topics": [
                {"topic": _topic_label(topic), "count": count}
                for topic, count in sorted(
                    (
                        (topic, count)
                        for (group_key, topic), count in topic_counts.items()
                        if group_key == owner_group
                    ),
                    key=lambda item: (-item[1], item[0]),
                )[:3]
            ],
            "new_items": [
                {
                    "started_at_msk": item.started_at_msk.strftime("%Y-%m-%d %H:%M"),
                    "topic": _topic_label(item.topic),
                    "priority": _priority_label(item.priority),
                    "manager_id": item.manager_id,
                    "summary": _select_case_summary(item, owner_group=owner_group),
                    "next_step": item.next_step,
                }
                for item in new_items[:new_limit]
            ],
            "overdue_items": [
                {
                    "started_at_msk": item.started_at_msk.strftime("%Y-%m-%d %H:%M"),
                    "topic": _topic_label(item.topic),
                    "priority": _priority_label(item.priority),
                    "manager_id": item.manager_id,
                    "summary": _select_case_summary(item, owner_group=owner_group),
                    "next_step": item.next_step,
                }
                for item in overdue_items[:overdue_limit]
            ],
        }
    return dict(sorted(digest.items()))


def render_owner_group_message(payload: dict[str, Any]) -> str:
    lines = [
        f"{payload['owner_label']} | кейсы по звонкам за {payload['report_date']}",
        f"Новые: {payload['new_count']}. Просроченные: {payload['overdue_count']}.",
    ]
    if payload.get("oldest_overdue_date"):
        lines.append(f"Самая старая просрочка: с {payload['oldest_overdue_date']}.")
    if payload.get("top_topics"):
        lines.append(
            "Основные темы: "
            + ", ".join(f"{item['topic']} {item['count']}" for item in payload["top_topics"])
            + "."
        )

    if payload["new_items"]:
        lines.append("Новые кейсы:")
        for index, item in enumerate(payload["new_items"], start=1):
            manager = f" | менеджер {item['manager_id']}" if item.get("manager_id") else ""
            lines.append(
                f"{index}) {item['started_at_msk']} | {item['topic']} | {item['priority']}{manager} | {item['summary']}"
            )

    if payload["overdue_items"]:
        lines.append("Просроченные кейсы:")
        for index, item in enumerate(payload["overdue_items"], start=1):
            manager = f" | менеджер {item['manager_id']}" if item.get("manager_id") else ""
            lines.append(
                f"{index}) {item['started_at_msk']} | {item['topic']} | {item['priority']}{manager} | {item['summary']}"
            )

    lines.append(
        "На сегодня: разберите самые старые кейсы, назначьте владельца, срок и короткий статус в этом чате."
    )
    return "\n".join(lines)


def _send_chat_message(base: str, *, dialog_id: str, message: str) -> dict[str, Any]:
    response = _b24_call(
        base,
        "im.message.add",
        [("DIALOG_ID", dialog_id), ("MESSAGE", message)],
    )
    result = response.get("result")
    message_id = None
    if isinstance(result, dict):
        message_id = result.get("MESSAGE_ID") or result.get("message_id") or result.get("ID")
    else:
        message_id = result
    return {"message_id": message_id}


def main() -> None:
    args = parse_args()
    env = _load_env(os.getenv("OPENCLAW_ENV_FILE") or "/home/deploy/.openclaw/.env")
    base = _resolve_bitrix_base(env, args.contour)
    database_url = env.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is missing")

    anchor_date = (
        datetime.strptime(args.anchor_date, "%Y-%m-%d").date()
        if args.anchor_date
        else _default_anchor_date()
    )

    lookback_days = int(env.get("MEETING_ACTION_LOOKBACK_DAYS", "7"))
    cache_dir = Path(args.cache_dir)
    cache_key = _rows_cache_key(anchor_date, lookback_days)
    rows = (
        None if args.no_cache else _load_cached_rows(cache_dir, cache_key, args.cache_ttl_seconds)
    )
    if rows is None:
        rows = _query_rows(
            database_url,
            lookback_days=lookback_days,
            env=env,
        )
        if not args.no_cache:
            _write_cached_rows(cache_dir, cache_key, rows)

    digest = build_owner_group_digest(
        rows,
        anchor_date=anchor_date,
        overdue_days=args.overdue_days,
        new_limit=args.new_limit,
        overdue_limit=args.overdue_limit,
    )
    routing = _load_routing(env)
    disabled_owner_groups = _load_disabled_owner_groups(env)
    delivery_state_path = Path(args.delivery_state_path) if args.delivery_state_path else None
    delivery_state = _load_delivery_state(delivery_state_path)

    requested_owner_groups = {item.strip() for item in args.owner_group if item.strip()}
    summary: list[dict[str, Any]] = []
    for owner_group, payload in digest.items():
        if requested_owner_groups and owner_group not in requested_owner_groups:
            continue
        if owner_group in disabled_owner_groups:
            summary.append(
                {
                    "owner_group": owner_group,
                    "status": "skipped_disabled",
                    "new_count": payload["new_count"],
                    "overdue_count": payload["overdue_count"],
                }
            )
            continue
        route = routing.get(owner_group)
        if route is None:
            summary.append(
                {
                    "owner_group": owner_group,
                    "status": "skipped_no_route",
                    "new_count": payload["new_count"],
                    "overdue_count": payload["overdue_count"],
                }
            )
            continue
        if payload["new_count"] == 0 and payload["overdue_count"] == 0:
            summary.append(
                {
                    "owner_group": owner_group,
                    "status": "skipped_empty",
                    "new_count": 0,
                    "overdue_count": 0,
                }
            )
            continue

        target = _resolve_chat(base, route)
        message = render_owner_group_message(payload)
        report_date = str(payload.get("report_date") or anchor_date.isoformat())
        message_hash = _message_hash(message)
        dedupe_key = _delivery_dedupe_key(
            contour=args.contour,
            dialog_id=target["dialog_id"],
            owner_group=owner_group,
            report_date=report_date,
            message_hash=message_hash,
        )
        existing_delivery = _find_delivery(delivery_state, dedupe_key)
        if existing_delivery is not None:
            summary.append(
                {
                    "owner_group": owner_group,
                    "status": "noop",
                    "dialog_id": target["dialog_id"],
                    "chat_title": target.get("title"),
                    "new_count": payload["new_count"],
                    "overdue_count": payload["overdue_count"],
                    "dedupe_key": dedupe_key,
                    "message_hash": message_hash,
                    "message_id": existing_delivery.get("message_id"),
                }
            )
            continue
        if args.dry_run:
            summary.append(
                {
                    "owner_group": owner_group,
                    "status": "dry_run",
                    "dialog_id": target["dialog_id"],
                    "chat_title": target.get("title"),
                    "new_count": payload["new_count"],
                    "overdue_count": payload["overdue_count"],
                    "dedupe_key": dedupe_key,
                    "message_hash": message_hash,
                    "message": message,
                }
            )
            continue

        result = _send_chat_message(base, dialog_id=target["dialog_id"], message=message)
        _record_delivery(
            delivery_state,
            dedupe_key=dedupe_key,
            contour=args.contour,
            dialog_id=target["dialog_id"],
            owner_group=owner_group,
            report_date=report_date,
            message_hash=message_hash,
            message_id=result.get("message_id"),
        )
        _save_delivery_state(delivery_state_path, delivery_state)
        summary.append(
            {
                "owner_group": owner_group,
                "status": "sent",
                "dialog_id": target["dialog_id"],
                "chat_title": target.get("title"),
                "new_count": payload["new_count"],
                "overdue_count": payload["overdue_count"],
                "dedupe_key": dedupe_key,
                "message_hash": message_hash,
                "message_id": result.get("message_id"),
            }
        )

    output = {
        "anchor_date": anchor_date.isoformat(),
        "contour": args.contour,
        "owners_processed": len(summary),
        "results": summary,
    }
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    for item in summary:
        owner_group = item["owner_group"]
        status = item["status"]
        if status == "dry_run":
            print(
                f"[DRY-RUN] {owner_group} -> {item['dialog_id']} ({item.get('chat_title') or 'unknown'})"
            )
            print(item["message"])
            print()
        elif status == "sent":
            print(
                f"[SENT] {owner_group} -> {item['dialog_id']} "
                f"(msg={item.get('message_id')}, new={item['new_count']}, overdue={item['overdue_count']})"
            )
        elif status == "noop":
            print(
                f"[NOOP] {owner_group} -> {item['dialog_id']} "
                f"(msg={item.get('message_id')}, new={item['new_count']}, overdue={item['overdue_count']})"
            )
        elif status == "skipped_no_route":
            print(
                f"[SKIP] {owner_group}: no Bitrix route configured "
                f"(new={item['new_count']}, overdue={item['overdue_count']})"
            )
        elif status == "skipped_disabled":
            print(
                f"[SKIP] {owner_group}: disabled by config "
                f"(new={item['new_count']}, overdue={item['overdue_count']})"
            )
        else:
            print(f"[SKIP] {owner_group}: empty digest")


if __name__ == "__main__":
    main()
