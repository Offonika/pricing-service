"""Watchdog for the nightly competitor matching pipeline."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db import session_scope
from app.models import CompetitorFtpFile, CompetitorItem, ProductLiveCandidateCache
from app.models.competitor_item_match import CompetitorItemMatch


def _json_default(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _load_embedding_meta(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"exists": True, "error": type(exc).__name__}
    meta = payload.get("meta") or {}
    matrix_file = meta.get("matrix_file")
    matrix_path = path.parent / matrix_file if matrix_file else None
    return {
        "exists": True,
        "updated_at": meta.get("updated_at"),
        "model": meta.get("model"),
        "dim": meta.get("dim"),
        "matrix_file": matrix_file,
        "matrix_exists": bool(matrix_path and matrix_path.exists()),
    }


def _status_for_date(value: date | None, *, max_lag_days: int) -> str:
    if value is None:
        return "missing"
    if value < date.today() - timedelta(days=max_lag_days):
        return "stale"
    return "fresh"


def _status_for_datetime(value: datetime | None, *, max_lag_hours: int) -> str:
    if value is None:
        return "missing"
    if value.tzinfo is None:
        comparable = value.replace(tzinfo=UTC)
        now = datetime.now(UTC)
    else:
        comparable = value
        now = datetime.now(value.tzinfo)
    if comparable < now - timedelta(hours=max_lag_hours):
        return "stale"
    return "fresh"


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _freshest_datetime(*values: datetime | None) -> datetime | None:
    candidates = [(utc_value, value) for value in values if (utc_value := _as_utc(value))]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _latest_report_disables_embeddings(payload: dict[str, Any]) -> bool:
    return (
        str(payload.get("embeddings_enabled", "")).lower() in {"0", "false", "no"}
        or str(payload.get("embedding_status", "")).lower() == "disabled"
    )


def build_report(
    session: Session,
    *,
    embeddings_dir: Path,
    latest_report: Path,
    embeddings_enabled: bool,
    max_ftp_lag_days: int,
    max_runtime_lag_hours: int,
) -> dict[str, Any]:
    ftp_rows = list(
        session.execute(
            select(
                CompetitorFtpFile.source,
                func.max(CompetitorFtpFile.file_date),
                func.max(CompetitorFtpFile.ingested_at),
            )
            .group_by(CompetitorFtpFile.source)
            .order_by(CompetitorFtpFile.source)
        )
    )
    latest_payload: dict[str, Any] = {}
    if latest_report.exists():
        try:
            latest_payload = json.loads(latest_report.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            latest_payload = {"error": type(exc).__name__}
    latest_finished_at = _parse_datetime(latest_payload.get("finished_at"))
    ftp = {
        source: {
            "max_file_date": max_file_date,
            "max_ingested_at": max_ingested_at,
            "status": _status_for_date(max_file_date, max_lag_days=max_ftp_lag_days),
        }
        for source, max_file_date, max_ingested_at in ftp_rows
    }
    item_scraped_at = session.scalar(select(func.max(CompetitorItem.scraped_at)))
    item_updated_at = session.scalar(select(func.max(CompetitorItem.updated_at)))
    item_last_seen_at = session.scalar(select(func.max(CompetitorItem.last_seen_at)))
    item_activity_at = _freshest_datetime(item_updated_at, item_scraped_at, item_last_seen_at)
    match_updated_at = session.scalar(select(func.max(CompetitorItemMatch.updated_at)))
    match_activity_at = match_updated_at
    if (
        latest_payload.get("status") == "success"
        and _status_for_datetime(latest_finished_at, max_lag_hours=max_runtime_lag_hours) == "fresh"
    ):
        # A successful nightly run can legitimately produce no new match rows.
        match_activity_at = _freshest_datetime(match_updated_at, latest_finished_at)
    cache_computed_at = session.scalar(select(func.max(ProductLiveCandidateCache.computed_at)))
    effective_embeddings_enabled = embeddings_enabled and not _latest_report_disables_embeddings(
        latest_payload
    )

    checks = {
        "ftp": "fresh" if ftp and all(row["status"] == "fresh" for row in ftp.values()) else "bad",
        "competitor_items": _status_for_datetime(
            item_activity_at, max_lag_hours=max_runtime_lag_hours
        ),
        "matches": _status_for_datetime(match_activity_at, max_lag_hours=max_runtime_lag_hours),
        "live_candidate_cache": _status_for_datetime(
            cache_computed_at, max_lag_hours=max_runtime_lag_hours
        ),
        "nightly_report": latest_payload.get("status") or "missing",
    }
    embedding_meta = {
        "our_catalog": _load_embedding_meta(embeddings_dir / "our_catalog_index.json"),
        "competitor_items": _load_embedding_meta(embeddings_dir / "competitor_items_index.json"),
    }
    if not effective_embeddings_enabled:
        checks["embeddings"] = "disabled"
    else:
        embedding_statuses = []
        for meta in embedding_meta.values():
            if not meta.get("exists") or not meta.get("matrix_exists"):
                embedding_statuses.append("bad")
                continue
            embedding_statuses.append(
                _status_for_datetime(
                    _parse_datetime(meta.get("updated_at")),
                    max_lag_hours=max_runtime_lag_hours,
                )
            )
        if all(status == "fresh" for status in embedding_statuses):
            checks["embeddings"] = "fresh"
        elif any(status == "bad" for status in embedding_statuses):
            checks["embeddings"] = "bad"
        elif any(status == "stale" for status in embedding_statuses):
            checks["embeddings"] = "stale"
        else:
            checks["embeddings"] = "missing"
    ok = all(value in {"fresh", "success", "disabled"} for value in checks.values())
    if checks["nightly_report"] == "blocked_embeddings":
        ok = False
    return {
        "checked_at": datetime.now(UTC),
        "ok": ok,
        "checks": checks,
        "ftp": ftp,
        "competitor_item_max_scraped_at": item_scraped_at,
        "competitor_item_max_updated_at": item_updated_at,
        "competitor_item_max_last_seen_at": item_last_seen_at,
        "competitor_item_latest_activity_at": item_activity_at,
        "match_max_updated_at": match_updated_at,
        "match_latest_activity_at": match_activity_at,
        "live_candidate_cache_max_computed_at": cache_computed_at,
        "embeddings": embedding_meta,
        "embeddings_enabled": effective_embeddings_enabled,
        "nightly_latest": latest_payload,
    }


def _send_telegram(text: str) -> None:
    token = os.environ.get("COMPETITOR_MATCHING_ALERT_TELEGRAM_TOKEN")
    chat_id = os.environ.get("COMPETITOR_MATCHING_ALERT_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    response = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text[:3900]},
        timeout=20.0,
    )
    response.raise_for_status()


def _alert_text(report: dict[str, Any]) -> str:
    checks = report.get("checks") or {}
    bad = [f"{key}={value}" for key, value in checks.items() if value not in {"fresh", "success"}]
    return (
        "Competitor matching nightly требует внимания\n"
        f"Проверка: {_json_default(report.get('checked_at'))}\n"
        f"Проблемы: {', '.join(bad) if bad else 'unknown'}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Check competitor matching nightly freshness")
    parser.add_argument("--report-file", type=Path, help="Write watchdog JSON report")
    parser.add_argument("--latest-report", type=Path, default=None)
    parser.add_argument("--embeddings-dir", type=Path, default=None)
    parser.add_argument("--embeddings-disabled", action="store_true")
    parser.add_argument("--max-ftp-lag-days", type=int, default=1)
    parser.add_argument("--max-runtime-lag-hours", type=int, default=30)
    parser.add_argument("--no-telegram", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    embeddings_dir = args.embeddings_dir or Path(settings.embeddings_dir)
    latest_report = args.latest_report or Path("build/logs/competitor_matching_nightly_latest.json")
    with session_scope(read_only=True) as session:
        report = build_report(
            session,
            embeddings_dir=embeddings_dir,
            latest_report=latest_report,
            embeddings_enabled=(
                not args.embeddings_disabled
                and os.environ.get("COMPETITOR_MATCHING_EMBEDDINGS_ENABLED", "1") == "1"
            ),
            max_ftp_lag_days=args.max_ftp_lag_days,
            max_runtime_lag_hours=args.max_runtime_lag_hours,
        )

    output = json.dumps(report, ensure_ascii=False, indent=2, default=_json_default)
    if args.report_file:
        args.report_file.parent.mkdir(parents=True, exist_ok=True)
        args.report_file.write_text(output + "\n", encoding="utf-8")
    print(output)
    if not report["ok"] and not args.no_telegram:
        _send_telegram(_alert_text(report))
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
