from __future__ import annotations

import base64
import csv
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from html import unescape
from pathlib import Path
from typing import Any

import httpx
from openai import OpenAI
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.infrastructure.db import build_onec_engine_from_settings
from app.models.site_order_fulfillment import (
    BitrixChatMention,
    BitrixChatMessage,
    BitrixChatReaction,
    PickupInventorySubmission,
    SiteOrderExecutionCase,
    SiteOrderExecutionEvent,
)

CHAT_SITE_MASTER_MOBILE = "site_master_mobile"
CHAT_COURIER_SPB = "courier_spb"
CHAT_PICKUP_READY = "pickup_ready"
CHAT_PICKUP_INVENTORY = "pickup_inventory"
CHAT_PICKUP_MOVEMENT = "pickup_movement"
CHAT_PICKUP_EXCEPTION = "pickup_exception"
DEFAULT_CHAT_DIALOG_IDS = {
    CHAT_SITE_MASTER_MOBILE: "chat733",
    CHAT_COURIER_SPB: "chat727",
    CHAT_PICKUP_READY: "chat8729",
    CHAT_PICKUP_INVENTORY: "chat8961",
    CHAT_PICKUP_MOVEMENT: "chat729",
    CHAT_PICKUP_EXCEPTION: "chat739",
}

SOURCE_BITRIX_CHAT = "bitrix_chat"
PARSER_VERSION = "bitrix-order-v2"

GENERATED_ORDER_REPORT_MARKERS = (
    "master-mobile.ru: контроль интернет-заказов",
    "master-mobile.ru: crm интернет-заказы",
    "интернет-заказы: требуется действие",
)
NON_AUTHORITATIVE_CHAT_MARKERS = (
    "[quote",
    "[/quote]",
    "пересланное сообщение",
    "forwarded message",
)
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?7|8)(?:[\s()\-]*\d){10}(?!\d)")

EVENT_PICKUP_UNCLAIMED = "pickup_unclaimed_reported"
EVENT_PICKUP_STORED = "pickup_stored_at_point"
EVENT_PICKUP_RECEIVED = "pickup_client_received"
EVENT_PICKUP_DISMANTLING = "pickup_dismantling_started"
EVENT_PICKUP_DISMANTLED = "pickup_dismantled"
EVENT_PICKUP_MOVING = "pickup_moving_to_point"
EVENT_PICKUP_RECEIPT_OVERDUE = "pickup_receipt_overdue"
EVENT_PICKUP_REDIRECTED = "pickup_redirected"
EVENT_PICKUP_EXCEPTION = "pickup_exception_reported"
EVENT_PICKUP_NOTIFICATION_CONFIRMED = "pickup_notification_confirmed"
EVENT_PICKUP_NOTIFICATION_REVOKED = "pickup_notification_revoked"
EVENT_PICKUP_DISMANTLE_CANDIDATE = "pickup_dismantle_candidate"
EVENT_COURIER_DELIVERED_PENDING = "courier_spb_delivered_payment_pending"
EVENT_COURIER_DELIVERED_PAID = "courier_spb_delivered_paid"
EVENT_COURIER_FAILED = "courier_spb_failed"
EVENT_COURIER_RESCHEDULED = "courier_spb_rescheduled"
EVENT_COURIER_IN_PROGRESS = "courier_spb_in_progress"

STRICT_SITE_EVENT_PATTERNS = {
    EVENT_PICKUP_UNCLAIMED: re.compile(
        r"^(?:заказ\s+)?2\d{5}\s*(?:[-—:]\s*)?"
        r"(?:клиент\s+)?не\s+(?:забрал(?:и)?|забран(?:а|ы)?)\s*[.!]?$"
    ),
    EVENT_PICKUP_RECEIVED: re.compile(
        r"^(?:заказ\s+)?2\d{5}\s*(?:[-—:]\s*)?"
        r"(?:выдан(?:ы|а)?(?:\s+клиенту)?|выдали(?:\s+клиенту)?|"
        r"клиент\s+забрал|забрал(?:и)?(?:\s+клиент)?)\s*[.!]?$"
    ),
    EVENT_PICKUP_DISMANTLING: re.compile(
        r"^(?:заказ\s+)?2\d{5}\s*(?:[-—:]\s*)?"
        r"(?:расформирован|расформировали|разобран|разобрали|"
        r"начато\s+расформирование)\s*[.!]?$"
    ),
}

CRM_STAGE_MANUAL_REVIEW = "PREPARATION"
CRM_STAGE_PICKUP_WAITING = "PICKUP_WAITING"
CRM_STAGE_PICKUP_STORAGE = "PICKUP_STORAGE"
TERMINAL_CRM_STAGES = {"WON", "LOSE", "DISMANTLING", "APOLOGY"}

DERIVED_TO_CRM_STAGE = {
    EVENT_PICKUP_UNCLAIMED: CRM_STAGE_PICKUP_WAITING,
    EVENT_PICKUP_STORED: CRM_STAGE_PICKUP_WAITING,
    EVENT_PICKUP_RECEIVED: "WON",
    EVENT_PICKUP_DISMANTLING: "DISMANTLING",
    EVENT_PICKUP_DISMANTLED: "LOSE",
    EVENT_PICKUP_MOVING: "FINAL_INVOICE",
    EVENT_PICKUP_REDIRECTED: "FINAL_INVOICE",
    EVENT_PICKUP_EXCEPTION: CRM_STAGE_MANUAL_REVIEW,
    EVENT_PICKUP_NOTIFICATION_CONFIRMED: CRM_STAGE_PICKUP_WAITING,
    EVENT_PICKUP_NOTIFICATION_REVOKED: CRM_STAGE_PICKUP_WAITING,
    EVENT_PICKUP_DISMANTLE_CANDIDATE: CRM_STAGE_PICKUP_WAITING,
    EVENT_COURIER_DELIVERED_PENDING: "IN_DELIVERY",
    EVENT_COURIER_DELIVERED_PAID: "WON",
    EVENT_COURIER_FAILED: CRM_STAGE_MANUAL_REVIEW,
    EVENT_COURIER_RESCHEDULED: "IN_DELIVERY",
    EVENT_COURIER_IN_PROGRESS: "IN_DELIVERY",
}

CHAT_AUTO_APPLY_TARGET_STAGES = {
    CRM_STAGE_PICKUP_WAITING,
    "IN_DELIVERY",
    "DISMANTLING",
    "WON",
}
CHAT_AUTO_APPLY_CURRENT_STAGES = {
    EVENT_PICKUP_UNCLAIMED: {"PREPARATION", "EXECUTING", "FINAL_INVOICE", "IN_DELIVERY"},
    EVENT_PICKUP_STORED: {"PREPARATION", "EXECUTING", "FINAL_INVOICE", "IN_DELIVERY"},
    EVENT_PICKUP_RECEIVED: {CRM_STAGE_PICKUP_WAITING},
    EVENT_PICKUP_DISMANTLING: {CRM_STAGE_PICKUP_WAITING},
    EVENT_COURIER_DELIVERED_PENDING: {"FINAL_INVOICE", "IN_DELIVERY"},
    EVENT_COURIER_DELIVERED_PAID: {"IN_DELIVERY"},
    EVENT_COURIER_RESCHEDULED: {"FINAL_INVOICE", "IN_DELIVERY"},
    EVENT_COURIER_IN_PROGRESS: {"FINAL_INVOICE", "IN_DELIVERY"},
}

KNOWN_RAW_DELIVERY_METHODS = {
    "Самовывоз",
    "СДЭК (Самовывоз)",
    "Доставка курьером",
    "Почта России (Доставка в отделение)",
}
DELIVERY_CLASS_PICKUP = "pickup"
DELIVERY_CLASS_COURIER = "courier"
DELIVERY_CLASS_CARRIER = "carrier"
DELIVERY_CLASS_MARKETPLACE = "marketplace"
DELIVERY_CLASS_UNKNOWN = "unknown"
DELIVERY_CARRIER_MARKERS = ("сдэк", "почта россии")
DELIVERY_COURIER_MARKERS = (
    "курьер",
    "доставка курьером",
    "достависта",
    "dostavista",
    "такси",
)
DELIVERY_PICKUP_MARKERS = (
    "самовывоз",
    "магазин",
    "савелово",
    "горбушкин",
    "горбушка",
    "митино",
    "теплый стан",
    "тёплый стан",
    "пятигорск",
    "люблино",
    "центральный склад",
    "без доставки",
)
DELIVERY_WATCH_MARKERS = ("boxberry", "ems")

CRM_ORDER_NUMBER_FIELD = "UF_CRM_1772784329053"
CRM_DELIVERY_FIELD = "UF_CRM_1772784390536"
CRM_POST_DELIVERY_TYPE_FIELD = "UF_CRM_1772784574785"
CRM_PAYMENT_FIELD = "UF_CRM_1772784357019"
CRM_REVIEW_SELECT_FIELDS = (
    "ID",
    "TITLE",
    "STAGE_ID",
    CRM_ORDER_NUMBER_FIELD,
    CRM_DELIVERY_FIELD,
    CRM_POST_DELIVERY_TYPE_FIELD,
    CRM_PAYMENT_FIELD,
)

REVIEW_CSV_FIELDS = (
    "site_order_number",
    "bitrix_deal_id",
    "crm_stage",
    "crm_delivery",
    "crm_payment_status",
    "onec_raw_delivery",
    "onec_order_date",
    "onec_courier",
    "onec_delivery_cost",
    "chat_event",
    "chat_code",
    "chat_author_id",
    "chat_message_strict",
    "event_confidence",
    "evidence_redacted",
    "recommended_stage",
    "action",
    "manual_review_reason",
)

STAGE_OUTBOX_CSV_FIELDS = (
    "idempotency_key",
    "site_order_number",
    "bitrix_deal_id",
    "current_stage",
    "target_stage",
    "operation",
    "state",
    "chat_event",
    "event_confidence",
    "evidence_redacted",
    "payload_json",
    "block_reason",
)

STAGE_APPLY_RESULT_CSV_FIELDS = (
    "idempotency_key",
    "site_order_number",
    "bitrix_deal_id",
    "current_stage",
    "live_stage",
    "live_order_number",
    "target_stage",
    "operation",
    "input_state",
    "result",
    "applied",
    "dry_run",
    "reason",
)

ORDER_RE = re.compile(r"(?<!\d)(2\d{5})(?!\d)")
USER_TAG_RE = re.compile(r"\[USER=\d+].*?\[/USER]", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"\[[^\]]+]|</?[^>]+>")


class BitrixChatError(RuntimeError):
    """Raised when Bitrix chat REST returns an error."""


class BitrixStageTechnicalReviewError(BitrixChatError):
    """Raised for Bitrix stage errors that require manual product/shipment cleanup."""


@dataclass(slots=True)
class ParsedOrderMention:
    site_order_number: str
    event_type: str
    confidence: str
    evidence_text: str | None = None
    payload: dict[str, Any] | None = None


