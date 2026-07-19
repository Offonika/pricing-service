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

export interface CptSnapshot {
  id: number;
  run_id: number;
  counterparty_ref: string;
  snapshot_month: string;
  current_price_type: string | null;
  current_level: string | null;
  monthly_sales: Record<string, string> | null;
  total_3m: string | null;
  last_month: string | null;
  economics: Record<string, unknown> | null;
  payments: Record<string, unknown> | null;
  returns: Record<string, unknown>;
  history: Record<string, unknown>;
  source_status: string;
  stop_factors: string[];
  system_recommendation: string;
  recommended_price_type: string | null;
  recommendation_reason: string;
  reasons: string[];
  action_required: boolean;
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
  events: CptCaseEvent[];
}

export type CptWorklist =
  | "manager_work"
  | "isolate"
  | "recovery"
  | "data_check"
  | "special_review"
  | "downgrade_approval";

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
