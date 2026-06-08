from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class MatchStatus(StrEnum):
    none = "none"
    live_candidates = "live_candidates"
    candidates = "candidates"
    auto = "auto"
    manual = "manual"
    matched = "matched"
    ambiguous = "ambiguous"
    uncertain = "uncertain"
    multiple = "multiple"


class ProductSort(StrEnum):
    default = "default"
    name_asc = "name_asc"


class CurrentMatch(BaseModel):
    competitor_item_id: int | None = None
    competitor_id: int | None = None
    competitor_name: str | None = None
    sku: str | None = None
    name: str | None = None
    url: str | None = None
    price: float | None = None
    confidence: float | None = None
    status: str | None = None
    mode: str = Field(default="manual", description="auto|manual")


class CandidateFacetOption(BaseModel):
    value: str
    label: str
    count: int


class ProductFacets(BaseModel):
    subjects: list[CandidateFacetOption] = Field(default_factory=list)
    brands: list[CandidateFacetOption] = Field(default_factory=list)
    categories: list[CandidateFacetOption] = Field(default_factory=list)
    compatibility_brands: list[CandidateFacetOption] = Field(default_factory=list)


class ProductRow(BaseModel):
    id: int
    name: str
    article: str
    brand: str | None = None
    category: str | None = None
    subject: str | None = None
    status: MatchStatus
    current_match: CurrentMatch | None = None
    candidate_previews: list[CurrentMatch] = Field(default_factory=list)
    accepted_count: int = 0
    suggested_count: int = 0
    review_count: int = 0
    live_candidate_count: int = 0
    compatibility_models: list[str] = Field(default_factory=list)


class PropertySummary(BaseModel):
    total: int = 0
    matched: int = 0
    missing: int = 0
    conflict: int = 0
    unmapped: int = 0
    status: str
    label: str
    conflicts: list[str] = Field(default_factory=list)
    block_conflict: int = 0
    review_conflict: int = 0
    hint_conflict: int = 0


class CompatibilityHint(BaseModel):
    status: Literal["existing", "inferred_model", "inferred_code", "required", "not_required"] = (
        "not_required"
    )
    label: str = ""
    detail: str = ""
    matched_values: list[str] = Field(default_factory=list)


class Candidate(BaseModel):
    competitor_item_id: int | None = None
    competitor_id: int | None = None
    competitor_name: str | None = None
    sku: str | None = None
    name: str | None = None
    url: str | None = None
    price: float | None = None
    in_stock: bool | None = None
    confidence: float | None = None
    status: str | None = None
    item_type: str | None = None
    category_group: str | None = None
    brand: str | None = None
    model: str | None = None
    quality: str | None = None
    color: str | None = None
    score: float | None = None
    reason: str | None = None
    needs_compat_review: bool = False
    compatibility_hint: CompatibilityHint = Field(default_factory=CompatibilityHint)
    last_seen_at: datetime | None = None
    attrs: dict[str, Any] | None = None
    property_summary: PropertySummary | None = None


class CandidateFacets(BaseModel):
    sources: list[CandidateFacetOption] = Field(default_factory=list)
    item_types: list[CandidateFacetOption] = Field(default_factory=list)
    category_groups: list[CandidateFacetOption] = Field(default_factory=list)
    brands: list[CandidateFacetOption] = Field(default_factory=list)
    qualities: list[CandidateFacetOption] = Field(default_factory=list)
    colors: list[CandidateFacetOption] = Field(default_factory=list)


class PaginatedProducts(BaseModel):
    items: list[ProductRow]
    page: int
    page_size: int
    total: int
    facets: ProductFacets | None = None


class PaginatedCandidates(BaseModel):
    items: list[Candidate]
    total: int
    facets: CandidateFacets | None = None


class MatchRequest(BaseModel):
    competitor_item_id: int | None = None
    competitor_id: int | None = None
    confidence: float | None = None
    mode: str | None = None
    reason: str | None = None


class RejectRequest(BaseModel):
    competitor_item_id: int | None = None
    competitor_id: int | None = None
    reason: str | None = None


class RevokeRequest(BaseModel):
    competitor_item_id: int
    reason: str | None = None


