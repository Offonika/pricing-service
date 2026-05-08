#!/usr/bin/env python3
"""Consume frozen weekly KPI report manifests from server A and deliver them on server B."""

from __future__ import annotations

import argparse
import base64
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
DEFAULT_STATE_PATH = "/home/deploy/.openclaw/workspace/.data/weekly-kpi-reports/state.json"
DEFAULT_ARTIFACT_DIR = "/home/deploy/.openclaw/workspace/.data/weekly-kpi-reports/artifacts"
BITRIX_TARGET_AUTO = "auto"
BITRIX_TARGET_BOX = "box"
BITRIX_TARGET_CLOUD = "cloud"
BITRIX_TARGET_VALUES = {BITRIX_TARGET_AUTO, BITRIX_TARGET_BOX, BITRIX_TARGET_CLOUD}
SIGNAL_LABELS = {
    "good": "Хорошо",
    "attention": "Требует внимания",
    "critical": "Критично",
    "blocked": "Заблокировано",
    "neutral": "Нейтрально",
}
RETAIL_DIRECTOR_ROLE_CODES = {"retail_director", "retail_network_head"}


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
        description="Pull weekly KPI reports from server A and deliver them to Bitrix."
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


def _state_key(report_key: str, revision: int) -> str:
    return f"{report_key}|r{revision}"


