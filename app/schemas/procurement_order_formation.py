from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProcurementOrderFormationSessionRequest(BaseModel):
    access_token: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    member_id: str = Field(min_length=1)


class ProcurementOrderFormationUser(BaseModel):
    user_id: str
    name: str | None = None


class ProcurementOrderFormationSessionResponse(BaseModel):
    session_token: str
    token_type: str = "bearer"
    expires_at: datetime
    expires_in: int
    user: ProcurementOrderFormationUser


class ProcurementClassificationProposalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    previous_status: str | None = None
    proposed_status: str
    proposed_status_label: str
    reason: str
    manual_minimum: Decimal | None = None
    review_date: date | None = None
    replacement_sku_code: str | None = None
    replacement_sku_name: str | None = None
    blocks_order_line: bool
    requested_at: datetime
    requested_by_bitrix_user_id: str
    requested_by_name: str | None = None
    approved_at: datetime | None = None
    approved_by_bitrix_user_id: str | None = None
    approved_by_name: str | None = None
    rejected_at: datetime | None = None
    rejected_by_bitrix_user_id: str | None = None
    rejected_by_name: str | None = None
    rejection_reason: str | None = None
    can_approve: bool = False
    can_reject: bool = False
    # Предложение подал сам текущий пользователь: своё решение согласовывает
    # второй сотрудник, кроме статуса «Допродаём».
    self_proposed: bool = False
    onec_status: str
    onec_message_id: str | None = None
    onec_error: str | None = None
    bitrix_readback_value: str | None = None
    reflected_at: datetime | None = None


