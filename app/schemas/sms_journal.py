"""HTTP contracts for the internal SMS journal."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

SafeCode = str


class SmsAttemptCreateRequest(BaseModel):
    event_id: UUID | None = None
    created_at: datetime | None = None
    source_system: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    source_entity_type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    source_entity_id: str = Field(min_length=1, max_length=255)
    event_type: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    actor_id: str | None = Field(default=None, max_length=128)
    recipient_phone: str = Field(min_length=7, max_length=32)
    message_text: str = Field(min_length=1, max_length=4000)
    secret_kind: Literal["none", "otp", "password"] = "none"
    redaction_values: list[str] = Field(default_factory=list, max_length=10)
    provider: str = Field(default="megafon", min_length=1, max_length=64)
    sender_name: str | None = Field(default=None, max_length=64)
    attempt_number: int = Field(default=1, ge=1, le=100)

    @field_validator("recipient_phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        digits = "".join(character for character in value if character.isdigit())
        if not 7 <= len(digits) <= 15:
            raise ValueError("recipient_phone must contain 7 to 15 digits")
        return value

    @model_validator(mode="after")
    def require_secret_redaction(self) -> SmsAttemptCreateRequest:
        if self.secret_kind != "none" and not self.redaction_values:
            raise ValueError("redaction_values are required for OTP or password messages")
        if self.secret_kind == "none" and self.redaction_values:
            raise ValueError("secret_kind must identify provided redaction_values")
        return self


class SmsSendResultRequest(BaseModel):
    send_status: Literal["accepted", "failed", "unknown"]
    provider_message_id: str | None = Field(default=None, max_length=255)
    provider_error_code: SafeCode | None = Field(default=None, max_length=128)
    provider_error_detail: str | None = Field(default=None, max_length=1000)
    sent_at: datetime | None = None
    billed_segments: int | None = Field(default=None, ge=0, le=100)
    unit_price: Decimal | None = Field(default=None, ge=0)
    total_cost: Decimal | None = Field(default=None, ge=0)
    reconciliation_period: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")


class SmsDeliveryUpdateRequest(BaseModel):
    delivery_status: Literal["delivered", "undelivered", "expired", "unknown"]
    delivered_at: datetime | None = None
    provider_error_code: SafeCode | None = Field(default=None, max_length=128)
    provider_error_detail: str | None = Field(default=None, max_length=1000)


class SmsAttemptResponse(BaseModel):
    event_id: UUID
    source_system: str
    source_entity_type: str
    source_entity_id: str
    event_type: str
    recipient_phone_masked: str
    message_fingerprint: str
    contains_redacted_secret: bool
    character_count: int
    encoding: Literal["GSM-7", "UCS-2"]
    estimated_segments: int
    provider: str
    sender_name: str | None = None
    provider_message_id: str | None = None
    send_status: str
    delivery_status: str
    provider_error_code: str | None = None
    attempt_number: int
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    billed_segments: int | None = None
    unit_price: Decimal | None = None
    total_cost: Decimal | None = None
    reconciliation_period: str | None = None
    retention_expires_at: datetime
    created_at: datetime
    updated_at: datetime
    idempotency_replayed: bool = False
