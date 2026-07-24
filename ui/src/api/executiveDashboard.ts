import { api } from "./client";

export type ExecutiveAccessLevel = "full" | "domain";

export interface ExecutiveDashboardMetric {
  key: string;
  label: string;
  value?: string | number | null;
  unit?: string | null;
  tone: string;
  masked: boolean;
  source_status: string;
}

export interface ExecutiveDashboardBlock {
  key: string;
  title: string;
  source_status: string;
  freshness_status: string;
  as_of?: string | null;
  summary: Record<string, unknown>;
  metrics: ExecutiveDashboardMetric[];
  drilldown_url?: string | null;
}

export interface ExecutiveSourceStatus {
  source_key: string;
  title: string;
  source_status: string;
  freshness_status: string;
  as_of?: string | null;
  max_lag_days?: number | null;
  note?: string | null;
  source_amount?: string | null;
  adjustment_amount?: string | null;
  adjusted_amount?: string | null;
  recognition_method?: string | null;
  estimated_count: number;
}

export interface ExecutiveDashboardAction {
  stable_key: string;
  business_date: string;
  domain: string;
  severity: string;
  title: string;
  description?: string | null;
  amount?: string | null;
  currency: string;
  responsible_bitrix_user_id?: string | null;
  deadline_at?: string | null;
  status: string;
  source_system: string;
  source_ref?: string | null;
  dedupe_key: string;
  drilldown_url?: string | null;
  payload: Record<string, unknown>;
}

export interface ExecutiveDashboardResponse {
  as_of: string;
  generated_at: string;
  freshness_status: string;
  source_status: string;
  access_level: ExecutiveAccessLevel;
  roles: string[];
  allowed_blocks: string[];
  allowed_action_domains: string[];
  blocks: ExecutiveDashboardBlock[];
  source_freshness: ExecutiveSourceStatus[];
  top_actions: ExecutiveDashboardAction[];
  summary: Record<string, unknown>;
}

export interface ExecutiveDashboardActionsResponse {
  as_of: string;
  freshness_status: string;
  source_status: string;
  total_count: number;
  payload: ExecutiveDashboardAction[];
}

export type ExecutiveManagementBalanceView = "closed" | "operational";

export interface ExecutiveManagementBalanceLineItem {
  key: string;
  label: string;
  section: "asset" | "liability" | "equity";
  amount?: string | null;
  delta_previous?: string | null;
  source_key: string;
  source_status: string;
  source_as_of?: string | null;
  note?: string | null;
  source_amount?: string | null;
  adjustment_amount?: string | null;
  adjusted_amount?: string | null;
  recognition_method?: string | null;
  estimated_count: number;
}

export interface ExecutiveManagementBalanceResponse {
  month: string;
  balance_date: string;
  view: ExecutiveManagementBalanceView;
  version: number;
  status: string;
  source_status: string;
  freshness_status: string;
  generated_at: string;
  closed_at?: string | null;
  closed_by?: string | null;
  currency: string;
  assets: ExecutiveManagementBalanceLineItem[];
  liabilities: ExecutiveManagementBalanceLineItem[];
  equity: ExecutiveManagementBalanceLineItem[];
  assets_total: string;
  liabilities_total: string;
  equity_total: string;
  liabilities_and_equity_total: string;
  imbalance_amount: string;
  can_close: boolean;
  validation_errors: Array<Record<string, unknown>>;
  source_summary: Record<string, unknown>;
  available_months: string[];
  note?: string | null;
}

export interface ExecutiveManagementBalanceTurnoverLine {
  key: string;
  label: string;
  section: "asset" | "liability" | "equity";
  opening_balance?: string | null;
  debit_turnover?: string | null;
  credit_turnover?: string | null;
  closing_balance?: string | null;
  reconciliation_difference?: string | null;
  turnover_method: "net_change_from_snapshots";
  source_key: string;
  source_status: string;
  source_as_of?: string | null;
  note?: string | null;
}