class DisplayFamilyOrderRecommendationRead(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(alias="schema")
    mode: str
    status: str
    registry_version_number: int | None = None
    registry_inventory_checksum: str = ""
    family_record_id: int | None = None
    family_id: str = ""
    family_label: str = ""
    registry_member_count: int | None = None
    calculation_member_count: int | None = None
    segment_id: str = ""
    quality_segment: str = ""
    construction_segment: str = ""
    baseline_order_qty: Decimal
    allocated_order_qty: Decimal
    family_pool_order_qty: Decimal
    segment_pool_order_qty: Decimal
    baseline_share_pct: Decimal
    target_share_pct: Decimal
    allocation_source: str = ""
    confidence: str
    manual_approval_required: bool
    registry_warning_codes: list[str] = Field(default_factory=list)
    conflict_codes: list[str] = Field(default_factory=list)
    reason_ru: str = ""
    matching_review_confirmed: bool = False
    matching_review_confirmed_at: datetime | None = None
    matching_review_confirmed_by: str | None = None


class ProcurementBlockerResolutionRead(BaseModel):
    kind: str
    label: str
    requires_reason: bool = False
    requires_replacement: bool = False


class ProcurementBlockerDetailRead(BaseModel):
    code: str
    scope: str
    severity: str
    line_id: int | None = None
    line_number: int | None = None
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    resolution_actions: list[ProcurementBlockerResolutionRead] = Field(default_factory=list)


class ProcurementOrderFormationLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    line_number: int
    version: int
    bitrix_product_id: str | None = None
    bitrix_product_xml_id: str
    nomenclature_ref: str
    nomenclature_code: str | None = None
    nomenclature_name: str
    recommended_quantity: Decimal
    final_quantity: Decimal
    purchase_price: Decimal
    amount: Decimal
    currency: str
    source_kind: str
    explicit_demand: bool
    risk_level: str | None = None
    risk_codes: list[str] = Field(default_factory=list)
    recommendation_reason: str | None = None
    blockers: list[str] = Field(default_factory=list)
    blocker_details: list[ProcurementBlockerDetailRead] = Field(default_factory=list)
    assortment_status: str | None = None
    lifecycle_status: str | None = None
    quality: str | None = None
    procurement_profile: str | None = None
    manual_minimum: Decimal | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    display_family_recommendation: DisplayFamilyOrderRecommendationRead | None = None
    removed: bool
    effective_assortment_status: str | None = None
    effective_assortment_status_label: str | None = None
    latest_classification: ProcurementClassificationProposalRead | None = None
    photo_thumbnail_url: str | None = None
    photo_original_url: str | None = None
    product_card_url: str | None = None
    photo_source: str | None = None
    photo_count: int = 0
    profitability_pct: Decimal | None = None
    profitability_status: str | None = None
    profitability_source: str | None = None
    profitability_explanation: str | None = None
    metrics_as_of: date | None = None
    metrics_window_days: int | None = None
    product_defect_pct: Decimal | None = None
    product_defect_history_units: int | None = None
    product_defect_confidence: str | None = None
    product_defect_source: str | None = None
    supplier_defect_pct: Decimal | None = None
    supplier_defect_history_units: int | None = None
    supplier_defect_confidence: str | None = None
    supplier_defect_attribution: str | None = None
    supplier_defect_source_status: str | None = None
    price_change_pct: Decimal | None = None
    price_change_status: str | None = None
    price_history_count: int | None = None
    price_history_currency_ref: str | None = None
    price_history_expected_currency: str | None = None
    price_history_available_currencies: list[str] = Field(default_factory=list)
    supplier_prepare_days: int | None = None
    logistics_days: int | None = None
    lead_time_days: int | None = None
    lead_time_source_level: str | None = None
    lead_time_confidence: str | None = None
    delivery_days: int | None = None


class ProcurementSupplierProfileRead(BaseModel):
    supplier_ref: str | None = None
    supplier_code: str | None = None
    supplier_name: str | None = None
    version: int = 0
    qualification_class: str | None = None
    qualification_label: str | None = None
    class_description: str | None = None
    profitability_pct: Decimal | None = None
    defect_pct: Decimal | None = None
    defect_history_units: int | None = None
    defect_confidence: str | None = None
    defect_attribution: str = "unconfirmed"
    on_time_pct: Decimal | None = None
    payment_terms: str | None = None
    credit_days: int | None = None
    credit_limit: Decimal | None = None
    terms_source: str = "onec_contract"
    terms_status: str = "missing"
    advantages: list[str] = Field(default_factory=list)
    internal_note: str | None = None
    history_order_count: int | None = None
    supplier_prepare_days: int | None = None
    logistics_days: int | None = None
    lead_time_days: int | None = None
    lead_time_confidence: str | None = None
    price_history_count: int | None = None
    facts_updated_at: datetime | None = None
    manual_updated_at: datetime | None = None
    manual_updated_by_name: str | None = None
    updated_at: datetime | None = None
    data_status: str = "missing"
    can_edit: bool = False


class ProcurementOrderFormationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    stable_key: str
    status: str
    version: int
    bitrix_entity_type_id: int | None = None
    bitrix_item_id: str | None = None
    bitrix_category_id: int | None = None
    bitrix_stage_id: str | None = None
    bitrix_item_url: str | None = None
    supplier_ref: str | None = None
    supplier_code: str | None = None
    supplier_name: str
    contract_ref: str | None = None
    contract_code: str | None = None
    contract_name: str
    warehouse_ref: str | None = None
    warehouse_code: str | None = None
    warehouse_name: str
    currency: str
    procurement_contour: str
    route: str
    batch_id: str
    order_date: date
    responsible_bitrix_user_id: str | None = None
    responsible_name: str | None = None
    calculation_id: str
    source_run_id: str | None = None
    approved_version: int | None = None
    approved_at: datetime | None = None
    approved_by_bitrix_user_id: str | None = None
    approved_by_name: str | None = None
    onec_status: str
    onec_message_id: str | None = None
    onec_document_ref: str | None = None
    onec_document_number: str | None = None
    onec_document_date: date | None = None
    onec_error: str | None = None
    blockers: list[str] = Field(default_factory=list)
    blocker_details: list[ProcurementBlockerDetailRead] = Field(default_factory=list)
    total_amount: Decimal = Decimal("0")
    lines: list[ProcurementOrderFormationLineRead] = Field(default_factory=list)
    manual_status_options: dict[str, str] = Field(default_factory=dict)
    supplier_profile: ProcurementSupplierProfileRead = Field(
        default_factory=ProcurementSupplierProfileRead
    )


class ProcurementOrderAssistantSummary(BaseModel):
    lines: int = 0
    ready_lines: int = 0
    supplier_missing_lines: int = 0
    price_changed_lines: int = 0
    low_profitability_lines: int = 0
    high_defect_lines: int = 0
    photo_missing_lines: int = 0
    orders: int = 0


class ProcurementOrderAssistantResponse(BaseModel):
    updated_at: datetime | None = None
    summary: ProcurementOrderAssistantSummary
    orders: list[ProcurementOrderFormationRead] = Field(default_factory=list)


class ProcurementOrderAssistantAssembleItem(BaseModel):
    order_id: int
    expected_version: int = Field(ge=1)


class ProcurementOrderAssistantAssembleRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=255)
    items: list[ProcurementOrderAssistantAssembleItem] = Field(min_length=1, max_length=100)