def _safe_path_chunk(value: str) -> str:
    safe = value.replace("/", "-").replace("\\", "-").strip()
    return safe or "unknown"


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _normalize_target(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in BITRIX_TARGET_VALUES:
        return normalized
    return BITRIX_TARGET_AUTO


def _resolve_target_user_id(manifest: dict[str, Any], *, target_mode: str) -> int | None:
    employee = manifest.get("employee") or {}
    cloud_id = _coerce_int(employee.get("bitrix_user_id"))
    box_id = _coerce_int(employee.get("bitrix_box_user_id"))
    mode = _normalize_target(target_mode)
    if mode == BITRIX_TARGET_BOX:
        return box_id
    if mode == BITRIX_TARGET_CLOUD:
        return cloud_id
    return box_id or cloud_id


def _health_status(health_payload: Any) -> str:
    if isinstance(health_payload, dict):
        return str(
            health_payload.get("status") or health_payload.get("freshness_status") or "unknown"
        )
    return "unknown"


def _to_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    rendered = str(value).replace(" ", "").replace(",", ".").strip()
    if not rendered:
        return 0.0
    try:
        return float(rendered)
    except ValueError:
        return 0.0


def _format_money(value: float) -> str:
    return f"{value:,.0f} ₽".replace(",", " ")


def _format_percent_value(value: Any, *, decimals: int = 4) -> str:
    rendered = f"{_to_float(value):.{decimals}f}".replace(".", ",")
    return f"{rendered}%"


def _is_retail_director_manifest(manifest: dict[str, Any]) -> bool:
    employee = manifest.get("employee") or {}
    role_code = str(employee.get("role_code") or "").strip().lower()
    position_code = str(employee.get("position_code") or "").strip().lower()
    return role_code in RETAIL_DIRECTOR_ROLE_CODES or position_code in RETAIL_DIRECTOR_ROLE_CODES


def _closed_month_for_manifest(manifest: dict[str, Any]) -> str | None:
    period = manifest.get("period") or {}
    week_end_raw = str(period.get("week_end") or "").strip()
    if not week_end_raw:
        return None
    week_end = date.fromisoformat(week_end_raw)
    previous_month = (week_end.replace(day=1) - timedelta(days=1)).replace(day=1)
    return previous_month.strftime("%Y-%m")


def _render_weekly_kpi_overview(
    manifest: dict[str, Any],
    *,
    is_correction: bool,
    retail_director_monthly_kpi: dict[str, Any] | None = None,
) -> str:
    summary_payload = manifest.get("summary_payload") or {}
    if not isinstance(summary_payload, dict):
        summary_payload = {}
    header = summary_payload.get("header") or {}
    if not isinstance(header, dict):
        header = {}
    employee = manifest.get("employee") or {}
    period = manifest.get("period") or {}

    title = str(header.get("title") or "").strip()
    if not title:
        week_start = period.get("week_start") or "?"
        week_end = period.get("week_end") or "?"
        title = f"Отчет за неделю {week_start} — {week_end}"

    subtitle = str(header.get("subtitle") or "").strip()
    if not subtitle:
        employee_name = employee.get("employee_name") or "Сотрудник"
        position_name = (
            employee.get("position_name") or employee.get("role_code") or "роль не указана"
        )
        subtitle = f"{employee_name} / {position_name}"

    signal = str(
        summary_payload.get("overall_signal") or manifest.get("overall_signal") or "neutral"
    )
    wins = [str(item).strip() for item in summary_payload.get("wins") or [] if str(item).strip()]
    risks = [str(item).strip() for item in summary_payload.get("risks") or [] if str(item).strip()]
    next_actions = [
        str(item).strip() for item in summary_payload.get("next_actions") or [] if str(item).strip()
    ]

    lines = []
    if is_correction:
        lines.append("Исправленная версия weekly KPI-отчета.")
        lines.append("")
    lines.append(title)
    lines.append(subtitle)
    lines.append(f"Общий сигнал: {SIGNAL_LABELS.get(signal, signal)}")

    if wins:
        lines.append("")
        lines.append("Сильные стороны:")
        lines.extend(f"- {item}" for item in wins[:3])

    if risks:
        lines.append("")
        lines.append("Зоны внимания:")
        lines.extend(f"- {item}" for item in risks[:3])

    if next_actions:
        lines.append("")
        lines.append("Следующие шаги:")
        lines.extend(f"- {item}" for item in next_actions[:3])

    if retail_director_monthly_kpi:
        month = str(retail_director_monthly_kpi.get("month") or "").strip()
        lines.append("")
        lines.append(f"Закрытый месяц {month}:")
        lines.append(
            "- Потери: "
            f"списания {_format_money(_to_float(retail_director_monthly_kpi.get('writeoff_amount')))}, "
            f"оприходования {_format_money(_to_float(retail_director_monthly_kpi.get('receipt_amount')))}, "
            f"чистые потери {_format_money(_to_float(retail_director_monthly_kpi.get('shrinkage_amount')))}, "
            f"уровень {_format_percent_value(retail_director_monthly_kpi.get('shrinkage_pct'))}."
        )
        lines.append(
            "- Премия: "
            f"индекс KPI {str(retail_director_monthly_kpi.get('kpi_index_sum') or '')}, "
            f"бонус {_format_money(_to_float(retail_director_monthly_kpi.get('kpi_bonus_amount')))}, "
            f"к выплате {_format_money(_to_float(retail_director_monthly_kpi.get('to_pay')))}."
        )

    return "\n".join(lines)


def _write_local_artifact(
    *,
    artifact_dir: Path,
    manifest: dict[str, Any],
    artifact_bytes: bytes,
) -> Path:
    period = manifest.get("period") or {}
    report_key = str(manifest.get("report_key") or "unknown-report")
    revision = int(manifest.get("revision") or 0)
    week_end = str(period.get("week_end") or "unknown-week")
    filename = f"weekly-kpi-{_safe_path_chunk(report_key)}-r{revision}.xlsx"
    output_path = artifact_dir / week_end / _safe_path_chunk(report_key) / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(artifact_bytes)
    return output_path


def _b24_call(base_url: str, method: str, params: list[tuple[str, str]]) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{method}.json"
    data = None
    if params:
        data = urllib.parse.urlencode(params, doseq=True).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("error"):
        raise RuntimeError(
            f"Bitrix24 {method}: {payload['error']} {payload.get('error_description', '')}"
        )
    return payload


def _upload_b24_disk_file(*, webhook_url: str, folder_id: int, file_path: Path) -> int:
    encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    response = _b24_call(
        webhook_url,
        "disk.folder.uploadfile",
        [
            ("id", str(folder_id)),
            ("data[NAME]", file_path.name),
            ("fileContent[0]", file_path.name),
            ("fileContent[1]", encoded),
            ("generateUniqueName", "true"),
        ],
    )
    result = response.get("result") or {}
    object_id = _coerce_int(result.get("ID") if isinstance(result, dict) else result)
    if object_id is None:
        raise RuntimeError("Bitrix24 disk.folder.uploadfile returned empty object id")
    return object_id


def _extract_file_link(payload: Any) -> str | None:
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    if isinstance(payload, dict):
        for key in ("DOWNLOAD_URL", "DETAIL_URL", "SHOW_URL", "externalLink", "LINK"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _resolve_b24_file_url(*, webhook_url: str, file_object_id: int) -> str | None:
    for method, params in (
        ("disk.file.getExternalLink", [("id", str(file_object_id))]),
        ("disk.file.get", [("id", str(file_object_id))]),
    ):
        try:
            response = _b24_call(webhook_url, method, params)
        except Exception:
            continue
        result = response.get("result")
        link = _extract_file_link(result)
        if link:
            return link
    return None


def _notify_b24_user(
    *,
    webhook_url: str,
    user_id: int,
    message: str,
    tag: str,
    method: str = "im.notify.system.add",
) -> dict[str, Any]:
    response = _b24_call(
        webhook_url,
        method,
        [
            ("USER_ID", str(user_id)),
            ("MESSAGE", message),
            ("TAG", tag),
            ("SUB_TAG", tag),
        ],
    )
    result = response.get("result")
    if isinstance(result, dict):
        notify_id = _coerce_int(result.get("ID") or result.get("id"))
    else:
        notify_id = _coerce_int(result)
    return {"notify_id": notify_id}


def deliver_weekly_kpi_report_to_bitrix(
    *,
    webhook_url: str,
    disk_folder_id: int | None,
    user_id: int,
    message: str,
    artifact_path: Path,
    report_key: str,
    revision: int,
    notify_method: str = "im.notify.system.add",
) -> dict[str, Any]:
    disk_object_id = None
    artifact_url = None
    delivery_message = message
    if disk_folder_id is not None:
        disk_object_id = _upload_b24_disk_file(
            webhook_url=webhook_url,
            folder_id=disk_folder_id,
            file_path=artifact_path,
        )
        artifact_url = _resolve_b24_file_url(webhook_url=webhook_url, file_object_id=disk_object_id)
        if artifact_url:
            delivery_message = f"{delivery_message}\n\nФайл отчета: {artifact_url}"

    notify_result = _notify_b24_user(
        webhook_url=webhook_url,
        user_id=user_id,
        message=delivery_message,
        tag=f"weekly-kpi|{report_key}|r{revision}",
        method=notify_method,
    )
    return {
        "notify_id": notify_result.get("notify_id"),
        "disk_object_id": disk_object_id,
        "artifact_url": artifact_url,
    }


def _latest_delivered_revision_by_key(state: dict[str, Any]) -> dict[str, int]:
    latest: dict[str, int] = {}
    reports = state.get("reports") or {}
    if not isinstance(reports, dict):
        return latest
    for item in reports.values():
        if not isinstance(item, dict):
            continue
        if item.get("delivery_status") != "delivered":
            continue
        report_key = str(item.get("report_key") or "")
        revision = _coerce_int(item.get("revision")) or 0
        if not report_key:
            continue
        latest[report_key] = max(latest.get(report_key, 0), revision)
    return latest


def sync_weekly_kpi_reports(
    *,
    fetch_json: Callable[[str, dict[str, str]], Any],
    download_artifact: Callable[[str], bytes],
    deliver_report: Callable[..., dict[str, Any]],
    week_end: date,
    state_path: Path,
    artifact_dir: Path,
    target_mode: str = BITRIX_TARGET_AUTO,
    dry_run: bool = False,
) -> dict[str, Any]:
    try:
        health = fetch_json(
            "/api/management/weekly-kpi-reports/health",
            {"week_end": week_end.isoformat()},
        )
        listing = fetch_json(
            "/api/management/weekly-kpi-reports",
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
            "undelivered": 0,
            "failed": 1,
            "actions": [],
        }

    state = _load_state(state_path)
    delivered_revisions = _latest_delivered_revision_by_key(state)
    items = listing.get("payload") if isinstance(listing, dict) else []
    manifests = [item for item in items if isinstance(item, dict)]

    summary = {
        "status": "ok",
        "week_end": week_end.isoformat(),
        "health_status": _health_status(health),
        "fetched": len(manifests),
        "delivered": 0,
        "noop": 0,
        "undelivered": 0,
        "failed": 0,
        "actions": [],
    }

    for manifest in manifests:
        report_key = str(manifest.get("report_key") or "")
        revision = _coerce_int(manifest.get("revision")) or 0
        report_id = _coerce_int(manifest.get("report_id"))
        state_key = _state_key(report_key, revision)
        current = (state.get("reports") or {}).get(state_key)
        if isinstance(current, dict) and current.get("delivery_status") == "delivered":
            summary["noop"] += 1
            summary["actions"].append(
                {
                    "action": "noop",
                    "report_key": report_key,
                    "revision": revision,
                    "state_key": state_key,
                }
            )
            continue

        target_user_id = _resolve_target_user_id(manifest, target_mode=target_mode)
        if target_user_id is None:
            summary["undelivered"] += 1
            summary["actions"].append(
                {
                    "action": "undelivered",
                    "report_key": report_key,
                    "revision": revision,
                    "reason": "missing_bitrix_target_user",
                }
            )
            if not dry_run:
                state.setdefault("reports", {})[state_key] = {
                    "report_key": report_key,
                    "revision": revision,
                    "report_id": report_id,
                    "week_end": week_end.isoformat(),
                    "delivery_status": "undelivered",
                    "error": "missing_bitrix_target_user",
                    "updated_at": _utcnow().isoformat(),
                }
                _save_state(state_path, state)
            continue

        is_correction = delivered_revisions.get(
            report_key, 0
        ) > 0 and revision > delivered_revisions.get(report_key, 0)
        retail_director_monthly_kpi = None
        if _is_retail_director_manifest(manifest):
            closed_month = _closed_month_for_manifest(manifest)
            if closed_month:
                try:
                    monthly_payload = fetch_json(
                        "/api/management/retail-director-monthly-kpi",
                        {"month": closed_month},
                    )
                    payload = (
                        monthly_payload.get("payload")
                        if isinstance(monthly_payload, dict)
                        else None
                    )
                    if isinstance(payload, dict):
                        retail_director_monthly_kpi = {
                            "month": payload.get("month") or closed_month,
                            **payload,
                        }
                except Exception:
                    retail_director_monthly_kpi = None
        overview = _render_weekly_kpi_overview(
            manifest,
            is_correction=is_correction,
            retail_director_monthly_kpi=retail_director_monthly_kpi,
        )
        artifact_url = str(manifest.get("artifact_url") or "")
        action_record: dict[str, Any] = {
            "action": "deliver" if not dry_run else "dry_run",
            "report_key": report_key,
            "revision": revision,
            "report_id": report_id,
            "target_user_id": target_user_id,
            "is_correction": is_correction,
        }

        if dry_run:
            summary["delivered"] += 1
            summary["actions"].append(action_record)
            continue

        try:
            artifact_bytes = download_artifact(artifact_url)
            artifact_path = _write_local_artifact(
                artifact_dir=artifact_dir,
                manifest=manifest,
                artifact_bytes=artifact_bytes,
            )
            delivery_result = deliver_report(
                target_user_id=target_user_id,
                overview=overview,
                artifact_path=artifact_path,
                report_key=report_key,
                revision=revision,
                manifest=manifest,
                is_correction=is_correction,
            )
            state.setdefault("reports", {})[state_key] = {
                "report_key": report_key,
                "revision": revision,
                "report_id": report_id,
                "week_end": week_end.isoformat(),
                "target_user_id": target_user_id,
                "delivery_status": "delivered",
                "is_correction": is_correction,
                "artifact_path": str(artifact_path),
                "artifact_url": delivery_result.get("artifact_url"),
                "disk_object_id": delivery_result.get("disk_object_id"),
                "notify_id": delivery_result.get("notify_id"),
                "delivered_at": _utcnow().isoformat(),
            }
            _save_state(state_path, state)
            delivered_revisions[report_key] = max(delivered_revisions.get(report_key, 0), revision)
            summary["delivered"] += 1
            action_record["artifact_path"] = str(artifact_path)
            action_record["notify_id"] = delivery_result.get("notify_id")
            summary["actions"].append(action_record)
        except Exception as error:
            summary["failed"] += 1
            state.setdefault("reports", {})[state_key] = {
                "report_key": report_key,
                "revision": revision,
                "report_id": report_id,
                "week_end": week_end.isoformat(),
                "target_user_id": target_user_id,
                "delivery_status": "failed",
                "error": str(error),
                "updated_at": _utcnow().isoformat(),
            }
            _save_state(state_path, state)
            action_record["action"] = "failed"
            action_record["error"] = str(error)
            summary["actions"].append(action_record)

    return summary


def render_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"weekly_kpi_reports_from_a: {summary.get('status', 'unknown')}",
        f"Неделя: {summary.get('week_end', '-')}",
        f"Источник: {summary.get('health_status', 'unknown')}",
        (
            "Получено: {fetched}; delivered: {delivered}; noop: {noop}; "
            "undelivered: {undelivered}; failed: {failed}"
        ).format(
            fetched=summary.get("fetched", 0),
            delivered=summary.get("delivered", 0),
            noop=summary.get("noop", 0),
            undelivered=summary.get("undelivered", 0),
            failed=summary.get("failed", 0),
        ),
    ]
    error = summary.get("error")
    if error:
        lines.append(f"Ошибка: {error}")
    for item in (summary.get("actions") or [])[:10]:
        lines.append(
            f"- {item.get('action')}: {item.get('report_key')} "
            f"(revision={item.get('revision')}, target={item.get('target_user_id', '-')})"
        )
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
    download_artifact = _build_artifact_downloader(
        source_url=source_url,
        token=source_token,
        timeout=timeout,
        retries=retries,
        retry_delay=retry_delay,
    )

    webhook_url = (
        env.get("WEEKLY_KPI_B24_WEBHOOK_URL")
        or env.get("MANAGEMENT_B24_BOX_WEBHOOK_URL")
        or env.get("MANAGEMENT_B24_WEBHOOK_URL")
    )
    if not args.dry_run and not webhook_url:
        raise SystemExit(
            "Missing required env: WEEKLY_KPI_B24_WEBHOOK_URL|MANAGEMENT_B24_BOX_WEBHOOK_URL|"
            "MANAGEMENT_B24_WEBHOOK_URL"
        )

    disk_folder_id = _coerce_int(env.get("WEEKLY_KPI_B24_DISK_FOLDER_ID"))
    notify_method = env.get("WEEKLY_KPI_B24_NOTIFY_METHOD", "im.notify.system.add")
    state_path = Path(env.get("WEEKLY_KPI_STATE_PATH", DEFAULT_STATE_PATH))
    artifact_dir = Path(env.get("WEEKLY_KPI_REPORT_DIR", DEFAULT_ARTIFACT_DIR))
    target_mode = env.get("WEEKLY_KPI_B24_TARGET_MODE", BITRIX_TARGET_AUTO)
    week_end = _parse_date(args.week_end)

    def _deliver(
        *,
        target_user_id: int,
        overview: str,
        artifact_path: Path,
        report_key: str,
        revision: int,
        manifest: dict[str, Any],
        is_correction: bool,
    ) -> dict[str, Any]:
        del manifest, is_correction
        assert webhook_url is not None
        return deliver_weekly_kpi_report_to_bitrix(
            webhook_url=webhook_url,
            disk_folder_id=disk_folder_id,
            user_id=target_user_id,
            message=overview,
            artifact_path=artifact_path,
            report_key=report_key,
            revision=revision,
            notify_method=notify_method,
        )

    summary = sync_weekly_kpi_reports(
        fetch_json=fetch_json,
        download_artifact=download_artifact,
        deliver_report=_deliver,
        week_end=week_end,
        state_path=state_path,
        artifact_dir=artifact_dir,
        target_mode=target_mode,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    print(render_summary(summary))


if __name__ == "__main__":
    main()
