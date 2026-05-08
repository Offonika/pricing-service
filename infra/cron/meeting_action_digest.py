#!/usr/bin/env python3
"""Summarize AI-derived follow-up items from server B call transcripts/analysis."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from .calls_unified_projection import build_meeting_action_rows_sql
except ImportError:  # pragma: no cover - script execution path
    from calls_unified_projection import build_meeting_action_rows_sql

OPEN_STATUSES = {"pending_review", "callback", "support"}
DEFAULT_CACHE_TTL_SECONDS = 3 * 60 * 60
DEFAULT_CACHE_DIR = Path("/home/deploy/.openclaw/workspace/cache/morning_snapshots")
MSK_TZ = ZoneInfo("Europe/Moscow")


@dataclass
class CallActionCandidate:
    call_id: str
    started_at_msk: datetime
    source: str
    store_id: str
    manager_id: str
    outcome: str
    sentiment: str
    summary: str
    transcript: str
    topic: str
    owner_group: str
    priority: str
    next_step: str
    needs_follow_up: bool


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
    env["PGCLIENTENCODING"] = "SQL_ASCII"
    return env


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build AI meeting/call action digest for morning management reports."
    )
    parser.add_argument("--date", dest="anchor_date", help="Anchor date in YYYY-MM-DD format")
    parser.add_argument("--role-code", default="", help="Optional role code for prompt context")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text digest")
    parser.add_argument(
        "--cache-ttl-seconds",
        type=int,
        default=DEFAULT_CACHE_TTL_SECONDS,
        help="Сколько секунд использовать файловый snapshot утреннего окна",
    )
    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_CACHE_DIR),
        help="Директория для файловых snapshot/cache",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Отключить чтение и запись файлового cache",
    )
    return parser.parse_args()


def _default_anchor_date() -> date:
    return datetime.now(MSK_TZ).date() - timedelta(days=1)


def _query_rows(
    database_url: str, *, lookback_days: int, env: dict[str, str]
) -> list[dict[str, str]]:
    schema_sql = """
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS external_call_id text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS portal_number text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS call_failed_code text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS provider_name text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS resolved_manager_id bigint;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS resolved_manager_name text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS resolved_store_id text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS resolved_store_name text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS resolved_line_id text;
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS resolution_source text NOT NULL DEFAULT 'unresolved';
    ALTER TABLE calls ADD COLUMN IF NOT EXISTS manager_resolution_conflict boolean NOT NULL DEFAULT false;
    """
    schema_proc = subprocess.run(
        ["psql", database_url, "-v", "ON_ERROR_STOP=1", "-c", schema_sql],
        env=env,
        text=True,
        capture_output=True,
    )
    if schema_proc.returncode != 0:
        raise RuntimeError((schema_proc.stderr or schema_proc.stdout).strip())

    sql = build_meeting_action_rows_sql(lookback_days)
    proc = subprocess.run(
        ["psql", database_url, "-At", "-F", "\t", "-c", sql],
        env=env,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip())

    rows: list[dict[str, str]] = []
    for raw in proc.stdout.splitlines():
        parts = raw.split("\t", 8)
        if len(parts) != 9:
            continue
        rows.append(
            {
                "call_id": parts[0],
                "source": parts[1],
                "store_id": parts[2],
                "manager_id": parts[3],
                "started_at_msk": parts[4],
                "outcome": parts[5],
                "sentiment": parts[6],
                "summary": parts[7],
                "transcript": parts[8],
            }
        )
    return rows


def _rows_cache_key(anchor_date: date, lookback_days: int) -> str:
    payload = {
        "format_version": 2,
        "anchor_date": anchor_date.isoformat(),
        "lookback_days": lookback_days,
    }
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    digest = sha256(raw).hexdigest()[:16]
    return f"meeting_action_rows_{anchor_date.isoformat()}_{digest}.json"


def _load_cached_rows(
    cache_dir: Path, cache_key: str, ttl_seconds: int
) -> list[dict[str, str]] | None:
    path = cache_dir / cache_key
    if not path.exists():
        return None
    age_seconds = max(0.0, datetime.now().timestamp() - path.stat().st_mtime)
    if ttl_seconds >= 0 and age_seconds > ttl_seconds:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rows = payload.get("rows")
    return rows if isinstance(rows, list) else None


def _write_cached_rows(cache_dir: Path, cache_key: str, rows: list[dict[str, str]]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "cachedAt": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
    }
    (cache_dir / cache_key).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


TOPIC_RULES: list[tuple[str, list[str], str, str, str]] = [
    (
        "возврат/обмен",
        ["возврат", "обмен", "нерабоч", "брак", "гарант", "не работает", "дефект"],
        "service_quality",
        "high",
        "Проверить сценарий возврата/обмена и дать клиенту подтверждённое решение.",
    ),
    (
        "статус заказа",
        ["где мой товар", "статус заказа", "не забирали", "не позвонил", "когда будет", "задерж"],
        "logistics_sales",
        "high",
        "Подтвердить статус заказа и сообщить клиенту срок/точку выдачи.",
    ),
    (
        "цена/промокод",
        ["промокод", "цены", "старая сумма", "дорого", "скидк", "цена"],
        "sales_support",
        "medium",
        "Проверить цену/промокод в заказе и зафиксировать корректировку.",
    ),
    (
        "наличие/замена",
        ["нет в наличии", "нету", "налич", "под заказ", "замен"],
        "procurement",
        "medium",
        "Проверить наличие, срок поставки или предложить закупочное решение по позиции.",
    ),
    (
        "отмена/коррекция заказа",
        ["отменить", "не актуален", "скорректир", "оформил заказ", "корзину"],
        "sales_support",
        "medium",
        "Отменить или скорректировать заказ и подтвердить клиенту результат.",
    ),
    (
        "callback/дозвон",
        [
            "перезвони",
            "перезвон",
            "сотрудники заняты",
            "оставьте нам голосовое сообщение",
            "удобно говорить",
        ],
        "sales_support",
        "medium",
        "Сделать обратный звонок и закрыть коммуникацию с клиентом.",
    ),
]


def _compact(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def classify_candidate(row: dict[str, str]) -> CallActionCandidate | None:
    summary = _compact(row.get("summary", ""))
    transcript = _compact(row.get("transcript", ""))
    text = f"{summary} {transcript}".strip().lower()
    if not text:
        return None

    topic = "общее follow-up"
    owner_group = "sales_support"
    priority = "medium"
    next_step = "Разобрать кейс и назначить ответственного."
    matched = False
    for rule_topic, tokens, rule_owner, rule_priority, rule_step in TOPIC_RULES:
        if any(token in text for token in tokens):
            topic = rule_topic
            owner_group = rule_owner
            priority = rule_priority
            next_step = rule_step
            matched = True
            break

    outcome = (row.get("outcome") or "").strip().lower() or "unknown"
    needs_follow_up = matched or outcome in OPEN_STATUSES
    if not needs_follow_up:
        return None

    if row.get("source") == "retail_megafon" and row.get("store_id") not in {"", "unknown"}:
        owner_group = f"retail:{row['store_id']}"

    started_at_msk = datetime.strptime(row["started_at_msk"], "%Y-%m-%d %H:%M:%S")
    return CallActionCandidate(
        call_id=row["call_id"],
        started_at_msk=started_at_msk,
        source=row.get("source") or "bitrix",
        store_id=row.get("store_id") or "unknown",
        manager_id=row.get("manager_id") or "",
        outcome=outcome,
        sentiment=(row.get("sentiment") or "").strip().lower() or "unknown",
        summary=summary or transcript[:240],
        transcript=transcript,
        topic=topic,
        owner_group=owner_group,
        priority=priority,
        next_step=next_step,
        needs_follow_up=needs_follow_up,
    )


def build_meeting_action_digest(
    rows: list[dict[str, str]],
    *,
    anchor_date: date,
    role_code: str = "",
    overdue_days: int = 2,
    max_new_items: int = 5,
    max_overdue_items: int = 5,
) -> dict[str, Any]:
    report_date = anchor_date - timedelta(days=1)
    candidates = [item for item in (classify_candidate(row) for row in rows) if item is not None]

    new_items = [item for item in candidates if item.started_at_msk.date() == report_date]
    overdue_items = [
        item
        for item in candidates
        if item.outcome in OPEN_STATUSES
        and (report_date - item.started_at_msk.date()).days >= overdue_days
    ]

    def _priority_rank(item: CallActionCandidate) -> tuple[int, datetime]:
        rank = {"high": 0, "medium": 1, "low": 2}.get(item.priority, 3)
        return rank, item.started_at_msk

    new_items_sorted = sorted(new_items, key=_priority_rank)[:max_new_items]
    overdue_items_sorted = sorted(
        overdue_items, key=lambda item: (item.started_at_msk, item.priority)
    )[:max_overdue_items]

    by_topic = Counter(item.topic for item in new_items)
    by_outcome = Counter(item.outcome for item in candidates)
    by_owner = Counter(item.owner_group for item in new_items)

    status = "ready" if candidates else "empty"
    note = "AI action items сформированы из transcripts/call_analysis сервера B."
    if not candidates:
        note = "Нет AI action items: за период не найдено транскрибированных кейсов, требующих follow-up."

    return {
        "anchor_date": anchor_date.isoformat(),
        "report_date": report_date.isoformat(),
        "role_code": role_code,
        "status": status,
        "note": note,
        "total_candidates": len(candidates),
        "new_items_count": len(new_items),
        "overdue_items_count": len(overdue_items),
        "by_topic": dict(sorted(by_topic.items())),
        "by_outcome": dict(sorted(by_outcome.items())),
        "by_owner": dict(sorted(by_owner.items())),
        "new_items": [
            {
                "call_id": item.call_id,
                "started_at_msk": item.started_at_msk.strftime("%Y-%m-%d %H:%M"),
                "topic": item.topic,
                "owner_group": item.owner_group,
                "priority": item.priority,
                "outcome": item.outcome,
                "manager_id": item.manager_id,
                "summary": item.summary[:220],
                "next_step": item.next_step,
            }
            for item in new_items_sorted
        ],
        "overdue_items": [
            {
                "call_id": item.call_id,
                "started_at_msk": item.started_at_msk.strftime("%Y-%m-%d %H:%M"),
                "topic": item.topic,
                "owner_group": item.owner_group,
                "priority": item.priority,
                "outcome": item.outcome,
                "manager_id": item.manager_id,
                "summary": item.summary[:220],
                "next_step": item.next_step,
            }
            for item in overdue_items_sorted
        ],
    }


def render_meeting_action_digest(digest: dict[str, Any]) -> str:
    lines = [
        "AI action items / server B",
        f"Период: {digest['report_date']} (вчера относительно anchor {digest['anchor_date']}).",
        digest["note"],
        f"Новых follow-up кейсов: {digest['new_items_count']}.",
        f"Зависших open-кейсов: {digest['overdue_items_count']}.",
    ]
    if digest["by_topic"]:
        lines.append(
            "Темы: "
            + ", ".join(f"{topic}={count}" for topic, count in digest["by_topic"].items())
            + "."
        )
    if digest["by_outcome"]:
        lines.append(
            "Статусы: "
            + ", ".join(f"{outcome}={count}" for outcome, count in digest["by_outcome"].items())
            + "."
        )
    if digest["new_items"]:
        lines.append("Новые action items:")
        for index, item in enumerate(digest["new_items"], start=1):
            lines.append(
                f"{index}) {item['started_at_msk']} | {item['topic']} | owner={item['owner_group']} | "
                f"status={item['outcome']} | {item['summary']} | шаг: {item['next_step']}"
            )
    if digest["overdue_items"]:
        lines.append("Просроченные/open:")
        for index, item in enumerate(digest["overdue_items"], start=1):
            lines.append(
                f"{index}) {item['started_at_msk']} | {item['topic']} | owner={item['owner_group']} | "
                f"status={item['outcome']} | {item['summary']}"
            )
    return "\n".join(lines)


def main() -> None:
    args = _parse_args()
    env = _load_env(os.getenv("OPENCLAW_ENV_FILE") or "/home/deploy/.openclaw/.env")
    database_url = env.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("Missing required env: DATABASE_URL")

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
    digest = build_meeting_action_digest(
        rows,
        anchor_date=anchor_date,
        role_code=args.role_code,
        overdue_days=int(env.get("MEETING_ACTION_OVERDUE_DAYS", "2")),
    )

    if args.json:
        print(json.dumps(digest, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_meeting_action_digest(digest))


if __name__ == "__main__":
    main()
