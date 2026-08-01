import { api } from "./client";

export interface ProcurementAssortmentDecision {
  item_id: string;
  entity_type_id: number;
  title: string;
  sku_code: string;
  sku_name: string;
  status_decision: string;
  status_decision_label: string;
  status_reason: string;
  status_approved_by: string;
  status_changed_at: string;
  commercial_marks: string[];
  sync_blockers: string[];
  manual_override_preview?: Record<string, unknown> | null;
}

export interface ProcurementAssortmentDecisionUpdate {
  status_decision: string;
  status_reason: string;
  status_approved_by: string;
  status_changed_at: string;
  commercial_marks: string[];
}

export interface ProcurementAssortmentDecisionSyncResponse {
  decision: ProcurementAssortmentDecision;
  synced: boolean;
  merge_action: string;
  manual_overrides_path: string;
  blockers: string[];
}

export async function fetchProcurementAssortmentDecision(itemId: string) {
  const { data } = await api.get<ProcurementAssortmentDecision>(
    `/procurement-assortment/orders/${encodeURIComponent(itemId)}/decision`
  );
  return data;
}

export async function saveProcurementAssortmentDecision(
  itemId: string,
  payload: ProcurementAssortmentDecisionUpdate
) {
  const { data } = await api.post<{
    decision: ProcurementAssortmentDecision;
    updated: boolean;
  }>(`/procurement-assortment/orders/${encodeURIComponent(itemId)}/decision`, payload);
  return data;
}

export async function syncProcurementAssortmentDecision(itemId: string) {
  const { data } = await api.post<ProcurementAssortmentDecisionSyncResponse>(
    `/procurement-assortment/orders/${encodeURIComponent(itemId)}/decision/sync`,
    {}
  );
  return data;
}

export interface ProcurementClassificationProposal {
  id: number;
  status: string;
  previous_status?: string | null;
  proposed_status: string;
  proposed_status_label: string;
  reason: string;
  manual_minimum?: string | null;
  review_date?: string | null;
  blocks_order_line: boolean;
  requested_at: string;
  requested_by_bitrix_user_id: string;
  requested_by_name?: string | null;
  approved_at?: string | null;
  approved_by_bitrix_user_id?: string | null;
  approved_by_name?: string | null;
  onec_status: string;
  onec_message_id?: string | null;
}

export interface ProcurementB2BCustomerDemand {
  mode: "advisory_only" | string;
  profile_as_of_exclusive?: string;
  profile_age_days?: number | null;
  dependency_class?: string;
  active_customer_count?: number | null;
  passive_customer_count?: number | null;
  due_customer_count?: number | null;
  managed_sales_qty_window?: string;
  active_daily_rate?: string;
  client_forecast_qty?: string;
  ordinary_net_sales_qty_window?: string;
  replacement_target_stock_qty?: string;
  replacement_decision?: string;
  replacement_recommended_order_qty?: string;
  order_delta_qty?: string;
  reason_ru?: string;
}

export interface ProcurementOrderFormationLine {
  id: number;
  line_number: number;
  version: number;
  bitrix_product_id?: string | null;
  bitrix_product_xml_id: string;
  nomenclature_ref: string;
  nomenclature_code?: string | null;
  nomenclature_name: string;
  recommended_quantity: string;
  final_quantity: string;
  purchase_price: string;
  amount: string;
  currency: string;
  source_kind: string;
  explicit_demand: boolean;
  risk_level?: string | null;
  risk_codes: string[];
  recommendation_reason?: string | null;
  blockers: string[];
  assortment_status?: string | null;
  lifecycle_status?: string | null;
  quality?: string | null;
  procurement_profile?: string | null;
  manual_minimum?: string | null;
  payload?: {
    b2b_customer_demand?: ProcurementB2BCustomerDemand;
    [key: string]: unknown;
  };
  removed: boolean;
  effective_assortment_status?: string | null;
  effective_assortment_status_label?: string | null;
  latest_classification?: ProcurementClassificationProposal | null;
}

export interface ProcurementOrderFormation {
  id: number;
  stable_key: string;
  status: string;
  version: number;
  bitrix_item_id?: string | null;
  supplier_name: string;
  contract_name: string;
  currency: string;
  warehouse_name: string;
  procurement_contour: string;
  route: string;
  batch_id: string;
  order_date: string;
  responsible_name?: string | null;
  calculation_id: string;
  approved_version?: number | null;
  approved_at?: string | null;
  approved_by_name?: string | null;
  onec_status: string;
  onec_document_number?: string | null;
  onec_error?: string | null;
  blockers: string[];
  total_amount: string;
  lines: ProcurementOrderFormationLine[];
  manual_status_options: Record<string, string>;
}