export interface ExecutiveManagementBalanceTurnoverTotal {
  section: "asset" | "liability" | "equity";
  label: string;
  opening_balance: string;
  debit_turnover: string;
  credit_turnover: string;
  closing_balance: string;
  reconciliation_difference: string;
  unknown_line_count: number;
}

export interface ExecutiveManagementBalanceTurnoverResponse {
  month: string;
  date_from: string;
  date_to: string;
  view: ExecutiveManagementBalanceView;
  opening_version: number;
  closing_version: number;
  opening_status: string;
  closing_status: string;
  opening_validation_error_count: number;
  opening_content_sha256: string;
  closing_content_sha256: string;
  turnover_method: "net_change_from_snapshots";
  source_scope: "onec_ut_10_3_plus_bp_accrued_taxes";
  source_status: string;
  currency: string;
  lines: ExecutiveManagementBalanceTurnoverLine[];
  totals: ExecutiveManagementBalanceTurnoverTotal[];
  excluded_lines: Array<Record<string, unknown>>;
  opening_imbalance_amount: string;
  closing_imbalance_amount: string;
  unknown_line_count: number;
  note: string;
}

export interface ExecutiveCashflowRatio {
  key: string;
  label: string;
  value?: string | number | null;
  unit?: string | null;
  tone: string;
  note?: string | null;
}

export interface ExecutiveCashflowBreakdownRow {
  key: string;
  label: string;
  inflow_amount: string;
  outflow_amount: string;
  net_amount: string;
  movement_count: number;
  review_count: number;
  meta: Record<string, unknown>;
}

export interface ExecutiveCashflowDailyRow {
  business_date: string;
  inflow_amount: string;
  outflow_amount: string;
  net_amount: string;
  external_net_amount: string;
  internal_net_amount: string;
  movement_count: number;
  review_count: number;
}

export interface ExecutiveCashflowQualityIssue {
  issue_key: string;
  issue_type: string;
  issue_label: string;
  severity: string;
  business_date: string;
  amount_abs: string;
  description?: string | null;
  proposed_action?: string | null;
  status: string;
  document_number?: string | null;
  bitrix_task_id?: string | null;
  task_status?: string | null;
  drilldown_url?: string | null;
}

export interface ExecutiveCashflowPeriodResponse {
  date_from: string;
  date_to: string;
  generated_at?: string | null;
  source_status: string;
  freshness_status: string;
  note?: string | null;
  totals: Record<string, string | number | null>;
  ratios: ExecutiveCashflowRatio[];
  cash_position: Record<string, unknown>;
  daily: ExecutiveCashflowDailyRow[];
  by_group: ExecutiveCashflowBreakdownRow[];
  by_article: ExecutiveCashflowBreakdownRow[];
  by_cash_account: ExecutiveCashflowBreakdownRow[];
  by_currency: ExecutiveCashflowBreakdownRow[];
  quality_issues: ExecutiveCashflowQualityIssue[];
  filters: Record<string, unknown>;
}

export interface ExecutiveProfitLossLineItem {
  key: string;
  label: string;
  amount?: string | number | null;
  unit?: string | null;
  line_type: string;
  tone: string;
  source_status: string;
  note?: string | null;
}

export interface ExecutiveProfitLossRatio {
  key: string;
  label: string;
  value?: string | number | null;
  unit?: string | null;
  tone: string;
  note?: string | null;
}

export interface ExecutiveProfitLossBreakdownRow {
  key: string;
  label: string;
  revenue: string;
  cost_of_sales: string;
  gross_profit: string;
  sales_count: string;
  row_count: number;
  gross_margin_pct?: string | number | null;
  meta: Record<string, unknown>;
}

export interface ExecutiveProfitLossDailyRow extends ExecutiveProfitLossBreakdownRow {
  business_date: string;
}

export interface ExecutiveProfitLossExpenseBreakdownRow {
  key: string;
  label: string;
  amount: string;
  movement_count: number;
  review_count: number;
  source_status: string;
  recognition_method: string;
  cashflow_amount?: string | null;
  recognized_amount?: string | null;
  adjustment_amount?: string | null;
  estimated_count: number;
  meta: Record<string, unknown>;
}

