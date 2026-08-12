import { api } from "./client";

export interface CptEnvelope {
  run_id: number | null;
  snapshot_month: string | null;
  ruleset_version: string | null;
  source_status: string;
}

export interface CptSummary {
  profile_count: number;
  actionable_count: number;
  levels: Record<string, number>;
  recommendations: Record<string, number>;
  source_statuses: Record<string, number>;
  review_types: Record<string, number>;
  departments: Record<string, number>;
}

export interface CptSummaryResponse extends CptEnvelope {
  summary: CptSummary;
}

export interface CptWorklistsResponse extends CptEnvelope {
  worklists: Record<string, number>;
}

export interface CptCaseItem {
  id: number;
  case_key: string;
  counterparty_ref: string;
  counterparty_code: string | null;
  counterparty_name: string | null;
  snapshot_month: string;
  stage: string;
  case_type: string;
  review_type: string | null;
  reasons: string[];
  owner_name: string | null;
  department_name: string | null;
  due_at: string | null;
  system_recommendation: string;
  recommended_price_type: string | null;
  human_final_decision: string | null;
  approval_status: string;
  action_required: boolean;
  snapshot_hash: string;
  version: number;
}

export interface CptCaseListResponse extends CptEnvelope {
  total: number;
  limit: number;
  offset: number;
  payload: CptCaseItem[];
}

export type CptProfileSearchState = "no_change" | "change_proposed" | "data_issue";

export interface CptProfileSearchItem {
  counterparty_ref: string;
  counterparty_code: string | null;
  counterparty_name: string | null;
  current_price_type: string | null;
  recommended_price_type: string | null;
  result_state: CptProfileSearchState;
  result_label: string;
  can_review: boolean;
  quality_sample_id: number | null;
  quality_sample_status: "pending" | "reviewed" | null;
}

export interface CptProfileSearchResponse extends CptEnvelope {
  total: number;
  limit: number;
  offset: number;
  payload: CptProfileSearchItem[];
}

export interface CptDataIssueItem {
  counterparty_ref: string;
  counterparty_code: string | null;
  counterparty_name: string | null;
  current_price_type: string | null;
  issue_source: "calculation" | "expert";
  issue_text: string;
  reported_by: string | null;
  reported_at: string | null;
  comment: string | null;
  case_id: number | null;
}

export interface CptDataIssueListResponse extends CptEnvelope {
  total: number;
  limit: number;
  offset: number;
  payload: CptDataIssueItem[];
}

export type CptReviewKind = "price_type" | "client_action";
export type CptReviewResult = "confirm" | "correct" | "no_action" | "data_issue";
export type CptExternalState =
  | "not_created"
  | "held"
  | "pending"
  | "preflight"
  | "ready_to_apply"
  | "applying"
  | "applied"
  | "cancelled"
  | "technical_review";

export interface CptReviewDimension {
  kind: CptReviewKind;
  system_value: string | null;
  system_label: string;
  can_review: boolean;
  unavailable_reason: string | null;
  allowed_results: CptReviewResult[];
  allowed_corrected_values: string[];
  review_id: number | null;
  result: CptReviewResult | null;
  final_value: string | null;
  comment: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  version: number;
  decision_mode: "test" | "live" | null;
  external_state: CptExternalState;
  external_message: string | null;
}

export interface CptReviewCard {
  snapshot_id: number;
  case_id: number | null;
  counterparty_ref: string;
  counterparty_code: string | null;
  counterparty_name: string | null;
  owner_name: string | null;
  department_name: string | null;
  snapshot_month: string;
  current_price_type: string | null;
  recommended_price_type: string | null;
  recommendation_text: string;
  data_state: "ready" | "technical_review" | "insufficient_history" | "new_client";
  data_state_label: string;
  snapshot_hash: string;
  contracts: CptContractCandidate[];
  price_type: CptReviewDimension;
  client_action: CptReviewDimension;
}

export interface CptReviewCardListResponse extends CptEnvelope {
  total: number;
  limit: number;
  offset: number;
  payload: CptReviewCard[];
}

