import { api } from "./client";
import {
  isBitrixReceivablesRoute,
  refreshBitrixReceivablesSession,
} from "./bitrix";

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

export interface ReceivableDepartmentOption {
  department_ref: string;
  department_name: string;
}

export interface ReceivableCacheComponent {
  source_status: string;
  cached_count: number;
  computed_at?: string | null;
  source_max_document_date?: string | null;
  source_lag_days?: number | null;
}

export interface ReceivableWorkplaceMetaResponse {
  latest_snapshot_date?: string | null;
  department_options: ReceivableDepartmentOption[];
  cache_status: Record<string, ReceivableCacheComponent>;
}

export interface ReceivableDocument {
  document_ref?: string | null;
  document_number?: string | null;
  document_date?: string | null;
  amount: string;
  gross_amount?: string | null;
  open_amount?: string | null;
  closing_amount?: string | null;
  return_amount?: string | null;
  manager_name?: string | null;
  due_date?: string | null;
  overdue_days?: number | null;
  is_overdue: boolean;
  selection_rule?: string | null;
  statement_balance_after?: string | null;
  match_details: Array<Record<string, unknown>>;
  document_structure_status?: string | null;
}

export interface ReceivableWorkplaceItem {
  snapshot_date: string;
  stable_key: string;
  counterparty_ref: string;
  counterparty_code?: string | null;
  counterparty_name?: string | null;
  bitrix_detail_url?: string | null;
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
  payment_postponed_count: number;
  comment?: string | null;
  needs_call_today: boolean;
  no_phone_marker: boolean;
  needs_credit_depth_default: boolean;
  criticality: string;
  documents: ReceivableDocument[];
  staff_options: ReceivableStaffOption[];
  supervisor_notes: ReceivableSupervisorNote[];
}

export type SupervisorNoteVisibility = "personal" | "shared";

export interface ReceivableSupervisorNote {
  id: number;
  visibility: SupervisorNoteVisibility;
  comment: string;
  author_user_id: string;
  author_name: string;
  created_at: string;
  updated_at: string;
  can_edit: boolean;
}

export interface ReceivableSupervisorNoteMutationResponse {
  note?: ReceivableSupervisorNote | null;
  event: {
    event_type: string;
    event_at: string;
    source: string;
    idempotent?: boolean;
  };
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
  total_count: number;
  visible_count: number;
  summary_scope: string;
  department_options: ReceivableDepartmentOption[];
  cache_status: Record<string, ReceivableCacheComponent>;
  status_options: ReceivableStatusOption[];
  payload: ReceivableWorkplaceItem[];
}

export type ReceivableWorkplaceSortBy = "balance" | "overdue_days";
export type ReceivableWorkplaceSortDir = "desc" | "asc";