export interface ExecutiveProfitLossOpenQuestion {
  key: string;
  label: string;
  amount: string;
  reason: string;
  proposed_action?: string | null;
  movement_count: number;
  review_count: number;
  source_status: string;
  recognition_method: string;
  meta: Record<string, unknown>;
}

export interface ExecutiveProfitLossInventoryLoss {
  schema_version: number;
  month: string;
  source_status: string;
  detail_source_status: string;
  writeoff_amount?: string | number | null;
  receipt_amount?: string | number | null;
  loss_amount?: string | number | null;
  loss_pct?: string | number | null;
  norm_pct?: string | number | null;
  variance_to_norm_pct?: string | number | null;
  matched_store_count?: number | null;
  previous_month?: ExecutiveProfitLossInventoryHistoryItem | null;
  average_loss_amount_3m?: string | number | null;
  average_loss_pct_3m?: string | number | null;
  history_source_status: string;
  history: ExecutiveProfitLossInventoryHistoryItem[];
  stores: ExecutiveProfitLossInventoryStore[];
  top_documents: ExecutiveProfitLossInventoryDocument[];
  actions: ExecutiveProfitLossInventoryAction[];
  data_quality: ExecutiveProfitLossInventoryDataQuality;
  owner?: ExecutiveProfitLossInventoryOwner | null;
  warnings: string[];
  note?: string | null;
}

export interface ExecutiveProfitLossInventoryHistoryItem {
  month: string;
  source_status: string;
  writeoff_amount?: string | number | null;
  receipt_amount?: string | number | null;
  loss_amount?: string | number | null;
  loss_pct?: string | number | null;
}

export interface ExecutiveProfitLossInventoryStore {
  store_ref: string;
  store_name: string;
  sales_amount?: string | number | null;
  writeoff_amount?: string | number | null;
  receipt_amount?: string | number | null;
  loss_amount?: string | number | null;
  loss_pct?: string | number | null;
  norm_pct?: string | number | null;
  variance_to_norm_pct?: string | number | null;
  above_norm: boolean;
  source_status: string;
  has_operations: boolean;
}

export interface ExecutiveProfitLossInventoryDocument {
  stable_key: string;
  operation_kind: string;
  operation_label: string;
  document_type: string;
  document_ref: string;
  document_number: string;
  document_date?: string | null;
  store_ref: string;
  store_name: string;
  amount: string | number;
  effect_amount: string | number;
}

export interface ExecutiveProfitLossInventoryAction {
  stable_key: string;
  action_type: string;
  severity: string;
  title: string;
  description: string;
  amount?: string | number | null;
  store_ref?: string | null;
  store_name?: string | null;
  responsible_name?: string | null;
  recommended_action: string;
}

export interface ExecutiveProfitLossInventoryDataQuality {
  source_status: string;
  approved_store_count: number;
  source_store_count: number;
  matched_store_count: number;
  unmatched_store_count: number;
  source_document_count: number;
  matched_document_count: number;
  unmatched_document_count: number;
  unmatched_writeoff_amount: string | number;
  unmatched_receipt_amount: string | number;
  excluded_store_count?: number;
  excluded_document_count?: number;
  excluded_writeoff_amount?: string | number;
  excluded_receipt_amount?: string | number;
  store_scope_status?: string;
  store_scope_source?: string | null;
  store_scope_month?: string | null;
  norm_source_status?: string;
  norm_source?: string | null;
}

export interface ExecutiveProfitLossInventoryOwner {
  employee_key?: string | null;
  employee_bitrix_id?: string | null;
  employee_name?: string | null;
  role_code?: string | null;
}

export interface ExecutiveProfitLossPeriodResponse {
  date_from: string;
  date_to: string;
  generated_at?: string | null;
  source_status: string;
  freshness_status: string;
  note?: string | null;
  totals: Record<string, string | number | null>;
  ratios: ExecutiveProfitLossRatio[];
  lines: ExecutiveProfitLossLineItem[];
  daily: ExecutiveProfitLossDailyRow[];
  monthly: ExecutiveProfitLossMonthlyRow[];
  by_store: ExecutiveProfitLossBreakdownRow[];
  by_manager: ExecutiveProfitLossBreakdownRow[];
  expense_source_status: string;
  expense_breakdown: ExecutiveProfitLossExpenseBreakdownRow[];
  expense_open_questions: ExecutiveProfitLossOpenQuestion[];
  inventory_loss?: ExecutiveProfitLossInventoryLoss | null;
  filters: Record<string, unknown>;
}