@dataclass(slots=True)
class IngestResult:
    message: BitrixChatMessage
    mentions: list[BitrixChatMention]
    events: list[SiteOrderExecutionEvent]
    duplicate_message: bool
    edited_message: bool = False
    reactions_added: int = 0
    reactions_removed: int = 0


@dataclass(slots=True)
class DeliveryMethodReportRow:
    raw_delivery_method: str
    count: int
    status: str
    note: str | None = None


@dataclass(slots=True)
class BitrixDealSnapshot:
    deal_id: int
    title: str | None = None
    stage_id: str | None = None
    delivery: str | None = None
    post_delivery_type: str | None = None
    payment_status: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class OneCOrderSnapshot:
    site_order_number: str
    order_date: datetime | None = None
    raw_delivery: str | None = None
    courier: str | None = None
    delivery_cost: Decimal | None = None
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class OrderFulfillmentReviewRow:
    site_order_number: str
    bitrix_deal_id: int | None
    crm_stage: str | None
    crm_delivery: str | None
    crm_payment_status: str | None
    onec_raw_delivery: str | None
    onec_order_date: datetime | None
    onec_courier: str | None
    onec_delivery_cost: Decimal | None
    chat_event: str
    event_confidence: str | None
    evidence_redacted: str | None
    recommended_stage: str | None
    action: str
    manual_review_reason: str | None
    chat_code: str | None = None
    chat_author_id: str | None = None
    chat_message_strict: bool = False


@dataclass(slots=True)
class OrderFulfillmentStageOutboxRow:
    idempotency_key: str
    site_order_number: str
    bitrix_deal_id: int
    current_stage: str | None
    target_stage: str
    operation: str
    state: str
    chat_event: str
    event_confidence: str | None
    evidence_redacted: str | None
    payload_json: str
    block_reason: str | None


@dataclass(slots=True)
class OrderFulfillmentStageApplyResult:
    idempotency_key: str
    site_order_number: str
    bitrix_deal_id: int
    current_stage: str | None
    live_stage: str | None
    live_order_number: str | None
    target_stage: str
    operation: str
    input_state: str
    result: str
    applied: bool
    dry_run: bool
    reason: str | None


class BitrixChatClient:
    def __init__(
        self,
        webhook_url: str,
        *,
        timeout: float = 30.0,
        urlopen=None,
        bot_client_id: str | None = None,
    ) -> None:
        self.webhook_url = webhook_url.rstrip("/") + "/"
        self.timeout = timeout
        self._urlopen = urlopen or urllib.request.urlopen
        self.bot_client_id = _clean_string(bot_client_id) or None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.webhook_url + method + ".json",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            detail = _bitrix_error_description_from_body(body) or exc.reason
            raise BitrixChatError(f"{method}: http_{exc.code} {detail}") from exc
        if "error" in data:
            raise BitrixChatError(f"{method}: {data.get('error')} {data.get('error_description')}")
        return data

    def get_dialog_messages(
        self,
        dialog_id: str,
        *,
        limit: int = 50,
        last_id: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"DIALOG_ID": dialog_id, "LIMIT": limit}
        if last_id is not None:
            params["LAST_ID"] = last_id
        response = self.call("im.dialog.messages.get", params)
        result = response.get("result")
        if not isinstance(result, dict):
            raise BitrixChatError("im.dialog.messages.get returned invalid result")
        return result

    def list_dialog_user_ids(self, dialog_id: str) -> set[str]:
        user_ids: set[str] = set()
        start = 0
        seen_starts: set[int] = set()
        for _ in range(200):
            params: dict[str, Any] = {"DIALOG_ID": dialog_id}
            if start:
                params["start"] = start
            response = self.call("im.dialog.users.list", params)
            result = response.get("result") or []
            if isinstance(result, dict):
                if "users" in result or "USERS" in result:
                    result = result.get("users") or result.get("USERS") or []
            if isinstance(result, dict):
                if "ID" in result or "id" in result:
                    result = [result]
                else:
                    result = [
                        (
                            ({**item, "ID": item.get("ID") or item.get("id") or key})
                            if isinstance(item, dict)
                            else {"ID": key}
                        )
                        for key, item in result.items()
                    ]
            if not isinstance(result, list):
                raise BitrixChatError("im.dialog.users.list returned invalid result")
            for item in result:
                value = item.get("id") or item.get("ID") if isinstance(item, dict) else item
                if clean_value := _clean_string(value):
                    user_ids.add(clean_value)

            next_value = _clean_string(response.get("next"))
            if not next_value:
                return user_ids
            if not next_value.isdigit():
                raise BitrixChatError("im.dialog.users.list returned invalid next page")
            next_start = int(next_value)
            if next_start <= start or next_start in seen_starts:
                raise BitrixChatError("im.dialog.users.list returned repeated next page")
            seen_starts.add(start)
            start = next_start
        raise BitrixChatError("im.dialog.users.list exceeded pagination limit")

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        response = self.call("user.get", {"FILTER": {"ID": user_id}})
        result = response.get("result") or []
        if isinstance(result, dict):
            result = [result]
        if not isinstance(result, list) or not result:
            return None
        item = result[0]
        return item if isinstance(item, dict) else None

    def add_bot_message(
        self,
        *,
        dialog_id: str,
        bot_id: int,
        message: str,
        keyboard: list[dict[str, Any]] | None = None,
    ) -> str:
        params: dict[str, Any] = {
            "DIALOG_ID": dialog_id,
            "BOT_ID": bot_id,
            "MESSAGE": message,
        }
        if keyboard is not None:
            params["KEYBOARD"] = keyboard
        response = self.call("imbot.message.add", self._with_bot_client_id(params))
        result = response.get("result")
        if isinstance(result, dict):
            result = result.get("MESSAGE_ID") or result.get("message_id") or result.get("id")
        message_id = _clean_string(result)
        if not message_id.isdigit() or int(message_id) <= 0:
            raise BitrixChatError("imbot.message.add returned invalid message id")
        return message_id

    def update_bot_message(
        self,
        *,
        message_id: str,
        bot_id: int,
        message: str,
        keyboard: list[dict[str, Any]] | None = None,
    ) -> Any:
        params: dict[str, Any] = {
            "MESSAGE_ID": message_id,
            "BOT_ID": bot_id,
            "MESSAGE": message,
        }
        if keyboard is not None:
            params["KEYBOARD"] = keyboard
        result = self.call("imbot.message.update", self._with_bot_client_id(params)).get("result")
        if not result:
            raise BitrixChatError("imbot.message.update returned empty result")
        return result

    def start_business_process(
        self,
        *,
        template_id: int,
        deal_id: int,
        parameters: dict[str, Any] | None = None,
    ) -> Any:
        response = self.call(
            "bizproc.workflow.start",
            {
                "TEMPLATE_ID": template_id,
                "DOCUMENT_ID": ["crm", "CCrmDocumentDeal", f"DEAL_{deal_id}"],
                "PARAMETERS": parameters or {},
            },
        )
        return response.get("result")

    def add_task(self, fields: dict[str, Any]) -> Any:
        method = "tasks.task.add"
        try:
            response = self.call(method, {"fields": fields})
        except BitrixChatError as exc:
            if not _is_bitrix_method_unavailable_error(exc):
                raise
            method = "task.item.add"
            response = self.call(method, {"arNewTaskData": fields})
        result = response.get("result")
        if not result:
            raise BitrixChatError(f"{method} returned empty task result")
        return result

    def register_bot(self, fields: dict[str, Any]) -> Any:
        return self.call("imbot.register", self._with_bot_client_id(fields)).get("result")

    def update_bot(self, bot_id: int | str, fields: dict[str, Any]) -> Any:
        update_fields = {
            key: value
            for key, value in fields.items()
            if key not in {"CLIENT_ID", "TYPE", "OPENLINE"}
        }
        params: dict[str, Any] = {"BOT_ID": bot_id, "FIELDS": update_fields}
        if not self.bot_client_id and fields.get("CLIENT_ID"):
            params["CLIENT_ID"] = fields["CLIENT_ID"]
        return self.call(
            "imbot.update",
            self._with_bot_client_id(params),
        ).get("result")

    def register_bot_command(self, fields: dict[str, Any]) -> Any:
        return self.call(
            "imbot.command.register",
            self._with_bot_client_id(fields),
        ).get("result")

    def update_bot_command(
        self,
        command_id: int | str,
        fields: dict[str, Any],
    ) -> Any:
        update_fields = {
            key: value
            for key, value in fields.items()
            if key not in {"CLIENT_ID", "BOT_ID", "COMMON"}
        }
        params: dict[str, Any] = {"COMMAND_ID": command_id, "FIELDS": update_fields}
        if not self.bot_client_id and fields.get("CLIENT_ID"):
            params["CLIENT_ID"] = fields["CLIENT_ID"]
        return self.call(
            "imbot.command.update",
            self._with_bot_client_id(params),
        ).get("result")

    def list_bots(self) -> list[dict[str, Any]]:
        response = self.call("imbot.bot.list", self._with_bot_client_id({}))
        result = response.get("result") or []
        if isinstance(result, dict):
            if "bots" in result or "BOTS" in result:
                result = result.get("bots") or result.get("BOTS") or []
            if isinstance(result, dict):
                if "ID" in result or "BOT_ID" in result or "id" in result:
                    result = [result]
                else:
                    result = [
                        {**value, "ID": value.get("ID") or key}
                        for key, value in result.items()
                        if isinstance(value, dict)
                    ]
        if not isinstance(result, list):
            raise BitrixChatError("imbot.bot.list returned invalid result")
        return [item for item in result if isinstance(item, dict)]

    def _with_bot_client_id(self, params: dict[str, Any]) -> dict[str, Any]:
        result = dict(params)
        if self.bot_client_id:
            result["CLIENT_ID"] = self.bot_client_id
        return result

    def list_deal_user_fields(self, field_name: str) -> list[dict[str, Any]]:
        response = self.call(
            "crm.deal.userfield.list",
            {"filter": {"FIELD_NAME": field_name}},
        )
        result = response.get("result") or []
        if not isinstance(result, list):
            raise BitrixChatError("crm.deal.userfield.list returned invalid result")
        return [item for item in result if isinstance(item, dict)]

    def add_deal_user_field(self, fields: dict[str, Any]) -> Any:
        return self.call("crm.deal.userfield.add", {"fields": fields}).get("result")

    def list_deals_by_site_order(self, site_order_number: str) -> list[BitrixDealSnapshot]:
        response = self.call(
            "crm.deal.list",
            {
                "filter": {f"={CRM_ORDER_NUMBER_FIELD}": site_order_number},
                "select": list(CRM_REVIEW_SELECT_FIELDS),
            },
        )
        result = response.get("result") or []
        if not isinstance(result, list):
            raise BitrixChatError("crm.deal.list returned invalid result")
        return [deal for item in result if (deal := bitrix_deal_from_payload(item)) is not None]

    def list_deals_by_stages(
        self,
        stage_ids: list[str] | tuple[str, ...] | set[str],
        *,
        limit: int = 5000,
    ) -> list[BitrixDealSnapshot]:
        """Read a bounded, paginated CRM candidate set for historical reconciliation."""

        normalized_stages = [value for item in stage_ids if (value := _clean_string(item))]
        if not normalized_stages:
            return []
        rows: list[BitrixDealSnapshot] = []
        start: int | str = 0
        while len(rows) < max(1, min(limit, 5000)):
            response = self.call(
                "crm.deal.list",
                {
                    "filter": {
                        "STAGE_ID": normalized_stages,
                        f"!{CRM_ORDER_NUMBER_FIELD}": "",
                    },
                    "select": list(CRM_REVIEW_SELECT_FIELDS),
                    "order": {"ID": "ASC"},
                    "start": start,
                },
            )
            result = response.get("result") or []
            if not isinstance(result, list):
                raise BitrixChatError("crm.deal.list returned invalid result")
            rows.extend(
                deal for item in result if (deal := bitrix_deal_from_payload(item)) is not None
            )
            next_value = response.get("next")
            if next_value in (None, "") or not result:
                break
            start = next_value
        return rows[: max(1, min(limit, 5000))]

    def get_deal_by_id(self, deal_id: int) -> BitrixDealSnapshot | None:
        response = self.call("crm.deal.get", {"id": deal_id})
        result = response.get("result")
        if not isinstance(result, dict):
            return None
        return bitrix_deal_from_payload(result)

    def update_deal_stage(self, deal_id: int, target_stage: str) -> Any:
        return self.update_deal_fields(deal_id, {"STAGE_ID": target_stage})

    def update_deal_fields(self, deal_id: int, fields: dict[str, Any]) -> Any:
        try:
            response = self.call(
                "crm.deal.update",
                {"id": deal_id, "fields": fields},
            )
        except BitrixChatError as exc:
            if _is_bitrix_stage_technical_review_error(exc):
                raise BitrixStageTechnicalReviewError(_safe_error_reason(str(exc))) from exc
            raise
        result = response.get("result")
        if not result:
            raise BitrixChatError("crm.deal.update returned empty result")
        return result

    def list_deal_stage_ids(self, entity_id: str = "DEAL_STAGE") -> set[str]:
        response = self.call(
            "crm.status.list",
            {"filter": {"ENTITY_ID": entity_id}, "order": {"SORT": "ASC"}},
        )
        result = response.get("result") or []
        if not isinstance(result, list):
            raise BitrixChatError("crm.status.list returned invalid result")
        return {stage_id for item in result if (stage_id := _clean_string(item.get("STATUS_ID")))}

    def get_download_url(self, file_id: str) -> str:
        response = self.call("disk.file.get", {"id": file_id})
        result = response.get("result") or {}
        if not isinstance(result, dict):
            raise BitrixChatError("disk.file.get returned invalid result")
        url = _clean_string(
            result.get("DOWNLOAD_URL")
            or result.get("downloadUrl")
            or result.get("urlMachine")
            or result.get("url")
        )
        if not url:
            raise BitrixChatError(f"disk.file.get returned empty download URL for {file_id}")
        return url

    def download_file(
        self,
        file_id: str,
        *,
        max_bytes: int,
        download_url: str | None = None,
    ) -> bytes:
        url = download_url or self.get_download_url(file_id)
        request = urllib.request.Request(url, method="GET")
        with self._urlopen(request, timeout=self.timeout) as response:
            content_length = _int_or_none(response.headers.get("Content-Length"))
            if content_length is not None and content_length > max_bytes:
                raise BitrixChatError(f"Bitrix file {file_id} exceeds max size {max_bytes} bytes")
            content = response.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise BitrixChatError(f"Bitrix file {file_id} exceeds max size {max_bytes} bytes")
        return content