export interface ReceivableWorkplaceActionPayload {
  action_id?: string | null;
  status?: string | null;
  contacted_staff_ref?: string | null;
  contacted_staff_name?: string | null;
  promised_payment_date?: string | null;
  last_contact_at?: string | null;
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

export interface ReceivableWorkplaceEditState {
  status: string;
  contacted_staff_ref: string;
  promised_payment_date: string;
  last_contact_at: string;
  next_action_date: string;
  payment_postponed: boolean;
  comment: string;
}

type ReceivablesRetryOptions = {
  refreshSession?: () => Promise<unknown>;
  isBitrixRoute?: () => boolean;
};

function responseStatus(error: unknown) {
  return typeof error === "object" && error !== null && "response" in error
    ? (error as { response?: { status?: number } }).response?.status
    : undefined;
}

function responseDetail(error: unknown) {
  if (typeof error !== "object" || error === null || !("response" in error)) return "";
  const detail = (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
  return typeof detail === "string" ? detail.trim() : "";
}

export function receivablesErrorMessage(error: unknown, fallback: string) {
  const status = responseStatus(error);
  if (status === 401) {
    return "Сессия истекла и не обновилась. Введённые данные остались на экране; повторите сохранение.";
  }
  const detail = responseDetail(error);
  if (detail) return detail;
  if (status === 403) {
    return "Нет доступа к рабочему месту: проверьте привязку пользователя к подразделению.";
  }
  return error instanceof Error && error.message ? error.message : fallback;
}

export async function withReceivablesAuthRetry<T>(
  request: () => Promise<T>,
  options: ReceivablesRetryOptions = {}
) {
  const isBitrixRoute = options.isBitrixRoute || isBitrixReceivablesRoute;
  const refreshSession = options.refreshSession || refreshBitrixReceivablesSession;
  try {
    return await request();
  } catch (error: unknown) {
    if (responseStatus(error) !== 401 || !isBitrixRoute()) throw error;
    try {
      await refreshSession();
    } catch (refreshError: unknown) {
      throw new Error(receivablesErrorMessage(refreshError, "Не удалось обновить сессию Bitrix24."));
    }
    return request();
  }
}

function dateValue(value?: string | null) {
  return value ? value.slice(0, 10) : "";
}

export function buildReceivableWorkplaceActionPayload(
  item: ReceivableWorkplaceItem,
  edit: ReceivableWorkplaceEditState,
  actionId: string
) {
  const payload: ReceivableWorkplaceActionPayload = { action_id: actionId };
  if (edit.status !== item.status) payload.status = edit.status;

  const currentStaffRef = item.contacted_staff_ref || "";
  if (edit.contacted_staff_ref !== currentStaffRef) {
    const staff = item.staff_options.find(
      (option) => option.staff_ref === edit.contacted_staff_ref
    );
    payload.contacted_staff_ref = edit.contacted_staff_ref || null;
    payload.contacted_staff_name = staff?.staff_name || null;
  }

  const dateFields: Array<
    ["promised_payment_date" | "last_contact_at" | "next_action_date", string]
  > = [
    ["promised_payment_date", edit.promised_payment_date],
    ["last_contact_at", edit.last_contact_at],
    ["next_action_date", edit.next_action_date],
  ];
  for (const [field, value] of dateFields) {
    if (value !== dateValue(item[field])) payload[field] = value || null;
  }

  if (edit.payment_postponed) payload.payment_postponed = true;
  if (edit.comment !== (item.comment || "")) payload.comment = edit.comment;
  return payload;
}

export interface CounterpartyFolderRecommendation {
  counterparty_ref: string;
  counterparty_code?: string | null;
  counterparty_name?: string | null;
  current_balance: string;
  current_folder_name?: string | null;
  current_folder_display_name?: string | null;
  recommended_folder_name?: string | null;
  recommended_folder_display_name?: string | null;
  debt_department_name?: string | null;
  debt_department_display_name?: string | null;
  snapshot_department_name?: string | null;
  snapshot_department_display_name?: string | null;
  debt_document_number?: string | null;
  debt_document_date?: string | null;
  origin_document_number?: string | null;
  origin_document_date?: string | null;
  effective_overdue_days?: number | null;
  status: string;
  review_reason?: string | null;
  exclusion_reason?: string | null;
  business_review_reason?: string | null;
  signal_key?: string | null;
  queue: CounterpartyFolderQueue;
  action_required: boolean;
}

export type CounterpartyFolderQueue =
  | "actionable"
  | "business_review"
  | "data_quality"
  | "excluded"
  | "all";

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
  min_debt?: number;
  sort_by?: ReceivableWorkplaceSortBy;
  sort_dir?: ReceivableWorkplaceSortDir;
}) {
  const response = await withReceivablesAuthRetry(() =>
    api.get<ReceivableWorkplaceResponse>("/receivables/workplace", {
      params: {
        date: params.date,
        department_ref: params.department_ref || undefined,
        limit: 100,
        min_debt: params.min_debt,
        sort_by: params.sort_by || "balance",
        sort_dir: params.sort_dir || "desc",
        status: params.status || undefined,
      },
    })
  );
  return response.data;
}

export async function fetchReceivableWorkplaceMeta(date?: string) {
  const response = await withReceivablesAuthRetry(() =>
    api.get<ReceivableWorkplaceMetaResponse>("/receivables/workplace/meta", {
      params: { date: date || undefined },
    })
  );
  return response.data;
}

export async function updateReceivableWorkplaceItem(
  date: string,
  counterpartyRef: string,
  payload: ReceivableWorkplaceActionPayload
) {
  const response = await withReceivablesAuthRetry(() =>
    api.patch<ReceivableWorkplaceActionResponse>(
      `/receivables/workplace/${encodeURIComponent(counterpartyRef)}`,
      payload,
      { params: { date } }
    )
  );
  return response.data;
}

export async function fetchCounterpartyFolderRecommendations(
  date: string,
  queue: CounterpartyFolderQueue = "actionable"
) {
  const response = await withReceivablesAuthRetry(() =>
    api.get<CounterpartyFolderRecommendationResponse>(
      "/receivables/workplace/folder-recommendations",
      { params: { date, queue, limit: 100 } }
    )
  );
  return response.data;
}

export async function upsertReceivableSupervisorNote(
  date: string,
  counterpartyRef: string,
  visibility: SupervisorNoteVisibility,
  comment: string,
  actionId: string
) {
  const response = await withReceivablesAuthRetry(() =>
    api.put<ReceivableSupervisorNoteMutationResponse>(
      `/receivables/workplace/${encodeURIComponent(counterpartyRef)}/supervisor-notes/${visibility}`,
      { comment, action_id: actionId },
      { params: { date } }
    )
  );
  return response.data;
}

export async function deleteReceivableSupervisorNote(
  date: string,
  counterpartyRef: string,
  visibility: SupervisorNoteVisibility,
  actionId: string
) {
  const response = await withReceivablesAuthRetry(() =>
    api.delete<ReceivableSupervisorNoteMutationResponse>(
      `/receivables/workplace/${encodeURIComponent(counterpartyRef)}/supervisor-notes/${visibility}`,
      { params: { date, action_id: actionId } }
    )
  );
  return response.data;
}