export interface CptReviewSaveResponse extends CptEnvelope {
  card: CptReviewCard;
  saved_kind: CptReviewKind;
}

export interface CptReviewKindMetrics {
  reviewed_count: number;
  confirmed_count: number;
  corrected_count: number;
  no_action_count: number;
  data_issue_count: number;
  correction_rate: number;
}

export interface CptReviewMetricsResponse extends CptEnvelope {
  price_type: CptReviewKindMetrics;
  client_action: CptReviewKindMetrics;
}

export interface CptContractCandidate {
  contract_ref?: string | null;
  contract_name?: string | null;
  price_type_name?: string | null;
  price_type_marked?: boolean;
  price_type_missing?: boolean;
  sale_document_count_12m?: number;
  sales_amount_12m?: string | null;
  last_sale_at?: string | null;
  is_working?: boolean;
  used_for_calculation?: boolean;
  price_type_change_target?: boolean;
  ignored_reason?: string | null;
}

export type CptPortfolioBucket = "working_bronze" | "review_queue" | "all";

export interface CptPortfolioItem {
  counterparty_ref: string;
  counterparty_code: string;
  counterparty_name: string | null;
  department_name: string | null;
  owner_name: string | null;
  bucket: Exclude<CptPortfolioBucket, "all">;
  expected_bucket: Exclude<CptPortfolioBucket, "all">;
  expected_price_type: string | null;
  current_price_type: string | null;
  price_type_variant: string | null;
  working_contracts: CptContractCandidate[];
  action_required: boolean;
  system_recommendation: string | null;
  recommended_price_type: string | null;
  source_status: string;
  stop_factors: string[];
  review_status: "ready" | "business_conflict" | "technical_incomplete" | "missing_snapshot";
  case_id: number | null;
  case_type: string | null;
  case_stage: string | null;
  reconciliation_status: "match" | "mismatch" | "missing_snapshot";
}

export interface CptPortfolioResponse extends CptEnvelope {
  batch_key: string;
  batch_label: string;
  expected_counts: Record<string, number>;
  counts: Record<string, number>;
  review_status_counts: Record<string, number>;
  mismatch_count: number;
  total: number;
  limit: number;
  offset: number;
  payload: CptPortfolioItem[];
}

export interface CptSnapshot {
  id: number;
  run_id: number;
  counterparty_ref: string;
  snapshot_month: string;
  ruleset_version: string;
  current_price_type: string | null;
  current_level: string | null;
  price_type_variant: string | null;
  contract_candidates: CptContractCandidate[];
  monthly_sales: Record<string, string> | null;
  total_3m: string | null;
  last_month: string | null;
  economics: Record<string, unknown> | null;
  payments: Record<string, unknown> | null;
  returns: Record<string, unknown>;
  history: Record<string, unknown>;
  source_status: string;
  source_statuses: Record<string, string>;
  conflicts: string[];
  stop_factors: string[];
  system_recommendation: string;
  recommended_price_type: string | null;
  recommendation_reason: string;
  reasons: string[];
  action_required: boolean;
  case_type: string | null;
  review_type: string | null;
  snapshot_hash: string;
  money_visible: boolean;
}

export interface CptCaseEvent {
  id: number;
  event_type: string;
  event_at: string;
  actor: string;
  before_status: string | null;
  after_status: string | null;
  comment: string | null;
}

export interface CptCaseDetailResponse extends CptEnvelope {
  case: CptCaseItem;
  snapshot: CptSnapshot;
  guidance: {
    title: string;
    rules: string;
    recommended_action: string;
    expected_price_type: string;
    manager_attention: string[];
  } | null;
  events: CptCaseEvent[];
}

export type CptWorklist =
  | "manager_work"
  | "isolate"
  | "recovery"
  | "data_check"
  | "special_review"
  | "downgrade_approval";

export type CptQualityGroup = CptWorklist | "no_action";