class MatchingActionResponse(BaseModel):
    ok: bool = True
    match: CurrentMatch | None = None


class DecisionHistoryItem(BaseModel):
    id: int
    product_id: int
    competitor_item_id: int
    competitor_name: str | None = None
    sku: str | None = None
    name: str | None = None
    action: Literal["accept", "reject", "revoke"]
    reason: str | None = None
    created_by: str | None = None
    created_at: datetime
    previous_product_id: int | None = None
    previous_status: str | None = None


class DecisionHistoryResponse(BaseModel):
    items: list[DecisionHistoryItem]


class PropertyComparisonItem(BaseModel):
    property_key: str
    label: str
    product_value: str | None = None
    competitor_value: str | None = None
    mapped_value: str | None = None
    status: str
    severity: str
    comparison_mode: str


class PropertyComparisonResponse(BaseModel):
    profile_id: int
    profile_code: str
    profile_name: str
    summary: PropertySummary
    items: list[PropertyComparisonItem] = Field(default_factory=list)


class PropertyProfile(BaseModel):
    id: int
    code: str
    name: str
    item_type: str | None = None
    sort_order: int
    is_active: bool


class PropertyRule(BaseModel):
    id: int
    profile_id: int
    profile_code: str | None = None
    property_key: str
    label: str
    product_field: str
    competitor_field: str
    comparison_mode: str
    severity: str
    config_json: dict[str, Any] | None = None
    sort_order: int
    is_active: bool
    default_label: str | None = None
    default_product_field: str | None = None
    default_competitor_field: str | None = None
    default_comparison_mode: str | None = None
    default_severity: str | None = None
    default_config_json: dict[str, Any] | None = None
    default_sort_order: int | None = None
    has_default_drift: bool = False


class PropertyRuleRequest(BaseModel):
    profile_id: int | None = None
    profile_code: str | None = None
    property_key: str
    label: str
    product_field: str
    competitor_field: str
    comparison_mode: str = "exact"
    severity: str = "review"
    config_json: dict[str, Any] | None = None
    sort_order: int = 100
    is_active: bool = True


class PropertyRulePatch(BaseModel):
    property_key: str | None = None
    label: str | None = None
    product_field: str | None = None
    competitor_field: str | None = None
    comparison_mode: str | None = None
    severity: str | None = None
    config_json: dict[str, Any] | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class PropertyValueMap(BaseModel):
    id: int
    rule_id: int
    profile_code: str | None = None
    property_key: str | None = None
    competitor_source: str | None = None
    competitor_value: str
    mapped_value: str
    notes: str | None = None
    is_active: bool


class PropertyValueMapRequest(BaseModel):
    rule_id: int
    competitor_source: str | None = None
    competitor_value: str
    mapped_value: str
    notes: str | None = None
    is_active: bool = True


class PropertyValueMapPatch(BaseModel):
    competitor_source: str | None = None
    competitor_value: str | None = None
    mapped_value: str | None = None
    notes: str | None = None
    is_active: bool | None = None


class PropertyValueSuggestion(BaseModel):
    rule_id: int
    property_key: str
    competitor_source: str | None = None
    competitor_value: str
    count: int
    sample_competitor_item_id: int
    sample_name: str | None = None
    suggested_mapped_value: str | None = None
    safe_auto: bool = False
    safe_reason: str | None = None


class AcceptSafePropertyValueSuggestionsRequest(BaseModel):
    profile_code: str
    rule_id: int | None = None
    source: str | None = None
    limit: int = 100


class AcceptSafePropertyValueSuggestionsResponse(BaseModel):
    created_count: int
    skipped_count: int
    created: list[PropertyValueSuggestion] = Field(default_factory=list)


class CompatibilitySummary(BaseModel):
    brands: int
    brand_aliases: int
    phone_models: int
    product_links: int
    competitor_links: int
    unresolved_product_values: int
    unresolved_competitor_values: int
    blocked_values: int


class CompatibilityBrand(BaseModel):
    id: int
    code: str
    name: str
    display_name: str
    group_code: str | None = None
    is_active: bool
    models_count: int = 0
    unresolved_count: int = 0


