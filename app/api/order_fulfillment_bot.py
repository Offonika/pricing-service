from __future__ import annotations

import hmac
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_order_fulfillment_internal_token
from app.core.config import Settings, get_settings
from app.models.site_order_fulfillment import (
    BitrixChatActionCandidate,
    SiteOrderFulfillmentOutbox,
)
from app.services import pickup_control
from app.services import site_order_fulfillment_bot as bot

router = APIRouter()
internal_router = APIRouter(dependencies=[Depends(require_order_fulfillment_internal_token)])
CALLBACK_MAX_FORM_FIELDS = 256
CALLBACK_MAX_JSON_DEPTH = 16
CALLBACK_MAX_JSON_VALUES = 512


@router.post(
    "/events",
    responses={
        400: {"description": "Malformed or unsupported Bitrix callback"},
        403: {"description": "Callback authentication or authorization failed"},
        404: {"description": "Bot callback is disabled"},
        413: {"description": "Callback body exceeds the configured limit"},
    },
)
async def bitrix_bot_event(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.order_fulfillment_bot_enabled:
        raise HTTPException(status_code=404, detail="not found")
    body = await _read_limited_body(
        request,
        max_bytes=max(1024, settings.order_fulfillment_bot_callback_max_body_bytes),
    )
    form = _decode_form(body)
    _verify_bitrix_event(form, settings=settings)
    event_name = _value(form, "event").upper()
    if not event_name:
        raise HTTPException(status_code=400, detail="event name is missing")
    if event_name not in {"ONIMBOTMESSAGEADD", "ONIMCOMMANDADD", "ONIMBOTCOMMANDADD"}:
        return {"accepted": True, "ignored": True}
    dialog_id = _value(
        form,
        "data[PARAMS][DIALOG_ID]",
        "data[COMMAND][DIALOG_ID]",
        "data[COMMAND][0][DIALOG_ID]",
        "data[DIALOG_ID]",
        "dialog_id",
    ) or _command_value(form, "DIALOG_ID")
    if dialog_id not in set(settings.order_fulfillment_bot_source_chat_ids):
        raise HTTPException(status_code=403, detail="source chat is not allowed")

    if event_name == "ONIMBOTMESSAGEADD":
        text_value = _value(
            form,
            "data[PARAMS][MESSAGE]",
            "data[MESSAGE]",
            "message",
        )
        message_id = _value(
            form,
            "data[PARAMS][MESSAGE_ID]",
            "data[MESSAGE_ID]",
            "message_id",
        )
        author_id = _value(
            form,
            "data[PARAMS][FROM_USER_ID]",
            "data[USER][ID]",
            "user_id",
        )
        if not message_id:
            raise HTTPException(status_code=400, detail="message id is missing")
        menu_interaction = _russian_menu_interaction(text_value)
        if menu_interaction is not None:
            command_kind, raw_orders = menu_interaction
            try:
                orders = _public_command_orders(
                    raw_orders,
                    allow_many=command_kind == "structured_arrival",
                )
                candidates = bot.create_interactive_candidates(
                    db,
                    dialog_id=dialog_id,
                    source_message_id=message_id,
                    actor_id=author_id or None,
                    order_numbers=orders,
                    interaction=command_kind,
                    settings=settings,
                    apply_enabled_probe=lambda: bot.runtime_apply_enabled_from_env(
                        initial_enabled=settings.order_fulfillment_bot_apply_enabled,
                    ),
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {
                "accepted": True,
                "interaction": command_kind,
                "orders": len(candidates),
                "candidate_id": candidates[0].id,
            }
        try:
            candidates = bot.create_candidates_from_message(
                db,
                dialog_id=dialog_id,
                message_id=message_id,
                author_id=author_id or None,
                text_value=text_value,
                message_at=_parse_datetime(
                    _value(
                        form,
                        "data[PARAMS][DATE_CREATE]",
                        "data[DATE_CREATE]",
                    )
                ),
                settings=settings,
                payload={"event": event_name},
                apply_enabled_probe=lambda: bot.runtime_apply_enabled_from_env(
                    initial_enabled=settings.order_fulfillment_bot_apply_enabled,
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"accepted": True, "candidates": len(candidates)}

    if event_name in {"ONIMCOMMANDADD", "ONIMBOTCOMMANDADD"}:
        command = _command_value(form, "COMMAND", fallback_keys=("data[COMMAND]", "command"))
        if not command:
            raise HTTPException(status_code=400, detail="command is missing")
        command_key = command.casefold()
        supported_commands = {
            settings.order_fulfillment_bot_command.casefold(): (
                "callback",
                settings.order_fulfillment_bot_command_id,
            ),
            settings.order_fulfillment_bot_search_command.casefold(): (
                "search",
                settings.order_fulfillment_bot_search_command_id,
            ),
            settings.order_fulfillment_bot_arrival_command.casefold(): (
                "structured_arrival",
                settings.order_fulfillment_bot_arrival_command_id,
            ),
        }
        command_config = supported_commands.get(command_key)
        if command_config is None:
            raise HTTPException(status_code=400, detail="unsupported command")
        command_kind, expected_command_id = command_config
        command_id = _command_entry_id(form)
        if command_kind != "callback" and expected_command_id is None:
            raise HTTPException(status_code=503, detail="public command is not configured")
        if expected_command_id is not None and command_id != str(expected_command_id):
            raise HTTPException(status_code=400, detail="unexpected command id")
        params = _command_value(
            form,
            "COMMAND_PARAMS",
            fallback_keys=(
                "data[COMMAND_PARAMS]",
                "data[PARAMS][COMMAND_PARAMS]",
                "command_params",
            ),
        )
        if not params:
            params = _command_value(form, "PARAMS")
        actor_id = _value(
            form,
            "data[PARAMS][FROM_USER_ID]",
            "data[PARAMS][AUTHOR_ID]",
            "data[USER][ID]",
            "data[USER_ID]",
            "user_id",
        ) or _command_value(form, "USER_ID")
        if command_kind in {"search", "structured_arrival"}:
            source_message_id = _command_value(
                form,
                "MESSAGE_ID",
                fallback_keys=(
                    "data[PARAMS][MESSAGE_ID]",
                    "data[MESSAGE_ID]",
                    "message_id",
                ),
            )
            if not source_message_id or not source_message_id.isdigit():
                raise HTTPException(status_code=400, detail="command message id is missing")
            try:
                orders = _public_command_orders(
                    params, allow_many=command_kind == "structured_arrival"
                )
                candidates = bot.create_interactive_candidates(
                    db,
                    dialog_id=dialog_id,
                    source_message_id=source_message_id,
                    actor_id=actor_id,
                    order_numbers=orders,
                    interaction=command_kind,
                    settings=settings,
                    apply_enabled_probe=lambda: bot.runtime_apply_enabled_from_env(
                        initial_enabled=settings.order_fulfillment_bot_apply_enabled,
                    ),
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {
                "accepted": True,
                "interaction": command_kind,
                "orders": len(candidates),
                "candidate_id": candidates[0].id,
            }
        token = params
        try:
            if bot.callback_token_kind(token) == bot.INVENTORY_CALLBACK_KIND:
                operation, duplicate = bot.queue_inventory_clarification_action(
                    db,
                    token=token,
                    actor_id=actor_id,
                    dialog_id=dialog_id,
                    settings=settings,
                )
                return {
                    "accepted": True,
                    "inventory_clarification": True,
                    "operation_id": operation.id,
                    "duplicate": duplicate,
                }
            action_hint = bot.callback_token_action(token)
            interaction_hint = bot.callback_token_interaction(token)
            if action_hint in bot.UI_ACTIONS or (
                action_hint == bot.ACTION_CANCEL
                and interaction_hint in {"search", "structured_arrival"}
            ):
                operation, duplicate = bot.queue_interactive_callback(
                    db,
                    token=token,
                    actor_id=actor_id,
                    dialog_id=dialog_id,
                    settings=settings,
                )
                return {
                    "accepted": True,
                    "interactive": True,
                    "operation_id": operation.id,
                    "duplicate": duplicate,
                }
            if action_hint == bot.ACTION_CONFIRM_ARRIVAL:
                action, duplicate = bot.queue_structured_arrival(
                    db,
                    token=token,
                    actor_id=actor_id,
                    dialog_id=dialog_id,
                    settings=settings,
                )
                return {
                    "accepted": True,
                    "structured_arrival": True,
                    "action_id": action.id,
                    "duplicate": duplicate,
                }
            action, duplicate = bot.queue_callback_action(
                db,
                token=token,
                actor_id=actor_id,
                dialog_id=dialog_id,
                settings=settings,
            )
        except bot.BotSecurityError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {"accepted": True, "action_id": action.id, "duplicate": duplicate}

    raise HTTPException(status_code=400, detail="unsupported bot event")


def _public_command_orders(value: str, *, allow_many: bool) -> list[str]:
    normalized = value.strip()
    if not normalized:
        raise ValueError("order_number_required")
    if not re.fullmatch(r"[\d\s,;№#]+", normalized):
        raise ValueError("order_number_invalid")
    orders = re.findall(r"(?<!\d)\d{6}(?!\d)", normalized)
    unique = list(dict.fromkeys(orders))
    if not unique or (not allow_many and len(unique) != 1) or len(unique) > 20:
        raise ValueError("order_count_invalid")
    remaining = re.sub(r"(?<!\d)\d{6}(?!\d)", "", normalized)
    if re.sub(r"[\s,;№#]", "", remaining):
        raise ValueError("order_number_invalid")
    return unique


def _russian_menu_interaction(value: str) -> tuple[str, str] | None:
    normalized = value.strip()
    patterns = (
        ("search", r"(?i)^найти\s+заказ(?:\s+самовывоза)?\s+(.+)$"),
        ("structured_arrival", r"(?i)^зафиксировать\s+поступление\s+(.+)$"),
    )
    for interaction, pattern in patterns:
        if match := re.fullmatch(pattern, normalized):
            return interaction, match.group(1)
    return None


@internal_router.get("/health")
def bot_health(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    runtime_apply_enabled = bot.runtime_apply_enabled_from_env(
        initial_enabled=settings.order_fulfillment_bot_apply_enabled,
    )
    candidates = int(db.scalar(select(func.count(BitrixChatActionCandidate.id))) or 0)
    pending = int(
        db.scalar(
            select(func.count(SiteOrderFulfillmentOutbox.id)).where(
                SiteOrderFulfillmentOutbox.status.in_([bot.OUTBOX_PENDING, bot.OUTBOX_RETRY])
            )
        )
        or 0
    )
    failed = int(
        db.scalar(
            select(func.count(SiteOrderFulfillmentOutbox.id)).where(
                SiteOrderFulfillmentOutbox.status == bot.OUTBOX_FAILED
            )
        )
        or 0
    )
    processing = int(
        db.scalar(
            select(func.count(SiteOrderFulfillmentOutbox.id)).where(
                SiteOrderFulfillmentOutbox.status == bot.OUTBOX_PROCESSING
            )
        )
        or 0
    )
    active_statuses = [bot.OUTBOX_PENDING, bot.OUTBOX_RETRY, bot.OUTBOX_PROCESSING]
    blocked_by_apply = 0
    if not runtime_apply_enabled:
        blocked_by_apply = int(
            db.scalar(
                select(func.count(SiteOrderFulfillmentOutbox.id)).where(
                    SiteOrderFulfillmentOutbox.status.in_(active_statuses),
                    SiteOrderFulfillmentOutbox.operation.in_(bot.APPLY_GATED_OUTBOX_OPERATIONS),
                )
            )
            or 0
        )
    oldest_active_created_at = db.scalar(
        select(func.min(SiteOrderFulfillmentOutbox.created_at)).where(
            SiteOrderFulfillmentOutbox.status.in_(active_statuses)
        )
    )
    oldest_active_age_seconds: int | None = None
    if isinstance(oldest_active_created_at, datetime):
        oldest_active_utc = (
            oldest_active_created_at.astimezone(UTC).replace(tzinfo=None)
            if oldest_active_created_at.tzinfo is not None
            else oldest_active_created_at
        )
        oldest_active_age_seconds = max(
            0,
            int((bot.utcnow() - oldest_active_utc).total_seconds()),
        )
    pickup_metrics = pickup_control.pickup_operational_metrics(
        db,
        settings=settings,
    )
    return {
        "enabled": settings.order_fulfillment_bot_enabled,
        "apply_enabled": runtime_apply_enabled,
        "apply_configured_at_startup": settings.order_fulfillment_bot_apply_enabled,
        "sms_enabled": settings.order_fulfillment_bot_sms_enabled,
        "sms_workflow_configured": (
            settings.order_fulfillment_bot_sms_workflow_template_id is not None
        ),
        "sms_workflow_template_id": settings.order_fulfillment_bot_sms_workflow_template_id,
        "stage_apply_enabled": settings.order_fulfillment_pickup_stage_apply_enabled,
        "auto_arrival_enabled": settings.order_fulfillment_pickup_auto_arrival_enabled,
        "sla_enabled": settings.order_fulfillment_pickup_sla_enabled,
        "inventory_enabled": settings.order_fulfillment_pickup_inventory_enabled,
        "inventory_won_enabled": settings.order_fulfillment_inventory_won_enabled,
        "lost_orders_enabled": settings.order_fulfillment_lost_orders_enabled,
        "candidate_count": candidates,
        "outbox_pending": pending,
        "outbox_processing": processing,
        "outbox_failed": failed,
        "outbox_blocked_by_apply": blocked_by_apply,
        "oldest_active_outbox_age_seconds": oldest_active_age_seconds,
        **pickup_metrics,
        "pickup_warehouse_allowlist": (settings.order_fulfillment_pickup_warehouse_external_ids),
        "pickup_warehouse_alias_count": len(settings.order_fulfillment_pickup_warehouse_aliases),
        "task_route_count": len(settings.order_fulfillment_point_task_routes),
        "inventory_won_warehouse_allowlist": (
            settings.order_fulfillment_inventory_won_warehouse_external_ids
        ),
    }


def _verify_bitrix_event(form: dict[str, str], *, settings: Settings) -> None:
    expected_token = settings.order_fulfillment_bot_application_token or ""
    actual_token = _value(form, "auth[application_token]", "application_token")
    if not expected_token or not hmac.compare_digest(actual_token, expected_token):
        raise HTTPException(status_code=403, detail="invalid application token")
    domain = _value(form, "auth[domain]", "domain").casefold()
    allowed_domains = {item.casefold() for item in settings.order_fulfillment_bot_allowed_domains}
    if not allowed_domains or domain not in allowed_domains:
        raise HTTPException(status_code=403, detail="domain is not allowed")
    member_id = _value(form, "auth[member_id]", "member_id")
    allowed_members = set(settings.order_fulfillment_bot_allowed_member_ids)
    if not allowed_members or member_id not in allowed_members:
        raise HTTPException(status_code=403, detail="member is not allowed")


def _decode_form(body: bytes) -> dict[str, str]:
    try:
        parsed = parse_qs(
            body.decode("utf-8", errors="replace"),
            keep_blank_values=True,
            max_num_fields=CALLBACK_MAX_FORM_FIELDS,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="callback has too many form fields") from None
    values = {key: items[-1] if items else "" for key, items in parsed.items()}
    for container_name in ("data", "auth"):
        raw = values.get(container_name)
        if not raw or not raw.lstrip().startswith("{"):
            continue
        try:
            nested = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid nested callback JSON") from None
        except RecursionError:
            raise HTTPException(
                status_code=400,
                detail="callback JSON is too deeply nested",
            ) from None
        try:
            _flatten_json(nested, prefix=container_name, target=values)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    return values


def _flatten_json(value: Any, *, prefix: str, target: dict[str, str]) -> None:
    stack: list[tuple[str, Any, int]] = [(prefix, value, 0)]
    flattened_values = 0
    while stack:
        item_prefix, item, depth = stack.pop()
        if depth > CALLBACK_MAX_JSON_DEPTH:
            raise ValueError("callback JSON is too deeply nested")
        if isinstance(item, dict):
            for key, nested in reversed(list(item.items())):
                stack.append((f"{item_prefix}[{key}]", nested, depth + 1))
            continue
        if isinstance(item, list):
            for index in range(len(item) - 1, -1, -1):
                stack.append((f"{item_prefix}[{index}]", item[index], depth + 1))
            continue
        if item is None:
            continue
        flattened_values += 1
        if flattened_values > CALLBACK_MAX_JSON_VALUES:
            raise ValueError("callback JSON has too many values")
        target[item_prefix] = str(item)


def _value(form: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = str(form.get(key) or "").strip()
        if value:
            return value
    return ""


def _command_value(
    form: dict[str, str],
    field_name: str,
    *,
    fallback_keys: tuple[str, ...] = (),
) -> str:
    direct = _value(form, *fallback_keys)
    if direct:
        return direct
    pattern = re.compile(
        rf"^data\[COMMAND\]\[[^\]]+\]\[{re.escape(field_name)}\]$",
        re.IGNORECASE,
    )
    for key, value in form.items():
        if pattern.fullmatch(key) and str(value).strip():
            return str(value).strip()
    return ""


def _command_entry_id(form: dict[str, str]) -> str:
    direct = _command_value(
        form,
        "COMMAND_ID",
        fallback_keys=(
            "data[COMMAND_ID]",
            "data[PARAMS][COMMAND_ID]",
            "command_id",
        ),
    )
    if direct:
        return direct
    pattern = re.compile(r"^data\[COMMAND\]\[([^\]]+)\]\[[^\]]+\]$", re.IGNORECASE)
    for key in form:
        match = pattern.fullmatch(key)
        if match:
            return match.group(1).strip()
    return ""


async def _read_limited_body(request: Request, *, max_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail="request body is too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid content length") from None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="request body is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None