export interface ProcurementDashboardCard {
  status: string;
  label: string;
  total_count: number;
  action_count: number;
  action_kind: "transition" | "review";
  action_label: string;
  target_status?: string | null;
  action_breakdown: Record<string, number>;
  ready_count: number;
  blocked_count: number;
  review_count: number;
  overdue_count: number;
  urgency: string;
}

export interface ProcurementDashboardAttentionItem {
  proposal_id?: number | null;
  nomenclature_code: string;
  product_name: string;
  current_status: string;
  current_status_label: string;
  kind: string;
  filter_status: string;
  action_label: string;
  fact_summary: string;
  decision_state: string;
  decision_state_label: string;
  reason: string;
  recommendation: string;
  deadline_label: string;
  urgency: string;
}

export interface ProcurementDashboard {
  folder: string;
  responsible_user_id: string;
  responsible_name: string;
  run_id?: number | null;
  run_key?: string | null;
  updated_at?: string | null;
  cards: ProcurementDashboardCard[];
  decision_summary: {
    ready_count: number;
    review_count: number;
    blocked_count: number;
  };
  manual_status_counts: Record<string, number>;
  attention: ProcurementDashboardAttentionItem[];
  manual_attention: ProcurementDashboardAttentionItem[];
}

export interface ProcurementLifecycleTransition {
  proposal_id?: number | null;
  nomenclature_code: string;
  nomenclature_ref?: string | null;
  product_guid?: string | null;
  product_name: string;
  folder: string;
  action_kind: string;
  current_status: string;
  current_status_label: string;
  target_status?: string | null;
  target_status_label?: string | null;
  proposal_status: string;
  reason: string;
  facts: Record<string, unknown>;
  blockers: string[];
  risk_codes: string[];
  run_id: number;
  run_key: string;
  facts_hash: string;
  responsible_bitrix_user_id?: string | null;
  responsible_name?: string | null;
  decision_state: string;
  ready: boolean;
  selectable: boolean;
  stale: boolean;
  created_at?: string | null;
}

export interface ProcurementLifecycleTransitionList {
  status: string;
  scope: string;
  total: number;
  page: number;
  page_size: number;
  ready_count: number;
  review_count: number;
  blocked_count: number;
  stale_count: number;
  items: ProcurementLifecycleTransition[];
}

export interface ProcurementLifecycleApprovalResponse {
  mode: string;
  message_id?: string | null;
  xml_preview: string;
  summary: {
    approved: number;
    stale: number;
    blocked: number;
    conflict: number;
    failed: number;
  };
  items: Array<{ proposal_id: number; result: string; message: string }>;
}

export interface ProcurementOrderListItem {
  id: number;
  stable_key: string;
  status: string;
  version: number;
  supplier_name: string;
  contract_name: string;
  warehouse_name: string;
  currency: string;
  route: string;
  batch_id: string;
  order_date: string;
  responsible_name?: string | null;
  source_run_id?: string | null;
  onec_status: string;
  onec_document_number?: string | null;
  onec_error?: string | null;
  line_count: number;
  total_quantity: string;
  total_amount: string;
  blockers: string[];
  updated_at: string;
}

export interface ProcurementOrderList {
  total: number;
  page: number;
  page_size: number;
  summary: { orders: number; lines: number; quantity: string; amount: string };
  items: ProcurementOrderListItem[];
}

export interface ProcurementClassificationQueue {
  total: number;
  page: number;
  page_size: number;
  pending: number;
  approved_today: number;
  readback_conflicts: number;
  items: Array<{
    proposal: ProcurementClassificationProposal;
    order_id: number;
    order_version: number;
    line_id: number;
    line_version: number;
    nomenclature_code?: string | null;
    nomenclature_ref: string;
    product_name: string;
    supplier_name: string;
    effective_status?: string | null;
  }>;
}

export interface ProcurementEventList {
  total: number;
  page: number;
  page_size: number;
  items: Array<{
    id: number;
    order_id?: number | null;
    entity_type: string;
    entity_id: string;
    event_type: string;
    actor: string;
    bitrix_user_id?: string | null;
    user_name?: string | null;
    before: Record<string, unknown>;
    after: Record<string, unknown>;
    payload: Record<string, unknown>;
    created_at: string;
  }>;
}

export async function fetchProcurementOrderFormation(itemId: string) {
  const { data } = await api.get<ProcurementOrderFormation>(
    `/procurement-order-formation/orders/by-bitrix/${encodeURIComponent(itemId)}`
  );
  return data;
}

