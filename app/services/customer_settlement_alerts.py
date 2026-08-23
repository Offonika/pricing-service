from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.customer_settlement import (
    CustomerSettlementAlertOutbox,
    CustomerSettlementAlertState,
)
from app.services.customer_settlements import ensure_utc, utc_now

ALERT_CHANNEL = "bitrix_task_2883"
ALERT_TASK_ID = "2883"
_HEALTH_LEVELS = {"ok", "warning", "critical"}


def overall_health_status(metrics: dict[str, Any]) -> str:
    values = {metrics.get("freshness_status"), metrics.get("mapping_status")}
    if not values.issubset(_HEALTH_LEVELS):
        return "critical"
    return "critical" if "critical" in values else "warning" if "warning" in values else "ok"


def _event_key(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()


def _safe_health_level(value: Any) -> str:
    return str(value) if value in _HEALTH_LEVELS else "unknown"


def _safe_health_count(value: Any) -> str:
    return (
        str(value)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else "unknown"
    )


def _safe_health_message(level: str, metrics: dict[str, Any], *, recovered: bool) -> str:
    title = (
        "Взаиморасчёты: состояние восстановлено" if recovered else f"Взаиморасчёты: {level.upper()}"
    )
    return "\n".join(
        (
            title,
            f"Свежесть финансового среза: {_safe_health_level(metrics.get('freshness_status'))}",
            f"Свежесть mapping: {_safe_health_level(metrics.get('mapping_status'))}",
            f"Строк expected/loaded/zero: {_safe_health_count(metrics.get('expected_rows'))}/"
            f"{_safe_health_count(metrics.get('loaded_rows'))}/"
            f"{_safe_health_count(metrics.get('zero_rows'))}",
            "Финансовые суммы и идентификаторы клиентов намеренно не включены.",
        )
    )


def enqueue_health_alert_if_needed(
    session: Session,
    *,
    metrics: dict[str, Any],
    repeat_seconds: int,
    now: datetime | None = None,
) -> CustomerSettlementAlertOutbox | None:
    current_time = ensure_utc(now or utc_now())
    current_level = overall_health_status(metrics)
    state = session.scalar(
        select(CustomerSettlementAlertState).where(
            CustomerSettlementAlertState.channel == ALERT_CHANNEL
        )
    )
    if state is None:
        state = CustomerSettlementAlertState(
            channel=ALERT_CHANNEL,
            current_level=current_level,
        )
        session.add(state)
        session.flush()
        should_send = current_level != "ok"
        previous_level = "unknown"
    else:
        previous_level = state.current_level
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
    bucket = int(current_time.timestamp()) // max(60, repeat_seconds)
    event_key = _event_key(ALERT_CHANNEL, previous_level, current_level, bucket)
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


def _post_bitrix_comment(
    *, webhook_url: str, task_id: str, message: str, timeout_seconds: float
) -> str:
    parts = urllib.parse.urlparse(webhook_url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise RuntimeError("bitrix_alert_webhook_invalid")
    url = f"{webhook_url.rstrip('/')}/task.commentitem.add.json"
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode({"taskId": task_id, "arFields[POST_MESSAGE]": message}).encode(
            "utf-8"
        ),
        headers={"Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
    ) as exc:
        raise RuntimeError("bitrix_alert_delivery_failed") from exc
    if not isinstance(body, dict) or body.get("error") or not body.get("result"):
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
    if str(task_id).strip() != ALERT_TASK_ID:
        raise RuntimeError("bitrix_alert_task_is_not_allowed")
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
                task_id=task_id,
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