OCR_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "orders": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Номера заказов сайта, обычно шестизначные.",
        },
        "delivery_status": {
            "type": "string",
            "enum": ["delivered", "failed", "rescheduled", "in_progress", "unknown"],
        },
        "payment_collected": {"type": ["boolean", "null"]},
        "amount": {"type": ["string", "null"]},
        "courier": {"type": ["string", "null"]},
        "comment": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "orders",
        "delivery_status",
        "payment_collected",
        "amount",
        "courier",
        "comment",
        "confidence",
    ],
}


class CourierSpbOcrClient:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: OpenAI | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.model = (
            self.settings.order_fulfillment_ocr_model
            or self.settings.card_balance_ocr_model
            or self.settings.openai_model
        )
        self.client = client or _build_openai_client(self.settings)

    def extract(self, *, image_bytes: bytes, mime_type: str = "image/png") -> dict[str, Any]:
        if not image_bytes:
            raise ValueError("image_bytes is empty")
        payload = base64.b64encode(image_bytes).decode("utf-8")
        response = self.client.responses.create(
            model=self.model,
            temperature=0,
            max_output_tokens=400,
            timeout=self.settings.order_fulfillment_ocr_timeout_seconds,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "courier_spb_order_ocr_result",
                    "strict": True,
                    "schema": OCR_JSON_SCHEMA,
                }
            },
            input=[
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Ты распознаешь скриншоты из Bitrix-чата доставок СПб. "
                                "Верни только JSON: номера заказов сайта, статус доставки, "
                                "факт получения оплаты курьером, сумму, курьера и короткий "
                                "комментарий. Если статус или оплата неочевидны, используй "
                                "unknown/null и низкую confidence."
                            ),
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Распознай отчет доставки СПб на изображении.",
                        },
                        {
                            "type": "input_image",
                            "image_url": f"data:{mime_type};base64,{payload}",
                            "detail": "high",
                        },
                    ],
                },
            ],
        )
        return normalize_courier_ocr_payload(json.loads(response.output_text or "{}"))


def ocr_is_available(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.order_fulfillment_ocr_enabled and _clean_string(settings.openai_api_key))


def _build_openai_client(settings: Settings) -> OpenAI:
    kwargs: dict[str, Any] = {
        "api_key": settings.openai_api_key,
        "base_url": settings.openai_api_base,
    }
    if _clean_string(settings.openai_http_proxy):
        kwargs["http_client"] = httpx.Client(proxy=settings.openai_http_proxy)
    return OpenAI(**kwargs)


def parse_bitrix_message(
    *,
    chat_code: str,
    text_value: str | None,
    ocr_payloads: list[dict[str, Any]] | None = None,
) -> list[ParsedOrderMention]:
    if chat_code == CHAT_COURIER_SPB:
        mentions: list[ParsedOrderMention] = []
        for payload in ocr_payloads or []:
            mentions.extend(parse_courier_ocr_payload(payload))
        return mentions
    return parse_site_chat_text(text_value or "")


def parse_site_chat_text(text_value: str) -> list[ParsedOrderMention]:
    normalized_text = _clean_text(text_value)
    if any(marker in normalized_text for marker in GENERATED_ORDER_REPORT_MARKERS):
        return []
    order_numbers = extract_order_numbers(text_value)
    if not order_numbers:
        return []
    event_type, confidence = classify_site_chat_event(text_value)
    if event_type is None:
        return []
    strict_pattern = STRICT_SITE_EVENT_PATTERNS.get(event_type)
    strict_match = bool(
        len(order_numbers) == 1
        and strict_pattern is not None
        and not _contains_non_authoritative_chat_marker(text_value)
        and strict_pattern.fullmatch(_strict_chat_text(text_value))
    )
    confidence = "strong" if strict_match else "medium"
    evidence = _redact_text(text_value)
    return [
        ParsedOrderMention(
            site_order_number=order_number,
            event_type=event_type,
            confidence=confidence,
            evidence_text=evidence,
            payload={"parser": "site_chat_text", "strict_match": strict_match},
        )
        for order_number in order_numbers
    ]


def parse_courier_ocr_payload(payload: dict[str, Any]) -> list[ParsedOrderMention]:
    normalized = normalize_courier_ocr_payload(payload)
    order_numbers = normalized["orders"]
    if not order_numbers:
        return []
    event_type = courier_event_type(
        normalized.get("delivery_status"),
        payment_collected=normalized.get("payment_collected"),
    )
    confidence_value = normalized.get("confidence")
    confidence = "strong" if confidence_value is not None and confidence_value >= 0.75 else "medium"
    strict_match = bool(
        len(order_numbers) == 1
        and confidence_value is not None
        and confidence_value >= 0.75
        and normalized.get("delivery_status") in {"delivered", "rescheduled", "in_progress"}
        and (
            normalized.get("delivery_status") != "delivered"
            or normalized.get("payment_collected") is not None
        )
    )
    evidence = _redact_text(
        _clean_string(normalized.get("comment")) or normalized.get("delivery_status") or ""
    )
    return [
        ParsedOrderMention(
            site_order_number=order_number,
            event_type=event_type,
            confidence=confidence,
            evidence_text=evidence,
            payload={
                **normalized,
                "parser": "courier_spb_ocr",
                "strict_match": strict_match,
            },
        )
        for order_number in order_numbers
    ]


def normalize_courier_ocr_payload(payload: dict[str, Any]) -> dict[str, Any]:
    orders: list[str] = []
    raw_orders = (
        payload.get("orders")
        or payload.get("order_numbers")
        or payload.get("site_order_numbers")
        or []
    )
    if isinstance(raw_orders, (str, int)):
        raw_orders = [raw_orders]
    for raw_order in raw_orders:
        orders.extend(extract_order_numbers(str(raw_order)))
    for key in ("site_order_number", "order_number", "order"):
        if payload.get(key):
            orders.extend(extract_order_numbers(str(payload.get(key))))
    if not orders and payload.get("text"):
        orders.extend(extract_order_numbers(str(payload.get("text"))))
    status = _clean_string(payload.get("delivery_status") or payload.get("status")).lower()
    if status not in {"delivered", "failed", "rescheduled", "in_progress", "unknown"}:
        status = _infer_courier_status(" ".join(str(value) for value in payload.values()))
    payment_collected = _bool_or_none(
        payload.get("payment_collected") if "payment_collected" in payload else payload.get("paid")
    )
    amount = _decimal_string_or_none(payload.get("amount"))
    if payment_collected is None and amount is not None:
        payment_collected = True
    return {
        "orders": list(dict.fromkeys(orders)),
        "delivery_status": status or "unknown",
        "payment_collected": payment_collected,
        "amount": amount,
        "courier": _clean_string(payload.get("courier")),
        "comment": _clean_string(payload.get("comment") or payload.get("evidence")),
        "confidence": _confidence_float(payload.get("confidence")),
    }


def courier_event_type(delivery_status: str | None, *, payment_collected: bool | None) -> str:
    status = (delivery_status or "unknown").strip().lower()
    if status == "delivered":
        return (
            EVENT_COURIER_DELIVERED_PAID
            if payment_collected is True
            else EVENT_COURIER_DELIVERED_PENDING
        )
    if status == "failed":
        return EVENT_COURIER_FAILED
    if status == "rescheduled":
        return EVENT_COURIER_RESCHEDULED
    return EVENT_COURIER_IN_PROGRESS


