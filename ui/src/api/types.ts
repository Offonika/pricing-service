export type MatchStatus =
  | "none"
  | "live_candidates"
  | "candidates"
  | "auto"
  | "manual"
  | "matched"
  | "ambiguous"
  | "uncertain"
  | "multiple";

export type ProductSort = "default" | "name_asc";

export interface CurrentMatch {
  competitor_item_id?: number;
  competitor_id?: number;
  competitor_name?: string;
  sku?: string;
  name?: string;
  url?: string;
  price?: number;
  confidence?: number;
  status?: string;
  mode: "auto" | "manual" | "embedding_auto" | "llm_arbitrate";
}

export interface ProductRow {
  id: number;
  name: string;
  article: string;
  brand?: string;
  category?: string;
  subject?: string;
  status: MatchStatus;
  current_match?: CurrentMatch;
  candidate_previews?: CurrentMatch[];
  accepted_count?: number;
  suggested_count?: number;
  review_count?: number;
  live_candidate_count?: number;
  compatibility_models?: string[];
}

export type CompatibilityHintStatus = "existing" | "inferred_model" | "inferred_code" | "required" | "not_required";

export interface CompatibilityHint {
  status: CompatibilityHintStatus;
  label: string;
  detail: string;
  matched_values: string[];
}

export interface Candidate {
  competitor_item_id?: number;
  competitor_id?: number;
  competitor_name?: string;
  sku?: string;
  name?: string;
  url?: string;
  price?: number;
  in_stock?: boolean;
  confidence?: number;
  status?: "available" | "suggested" | "rejected" | "current" | "accepted" | "needs_review" | "ambiguous" | "locked";
  item_type?: string;
  category_group?: string;
  brand?: string;
  model?: string;
  quality?: string;
  color?: string;
  score?: number;
  reason?: string;
  needs_compat_review?: boolean;
  compatibility_hint?: CompatibilityHint;
  last_seen_at?: string;
  attrs?: Record<string, unknown>;
  property_summary?: PropertySummary | null;
}

export interface CandidateFacetOption {
  value: string;
  label: string;
  count: number;
}

export interface CandidateFacets {
  sources: CandidateFacetOption[];
  item_types: CandidateFacetOption[];
  category_groups: CandidateFacetOption[];
  brands: CandidateFacetOption[];
  qualities: CandidateFacetOption[];
  colors: CandidateFacetOption[];
}

export interface ProductFacets {
  subjects: CandidateFacetOption[];
  brands: CandidateFacetOption[];
  categories: CandidateFacetOption[];
  compatibility_brands: CandidateFacetOption[];
}

export interface PaginatedProducts {
  items: ProductRow[];
  page: number;
  page_size: number;
  total: number;
  facets?: ProductFacets;
}

export interface PaginatedCandidates {
  items: Candidate[];
  total: number;
  facets?: CandidateFacets;
}

export interface DecisionHistoryItem {
  id: number;
  product_id: number;
  competitor_item_id: number;
  competitor_name?: string;
  sku?: string;
  name?: string;
  action: "accept" | "reject" | "revoke";
  reason?: string;
  created_by?: string;
  created_at: string;
  previous_product_id?: number;
  previous_status?: string;
}

export interface DecisionHistoryResponse {
  items: DecisionHistoryItem[];
}

export interface BulkRejectItemResult {
  competitor_item_id: number;
  status: "rejected" | "skipped";
  reason?: string;
}

export interface BulkRejectResponse {
  ok: boolean;
  rejected_count: number;
  skipped_count: number;
  items: BulkRejectItemResult[];
}

export interface PropertySummary {
  total: number;
  matched: number;
  missing: number;
  conflict: number;
  unmapped: number;
  status: "match" | "missing" | "conflict" | "unmapped" | string;
  label: string;
  conflicts: string[];
  block_conflict?: number;
  review_conflict?: number;
  hint_conflict?: number;
}

export interface PropertyComparisonItem {
  property_key: string;
  label: string;
  product_value?: string | null;
  competitor_value?: string | null;
  mapped_value?: string | null;
  status: "match" | "missing" | "conflict" | "unmapped" | string;
  severity: "block" | "review" | "hint" | string;
  comparison_mode: string;
}

export interface PropertyComparisonResponse {
  profile_id: number;
  profile_code: string;
  profile_name: string;
  summary: PropertySummary;
  items: PropertyComparisonItem[];
}

export interface PropertyProfile {
  id: number;
  code: string;
  name: string;
  item_type?: string | null;
  sort_order: number;
  is_active: boolean;
}

export interface PropertyRule {
  id: number;
  profile_id: number;
  profile_code?: string | null;
  property_key: string;
  label: string;
  product_field: string;
  competitor_field: string;
  comparison_mode: string;
  severity: string;
  config_json?: Record<string, unknown> | null;
  sort_order: number;
  is_active: boolean;
  default_label?: string | null;
  default_product_field?: string | null;
  default_competitor_field?: string | null;
  default_comparison_mode?: string | null;
  default_severity?: string | null;
  default_config_json?: Record<string, unknown> | null;
  default_sort_order?: number | null;
  has_default_drift?: boolean;
}

export interface PropertyRulePayload {
  profile_id?: number;
  profile_code?: string;
  property_key: string;
  label: string;
  product_field: string;
  competitor_field: string;
  comparison_mode: string;
  severity: string;
  config_json?: Record<string, unknown> | null;
  sort_order?: number;
  is_active?: boolean;
}

