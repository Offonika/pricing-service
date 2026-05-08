#!/usr/bin/env python3
"""Consume pending return-scheme alert batches and deliver them locally or via bridge."""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

DEFAULT_LOCAL_SOURCE_URL = "http://127.0.0.1:18080"
DEFAULT_LOCAL_ENV_FILE = "/opt/MM/pricing-service/.env"


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


def _parse_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(str(value or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _parse_int_list(value: Any) -> list[int]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        raw_items = value
    else:
        text = str(value).strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = []
            raw_items = parsed if isinstance(parsed, list) else []
        else:
            raw_items = text.split(",")
    result: list[int] = []
    for item in raw_items:
        parsed = _parse_int(item)
        if parsed > 0 and parsed not in result:
            result.append(parsed)
    return result


def _resolve_b24_webhook_url(env: dict[str, str]) -> str | None:
    return (
        env.get("RETURN_SCHEME_B24_WEBHOOK_URL")
        or env.get("BITRIX24_WEBHOOK_URL")
        or env.get("EXPERTISE_BITRIX_WEBHOOK_URL")
        or env.get("CARD_BALANCE_BITRIX_WEBHOOK_URL")
    )


def _resolve_b24_assignee_id(env: dict[str, str]) -> int:
    return _parse_int(
        env.get("RETURN_SCHEME_B24_ASSIGNEE_ID")
        or env.get("MANAGEMENT_B24_DEFAULT_RESPONSIBLE_ID")
        or env.get("EXPERTISE_BITRIX_NOTIFY_RESPONSIBLE_USER_ID")
    )


def _resolve_b24_observer_ids(env: dict[str, str]) -> list[int]:
    return _parse_int_list(
        env.get("RETURN_SCHEME_B24_OBSERVER_IDS")
        or env.get("MANAGEMENT_B24_DEFAULT_OBSERVER_IDS")
        or env.get("EXPERTISE_BITRIX_NOTIFY_AUDITOR_USER_IDS")
    )


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        if not body:
            return {}
        return json.loads(body)


def _http_download(
    url: str,
    *,
    destination: Path,
    headers: dict[str, str] | None = None,
    timeout: int = 120,
) -> Path:
    request = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        destination.write_bytes(response.read())
    return destination


def _send_telegram_document(
    *,
    token: str,
    chat_id: str,
    message: str,
    report_path: Path,
    timeout: int = 60,
) -> None:
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    boundary = f"----returnscheme{int(time.time() * 1000)}"
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


def _b24_call(base_url: str, method: str, params: list[tuple[str, str]]) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{method}.json"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"telegram_sent_batches": {}, "acknowledged_batches": [], "created_task_keys": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def _batch_anchor_date(batch: dict[str, Any]) -> str:
    generated_at = str(batch.get("generated_at") or "").strip()
    if len(generated_at) >= 10:
        return generated_at[:10]
    return "unknown-date"


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        normalized = str(value).strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _is_batch_fresh(
    batch: dict[str, Any],
    *,
    max_age_days: int,
    now: datetime | None = None,
) -> bool:
    if max_age_days <= 0:
        return True
    generated_at = _parse_iso_datetime(batch.get("generated_at"))
    if generated_at is None:
        return False
    current_time = now or datetime.now()
    age_seconds = (current_time - generated_at).total_seconds()
    return age_seconds <= max_age_days * 24 * 60 * 60


def _format_amount(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value or "0")
    return f"{amount:,.2f}".replace(",", " ")


def _escalation_candidates(batch: dict[str, Any], critical_amount: float) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for incident in batch.get("incidents", []):
        reasons: list[str] = []
        if incident.get("repeat_store_product_7d_count", 0) >= 2:
            reasons.append(
                "по этому магазину и товару за 7 дней найдено строк: "
                f"{incident.get('repeat_store_product_7d_count', 0)}"
            )
        if incident.get("manager_ref") and incident.get("repeat_employee_7d_count", 0) >= 2:
            reasons.append(
                "по этому сотруднику за 7 дней найдено строк: "
                f"{incident.get('repeat_employee_7d_count', 0)}"
            )
        try:
            incident_amount = float(incident.get("amount") or 0.0)
        except (TypeError, ValueError):
            incident_amount = 0.0
        if critical_amount > 0 and incident_amount >= critical_amount:
            reasons.append(f"критичная сумма >= {critical_amount:g}")
        if reasons:
            items.append({"incident": incident, "reasons": reasons})
    unique: dict[str, dict[str, Any]] = {}
    for item in items:
        incident = item["incident"]
        unique[f"incident:{incident['id']}"] = item
    return list(unique.values())


def _create_b24_task(
    *,
    webhook_url: str,
    assignee_id: int,
    observer_ids: list[int],
    title: str,
    description: str,
) -> dict[str, Any]:
    params = [
        ("fields[TITLE]", title),
        ("fields[DESCRIPTION]", description),
        ("fields[RESPONSIBLE_ID]", str(assignee_id)),
    ]
    for observer_id in observer_ids:
        params.append(("fields[AUDITORS][]", str(observer_id)))
    return _b24_call(webhook_url, "tasks.task.add", params)


def _extract_task_id(response: dict[str, Any]) -> int:
    result = response.get("result")
    if isinstance(result, int):
        return result
    if isinstance(result, str) and result.isdigit():
        return int(result)
    if isinstance(result, dict):
        task = result.get("task")
        if isinstance(task, dict):
            task_id = task.get("id")
            if isinstance(task_id, int):
                return task_id
            if isinstance(task_id, str) and task_id.isdigit():
                return int(task_id)
        task_id = result.get("id")
        if isinstance(task_id, int):
            return task_id
        if isinstance(task_id, str) and task_id.isdigit():
            return int(task_id)
    raise RuntimeError("Bitrix24 tasks.task.add returned empty result")


def _update_b24_task(
    *,
    webhook_url: str,
    task_id: int,
    assignee_id: int,
    observer_ids: list[int],
    title: str,
    description: str,
) -> None:
    params = [
        ("taskId", str(task_id)),
        ("fields[TITLE]", title),
        ("fields[DESCRIPTION]", description),
        ("fields[RESPONSIBLE_ID]", str(assignee_id)),
    ]
    for observer_id in observer_ids:
        params.append(("fields[AUDITORS][]", str(observer_id)))
    _b24_call(webhook_url, "tasks.task.update", params)


def _build_daily_task_payload(
    batch: dict[str, Any], critical_amount: float
) -> dict[str, str] | None:
    escalation_items = _escalation_candidates(batch, critical_amount)
    if not escalation_items:
        return None

    anchor_date = _batch_anchor_date(batch)
    lines = [
        f"Автоматическая проверка возвратов за {anchor_date}.",
        "",
        "Коротко",
        "- Это сигнал на ручную проверку, а не готовый вывод о нарушении.",
        (
            "- Система нашла цепочку: розничная продажа -> возврат -> "
            "повторная продажа не по розничному типу цены."
        ),
        (
            "- Счетчики ниже считают строки товаров. Если несколько товаров прошли "
            "одним возвратом, это один операционный эпизод, а не несколько отдельных повторов."
        ),
        "",
        "Что нужно сделать",
        "- Открыть документы продажи, возврата и повторной продажи в 1С.",
        "- Проверить, был ли понятный бизнес-сценарий: обмен, исправление цены, пересорт или ошибка оформления.",
        "- В комментарии к задаче написать итог: нормально / ошибка данных / нужно разбирать с сотрудником.",
        "",
        "Почему создана задача",
        "- Есть строки за последние 7 дней по тому же сотруднику или тому же магазину/товару.",
        "- Либо сумма отдельной строки выше порога эскалации.",
        "",
        "Служебно",
        f"Batch: {batch.get('id')}",
        f"Новых инцидентов: {batch.get('new_incidents_count', 0)}",
        f"Строк в уведомлении: {batch.get('notification_incidents_count', 0)}",
        f"Строк на ручную проверку: {len(escalation_items)}",
        f"Порог критичной суммы: {_format_amount(critical_amount)}",
        "",
    ]
    for index, item in enumerate(escalation_items, start=1):
        incident = item["incident"]
        lines.extend(
            [
                f"{index}. Строка для проверки (Incident #{incident.get('id')})",
                f"Почему попала в задачу: {', '.join(item['reasons'])}",
                f"Магазин: {incident.get('store_name') or incident.get('store_ref') or '-'}",
                f"Товар: {incident.get('product_name') or incident.get('product_ref') or '-'}",
                f"Сотрудник: {incident.get('manager_name') or incident.get('manager_ref') or '-'}",
                f"Сумма: {_format_amount(incident.get('amount'))}",
                (
                    "Первая реализация: "
                    f"{incident.get('first_sale_doc_number') or '-'} "
                    f"от {incident.get('first_sale_doc_datetime') or '-'}"
                ),
                (
                    "Возврат: "
                    f"{incident.get('return_doc_number') or '-'} "
                    f"от {incident.get('return_doc_datetime') or '-'}"
                ),
                (
                    "Вторая реализация: "
                    f"{incident.get('second_sale_doc_number') or '-'} "
                    f"от {incident.get('second_sale_doc_datetime') or '-'}"
                ),
                f"Тип цены второй реализации: {incident.get('second_price_type') or '-'}",
                "",
            ]
        )

    description = "\n".join(lines).rstrip()
    fingerprint = sha256(description.encode("utf-8")).hexdigest()
    return {
        "date_key": anchor_date,
        "title": f"[RETURN_SCHEME_ESC] Проверить возвраты за {anchor_date}: {len(escalation_items)} строк(и)",
        "description": description,
        "fingerprint": fingerprint,
    }


def _upsert_daily_b24_task(
    *,
    batch: dict[str, Any],
    critical_amount: float,
    webhook_url: str,
    assignee_id: int,
    observer_ids: list[int],
    daily_tasks_state: dict[str, dict[str, Any]],
    create_task: Any = _create_b24_task,
    update_task: Any = _update_b24_task,
) -> str | None:
    payload = _build_daily_task_payload(batch, critical_amount)
    if payload is None:
        return None

    date_key = payload["date_key"]
    existing = daily_tasks_state.get(date_key) or {}
    task_id = existing.get("task_id")
    if task_id:
        if existing.get("fingerprint") == payload["fingerprint"]:
            return "noop"
        update_task(
            webhook_url=webhook_url,
            task_id=int(task_id),
            assignee_id=assignee_id,
            observer_ids=observer_ids,
            title=payload["title"],
            description=payload["description"],
        )
        action = "updated"
    else:
        response = create_task(
            webhook_url=webhook_url,
            assignee_id=assignee_id,
            observer_ids=observer_ids,
            title=payload["title"],
            description=payload["description"],
        )
        action = "created"
        task_id = _extract_task_id(response)

    daily_tasks_state[date_key] = {
        "task_id": int(task_id),
        "fingerprint": payload["fingerprint"],
        "title": payload["title"],
    }
    return action


def main() -> None:
    env = _load_env(
        os.getenv("RETURN_SCHEME_B_ENV_FILE")
        or os.getenv("OPENCLAW_ENV_FILE")
        or os.getenv("PRICING_ENV_FILE")
        or DEFAULT_LOCAL_ENV_FILE
    )
    telegram_token = env.get("RETURN_SCHEME_ALERT_TELEGRAM_TOKEN") or env.get("TELEGRAM_TOKEN_MM")
    source_url = env.get("RETURN_SCHEME_SOURCE_URL") or DEFAULT_LOCAL_SOURCE_URL
    source_token = (
        env.get("RETURN_SCHEME_SOURCE_TOKEN")
        or env.get("RETURN_SCHEME_INTERNAL_API_TOKEN")
        or env.get("MANAGEMENT_INTERNAL_API_TOKEN")
    )
    webhook_url = _resolve_b24_webhook_url(env)
    assignee_id = _resolve_b24_assignee_id(env)
    observer_ids = _resolve_b24_observer_ids(env)
    telegram_chat_id = env.get("RETURN_SCHEME_ALERT_TELEGRAM_CHAT_ID")
    telegram_enabled = bool(telegram_token and telegram_chat_id)
    b24_enabled = bool(webhook_url and assignee_id > 0)
    missing: list[str] = []
    if not source_token:
        missing.append(
            "RETURN_SCHEME_SOURCE_TOKEN|MANAGEMENT_INTERNAL_API_TOKEN|RETURN_SCHEME_INTERNAL_API_TOKEN"
        )
    if not telegram_enabled and not b24_enabled:
        missing.append(
            "RETURN_SCHEME_ALERT_TELEGRAM_* or RETURN_SCHEME_B24_WEBHOOK_URL+RETURN_SCHEME_B24_ASSIGNEE_ID"
        )
    if missing:
        raise SystemExit(f"Missing required env: {', '.join(missing)}")

    state_path = Path(
        env.get(
            "RETURN_SCHEME_STATE_PATH",
            "/home/deploy/.openclaw/workspace/.data/return-scheme/state.json",
        )
    )
    state = _load_state(state_path)
    telegram_sent_batches = state.setdefault("telegram_sent_batches", {})
    acknowledged_batches = set(state.setdefault("acknowledged_batches", []))
    state.setdefault("created_task_keys", [])
    daily_tasks = state.setdefault("daily_tasks", {})

    source_base = source_url.rstrip("/")
    auth_headers = {"Authorization": f"Bearer {source_token}"}
    pending_url = f"{source_base}/api/internal/alerts/return-scheme/pending"
    payload = _http_json(pending_url, headers=auth_headers)
    batches = payload.get("items", [])

    critical_amount = float(env.get("RETURN_SCHEME_CRITICAL_AMOUNT", "10000") or 10000)
    max_batch_age_days = int(env.get("RETURN_SCHEME_B24_MAX_BATCH_AGE_DAYS", "7") or 7)

    for batch in batches:
        batch_id = int(batch["id"])
        if batch_id in acknowledged_batches:
            continue

        if str(batch_id) not in telegram_sent_batches:
            task_action: str | None = None
            task_skipped_reason: str | None = None
            if (
                webhook_url
                and assignee_id > 0
                and _is_batch_fresh(
                    batch,
                    max_age_days=max_batch_age_days,
                )
            ):
                task_action = _upsert_daily_b24_task(
                    batch=batch,
                    critical_amount=critical_amount,
                    webhook_url=webhook_url,
                    assignee_id=assignee_id,
                    observer_ids=observer_ids,
                    daily_tasks_state=daily_tasks,
                )
            elif webhook_url and assignee_id > 0:
                task_skipped_reason = (
                    "B24-задача не создана: batch старше "
                    f"{max_batch_age_days} дн. или дата batch не распознана."
                )

            message = batch.get("summary", {}).get("message") or "Контроль возвратной схемы"
            if task_action == "created":
                message += "\nСводная эскалация в B24 создана."
            elif task_action == "updated":
                message += "\nСводная эскалация в B24 обновлена."
            elif task_skipped_reason:
                message += f"\n{task_skipped_reason}"

            if telegram_enabled:
                with tempfile.TemporaryDirectory(prefix="return_scheme_batch_") as tmp_dir:
                    report_path = Path(tmp_dir) / f"return-scheme-batch-{batch_id}.xlsx"
                    _http_download(
                        f"{source_base}/api/internal/alerts/return-scheme/{batch_id}/report",
                        destination=report_path,
                        headers=auth_headers,
                    )
                    _send_telegram_document(
                        token=telegram_token,
                        chat_id=telegram_chat_id,
                        message=message,
                        report_path=report_path,
                    )
            elif task_action or task_skipped_reason:
                print(message)
            else:
                message = batch.get("summary", {}).get("message") or "Контроль возвратной схемы"
                print(f"{message}\nB24-задача не создана: нет кейсов для эскалации.")
            telegram_sent_batches[str(batch_id)] = {"sent_at": int(time.time())}
            _save_state(state_path, state)

        ack_url = f"{source_base}/api/internal/alerts/return-scheme/{batch_id}/ack"
        _http_json(ack_url, method="POST", headers=auth_headers, payload={})
        acknowledged_batches.add(batch_id)
        state["acknowledged_batches"] = sorted(acknowledged_batches)
        _save_state(state_path, state)


if __name__ == "__main__":
    main()