def classify_site_chat_event(text_value: str) -> tuple[str | None, str]:
    lowered = _clean_text(text_value)
    if any(marker in lowered for marker in ("разобрал", "разобрали", "расформ", "возврат")):
        return EVENT_PICKUP_DISMANTLING, "medium"
    if any(
        marker in lowered
        for marker in ("не забрал", "не забрали", "не забран", "не забраны", "не забрана")
    ):
        return EVENT_PICKUP_UNCLAIMED, "medium"
    if any(marker in lowered for marker in ("выдали", "выдан", "забрали", "забрал")):
        return EVENT_PICKUP_RECEIVED, "strong"
    return None, "weak"


def ingest_bitrix_message(
    session: Session,
    *,
    chat_code: str,
    dialog_id: str,
    chat_id: int,
    message_id: int,
    message_at: datetime | None = None,
    author_id: str | None = None,
    text_value: str | None = None,
    payload: dict[str, Any] | None = None,
    ocr_payloads: list[dict[str, Any]] | None = None,
    create_execution_events: bool = False,
) -> IngestResult:
    message_at = _naive_utc_datetime(message_at)
    stored_payload = dict(payload or {})
    order_numbers = extract_order_numbers(text_value or "")
    if order_numbers:
        stored_payload["_mm_order_numbers"] = order_numbers
    _acquire_bitrix_message_lock(session, chat_id=chat_id, message_id=message_id)
    existing = session.scalar(
        select(BitrixChatMessage).where(
            BitrixChatMessage.chat_id == chat_id,
            BitrixChatMessage.message_id == message_id,
        )
    )
    if existing is not None:
        new_hash = _text_hash(text_value)
        edited = bool(existing.raw_text_hash and new_hash and existing.raw_text_hash != new_hash)
        if edited:
            existing.parse_status = "edited_manual_review"
            existing.raw_text_hash = new_hash
            existing.raw_text_redacted = _redact_text(text_value or "")
            edited_at = datetime.now(UTC).replace(tzinfo=None)
            for submission in session.scalars(
                select(PickupInventorySubmission).where(
                    PickupInventorySubmission.source_message_id == existing.id,
                    PickupInventorySubmission.status == "confirmed",
                )
            ).all():
                submission.status = "manual_review"
                submission.payload = {
                    **(submission.payload or {}),
                    "source_message_edited_at": edited_at.isoformat(),
                    "source_message_edit_conflict": True,
                }
        existing.payload = stored_payload or existing.payload or {}
        reactions_added, reactions_removed = sync_bitrix_message_reactions(
            session,
            message=existing,
            payload=stored_payload,
            now=datetime.now(UTC).replace(tzinfo=None),
        )
        mentions = list(existing.mentions)
        events = session.scalars(
            select(SiteOrderExecutionEvent).where(
                SiteOrderExecutionEvent.raw_message_id == existing.id
            )
        ).all()
        session.commit()
        return IngestResult(
            existing,
            mentions,
            list(events),
            True,
            edited_message=edited,
            reactions_added=reactions_added,
            reactions_removed=reactions_removed,
        )

    parsed_mentions = parse_bitrix_message(
        chat_code=chat_code,
        text_value=text_value,
        ocr_payloads=ocr_payloads,
    )
    message = BitrixChatMessage(
        chat_code=chat_code,
        dialog_id=dialog_id,
        chat_id=chat_id,
        message_id=message_id,
        message_at=message_at,
        author_id=author_id,
        raw_text_hash=_text_hash(text_value),
        raw_text_redacted=_redact_text(text_value or ""),
        parser_version=PARSER_VERSION,
        parse_status="parsed" if parsed_mentions else "no_mentions",
        payload=stored_payload,
    )
    session.add(message)
    session.flush()
    reactions_added, reactions_removed = sync_bitrix_message_reactions(
        session,
        message=message,
        payload=stored_payload,
        now=datetime.now(UTC).replace(tzinfo=None),
    )

    mentions: list[BitrixChatMention] = []
    events: list[SiteOrderExecutionEvent] = []
    for parsed in parsed_mentions:
        mention = BitrixChatMention(
            message_id=message.id,
            site_order_number=parsed.site_order_number,
            event_type=parsed.event_type,
            confidence=parsed.confidence,
            evidence_text=parsed.evidence_text,
            payload=parsed.payload,
        )
        session.add(mention)
        mentions.append(mention)
        if create_execution_events:
            event = upsert_execution_event(
                session,
                site_order_number=parsed.site_order_number,
                event_type=parsed.event_type,
                event_at=message_at,
                source=SOURCE_BITRIX_CHAT,
                source_ref=f"{dialog_id}:{message_id}",
                confidence=parsed.confidence,
                raw_message_id=message.id,
                payload=parsed.payload,
            )
            if event is not None:
                events.append(event)
    session.commit()
    return IngestResult(
        message,
        mentions,
        events,
        False,
        reactions_added=reactions_added,
        reactions_removed=reactions_removed,
    )