export interface ExecutiveProfitLossMonthlyRow {
  month: string;
  revenue: string | number;
  gross_profit?: string | number | null;
  operating_expenses?: string | number | null;
  operating_profit?: string | number | null;
  net_profit?: string | number | null;
  gross_margin_pct?: string | number | null;
  operating_margin_pct?: string | number | null;
  net_profit_margin_pct?: string | number | null;
  comparison_net_profit?: string | number | null;
  source_status: string;
  is_preliminary: boolean;
  note?: string | null;
}

export interface ExecutiveSalesDailyRow {
  business_date: string;
  actual_revenue?: string | number | null;
  forecast_revenue?: string | number | null;
}

export interface ExecutiveSalesMonthlyRow {
  month: string;
  revenue: string;
  gross_profit: string;
  sales_count: string;
  gross_margin_pct?: string | number | null;
  forecast_revenue?: string | number | null;
  comparison_sales_count?: string | number | null;
}

export interface ExecutiveSalesBreakdownRow {
  key: string;
  label: string;
  revenue: string;
  gross_profit: string;
  sales_count: string;
  gross_margin_pct?: string | number | null;
  meta: Record<string, unknown>;
}

export interface ExecutiveSalesFilterOption {
  key: string;
  label: string;
}

export interface ExecutiveSalesPlanContext {
  source_status: string;
  period_month: string;
  revision_no?: number | null;
  snapshot_id?: string | null;
  frozen_at?: string | null;
  scope_type: string;
  scope_key?: string | null;
  approved_revenue?: string | number | null;
  approved_margin_pct?: string | number | null;
  approved_gross_profit?: string | number | null;
  comparison_basis: string;
  comparison_revenue?: string | number | null;
  plan_attainment_pct?: string | number | null;
  note?: string | null;
}

export interface ExecutiveSalesDiagnosticKpi {
  key: string;
  value?: string | number | null;
  unit: string;
  source_status: string;
  note?: string | null;
  meta: Record<string, unknown>;
}

export interface ExecutiveSalesPeriodResponse {
  month: string;
  date_from: string;
  date_to: string;
  as_of?: string | null;
  generated_at?: string | null;
  source_status: string;
  freshness_status: string;
  forecast_status: string;
  plan_status?: string;
  note?: string | null;
  forecast_note?: string | null;
  plan_note?: string | null;
  plan?: ExecutiveSalesPlanContext | null;
  diagnostic_kpis?: ExecutiveSalesDiagnosticKpi[];
  totals: Record<string, string | number | null>;
  comparison: Record<string, string | number | null>;
  daily: ExecutiveSalesDailyRow[];
  monthly: ExecutiveSalesMonthlyRow[];
  by_store: ExecutiveSalesBreakdownRow[];
  by_manager: ExecutiveSalesBreakdownRow[];
  stores: ExecutiveSalesFilterOption[];
  managers: ExecutiveSalesFilterOption[];
  filters: Record<string, unknown>;
}

export interface ExecutiveOnlineStoreDailyRow {
  business_date: string;
  visits: number;
  visitors: number;
  purchases: number;
  click_buy: number;
  begin_checkout: number;
  phone_clicks: number;
  site_searches: number;
  purchase_conversion_pct: string | number;
}

export interface ExecutiveOnlineStoreTrafficSourceRow {
  key: string;
  label: string;
  visits: number;
  visitors: number;
  purchases: number;
  purchase_conversion_pct: string | number;
}

export interface ExecutiveOnlineStoreLandingPageRow {
  url: string;
  visits: number;
  visitors: number;
  purchases: number;
  click_buy: number;
  begin_checkout: number;
  purchase_conversion_pct: string | number;
}