export interface CptQualitySample {
  id: number;
  run_id: number;
  snapshot_id: number;
  counterparty_ref: string;
  counterparty_code: string | null;
  counterparty_name: string | null;
  current_price_type: string | null;
  recommended_price_type: string | null;
  system_recommendation: string;
  recommendation_reason: string;
  stop_factors: string[];
  system_group: CptQualityGroup;
  correct_group: CptQualityGroup | null;
  review_result: "correct" | "incorrect" | "data_issue" | null;
  status: "pending" | "reviewed";
  selected_by: string;
  selected_at: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  comment: string | null;
  version: number;
}

export interface CptQualitySampleListResponse extends CptEnvelope {
  total: number;
  limit: number;
  offset: number;
  payload: CptQualitySample[];
}

export interface CptQualityProfile {
  id: number;
  counterparty_ref: string;
  counterparty_code: string | null;
  counterparty_name: string | null;
  department_ref: string | null;
  department_name: string | null;
  owner_ref: string | null;
  owner_name: string | null;
  master_data_flags: string[];
}

export interface CptQualitySampleDetailResponse extends CptEnvelope {
  sample: CptQualitySample;
  profile: CptQualityProfile;
  snapshot: CptSnapshot;
}

export interface CptQualityGroupMetrics {
  population_count: number;
  selected_count: number;
  reviewed_count: number;
  true_positive: number;
  false_positive: number;
  false_negative: number;
  precision: number | null;
  recall: number | null;
}

export interface CptQualityMetricsResponse extends CptEnvelope {
  metrics_scope: "portfolio" | "special_review_only";
  metrics_ready: boolean;
  population_count: number;
  selected_count: number;
  reviewed_count: number;
  coverage: number;
  override_rate: number;
  critical_false_downgrade_count: number;
  groups: Record<string, CptQualityGroupMetrics>;
  matrix: Record<string, Record<string, number>>;
}

function monthParams(month?: string | null) {
  return month ? { snapshot_month: month } : {};
}

export async function fetchCptSummary(month?: string | null): Promise<CptSummaryResponse> {
  const { data } = await api.get<CptSummaryResponse>("/customer-price-types/summary", {
    params: monthParams(month),
  });
  return data;
}

export async function fetchCptWorklists(month?: string | null): Promise<CptWorklistsResponse> {
  const { data } = await api.get<CptWorklistsResponse>("/customer-price-types/worklists", {
    params: monthParams(month),
  });
  return data;
}

export async function fetchCptPortfolio(options: {
  bucket?: CptPortfolioBucket;
  search?: string | null;
  currentPriceType?: string | null;
  actionRequired?: boolean | null;
  limit?: number;
  offset?: number;
} = {}): Promise<CptPortfolioResponse> {
  const { data } = await api.get<CptPortfolioResponse>("/customer-price-types/portfolio", {
    params: {
      batch_key: "reviewed-working-contracts-2026-07",
      bucket: options.bucket ?? "all",
      ...(options.search ? { search: options.search } : {}),
      ...(options.currentPriceType ? { current_price_type: options.currentPriceType } : {}),
      ...(options.actionRequired != null ? { action_required: options.actionRequired } : {}),
      limit: options.limit ?? 100,
      offset: options.offset ?? 0,
    },
  });
  return data;
}

export async function fetchCptCases(options: {
  month?: string | null;
  worklist?: CptWorklist | null;
  search?: string | null;
  limit?: number;
  offset?: number;
}): Promise<CptCaseListResponse> {
  const { data } = await api.get<CptCaseListResponse>("/customer-price-types/cases", {
    params: {
      ...monthParams(options.month),
      ...(options.worklist ? { worklist: options.worklist } : {}),
      ...(options.search ? { search: options.search } : {}),
      limit: options.limit ?? 50,
      offset: options.offset ?? 0,
    },
  });
  return data;
}

export async function fetchCptCaseDetail(caseId: number): Promise<CptCaseDetailResponse> {
  const { data } = await api.get<CptCaseDetailResponse>(`/customer-price-types/cases/${caseId}`);
  return data;
}