class ProcurementOrderAssistantAssembleResult(BaseModel):
    order_id: int
    status: str
    message: str


class ProcurementOrderAssistantAssembleResponse(BaseModel):
    approved: int = 0
    blocked: int = 0
    stale: int = 0
    items: list[ProcurementOrderAssistantAssembleResult] = Field(default_factory=list)


class ProcurementOrderLineUpdateRequest(BaseModel):
    expected_order_version: int = Field(ge=1)
    expected_line_version: int = Field(ge=1)
    final_quantity: Decimal | None = Field(default=None, ge=0)
    purchase_price: Decimal | None = Field(default=None, ge=0)
    removed: bool | None = None
    removal_reason: str | None = Field(default=None, max_length=1000)
    replacement_sku_code: str | None = Field(default=None, max_length=64)
    explicit_demand: bool | None = None

    @model_validator(mode="after")
    def ensure_update_present(self) -> ProcurementOrderLineUpdateRequest:
        if all(
            value is None
            for value in (
                self.final_quantity,
                self.purchase_price,
                self.removed,
                self.explicit_demand,
            )
        ):
            raise ValueError("at least one line field must be provided")
        if self.removed is True and not str(self.removal_reason or "").strip():
            raise ValueError("removal_reason is required when removing a line")
        if self.removed is not True and (
            self.removal_reason is not None or self.replacement_sku_code is not None
        ):
            raise ValueError("removal metadata requires removed=true")
        return self


class ProcurementOrderConditionsUpdateRequest(BaseModel):
    expected_order_version: int = Field(ge=1)
    supplier_ref: str | None = None
    supplier_code: str | None = None
    supplier_name: str | None = None
    contract_ref: str | None = None
    contract_code: str | None = None
    contract_name: str | None = None
    warehouse_ref: str | None = None
    warehouse_code: str | None = None
    warehouse_name: str | None = None
    currency: str | None = None
    procurement_contour: str | None = None
    route: str | None = None
    batch_id: str | None = None
    order_date: date | None = None
    responsible_bitrix_user_id: str | None = None
    responsible_name: str | None = None


class ProcurementClassificationCreateRequest(BaseModel):
    expected_order_version: int = Field(ge=1)
    expected_line_version: int = Field(ge=1)
    proposed_status: str
    reason: str = Field(min_length=1, max_length=4000)
    manual_minimum: Decimal | None = Field(default=None, ge=0)
    review_date: date | None = None
    # Карточка-победитель семьи: обязательна для статусов, снимающих позицию с
    # ведения; пустой код допускается только вместе с no_replacement.
    replacement_sku_code: str | None = Field(default=None, max_length=64)
    no_replacement: bool = False


class ProcurementClassificationRejectRequest(BaseModel):
    expected_order_version: int = Field(ge=1)
    expected_line_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=4000)


class ProcurementClassificationRejectResponse(BaseModel):
    order: ProcurementOrderFormationRead
    proposal: ProcurementClassificationProposalRead


class ProcurementSupplierProfileUpdateRequest(BaseModel):
    expected_version: int = Field(ge=0)
    qualification_class: str | None = Field(default=None, pattern="^[ABCabc]$")
    qualification_label: str | None = Field(default=None, max_length=255)
    advantages: list[str] = Field(default_factory=list, max_length=20)
    internal_note: str | None = Field(default=None, max_length=4000)


