#!/usr/bin/env python3
"""Pull weekly manager sales bundle from server A and deliver it via Openclaw on server B."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

DEFAULT_LOCAL_SOURCE_URL = "http://127.0.0.1:18080"
DEFAULT_LOCAL_ENV_FILE = "/opt/MM/pricing-service/.env"
DEFAULT_STATE_PATH = "/home/deploy/.openclaw/workspace/.data/weekly-manager-sales/state.json"
DEFAULT_ARTIFACT_DIR = "/home/deploy/.openclaw/workspace/.data/weekly-manager-sales/artifacts"


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
        description="Pull weekly manager sales report from server A and deliver it on server B."
    )
    parser.add_argument(
        "--week-end",
        dest="week_end",
        help="Closed week end in YYYY-MM-DD format; default is the last completed Sunday",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Render actions without side effects"
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable summary")
    return parser.parse_args()


def last_completed_week_end(today: date | None = None) -> date:
    anchor = today or date.today()
    return anchor - timedelta(days=(anchor.weekday() + 1) % 7)


def _parse_date(value: str | None) -> date:
    if value:
        return date.fromisoformat(value)
    return last_completed_week_end()


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


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


def _http_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int,
) -> bytes:
    request = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return response.read()


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


def _build_artifact_downloader(
    *,
    source_url: str,
    token: str,
    timeout: int,
    retries: int,
    retry_delay: float,
) -> Callable[[str], bytes]:
    base = source_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}

    def _download(artifact_url: str) -> bytes:
        if artifact_url.startswith("http://") or artifact_url.startswith("https://"):
            url = artifact_url
        else:
            url = f"{base}{artifact_url}"

        attempts = max(1, retries + 1)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return _http_bytes(url, headers=headers, timeout=timeout)
            except (
                urllib.error.HTTPError,
                urllib.error.URLError,
                TimeoutError,
            ) as error:
                last_error = error
                if attempt + 1 >= attempts:
                    break
                time.sleep(retry_delay)
        assert last_error is not None
        raise last_error

    return _download


def _health_status(health_payload: Any) -> str:
    if isinstance(health_payload, dict):
        return str(
            health_payload.get("status") or health_payload.get("freshness_status") or "unknown"
        )
    return "unknown"


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


def _state_key(report_key: str, revision: str) -> str:
    return f"{report_key}|r{revision}"


def _safe_path_chunk(value: str) -> str:
    safe = value.replace("/", "-").replace("\\", "-").strip()
    return safe or "unknown"


def _parse_chat_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [chunk.strip() for chunk in raw.split(",") if chunk.strip()]


def _resolve_chat_ids_for_artifact(env: dict[str, str], *, artifact_type: str) -> list[str]:
    normalized_type = artifact_type.strip().upper()
    scoped_chat_ids: str | None = None
    if normalized_type:
        scoped_chat_ids = env.get(
            f"WEEKLY_MANAGER_SALES_B_TELEGRAM_CHAT_ID_{normalized_type}"
        ) or env.get(f"WEEKLY_MANAGER_SALES_REPORT_TELEGRAM_CHAT_ID_{normalized_type}")
    return _parse_chat_ids(
        scoped_chat_ids
        or env.get("WEEKLY_MANAGER_SALES_B_TELEGRAM_CHAT_ID")
        or env.get("WEEKLY_MANAGER_SALES_REPORT_TELEGRAM_CHAT_ID")
        or env.get("WEEKLY_BUYER_DIGEST_ALERT_TELEGRAM_CHAT_ID")
    )


def _artifact_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _has_prior_delivered_revision(
    state: dict[str, Any],
    *,
    report_key: str,
    revision: str,
) -> bool:
    reports = state.get("reports") or {}
    if not isinstance(reports, dict):
        return False
    for item in reports.values():
        if not isinstance(item, dict):
            continue
        if item.get("delivery_status") != "delivered":
            continue
        if str(item.get("report_key") or "") != report_key:
            continue
        if str(item.get("revision") or "") == revision:
            continue
        return True
    return False


def _write_local_artifact(
    *,
    artifact_dir: Path,
    manifest: dict[str, Any],
    artifact: dict[str, Any],
    artifact_bytes: bytes,
) -> Path:
    period = manifest.get("period") or {}
    week_end = str(period.get("week_end") or "unknown-week")
    report_key = str(manifest.get("report_key") or "unknown-report")
    revision = str(manifest.get("revision") or "unknown-revision")
    artifact_type = str(artifact.get("artifact_type") or "artifact")
    filename = Path(str(artifact.get("filename") or f"{artifact_type}.xlsx")).name

    output_path = (
        artifact_dir
        / week_end
        / _safe_path_chunk(report_key)
        / _safe_path_chunk(revision)
        / f"{artifact_type}-{filename}"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(artifact_bytes)
    return output_path


def _send_telegram_document(
    *,
    token: str,
    chat_id: str,
    message: str,
    report_path: Path,
    timeout: int = 60,
) -> None:
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    boundary = f"----weeklymanagersales{int(time.time() * 1000)}"
    file_bytes = report_path.read_bytes()

    parts = []
    for name, value in (("chat_id", chat_id), ("caption", message)):
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())

    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        (
            f'Content-Disposition: form-data; name="document"; filename="{report_path.name}"\r\n'
            "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
        ).encode()
    )
    parts.append(file_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def sync_weekly_manager_sales_report(
    *,
    fetch_json: Callable[[str, dict[str, str]], Any],
    download_artifact: Callable[[str], bytes],
    deliver_artifact: Callable[..., dict[str, Any]],
    week_end: date,
    state_path: Path,
    artifact_dir: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    try:
        health = fetch_json(
            "/api/management/weekly-manager-sales-report/health",
            {"week_end": week_end.isoformat()},
        )
    except Exception as error:
        return {
            "status": "error",
            "week_end": week_end.isoformat(),
            "health_status": "unavailable",
            "error": str(error),
            "fetched": 0,
            "delivered": 0,
            "noop": 0,
            "failed": 1,
            "sent_documents": 0,
            "actions": [],
        }

    health_status = _health_status(health)
    if health_status != "ready":
        return {
            "status": "skipped",
            "week_end": week_end.isoformat(),
            "health_status": health_status,
            "error": (health.get("error") if isinstance(health, dict) else None),
            "fetched": 0,
            "delivered": 0,
            "noop": 0,
            "failed": 0,
            "sent_documents": 0,
            "actions": [],
        }

    try:
        payload = fetch_json(
            "/api/management/weekly-manager-sales-report",
            {"week_end": week_end.isoformat()},
        )
    except Exception as error:
        return {
            "status": "error",
            "week_end": week_end.isoformat(),
            "health_status": health_status,
            "error": str(error),
            "fetched": 0,
            "delivered": 0,
            "noop": 0,
            "failed": 1,
            "sent_documents": 0,
            "actions": [],
        }

    manifest = payload.get("payload") if isinstance(payload, dict) else None
    if not isinstance(manifest, dict):
        return {
            "status": "error",
            "week_end": week_end.isoformat(),
            "health_status": health_status,
            "error": "weekly manager sales manifest is empty",
            "fetched": 0,
            "delivered": 0,
            "noop": 0,
            "failed": 1,
            "sent_documents": 0,
            "actions": [],
        }

    report_key = str(manifest.get("report_key") or "")
    revision = str(manifest.get("revision") or "")
    state_key = _state_key(report_key, revision)
    state = _load_state(state_path)
    current = (state.get("reports") or {}).get(state_key)
    if isinstance(current, dict) and current.get("delivery_status") == "delivered":
        return {
            "status": "ok",
            "week_end": week_end.isoformat(),
            "health_status": health_status,
            "fetched": 1,
            "delivered": 0,
            "noop": 1,
            "failed": 0,
            "sent_documents": 0,
            "actions": [
                {
                    "action": "noop",
                    "report_key": report_key,
                    "revision": revision,
                    "state_key": state_key,
                }
            ],
        }

    artifacts = [item for item in manifest.get("artifacts") or [] if isinstance(item, dict)]
    is_correction = _has_prior_delivered_revision(
        state,
        report_key=report_key,
        revision=revision,
    )
    action_record: dict[str, Any] = {
        "action": "deliver" if not dry_run else "dry_run",
        "report_key": report_key,
        "revision": revision,
        "artifact_count": len(artifacts),
        "is_correction": is_correction,
    }

    if dry_run:
        return {
            "status": "ok",
            "week_end": week_end.isoformat(),
            "health_status": health_status,
            "fetched": 1,
            "delivered": 1,
            "noop": 0,
            "failed": 0,
            "sent_documents": 0,
            "actions": [action_record],
        }

    try:
        artifact_results: list[dict[str, Any]] = []
        sent_documents = 0
        for artifact in artifacts:
            artifact_url = str(artifact.get("artifact_url") or "")
            artifact_bytes = download_artifact(artifact_url)
            expected_sha256 = str(artifact.get("sha256") or "").strip()
            actual_sha256 = _artifact_sha256(artifact_bytes)
            if expected_sha256 and actual_sha256 != expected_sha256:
                raise RuntimeError(
                    "artifact checksum mismatch for "
                    f"{artifact.get('artifact_type') or 'artifact'}"
                )

            artifact_path = _write_local_artifact(
                artifact_dir=artifact_dir,
                manifest=manifest,
                artifact=artifact,
                artifact_bytes=artifact_bytes,
            )
            delivery_result = deliver_artifact(
                artifact_type=str(artifact.get("artifact_type") or ""),
                title=str(artifact.get("title") or ""),
                message=str(artifact.get("message") or ""),
                artifact_path=artifact_path,
                manifest=manifest,
                is_correction=is_correction,
            )
            sent_documents += int(delivery_result.get("sent_count") or 0)
            artifact_results.append(
                {
                    "artifact_type": artifact.get("artifact_type"),
                    "filename": artifact.get("filename"),
                    "artifact_path": str(artifact_path),
                    "sha256": actual_sha256,
                    "sent_count": int(delivery_result.get("sent_count") or 0),
                }
            )

        state.setdefault("reports", {})[state_key] = {
            "report_key": report_key,
            "revision": revision,
            "week_end": week_end.isoformat(),
            "delivery_status": "delivered",
            "is_correction": is_correction,
            "artifact_results": artifact_results,
            "sent_documents": sent_documents,
            "delivered_at": _utcnow().isoformat(),
        }
        _save_state(state_path, state)
        action_record["sent_documents"] = sent_documents
        action_record["artifacts"] = artifact_results
        return {
            "status": "ok",
            "week_end": week_end.isoformat(),
            "health_status": health_status,
            "fetched": 1,
            "delivered": 1,
            "noop": 0,
            "failed": 0,
            "sent_documents": sent_documents,
            "actions": [action_record],
        }
    except Exception as error:
        state.setdefault("reports", {})[state_key] = {
            "report_key": report_key,
            "revision": revision,
            "week_end": week_end.isoformat(),
            "delivery_status": "failed",
            "error": str(error),
            "updated_at": _utcnow().isoformat(),
        }
        _save_state(state_path, state)
        action_record["action"] = "failed"
        action_record["error"] = str(error)
        return {
            "status": "error",
            "week_end": week_end.isoformat(),
            "health_status": health_status,
            "error": str(error),
            "fetched": 1,
            "delivered": 0,
            "noop": 0,
            "failed": 1,
            "sent_documents": 0,
            "actions": [action_record],
        }


def render_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"weekly_manager_sales_reports_from_a: {summary.get('status', 'unknown')}",
        f"Неделя: {summary.get('week_end', '-')}",
        f"Источник: {summary.get('health_status', 'unknown')}",
        (
            "Получено: {fetched}; delivered: {delivered}; noop: {noop}; "
            "failed: {failed}; sent_documents: {sent_documents}"
        ).format(
            fetched=summary.get("fetched", 0),
            delivered=summary.get("delivered", 0),
            noop=summary.get("noop", 0),
            failed=summary.get("failed", 0),
            sent_documents=summary.get("sent_documents", 0),
        ),
    ]
    error = summary.get("error")
    if error:
        lines.append(f"Ошибка: {error}")
    for item in summary.get("actions") or []:
        lines.append(
            f"- {item.get('action')}: {item.get('report_key')} "
            f"(revision={item.get('revision')}, correction={item.get('is_correction', False)})"
        )
    return "\n".join(lines)


def main() -> None:
    args = _parse_args()
    env = _load_env(
        os.getenv("WEEKLY_MANAGER_SALES_B_ENV_FILE")
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
    download_artifact = _build_artifact_downloader(
        source_url=source_url,
        token=source_token,
        timeout=timeout,
        retries=retries,
        retry_delay=retry_delay,
    )

    telegram_token = (
        env.get("WEEKLY_MANAGER_SALES_B_TELEGRAM_TOKEN")
        or env.get("WEEKLY_MANAGER_SALES_REPORT_TELEGRAM_TOKEN")
        or env.get("WEEKLY_BUYER_DIGEST_ALERT_TELEGRAM_TOKEN")
        or env.get("TELEGRAM_TOKEN_MM")
    )
    configured_chat_ids = sorted(
        {
            *(_resolve_chat_ids_for_artifact(env, artifact_type="sales")),
            *(_resolve_chat_ids_for_artifact(env, artifact_type="employee")),
        }
    )
    if not args.dry_run and (not telegram_token or not configured_chat_ids):
        raise SystemExit(
            "Missing required env: WEEKLY_MANAGER_SALES_B_TELEGRAM_TOKEN|"
            "WEEKLY_MANAGER_SALES_REPORT_TELEGRAM_TOKEN|WEEKLY_BUYER_DIGEST_ALERT_TELEGRAM_TOKEN|"
            "TELEGRAM_TOKEN_MM and WEEKLY_MANAGER_SALES_B_TELEGRAM_CHAT_ID|"
            "WEEKLY_MANAGER_SALES_B_TELEGRAM_CHAT_ID_<ARTIFACT>|"
            "WEEKLY_MANAGER_SALES_REPORT_TELEGRAM_CHAT_ID|"
            "WEEKLY_BUYER_DIGEST_ALERT_TELEGRAM_CHAT_ID"
        )

    state_path = Path(env.get("WEEKLY_MANAGER_SALES_STATE_PATH", DEFAULT_STATE_PATH))
    artifact_dir = Path(env.get("WEEKLY_MANAGER_SALES_REPORT_DIR", DEFAULT_ARTIFACT_DIR))
    week_end = _parse_date(args.week_end)

    def _deliver(
        *,
        artifact_type: str,
        title: str,
        message: str,
        artifact_path: Path,
        manifest: dict[str, Any],
        is_correction: bool,
    ) -> dict[str, Any]:
        del manifest, title
        assert telegram_token is not None
        chat_ids = _resolve_chat_ids_for_artifact(env, artifact_type=artifact_type)
        if not chat_ids:
            raise RuntimeError(
                f"Missing Telegram chat IDs for weekly manager sales artifact_type={artifact_type}"
            )
        caption = message
        if is_correction:
            caption = "Исправленная версия weekly-отчета.\n\n" + caption
        sent_count = 0
        for chat_id in chat_ids:
            _send_telegram_document(
                token=telegram_token,
                chat_id=chat_id,
                message=caption,
                report_path=artifact_path,
            )
            sent_count += 1
        return {
            "artifact_type": artifact_type,
            "sent_count": sent_count,
        }

    summary = sync_weekly_manager_sales_report(
        fetch_json=fetch_json,
        download_artifact=download_artifact,
        deliver_artifact=_deliver,
        week_end=week_end,
        state_path=state_path,
        artifact_dir=artifact_dir,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    print(render_summary(summary))


if __name__ == "__main__":
    main()
