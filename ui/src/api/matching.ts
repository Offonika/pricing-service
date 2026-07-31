import { api } from "./client";
import type {
  AcceptSafePropertyValueSuggestionsResponse,
  CompatibilityApplyPayload,
  CompatibilityApplyResponse,
  CompatibilityBlockPayload,
  CompatibilityBrand,
  CompatibilityBrandAlias,
  CompatibilityBrandAliasPatch,
  CompatibilityBrandAliasPayload,
  CompatibilityBrandPayload,
  CompatibilityHistoryItem,
  CompatibilityPhoneModel,
  CompatibilityPhoneModelPayload,
  CompatibilityPreviewPayload,
  CompatibilityPreviewResponse,
  CompatibilitySummary,
  CompatibilityUnresolvedGroup,
  CompatibilityUnresolvedItem,
  BulkRejectResponse,
  MatchingDecisionReasonCode,
  DecisionHistoryResponse,
  PaginatedCandidates,
  PaginatedProducts,
  ProductSort,
  PropertyComparisonResponse,
  PropertyProfile,
  PropertyRule,
  PropertyRulePayload,
  PropertyValueMap,
  PropertyValueMapPayload,
  PropertyValueSuggestion,
} from "./types";

export async function fetchProducts(params: {
  page?: number;
  page_size?: number;
  search?: string;
  status?: string | string[];
  sort?: ProductSort;
  subject?: string;
  brand?: string;
  category?: string;
  compatibility_brand?: string;
  include_live_counts?: boolean;
}) {
  const { data } = await api.get<PaginatedProducts>("/matching/products", { params });
  return data;
}

export async function fetchCandidates(
  productId: number,
  params?: {
    offset?: number;
    limit?: number;
    q?: string;
    source?: string;
    include_rejected?: boolean;
    in_stock?: boolean;
    category_group?: string;
    item_type?: string;
    brand?: string;
    model?: string;
    quality?: string;
    color?: string;
    candidate_status?: string;
    price_min?: number;
    price_max?: number;
    include_property_summary?: boolean;
  }
) {
  const { data } = await api.get<PaginatedCandidates>(`/matching/products/${productId}/candidate-search`, {
    params,
  });
  return data;
}

export async function fetchTopCandidate(productId: number) {
  const { data } = await api.get<PaginatedCandidates>(`/matching/products/${productId}/candidates`, {
    params: { limit: 1 },
  });
  return data.items?.[0];
}

export async function acceptMatch(productId: number, competitorId: number) {
  const { data } = await api.post(`/matching/products/${productId}`, { competitor_id: competitorId });
  return data;
}

export async function acceptItemMatch(
  productId: number,
  competitorItemId: number,
  reasonCode: MatchingDecisionReasonCode = "confirmed_attributes",
  reason?: string,
) {
  const { data } = await api.post(`/matching/products/${productId}/matches`, {
    competitor_item_id: competitorItemId,
    reason_code: reasonCode,
    reason,
  });
  return data;
}

export async function rejectItemMatch(
  productId: number,
  competitorItemId: number,
  reasonCode: MatchingDecisionReasonCode,
  reason?: string,
) {
  const { data } = await api.post(`/matching/products/${productId}/reject`, {
    competitor_item_id: competitorItemId,
    reason_code: reasonCode,
    reason,
  });
  return data;
}

export async function bulkRejectItemMatches(
  productId: number,
  competitorItemIds: number[],
  reasonCode: MatchingDecisionReasonCode,
  reason?: string,
) {
  const { data } = await api.post<BulkRejectResponse>(`/matching/products/${productId}/reject-bulk`, {
    competitor_item_ids: competitorItemIds,
    reason_code: reasonCode,
    reason,
  });
  return data;
}

export async function revokeItemMatch(
  productId: number,
  competitorItemId: number,
  reasonCode: MatchingDecisionReasonCode,
  reason?: string,
) {
  const { data } = await api.post(`/matching/products/${productId}/revoke`, {
    competitor_item_id: competitorItemId,
    reason_code: reasonCode,
    reason,
  });
  return data;
}

export async function revokeMatch(productId: number, competitorId: number) {
  await api.delete(`/matching/products/${productId}/${competitorId}`);
  return true;
}

export async function fetchGlobalCandidates(params?: { offset?: number; limit?: number; q?: string; in_stock?: boolean }) {
  const { data } = await api.get<PaginatedCandidates>("/matching/candidates", { params });
  return data;
}

export async function fetchMatchHistory(productId: number) {
  const { data } = await api.get<DecisionHistoryResponse>(`/matching/products/${productId}/history`);
  return data;
}

export async function fetchPropertyProfiles() {
  const { data } = await api.get<PropertyProfile[]>("/matching/property-profiles");
  return data;
}

export async function fetchPropertyRules(params?: { profile_id?: number; profile_code?: string }) {
  const { data } = await api.get<PropertyRule[]>("/matching/property-rules", { params });
  return data;
}

export async function createPropertyRule(payload: PropertyRulePayload) {
  const { data } = await api.post<PropertyRule>("/matching/property-rules", payload);
  return data;
}