class CompatibilityBrandRequest(BaseModel):
    code: str
    name: str | None = None
    display_name: str | None = None
    group_code: str | None = None


class CompatibilityBrandAliasRequest(BaseModel):
    brand_id: int
    raw_value: str
    source: str = "manual"


class CompatibilityBrandAlias(BaseModel):
    id: int
    brand_id: int
    brand_display_name: str | None = None
    source: str
    raw_value: str
    normalized_key: str
    confidence: float | None = None
    is_manual: bool
    is_active: bool
    decision_reason: str | None = None
    first_seen_at: datetime
    last_seen_at: datetime


class CompatibilityBrandAliasPatch(BaseModel):
    is_active: bool | None = None


class CompatibilityPhoneModel(BaseModel):
    id: int
    brand_id: int | None = None
    brand_code: str | None = None
    brand_display_name: str | None = None
    brand: str
    model_name: str
    variant: str | None = None
    is_active: bool
    aliases_count: int = 0
    product_links_count: int = 0
    competitor_links_count: int = 0
    suggestion_kind: str | None = None


class CompatibilityPhoneModelRequest(BaseModel):
    brand_id: int
    model_name: str
    variant: str | None = None


class CompatibilityUnresolvedItem(BaseModel):
    entity_type: str
    entity_id: int
    source: str | None = None
    raw_value: str
    raw_brand: str | None = None
    raw_model: str | None = None
    raw_variant: str | None = None
    normalized_key: str
    brand_id: int | None = None
    brand_display_name: str | None = None
    sample_name: str | None = None
    current_phone_model_ids: list[int] = Field(default_factory=list)


class CompatibilityUnresolvedGroup(BaseModel):
    group_key: str
    entity_type: str
    source: str | None = None
    raw_value: str
    raw_brand: str | None = None
    raw_model: str | None = None
    raw_variant: str | None = None
    normalized_key: str
    brand_id: int | None = None
    brand_display_name: str | None = None
    affected_count: int
    product_count: int
    competitor_count: int
    examples: list[CompatibilityUnresolvedItem] = Field(default_factory=list)
    suggested_phone_models: list[CompatibilityPhoneModel] = Field(default_factory=list)
    safe_auto_model_id: int | None = None
    is_noise_candidate: bool = False


class CompatibilityPreviewRequest(BaseModel):
    group_key: str | None = None
    entity_type: str | None = None
    source: str | None = None
    raw_value: str | None = None
    raw_brand: str | None = None
    raw_model: str | None = None
    raw_variant: str | None = None
    brand_id: int | None = None
    target_phone_model_ids: list[int] = Field(default_factory=list)


class CompatibilityPreviewResponse(BaseModel):
    preview_token: str
    affected_count: int
    affected_product_count: int
    affected_competitor_count: int
    target_phone_model_ids: list[int] = Field(default_factory=list)
    target_phone_models: list[CompatibilityPhoneModel] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    items: list[CompatibilityUnresolvedItem] = Field(default_factory=list)


class CompatibilityApplyRequest(CompatibilityPreviewRequest):
    preview_token: str | None = None
    scope: str = "previewed"
    notes: str | None = None


class CompatibilityBlockRequest(BaseModel):
    group_key: str | None = None
    entity_type: str | None = None
    source: str | None = None
    raw_value: str | None = None
    raw_brand: str | None = None
    raw_model: str | None = None
    raw_variant: str | None = None
    reason: str | None = None
    notes: str | None = None


class CompatibilityApplyResponse(BaseModel):
    preview_token: str
    affected_count: int
    product_links_created: int
    competitor_links_created: int
    decisions_created: int


class CompatibilityHistoryItem(BaseModel):
    action: str
    source: str | None = None
    raw_value: str
    normalized_key: str
    brand_id: int | None = None
    brand_display_name: str | None = None
    phone_model_ids: list[int] = Field(default_factory=list)
    phone_model_labels: list[str] = Field(default_factory=list)
    actor: str | None = None
    notes: str | None = None
    reason: str | None = None
    affected_count: int
    created_at: datetime
