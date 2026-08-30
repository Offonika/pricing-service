from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_EVENT_ID = re.compile(r"^site-support:(\d+):(\d+)$")
_EMAIL_EVENT_ID = re.compile(r"^bitrix-mail:(shop|info):(\d+)$")
SITE_SERVICE_REQUEST_REPLY_MAX_LENGTH = 200_000


class SiteServiceRequestFilePayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    file_id: int = Field(alias="fileId", gt=0)
    name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(alias="mimeType", min_length=1, max_length=255)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("name")
    @classmethod
    def validate_safe_name(cls, value: str) -> str:
        normalized = value.strip()
        if (
            normalized in {"", ".", ".."}
            or "/" in normalized
            or "\\" in normalized
            or "\x00" in normalized
        ):
            raise ValueError("unsafe file name")
        return normalized


class SiteServiceRequestHistoryMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    message_id: int = Field(alias="messageId", gt=0)
    author_kind: Literal["customer", "support", "support-team", "support_team"] = Field(
        alias="authorKind"
    )
    is_visible_to_customer: bool = Field(default=True, alias="isVisibleToCustomer")
    created_at: datetime = Field(alias="createdAt")
    text: str = Field(max_length=200_000)
    files: list[SiteServiceRequestFilePayload]

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone is required")
        return value


class SiteServiceRequestTicket(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(gt=0)
    site_id: str = Field(alias="siteId", min_length=1, max_length=32)
    owner_user_id: int = Field(alias="ownerUserId", gt=0)
    title: str = Field(min_length=1, max_length=255)
    phone: str = Field(min_length=1, max_length=64)
    email: str | None = Field(default=None, max_length=320)
    order_number: str | None = Field(default=None, alias="orderNumber", max_length=64)
    request_type: Literal[
        "warranty",
        "refund_money",
        "replacement",
        "delivery_return",
        "consultation",
        "other",
    ] = Field(alias="requestType")
    is_closed: bool = Field(alias="isClosed")


class SiteServiceRequestEventPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: Literal[1] = Field(alias="schemaVersion")
    event_id: str = Field(alias="eventId", min_length=1, max_length=255)
    event_type: str = Field(
        alias="eventType",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_.-]+$",
    )
    occurred_at: datetime = Field(alias="occurredAt")
    ticket: SiteServiceRequestTicket
    history: list[SiteServiceRequestHistoryMessage] = Field(min_length=1)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone is required")
        return value

    @model_validator(mode="after")
    def validate_event_identity(self) -> SiteServiceRequestEventPayload:
        match = _EVENT_ID.fullmatch(self.event_id)
        if match is None:
            raise ValueError("eventId has invalid format")
        ticket_id, message_id = (int(value) for value in match.groups())
        if ticket_id != self.ticket.id:
            raise ValueError("eventId ticket does not match ticket.id")

        message_ids = [message.message_id for message in self.history]
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("history contains duplicate messageId")
        if message_id not in set(message_ids):
            raise ValueError("eventId message is missing from history")

        file_keys = [
            (message.message_id, file.file_id) for message in self.history for file in message.files
        ]
        if len(file_keys) != len(set(file_keys)):
            raise ValueError("history contains duplicate file")
        return self

    @property
    def source_message_id(self) -> int:
        match = _EVENT_ID.fullmatch(self.event_id)
        assert match is not None
        return int(match.group(2))


class SiteServiceRequestEventAcceptedResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    event_id: str = Field(alias="eventId")
    status: Literal["accepted"]
    duplicate: bool
    missing_file_ids: list[int] = Field(alias="missingFileIds")


class SiteServiceRequestSnapshotAcceptedResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ticket_id: int = Field(alias="ticketId", gt=0)
    snapshot_message_id: int = Field(alias="snapshotMessageId", gt=0)
    status: Literal["accepted"]


class SiteServiceEmailEventPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: Literal[1] = Field(alias="schemaVersion")
    event_id: str = Field(alias="eventId", min_length=1, max_length=255)
    event_type: Literal["email.received", "email.replied"] = Field(alias="eventType")
    occurred_at: datetime = Field(alias="occurredAt")
    mailbox: Literal["shop", "info"]
    message_id: int = Field(alias="messageId", gt=0)
    activity_id: int = Field(alias="activityId", gt=0)
    thread_id: int = Field(alias="threadId", gt=0)
    request_type: Literal[
        "warranty",
        "refund_money",
        "replacement",
        "delivery_return",
        "consultation",
        "other",
    ] = Field(alias="requestType")
    order_number: str = Field(alias="orderNumber", min_length=1, max_length=64)
    crm_contact_id: int = Field(alias="crmContactId", gt=0)
    crm_deal_id: int = Field(alias="crmDealId", gt=0)
    existing_service_item_id: int | None = Field(
        default=None,
        alias="existingServiceItemId",
        gt=0,
    )

    @field_validator("occurred_at")
    @classmethod
    def require_email_event_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone is required")
        return value

    @model_validator(mode="after")
    def validate_email_event_identity(self) -> SiteServiceEmailEventPayload:
        match = _EMAIL_EVENT_ID.fullmatch(self.event_id)
        if match is None:
            raise ValueError("eventId has invalid format")
        mailbox, message_id = match.groups()
        if mailbox != self.mailbox or int(message_id) != self.message_id:
            raise ValueError("eventId does not match email source")
        return self

    @property
    def source_key(self) -> str:
        return f"bitrix-mail:{self.mailbox}:{self.thread_id}"


class SiteServiceRequestFileStagedResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    event_id: str = Field(alias="eventId")
    file_id: int = Field(alias="fileId", gt=0)
    status: Literal["staged", "uploaded", "failed"]
    duplicate: bool
    error_code: Literal["file_unavailable"] | None = Field(
        default=None,
        alias="errorCode",
    )


class SiteServiceRequestCommandFilePayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    file_id: int = Field(alias="fileId", gt=0)
    name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(alias="mimeType", min_length=1, max_length=255)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    download_path: str = Field(alias="downloadPath", min_length=1, max_length=512)


class SiteServiceRequestCommandPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    command_id: int = Field(alias="commandId", gt=0)
    command_key: str = Field(alias="commandKey", min_length=1, max_length=255)
    ticket_id: int = Field(alias="ticketId", gt=0)
    reply_text: str = Field(
        alias="replyText",
        min_length=1,
        max_length=SITE_SERVICE_REQUEST_REPLY_MAX_LENGTH,
    )
    lease_until: datetime = Field(alias="leaseUntil")
    lease_token: str = Field(alias="leaseToken", min_length=32, max_length=128)
    files: list[SiteServiceRequestCommandFilePayload] = Field(default_factory=list, max_length=20)


class SiteServiceRequestCommandsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: Literal[1] = Field(default=1, alias="schemaVersion")
    commands: list[SiteServiceRequestCommandPayload] = Field(max_length=20)


class SiteServiceRequestCommandAckPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: Literal[1] = Field(alias="schemaVersion")
    lease_token: str = Field(alias="leaseToken", min_length=32, max_length=128)
    status: Literal["applied", "failed"]
    ticket_id: int | None = Field(default=None, alias="ticketId", gt=0)
    message_id: int | None = Field(default=None, alias="messageId", gt=0)
    applied_at: datetime | None = Field(default=None, alias="appliedAt")
    error_code: (
        Literal[
            "ticket_not_found",
            "support_user_invalid",
            "message_write_failed",
            "attachment_download_failed",
            "attachment_write_failed",
        ]
        | None
    ) = Field(default=None, alias="errorCode")

    @model_validator(mode="after")
    def validate_ack_shape(self) -> SiteServiceRequestCommandAckPayload:
        if self.status == "applied":
            if (
                self.ticket_id is None
                or self.message_id is None
                or self.applied_at is None
                or self.error_code is not None
            ):
                raise ValueError("applied ack fields are invalid")
            if self.applied_at.tzinfo is None or self.applied_at.utcoffset() is None:
                raise ValueError("timezone is required")
        elif (
            self.error_code is None
            or self.ticket_id is not None
            or self.message_id is not None
            or self.applied_at is not None
        ):
            raise ValueError("failed ack fields are invalid")
        return self


class SiteServiceRequestCommandAckResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    command_id: int = Field(alias="commandId", gt=0)
    status: Literal["applied", "failed"]
    duplicate: bool


class SiteServiceRequestHealthResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: Literal["healthy", "degraded", "disabled"]
    alert_codes: list[
        Literal[
            "event_lag",
            "dead_letter",
            "assignment_failure",
            "outbound_failure",
            "escalation_delivery_pending",
            "file_failure",
            "worker_failure",
            "worker_stale",
        ]
    ] = Field(alias="alertCodes")
    pending_events: int = Field(alias="pendingEvents", ge=0)
    failed_events: int = Field(alias="failedEvents", ge=0)
    oldest_pending_lag_seconds: int | None = Field(
        alias="oldestPendingLagSeconds",
        ge=0,
    )
    pending_commands: int = Field(alias="pendingCommands", ge=0)
    unlinked_cases: int = Field(alias="unlinkedCases", ge=0)
    assignment_failures: int = Field(alias="assignmentFailures", ge=0)
    outbound_failures: int = Field(alias="outboundFailures", ge=0)
    pending_escalation_deliveries: int = Field(alias="pendingEscalationDeliveries", ge=0)
    failed_files: int = Field(alias="failedFiles", ge=0)
    worker_failure: bool = Field(alias="workerFailure")
    worker_stale: bool = Field(alias="workerStale")
    worker_last_started_at: datetime | None = Field(alias="workerLastStartedAt")
    worker_last_success_at: datetime | None = Field(alias="workerLastSuccessAt")
    worker_last_failure_at: datetime | None = Field(alias="workerLastFailureAt")
    worker_last_error_code: str | None = Field(
        alias="workerLastErrorCode",
        max_length=128,
    )
    worker_consecutive_failures: int = Field(alias="workerConsecutiveFailures", ge=0)
    last_successful_exchange_at: datetime | None = Field(
        alias="lastSuccessfulExchangeAt",
    )
    ingest_enabled: bool = Field(alias="ingestEnabled")
    email_ingest_enabled: bool = Field(alias="emailIngestEnabled")
    bitrix_writes_enabled: bool = Field(alias="bitrixWritesEnabled")
    outbound_replies_enabled: bool = Field(alias="outboundRepliesEnabled")


class SiteServiceRequestUiSessionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    access_token: str = Field(alias="accessToken", min_length=1, max_length=4096)
    domain: str = Field(min_length=1, max_length=255)
    member_id: str = Field(alias="memberId", min_length=1, max_length=255)
    placement: Literal["CRM_DYNAMIC_1134_DETAIL_TAB"]
    item_id: int = Field(alias="itemId", gt=0)


class SiteServiceRequestUiUser(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=255)
    is_admin: bool = Field(alias="isAdmin")


class SiteServiceRequestUiSessionResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_token: str = Field(alias="sessionToken")
    expires_at: datetime = Field(alias="expiresAt")
    item_id: int = Field(alias="itemId", gt=0)
    user: SiteServiceRequestUiUser


class SiteServiceRequestConversationAttachment(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    mime_type: str = Field(alias="mimeType")
    size: int = Field(ge=0)
    status: Literal["pending", "staged", "uploaded", "leased", "applied", "failed"]
    download_url: str | None = Field(default=None, alias="downloadUrl")


class SiteServiceRequestConversationMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    direction: Literal["inbound", "outbound", "internal"]
    author_label: str = Field(alias="authorLabel")
    text: str | None
    created_at: datetime = Field(alias="createdAt")
    delivery_status: Literal["received", "sending", "delivered", "failed", "note"] = Field(
        alias="deliveryStatus"
    )
    error_code: str | None = Field(default=None, alias="errorCode", max_length=128)
    retryable: bool = False
    visible_to_customer: bool = Field(alias="visibleToCustomer")
    attachments: list[SiteServiceRequestConversationAttachment] = Field(default_factory=list)


class SiteServiceRequestConversationResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    item_id: int = Field(alias="itemId", gt=0)
    source_kind: Literal["site_ticket", "bitrix_mail"] = Field(alias="sourceKind")
    can_reply: bool = Field(alias="canReply")
    original_url: str | None = Field(default=None, alias="originalUrl")
    next_before_id: int | None = Field(default=None, alias="nextBeforeId")
    messages: list[SiteServiceRequestConversationMessage]


class SiteServiceRequestConversationMutationResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    duplicate: bool
    status: Literal["pending", "leased", "applied", "failed", "note"]


class SiteServiceRequestInternalNoteRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    client_request_id: str = Field(
        alias="clientRequestId", min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    )
    text: str = Field(min_length=1, max_length=20_000)