def sync_bitrix_message_reactions(
    session: Session,
    *,
    message: BitrixChatMessage,
    payload: dict[str, Any],
    now: datetime,
) -> tuple[int, int]:
    params = payload.get("params") or payload.get("PARAMS") or {}
    likes_present = isinstance(params, dict) and ("LIKE" in params or "like" in params)
    if not likes_present:
        return 0, 0
    raw_likes = params.get("LIKE", params.get("like"))
    if raw_likes is None:
        raw_likes = []
    if not isinstance(raw_likes, list):
        raw_likes = [raw_likes]
    active_actor_ids = {actor_id for value in raw_likes if (actor_id := _clean_string(value))}
    existing = {
        (row.actor_id, row.reaction): row
        for row in session.scalars(
            select(BitrixChatReaction).where(BitrixChatReaction.message_id == message.id)
        ).all()
    }
    added = 0
    removed = 0
    for actor_id in active_actor_ids:
        row = existing.get((actor_id, "like"))
        if row is None:
            session.add(
                BitrixChatReaction(
                    message_id=message.id,
                    actor_id=actor_id,
                    reaction="like",
                    is_active=True,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            added += 1
            continue
        if not row.is_active:
            row.is_active = True
            row.removed_at = None
            added += 1
        row.last_seen_at = now
    for (actor_id, reaction), row in existing.items():
        if reaction == "like" and row.is_active and actor_id not in active_actor_ids:
            row.is_active = False
            row.removed_at = now
            row.last_seen_at = now
            removed += 1
    return added, removed


def bitrix_message_text_for_parsing(message: BitrixChatMessage) -> str:
    payload = message.payload or {}
    for key in ("text", "message", "TEXT", "MESSAGE"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    text_value = message.raw_text_redacted or ""
    for order_number in bitrix_message_order_numbers(message):
        text_value = text_value.replace("<order>", order_number, 1)
    return text_value


def bitrix_message_order_numbers(message: BitrixChatMessage) -> list[str]:
    payload = message.payload or {}
    stored = payload.get("_mm_order_numbers")
    if isinstance(stored, list):
        normalized = [
            value
            for item in stored
            if (value := _clean_string(item)) and re.fullmatch(r"2\d{5}", value)
        ]
        if normalized:
            return list(dict.fromkeys(normalized))
    return extract_order_numbers(bitrix_message_text_for_parsing_payload(payload))


def bitrix_message_text_for_parsing_payload(payload: dict[str, Any]) -> str:
    for key in ("text", "message", "TEXT", "MESSAGE"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def upsert_execution_event(
    session: Session,
    *,
    site_order_number: str,
    event_type: str,
    event_at: datetime | None,
    source: str,
    source_ref: str | None,
    confidence: str,
    raw_message_id: int | None,
    payload: dict[str, Any] | None,
    warehouse_id: int | None = None,
    actor_ref: str | None = None,
) -> SiteOrderExecutionEvent | None:
    event_at = _naive_utc_datetime(event_at)
    idempotency_key = f"{source}|{source_ref or '-'}|{site_order_number}|{event_type}"
    existing = session.scalar(
        select(SiteOrderExecutionEvent).where(
            SiteOrderExecutionEvent.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        return None

    case = session.scalar(
        select(SiteOrderExecutionCase).where(
            SiteOrderExecutionCase.site_order_number == site_order_number
        )
    )
    if case is None:
        case = SiteOrderExecutionCase(
            site_order_number=site_order_number,
            current_derived_status="manual_review",
            current_crm_stage=CRM_STAGE_MANUAL_REVIEW,
            confidence="weak",
            payload={},
        )
        session.add(case)
        session.flush()

    event = SiteOrderExecutionEvent(
        case_id=case.id,
        event_type=event_type,
        event_at=event_at,
        source=source,
        source_ref=source_ref,
        confidence=confidence,
        raw_message_id=raw_message_id,
        warehouse_id=warehouse_id,
        actor_ref=actor_ref,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    session.add(event)
    session.flush()
    target_crm_stage = DERIVED_TO_CRM_STAGE.get(event_type)
    if target_crm_stage is not None and _event_is_not_older_than_current(
        session, case=case, event=event
    ):
        case.current_derived_status = event_type
        case.current_crm_stage = target_crm_stage
        case.confidence = confidence
        case.last_evidence_event_id = event.id
        case.updated_at = datetime.now()
    return event


def _event_is_not_older_than_current(
    session: Session,
    *,
    case: SiteOrderExecutionCase,
    event: SiteOrderExecutionEvent,
) -> bool:
    if case.last_evidence_event_id is None:
        return True
    current = session.get(SiteOrderExecutionEvent, case.last_evidence_event_id)
    if current is None:
        return True
    if event.event_at is None:
        return current.event_at is None and event.id >= current.id
    if current.event_at is None:
        return True
    return (event.event_at, event.id) >= (current.event_at, current.id)


def build_recommendations(
    session: Session,
    *,
    limit: int = 100,
    status: str | None = None,
) -> list[dict[str, Any]]:
    statement = select(SiteOrderExecutionCase)
    if status:
        statement = statement.where(SiteOrderExecutionCase.current_derived_status == status)
    rows = session.scalars(
        statement.order_by(SiteOrderExecutionCase.updated_at.desc()).limit(limit)
    ).all()
    recommendations: list[dict[str, Any]] = []
    for row in rows:
        recommended_stage = DERIVED_TO_CRM_STAGE.get(row.current_derived_status)
        action = "manual_review" if recommended_stage == "manual_review" else "update_stage"
        recommendations.append(
            {
                "site_order_number": row.site_order_number,
                "bitrix_deal_id": row.bitrix_deal_id,
                "current_stage": row.current_crm_stage,
                "recommended_stage": recommended_stage,
                "derived_status": row.current_derived_status,
                "confidence": row.confidence,
                "action": action,
                "evidence_event_id": row.last_evidence_event_id,
            }
        )
    return recommendations


def build_review_rows(
    session: Session,
    *,
    limit: int = 100,
    status: str | None = None,
    bitrix_client: BitrixChatClient | None = None,
    onec_engine=None,
    settings: Settings | None = None,
    deals_by_order: dict[str, list[BitrixDealSnapshot]] | None = None,
    onec_by_order: dict[str, OneCOrderSnapshot] | None = None,
) -> list[OrderFulfillmentReviewRow]:
    settings = settings or get_settings()
    cases = _load_review_cases(session, limit=limit, status=status)
    order_numbers = [case.site_order_number for case in cases]
    deal_map = deals_by_order if deals_by_order is not None else {}
    onec_map = onec_by_order if onec_by_order is not None else {}
    bitrix_missing = False
    onec_missing = False
    bitrix_errors_by_order: dict[str, str] = {}

    if deals_by_order is None:
        if bitrix_client is None:
            bitrix_missing = True
        else:
            deal_map, bitrix_errors_by_order = fetch_bitrix_deals_for_orders(
                bitrix_client,
                order_numbers,
            )
    if onec_by_order is None:
        if onec_engine is None:
            onec_missing = True
        else:
            onec_map = fetch_onec_orders(onec_engine, order_numbers)

    rows: list[OrderFulfillmentReviewRow] = []
    for case in cases:
        event = _last_event_for_case(session, case)
        mention = _mention_for_event(session, event, case.site_order_number) if event else None
        deals = deal_map.get(case.site_order_number, [])
        onec_order = onec_map.get(case.site_order_number)
        row = build_review_row(
            case=case,
            event=event,
            mention=mention,
            deals=deals,
            onec_order=onec_order,
            bitrix_missing=bitrix_missing,
            onec_missing=onec_missing,
            bitrix_error=bitrix_errors_by_order.get(case.site_order_number),
            settings=settings,
        )
        rows.append(row)
    return rows


def build_review_row(
    *,
    case: SiteOrderExecutionCase,
    event: SiteOrderExecutionEvent | None,
    mention: BitrixChatMention | None,
    deals: list[BitrixDealSnapshot],
    onec_order: OneCOrderSnapshot | None,
    bitrix_missing: bool = False,
    onec_missing: bool = False,
    bitrix_error: str | None = None,
    settings: Settings | None = None,
) -> OrderFulfillmentReviewRow:
    settings = settings or get_settings()
    event_type = event.event_type if event is not None else case.current_derived_status
    confidence = event.confidence if event is not None else case.confidence
    evidence = mention.evidence_text if mention is not None else None
    raw_message = event.raw_message if event is not None else None
    chat_code = raw_message.chat_code if raw_message is not None else None
    chat_author_id = raw_message.author_id if raw_message is not None else None
    message_strict = bool((mention.payload or {}).get("strict_match")) if mention else False
    deal = deals[0] if len(deals) == 1 else None
    recommended_stage, action, reasons = review_decision(
        event_type=event_type,
        event_source=event.source if event is not None else None,
        event_confidence=confidence,
        chat_code=chat_code,
        chat_author_id=chat_author_id,
        chat_message_strict=message_strict,
        allowed_chat_author_ids=_allowed_chat_author_ids(settings, chat_code),
        deal=deal,
        deal_count=len(deals),
        onec_order=onec_order,
        bitrix_missing=bitrix_missing,
        onec_missing=onec_missing,
        bitrix_error=bitrix_error,
    )
    return OrderFulfillmentReviewRow(
        site_order_number=case.site_order_number,
        bitrix_deal_id=deal.deal_id if deal else None,
        crm_stage=deal.stage_id if deal else None,
        crm_delivery=deal.delivery if deal else None,
        crm_payment_status=deal.payment_status if deal else None,
        onec_raw_delivery=onec_order.raw_delivery if onec_order else None,
        onec_order_date=onec_order.order_date if onec_order else None,
        onec_courier=onec_order.courier if onec_order else None,
        onec_delivery_cost=onec_order.delivery_cost if onec_order else None,
        chat_event=event_type,
        chat_code=chat_code,
        chat_author_id=chat_author_id,
        chat_message_strict=message_strict,
        event_confidence=confidence,
        evidence_redacted=evidence,
        recommended_stage=recommended_stage,
        action=action,
        manual_review_reason="; ".join(reasons) if reasons else None,
    )


def review_decision(
    *,
    event_type: str,
    event_source: str | None = None,
    event_confidence: str | None = None,
    chat_code: str | None = None,
    chat_author_id: str | None = None,
    chat_message_strict: bool = False,
    allowed_chat_author_ids: set[str] | None = None,
    deal: BitrixDealSnapshot | None,
    deal_count: int,
    onec_order: OneCOrderSnapshot | None,
    bitrix_missing: bool = False,
    onec_missing: bool = False,
    bitrix_error: str | None = None,
) -> tuple[str | None, str, list[str]]:
    recommended_stage = DERIVED_TO_CRM_STAGE.get(event_type)
    reasons: list[str] = []

    if bitrix_missing:
        reasons.append("bitrix_config_missing")
    if bitrix_error:
        reasons.append(f"bitrix_lookup_error:{bitrix_error}")
    if onec_missing:
        reasons.append("onec_config_missing")
    if deal_count == 0:
        reasons.append("bitrix_deal_not_found")
    elif deal_count > 1:
        reasons.append("multiple_bitrix_deals")

    if deal is not None and _delivery_conflicts_with_event(event_type, deal, onec_order):
        reasons.append("delivery_conflict")
    if deal is not None and _clean_string(deal.stage_id) in TERMINAL_CRM_STAGES:
        reasons.append("terminal_crm_stage")

    if (
        event_type == EVENT_PICKUP_RECEIVED
        and not _is_internal_pickup_deal(deal)
        and not _payment_confirmed(deal)
    ):
        reasons.append("pickup_received_without_payment_confirmation")
        recommended_stage = CRM_STAGE_MANUAL_REVIEW

    if event_type == EVENT_COURIER_FAILED:
        recommended_stage = CRM_STAGE_MANUAL_REVIEW

    if reasons:
        recommended_stage = (
            deal.stage_id
            if deal is not None and _clean_string(deal.stage_id) in TERMINAL_CRM_STAGES
            else CRM_STAGE_MANUAL_REVIEW
        )

    manual_review_event = (
        recommended_stage == CRM_STAGE_MANUAL_REVIEW and event_type == EVENT_COURIER_FAILED
    )
    if not reasons and not manual_review_event and event_source == SOURCE_BITRIX_CHAT:
        reasons.extend(
            chat_auto_apply_guard_reasons(
                event_type=event_type,
                event_confidence=event_confidence,
                chat_code=chat_code,
                chat_author_id=chat_author_id,
                chat_message_strict=chat_message_strict,
                allowed_chat_author_ids=allowed_chat_author_ids or set(),
                delivery_kind=_delivery_kind(deal.delivery if deal is not None else None)
                or _delivery_kind(onec_order.raw_delivery if onec_order is not None else None),
                current_stage=deal.stage_id if deal is not None else None,
                target_stage=recommended_stage,
            )
        )
    action = "manual_review" if reasons or manual_review_event else "update_stage"
    return recommended_stage, action, reasons


def chat_auto_apply_guard_reasons(
    *,
    event_type: str,
    event_confidence: str | None,
    chat_code: str | None,
    chat_author_id: str | None,
    chat_message_strict: bool,
    allowed_chat_author_ids: set[str],
    delivery_kind: str | None,
    current_stage: str | None,
    target_stage: str | None,
) -> list[str]:
    reasons: list[str] = []
    expected_chat = (
        CHAT_SITE_MASTER_MOBILE
        if event_type.startswith("pickup_")
        else CHAT_COURIER_SPB if event_type.startswith("courier_") else None
    )
    if expected_chat is None or event_type not in CHAT_AUTO_APPLY_CURRENT_STAGES:
        reasons.append("chat_event_not_auto_applicable")
    elif chat_code != expected_chat:
        reasons.append("chat_event_wrong_chat")
    expected_delivery_kind = (
        "pickup"
        if event_type.startswith("pickup_")
        else "courier" if event_type.startswith("courier_") else None
    )
    if delivery_kind is None:
        reasons.append("chat_delivery_unknown")
    elif delivery_kind != expected_delivery_kind:
        reasons.append("chat_delivery_mismatch")
    if event_confidence != "strong":
        reasons.append("chat_confidence_not_strong")
    if not chat_message_strict:
        reasons.append("chat_message_not_strict")
    clean_author_id = _clean_string(chat_author_id)
    if not allowed_chat_author_ids:
        reasons.append("chat_author_allowlist_empty")
    elif not clean_author_id:
        reasons.append("chat_author_missing")
    elif clean_author_id not in allowed_chat_author_ids:
        reasons.append("chat_author_not_allowed")
    clean_target_stage = _clean_string(target_stage)
    if clean_target_stage not in CHAT_AUTO_APPLY_TARGET_STAGES:
        reasons.append(f"chat_target_not_allowed:{clean_target_stage or '-'}")
    allowed_current_stages = CHAT_AUTO_APPLY_CURRENT_STAGES.get(event_type, set())
    clean_current_stage = _clean_string(current_stage)
    if clean_current_stage not in allowed_current_stages:
        reasons.append(
            f"chat_transition_not_allowed:{clean_current_stage or '-'}->{clean_target_stage or '-'}"
        )
    return reasons


def _allowed_chat_author_ids(settings: Settings, chat_code: str | None) -> set[str]:
    if chat_code == CHAT_SITE_MASTER_MOBILE:
        values = settings.order_fulfillment_site_chat_apply_author_ids
    elif chat_code == CHAT_COURIER_SPB:
        values = settings.order_fulfillment_courier_chat_apply_author_ids
    else:
        values = []
    return {str(value) for value in values}


def fetch_bitrix_deals_for_orders(
    client: BitrixChatClient,
    order_numbers: list[str],
    *,
    attempts: int = 3,
    retry_delay_seconds: float = 0.25,
) -> tuple[dict[str, list[BitrixDealSnapshot]], dict[str, str]]:
    result: dict[str, list[BitrixDealSnapshot]] = {}
    errors: dict[str, str] = {}
    for order_number in dict.fromkeys(order_numbers):
        if not order_number:
            continue
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                result[order_number] = client.list_deals_by_site_order(order_number)
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - per-order enrichment must not break CSV.
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(retry_delay_seconds * (attempt + 1))
        if last_error is not None:
            result[order_number] = []
            errors[order_number] = type(last_error).__name__
    return result, errors


def fetch_onec_orders(onec_engine, order_numbers: list[str]) -> dict[str, OneCOrderSnapshot]:
    unique_orders = [item for item in dict.fromkeys(order_numbers) if item]
    if not unique_orders:
        return {}
    params = {f"order_{index}": order for index, order in enumerate(unique_orders)}
    placeholders = ", ".join(f":order_{index}" for index in range(len(unique_orders)))
    statement = text(f"""
        SELECT
            LTRIM(RTRIM(d._Fld2425)) AS site_order_number,
            d._Date_Time AS order_date,
            NULLIF(LTRIM(RTRIM(d._Fld9266)), N'') AS raw_delivery,
            r._Description AS courier,
            d._Fld9940 AS delivery_cost
        FROM dbo._Document132 AS d WITH (NOLOCK)
        LEFT JOIN dbo._Reference94 AS r WITH (NOLOCK)
            ON r._IDRRef = d._Fld9939RRef
        WHERE LTRIM(RTRIM(d._Fld2425)) IN ({placeholders})
        ORDER BY d._Date_Time DESC
        """)
    snapshots: dict[str, OneCOrderSnapshot] = {}
    with onec_engine.connect() as connection:
        for row in connection.execute(statement, params):
            site_order_number = _clean_string(row.site_order_number)
            if not site_order_number or site_order_number in snapshots:
                continue
            snapshots[site_order_number] = OneCOrderSnapshot(
                site_order_number=site_order_number,
                order_date=row.order_date,
                raw_delivery=_clean_string(row.raw_delivery) or None,
                courier=_clean_string(row.courier) or None,
                delivery_cost=_decimal_or_none(row.delivery_cost),
                raw={},
            )
    return snapshots


def bitrix_deal_from_payload(payload: Any) -> BitrixDealSnapshot | None:
    if not isinstance(payload, dict):
        return None
    deal_id = _int_or_none(payload.get("ID") or payload.get("id"))
    if deal_id is None:
        return None
    return BitrixDealSnapshot(
        deal_id=deal_id,
        title=_clean_string(payload.get("TITLE") or payload.get("title")) or None,
        stage_id=_clean_string(payload.get("STAGE_ID") or payload.get("stageId")) or None,
        delivery=_clean_string(payload.get(CRM_DELIVERY_FIELD)) or None,
        post_delivery_type=_clean_string(payload.get(CRM_POST_DELIVERY_TYPE_FIELD)) or None,
        payment_status=_clean_string(payload.get(CRM_PAYMENT_FIELD)) or None,
        raw=payload,
    )


def review_rows_to_dicts(rows: list[OrderFulfillmentReviewRow]) -> list[dict[str, Any]]:
    return [review_row_to_dict(row) for row in rows]


def review_row_to_dict(row: OrderFulfillmentReviewRow) -> dict[str, Any]:
    return {
        "site_order_number": row.site_order_number,
        "bitrix_deal_id": row.bitrix_deal_id,
        "crm_stage": row.crm_stage,
        "crm_delivery": row.crm_delivery,
        "crm_payment_status": row.crm_payment_status,
        "onec_raw_delivery": row.onec_raw_delivery,
        "onec_order_date": row.onec_order_date.isoformat() if row.onec_order_date else None,
        "onec_courier": row.onec_courier,
        "onec_delivery_cost": (
            str(row.onec_delivery_cost) if row.onec_delivery_cost is not None else None
        ),
        "chat_event": row.chat_event,
        "chat_code": row.chat_code,
        "chat_author_id": row.chat_author_id,
        "chat_message_strict": row.chat_message_strict,
        "event_confidence": row.event_confidence,
        "evidence_redacted": row.evidence_redacted,
        "recommended_stage": row.recommended_stage,
        "action": row.action,
        "manual_review_reason": row.manual_review_reason,
    }


def write_review_csv(path: Path, rows: list[OrderFulfillmentReviewRow]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=list(REVIEW_CSV_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow(review_row_to_dict(row))
    return path


def build_stage_outbox_rows(
    rows: list[OrderFulfillmentReviewRow],
    *,
    available_stage_ids: set[str] | None = None,
    allowed_target_stages: set[str] | None = None,
) -> list[OrderFulfillmentStageOutboxRow]:
    outbox_rows: list[OrderFulfillmentStageOutboxRow] = []
    for row in rows:
        target_stage = _clean_string(row.recommended_stage)
        current_stage = _clean_string(row.crm_stage)
        if row.action != "update_stage":
            continue
        if row.bitrix_deal_id is None or not target_stage:
            continue
        if allowed_target_stages is not None and target_stage not in allowed_target_stages:
            continue
        if current_stage == target_stage:
            continue
        if current_stage in TERMINAL_CRM_STAGES:
            continue

        state = "ready"
        block_reason = None
        if available_stage_ids is not None and target_stage not in available_stage_ids:
            state = "blocked_missing_target_stage"
            block_reason = f"target_stage_not_found:{target_stage}"

        fields = {"STAGE_ID": target_stage}
        if target_stage == "WON":
            fields[CRM_PAYMENT_FIELD] = "1"
        payload = {"id": row.bitrix_deal_id, "fields": fields}
        idempotency_source = "|".join(
            [
                "site-order-stage",
                row.site_order_number,
                str(row.bitrix_deal_id),
                current_stage or "-",
                target_stage,
                row.chat_event,
            ]
        )
        outbox_rows.append(
            OrderFulfillmentStageOutboxRow(
                idempotency_key=hashlib.sha256(idempotency_source.encode("utf-8")).hexdigest(),
                site_order_number=row.site_order_number,
                bitrix_deal_id=row.bitrix_deal_id,
                current_stage=current_stage or None,
                target_stage=target_stage,
                operation="update_stage",
                state=state,
                chat_event=row.chat_event,
                event_confidence=row.event_confidence,
                evidence_redacted=row.evidence_redacted,
                payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                block_reason=block_reason,
            )
        )
    return outbox_rows


def stage_outbox_row_to_dict(row: OrderFulfillmentStageOutboxRow) -> dict[str, Any]:
    return {
        "idempotency_key": row.idempotency_key,
        "site_order_number": row.site_order_number,
        "bitrix_deal_id": row.bitrix_deal_id,
        "current_stage": row.current_stage,
        "target_stage": row.target_stage,
        "operation": row.operation,
        "state": row.state,
        "chat_event": row.chat_event,
        "event_confidence": row.event_confidence,
        "evidence_redacted": row.evidence_redacted,
        "payload_json": row.payload_json,
        "block_reason": row.block_reason,
    }


def write_stage_outbox_csv(
    path: Path,
    rows: list[OrderFulfillmentStageOutboxRow],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=list(STAGE_OUTBOX_CSV_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow(stage_outbox_row_to_dict(row))
    return path


def load_stage_outbox_csv(path: Path) -> list[OrderFulfillmentStageOutboxRow]:
    rows: list[OrderFulfillmentStageOutboxRow] = []
    with path.open(encoding="utf-8-sig", newline="") as file_obj:
        reader = csv.DictReader(file_obj)
        for raw_row in reader:
            deal_id = _int_or_none(raw_row.get("bitrix_deal_id"))
            if deal_id is None:
                continue
            rows.append(
                OrderFulfillmentStageOutboxRow(
                    idempotency_key=_clean_string(raw_row.get("idempotency_key")),
                    site_order_number=_clean_string(raw_row.get("site_order_number")),
                    bitrix_deal_id=deal_id,
                    current_stage=_clean_string(raw_row.get("current_stage")) or None,
                    target_stage=_clean_string(raw_row.get("target_stage")),
                    operation=_clean_string(raw_row.get("operation")),
                    state=_clean_string(raw_row.get("state")),
                    chat_event=_clean_string(raw_row.get("chat_event")),
                    event_confidence=_clean_string(raw_row.get("event_confidence")) or None,
                    evidence_redacted=_clean_string(raw_row.get("evidence_redacted")) or None,
                    payload_json=_clean_string(raw_row.get("payload_json")),
                    block_reason=_clean_string(raw_row.get("block_reason")) or None,
                )
            )
    return rows


def apply_stage_outbox_rows(
    rows: list[OrderFulfillmentStageOutboxRow],
    *,
    client: Any,
    apply: bool = False,
    limit: int | None = None,
    target_stage: str = CRM_STAGE_PICKUP_WAITING,
    attempts: int = 3,
    retry_delay_seconds: float = 0.5,
) -> list[OrderFulfillmentStageApplyResult]:
    results: list[OrderFulfillmentStageApplyResult] = []
    safe_attempts = max(1, int(attempts))
    for row in rows[:limit] if limit is not None else rows:
        live_deal: BitrixDealSnapshot | None = None
        live_lookup_error: str | None = None
        if row.state == "ready" and row.operation == "update_stage":
            live_deal, live_lookup_error = _get_deal_by_id_with_retries(
                client,
                row.bitrix_deal_id,
                attempts=safe_attempts,
                retry_delay_seconds=retry_delay_seconds,
            )
        result = evaluate_stage_outbox_row(
            row,
            live_deal=live_deal,
            live_lookup_error=live_lookup_error,
            target_stage=target_stage,
            dry_run=not apply,
        )
        if apply and result.result == "ready":
            try:
                update_fields = _stage_outbox_update_fields(row)
                _update_deal_with_retries(
                    client,
                    row,
                    update_fields,
                    attempts=safe_attempts,
                    retry_delay_seconds=retry_delay_seconds,
                )
            except BitrixStageTechnicalReviewError as exc:
                result = _stage_apply_result(
                    row,
                    live_deal=live_deal,
                    result="technical_review",
                    reason=_safe_error_reason(str(exc)),
                    dry_run=False,
                    applied=False,
                )
            except Exception as exc:  # noqa: BLE001 - capture per-row update failure.
                result = _stage_apply_result(
                    row,
                    live_deal=live_deal,
                    result="update_error",
                    reason=_stage_update_error_reason(exc),
                    dry_run=False,
                    applied=False,
                )
            else:
                result = _stage_apply_result(
                    row,
                    live_deal=live_deal,
                    result="applied",
                    reason=None,
                    dry_run=False,
                    applied=True,
                )
        results.append(result)
    return results


def _get_deal_by_id_with_retries(
    client: Any,
    deal_id: int,
    *,
    attempts: int,
    retry_delay_seconds: float,
) -> tuple[BitrixDealSnapshot | None, str | None]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return client.get_deal_by_id(deal_id), None
        except Exception as exc:  # noqa: BLE001 - one deal must not stop the batch.
            last_error = exc
            if not _should_retry_bitrix_error(exc) or attempt + 1 >= attempts:
                break
            time.sleep(retry_delay_seconds * (attempt + 1))
    if last_error is None:
        return None, None
    return None, _stage_update_error_reason(last_error)


def _update_deal_with_retries(
    client: Any,
    row: OrderFulfillmentStageOutboxRow,
    update_fields: dict[str, Any],
    *,
    attempts: int,
    retry_delay_seconds: float,
) -> None:
    for attempt in range(attempts):
        try:
            if hasattr(client, "update_deal_fields"):
                client.update_deal_fields(row.bitrix_deal_id, update_fields)
            else:
                client.update_deal_stage(row.bitrix_deal_id, row.target_stage)
            return
        except BitrixStageTechnicalReviewError:
            raise
        except Exception as exc:  # noqa: BLE001 - retry transient Bitrix/network failures.
            if not _should_retry_bitrix_error(exc) or attempt + 1 >= attempts:
                raise
            time.sleep(retry_delay_seconds * (attempt + 1))


def _stage_outbox_update_fields(row: OrderFulfillmentStageOutboxRow) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if row.payload_json:
        try:
            payload = json.loads(row.payload_json)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and isinstance(payload.get("fields"), dict):
            fields.update(payload["fields"])
    fields["STAGE_ID"] = row.target_stage
    return fields


def evaluate_stage_outbox_row(
    row: OrderFulfillmentStageOutboxRow,
    *,
    live_deal: BitrixDealSnapshot | None,
    live_lookup_error: str | None = None,
    target_stage: str = CRM_STAGE_PICKUP_WAITING,
    dry_run: bool = True,
) -> OrderFulfillmentStageApplyResult:
    if row.state != "ready":
        return _stage_apply_result(
            row,
            live_deal=live_deal,
            result="skipped_not_ready",
            reason=row.block_reason or row.state,
            dry_run=dry_run,
            applied=False,
        )
    if row.operation != "update_stage":
        return _stage_apply_result(
            row,
            live_deal=live_deal,
            result="skipped_operation",
            reason=row.operation,
            dry_run=dry_run,
            applied=False,
        )
    if row.target_stage != target_stage:
        return _stage_apply_result(
            row,
            live_deal=live_deal,
            result="skipped_target_stage",
            reason=f"target_stage:{row.target_stage}",
            dry_run=dry_run,
            applied=False,
        )
    if live_lookup_error:
        return _stage_apply_result(
            row,
            live_deal=live_deal,
            result="live_lookup_error",
            reason=live_lookup_error,
            dry_run=dry_run,
            applied=False,
        )
    if live_deal is None:
        return _stage_apply_result(
            row,
            live_deal=live_deal,
            result="deal_not_found",
            reason=None,
            dry_run=dry_run,
            applied=False,
        )

    live_stage = _clean_string(live_deal.stage_id)
    live_order_number = _clean_string((live_deal.raw or {}).get(CRM_ORDER_NUMBER_FIELD))
    if live_order_number != row.site_order_number:
        return _stage_apply_result(
            row,
            live_deal=live_deal,
            result="order_mismatch",
            reason=f"live_order:{live_order_number or '-'}",
            dry_run=dry_run,
            applied=False,
        )
    if live_stage in TERMINAL_CRM_STAGES:
        return _stage_apply_result(
            row,
            live_deal=live_deal,
            result="terminal_live_stage",
            reason=live_stage,
            dry_run=dry_run,
            applied=False,
        )
    if live_stage == row.target_stage:
        return _stage_apply_result(
            row,
            live_deal=live_deal,
            result="already_target_stage",
            reason=live_stage,
            dry_run=dry_run,
            applied=False,
        )
    if live_stage != _clean_string(row.current_stage):
        return _stage_apply_result(
            row,
            live_deal=live_deal,
            result="current_stage_mismatch",
            reason=f"live_stage:{live_stage or '-'}",
            dry_run=dry_run,
            applied=False,
        )
    return _stage_apply_result(
        row,
        live_deal=live_deal,
        result="dry_run_ready" if dry_run else "ready",
        reason=None,
        dry_run=dry_run,
        applied=False,
    )


def _stage_apply_result(
    row: OrderFulfillmentStageOutboxRow,
    *,
    live_deal: BitrixDealSnapshot | None,
    result: str,
    reason: str | None,
    dry_run: bool,
    applied: bool,
) -> OrderFulfillmentStageApplyResult:
    live_stage = _clean_string(live_deal.stage_id) if live_deal else None
    live_order_number = (
        _clean_string((live_deal.raw or {}).get(CRM_ORDER_NUMBER_FIELD)) if live_deal else None
    )
    return OrderFulfillmentStageApplyResult(
        idempotency_key=row.idempotency_key,
        site_order_number=row.site_order_number,
        bitrix_deal_id=row.bitrix_deal_id,
        current_stage=row.current_stage,
        live_stage=live_stage or None,
        live_order_number=live_order_number or None,
        target_stage=row.target_stage,
        operation=row.operation,
        input_state=row.state,
        result=result,
        applied=applied,
        dry_run=dry_run,
        reason=reason,
    )


def stage_apply_result_to_dict(row: OrderFulfillmentStageApplyResult) -> dict[str, Any]:
    return {
        "idempotency_key": row.idempotency_key,
        "site_order_number": row.site_order_number,
        "bitrix_deal_id": row.bitrix_deal_id,
        "current_stage": row.current_stage,
        "live_stage": row.live_stage,
        "live_order_number": row.live_order_number,
        "target_stage": row.target_stage,
        "operation": row.operation,
        "input_state": row.input_state,
        "result": row.result,
        "applied": "1" if row.applied else "0",
        "dry_run": "1" if row.dry_run else "0",
        "reason": row.reason,
    }


def write_stage_apply_result_csv(
    path: Path,
    rows: list[OrderFulfillmentStageApplyResult],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=list(STAGE_APPLY_RESULT_CSV_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow(stage_apply_result_to_dict(row))
    return path


def ingest_bitrix_chat(
    session: Session,
    *,
    client: BitrixChatClient,
    chat_code: str,
    dialog_id: str,
    limit: int,
    run_ocr: bool,
    ocr_client: CourierSpbOcrClient | None = None,
    settings: Settings | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    result = result or client.get_dialog_messages(dialog_id, limit=limit)
    messages = _list_items(result.get("messages") or [])
    files_by_id = _files_by_id(result.get("files") or [])
    stats = {
        "messages": 0,
        "mentions": 0,
        "events": 0,
        "duplicates": 0,
        "edited": 0,
        "reactions_added": 0,
        "reactions_removed": 0,
        "ocr_images": 0,
    }
    for message in messages:
        stats["messages"] += 1
        text_value = str(message.get("text") or message.get("message") or "")
        chat_id = _int_or_none(
            message.get("chat_id")
            or message.get("chatId")
            or result.get("chat_id")
            or result.get("chatId")
        ) or _numeric_dialog_id(dialog_id)
        message_id = int(message.get("id") or message.get("ID"))
        existing_message_id = session.scalar(
            select(BitrixChatMessage.id).where(
                BitrixChatMessage.chat_id == chat_id,
                BitrixChatMessage.message_id == message_id,
            )
        )
        ocr_payloads: list[dict[str, Any]] = []
        if existing_message_id is None and chat_code == CHAT_COURIER_SPB and run_ocr:
            ocr_payloads = _ocr_payloads_for_message(
                message,
                files_by_id=files_by_id,
                client=client,
                ocr_client=ocr_client or CourierSpbOcrClient(settings=settings),
                max_bytes=settings.order_fulfillment_ocr_max_image_bytes,
            )
            stats["ocr_images"] += len(ocr_payloads)
        ingested = ingest_bitrix_message(
            session,
            chat_code=chat_code,
            dialog_id=dialog_id,
            chat_id=chat_id,
            message_id=message_id,
            message_at=parse_datetime(message.get("date")),
            author_id=_clean_string(message.get("author_id")),
            text_value=text_value,
            payload=message,
            ocr_payloads=ocr_payloads,
            create_execution_events=(
                chat_code == CHAT_COURIER_SPB
                or (
                    chat_code == CHAT_SITE_MASTER_MOBILE
                    and not settings.order_fulfillment_bot_enabled
                )
            ),
        )
        stats["mentions"] += len(ingested.mentions)
        stats["events"] += len(ingested.events)
        stats["duplicates"] += 1 if ingested.duplicate_message else 0
        stats["edited"] += 1 if ingested.edited_message else 0
        stats["reactions_added"] += ingested.reactions_added
        stats["reactions_removed"] += ingested.reactions_removed
    return stats


def poll_bitrix_chat_pages(
    session: Session,
    *,
    client: BitrixChatClient,
    chat_code: str,
    dialog_id: str,
    page_size: int = 50,
    max_pages: int = 20,
    lookback_since: datetime | None = None,
    run_ocr: bool = False,
    ocr_client: CourierSpbOcrClient | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    lookback_since = _naive_utc_datetime(lookback_since)
    totals: dict[str, Any] = {
        "pages": 0,
        "messages": 0,
        "mentions": 0,
        "events": 0,
        "duplicates": 0,
        "edited": 0,
        "reactions_added": 0,
        "reactions_removed": 0,
        "ocr_images": 0,
        "oldest_message_id": None,
        "reached_lookback": False,
        "page_limit_reached": False,
    }
    last_id: int | None = None
    seen_oldest_ids: set[int] = set()
    for _ in range(max(1, max_pages)):
        result = client.get_dialog_messages(
            dialog_id,
            limit=max(1, min(page_size, 50)),
            last_id=last_id,
        )
        messages = _list_items(result.get("messages") or [])
        if not messages:
            break
        message_ids = [
            value
            for item in messages
            if (value := _int_or_none(item.get("id") or item.get("ID"))) is not None
        ]
        if not message_ids:
            break
        oldest_id = min(message_ids)
        if oldest_id in seen_oldest_ids:
            break
        seen_oldest_ids.add(oldest_id)
        page_stats = ingest_bitrix_chat(
            session,
            client=client,
            chat_code=chat_code,
            dialog_id=dialog_id,
            limit=page_size,
            run_ocr=run_ocr,
            ocr_client=ocr_client,
            settings=settings,
            result=result,
        )
        totals["pages"] += 1
        for key in (
            "messages",
            "mentions",
            "events",
            "duplicates",
            "edited",
            "reactions_added",
            "reactions_removed",
            "ocr_images",
        ):
            totals[key] += int(page_stats.get(key) or 0)
        totals["oldest_message_id"] = oldest_id
        message_dates = [
            normalized
            for item in messages
            if (parsed := parse_datetime(item.get("date") or item.get("DATE"))) is not None
            and (normalized := _naive_utc_datetime(parsed)) is not None
        ]
        if lookback_since is not None and message_dates and min(message_dates) <= lookback_since:
            totals["reached_lookback"] = True
            break
        last_id = oldest_id
    else:
        totals["page_limit_reached"] = True
    return totals


def build_delivery_method_report_from_rows(
    rows: list[tuple[str | None, int]],
    *,
    known_methods: set[str] | None = None,
) -> list[DeliveryMethodReportRow]:
    known = known_methods or KNOWN_RAW_DELIVERY_METHODS
    report: list[DeliveryMethodReportRow] = []
    for raw_method, count in rows:
        normalized = _clean_string(raw_method)
        delivery_class = classify_delivery_method(normalized)
        if normalized in known or delivery_class != DELIVERY_CLASS_UNKNOWN:
            continue
        marker = delivery_watch_marker(normalized)
        report.append(
            DeliveryMethodReportRow(
                raw_delivery_method=normalized or "<empty>",
                count=int(count),
                status="watch" if marker else "unknown",
                note=f"watch marker: {marker}" if marker else None,
            )
        )
    return report


def query_unknown_delivery_methods(
    settings: Settings | None = None,
    *,
    date_from: date | None = None,
) -> list[DeliveryMethodReportRow]:
    settings = settings or get_settings()
    if not settings.onec_database_url:
        raise RuntimeError("ONEC_DATABASE_URL is not configured")
    engine = build_onec_engine_from_settings()
    date_filter = "AND d._Date_Time >= :date_from" if date_from else ""
    with engine.connect() as connection:
        rows = connection.execute(
            text(f"""
                SELECT
                    COALESCE(NULLIF(LTRIM(RTRIM(d._Fld9266)), N''), N'') AS raw_delivery,
                    COUNT(*) AS cnt
                FROM dbo._Document132 AS d WITH (NOLOCK)
                WHERE d._Fld2425 IS NOT NULL
                  AND LTRIM(RTRIM(d._Fld2425)) <> N''
                  {date_filter}
                GROUP BY COALESCE(NULLIF(LTRIM(RTRIM(d._Fld9266)), N''), N'')
                ORDER BY cnt DESC
                """),
            {"date_from": date_from} if date_from else {},
        ).fetchall()
    known = {str(item).strip() for item in settings.order_fulfillment_known_raw_deliveries}
    return build_delivery_method_report_from_rows(
        [(row[0], int(row[1])) for row in rows],
        known_methods=known,
    )


def classify_delivery_method(value: str | None) -> str:
    normalized = _clean_text(_clean_string(value))
    if not normalized:
        return DELIVERY_CLASS_UNKNOWN
    if any(marker in normalized for marker in DELIVERY_CARRIER_MARKERS):
        return DELIVERY_CLASS_CARRIER
    if any(marker in normalized for marker in DELIVERY_COURIER_MARKERS):
        return DELIVERY_CLASS_COURIER
    if any(marker in normalized for marker in DELIVERY_PICKUP_MARKERS):
        return DELIVERY_CLASS_PICKUP
    return DELIVERY_CLASS_UNKNOWN


def delivery_watch_marker(value: str | None) -> str | None:
    normalized = _clean_text(_clean_string(value))
    return next((marker for marker in DELIVERY_WATCH_MARKERS if marker in normalized), None)


def extract_order_numbers(text_value: str) -> list[str]:
    return list(dict.fromkeys(ORDER_RE.findall(text_value or "")))


def parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text_value = str(value).strip()
    if not text_value:
        return None
    try:
        return datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _naive_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _ocr_payloads_for_message(
    message: dict[str, Any],
    *,
    files_by_id: dict[str, dict[str, Any]],
    client: BitrixChatClient,
    ocr_client: CourierSpbOcrClient,
    max_bytes: int,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for file_id in _message_file_ids(message):
        file_meta = files_by_id.get(file_id, {})
        # URLs from im.dialog.messages.get can point to an HTML viewer/download page.
        # disk.file.get returns the machine download URL suitable for OCR bytes.
        content = client.download_file(file_id, max_bytes=max_bytes)
        mime_type = _clean_string(file_meta.get("contentType")) or "image/png"
        payloads.append(ocr_client.extract(image_bytes=content, mime_type=mime_type))
    return payloads


def _message_file_ids(message: dict[str, Any]) -> list[str]:
    params = message.get("params") or {}
    raw_ids = params.get("FILE_ID") or params.get("FILE_IDS") or []
    if isinstance(raw_ids, (str, int)):
        raw_ids = [raw_ids]
    return [_clean_string(item) for item in raw_ids if _clean_string(item)]


def _list_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        raw_items = value.values()
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []
    return [item for item in raw_items if isinstance(item, dict)]


def _files_by_id(files: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for file_meta in _list_items(files):
        file_id = _clean_string(file_meta.get("id") or file_meta.get("ID"))
        if file_id:
            result[file_id] = file_meta
    return result


def _load_review_cases(
    session: Session,
    *,
    limit: int,
    status: str | None,
) -> list[SiteOrderExecutionCase]:
    statement = select(SiteOrderExecutionCase)
    if status:
        statement = statement.where(SiteOrderExecutionCase.current_derived_status == status)
    return list(
        session.scalars(
            statement.order_by(SiteOrderExecutionCase.updated_at.desc()).limit(limit)
        ).all()
    )


def _last_event_for_case(
    session: Session,
    case: SiteOrderExecutionCase,
) -> SiteOrderExecutionEvent | None:
    if case.last_evidence_event_id:
        event = session.get(SiteOrderExecutionEvent, case.last_evidence_event_id)
        if event is not None:
            return event
    return session.scalar(
        select(SiteOrderExecutionEvent)
        .where(SiteOrderExecutionEvent.case_id == case.id)
        .order_by(
            SiteOrderExecutionEvent.event_at.desc().nullslast(), SiteOrderExecutionEvent.id.desc()
        )
        .limit(1)
    )


def _mention_for_event(
    session: Session,
    event: SiteOrderExecutionEvent,
    site_order_number: str,
) -> BitrixChatMention | None:
    if event.raw_message_id is None:
        return None
    return session.scalar(
        select(BitrixChatMention)
        .where(
            BitrixChatMention.message_id == event.raw_message_id,
            BitrixChatMention.site_order_number == site_order_number,
            BitrixChatMention.event_type == event.event_type,
        )
        .order_by(BitrixChatMention.id.desc())
        .limit(1)
    )


def _delivery_conflicts_with_event(
    event_type: str,
    deal: BitrixDealSnapshot,
    onec_order: OneCOrderSnapshot | None,
) -> bool:
    delivery_kind = _delivery_kind(deal.delivery) or _delivery_kind(
        onec_order.raw_delivery if onec_order else None
    )
    if delivery_kind is None:
        return False
    if event_type.startswith("pickup_"):
        return delivery_kind != "pickup"
    if event_type.startswith("courier_"):
        return delivery_kind != "courier"
    return False


def _delivery_kind(value: str | None) -> str | None:
    delivery_class = classify_delivery_method(value)
    if delivery_class == DELIVERY_CLASS_UNKNOWN:
        return None
    if delivery_class == DELIVERY_CLASS_CARRIER:
        return "carrier"
    if delivery_class == DELIVERY_CLASS_COURIER:
        return "courier"
    if delivery_class == DELIVERY_CLASS_PICKUP:
        return "pickup"
    return None


def _is_internal_pickup_deal(deal: BitrixDealSnapshot | None) -> bool:
    if deal is None:
        return False
    return _delivery_kind(deal.delivery) == "pickup"


def _payment_confirmed(deal: BitrixDealSnapshot | None) -> bool:
    if deal is None:
        return False
    value = deal.payment_status
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    normalized = _clean_text(str(value))
    return normalized in {"1", "true", "yes", "y", "да", "оплачено", "оплачен"}


def _infer_courier_status(text_value: str) -> str:
    lowered = _clean_text(text_value)
    if any(marker in lowered for marker in ("не достав", "отказ", "не дозвон")):
        return "failed"
    if any(marker in lowered for marker in ("перенос", "перенесли", "завтра")):
        return "rescheduled"
    if any(marker in lowered for marker in ("доставлен", "доставили", "получил", "выдан")):
        return "delivered"
    if any(marker in lowered for marker in ("курьер", "доставка", "в работе")):
        return "in_progress"
    return "unknown"


def _bitrix_error_description_from_body(body: str) -> str | None:
    if not body:
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return _clean_string(body)[:500] or None
    if not isinstance(payload, dict):
        return _clean_string(body)[:500] or None
    description = payload.get("error_description") or payload.get("error")
    return _clean_string(description)[:500] if description else None


def _is_bitrix_stage_technical_review_error(exc: Exception) -> bool:
    lowered = _clean_text(str(exc))
    markers = (
        "распределен по отгрузкам",
        "распределён по отгрузкам",
        "уменьшить количество товара",
        "товара нет в наличии",
        "нет в наличии",
        "остатк",
        "отгрузк",
    )
    return any(marker in lowered for marker in markers)


def _is_bitrix_method_unavailable_error(exc: Exception) -> bool:
    lowered = _clean_text(str(exc))
    return any(
        marker in lowered
        for marker in (
            "error_method_not_found",
            "method not found",
            "метод не найден",
        )
    )


def _acquire_bitrix_message_lock(
    session: Session,
    *,
    chat_id: int,
    message_id: int,
) -> None:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    digest = hashlib.sha256(f"bitrix-message:{chat_id}:{message_id}".encode()).digest()
    lock_key = int.from_bytes(digest[:8], "big", signed=True)
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": lock_key},
    )


def _numeric_dialog_id(dialog_id: str) -> int:
    match = re.search(r"(\d+)$", _clean_string(dialog_id))
    return int(match.group(1)) if match else 0


def _safe_error_reason(value: str) -> str:
    cleaned = _clean_string(value)
    if not cleaned:
        return "error"
    cleaned = re.sub(r"https?://\S+", "<url>", cleaned)
    return cleaned[:500]


def _should_retry_bitrix_error(exc: Exception) -> bool:
    if isinstance(exc, BitrixStageTechnicalReviewError):
        return False
    text = _clean_text(f"{type(exc).__name__} {exc}")
    markers = (
        "http_500",
        "http_502",
        "http_503",
        "http_504",
        "timed out",
        "timeout",
        "connection reset",
        "remote end closed",
        "temporarily",
        "temporarily unavailable",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
    )
    return any(marker in text for marker in markers)


def _stage_update_error_reason(exc: Exception) -> str:
    safe = _safe_error_reason(str(exc) or type(exc).__name__)
    if _should_retry_bitrix_error(exc):
        return f"transient_bitrix_error:{type(exc).__name__}: {safe}"
    if _is_bitrix_stage_technical_review_error(exc):
        return f"technical_review:{safe}"
    return f"{type(exc).__name__}: {safe}"


def _bool_or_none(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "да", "оплачено", "получено"}:
        return True
    if normalized in {"0", "false", "no", "нет", "не оплачено"}:
        return False
    return None


def _decimal_string_or_none(value: Any) -> str | None:
    if value is None or value == "":
        return None
    sanitized = (
        str(value)
        .replace("\u00a0", "")
        .replace(" ", "")
        .replace("руб.", "")
        .replace("руб", "")
        .replace("₽", "")
        .replace(",", ".")
    )
    try:
        candidate = Decimal(sanitized)
    except (InvalidOperation, ValueError):
        return None
    return str(candidate.quantize(Decimal("0.01")))


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _confidence_float(value: Any) -> float:
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, candidate))


def _clean_string(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _clean_text(value: str) -> str:
    text_value = USER_TAG_RE.sub("@user", value or "")
    text_value = TAG_RE.sub(" ", text_value)
    return _clean_string(unescape(text_value)).lower()


def _strict_chat_text(value: str) -> str:
    text_value = USER_TAG_RE.sub(" ", value or "")
    text_value = TAG_RE.sub(" ", text_value)
    return _clean_string(unescape(text_value)).lower()


def _contains_non_authoritative_chat_marker(value: str) -> bool:
    lowered = (value or "").lower()
    return any(marker in lowered for marker in NON_AUTHORITATIVE_CHAT_MARKERS)


def _redact_text(value: str) -> str:
    text_value = USER_TAG_RE.sub("@user", value or "")
    text_value = TAG_RE.sub(" ", text_value)
    text_value = EMAIL_RE.sub("<email>", text_value)
    text_value = PHONE_RE.sub("<phone>", text_value)
    text_value = ORDER_RE.sub("<order>", text_value)
    return _clean_string(unescape(text_value))[:1000]


def _text_hash(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
