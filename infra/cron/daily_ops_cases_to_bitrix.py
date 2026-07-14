#!/usr/bin/env python3
"""Send daily operations digests for procurement and logistics into Bitrix chats."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from .meeting_action_digest import (
        DEFAULT_CACHE_DIR,
        DEFAULT_CACHE_TTL_SECONDS,
        _load_cached_rows,
        _load_env,
        _query_rows,
        _rows_cache_key,
        _write_cached_rows,
        classify_candidate,
    )
    from .meeting_action_dispatch_to_bitrix import (
        _delivery_dedupe_key,
        _find_delivery,
        _load_delivery_state,
        _load_routing,
        _message_hash,
        _record_delivery,
        _resolve_bitrix_base,
        _resolve_chat,
        _save_delivery_state,
        _select_case_summary,
        _send_chat_message,
    )
except ImportError:  # pragma: no cover - script execution path
    from meeting_action_digest import (
        DEFAULT_CACHE_DIR,
        DEFAULT_CACHE_TTL_SECONDS,
        _load_cached_rows,
        _load_env,
        _query_rows,
        _rows_cache_key,
        _write_cached_rows,
        classify_candidate,
    )
    from meeting_action_dispatch_to_bitrix import (
        _delivery_dedupe_key,
        _find_delivery,
        _load_delivery_state,
        _load_routing,
        _message_hash,
        _record_delivery,
        _resolve_bitrix_base,
        _resolve_chat,
        _save_delivery_state,
        _select_case_summary,
        _send_chat_message,
    )

SUPPORTED_OWNER_GROUPS = ("procurement", "logistics_sales")
EXCLUDED_SUMMARY_PATTERNS = [
    r"по новому адресу",
    r"будем рады видеть вас",
]
STORE_PATTERNS = [
    ("Пятигорск", r"пятигорск"),
    ("Теплый Стан", r"тепл\w* стан|тёпл\w* стан"),
    ("Горбушкин Двор", r"горбуш"),
    ("Митино", r"митино"),
    ("Савеловский", r"савел"),
    ("Пресня", r"пресн"),
    ("Сайт", r"\bсайт\b"),
]
PART_PATTERNS = [
    ("запчасти", r"запчаст[ьи]"),
    ("дисплей", r"диспле[йя]"),
    ("шлейф", r"шлейф"),
    ("камера", r"камер[ауы]\b"),
    ("аккумулятор", r"аккумулятор"),
    ("лоток", r"лоток"),
    ("тачскрин", r"тачскрин"),
    ("экран", r"экран"),
]
MODEL_PATTERNS = [
    r"honor\s+[a-z0-9+\- ]{2,40}",
    r"iphone\s*\d{1,2}[a-z0-9+\- ]{0,20}",
    r"xiaomi\s+[a-z0-9+\- ]{2,40}",
    r"tecno\s+[a-z0-9+\- ]{2,40}",
    r"samsung\s+[a-z0-9+\- ]{2,40}",
    r"huawei\s+[a-z0-9+\- ]{2,40}",
    r"redmi\s+[a-z0-9+\- ]{2,40}",
    r"realme\s+[a-z0-9+\- ]{2,40}",
    r"poco\s+[a-z0-9+\- ]{2,40}",
    r"ipad\s+[a-z0-9+\- ]{2,40}",
]
ORDER_PATTERNS = [
    r"\b\d{2,3}[- ]\d{3}\b",
    r"\b\d{6}\b",
]
PROCUREMENT_COUNT_FORMS = ("новое обращение", "новых обращения", "новых обращений")
LOGISTICS_COUNT_FORMS = ("новый кейс", "новых кейса", "новых кейсов")


@dataclass
class DispatchResult:
    owner_group: str
    dialog_id: str
    status: str
    message_id: str | None
    report_date: str
    new_count: int
    chat_title: str | None = None
    message: str | None = None
    dedupe_key: str | None = None
    message_hash: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send daily operations digests for procurement and logistics to Bitrix."
    )
    parser.add_argument("--date", dest="anchor_date", help="Anchor date in YYYY-MM-DD format")
    parser.add_argument(
        "--owner-group",
        action="append",
        default=[],
        choices=list(SUPPORTED_OWNER_GROUPS),
        help="Dispatch only selected owner group (repeat for multiple)",
    )
    parser.add_argument(
        "--new-limit",
        type=int,
        default=5,
        help="How many newest cases to include per owner group",
    )
    parser.add_argument(
        "--contour",
        choices=["cloud", "box"],
        default=(os.environ.get("BITRIX24_CHAT_CONTOUR") or "box").strip().lower(),
        help="Which Bitrix24 contour to use for chat delivery",
    )
    parser.add_argument(
        "--cache-ttl-seconds",
        type=int,
        default=DEFAULT_CACHE_TTL_SECONDS,
        help="How long to reuse cached rows",
    )
    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_CACHE_DIR),
        help="Directory for cached rows",
    )
    parser.add_argument("--no-cache", action="store_true", help="Disable row cache")
    parser.add_argument("--dry-run", action="store_true", help="Render payloads without sending")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output")
    parser.add_argument(
        "--delivery-state-path",
        default=os.environ.get("DAILY_OPS_BITRIX_DELIVERY_STATE_PATH", ""),
        help="Durable JSON state for Bitrix chat delivery dedupe",
    )
    return parser.parse_args()


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _russian_count(value: int, forms: tuple[str, str, str]) -> str:
    if value % 10 == 1 and value % 100 != 11:
        form = forms[0]
    elif value % 10 in {2, 3, 4} and value % 100 not in {12, 13, 14}:
        form = forms[1]
    else:
        form = forms[2]
    return f"{value} {form}"


def _current_anchor_date() -> date:
    return datetime.now().date()


def _report_date(anchor_date: date) -> date:
    return anchor_date - timedelta(days=1)


def _load_rows(
    env: dict[str, str],
    *,
    anchor_date: date,
    cache_dir: Path,
    cache_ttl_seconds: int,
    no_cache: bool,
) -> list[dict[str, str]]:
    database_url = env.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is missing")
    lookback_days = int(env.get("MEETING_ACTION_LOOKBACK_DAYS", "7"))
    cache_key = _rows_cache_key(anchor_date, lookback_days)
    rows = None if no_cache else _load_cached_rows(cache_dir, cache_key, cache_ttl_seconds)
    if rows is None:
        rows = _query_rows(database_url, lookback_days=lookback_days, env=env)
        if not no_cache:
            _write_cached_rows(cache_dir, cache_key, rows)
    return rows


def _extract_store(text: str, fallback: str = "") -> str | None:
    cleaned_fallback = _normalize_text(fallback)
    fallback_lower = cleaned_fallback.lower()
    if (
        cleaned_fallback
        and cleaned_fallback not in {"unknown", "bitrix"}
        and not re.fullmatch(r"0x[0-9a-f]+", fallback_lower)
        and not fallback_lower.isdigit()
    ):
        return cleaned_fallback
    lowered = text.lower()
    for label, pattern in STORE_PATTERNS:
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            return label
    return None


def _clean_fragment(fragment: str) -> str:
    value = _normalize_text(fragment)
    value = re.sub(r"^[,.;: \-]+|[,.;: \-]+$", "", value)
    words = value.split()
    if len(words) > 6:
        value = " ".join(words[:6])
    return value.strip()


def _extract_model(text: str) -> str | None:
    lowered = text.lower()
    for pattern in MODEL_PATTERNS:
        match = re.search(pattern, lowered, flags=re.IGNORECASE)
        if match:
            return _clean_fragment(match.group(0)).replace("  ", " ").title()
    return None


def _extract_part(text: str) -> str | None:
    lowered = text.lower()
    for label, pattern in PART_PATTERNS:
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            return label
    return None


def _extract_order_number(text: str) -> str | None:
    for pattern in ORDER_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return match.group(0).replace(" ", "-")
    return None


def _normalize_product_label(part: str | None, model: str | None) -> str | None:
    if part and model:
        return f"{part} для {model}"
    if model:
        return model
    if part:
        return part
    return None


def _procurement_status(product_label: str | None) -> str:
    return "не подтверждено наличие" if product_label else "нужен ручной разбор"


def _logistics_status(text: str) -> str:
    lowered = text.lower()
    if any(
        token in lowered
        for token in ["не перезвони", "не написа", "никто не позвонил", "не позвонил"]
    ):
        return "нет обратной связи"
    if any(
        token in lowered
        for token in ["когда будет", "срок", "задерж", "готов", "отправ", "достав", "выдач"]
    ):
        return "ждет подтверждения срока"
    return "ждет подтверждения"


def _candidate_text(candidate: Any, *, owner_group: str) -> str:
    return _normalize_text(_select_case_summary(candidate, owner_group=owner_group))


def _is_excluded_summary(text: str) -> bool:
    lowered = text.lower()
    return any(
        re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in EXCLUDED_SUMMARY_PATTERNS
    )


def _build_candidates(
    rows: list[dict[str, str]], *, report_date: date, owner_groups: set[str]
) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for candidate in (classify_candidate(row) for row in rows):
        if candidate is None:
            continue
        if candidate.owner_group not in owner_groups:
            continue
        if candidate.started_at_msk.date() != report_date:
            continue
        text = _candidate_text(candidate, owner_group=candidate.owner_group)
        if _is_excluded_summary(text):
            continue
        grouped[candidate.owner_group].append(candidate)
    for owner_group in grouped:
        grouped[owner_group] = sorted(grouped[owner_group], key=lambda item: item.started_at_msk)
    return grouped


def render_procurement_message(report_date: date, candidates: list[Any], *, new_limit: int) -> str:
    lines = [
        f"Отдел закупки | дефицит по звонкам за {report_date.isoformat()}",
        "",
        (
            f"За день выявлено {_russian_count(len(candidates), PROCUREMENT_COUNT_FORMS)} по наличию. "
            "Ниже только позиции, где клиент искал товар или запчасть и не получил подтвержденное наличие."
        ),
        "",
    ]
    if not candidates:
        lines.append("Позиции дефицита: новых кейсов по наличию за день не выявлено.")
        return "\n".join(lines)

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in candidates:
        text = _candidate_text(candidate, owner_group="procurement")
        model = _extract_model(text)
        part = _extract_part(text)
        product_label = _normalize_product_label(part, model)
        store = _extract_store(text, candidate.store_id) or "подразделение не указано"
        key = (product_label or "нужен ручной разбор", store)
        if key not in grouped:
            grouped[key] = {
                "count": 0,
                "status": _procurement_status(product_label),
                "comment": text,
            }
        grouped[key]["count"] += 1

    lines.append("Позиции дефицита:")
    for index, ((product_label, store), payload) in enumerate(
        sorted(grouped.items(), key=lambda item: (-item[1]["count"], item[0][0], item[0][1]))[
            :new_limit
        ],
        start=1,
    ):
        label = product_label if product_label != "нужен ручной разбор" else "товар не распознан"
        count = payload["count"]
        status = payload["status"]
        comment = payload["comment"]
        lines.append(
            f"{index}. {label} | {store} | обращений: {count} | статус: {status} | комментарий: {comment}"
        )

    lines.extend(
        [
            "",
            "Фокус на сегодня:",
            "- проверить фактические остатки и доступность под заказ по позициям из списка;",
            "- кейсы без распознанной модели быстро дослушать и вынести точный товар в закупку;",
            "- повторяющиеся запросы по одной позиции считать сигналом неудовлетворенного спроса.",
        ]
    )
    return "\n".join(lines)


def _logistics_identifier(text: str) -> str:
    order_number = _extract_order_number(text)
    product = _normalize_product_label(_extract_part(text), _extract_model(text))
    if order_number and product:
        return f"Заказ {order_number}, {product}"
    if order_number:
        return f"Заказ {order_number}"
    if product:
        return product
    return "Заказ без номера"


def render_logistics_message(report_date: date, candidates: list[Any], *, new_limit: int) -> str:
    lines = [
        f"Логистика | новые кейсы за {report_date.isoformat()}",
        "",
        (
            f"За день выявлено {_russian_count(len(candidates), LOGISTICS_COUNT_FORMS)}. "
            "Ниже только обращения, где клиент ждет статус заказа, срок, выдачу, отправку или обратную связь."
        ),
        "",
    ]
    if not candidates:
        lines.append("Проблемные заказы: новых логистических кейсов за день не выявлено.")
        return "\n".join(lines)

    lines.append("Проблемные заказы:")
    for index, candidate in enumerate(candidates[:new_limit], start=1):
        text = _candidate_text(candidate, owner_group="logistics_sales")
        identifier = _logistics_identifier(text)
        store = _extract_store(text, candidate.store_id)
        status = _logistics_status(text)
        manager = (
            f"менеджер {candidate.manager_id}" if candidate.manager_id else "менеджер не указан"
        )
        if store:
            lines.append(
                f"{index}. {identifier} | {store} | {manager} | статус: {status} | комментарий: {text}"
            )
        else:
            lines.append(
                f"{index}. {identifier} | {manager} | статус: {status} | комментарий: {text}"
            )

    lines.extend(
        [
            "",
            "Фокус на сегодня:",
            "- дать подтвержденный статус по всем заказам из списка;",
            "- отдельно закрыть кейсы, где клиент уже ждет обратную связь;",
            "- неясные обращения быстро переводить в ручной разбор записи.",
        ]
    )
    return "\n".join(lines)


def build_messages(
    rows: list[dict[str, str]], *, anchor_date: date, owner_groups: set[str], new_limit: int
) -> dict[str, dict[str, Any]]:
    report_date = _report_date(anchor_date)
    grouped = _build_candidates(rows, report_date=report_date, owner_groups=owner_groups)
    payloads: dict[str, dict[str, Any]] = {}
    for owner_group in sorted(owner_groups):
        candidates = grouped.get(owner_group, [])
        if owner_group == "procurement":
            message = render_procurement_message(report_date, candidates, new_limit=new_limit)
            owner_label = "Отдел закупки"
        elif owner_group == "logistics_sales":
            message = render_logistics_message(report_date, candidates, new_limit=new_limit)
            owner_label = "Логистика"
        else:
            continue
        payloads[owner_group] = {
            "owner_group": owner_group,
            "owner_label": owner_label,
            "report_date": report_date.isoformat(),
            "new_count": len(candidates),
            "message": message,
        }
    return payloads


def main() -> None:
    args = parse_args()
    env = _load_env(os.getenv("OPENCLAW_ENV_FILE") or "/home/deploy/.openclaw/.env")
    anchor_date = (
        datetime.strptime(args.anchor_date, "%Y-%m-%d").date()
        if args.anchor_date
        else _current_anchor_date()
    )
    owner_groups = set(args.owner_group or SUPPORTED_OWNER_GROUPS)
    cache_dir = Path(args.cache_dir)
    rows = _load_rows(
        env,
        anchor_date=anchor_date,
        cache_dir=cache_dir,
        cache_ttl_seconds=args.cache_ttl_seconds,
        no_cache=args.no_cache,
    )
    payloads = build_messages(
        rows, anchor_date=anchor_date, owner_groups=owner_groups, new_limit=args.new_limit
    )

    base = _resolve_bitrix_base(env, args.contour)
    routing = _load_routing(env)
    delivery_state_path = Path(args.delivery_state_path) if args.delivery_state_path else None
    delivery_state = _load_delivery_state(delivery_state_path)
    results: list[DispatchResult] = []
    for owner_group in sorted(owner_groups):
        payload = payloads.get(owner_group)
        route = routing.get(owner_group)
        if payload is None:
            continue
        if route is None:
            raise SystemExit(f"Bitrix route is missing for owner_group={owner_group}")
        resolved = _resolve_chat(base, route)
        message_hash = _message_hash(payload["message"])
        dedupe_key = _delivery_dedupe_key(
            contour=args.contour,
            dialog_id=resolved["dialog_id"],
            owner_group=owner_group,
            report_date=payload["report_date"],
            message_hash=message_hash,
        )
        existing_delivery = _find_delivery(delivery_state, dedupe_key)
        if existing_delivery is not None:
            results.append(
                DispatchResult(
                    owner_group=owner_group,
                    dialog_id=resolved["dialog_id"],
                    chat_title=resolved.get("title"),
                    status="noop",
                    message_id=str(existing_delivery.get("message_id") or "") or None,
                    report_date=payload["report_date"],
                    new_count=payload["new_count"],
                    dedupe_key=dedupe_key,
                    message_hash=message_hash,
                )
            )
            continue
        if args.dry_run:
            results.append(
                DispatchResult(
                    owner_group=owner_group,
                    dialog_id=resolved["dialog_id"],
                    chat_title=resolved.get("title"),
                    status="dry_run",
                    message_id=None,
                    report_date=payload["report_date"],
                    new_count=payload["new_count"],
                    message=payload["message"],
                    dedupe_key=dedupe_key,
                    message_hash=message_hash,
                )
            )
            continue
        send_result = _send_chat_message(
            base, dialog_id=resolved["dialog_id"], message=payload["message"]
        )
        _record_delivery(
            delivery_state,
            dedupe_key=dedupe_key,
            contour=args.contour,
            dialog_id=resolved["dialog_id"],
            owner_group=owner_group,
            report_date=payload["report_date"],
            message_hash=message_hash,
            message_id=str(send_result.get("message_id") or "") or None,
        )
        _save_delivery_state(delivery_state_path, delivery_state)
        results.append(
            DispatchResult(
                owner_group=owner_group,
                dialog_id=resolved["dialog_id"],
                chat_title=resolved.get("title"),
                status="sent",
                message_id=str(send_result.get("message_id") or "") or None,
                report_date=payload["report_date"],
                new_count=payload["new_count"],
                dedupe_key=dedupe_key,
                message_hash=message_hash,
            )
        )

    if args.json:
        print(
            json.dumps(
                {
                    "anchor_date": anchor_date.isoformat(),
                    "contour": args.contour,
                    "results": [result.__dict__ for result in results],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    for result in results:
        if result.status == "dry_run":
            print(
                f"[DRY-RUN] {result.owner_group} -> {result.dialog_id} ({result.chat_title or 'unknown'})"
            )
            print(result.message or "")
            continue
        if result.status == "noop":
            print(
                f"[NOOP] {result.owner_group} -> {result.dialog_id} "
                f"message_id={result.message_id or 'unknown'} new={result.new_count}"
            )
            continue
        print(
            f"[SENT] {result.owner_group} -> {result.dialog_id} "
            f"message_id={result.message_id or 'unknown'} new={result.new_count}"
        )


if __name__ == "__main__":
    main()
