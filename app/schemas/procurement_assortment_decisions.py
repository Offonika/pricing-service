from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ProcurementAssortmentDecision(BaseModel):
    item_id: str
    entity_type_id: int
    title: str = ""
    sku_code: str = ""
    sku_name: str = ""
    status_decision: str = "no_change"
    status_decision_label: str = "Без изменения"
    status_reason: str = ""
    status_approved_by: str = ""
    status_changed_at: str = ""
    commercial_marks: list[str] = Field(default_factory=list)
    sync_blockers: list[str] = Field(default_factory=list)
    manual_override_preview: dict[str, Any] | None = None


class ProcurementAssortmentDecisionUpdateRequest(BaseModel):
    status_decision: str = Field(default="no_change")
    status_reason: str = ""
    status_approved_by: str = ""
    status_changed_at: str = ""
    commercial_marks: list[str] = Field(default_factory=list)


class ProcurementAssortmentDecisionUpdateResponse(BaseModel):
    decision: ProcurementAssortmentDecision
    updated: bool = True


class ProcurementAssortmentDecisionSyncResponse(BaseModel):
    decision: ProcurementAssortmentDecision
    synced: bool
    merge_action: str = ""
    manual_overrides_path: str
    blockers: list[str] = Field(default_factory=list)