export interface ExecutiveOnlineStorePeriodResponse {
  date_from: string;
  date_to: string;
  compare_date_from: string;
  compare_date_to: string;
  generated_at: string;
  source_status: string;
  freshness_status: string;
  counter_id: string;
  site: string;
  note?: string | null;
  totals: Record<string, string | number | null>;
  comparison: Record<string, string | number | null>;
  daily: ExecutiveOnlineStoreDailyRow[];
  traffic_sources: ExecutiveOnlineStoreTrafficSourceRow[];
  landing_pages: ExecutiveOnlineStoreLandingPageRow[];
}

export async function fetchExecutiveDashboard(date?: string) {
  const response = await api.get<ExecutiveDashboardResponse>("/management/executive-dashboard", {
    params: { date: date || undefined },
  });
  return response.data;
}

export async function fetchExecutiveManagementBalance(params?: {
  month?: string;
  view?: ExecutiveManagementBalanceView;
}) {
  const response = await api.get<ExecutiveManagementBalanceResponse>(
    "/management/executive-dashboard/management-balance",
    {
      params: {
        month: params?.month || undefined,
        view: params?.view || undefined,
      },
    }
  );
  return response.data;
}

export async function fetchExecutiveManagementBalanceTurnover(params?: {
  month?: string;
  view?: ExecutiveManagementBalanceView;
}) {
  const response = await api.get<ExecutiveManagementBalanceTurnoverResponse>(
    "/management/executive-dashboard/management-balance-turnover",
    {
      params: {
        month: params?.month || undefined,
        view: params?.view || undefined,
      },
    }
  );
  return response.data;
}

export async function closeExecutiveManagementBalance(month: string, note?: string) {
  const response = await api.post<ExecutiveManagementBalanceResponse>(
    `/management/executive-dashboard/management-balance/${month}/close`,
    { confirm: true, note: note || undefined }
  );
  return response.data;
}

export async function fetchExecutiveCashflowPeriod(params: {
  date_from?: string;
  date_to?: string;
  include_internal?: boolean;
}) {
  const response = await api.get<ExecutiveCashflowPeriodResponse>(
    "/management/executive-dashboard/cashflow-period",
    {
      params: {
        date_from: params.date_from || undefined,
        date_to: params.date_to || undefined,
        include_internal: params.include_internal,
      },
    }
  );
  return response.data;
}

export async function fetchExecutiveProfitLossPeriod(params: {
  date_from?: string;
  date_to?: string;
}) {
  const response = await api.get<ExecutiveProfitLossPeriodResponse>(
    "/management/executive-dashboard/profit-loss-period",
    {
      params: {
        date_from: params.date_from || undefined,
        date_to: params.date_to || undefined,
      },
    }
  );
  return response.data;
}

export async function fetchExecutiveSalesPeriod(params: {
  date_from?: string;
  date_to?: string;
  store_ref?: string;
  manager_ref?: string;
}) {
  const response = await api.get<ExecutiveSalesPeriodResponse>(
    "/management/executive-dashboard/sales-period",
    {
      params: {
        date_from: params.date_from || undefined,
        date_to: params.date_to || undefined,
        store_ref: params.store_ref || undefined,
        manager_ref: params.manager_ref || undefined,
      },
    }
  );
  return response.data;
}

export async function fetchExecutiveOnlineStorePeriod(params: {
  date_from?: string;
  date_to?: string;
}) {
  const response = await api.get<ExecutiveOnlineStorePeriodResponse>(
    "/management/executive-dashboard/online-store-period",
    {
      params: {
        date_from: params.date_from || undefined,
        date_to: params.date_to || undefined,
      },
    }
  );
  return response.data;
}

export async function fetchExecutiveDashboardActions(params: {
  date?: string;
  status?: string;
  domain?: string;
}) {
  const response = await api.get<ExecutiveDashboardActionsResponse>(
    "/management/executive-dashboard/actions",
    {
      params: {
        date: params.date || undefined,
        status: params.status || undefined,
        domain: params.domain || undefined,
      },
    }
  );
  return response.data;
}
