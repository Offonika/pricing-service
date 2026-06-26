import { api } from "./client";

export interface ReceivableStatusOption {
  value: string;
  label: string;
  scope: string;
}

export interface ReceivableStaffOption {
  staff_ref: string;
  staff_name: string;
  department_ref?: string | null;
  department_name?: string | null;
}

export interface ReceivableDocument {
  document_ref?: string | null;
  document_number?: string | null;
  document_date?: string | null;
  amount: string;
  manager_name?: string | null;
  due_date?: string | null;
  overdue_days?: number | null;
  is_overdue: boolean;
}

export interface ReceivableWorkplaceItem {
  snapshot_date: string;
  stable_key: string;
  counterparty_ref: string;
  counterparty_code?: string | null;
  counterparty_name?: string | null;
  department_ref?: string | null;
  department_name?: string | null;
  responsible_ref?: string | null;
  responsible_name?: string | null;
  phone?: string | null;
  phone_status: string;
  current_balance: string;
  overdue_amount: string;
  effective_due_date?: string | null;
  effective_overdue_days?: number | null;
  oldest_overdue_date?: string | null;
  invoice_count: number;
  overdue_invoice_count: number;
  promised_payment_date?: string | null;
  last_contact_at?: string | null;
  contacted_staff_ref?: string | null;
  contacted_staff_name?: string | null;
  status: string;
  next_action_date?: string | null;
  payment_postponed: boolean;
  comment?: string | null;
  needs_call_today: boolean;
  no_phone_marker: boolean;
  needs_credit_depth_default: boolean;
  criticality: string;
  documents: ReceivableDocument[];
  staff_options: ReceivableStaffOption[];
}

export interface ReceivableWorkplaceSummary {
  row_count: number;
  total_receivable: string;
  total_overdue: string;
  overdue_over_30_amount: string;
  overdue_over_90_amount: string;
  need_call_today_amount: string;
  no_phone_count: number;
  credit_depth_default_count: number;
}

export interface ReceivableWorkplaceResponse {
  as_of: string;
  freshness_status: string;
  source_status: string;
  summary: ReceivableWorkplaceSummary;
  status_options: ReceivableStatusOption[];
  payload: ReceivableWorkplaceItem[];
}

export interface ReceivableWorkplaceActionPayload {
  status?: string | null;
  contacted_staff_ref?: string | null;
  contacted_staff_name?: string | null;
  promised_payment_date?: string | null;
  next_action_date?: string | null;
  payment_postponed?: boolean | null;
  comment?: string | null;
}

export interface ReceivableWorkplaceActionResponse {
  item: ReceivableWorkplaceItem;
  event: {
    event_type: string;
    event_at: string;
    source: string;
  };
}

export interface CounterpartyFolderRecommendation {
  counterparty_ref: string;
  counterparty_code?: string | null;
  counterparty_name?: string | null;
  current_balance: string;
  current_folder_name?: string | null;
  recommended_folder_name?: string | null;
  debt_department_name?: string | null;
  debt_document_number?: string | null;
  effective_overdue_days?: number | null;
  status: string;
  review_reason?: string | null;
}

export interface CounterpartyFolderRecommendationResponse {
  as_of: string;
  freshness_status: string;
  source_status: string;
  report_revision: string;
  summary: Record<string, unknown>;
  payload: CounterpartyFolderRecommendation[];
}

export async function fetchReceivableWorkplace(params: {
  date: string;
  department_ref?: string;
  status?: string;
}) {
  const response = await api.get<ReceivableWorkplaceResponse>("/receivables/workplace", {
    params: {
      date: params.date,
      department_ref: params.department_ref || undefined,
      status: params.status || undefined,
    },
  });
  return response.data;
}

export async function updateReceivableWorkplaceItem(
  date: string,
  counterpartyRef: string,
  payload: ReceivableWorkplaceActionPayload
) {
  const response = await api.patch<ReceivableWorkplaceActionResponse>(
    `/receivables/workplace/${encodeURIComponent(counterpartyRef)}`,
    payload,
    { params: { date } }
  );
  return response.data;
}

export async function fetchCounterpartyFolderRecommendations(date: string) {
  const response = await api.get<CounterpartyFolderRecommendationResponse>(
    "/receivables/workplace/folder-recommendations",
    { params: { date, limit: 100 } }
  );
  return response.data;
}