export async function searchCptProfiles(search: string): Promise<CptProfileSearchResponse> {
  const { data } = await api.get<CptProfileSearchResponse>("/customer-price-types/profiles", {
    params: { search, limit: 50 },
  });
  return data;
}

export async function fetchCptDataIssues(search?: string | null): Promise<CptDataIssueListResponse> {
  const { data } = await api.get<CptDataIssueListResponse>("/customer-price-types/data-issues", {
    params: { ...(search ? { search } : {}), limit: 500 },
  });
  return data;
}

export async function fetchCptReviewCards(options: {
  reviewKind?: CptReviewKind | null;
  pendingOnly?: boolean;
  search?: string | null;
  limit?: number;
  offset?: number;
} = {}): Promise<CptReviewCardListResponse> {
  const { data } = await api.get<CptReviewCardListResponse>(
    "/customer-price-types/reviews/cards",
    {
      params: {
        ...(options.reviewKind ? { review_kind: options.reviewKind } : {}),
        ...(options.pendingOnly != null ? { pending_only: options.pendingOnly } : {}),
        ...(options.search ? { search: options.search } : {}),
        limit: options.limit ?? 100,
        offset: options.offset ?? 0,
      },
    },
  );
  return data;
}

export async function fetchCptReviewMetrics(): Promise<CptReviewMetricsResponse> {
  const { data } = await api.get<CptReviewMetricsResponse>(
    "/customer-price-types/reviews/metrics",
  );
  return data;
}

export async function saveCptReview(options: {
  snapshotId: number;
  reviewKind: CptReviewKind;
  result: CptReviewResult;
  correctedValue?: string | null;
  comment?: string | null;
  expectedVersion: number;
  snapshotHash: string;
}): Promise<CptReviewSaveResponse> {
  const suffix = options.reviewKind === "price_type" ? "price-type" : "client-action";
  const { data } = await api.put<CptReviewSaveResponse>(
    `/customer-price-types/reviews/cards/${options.snapshotId}/${suffix}`,
    {
      result: options.result,
      corrected_value: options.correctedValue ?? null,
      comment: options.comment?.trim() || null,
      expected_version: options.expectedVersion,
      snapshot_hash: options.snapshotHash,
    },
  );
  return data;
}

export async function prepareCptQualitySamples(perGroup: number) {
  const { data } = await api.post("/customer-price-types/quality/samples/prepare", {
    per_group: perGroup,
  });
  return data;
}

export async function fetchCptQualitySamples(options: {
  status?: "pending" | "reviewed" | null;
} = {}): Promise<CptQualitySampleListResponse> {
  const { data } = await api.get<CptQualitySampleListResponse>(
    "/customer-price-types/quality/samples",
    {
      params: {
        ...(options.status ? { status: options.status } : {}),
        limit: 500,
      },
    },
  );
  return data;
}

export async function fetchCptQualityMetrics(): Promise<CptQualityMetricsResponse> {
  const { data } = await api.get<CptQualityMetricsResponse>(
    "/customer-price-types/quality/metrics",
  );
  return data;
}

export async function fetchCptQualitySampleDetail(
  sampleId: number,
): Promise<CptQualitySampleDetailResponse> {
  const { data } = await api.get<CptQualitySampleDetailResponse>(
    `/customer-price-types/quality/samples/${sampleId}`,
  );
  return data;
}

export async function reviewCptQualitySample(options: {
  sampleId: number;
  reviewResult: "correct" | "incorrect" | "data_issue";
  correctGroup?: CptQualityGroup | null;
  comment?: string | null;
  expectedVersion: number;
}): Promise<CptQualitySample> {
  const { data } = await api.put<CptQualitySample>(
    `/customer-price-types/quality/samples/${options.sampleId}`,
    {
      review_result: options.reviewResult,
      correct_group: options.correctGroup ?? null,
      comment: options.comment || null,
      expected_version: options.expectedVersion,
    },
  );
  return data;
}
