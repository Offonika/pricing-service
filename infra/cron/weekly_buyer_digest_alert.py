#!/usr/bin/env python3
"""Отправка еженедельного обзора новинок в Telegram."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="ignore").splitlines()


def _extract_payload(lines: list[str]) -> dict[str, Any] | None:
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                continue
    return None


def _collect_tail(lines: list[str], limit: int = 5) -> str:
    tail = [line.strip() for line in lines if line.strip()]
    if not tail:
        return ""
    return "\n".join(tail[-limit:])


def _shorten(text: str, limit: int = 700) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _format_overview(payload: dict[str, Any], limit: int = 4) -> str:
    overview = payload.get("overview") or []
    preview = payload.get("preview") or ""
    lines: list[str] = []
    for idx, item in enumerate(overview[:limit], 1):
        if isinstance(item, str) and item.strip():
            lines.append(f"{idx}) {item.strip()}")
    if lines:
        return "\n".join(lines)
    if preview:
        return _shorten(str(preview), 800)
    return ""


def _clamp(text: str, limit: int = 4096) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _build_message(payload: dict[str, Any] | None, exit_code: int, tail: str) -> str:
    status_icon = "✅"
    if exit_code != 0 or (payload or {}).get("errors"):
        status_icon = "⚠️"
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    lines = [
        f"{status_icon} Еженедельный обзор новинок смартфонов для закупки",
        f"UTC: {now}",
    ]
    if payload:
        week_start = payload.get("week_start") or "?"
        week_end = payload.get("week_end") or "?"
        lines.extend([f"Период: {week_start} — {week_end}"])
        lines.append(
            f"Релизов: {payload.get('release_count', '?')} "
            f"(брендов: {payload.get('brand_count', '?')})"
        )
        lines.append(
            f"Digest: {payload.get('action', 'created')} "
            f"(id={payload.get('digest_id', '?')}, модель={payload.get('model') or 'n/a'})"
        )
        brands = payload.get("brands") or []
        if brands:
            lines.append(f"Бренды: {', '.join(brands[:8])}")
        overview = _format_overview(payload)
        if overview:
            lines.append("")
            lines.append("Кратко:")
            lines.append(overview)
    else:
        lines.append("Payload: не удалось распарсить JSON результата")

    if tail and exit_code != 0:
        lines.append("")
        lines.append("Хвост логов:")
        lines.append(tail)
    return _clamp("\n".join(lines))


def _send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with httpx.Client(timeout=10.0) as client:
        response = client.post(url, json={"chat_id": chat_id, "text": text})
        response.raise_for_status()


def _send_photo(token: str, chat_id: str, photo_path: Path, caption: str | None = None) -> None:
    if not photo_path.exists():
        raise FileNotFoundError(photo_path)
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    with photo_path.open("rb") as fh, httpx.Client(timeout=20.0) as client:
        response = client.post(
            url,
            data={"chat_id": chat_id, "caption": _shorten(caption or "", 1000) or None},
            files={"photo": fh},
        )
        response.raise_for_status()


def _send_document(token: str, chat_id: str, doc_path: Path, caption: str | None = None) -> None:
    if not doc_path.exists():
        raise FileNotFoundError(doc_path)
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    with doc_path.open("rb") as fh, httpx.Client(timeout=20.0) as client:
        response = client.post(
            url,
            data={"chat_id": chat_id, "caption": _shorten(caption or "", 1000) or None},
            files={"document": fh},
        )
        response.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser(description="Send weekly buyer digest status to Telegram")
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--output-file", required=True)
    args = parser.parse_args()

    token = os.getenv("WEEKLY_BUYER_DIGEST_ALERT_TELEGRAM_TOKEN") or os.getenv(
        "SMARTPHONE_RELEASES_ALERT_TELEGRAM_TOKEN"
    )
    chat_id = os.getenv("WEEKLY_BUYER_DIGEST_ALERT_TELEGRAM_CHAT_ID") or os.getenv(
        "SMARTPHONE_RELEASES_ALERT_TELEGRAM_CHAT_ID"
    )
    if not token or not chat_id:
        return

    output_path = Path(args.output_file)
    lines = _read_lines(output_path)
    payload = _extract_payload(lines)
    tail = _collect_tail(lines)
    message = _build_message(payload, args.exit_code, tail)

    cover_path = Path(payload.get("cover_path", "")) if payload else None
    image_path = Path(payload.get("image_path", "")) if payload else None
    markdown_path = Path(payload.get("markdown_path", "")) if payload else None

    try:
        photo_sent = False
        photo_candidate = cover_path if cover_path and cover_path.exists() else image_path
        if photo_candidate and photo_candidate.exists():
            _send_photo(token, chat_id, photo_candidate, caption=message)
            photo_sent = True
        if markdown_path and markdown_path.exists():
            _send_document(token, chat_id, markdown_path, caption="Полный текст в файле")
        # Дублируем текст отдельным сообщением, если не использовался как caption (нет фото).
        if not photo_sent:
            _send_telegram(token, chat_id, message)
    except Exception as exc:  # pragma: no cover - не роняем cron
        print(f"Failed to send weekly digest Telegram alert: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