class ProcurementOrderTransmissionResponse(BaseModel):
    order: ProcurementOrderFormationRead
    mode: str
    message_id: str
    xml_preview: str
    written_path: str | None = None


class ProcurementClassificationApprovalResponse(BaseModel):
    order: ProcurementOrderFormationRead
    proposal: ProcurementClassificationProposalRead
    mode: str
    message_id: str
    xml_preview: str
    written_path: str | None = None


class ProcurementDashboardCard(BaseModel):
    status: str
    label: str
    # Прежнее название статуса: витрина показывает его мелкой строкой под
    # действующим, чтобы старые отчёты и договорённости читались без перевода.
    legacy_label: str = ""
    total_count: int = 0
    action_count: int = 0
    action_kind: str
    action_label: str
    target_status: str | None = None
    action_breakdown: dict[str, int] = Field(default_factory=dict)
    ready_count: int = 0
    blocked_count: int = 0
    review_count: int = 0
    overdue_count: int = 0
    urgency: str = "neutral"


class ProcurementDashboardDecisionSummary(BaseModel):
    ready_count: int = 0
    review_count: int = 0
    blocked_count: int = 0


class ProcurementDashboardAttentionItem(BaseModel):
    proposal_id: int | None = None
    nomenclature_code: str
    product_name: str
    current_status: str
    current_status_label: str
    kind: str = "lifecycle"
    filter_status: str
    action_label: str
    fact_summary: str
    decision_state: str
    decision_state_label: str
    reason: str
    recommendation: str
    deadline_label: str
    urgency: str


class ProcurementDashboardResponse(BaseModel):
    folder: str
    responsible_user_id: str
    responsible_name: str
    run_id: int | None = None
    run_key: str | None = None
    updated_at: datetime | None = None
    cards: list[ProcurementDashboardCard] = Field(default_factory=list)
    decision_summary: ProcurementDashboardDecisionSummary = Field(
        default_factory=ProcurementDashboardDecisionSummary
    )
    manual_status_counts: dict[str, int] = Field(default_factory=dict)
    attention: list[ProcurementDashboardAttentionItem] = Field(default_factory=list)
    manual_attention: list[ProcurementDashboardAttentionItem] = Field(default_factory=list)


class ProcurementLifecycleTransitionRead(BaseModel):
    proposal_id: int | None = None
    nomenclature_code: str
    nomenclature_ref: str | None = None
    product_guid: str | None = None
    product_name: str
    folder: str
    action_kind: str
    current_status: str
    current_status_label: str
    target_status: str | None = None
    target_status_label: str | None = None
    proposal_status: str
    reason: str
    facts: dict[str, Any] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list)
    risk_codes: list[str] = Field(default_factory=list)
    run_id: int
    run_key: str
    facts_hash: str
    responsible_bitrix_user_id: str | None = None
    responsible_name: str | None = None
    decision_state: str = "view"
    actionability: str = "blocked"
    suggested_manual_status: str | None = None
    ready: bool = False
    selectable: bool = False
    stale: bool = False
    created_at: datetime | None = None


class ProcurementLifecycleTransitionList(BaseModel):
    status: str
    scope: str
    total: int
    page: int
    page_size: int
    ready_count: int
    review_count: int
    blocked_count: int
    stale_count: int
    items: list[ProcurementLifecycleTransitionRead] = Field(default_factory=list)


class ProcurementLifecycleTransitionApprovalItem(BaseModel):
    proposal_id: int
    expected_run_id: int
    expected_current_status: str
    facts_hash: str = Field(min_length=64, max_length=64)


class ProcurementLifecycleTransitionApprovalRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=255)
    items: list[ProcurementLifecycleTransitionApprovalItem] = Field(
        min_length=1,
        max_length=100,
    )


class ProcurementLifecycleTransitionApprovalResult(BaseModel):
    proposal_id: int
    result: str
    message: str = ""


class ProcurementLifecycleTransitionApprovalSummary(BaseModel):
    approved: int = 0
    stale: int = 0
    blocked: int = 0
    conflict: int = 0
    failed: int = 0


