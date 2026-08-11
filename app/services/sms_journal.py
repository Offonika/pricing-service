"""Business rules and encrypted persistence for the SMS journal."""

from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from typing import Any, Callable
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.sms_journal import SmsJournalApiRequest, SmsJournalAttempt

GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENSION = set("^{}\\[~]|€")


class SmsJournalConflictError(RuntimeError):
    pass


class SmsJournalNotFoundError(RuntimeError):
    pass


class SmsJournalConfigurationError(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _canonical_hash(payload: dict[str, Any]) -> str:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _clean_detail(value: str | None) -> str | None:
    return " ".join(value.split())[:1000] if value else None


def _phone_digits(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


def _mask_phone(value: str) -> str:
    digits = _phone_digits(value)
    return f"+***{digits[-4:]}"


def calculate_sms_segments(text: str) -> tuple[str, int]:
    if all(character in GSM7_BASIC or character in GSM7_EXTENSION for character in text):
        units = sum(2 if character in GSM7_EXTENSION else 1 for character in text)
        return "GSM-7", 1 if units <= 160 else (units + 152) // 153
    units = len(text.encode("utf-16-be")) // 2
    return "UCS-2", 1 if units <= 70 else (units + 66) // 67


class SmsJournalCipher:
    def __init__(self, encryption_key: str, phone_hash_key: str) -> None:
        try:
            key = base64.urlsafe_b64decode(encryption_key.encode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise SmsJournalConfigurationError("SMS journal encryption key is invalid") from exc
        if len(key) != 32:
            raise SmsJournalConfigurationError("SMS journal encryption key must decode to 32 bytes")
        if len(phone_hash_key) < 32:
            raise SmsJournalConfigurationError("SMS journal phone hash key is too short")
        self._cipher = AESGCM(key)
        self._phone_hash_key = phone_hash_key.encode("utf-8")

    def encrypt(self, value: str) -> str:
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(nonce, value.encode("utf-8"), None)
        return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            encrypted = base64.urlsafe_b64decode(value.encode("ascii"))
            if len(encrypted) <= 12:
                raise ValueError("ciphertext is too short")
            plaintext = self._cipher.decrypt(encrypted[:12], encrypted[12:], None)
            return plaintext.decode("utf-8")
        except (InvalidTag, ValueError, UnicodeError) as exc:
            raise SmsJournalConfigurationError("SMS journal ciphertext is invalid") from exc

    def phone_hash(self, phone: str) -> str:
        return hmac.new(
            self._phone_hash_key,
            _phone_digits(phone).encode("ascii"),
            hashlib.sha256,
        ).hexdigest()


class SmsJournalService:
    def __init__(
        self,
        session: Session,
        cipher: SmsJournalCipher,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.session = session
        self.cipher = cipher
        self.clock = clock

    def create_attempt(self, *, idempotency_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        redacted_text = self._redact_message(payload)
        normalized = {
            **payload,
            "event_id": str(payload["event_id"]) if payload.get("event_id") else None,
            "created_at": str(payload["created_at"]) if payload.get("created_at") else None,
            "recipient_phone": self.cipher.phone_hash(payload["recipient_phone"]),
            "message_text": hashlib.sha256(redacted_text.encode("utf-8")).hexdigest(),
            "redaction_values": [],
        }

        def command() -> dict[str, Any]:
            now = self.clock()
            created_at = _as_utc_naive(payload["created_at"]) if payload.get("created_at") else now
            event_id = payload.get("event_id") or uuid4()
            if self.session.get(SmsJournalAttempt, event_id) is not None:
                raise SmsJournalConflictError("event_id already exists")
            encoding, estimated_segments = calculate_sms_segments(redacted_text)
            row = SmsJournalAttempt(
                id=event_id,
                create_idempotency_key=idempotency_key,
                source_system=payload["source_system"],
                source_entity_type=payload["source_entity_type"],
                source_entity_id=payload["source_entity_id"],
                event_type=payload["event_type"],
                actor_id=payload.get("actor_id"),
                recipient_phone_encrypted=self.cipher.encrypt(payload["recipient_phone"]),
                recipient_phone_hash=self.cipher.phone_hash(payload["recipient_phone"]),
                recipient_phone_masked=_mask_phone(payload["recipient_phone"]),
                message_text_encrypted=self.cipher.encrypt(redacted_text),
                message_fingerprint=hashlib.sha256(redacted_text.encode("utf-8")).hexdigest(),
                contains_redacted_secret=payload["secret_kind"] != "none",
                character_count=len(redacted_text),
                encoding=encoding,
                estimated_segments=estimated_segments,
                provider=payload["provider"],
                sender_name=payload.get("sender_name"),
                attempt_number=payload["attempt_number"],
                retention_expires_at=_add_months(created_at, 13),
                created_at=created_at,
                updated_at=now,
            )
            self.session.add(row)
            self.session.flush()
            return self._response(row)

        return self._idempotent("attempt.create", idempotency_key, normalized, command)

    def record_send_result(
        self, event_id: UUID, *, idempotency_key: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        normalized = {"event_id": str(event_id), **payload}

        def command() -> dict[str, Any]:
            row = self._require(event_id)
            row.send_status = payload["send_status"]
            row.provider_message_id = payload.get("provider_message_id")
            if payload.get("provider_error_code") is not None:
                row.provider_error_code = payload["provider_error_code"]
            if payload.get("provider_error_detail") is not None:
                clean_detail = _clean_detail(payload["provider_error_detail"])
                row.provider_error_detail_encrypted = (
                    self.cipher.encrypt(clean_detail) if clean_detail else None
                )
            row.sent_at = _as_utc_naive(payload.get("sent_at") or self.clock())
            row.billed_segments = payload.get("billed_segments")
            row.unit_price = payload.get("unit_price")
            row.total_cost = payload.get("total_cost")
            row.reconciliation_period = payload.get("reconciliation_period")
            row.updated_at = self.clock()
            self.session.flush()
            return self._response(row)

        return self._idempotent("attempt.send_result", idempotency_key, normalized, command)

    def update_delivery(
        self, event_id: UUID, *, idempotency_key: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        normalized = {"event_id": str(event_id), **payload}

        def command() -> dict[str, Any]:
            row = self._require(event_id)
            row.delivery_status = payload["delivery_status"]
            if payload["delivery_status"] == "delivered":
                row.delivered_at = _as_utc_naive(payload.get("delivered_at") or self.clock())
            elif payload.get("delivered_at") is not None:
                raise SmsJournalConflictError("delivered_at requires delivered status")
            if payload.get("provider_error_code") is not None:
                row.provider_error_code = payload["provider_error_code"]
            if payload.get("provider_error_detail") is not None:
                clean_detail = _clean_detail(payload["provider_error_detail"])
                row.provider_error_detail_encrypted = (
                    self.cipher.encrypt(clean_detail) if clean_detail else None
                )
            row.updated_at = self.clock()
            self.session.flush()
            return self._response(row)

        return self._idempotent("attempt.delivery", idempotency_key, normalized, command)

    def get_attempt(self, event_id: UUID) -> dict[str, Any]:
        return self._response(self._require(event_id))

    def _idempotent(
        self,
        operation: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
        command: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        request_sha256 = _canonical_hash(request_payload)
        previous = self.session.scalar(
            select(SmsJournalApiRequest).where(
                SmsJournalApiRequest.idempotency_key == idempotency_key
            )
        )
        if previous is not None:
            if previous.operation != operation or previous.request_sha256 != request_sha256:
                raise SmsJournalConflictError("idempotency key already used for another payload")
            return {**previous.response_payload, "idempotency_replayed": True}
        response = {**command(), "idempotency_replayed": False}
        stored_response = json.loads(json.dumps(response, default=str))
        self.session.add(
            SmsJournalApiRequest(
                idempotency_key=idempotency_key,
                operation=operation,
                request_sha256=request_sha256,
                response_payload=stored_response,
            )
        )
        return response

    def _require(self, event_id: UUID) -> SmsJournalAttempt:
        row = self.session.get(SmsJournalAttempt, event_id)
        if row is None:
            raise SmsJournalNotFoundError("SMS journal event not found")
        return row

    @staticmethod
    def _redact_message(payload: dict[str, Any]) -> str:
        text = payload["message_text"]
        values = payload.get("redaction_values", [])
        if payload["secret_kind"] == "none":
            return text
        if not values or any(not value or value not in text for value in values):
            raise SmsJournalConflictError("every secret redaction value must occur in message_text")
        for value in sorted(set(values), key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
        return text

    @staticmethod
    def _response(row: SmsJournalAttempt) -> dict[str, Any]:
        return {
            "event_id": row.id,
            "source_system": row.source_system,
            "source_entity_type": row.source_entity_type,
            "source_entity_id": row.source_entity_id,
            "event_type": row.event_type,
            "recipient_phone_masked": row.recipient_phone_masked,
            "message_fingerprint": row.message_fingerprint,
            "contains_redacted_secret": row.contains_redacted_secret,
            "character_count": row.character_count,
            "encoding": row.encoding,
            "estimated_segments": row.estimated_segments,
            "provider": row.provider,
            "sender_name": row.sender_name,
            "provider_message_id": row.provider_message_id,
            "send_status": row.send_status,
            "delivery_status": row.delivery_status,
            "provider_error_code": row.provider_error_code,
            "attempt_number": row.attempt_number,
            "sent_at": row.sent_at,
            "delivered_at": row.delivered_at,
            "billed_segments": row.billed_segments,
            "unit_price": row.unit_price,
            "total_cost": row.total_cost,
            "reconciliation_period": row.reconciliation_period,
            "retention_expires_at": row.retention_expires_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
