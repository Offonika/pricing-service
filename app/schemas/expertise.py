from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

DecisionCode = Literal["approved", "rejected"]
CompletionOutcome = Literal["returned_to_central_defect", "returned_to_store"]


class ExpertiseSyncAttachmentItem(BaseModel):
    attachment_kind: str
    storage_ref: str
    comment: str | None = None


class ExpertiseCaseSyncItem(BaseModel):
    external_id: str
    onec_expertise_ref: str
    onec_expertise_number: str
    created_at_source: datetime
    organization_ref: str | None = None
    contract_ref: str | None = None
    linked_sale_ref: str | None = None
    linked_sale_number: str | None = None
    store_external_id: str | None = None
    store_name: str | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    problem_summary: str | None = None
    current_status: str | None = None
    decision_code: DecisionCode | None = None
    decision_label: str | None = None
    decision_comment: str | None = None
    client_notified: bool = False
    due_at: datetime | None = None
    owner_user_external_id: str
    linked_customer_order_ref: str | None = None
    linked_customer_order_number: str | None = None
    bitrix_entity_id: str | None = None
    bitrix_disk_folder_id: str | None = None
    bitrix_disk_folder_url: str | None = None
    bitrix_notify_task_id: str | None = None
    bitrix_last_sync_at: datetime | None = None
    bitrix_last_error: str | None = None
    payload: dict[str, Any]
    attachments: list[ExpertiseSyncAttachmentItem] | None = None
    idempotency_key: str | None = None


class ExpertiseSyncResponse(BaseModel):
    created: int
    updated: int


class ExpertiseCaseAttachmentResponse(BaseModel):
    id: int
    attachment_kind: str
    storage_ref: str
    comment: str | None = None
    created_at: datetime


class ExpertiseCaseListItem(BaseModel):
    id: int
    external_id: str
    onec_expertise_ref: str | None = None
    onec_expertise_number: str | None = None
    created_at_source: datetime | None = None
    organization_ref: str | None = None
    contract_ref: str | None = None
    linked_sale_ref: str | None = None
    linked_sale_number: str | None = None
    store_external_id: str | None = None
    store_name: str | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    problem_summary: str | None = None
    current_status: str
    decision_code: str | None = None
    decision_label: str | None = None
    decision_comment: str | None = None
    linked_customer_order_ref: str | None = None
    linked_customer_order_number: str | None = None
    client_notified: bool
    due_at: datetime | None = None
    owner_user_external_id: str | None = None
    bitrix_entity_id: str | None = None
    bitrix_disk_folder_id: str | None = None
    bitrix_disk_folder_url: str | None = None
    bitrix_notify_task_id: str | None = None
    bitrix_last_sync_at: datetime | None = None
    bitrix_last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class ExpertiseCaseDetailResponse(ExpertiseCaseListItem):
    payload: dict[str, Any] | None = None
    attachments: list[ExpertiseCaseAttachmentResponse] = Field(default_factory=list)


class ExpertiseCaseActionRequest(BaseModel):
    actor_external_id: str
    comment: str | None = None
    idempotency_key: str | None = None


class ExpertiseCaseDecisionRequest(ExpertiseCaseActionRequest):
    decision_code: DecisionCode
    decision_comment: str | None = None


class ExpertiseCaseCompletionRequest(ExpertiseCaseActionRequest):
    completion_outcome: CompletionOutcome


class ExpertiseCaseEventResponse(BaseModel):
    id: int
    event_type: str
    event_at: datetime
    actor_external_id: str | None = None
    source: str
    comment: str | None = None
    meta: dict[str, Any] | None = None
    created_at: datetime