export interface PropertyValueMap {
  id: number;
  rule_id: number;
  profile_code?: string | null;
  property_key?: string | null;
  competitor_source?: string | null;
  competitor_value: string;
  mapped_value: string;
  notes?: string | null;
  is_active: boolean;
}

export interface PropertyValueMapPayload {
  rule_id: number;
  competitor_source?: string | null;
  competitor_value: string;
  mapped_value: string;
  notes?: string | null;
  is_active?: boolean;
}

export interface PropertyValueSuggestion {
  rule_id: number;
  property_key: string;
  competitor_source?: string | null;
  competitor_value: string;
  count: number;
  sample_competitor_item_id: number;
  sample_name?: string | null;
  suggested_mapped_value?: string | null;
  safe_auto: boolean;
  safe_reason?: string | null;
}

export interface AcceptSafePropertyValueSuggestionsResponse {
  created_count: number;
  skipped_count: number;
  created: PropertyValueSuggestion[];
}

export interface CompatibilitySummary {
  brands: number;
  brand_aliases: number;
  phone_models: number;
  product_links: number;
  competitor_links: number;
  unresolved_product_values: number;
  unresolved_competitor_values: number;
  blocked_values: number;
}

export interface CompatibilityBrand {
  id: number;
  code: string;
  name: string;
  display_name: string;
  group_code?: string | null;
  is_active: boolean;
  models_count: number;
  unresolved_count: number;
}

export interface CompatibilityBrandPayload {
  code: string;
  name?: string | null;
  display_name?: string | null;
  group_code?: string | null;
}

export interface CompatibilityBrandAliasPayload {
  brand_id: number;
  raw_value: string;
  source?: string;
}

export interface CompatibilityBrandAlias {
  id: number;
  brand_id: number;
  brand_display_name?: string | null;
  source: string;
  raw_value: string;
  normalized_key: string;
  confidence?: number | null;
  is_manual: boolean;
  is_active: boolean;
  decision_reason?: string | null;
  first_seen_at: string;
  last_seen_at: string;
}

export interface CompatibilityBrandAliasPatch {
  is_active?: boolean;
}

export interface CompatibilityPhoneModel {
  id: number;
  brand_id?: number | null;
  brand_code?: string | null;
  brand_display_name?: string | null;
  brand: string;
  model_name: string;
  variant?: string | null;
  is_active: boolean;
  aliases_count: number;
  product_links_count: number;
  competitor_links_count: number;
  suggestion_kind?: "exact_base" | "exact_variant" | "hardware_variant" | "related_family" | null;
}

export interface CompatibilityPhoneModelPayload {
  brand_id: number;
  model_name: string;
  variant?: string | null;
}

export interface CompatibilityUnresolvedItem {
  entity_type: string;
  entity_id: number;
  source?: string | null;
  raw_value: string;
  raw_brand?: string | null;
  raw_model?: string | null;
  raw_variant?: string | null;
  normalized_key: string;
  brand_id?: number | null;
  brand_display_name?: string | null;
  sample_name?: string | null;
  current_phone_model_ids: number[];
}

export interface CompatibilityUnresolvedGroup {
  group_key: string;
  entity_type: string;
  source?: string | null;
  raw_value: string;
  raw_brand?: string | null;
  raw_model?: string | null;
  raw_variant?: string | null;
  normalized_key: string;
  brand_id?: number | null;
  brand_display_name?: string | null;
  affected_count: number;
  product_count: number;
  competitor_count: number;
  examples: CompatibilityUnresolvedItem[];
  suggested_phone_models: CompatibilityPhoneModel[];
  safe_auto_model_id?: number | null;
  is_noise_candidate: boolean;
}

export interface CompatibilityPreviewPayload {
  group_key?: string | null;
  entity_type?: string | null;
  source?: string | null;
  raw_value?: string | null;
  raw_brand?: string | null;
  raw_model?: string | null;
  raw_variant?: string | null;
  brand_id?: number | null;
  target_phone_model_ids: number[];
}

export interface CompatibilityPreviewResponse {
  preview_token: string;
  affected_count: number;
  affected_product_count: number;
  affected_competitor_count: number;
  target_phone_model_ids: number[];
  target_phone_models: CompatibilityPhoneModel[];
  warnings: string[];
  items: CompatibilityUnresolvedItem[];
}

export interface CompatibilityApplyPayload extends CompatibilityPreviewPayload {
  preview_token?: string | null;
  scope?: "previewed" | "group";
  notes?: string | null;
}

export interface CompatibilityBlockPayload {
  group_key?: string | null;
  entity_type?: string | null;
  source?: string | null;
  raw_value?: string | null;
  raw_brand?: string | null;
  raw_model?: string | null;
  raw_variant?: string | null;
  reason?: "noise" | "not_phone" | "bad_1c_value" | "not_supported" | "other" | null;
  notes?: string | null;
}

export interface CompatibilityApplyResponse {
  preview_token: string;
  affected_count: number;
  product_links_created: number;
  competitor_links_created: number;
  decisions_created: number;
}

export interface CompatibilityHistoryItem {
  action: string;
  source?: string | null;
  raw_value: string;
  normalized_key: string;
  brand_id?: number | null;
  brand_display_name?: string | null;
  phone_model_ids: number[];
  phone_model_labels: string[];
  actor?: string | null;
  notes?: string | null;
  reason?: string | null;
  affected_count: number;
  created_at: string;
}
