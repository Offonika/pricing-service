#!/usr/bin/env python3
"""Отправка ежедневного отчёта по схеме Розница -> Возврат -> Не розница в Telegram."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from app.services.return_scheme import (
    acknowledge_return_scheme_alert_batch_by_id,
    build_return_scheme_telegram_message,
    mark_return_scheme_incidents_notified_by_ids,
    send_return_scheme_telegram_report,
)


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Send return scheme monitoring report to Telegram")
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--output-file", required=True)
    args = parser.parse_args()

    direct_enabled = os.getenv("RETURN_SCHEME_DIRECT_TELEGRAM_ENABLED", "").strip().lower()
    if direct_enabled not in {"1", "true", "yes", "on"}:
        return

    token = os.getenv("RETURN_SCHEME_ALERT_TELEGRAM_TOKEN")
    chat_id = os.getenv("RETURN_SCHEME_ALERT_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return

    payload = _extract_payload(_read_lines(Path(args.output_file)))
    if not payload or args.exit_code != 0:
        return
    if int(payload.get("notification_incidents", 0) or 0) <= 0:
        return

    report_path_value = payload.get("report_path")
    if not report_path_value:
        return

    report_path = Path(report_path_value)
    if not report_path.exists():
        return

    try:
        send_return_scheme_telegram_report(
            token=token,
            chat_id=chat_id,
            message=build_return_scheme_telegram_message(payload),
            report_path=report_path,
        )
        batch_id = payload.get("batch_id")
        if batch_id:
            acknowledge_return_scheme_alert_batch_by_id(int(batch_id))
        else:
            mark_return_scheme_incidents_notified_by_ids(
                payload.get("notification_incident_ids", [])
            )
    except Exception as exc:  # pragma: no cover - не роняем cron
        print(f"Failed to send return scheme Telegram alert: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