export async function fetchProcurementOrder(orderId: number) {
  const { data } = await api.get<ProcurementOrderFormation>(
    `/procurement-order-formation/orders/${orderId}`
  );
  return data;
}

export async function fetchProcurementDashboard() {
  const { data } = await api.get<ProcurementDashboard>(
    "/procurement-order-formation/dashboard"
  );
  return data;
}

export async function fetchProcurementLifecycleTransitions(params: {
  status: string;
  scope?: "action" | "all";
  readiness?: "all" | "ready" | "review" | "blocked" | "stale";
  search?: string;
  proposal_id?: number;
  page?: number;
  page_size?: number;
}) {
  const { data } = await api.get<ProcurementLifecycleTransitionList>(
    "/procurement-order-formation/lifecycle/transitions",
    { params }
  );
  return data;
}

export async function approveProcurementLifecycleTransitions(
  items: ProcurementLifecycleTransition[]
) {
  const { data } = await api.post<ProcurementLifecycleApprovalResponse>(
    "/procurement-order-formation/lifecycle/transitions/approve",
    {
      idempotency_key: `ui-${crypto.randomUUID()}`,
      items: items.map((item) => ({
        proposal_id: item.proposal_id,
        expected_run_id: item.run_id,
        expected_current_status: item.current_status,
        facts_hash: item.facts_hash,
      })),
    }
  );
  return data;
}

export interface ProcurementOrderFilters {
  search?: string;
  status?: string;
  supplier?: string;
  blockers?: "all" | "with" | "without";
}

export async function fetchProcurementOrders(params: ProcurementOrderFilters & {
  page?: number;
  page_size?: number;
} = {}) {
  const { data } = await api.get<ProcurementOrderList>(
    "/procurement-order-formation/orders",
    { params }
  );
  return data;
}

export async function exportProcurementOrdersExcel(
  params: ProcurementOrderFilters = {}
) {
  const response = await api.get<Blob>(
    "/procurement-order-formation/orders/export.xlsx",
    { params, responseType: "blob" }
  );
  const disposition = String(response.headers["content-disposition"] || "");
  const filenameMatch = disposition.match(/filename="?([^";]+)"?/i);
  return {
    blob: response.data,
    filename: filenameMatch?.[1] || "procurement-orders.xlsx",
  };
}

export async function fetchProcurementClassifications(params: {
  status?: string;
  page?: number;
  page_size?: number;
} = {}) {
  const { data } = await api.get<ProcurementClassificationQueue>(
    "/procurement-order-formation/classification-proposals",
    { params }
  );
  return data;
}

export async function fetchProcurementEvents(params: {
  order_id?: number;
  entity_type?: string;
  event_type?: string;
  page?: number;
  page_size?: number;
} = {}) {
  const { data } = await api.get<ProcurementEventList>(
    "/procurement-order-formation/events",
    { params }
  );
  return data;
}

export async function updateProcurementOrderLine(
  orderId: number,
  lineId: number,
  payload: {
    expected_order_version: number;
    expected_line_version: number;
    final_quantity?: string;
    purchase_price?: string;
    removed?: boolean;
    explicit_demand?: boolean;
  }
) {
  const { data } = await api.patch<ProcurementOrderFormation>(
    `/procurement-order-formation/orders/${orderId}/lines/${lineId}`,
    payload
  );
  return data;
}

export async function createProcurementClassification(
  orderId: number,
  lineId: number,
  payload: {
    expected_order_version: number;
    expected_line_version: number;
    proposed_status: string;
    reason: string;
    manual_minimum?: string | null;
    review_date?: string | null;
  }
) {
  const { data } = await api.post<ProcurementOrderFormation>(
    `/procurement-order-formation/orders/${orderId}/lines/${lineId}/classification`,
    payload
  );
  return data;
}

export async function approveProcurementClassification(
  orderId: number,
  lineId: number,
  proposalId: number
) {
  const { data } = await api.post<{
    order: ProcurementOrderFormation;
    proposal: ProcurementClassificationProposal;
    mode: string;
    message_id: string;
  }>(
    `/procurement-order-formation/orders/${orderId}/lines/${lineId}/classification/${proposalId}/approve`,
    {}
  );
  return data;
}

export async function approveProcurementOrder(orderId: number) {
  const { data } = await api.post<ProcurementOrderFormation>(
    `/procurement-order-formation/orders/${orderId}/approve`,
    {}
  );
  return data;
}

export async function submitProcurementOrder(orderId: number) {
  const { data } = await api.post<{
    order: ProcurementOrderFormation;
    mode: string;
    message_id: string;
    xml_preview: string;
  }>(`/procurement-order-formation/orders/${orderId}/send-to-1c`);
  return data;
}
