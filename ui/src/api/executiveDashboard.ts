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
  available_months: string[];
  note?: string | null;
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
  by_store: ExecutiveProfitLossBreakdownRow[];
  by_manager: ExecutiveProfitLossBreakdownRow[];
  expense_source_status: string;
  expense_breakdown: ExecutiveProfitLossExpenseBreakdownRow[];
  expense_open_questions: ExecutiveProfitLossOpenQuestion[];
  filters: Record<string, unknown>;
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
  forecast_revenue?: string | number | null;
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

export interface ExecutiveSalesPeriodResponse {
  month: string;
  date_from: string;
  date_to: string;
  as_of?: string | null;
  generated_at?: string | null;
  source_status: string;
  freshness_status: string;
  forecast_status: string;
  note?: string | null;
  forecast_note?: string | null;
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
  month?: string;
  store_ref?: string;
  manager_ref?: string;
}) {
  const response = await api.get<ExecutiveSalesPeriodResponse>(
    "/management/executive-dashboard/sales-period",
    {
      params: {
        month: params.month || undefined,
        store_ref: params.store_ref || undefined,
        manager_ref: params.manager_ref || undefined,
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