export async function updatePropertyRule(ruleId: number, payload: Partial<PropertyRulePayload>) {
  const { data } = await api.patch<PropertyRule>(`/matching/property-rules/${ruleId}`, payload);
  return data;
}

export async function restorePropertyRuleDefault(ruleId: number) {
  const { data } = await api.post<PropertyRule>(`/matching/property-rules/${ruleId}/restore-default`);
  return data;
}

export async function fetchPropertyValueMaps(params?: { rule_id?: number; profile_code?: string }) {
  const { data } = await api.get<PropertyValueMap[]>("/matching/property-value-maps", { params });
  return data;
}

export async function createPropertyValueMap(payload: PropertyValueMapPayload) {
  const { data } = await api.post<PropertyValueMap>("/matching/property-value-maps", payload);
  return data;
}

export async function updatePropertyValueMap(valueMapId: number, payload: Partial<PropertyValueMapPayload>) {
  const { data } = await api.patch<PropertyValueMap>(`/matching/property-value-maps/${valueMapId}`, payload);
  return data;
}

export async function fetchPropertyValueSuggestions(params: {
  profile_code: string;
  rule_id?: number;
  source?: string;
  limit?: number;
}) {
  const { data } = await api.get<PropertyValueSuggestion[]>("/matching/property-value-suggestions", { params });
  return data;
}

export async function acceptSafePropertyValueSuggestions(payload: {
  profile_code: string;
  rule_id?: number;
  source?: string;
  limit?: number;
}) {
  const { data } = await api.post<AcceptSafePropertyValueSuggestionsResponse>(
    "/matching/property-value-suggestions/accept-safe",
    payload
  );
  return data;
}

export async function fetchPropertyComparison(
  productId: number,
  competitorItemId: number,
  profileCode?: string
) {
  const { data } = await api.get<PropertyComparisonResponse>(
    `/matching/products/${productId}/candidates/${competitorItemId}/properties`,
    { params: profileCode ? { profile_code: profileCode } : undefined }
  );
  return data;
}

export async function fetchCompatibilitySummary() {
  const { data } = await api.get<CompatibilitySummary>("/matching/compatibility/summary");
  return data;
}

export async function fetchCompatibilityBrands(params?: { q?: string; limit?: number }) {
  const { data } = await api.get<CompatibilityBrand[]>("/matching/compatibility/brands", { params });
  return data;
}

export async function createCompatibilityBrand(payload: CompatibilityBrandPayload) {
  const { data } = await api.post<CompatibilityBrand>("/matching/compatibility/brands", payload);
  return data;
}

export async function createCompatibilityBrandAlias(payload: CompatibilityBrandAliasPayload) {
  const { data } = await api.post<CompatibilityBrand>("/matching/compatibility/brand-aliases", payload);
  return data;
}

export async function fetchCompatibilityBrandAliases(params?: {
  brand_id?: number;
  q?: string;
  include_inactive?: boolean;
  limit?: number;
}) {
  const { data } = await api.get<CompatibilityBrandAlias[]>("/matching/compatibility/brand-aliases", { params });
  return data;
}

export async function patchCompatibilityBrandAlias(aliasId: number, payload: CompatibilityBrandAliasPatch) {
  const { data } = await api.patch<CompatibilityBrandAlias>(`/matching/compatibility/brand-aliases/${aliasId}`, payload);
  return data;
}

export async function fetchCompatibilityModels(params?: { brand_id?: number; q?: string; limit?: number }) {
  const { data } = await api.get<CompatibilityPhoneModel[]>("/matching/compatibility/models", { params });
  return data;
}

export async function createCompatibilityModel(payload: CompatibilityPhoneModelPayload) {
  const { data } = await api.post<CompatibilityPhoneModel>("/matching/compatibility/models", payload);
  return data;
}

export async function fetchCompatibilityUnresolved(params?: {
  entity_type?: string;
  brand_id?: number;
  source?: string;
  q?: string;
  limit?: number;
}) {
  const { data } = await api.get<CompatibilityUnresolvedItem[]>("/matching/compatibility/unresolved", { params });
  return data;
}

export async function fetchCompatibilityUnresolvedGroups(params?: {
  entity_type?: string;
  brand_id?: number;
  without_brand?: boolean;
  source?: string;
  q?: string;
  limit?: number;
}) {
  const { data } = await api.get<CompatibilityUnresolvedGroup[]>("/matching/compatibility/unresolved-groups", { params });
  return data;
}

export async function previewCompatibilityMapping(payload: CompatibilityPreviewPayload) {
  const { data } = await api.post<CompatibilityPreviewResponse>("/matching/compatibility/unresolved/preview", payload);
  return data;
}

export async function applyCompatibilityMapping(payload: CompatibilityApplyPayload) {
  const { data } = await api.post<CompatibilityApplyResponse>("/matching/compatibility/unresolved/apply", payload);
  return data;
}

export async function blockCompatibilityMapping(payload: CompatibilityBlockPayload) {
  const { data } = await api.post<CompatibilityApplyResponse>("/matching/compatibility/unresolved/block", payload);
  return data;
}

export async function fetchCompatibilityHistory(params?: { limit?: number }) {
  const { data } = await api.get<CompatibilityHistoryItem[]>("/matching/compatibility/history", { params });
  return data;
}
