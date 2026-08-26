from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.customer_settlement import (
    CustomerSettlementAlertOutbox,
    CustomerSettlementAlertState,
)
from app.services.customer_settlements import ensure_utc, utc_now

ALERT_CHANNEL = "bitrix_task_2883"
ALERT_TASK_ID = "2883"
_HEALTH_LEVELS = {"ok", "warning", "critical"}
_MAX_BITRIX_RESPONSE_BYTES = 1024 * 1024
_MAX_BITRIX_COMMENT_PAGES = 100


def overall_health_status(metrics: dict[str, Any]) -> str:
    values = {metrics.get("freshness_status"), metrics.get("mapping_status")}
    if not values.issubset(_HEALTH_LEVELS):
        return "critical"
    return "critical" if "critical" in values else "warning" if "warning" in values else "ok"


def _event_key(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()


def _safe_health_count(value: Any) -> str:
    return (
        f"{value:,}".replace(",", " ")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else "не определено"
    )


def _safe_health_text(value: Any, *, ok_text: str) -> str:
    if value == "ok":
        return ok_text
    if value == "warning":
        return "обновление задерживается"
    if value == "critical":
        return "требуется проверка"
    return "не удалось определить"


def _safe_health_message(level: str, metrics: dict[str, Any], *, recovered: bool) -> str:
    if recovered:
        title = "Взаиморасчёты снова работают нормально"
    elif level == "warning":
        title = "Взаиморасчёты: обновление задерживается"
    else:
        title = "Взаиморасчёты: требуется проверка"
    return "\n".join(
        (
            title,
            "Финансовые данные: "
            f"{_safe_health_text(metrics.get('freshness_status'), ok_text='обновлены')}.",
            "Связь кабинетов с клиентами 1С: "
            f"{_safe_health_text(metrics.get('mapping_status'), ok_text='работает')}.",
            f"Загружено клиентов: {_safe_health_count(metrics.get('loaded_rows'))} из "
            f"{_safe_health_count(metrics.get('expected_rows'))}.",
            f"Без долга и аванса: {_safe_health_count(metrics.get('zero_rows'))}.",
            "Суммы и данные клиентов в сообщение не включаются.",
        )
    )


def validate_customer_settlement_alert_webhook_url(webhook_url: str | None) -> str:
    normalized = str(webhook_url or "").strip().rstrip("/")
    try:
        parts = urllib.parse.urlparse(normalized)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise RuntimeError("bitrix_alert_webhook_invalid") from exc
    if (
        parts.scheme != "https"
        or not hostname
        or port == 0
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise RuntimeError("bitrix_alert_webhook_invalid")
    return normalized


def enqueue_health_alert_if_needed(
    session: Session,
    *,
    metrics: dict[str, Any],
    repeat_seconds: int,
    now: datetime | None = None,
) -> CustomerSettlementAlertOutbox | None:
    if repeat_seconds != 21600:
        raise RuntimeError("customer_settlement_alert_repeat_is_invalid")
    current_time = ensure_utc(now or utc_now())
    current_level = overall_health_status(metrics)
    state_statement = (
        select(CustomerSettlementAlertState)
        .where(CustomerSettlementAlertState.channel == ALERT_CHANNEL)
        .with_for_update()
    )
    state = session.scalar(state_statement)
    state_created = False
    if state is None:
        candidate = CustomerSettlementAlertState(
            channel=ALERT_CHANNEL,
            current_level=current_level,
        )
        try:
            with session.begin_nested():
                session.add(candidate)
                session.flush()
            state = candidate
            state_created = True
        except IntegrityError:
            state = session.scalar(state_statement)
            if state is None:
                raise

    if state_created:
        should_send = current_level != "ok"
        previous_level = "unknown"
        previous_notified_at = None
    else:
        previous_level = state.current_level
        previous_notified_at = state.last_notified_at
        changed = current_level != previous_level
        critical_repeat = (
            current_level == "critical"
            and state.last_notified_at is not None
            and current_time - ensure_utc(state.last_notified_at)
            >= timedelta(seconds=repeat_seconds)
        )
        should_send = changed or critical_repeat
        state.current_level = current_level
        state.updated_at = current_time
    if not should_send:
        return None

    recovered = current_level == "ok" and previous_level != "ok"
    event_key = _event_key(
        ALERT_CHANNEL,
        previous_level,
        current_level,
        (
            ensure_utc(previous_notified_at).isoformat()
            if previous_notified_at is not None
            else "never"
        ),
    )
    existing = session.scalar(
        select(CustomerSettlementAlertOutbox).where(
            CustomerSettlementAlertOutbox.event_key == event_key
        )
    )
    if existing is not None:
        return existing
    row = CustomerSettlementAlertOutbox(
        event_key=event_key,
        status="pending",
        severity=current_level,
        message=_safe_health_message(current_level, metrics, recovered=recovered),
        attempt_count=0,
        next_attempt_at=current_time,
    )
    session.add(row)
    state.last_notified_at = current_time
    session.flush()
    return row


def _read_bitrix_json(response) -> dict[str, Any]:
    raw_body = response.read(_MAX_BITRIX_RESPONSE_BYTES + 1)
    if len(raw_body) > _MAX_BITRIX_RESPONSE_BYTES:
        raise RuntimeError("bitrix_alert_delivery_response_too_large")
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("bitrix_alert_delivery_invalid_response") from exc
    if not isinstance(body, dict) or body.get("error"):
        raise RuntimeError("bitrix_alert_delivery_invalid_response")
    return body


def _request_bitrix_json(
    *, url: str, payload: dict[str, str], timeout_seconds: float
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={"Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return _read_bitrix_json(response)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("bitrix_alert_delivery_failed") from exc


def _find_bitrix_comment_by_marker(
    *, base_url: str, task_id: str, marker: str, timeout_seconds: float
) -> str | None:
    start = 0
    seen_starts: set[int] = set()
    for _page_number in range(_MAX_BITRIX_COMMENT_PAGES):
        if start in seen_starts:
            raise RuntimeError("bitrix_alert_delivery_invalid_response")
        seen_starts.add(start)
        body = _request_bitrix_json(
            url=f"{base_url}/task.commentitem.getlist.json",
            payload={
                "TASKID": task_id,
                "ORDER[ID]": "desc",
                "start": str(start),
            },
            timeout_seconds=timeout_seconds,
        )
        result = body.get("result")
        if not isinstance(result, list):
            raise RuntimeError("bitrix_alert_delivery_invalid_response")
        for item in result:
            if not isinstance(item, dict):
                raise RuntimeError("bitrix_alert_delivery_invalid_response")
            message = str(
                item.get("POST_MESSAGE")
                or item.get("postMessage")
                or item.get("MESSAGE")
                or item.get("message")
                or ""
            )
            if marker in message:
                return str(item.get("ID") or item.get("id") or "readback")
        next_start = body.get("next")
        if next_start in (None, ""):
            return None
        if (
            isinstance(next_start, bool)
            or not str(next_start).strip().isdigit()
            or int(str(next_start).strip()) <= start
        ):
            raise RuntimeError("bitrix_alert_delivery_invalid_response")
        start = int(str(next_start).strip())
    raise RuntimeError("bitrix_alert_delivery_pagination_limit_exceeded")


def _post_bitrix_comment(
    *,
    webhook_url: str,
    task_id: str,
    event_key: str,
    message: str,
    timeout_seconds: float,
) -> str:
    base_url = validate_customer_settlement_alert_webhook_url(webhook_url)
    marker = f"[#mm-settlements:{event_key}]"
    existing_ref = _find_bitrix_comment_by_marker(
        base_url=base_url,
        task_id=task_id,
        marker=marker,
        timeout_seconds=timeout_seconds,
    )
    if existing_ref is not None:
        return existing_ref
    body = _request_bitrix_json(
        url=f"{base_url}/task.commentitem.add.json",
        payload={
            "taskId": task_id,
            "arFields[POST_MESSAGE]": (
                f"{message}\n" f"Служебная метка для защиты от повторной отправки: {marker}"
            ),
        },
        timeout_seconds=timeout_seconds,
    )
    if not body.get("result"):
        raise RuntimeError("bitrix_alert_delivery_invalid_response")
    return str(body["result"])


def dispatch_customer_settlement_alerts(
    session: Session,
    *,
    webhook_url: str,
    task_id: str,
    timeout_seconds: float = 3.0,
    max_attempts: int = 5,
    now: datetime | None = None,
) -> dict[str, int]:
    normalized_task_id = str(task_id).strip()
    if normalized_task_id != ALERT_TASK_ID:
        raise RuntimeError("bitrix_alert_task_is_not_allowed")
    if timeout_seconds != 3.0 or max_attempts != 5:
        raise RuntimeError("bitrix_alert_delivery_contract_is_invalid")
    current_time = ensure_utc(now or utc_now())
    rows = session.scalars(
        select(CustomerSettlementAlertOutbox)
        .where(
            CustomerSettlementAlertOutbox.attempt_count < max_attempts,
            CustomerSettlementAlertOutbox.next_attempt_at <= current_time,
            or_(
                CustomerSettlementAlertOutbox.status == "pending",
                CustomerSettlementAlertOutbox.status == "failed",
            ),
        )
        .order_by(CustomerSettlementAlertOutbox.id)
        .limit(10)
        .with_for_update(skip_locked=True)
    ).all()
    sent = 0
    failed = 0
    for row in rows:
        row.attempt_count += 1
        try:
            row.external_ref = _post_bitrix_comment(
                webhook_url=webhook_url,
                task_id=normalized_task_id,
                event_key=row.event_key,
                message=row.message,
                timeout_seconds=timeout_seconds,
            )
            row.status = "sent"
            row.sent_at = current_time
            sent += 1
        except RuntimeError:
            row.status = "failed"
            row.next_attempt_at = current_time + timedelta(minutes=5)
            failed += 1
        row.updated_at = current_time
    session.flush()
    exhausted = (
        session.scalar(
            select(func.count())
            .select_from(CustomerSettlementAlertOutbox)
            .where(
                CustomerSettlementAlertOutbox.status == "failed",
                CustomerSettlementAlertOutbox.attempt_count >= max_attempts,
            )
        )
        or 0
    )
    return {
        "processed": len(rows),
        "sent": sent,
        "failed": failed,
        "exhausted": int(exhausted),
    }
