#!/usr/bin/env python3
"""Отправка статуса джобы мониторинга новинок в Telegram."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


def _read_lines(path: Path) -> List[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="ignore").splitlines()


def _extract_payload(lines: List[str]) -> Optional[Dict[str, Any]]:
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                continue
    return None


def _collect_tail(lines: List[str], limit: int = 5) -> str:
    tail = [line.strip() for line in lines if line.strip()]
    if not tail:
        return ""
    return "\n".join(tail[-limit:])


def _shorten(text: str, limit: int = 240) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _status_ru(status: Optional[str]) -> Optional[str]:
    if not status:
        return None
    mapping = {"rumor": "слух", "announced": "анонс", "released": "в продаже"}
    return mapping.get(str(status).lower().strip(), str(status))


def _format_stats(payload: Dict[str, Any]) -> List[str]:
    return [
        f"- Получено: {payload.get('fetched', '?')}",
        f"- Обработано: {payload.get('processed', '?')}",
        f"- Создано: {payload.get('created', '?')}",
        f"- Обновлено: {payload.get('updated', '?')}",
        f"- Пропущено: {payload.get('skipped', '?')}",
        f"- Ошибок: {payload.get('errors', '?')}",
    ]


def _format_sync(payload: Dict[str, Any]) -> List[str]:
    sync_keys = [
        ("sync_synced_models", "Новые модели"),
        ("sync_updated_models", "Обновлено моделей"),
        ("sync_keywords_created", "Ключевых слов создано"),
    ]
    lines: List[str] = []
    for key, label in sync_keys:
        if key in payload:
            lines.append(f"- {label}: {payload.get(key, '?')}")
    return lines


def _format_overview_item(item: Any, idx: int) -> str:
    if isinstance(item, str):
        text = item.strip()
        if not text:
            return ""
        return f"{idx}) {text}"
    if not isinstance(item, dict):
        return ""
    title = item.get("title") or "Новость"
    status = item.get("status_label") or _status_ru(item.get("status"))
    source = item.get("source")
    published_at = item.get("published_at")
    meta_bits = [bit for bit in (status, source, (published_at or "")[:10] or None) if bit]
    head = f"{idx}) {title}"
    if meta_bits:
        head += f" ({', '.join(meta_bits)})"
    summary = (item.get("summary") or "").strip()
    if summary:
        return f"{head}: {_shorten(summary)}"
    return head


def _format_overview(payload: Dict[str, Any], limit: int = 5) -> str:
    highlights = payload.get("highlights") or payload.get("news_overview") or []
    parts: List[str] = []
    for idx, item in enumerate(highlights[:limit], 1):
        formatted = _format_overview_item(item, idx)
        if formatted:
            parts.append(formatted)
    return "\n".join(parts)


def _build_message(job_name: str, payload: Optional[Dict[str, Any]], exit_code: int, tail: str) -> str:
    errors = (payload or {}).get("errors")
    status_icon = "✅" if exit_code == 0 and (errors is None or errors == 0) else "⚠️"
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    lines = [
        f"{status_icon} {job_name}",
        f"UTC: {now}",
        f"Код завершения: {exit_code}",
    ]
    if payload:
        lines.append("")
        lines.append("Статистика:")
        lines.extend(_format_stats(payload))
        sync_lines = _format_sync(payload)
        if sync_lines:
            lines.append("")
            lines.append("Синхронизация моделей:")
            lines.extend(sync_lines)
        overview = _format_overview(payload)
        if overview:
            lines.append("")
            lines.append("Обзор новостей:")
            lines.append(overview)
    else:
        lines.append("")
        lines.append("Payload: не удалось распарсить JSON результата")
    if tail:
        lines.append("")
        lines.append("Хвост логов:")
        lines.append(tail)
    return "\n".join(lines)


def _send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with httpx.Client(timeout=10.0) as client:
        response = client.post(url, json={"chat_id": chat_id, "text": text})
        response.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser(description="Send smartphone releases job status to Telegram")
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--output-file", required=True)
    parser.add_argument("--job-name", default="Мониторинг релизов смартфонов")
    args = parser.parse_args()

    token = os.getenv("SMARTPHONE_RELEASES_ALERT_TELEGRAM_TOKEN")
    chat_id = os.getenv("SMARTPHONE_RELEASES_ALERT_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return

    output_path = Path(args.output_file)
    lines = _read_lines(output_path)
    payload = _extract_payload(lines)
    tail = _collect_tail(lines)
    message = _build_message(args.job_name, payload, args.exit_code, tail)

    try:
        _send_telegram(token, chat_id, message)
    except Exception as exc:  # pragma: no cover - логируем и не роняем cron
        print(f"Failed to send Telegram alert: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