class ProcurementLifecycleTransitionApprovalResponse(BaseModel):
    mode: str
    message_id: str | None = None
    xml_preview: str = ""
    written_path: str | None = None
    summary: ProcurementLifecycleTransitionApprovalSummary
    items: list[ProcurementLifecycleTransitionApprovalResult]


class ProcurementLifecycleManualDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(pension|working)$")
    reason: str = Field(min_length=1, max_length=4000)
    replacement_sku_code: str | None = Field(default=None, max_length=64)
    no_replacement: bool = False
    expected_run_id: int = Field(ge=1)
    facts_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_replacement(self) -> ProcurementLifecycleManualDecisionRequest:
        replacement = str(self.replacement_sku_code or "").strip()
        if self.decision == "pension" and not replacement and not self.no_replacement:
            raise ValueError("replacement_sku_code or no_replacement is required for pension")
        if replacement and self.no_replacement:
            raise ValueError("replacement_sku_code and no_replacement are mutually exclusive")
        return self


class ProcurementLifecycleManualDecisionResponse(BaseModel):
    proposal_id: int
    result: str
    message: str
    decision: str
    approved_at: datetime


class ProcurementMatchingReviewConfirmRequest(BaseModel):
    expected_registry_version_number: int = Field(ge=1)
    expected_registry_inventory_checksum: str = Field(min_length=64, max_length=64)


class ProcurementMatchingReviewConfirmationRead(BaseModel):
    order_id: int
    line_id: int
    family_id: int
    nomenclature_code: str
    registry_version_number: int
    registry_inventory_checksum: str
    confirmed_at: datetime
    confirmed_by: str
    idempotent: bool = False


class ProcurementOrderListItem(BaseModel):
    id: int
    stable_key: str
    status: str
    version: int
    supplier_name: str
    contract_name: str
    warehouse_name: str
    currency: str
    route: str
    batch_id: str
    order_date: date
    responsible_name: str | None = None
    source_run_id: str | None = None
    onec_status: str
    onec_document_number: str | None = None
    onec_error: str | None = None
    line_count: int
    total_quantity: Decimal
    total_amount: Decimal
    blockers: list[str] = Field(default_factory=list)
    updated_at: datetime


class ProcurementOrderListSummary(BaseModel):
    orders: int = 0
    lines: int = 0
    quantity: Decimal = Decimal("0")
    amount: Decimal = Decimal("0")


class ProcurementOrderListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    summary: ProcurementOrderListSummary
    items: list[ProcurementOrderListItem] = Field(default_factory=list)


class ProcurementClassificationQueueItem(BaseModel):
    proposal: ProcurementClassificationProposalRead
    order_id: int
    order_version: int
    line_id: int
    line_version: int
    nomenclature_code: str | None = None
    nomenclature_ref: str
    product_name: str
    supplier_name: str
    effective_status: str | None = None


class ProcurementClassificationQueueResponse(BaseModel):
    total: int
    page: int
    page_size: int
    pending: int
    approved_today: int
    readback_conflicts: int
    items: list[ProcurementClassificationQueueItem] = Field(default_factory=list)


class ProcurementOrderFormationEventRead(BaseModel):
    id: int
    order_id: int | None = None
    entity_type: str
    entity_id: str
    event_type: str
    actor: str
    bitrix_user_id: str | None = None
    user_name: str | None = None
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ProcurementOrderFormationEventList(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[ProcurementOrderFormationEventRead] = Field(default_factory=list)


class ProcurementOrderImportRequest(BaseModel):
    stable_key: str
    bitrix_entity_type_id: int | None = None
    bitrix_item_id: str | None = None
    bitrix_category_id: int | None = None
    bitrix_stage_id: str | None = None
    bitrix_item_url: str | None = None
    supplier_ref: str | None = None
    supplier_code: str | None = None
    supplier_name: str
    contract_ref: str | None = None
    contract_code: str | None = None
    contract_name: str
    warehouse_ref: str | None = None
    warehouse_code: str | None = None
    warehouse_name: str
    currency: str = "RUB"
    procurement_contour: str = "ordinary"
    route: str = "ordinary"
    batch_id: str
    order_date: date
    responsible_bitrix_user_id: str | None = None
    responsible_name: str | None = None
    calculation_id: str
    source_run_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
