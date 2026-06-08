#!/usr/bin/env python3
"""Pull counterparty folder recommendations from server A and export CSV artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import os
import urllib.error
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

try:  # Deployed on Openclaw as a flat scripts directory.
    from weekly_kpi_reports_from_a import (  # type: ignore
        DEFAULT_LOCAL_ENV_FILE,
        DEFAULT_LOCAL_SOURCE_URL,
        _build_fetcher,
        _load_env,
    )
except ImportError:  # Local tests/imports from pricing-service repo.
    from infra.cron.weekly_kpi_reports_from_a import (
        DEFAULT_LOCAL_ENV_FILE,
        DEFAULT_LOCAL_SOURCE_URL,
        _build_fetcher,
        _load_env,
    )


DEFAULT_STATE_PATH = (
    "/home/deploy/.openclaw/workspace/.data/counterparty-folder-recommendations/state.json"
)
DEFAULT_ARTIFACT_DIR = (
    "/home/deploy/.openclaw/workspace/.data/counterparty-folder-recommendations/artifacts"
)
REPORT_ENDPOINT = "/api/management/counterparty-folder-recommendations"
STATUS_MOVE_RECOMMENDED = "move_recommended"
STATUS_OK = "ok"
STATUS_NO_OVERDUE = "no_overdue"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_VALUES = (
    STATUS_MOVE_RECOMMENDED,
    STATUS_OK,
    STATUS_NO_OVERDUE,
    STATUS_NEEDS_REVIEW,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pull counterparty folder recommendations and export a dry-run CSV."
    )
    parser.add_argument(
        "--date",
        dest="snapshot_date",
        help="Receivables snapshot date in YYYY-MM-DD format; default is today.",
    )
    parser.add_argument(
        "--status",
        choices=STATUS_VALUES,
        default=STATUS_MOVE_RECOMMENDED,
        help="Recommendation status filter; default is move_recommended.",
    )
    parser.add_argument("--limit", type=int, help="Optional max row count.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Fetch and summarize without writing state/artifact."
    )
    parser.add_argument("--force", action="store_true", help="Export even if this revision exists.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable summary.")
    return parser.parse_args()


def _parse_date(value: str | None) -> date:
    if value:
        return date.fromisoformat(value)
    return date.today()


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"reports": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {"reports": {}}
    payload.setdefault("reports", {})
    return payload


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _safe(value: Any) -> str:
    return str(value or "").strip()


def _safe_path_chunk(value: Any) -> str:
    safe = _safe(value).replace("/", "-").replace("\\", "-")
    return safe or "unknown"


def _format_dt(value: Any) -> str:
    if not value:
        return ""
    return str(value).replace("T", " ")[:19]


def _csv_number(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return str(value)


def _summary_int(report: dict[str, Any], key: str) -> int:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    try:
        return int(summary.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _state_key(snapshot_date: date, report_revision: str) -> str:
    return f"{snapshot_date.isoformat()}|{report_revision}"


def export_recommendations_csv(report: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    rows = report.get("payload") if isinstance(report.get("payload"), list) else []

    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["Отчет", "Контроль папок контрагентов по просроченной дебиторке"])
        writer.writerow(["Дата снапшота", report.get("as_of") or report.get("snapshot_date")])
        writer.writerow(["Ревизия", report.get("report_revision")])
        writer.writerow(["Всего в выгрузке", summary.get("total_count", len(rows))])
        writer.writerow(["К переносу", summary.get("move_recommended_count", 0)])
        writer.writerow(["На ручную проверку", summary.get("needs_review_count", 0)])
        writer.writerow([])
        writer.writerow(
            [
                "Контрагент",
                "Текущая папка",
                "Рекомендуемая папка",
                "Подразделение долга",
                "Сумма",
                "Документ",
                "Дата долга",
                "Просрочка дней",
                "Глубина кредита",
                "Дата просрочки",
                "Статус",
                "Причина проверки",
                "Менеджер долга",
                "Текущий менеджер",
                "Контрагент ref",
                "Документ ref",
            ]
        )
        for item in rows:
            if not isinstance(item, dict):
                continue
            document_label = " ".join(
                chunk
                for chunk in (
                    _safe(item.get("origin_document_number")),
                    _safe(item.get("origin_document_ref")),
                )
                if chunk
            )
            writer.writerow(
                [
                    item.get("counterparty_name") or item.get("counterparty_ref"),
                    item.get("current_folder_name"),
                    item.get("recommended_folder_name"),
                    item.get("debt_department_name"),
                    _csv_number(item.get("current_balance")),
                    document_label,
                    _format_dt(item.get("origin_document_date")),
                    item.get("overdue_days"),
                    item.get("credit_depth_days"),
                    _format_dt(item.get("due_date")),
                    item.get("status"),
                    item.get("review_reason"),
                    item.get("origin_manager_name"),
                    item.get("current_manager_name"),
                    item.get("counterparty_ref"),
                    item.get("origin_document_ref"),
                ]
            )

    return output_path


def sync_counterparty_folder_recommendations(
    *,
    fetch_json: Callable[[str, dict[str, str]], Any],
    snapshot_date: date,
    state_path: Path,
    artifact_dir: Path,
    status: str = STATUS_MOVE_RECOMMENDED,
    limit: int | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    params = {"date": snapshot_date.isoformat(), "status": status}
    if limit is not None:
        params["limit"] = str(limit)

    try:
        report = fetch_json(REPORT_ENDPOINT, params)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as error:
        return {
            "status": "error",
            "date": snapshot_date.isoformat(),
            "action": "failed",
            "error": str(error),
            "exported": 0,
            "noop": 0,
            "failed": 1,
        }

    if not isinstance(report, dict):
        return {
            "status": "error",
            "date": snapshot_date.isoformat(),
            "action": "failed",
            "error": "source returned non-object payload",
            "exported": 0,
            "noop": 0,
            "failed": 1,
        }

    report_revision = _safe(report.get("report_revision"))
    if not report_revision:
        return {
            "status": "error",
            "date": snapshot_date.isoformat(),
            "action": "failed",
            "error": "source payload has no report_revision",
            "exported": 0,
            "noop": 0,
            "failed": 1,
        }

    state = _load_state(state_path)
    key = _state_key(snapshot_date, report_revision)
    current = (state.get("reports") or {}).get(key)
    if isinstance(current, dict) and current.get("export_status") == "exported" and not force:
        return {
            "status": "ok",
            "date": snapshot_date.isoformat(),
            "report_revision": report_revision,
            "action": "noop",
            "reason": "already_exported",
            "artifact_path": current.get("artifact_path"),
            "move_recommended_count": _summary_int(report, "move_recommended_count"),
            "needs_review_count": _summary_int(report, "needs_review_count"),
            "exported": 0,
            "noop": 1,
            "failed": 0,
        }

    artifact_path = (
        artifact_dir
        / snapshot_date.isoformat()
        / f"counterparty-folder-{_safe_path_chunk(status)}-{report_revision}.csv"
    )
    action = {
        "status": "ok",
        "date": snapshot_date.isoformat(),
        "report_revision": report_revision,
        "action": "dry_run" if dry_run else "export",
        "artifact_path": str(artifact_path),
        "status_filter": status,
        "limit": limit,
        "source_snapshot_count": _summary_int(report, "source_snapshot_count"),
        "total_count": _summary_int(report, "total_count"),
        "move_recommended_count": _summary_int(report, "move_recommended_count"),
        "needs_review_count": _summary_int(report, "needs_review_count"),
        "exported": 0 if dry_run else 1,
        "noop": 0,
        "failed": 0,
    }
    if dry_run:
        return action

    export_recommendations_csv(report, artifact_path)
    state.setdefault("reports", {})[key] = {
        "export_status": "exported",
        "date": snapshot_date.isoformat(),
        "report_revision": report_revision,
        "status_filter": status,
        "limit": limit,
        "artifact_path": str(artifact_path),
        "exported_at": _utcnow().isoformat(),
    }
    _save_state(state_path, state)
    return action


def render_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"counterparty_folder_recommendations_from_a: {summary.get('status', 'unknown')}",
        f"Дата: {summary.get('date', '-')}",
        f"Действие: {summary.get('action', '-')}",
        (
            "exported: {exported}; noop: {noop}; failed: {failed}; "
            "к переносу: {move}; ручная проверка: {review}; всего строк: {total}"
        ).format(
            exported=summary.get("exported", 0),
            noop=summary.get("noop", 0),
            failed=summary.get("failed", 0),
            move=summary.get("move_recommended_count", 0),
            review=summary.get("needs_review_count", 0),
            total=summary.get("total_count", 0),
        ),
    ]
    if summary.get("report_revision"):
        lines.append(f"Ревизия: {summary['report_revision']}")
    if summary.get("artifact_path"):
        lines.append(f"Файл: {summary['artifact_path']}")
    if summary.get("reason"):
        lines.append(f"Причина: {summary['reason']}")
    if summary.get("error"):
        lines.append(f"Ошибка: {summary['error']}")
    return "\n".join(lines)


def main() -> None:
    args = _parse_args()
    env = _load_env(
        os.getenv("MANAGEMENT_B_ENV_FILE")
        or os.getenv("OPENCLAW_ENV_FILE")
        or os.getenv("PRICING_ENV_FILE")
        or DEFAULT_LOCAL_ENV_FILE
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

    timeout = int(env.get("MANAGEMENT_ADAPTER_TIMEOUT_SECONDS", "20"))
    retries = int(env.get("MANAGEMENT_ADAPTER_RETRIES", "2"))
    retry_delay = float(env.get("MANAGEMENT_ADAPTER_RETRY_DELAY_SECONDS", "1.0"))
    fetch_json = _build_fetcher(
        source_url=source_url,
        token=source_token,
        timeout=timeout,
        retries=retries,
        retry_delay=retry_delay,
    )

    summary = sync_counterparty_folder_recommendations(
        fetch_json=fetch_json,
        snapshot_date=_parse_date(args.snapshot_date),
        state_path=Path(
            env.get("COUNTERPARTY_FOLDER_RECOMMENDATIONS_STATE_PATH", DEFAULT_STATE_PATH)
        ),
        artifact_dir=Path(
            env.get("COUNTERPARTY_FOLDER_RECOMMENDATIONS_ARTIFACT_DIR", DEFAULT_ARTIFACT_DIR)
        ),
        status=args.status,
        limit=args.limit,
        dry_run=args.dry_run,
        force=args.force,
    )

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    print(render_summary(summary))


if __name__ == "__main__":
    main()
