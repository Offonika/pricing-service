import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  fetchExecutiveCashflowPeriod,
  closeExecutiveManagementBalance,
  fetchExecutiveDashboard,
  fetchExecutiveDashboardActions,
  fetchExecutiveManagementBalance,
  fetchExecutiveManagementBalanceTurnover,
  fetchExecutiveOnlineStorePeriod,
  fetchExecutiveProfitLossPeriod,
  fetchExecutiveSalesPeriod,
  type ExecutiveAccessLevel,
  type ExecutiveCashflowPeriodResponse,
  type ExecutiveCashflowRatio,
  type ExecutiveDashboardAction,
  type ExecutiveDashboardBlock,
  type ExecutiveDashboardMetric,
  type ExecutiveDashboardResponse,
  type ExecutiveManagementBalanceLineItem,
  type ExecutiveManagementBalanceResponse,
  type ExecutiveManagementBalanceTurnoverResponse,
  type ExecutiveManagementBalanceView,
  type ExecutiveOnlineStorePeriodResponse,
  type ExecutiveProfitLossBreakdownRow,
  type ExecutiveProfitLossExpenseBreakdownRow,
  type ExecutiveProfitLossInventoryLoss,
  type ExecutiveProfitLossLineItem,
  type ExecutiveProfitLossMonthlyRow,
  type ExecutiveProfitLossOpenQuestion,
  type ExecutiveProfitLossPeriodResponse,
  type ExecutiveProfitLossRatio,
  type ExecutiveSalesBreakdownRow,
  type ExecutiveSalesDailyRow,
  type ExecutiveSalesDiagnosticKpi,
  type ExecutiveSalesMonthlyRow,
  type ExecutiveSalesPeriodResponse,
  type ExecutiveSourceStatus,
} from "../api/executiveDashboard";
import { splitManagementBalanceBlock } from "./executiveDashboardLayout";
import { Button, ErrorState, LoadingState, MetricCard, PageShell, StatusBadge, type MetricDelta, type MetricTone } from "./ui";

type ExecutiveDashboardProps = {
  bitrixMode?: boolean;
  bitrixUserName?: string | null;
  accessLevel?: ExecutiveAccessLevel;
};

type CashPositionBreakdownRow = {
  cash_category?: string;
  cash_category_label?: string;
  cash_currency_code?: string;
  cash_currency_name?: string;
  balance_native?: string | number | null;
  balance_rub?: string | number | null;
  account_count?: number | null;
};

type ReconciliationIssueExample = {
  issue_key?: string;
  issue_type?: string;
  issue_type_label?: string;
  department?: string;
  merchant_id?: string;
  amount_delta?: string | number | null;
  sber_amount?: string | number | null;
  onec_amount?: string | number | null;
  operation_date?: string;
  status?: string;
  proposed_action?: string;
};

type ReconciliationIssueBreakdown = {
  issue_type?: string;
  label?: string;
  count?: number;
};

type ReconciliationReportDelivery = {
  status?: string;
  task_count?: number;
  task_ids?: string[];
  uploaded_file_ids?: string[];
  modes?: string[];
};

type ManagementBalanceLine = {
  key?: string;
  label?: string;
  amount?: string | number | null;
  source_status?: string;
  as_of?: string | null;
  masked?: boolean;
  recognition_method?: string;
  estimated_count?: number;
  note?: string;
};

const MONEY_TAB_KEY = "money_today";
const PROFIT_LOSS_TAB_KEY = "profit_loss";
const SALES_TAB_KEY = "sales";
const ONLINE_STORE_TAB_KEY = "online_store";
const ODDS_CASHFLOW_TAB_KEY = "odds_cashflow";
const TAB_DEFINITIONS = [
  { key: "today", label: "Сегодня" },
  { key: MONEY_TAB_KEY, label: "Деньги / ДДС" },
  { key: PROFIT_LOSS_TAB_KEY, label: "Прибыли / убытки" },
  { key: SALES_TAB_KEY, label: "Продажи" },
  { key: ONLINE_STORE_TAB_KEY, label: "Интернет-магазин" },
  { key: ODDS_CASHFLOW_TAB_KEY, label: "ОДДС CashFlow" },
  { key: "debtors", label: "Дебиторка покупателей" },
  { key: "receivables_control", label: "Контроль" },
  { key: "creditors_payables", label: "Управленческий баланс" },
  { key: "procurement_import", label: "Закупки" },
  { key: "warehouse_operations", label: "Склад" },
  { key: "reconciliation", label: "Сверки" },
  { key: "tasks", label: "Задачи" },
];
const TAB_LABELS = Object.fromEntries(TAB_DEFINITIONS.map((item) => [item.key, item.label]));
const TAB_KEYS = new Set(TAB_DEFINITIONS.map((item) => item.key));

const DOMAIN_LABELS: Record<string, string> = {
  money_today: "Деньги / ДДС",
  profit_loss: "Прибыли / убытки",
  sales: "Продажи",
  online_store: "Интернет-магазин",
  odds_cashflow: "ОДДС CashFlow",
  debtors: "Дебиторка покупателей",
  receivables_control: "Контроль дебиторки",
  creditors_payables: "Управленческий баланс",
  procurement_import: "Закупки",
  warehouse_operations: "Склад",
  reconciliation: "Сверки",
  tasks: "Задачи",
  daily_focus: "Фокус",
};

const SOURCE_FRESHNESS_KEY_BY_TAB: Record<string, string> = {
  money_today: "money_today",
  [PROFIT_LOSS_TAB_KEY]: "profit_loss",
  [SALES_TAB_KEY]: "sales",
  [ONLINE_STORE_TAB_KEY]: "online_store",
  [ODDS_CASHFLOW_TAB_KEY]: "money_today",
  debtors: "debtors",
  receivables_control: "receivables_control",
  creditors_payables: "creditors_payables",
  procurement_import: "procurement_import",
  warehouse_operations: "warehouse_operations",
  reconciliation: "reconciliation",
  tasks: "tasks",
};

const FLOW_STEPS = [
  { key: "money_today", label: "Деньги / ДДС", metricKeys: ["cash_position_total_balance", "cashflow_inflow_amount"] },
  { key: "profit_loss", label: "Прибыли / убытки", metricKeys: ["gross_profit", "gross_margin_pct"] },
  { key: "sales", label: "Продажи", metricKeys: ["revenue", "forecast_revenue_period_end"] },
  { key: "debtors", label: "Покупатели", metricKeys: ["total_receivable"] },
  { key: "receivables_control", label: "Контроль", metricKeys: ["folder_needs_review_count", "need_call_today_count"] },
  { key: "creditors_payables", label: "Баланс", metricKeys: ["balance_liabilities_total"] },
  { key: "procurement_import", label: "Закупки", metricKeys: ["open_supplier_orders"] },
  { key: "warehouse_operations", label: "Склад", metricKeys: ["pieces_picked", "avg_need_fact"] },
  { key: "reconciliation", label: "Сверки", metricKeys: ["unmatched_count"] },
  { key: "tasks", label: "Задачи", metricKeys: ["open_actions"] },
  { key: "daily_focus", label: "Фокус дня", metricKeys: ["focus_count"] },
];

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function isIsoDate(value: string) {
  return /^\d{4}-\d{2}-\d{2}$/.test(value);
}

function readInitialDashboardDate() {
  const value = new URLSearchParams(window.location.search).get("date") || "";
  return isIsoDate(value) ? value : todayIso();
}

function readInitialDashboardTab() {
  const value = new URLSearchParams(window.location.search).get("tab") || "";
  return TAB_KEYS.has(value) ? value : "today";
}

function dashboardPath(bitrixMode: boolean | undefined, date: string, tab: string) {
  const params = new URLSearchParams();
  params.set("date", date);
  params.set("tab", tab);
  return `${bitrixMode ? "/bitrix/executive-dashboard/" : "/executive-dashboard/"}?${params.toString()}`;
}

function updateDashboardHistory(date: string, tab: string, mode: "push" | "replace" = "push") {
  const url = new URL(window.location.href);
  url.searchParams.set("date", date);
  url.searchParams.set("tab", tab);
  const state = {
    ...(window.history.state || {}),
    executiveDashboard: true,
    executiveDashboardDate: date,
    executiveDashboardTab: tab,
  };
  const path = `${url.pathname}${url.search}${url.hash}`;
  if (mode === "replace") window.history.replaceState(state, "", path);
  else window.history.pushState(state, "", path);
}

function drilldownHrefWithReturn(
  href: string | null | undefined,
  options: {
    bitrixMode: boolean | undefined;
    date: string;
    tab: string;
  }
) {
  if (!href) return href;
  try {
    const target = new URL(href, window.location.origin);
    const isReceivablesDrilldown =
      target.origin === window.location.origin &&
      (target.pathname.startsWith("/bitrix/receivables") ||
        target.pathname.startsWith("/receivables/workplace"));
    if (!isReceivablesDrilldown) return href;
    target.searchParams.set("return_to", dashboardPath(options.bitrixMode, options.date, options.tab));
    return `${target.pathname}${target.search}${target.hash}`;
  } catch {
    return href;
  }
}

function isoFromDate(value: Date) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function addDaysIso(value: string, days: number) {
  const parsed = new Date(`${value}T00:00:00`);
  parsed.setDate(parsed.getDate() + days);
  return isoFromDate(parsed);
}

function monthStartIso(value: string) {
  return `${value.slice(0, 8)}01`;
}

function monthEndIso(value: string) {
  const start = new Date(`${monthStartIso(value)}T00:00:00`);
  const firstOfNextMonth = new Date(start.getFullYear(), start.getMonth() + 1, 1);
  firstOfNextMonth.setDate(firstOfNextMonth.getDate() - 1);
  return isoFromDate(firstOfNextMonth);
}

function statusLabel(status: string) {
  const labels: Record<string, string> = {
    fresh: "актуально",
    ready: "готово",
    partial: "частично",
    stale: "устарело",
    empty: "пусто",
    source_missing: "нет источника",
    source_unverified: "источник не подтверждён",
    source_error: "ошибка",
    insufficient_history: "недостаточно истории",
    not_applicable: "не рассчитывается",
    complete: "период закрыт",
    approved: "утверждено",
    draft: "черновик",
    provided: "задан вручную",
    fallback: "резервный источник",
    missing: "нет источника",
    unknown: "статус не указан",
  };
  return labels[status] || status;
}

function severityLabel(value: string) {
  const labels: Record<string, string> = {
    critical: "критично",
    high: "важно",
    warning: "предупреждение",
    medium: "средне",
    low: "низко",
  };
  return labels[value] || value;
}

function formatMoney(value: string | number | null | undefined, currency = "RUB") {
  if (value === null || value === undefined || value === "") return "0";
  const numberValue = Number(value);
  if (!Number.isFinite(numberValue)) return String(value);
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 0,
    style: "currency",
    currency,
  }).format(numberValue);
}

function formatMetricValue(value: string | number | null | undefined, unit?: string | null) {
  if (value === null || value === undefined) return "нет данных";
  if (unit === "RUB") return formatMoney(value);
  if (unit === "percent") return formatPercent(value);
  if (unit === "days") return `${formatPlainNumber(value)} дн.`;
  if (unit === "ratio") return formatPlainNumber(value);
  return typeof value === "number" ? new Intl.NumberFormat("ru-RU").format(value) : String(value);
}

function formatPercent(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === "") return "нет данных";
  const numberValue = Number(value);
  if (!Number.isFinite(numberValue)) return String(value);
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 1,
    style: "percent",
  }).format(numberValue);
}

function formatPlainNumber(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === "") return "0";
  const numberValue = Number(value);
  if (!Number.isFinite(numberValue)) return String(value);
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 2,
    minimumFractionDigits: Math.abs(numberValue) < 1000 && numberValue !== 0 ? 2 : 0,
  }).format(numberValue);
}

function formatPercentPoints(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === "") return "нет данных";
  return `${formatPlainNumber(value)}%`;
}

function formatDate(value?: string | null) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value.slice(0, 10);
  return parsed.toLocaleDateString("ru-RU");
}

function formatDateTime(value?: string | null) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value.replace("T", " ").slice(0, 16);
  return parsed.toLocaleString("ru-RU", {
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
}

function errorMessage(error: unknown) {
  const status =
    typeof error === "object" && error !== null && "response" in error
      ? (error as { response?: { status?: number } }).response?.status
      : undefined;
  if (status === 401) return "Сессия не принята или истекла. Обновите страницу в Bitrix24.";
  if (status === 403) return "Нет доступа к управленческой витрине.";
  if (status && status >= 500) return "Источник временно недоступен. Повторите загрузку через минуту.";
  if (error instanceof Error && /network|failed to fetch|timeout/i.test(error.message)) {
    return "Нет связи с витриной. Проверьте подключение и повторите загрузку.";
  }
  return "Не удалось загрузить витрину. Повторите попытку.";
}

function moneyBlock(data: ExecutiveDashboardResponse | null) {
  return data?.blocks.find((block) => block.key === MONEY_TAB_KEY) || null;
}

function isTabAllowed(data: ExecutiveDashboardResponse, tab: string) {
  if (tab === "today") return true;
  if (tab === ODDS_CASHFLOW_TAB_KEY) return Boolean(moneyBlock(data));
  if (tab === ONLINE_STORE_TAB_KEY) return data.allowed_blocks.includes(ONLINE_STORE_TAB_KEY);
  return data.blocks.some((block) => block.key === tab);
}

function actionDomainForTab(tab: string) {
  if (tab === "today") return undefined;
  if (tab === ODDS_CASHFLOW_TAB_KEY) return MONEY_TAB_KEY;
  if (tab === ONLINE_STORE_TAB_KEY) return undefined;
  return tab;
}

function visibleBlocks(data: ExecutiveDashboardResponse | null, tab: string) {
  if (!data) return [];
  if (tab === "today") return data.blocks.filter((block) => block.key !== "daily_focus");
  if ([ODDS_CASHFLOW_TAB_KEY, PROFIT_LOSS_TAB_KEY, SALES_TAB_KEY, ONLINE_STORE_TAB_KEY].includes(tab)) return [];
  return data.blocks.filter((block) => block.key === tab);
}

function tabsForData(data: ExecutiveDashboardResponse | null) {
  if (!data) return TAB_DEFINITIONS;
  const availableKeys = new Set(data.blocks.map((block) => block.key));
  return TAB_DEFINITIONS.filter(
    (item) =>
      item.key === "today" ||
      availableKeys.has(item.key) ||
      (item.key === ONLINE_STORE_TAB_KEY && data.allowed_blocks.includes(ONLINE_STORE_TAB_KEY)) ||
      (item.key === ODDS_CASHFLOW_TAB_KEY && availableKeys.has(MONEY_TAB_KEY))
  );
}

function tabLabel(tab: string) {
  return TAB_LABELS[tab] || DOMAIN_LABELS[tab] || tab;
}

function metricForStep(block: ExecutiveDashboardBlock | undefined, metricKeys: string[]) {
  if (!block) return null;
  return metricKeys.map((key) => block.metrics.find((metric) => metric.key === key)).find(Boolean) || block.metrics[0] || null;
}

function metricNumberValue(metric: ExecutiveDashboardMetric | null | undefined) {
  if (!metric || metric.value === null || metric.value === undefined || metric.value === "") return null;
  const value = Number(metric.value);
  return Number.isFinite(value) ? value : null;
}

function metricIsZero(metric: ExecutiveDashboardMetric | undefined) {
  return metricNumberValue(metric) === 0;
}

function metricValuesMatch(first: ExecutiveDashboardMetric | undefined, second: ExecutiveDashboardMetric | undefined) {
  const firstValue = metricNumberValue(first);
  const secondValue = metricNumberValue(second);
  if (firstValue === null || secondValue === null) return false;
  return firstValue === secondValue;
}

function visibleMetricsForBlock(block: ExecutiveDashboardBlock) {
  if (block.key === "procurement_import") {
    const paymentReady = block.metrics.find((metric) => metric.key === "payment_ready_amount");
    return block.metrics.filter(
      (metric) =>
        metric.key !== "currency_exposure" || !paymentReady || !metricValuesMatch(metric, paymentReady)
    );
  }
  if (block.key === "reconciliation") {
    return block.metrics.filter(
      (metric) =>
        metric.key === "unmatched_count" ||
        metric.key === "issue_amount_abs" ||
        !["unconfirmed_documents", "dds_issue_count", "report_task_count"].includes(metric.key) ||
        !metricIsZero(metric)
    );
  }
  return block.metrics;
}

function blockUsesPlaceholder(block: ExecutiveDashboardBlock) {
  return block.source_status === "source_missing" || block.source_status === "source_error";
}

function summaryString(summary: Record<string, unknown>, key: string) {
  const value = summary[key];
  return typeof value === "string" && value.trim() ? value : null;
}

function summaryArray<T>(summary: Record<string, unknown>, key: string): T[] {
  const value = summary[key];
  return Array.isArray(value) ? (value as T[]) : [];
}

function summaryRecord(summary: Record<string, unknown>, key: string): Record<string, unknown> {
  const value = summary[key];
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function metricByKey(block: ExecutiveDashboardBlock, keys: string[]) {
  const wanted = new Set(keys);
  return block.metrics.filter((metric) => wanted.has(metric.key));
}

function metricBySingleKey(block: ExecutiveDashboardBlock, key: string) {
  return block.metrics.find((metric) => metric.key === key) || null;
}

function metricDisplay(metric: ExecutiveDashboardMetric | null | undefined, fallback = "нет данных") {
  if (!metric) return fallback;
  return metric.masked ? "скрыто" : formatMetricValue(metric.value, metric.unit);
}

function currencyName(row: CashPositionBreakdownRow) {
  return row.cash_currency_name || row.cash_currency_code || "валюта";
}

function cashCategoryLabel(row: CashPositionBreakdownRow) {
  return row.cash_category_label || row.cash_category || "Деньги";
}

function reportDelivery(summary: Record<string, unknown>): ReconciliationReportDelivery {
  const value = summary.report_delivery;
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as ReconciliationReportDelivery)
    : {};
}

function recordText(row: Record<string, unknown>, keys: string[], fallback = "") {
  for (const key of keys) {
    const value = row[key];
    if (typeof value === "string" && value.trim()) return value;
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
  }
  return fallback;
}

function recordValue(row: Record<string, unknown>, key: string) {
  const value = row[key];
  return typeof value === "string" || typeof value === "number" ? value : null;
}

function MoneySection({
  metrics,
  note,
  status,
  title,
}: {
  metrics: ExecutiveDashboardMetric[];
  note: string | null;
  status: string;
  title: string;
}) {
  const empty = metrics.length === 0 || status === "source_missing" || status === "source_error";
  return (
    <section className={`executive-money-section executive-money-section--${status}`}>
      <header>
        <strong>{title}</strong>
        <em>{statusLabel(status)}</em>
      </header>
      {empty ? (
        <div className="executive-money-section__empty">
          {note || "Источник пока не подключен к витрине."}
        </div>
      ) : (
        <div className="executive-block__metrics">
          {metrics.map((metric) => (
            <div className={`executive-metric executive-metric--${metric.tone}`} key={metric.key}>
              <span>{metric.label}</span>
              <strong>{metric.masked ? "скрыто" : formatMetricValue(metric.value, metric.unit)}</strong>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function MoneyBlockCard({
  block,
  compactForMoneyTab = false,
  drilldownHref,
}: {
  block: ExecutiveDashboardBlock;
  compactForMoneyTab?: boolean;
  drilldownHref?: string | null;
}) {
  const cashPositionStatus = summaryString(block.summary, "cash_position_source_status") || "source_missing";
  const cashflowStatus = summaryString(block.summary, "cashflow_today_source_status") || "source_missing";
  const note = summaryString(block.summary, "note");
  const cashPositionNote = summaryString(block.summary, "cash_position_note");
  const cashflowNote = summaryString(block.summary, "cashflow_today_note");
  const currencyBreakdown = summaryArray<CashPositionBreakdownRow>(
    block.summary,
    "cash_position_breakdown_by_currency"
  ).slice(0, 8);
  const cashPositionMetrics = metricByKey(
    block,
    compactForMoneyTab
      ? [
          "cash_position_bank_balance_total",
          "cash_position_savings_balance_total",
          "cash_position_cashbox_balance_total",
          "cash_position_card_balance_total",
          "cash_position_other_balance_total",
        ]
      : [
          "cash_position_total_balance",
          "cash_position_bank_balance_total",
          "cash_position_savings_balance_total",
          "cash_position_cashbox_balance_total",
          "cash_position_card_balance_total",
          "cash_position_other_balance_total",
        ]
  );
  const cashflowMetrics = metricByKey(
    block,
    compactForMoneyTab
      ? ["cashflow_movement_count"]
      : [
          "cashflow_inflow_amount",
          "cashflow_outflow_amount",
          "cashflow_net_amount",
          "cashflow_movement_count",
        ]
  );
  const controlMetrics = metricByKey(
    block,
    compactForMoneyTab
      ? [
          "cash_position_negative_balance_total",
          "cashflow_review_count",
          "cashflow_internal_transfer_count",
          "acquiring_pending",
        ]
      : [
          "cash_position_foreign_balance_total",
          "cash_position_negative_balance_total",
          "cash_position_currency_count",
          "cashflow_review_count",
          "cashflow_internal_transfer_count",
          "acquiring_pending",
        ]
  );
  return (
    <section className={`executive-block executive-block--money executive-block--${block.source_status}`}>
      <header className="executive-block__header">
        <div>
          <h2>{block.title}</h2>
          {block.as_of && <span>на {formatDate(block.as_of)}</span>}
        </div>
        <em>{statusLabel(block.source_status)}</em>
      </header>
      <div className="executive-money-sections">
        <MoneySection
          metrics={cashPositionMetrics}
          note={cashPositionNote}
          status={cashPositionStatus}
          title={compactForMoneyTab ? "Структура остатков" : "Остатки сейчас"}
        />
        {currencyBreakdown.length > 0 &&
          cashPositionStatus !== "source_missing" &&
          cashPositionStatus !== "source_error" && (
            <section className="executive-money-section executive-money-section--currency">
              <header>
                <strong>Валютная структура</strong>
                <em>валюта + ₽</em>
              </header>
              <div className="executive-currency-table">
                {currencyBreakdown.map((row, index) => (
                  <div
                    className="executive-currency-row"
                    key={`${row.cash_category}-${row.cash_currency_code}-${index}`}
                  >
                    <span>
                      <strong>{cashCategoryLabel(row)}</strong>
                      <em>{currencyName(row)}</em>
                    </span>
                    <span>
                      {formatPlainNumber(row.balance_native)} {currencyName(row)}
                    </span>
                    <strong>{formatMoney(row.balance_rub)}</strong>
                  </div>
                ))}
              </div>
            </section>
          )}
        <MoneySection
          metrics={cashflowMetrics}
          note={cashflowNote}
          status={cashflowStatus}
          title={compactForMoneyTab ? "Детали ДДС" : "ДДС сегодня"}
        />
        <MoneySection
          metrics={controlMetrics}
          note={controlMetrics.length ? null : "Нет денежных контрольных сигналов на карточке."}
          status={controlMetrics.length ? block.source_status : "ready"}
          title="Контроль"
        />
      </div>
      {(note || drilldownHref) && (
        <footer className="executive-block__footer">
          {note && <span>{note}</span>}
          {drilldownHref && (
            <a className="executive-block__drilldown" href={drilldownHref}>
              Открыть источник
            </a>
          )}
        </footer>
      )}
    </section>
  );
}

function FlowMap({
  activeTab,
  data,
  onSelect,
}: {
  activeTab: string;
  data: ExecutiveDashboardResponse;
  onSelect: (tab: string) => void;
}) {
  const blockByKey = new Map(data.blocks.map((block) => [block.key, block]));
  const visibleSteps = FLOW_STEPS.filter((step) => blockByKey.has(step.key));
  return (
    <section
      className={visibleSteps.length <= 4 ? "executive-flow executive-flow--compact" : "executive-flow"}
      aria-label="Линия управленческой витрины"
    >
      {visibleSteps.map((step, index) => {
        const block = blockByKey.get(step.key);
        const metric = metricForStep(block, step.metricKeys);
        const targetTab = step.key === "daily_focus" ? "today" : step.key;
        const isActive = activeTab === targetTab || (activeTab === "today" && step.key === "daily_focus");
        const sourceStatus = block?.source_status || "source_missing";
        return (
          <button
            className={[
              "executive-flow__node",
              `executive-flow__node--${sourceStatus}`,
              isActive ? "executive-flow__node--active" : "",
            ]
              .filter(Boolean)
              .join(" ")}
            key={step.key}
            onClick={() => onSelect(targetTab)}
            aria-pressed={isActive}
            type="button"
          >
            <span className="executive-flow__number">{index + 1}</span>
            <span className="executive-flow__label">{step.label}</span>
            <strong>{metric ? (metric.masked ? "скрыто" : formatMetricValue(metric.value, metric.unit)) : "нет данных"}</strong>
            <small>{statusLabel(sourceStatus)}</small>
          </button>
        );
      })}
    </section>
  );
}

function MoneyTabOverview({
  block,
  reconciliationBlock,
}: {
  block: ExecutiveDashboardBlock;
  reconciliationBlock?: ExecutiveDashboardBlock;
}) {
  const total = metricBySingleKey(block, "cash_position_total_balance");
  const foreign = metricBySingleKey(block, "cash_position_foreign_balance_total");
  const negative = metricBySingleKey(block, "cash_position_negative_balance_total");
  const review = metricBySingleKey(block, "cashflow_review_count");
  const inflow = metricBySingleKey(block, "cashflow_inflow_amount");
  const outflow = metricBySingleKey(block, "cashflow_outflow_amount");
  const net = metricBySingleKey(block, "cashflow_net_amount");
  const unmatched = reconciliationBlock ? metricBySingleKey(reconciliationBlock, "unmatched_count") : null;
  const issueAmount = reconciliationBlock ? metricBySingleKey(reconciliationBlock, "issue_amount_abs") : null;
  const ddsIssueCount = reconciliationBlock ? metricBySingleKey(reconciliationBlock, "dds_issue_count") : null;
  const inflowValue = Math.abs(metricNumberValue(inflow) || 0);
  const outflowValue = Math.abs(metricNumberValue(outflow) || 0);
  const maxFlow = Math.max(inflowValue, outflowValue, 1);
  const inflowWidth = `${Math.max(4, Math.round((inflowValue / maxFlow) * 100))}%`;
  const outflowWidth = `${Math.max(4, Math.round((outflowValue / maxFlow) * 100))}%`;
  const unmatchedValue = metricNumberValue(unmatched);
  const issueAmountValue = metricNumberValue(issueAmount);
  const ddsIssueValue = metricNumberValue(ddsIssueCount);
  const negativeValue = metricNumberValue(negative);
  const reviewValue = metricNumberValue(review);
  const hasReconciliationIssue =
    (unmatchedValue !== null && unmatchedValue > 0) || (issueAmountValue !== null && issueAmountValue > 0);
  const hasDdsIssue =
    (ddsIssueValue !== null && ddsIssueValue > 0) ||
    (negativeValue !== null && negativeValue < 0) ||
    (reviewValue !== null && reviewValue > 0);

  return (
    <section className="executive-tab-context executive-tab-context--money" aria-label="KPI вкладки Деньги / ДДС">
      <div className="executive-tab-kpis">
        <div className="executive-tab-kpi executive-tab-kpi--primary">
          <span>Денег всего</span>
          <strong>{metricDisplay(total)}</strong>
          <small>1С, рублевый эквивалент</small>
        </div>
        <div className="executive-tab-kpi">
          <span>Остатки в валюте</span>
          <strong>{metricDisplay(foreign)}</strong>
          <small>валюта и пересчет в ₽ ниже</small>
        </div>
        <div className={`executive-tab-kpi ${hasReconciliationIssue ? "executive-tab-kpi--warning" : ""}`}>
          <span>Сверки Сбер/1С</span>
          <strong>{metricDisplay(unmatched)}</strong>
          <small>{issueAmount ? `дельта: ${metricDisplay(issueAmount)}` : "регулярный отчет"}</small>
        </div>
        <div className={`executive-tab-kpi ${hasDdsIssue ? "executive-tab-kpi--warning" : ""}`}>
          <span>Ошибки ДДС</span>
          <strong>{ddsIssueCount ? metricDisplay(ddsIssueCount) : metricDisplay(review)}</strong>
          <small>{negative ? `минусы: ${metricDisplay(negative)}` : statusLabel(block.source_status)}</small>
        </div>
      </div>
      <div className="executive-cashflow-widget" aria-label="ДДС за день">
        <header>
          <div>
            <span>ДДС сегодня</span>
            <strong>{metricDisplay(net)}</strong>
          </div>
          <em>{statusLabel(block.source_status)}</em>
        </header>
        <div className="executive-cashflow-bars">
          <div className="executive-cashflow-bar">
            <span>Поступило</span>
            <div>
              <i style={{ width: inflowWidth }} />
            </div>
            <strong>{metricDisplay(inflow)}</strong>
          </div>
          <div className="executive-cashflow-bar executive-cashflow-bar--out">
            <span>Списано</span>
            <div>
              <i style={{ width: outflowWidth }} />
            </div>
            <strong>{metricDisplay(outflow)}</strong>
          </div>
        </div>
      </div>
    </section>
  );
}

function TabKpiOverview({ block, data }: { block: ExecutiveDashboardBlock; data: ExecutiveDashboardResponse }) {
  if (block.key === "money_today") {
    return (
      <MoneyTabOverview
        block={block}
        reconciliationBlock={data.blocks.find((item) => item.key === "reconciliation")}
      />
    );
  }
  const visibleMetrics = visibleMetricsForBlock(block).slice(0, 4);
  const sourceAnchor =
    typeof block.summary.source_anchor === "string" ? block.summary.source_anchor : null;
  const note = typeof block.summary.note === "string" ? block.summary.note : null;
  return (
    <section className={`executive-tab-context executive-tab-context--${block.source_status}`}>
      <div className="executive-tab-kpis">
        {blockUsesPlaceholder(block) ? (
          <div className="executive-tab-kpi executive-tab-kpi--placeholder">
            <span>{statusLabel(block.source_status)}</span>
            <strong>{sourceAnchor || block.title}</strong>
            <small>{note || "Источник пока не подключен к витрине."}</small>
          </div>
        ) : (
          visibleMetrics.map((metric) => (
            <div className={`executive-tab-kpi executive-tab-kpi--${metric.tone}`} key={metric.key}>
              <span>{metric.label}</span>
              <strong>{metricDisplay(metric)}</strong>
              <small>{statusLabel(metric.source_status)}</small>
            </div>
          ))
        )}
      </div>
    </section>
  );
}

function ProcurementImportPanel({
  actions,
  block,
  dashboardSourceStatus,
  generatedAt,
  onOpenAction,
}: {
  actions: ExecutiveDashboardAction[];
  block: ExecutiveDashboardBlock | null;
  dashboardSourceStatus: string;
  generatedAt: string;
  onOpenAction: (action: ExecutiveDashboardAction) => void;
}) {
  const [severityFilter, setSeverityFilter] = useState("");
  const [responsibleFilter, setResponsibleFilter] = useState("");
  const [supplierFilter, setSupplierFilter] = useState("");
  const [reasonFilter, setReasonFilter] = useState("");
  const [showAllActions, setShowAllActions] = useState(false);
  const note = block ? summaryString(block.summary, "note") : null;
  const orders = block ? metricBySingleKey(block, "open_supplier_orders") : null;
  const openAmount = block ? metricBySingleKey(block, "open_order_amount_rub") : null;
  const riskCountMetric = block ? metricBySingleKey(block, "procurement_at_risk_count") : null;
  const riskAmount = block ? metricBySingleKey(block, "procurement_at_risk_amount_rub") : null;
  const criticalCount = block ? metricBySingleKey(block, "critical_overdue_count") : null;
  const foreignAmount = block ? metricBySingleKey(block, "foreign_open_order_amount_rub") : null;
  const riskSummary = block ? summaryRecord(block.summary, "risk_summary") : {};
  const stages = block ? summaryArray<Record<string, unknown>>(block.summary, "stage_breakdown") : [];
  const currencies = block ? summaryArray<Record<string, unknown>>(block.summary, "currency_breakdown") : [];
  const dataQuality = block ? summaryRecord(block.summary, "data_quality") : {};
  const sourceStatus = block?.source_status || "source_missing";
  const effectiveSourceStatus = block?.freshness_status === "stale" ? "stale" : sourceStatus;
  const usePlaceholder = !block || blockUsesPlaceholder(block);
  const riskCount = metricNumberValue(riskCountMetric);
  const riskShare = riskSummary.at_risk_share_pct;
  const scoringV2 = block?.summary.risk_scoring_version === 2;
  const openAmountValue = metricNumberValue(openAmount);
  const foreignAmountValue = metricNumberValue(foreignAmount);
  const foreignShare =
    scoringV2 && !openAmount?.masked && !foreignAmount?.masked && openAmountValue && foreignAmountValue !== null
      ? (foreignAmountValue / openAmountValue) * 100
      : null;
  const filterOptions = (key: string) => Array.from(
    new Set(actions.flatMap((action) => {
      const value = actionPayloadText(action, key);
      return value ? [value] : [];
    }))
  ).sort();
  const filteredActions = actions
    .filter((action) =>
      (!severityFilter || action.severity === severityFilter) &&
      (!responsibleFilter || actionPayloadText(action, "responsible_name") === responsibleFilter) &&
      (!supplierFilter || actionPayloadText(action, "supplier_title") === supplierFilter) &&
      (!reasonFilter || actionPayloadText(action, "reason_code") === reasonFilter)
    )
    .sort((first, second) => {
      const severityRank: Record<string, number> = { critical: 0, high: 1, warning: 2, medium: 3, low: 4 };
      const severityDiff = (severityRank[first.severity] ?? 5) - (severityRank[second.severity] ?? 5);
      if (severityDiff) return severityDiff;
      const firstDeadline = Date.parse(actionPayloadText(first, "deadline_date") || "") || Number.POSITIVE_INFINITY;
      const secondDeadline = Date.parse(actionPayloadText(second, "deadline_date") || "") || Number.POSITIVE_INFINITY;
      if (firstDeadline !== secondDeadline) return firstDeadline - secondDeadline;
      return Number(second.amount || 0) - Number(first.amount || 0);
    });
  const visibleActions = showAllActions ? filteredActions : filteredActions.slice(0, 5);
  const totalOrders = metricNumberValue(orders) || stages.reduce((sum, row) => sum + Number(row.count || 0), 0);
  const visibleCurrencies = currencies.filter((row) => String(row.currency || "").toUpperCase() !== "RUB");
  const rubCurrency = currencies.find((row) => String(row.currency || "").toUpperCase() === "RUB");
  const rowShare = (row: Record<string, unknown>) => {
    if (!scoringV2) return null;
    const amount = Number(row.amount_rub);
    if (!openAmount?.masked && openAmountValue && Number.isFinite(amount)) return (amount / openAmountValue) * 100;
    const count = Number(row.count);
    return totalOrders && Number.isFinite(count) ? (count / totalOrders) * 100 : null;
  };
  const resetPreview = () => setShowAllActions(false);

  return (
    <section className="executive-cashflow-period executive-procurement-period" aria-label="Закупки">
      <header className="executive-panel__header">
        <div>
          <h2>Закупки</h2>
          <span>Открытые заказы, этапы поставки и приоритетная очередь действий</span>
        </div>
      </header>
      {usePlaceholder ? (
        <div className="executive-cashflow-period__empty">
          {note || "Источник закупок пока не подключен к витрине."}
        </div>
      ) : (
        <>
          <div aria-label="Статус источника закупок" className={`executive-procurement-status executive-procurement-status--${effectiveSourceStatus}`}>
            <div>
              <StatusBadge tone={effectiveSourceStatus === "ready" ? "success" : effectiveSourceStatus === "source_error" ? "danger" : "warning"}>
                {statusLabel(effectiveSourceStatus)}
              </StatusBadge>
              <strong>Данные актуальны на {block?.as_of ? formatDate(block.as_of) : "—"}</strong>
              <span>Свежесть: {statusLabel(block?.freshness_status || sourceStatus)}</span>
            </div>
            <span><strong>{actions.length}</strong> решений в работе</span>
          </div>
          {dashboardSourceStatus === "partial" && sourceStatus === "ready" && (
            <div className="executive-procurement-context-note" role="status">
              Закупки готовы. Статус «частично» относится к другим разделам управленческой витрины.
            </div>
          )}
          <div aria-label="Основные KPI закупок" className="executive-panel__kpis">
            <MetricCard
              className="executive-procurement-kpi"
              hint={`Сумма: ${metricDisplay(openAmount)}`}
              label="Открытые заказы"
              tooltip="Количество открытых заказов поставщикам в контурах карго и ВЭД-импорта."
              value={metricDisplay(orders)}
            />
            <MetricCard
              className="executive-procurement-kpi executive-procurement-kpi--warning"
              hint={`${metricDisplay(riskCountMetric)} заказов${riskShare !== undefined ? ` · ${riskShare}% открытых закупок` : ""}`}
              label="Заказы под риском"
              tone={riskCount && riskCount > 0 ? "warning" : "neutral"}
              tooltip="Только просрочка ожидаемого поступления или превышение нормального срока подготовки поставщиком."
              value={metricDisplay(riskAmount)}
            />
            <MetricCard
              className="executive-procurement-kpi executive-procurement-kpi--danger"
              hint={metricNumberValue(criticalCount) ? <StatusBadge tone="danger">Критично</StatusBadge> : "Критических заказов нет"}
              label="Критически просрочено"
              tone={metricNumberValue(criticalCount) ? "danger" : "neutral"}
              tooltip="Ожидаемая дата поступления прошла либо критически превышен срок подготовки."
              value={metricDisplay(criticalCount)}
            />
            <MetricCard
              className="executive-procurement-kpi"
              hint={foreignShare === null ? "Рублёвый эквивалент" : `${foreignShare.toLocaleString("ru-RU", { maximumFractionDigits: 1 })}% суммы открытых заказов`}
              label="Открытые закупки в валюте"
              tooltip="Рублёвый эквивалент открытых валютных заказов. Это не расчёт курсового риска."
              value={metricDisplay(foreignAmount)}
            />
          </div>
          {!scoringV2 && (
            <div className="executive-cashflow-period__note">
              Источник v1: показаны прежние признаки незаполненных дат без нового риск-скоринга.
            </div>
          )}
          <section className="executive-procurement-period__actions" aria-label="Решения по закупкам">
            <header>
              <div>
                <h3>Заказы в зоне внимания</h3>
                <span>Критические — первыми; откройте строку для формулы риска и действия в 1С.</span>
              </div>
            </header>
            <div className="executive-procurement-filters" aria-label="Фильтры закупочной очереди">
              <select aria-label="Важность" onChange={(event) => { setSeverityFilter(event.target.value); resetPreview(); }} value={severityFilter}>
                <option value="">Вся важность</option>
                <option value="critical">Критично</option><option value="warning">Предупреждение</option>
              </select>
              <select aria-label="Ответственный" onChange={(event) => { setResponsibleFilter(event.target.value); resetPreview(); }} value={responsibleFilter}>
                <option value="">Все ответственные</option>{filterOptions("responsible_name").map((value) => <option key={value}>{value}</option>)}
              </select>
              <select aria-label="Поставщик" onChange={(event) => { setSupplierFilter(event.target.value); resetPreview(); }} value={supplierFilter}>
                <option value="">Все поставщики</option>{filterOptions("supplier_title").map((value) => <option key={value}>{value}</option>)}
              </select>
              <select aria-label="Причина" onChange={(event) => { setReasonFilter(event.target.value); resetPreview(); }} value={reasonFilter}>
                <option value="">Все причины</option>{filterOptions("reason_code").map((value) => <option key={value} value={value}>{actionPayloadText(actions.find((action) => actionPayloadText(action, "reason_code") === value)!, "reason") || value}</option>)}
              </select>
            </div>
            {filteredActions.length === 0 ? <div className="executive-actions__empty">Заказов по выбранным фильтрам нет.</div> : (
              <div className="executive-actions__table-wrap">
                <table className="executive-actions__table executive-procurement-table">
                  <thead><tr><th>Заказ</th><th>Этап</th><th>Сумма</th><th>Срок</th><th>Просрочка</th><th>Ответственный</th><th>Причина</th></tr></thead>
                  <tbody>{visibleActions.map((action) => <tr aria-label={`Открыть заказ ${actionPayloadText(action, "onec_source_number") || action.source_ref}`} key={action.stable_key} onClick={() => onOpenAction(action)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onOpenAction(action); } }} tabIndex={0}>
                    <td data-label="Заказ"><strong>{actionPayloadText(action, "onec_source_number") || action.source_ref}</strong><small>{actionPayloadText(action, "supplier_title")}</small></td>
                    <td data-label="Этап">{actionPayloadText(action, "management_stage_label") || "—"}</td>
                    <td data-label="Сумма">{action.amount ? formatMoney(action.amount, action.currency) : "скрыто"}</td>
                    <td data-label="Срок">{formatDate(actionPayloadText(action, "deadline_date")) || "—"}</td>
                    <td data-label="Просрочка">{actionPayloadText(action, "days_overdue") ? `${actionPayloadText(action, "days_overdue")} дн.` : "—"}</td>
                    <td data-label="Ответственный">{actionPayloadText(action, "responsible_name") || "Не указан"}</td>
                    <td data-label="Причина"><StatusBadge tone={action.severity === "critical" ? "danger" : "warning"}>{severityLabel(action.severity)}</StatusBadge><span>{actionPayloadText(action, "reason")}</span></td>
                  </tr>)}</tbody>
                </table>
              </div>
            )}
            {!showAllActions && filteredActions.length > 5 && (
              <Button onClick={() => setShowAllActions(true)} variant="secondary">Показать все {filteredActions.length}</Button>
            )}
          </section>
          {(stages.length > 0 || currencies.length > 0) && (
            <div className="executive-procurement-breakdowns">
              <section aria-label="Этапы закупок">
                <h3>Этапы открытых заказов</h3>
                {scoringV2 && <div aria-hidden="true" className="executive-procurement-distribution">{stages.map((row) => <i key={String(row.key)} style={{ flexGrow: Math.max(0, Number(row.count || 0)) }} />)}</div>}
                {stages.map((row) => {
                  const share = rowShare(row);
                  return <div className="executive-procurement-breakdown-row" key={String(row.key)}>
                    <span>{String(row.label || row.key)}{share === null ? "" : ` · ${share.toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`}</span>
                    <strong>{String(row.count || 0)} · {row.amount_rub == null ? "скрыто" : formatMoney(row.amount_rub as string | number)}</strong>
                  </div>;
                })}
              </section>
              <section aria-label="Валютная структура закупок">
                <h3>Открытые закупки в иностранной валюте</h3>
                {scoringV2 && visibleCurrencies.length > 0 && <div aria-hidden="true" className="executive-procurement-distribution executive-procurement-distribution--currency">{visibleCurrencies.map((row) => <i key={String(row.currency)} style={{ flexGrow: Math.max(0, Number(row.count || 0)) }} />)}</div>}
                {visibleCurrencies.map((row) => {
                  const share = rowShare(row);
                  return <div className="executive-procurement-breakdown-row" key={String(row.currency)}>
                    <span>{String(row.currency || "—")}{share === null ? "" : ` · ${share.toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`}</span>
                    <strong>{String(row.count || 0)} · {row.amount_rub == null ? "скрыто" : formatMoney(row.amount_rub as string | number)}</strong>
                  </div>;
                })}
                {rubCurrency && <div className="executive-procurement-rub-row"><span>Рублёвые заказы</span><strong>{String(rubCurrency.count || 0)} · {rubCurrency.amount_rub == null ? "скрыто" : formatMoney(rubCurrency.amount_rub as string | number)}</strong></div>}
              </section>
            </div>
          )}
          {Object.keys(dataQuality).length > 0 && (
            <details className="executive-procurement-quality">
              <summary>Проблемы в данных</summary>
              <p>Полнота ответственных: {String(dataQuality.responsible_coverage_pct ?? "—")}% · без ответственного: {String(dataQuality.missing_responsible_count ?? 0)} · без ожидаемой даты после карго: {String(dataQuality.missing_expected_receipt_after_cargo_count ?? 0)}</p>
            </details>
          )}
          <details className="executive-procurement-source">
            <summary>Об источнике</summary>
            <p>Текущие открытые заказы карго и ВЭД из 1С. Витрина работает только на чтение и не является платёжным календарём.</p>
            <span>Источник сформирован {formatDateTime(generatedAt)}.</span>
          </details>
        </>
      )}
    </section>
  );
}

function WarehouseBlockCard({
  block,
  drilldownHref,
}: {
  block: ExecutiveDashboardBlock;
  drilldownHref?: string | null;
}) {
  const sourceAnchor =
    typeof block.summary.source_anchor === "string" ? block.summary.source_anchor : null;
  const note = typeof block.summary.note === "string" ? block.summary.note : null;
  const drilldownLabel =
    typeof block.summary.drilldown_label === "string" ? block.summary.drilldown_label : "Открыть складскую аналитику";
  const topWarehouses = summaryArray<Record<string, unknown>>(block.summary, "top_warehouses").slice(0, 5);
  const qualityBreakdown = summaryArray<Record<string, unknown>>(block.summary, "quality_breakdown").slice(0, 4);
  const usePlaceholder = blockUsesPlaceholder(block);
  return (
    <section className={`executive-block executive-block--warehouse executive-block--${block.source_status}`}>
      <header className="executive-block__header">
        <div>
          <h2>{block.title}</h2>
          {block.as_of && <span>на {formatDate(block.as_of)}</span>}
        </div>
        <em>{statusLabel(block.source_status)}</em>
      </header>
      {usePlaceholder ? (
        <div className="executive-block__placeholder">
          <strong>{sourceAnchor || statusLabel(block.source_status)}</strong>
          <span>{note || "Источник складской аналитики пока не подключен к витрине."}</span>
        </div>
      ) : (
        <>
          <div className="executive-block__metrics">
            {visibleMetricsForBlock(block).map((metric) => (
              <div className={`executive-metric executive-metric--${metric.tone}`} key={metric.key}>
                <span>{metric.label}</span>
                <strong>{metricDisplay(metric)}</strong>
              </div>
            ))}
          </div>
          {topWarehouses.length > 0 && (
            <div className="executive-warehouse-list" aria-label="Топ складов по сборке">
              {topWarehouses.map((row, index) => (
                <div className="executive-warehouse-row" key={`${recordText(row, ["warehouse_name"], "Склад")}-${index}`}>
                  <span>
                    <strong>{recordText(row, ["warehouse_name"], "Склад не указан")}</strong>
                    <em>
                      {formatPlainNumber(recordValue(row, "pieces_picked"))} шт. · {formatPlainNumber(recordValue(row, "picker_count"))} сборщ.
                    </em>
                  </span>
                  <strong>{formatPlainNumber(recordValue(row, "pick_hours"))} ч</strong>
                </div>
              ))}
            </div>
          )}
          {qualityBreakdown.length > 0 && (
            <div className="executive-warehouse-quality" aria-label="Контроль качества склада">
              {qualityBreakdown.map((row, index) => (
                <div className="executive-warehouse-quality__item" key={`${recordText(row, ["key", "label"], "quality")}-${index}`}>
                  <span>{recordText(row, ["label", "key"], "Контроль")}</span>
                  <strong>{formatPlainNumber(recordValue(row, "count"))}</strong>
                </div>
              ))}
            </div>
          )}
        </>
      )}
      {(sourceAnchor || note || drilldownHref) && (
        <footer className="executive-block__footer">
          {!usePlaceholder && sourceAnchor && <strong>{sourceAnchor}</strong>}
          {!usePlaceholder && note && <span>{note}</span>}
          {drilldownHref && (
            <a className="executive-block__drilldown" href={drilldownHref}>
              {drilldownLabel}
            </a>
          )}
        </footer>
      )}
    </section>
  );
}

function BlockCard({
  activeTab,
  bitrixMode,
  block,
  date,
}: {
  activeTab: string;
  bitrixMode?: boolean;
  block: ExecutiveDashboardBlock;
  date: string;
}) {
  const drilldownHref = drilldownHrefWithReturn(block.drilldown_url, {
    bitrixMode,
    date,
    tab: activeTab,
  });
  if (block.key === "money_today") {
    return (
      <MoneyBlockCard
        block={block}
        compactForMoneyTab={activeTab === "money_today"}
        drilldownHref={drilldownHref}
      />
    );
  }
  if (block.key === "reconciliation") {
    return <ReconciliationBlockCard block={block} drilldownHref={drilldownHref} />;
  }
  if (block.key === "creditors_payables") {
    return <ManagementBalanceBlockCard block={block} drilldownHref={drilldownHref} />;
  }
  if (block.key === "warehouse_operations") {
    return <WarehouseBlockCard block={block} drilldownHref={drilldownHref} />;
  }
  const sourceAnchor =
    typeof block.summary.source_anchor === "string" ? block.summary.source_anchor : null;
  const note = typeof block.summary.note === "string" ? block.summary.note : null;
  const drilldownLabel =
    typeof block.summary.drilldown_label === "string" ? block.summary.drilldown_label : "Открыть источник";
  const visibleMetrics = visibleMetricsForBlock(block);
  const usePlaceholder = blockUsesPlaceholder(block);
  return (
    <section className={`executive-block executive-block--${block.source_status}`}>
      <header className="executive-block__header">
        <div>
          <h2>{block.title}</h2>
          {block.as_of && <span>на {formatDate(block.as_of)}</span>}
        </div>
        <em>{statusLabel(block.source_status)}</em>
      </header>
      {usePlaceholder ? (
        <div className="executive-block__placeholder">
          <strong>{sourceAnchor || statusLabel(block.source_status)}</strong>
          <span>{note || "Источник пока не подключен к витрине."}</span>
        </div>
      ) : (
        <div className="executive-block__metrics">
          {visibleMetrics.map((metric) => (
            <div className={`executive-metric executive-metric--${metric.tone}`} key={metric.key}>
              <span>{metric.label}</span>
              <strong>{metric.masked ? "скрыто" : formatMetricValue(metric.value, metric.unit)}</strong>
            </div>
          ))}
        </div>
      )}
      {(sourceAnchor || note || drilldownHref) && (
        <footer className="executive-block__footer">
          {!usePlaceholder && sourceAnchor && <strong>{sourceAnchor}</strong>}
          {!usePlaceholder && note && <span>{note}</span>}
          {drilldownHref && (
            <a className="executive-block__drilldown" href={drilldownHref}>
              {drilldownLabel}
            </a>
          )}
        </footer>
      )}
    </section>
  );
}

function managementBalanceAmount(line: ManagementBalanceLine) {
  if (line.masked) return "скрыто";
  return line.amount === null || line.amount === undefined ? "нет данных" : formatMoney(line.amount);
}

export function ManagementBalanceBlockCard({
  block,
  drilldownHref,
}: {
  block: ExecutiveDashboardBlock;
  drilldownHref?: string | null;
}) {
  const sourceAnchor = summaryString(block.summary, "source_anchor");
  const note = summaryString(block.summary, "note");
  const assets = summaryArray<ManagementBalanceLine>(block.summary, "balance_assets");
  const liabilities = summaryArray<ManagementBalanceLine>(block.summary, "balance_liabilities");
  const equity = summaryArray<ManagementBalanceLine>(block.summary, "balance_equity");
  const assetsTotal = block.summary.balance_assets_total;
  const liabilitiesTotal = block.summary.balance_liabilities_total;
  const assetsTotalLabel = summaryString(block.summary, "balance_assets_total_label") || "Итого активы";
  const liabilitiesTotalLabel = summaryString(block.summary, "balance_liabilities_total_label") || "Итого пассивы";
  const totalDisplay = (value: unknown) =>
    value === null || value === undefined ? "скрыто" : formatMoney(value as string | number);

  return (
    <section className={`executive-block executive-block--management-balance executive-block--${block.source_status}`}>
      <header className="executive-block__header">
        <div>
          <h2>{block.title}</h2>
          {block.as_of && <span>на {formatDate(block.as_of)}</span>}
        </div>
        <em>{statusLabel(block.source_status)}</em>
      </header>
      {blockUsesPlaceholder(block) ? (
        <div className="executive-block__placeholder">
          <strong>{sourceAnchor || statusLabel(block.source_status)}</strong>
          <span>{note || "Источник пока не подключен к витрине."}</span>
        </div>
      ) : (
        <div className="executive-management-balance" aria-label="Управленческий баланс">
          <section className="executive-management-balance__column executive-management-balance__column--assets">
            <h3>Активы</h3>
            <div className="executive-management-balance__rows">
              {assets.map((line) => (
                <div className="executive-management-balance__row" key={line.key || line.label}>
                  <span>
                    {line.label || "Статья"}
                    {!!line.estimated_count && <small>Оценочно, без закрывающих документов</small>}
                  </span>
                  <strong>{managementBalanceAmount(line)}</strong>
                </div>
              ))}
            </div>
            <footer>
              <span>{assetsTotalLabel}</span>
              <strong>{totalDisplay(assetsTotal)}</strong>
            </footer>
          </section>
          <section className="executive-management-balance__column executive-management-balance__column--liabilities">
            <h3>Пассивы</h3>
            <h4 className="executive-management-balance__subsection-title">Обязательства</h4>
            <div className="executive-management-balance__rows">
              {liabilities.map((line) => (
                <div className="executive-management-balance__row" key={line.key || line.label}>
                  <span>
                    {line.label || "Статья"}
                    {!!line.estimated_count && <small>Оценочно, без закрывающих документов</small>}
                  </span>
                  <strong>{managementBalanceAmount(line)}</strong>
                </div>
              ))}
            </div>
            <h4 className="executive-management-balance__subsection-title executive-management-balance__equity-title">
              Собственные средства
            </h4>
            <div className="executive-management-balance__rows">
              {equity.map((line) => (
                <div className="executive-management-balance__row" key={line.key || line.label}>
                  <span>
                    {line.label || "Статья"}
                    {line.note && <small>{line.note}</small>}
                  </span>
                  <strong>{managementBalanceAmount(line)}</strong>
                </div>
              ))}
            </div>
            <footer>
              <span>{liabilitiesTotalLabel}</span>
              <strong>{totalDisplay(liabilitiesTotal)}</strong>
            </footer>
          </section>
        </div>
      )}
      {(sourceAnchor || note || drilldownHref) && (
        <footer className="executive-block__footer">
          {!blockUsesPlaceholder(block) && sourceAnchor && <strong>{sourceAnchor}</strong>}
          {!blockUsesPlaceholder(block) && note && <span>{note}</span>}
          {drilldownHref && (
            <a className="executive-block__drilldown" href={drilldownHref}>
              Открыть источник
            </a>
          )}
        </footer>
      )}
    </section>
  );
}

function formatBalanceMonth(value: string) {
  const [year, month] = value.split("-").map(Number);
  if (!year || !month) return value;
  return new Intl.DateTimeFormat("ru-RU", { month: "long", year: "numeric" }).format(
    new Date(year, month - 1, 1)
  );
}

function MonthlyBalanceRows({
  lines,
}: {
  lines: ExecutiveManagementBalanceLineItem[];
}) {
  return (
    <div className="executive-management-balance__rows">
      {lines.map((line) => (
        <div className="executive-management-balance__row executive-management-balance__row--monthly" key={line.key}>
          <span>
            {line.label}
            <small>
              {line.note || statusLabel(line.source_status)}
              {line.source_as_of ? ` · на ${formatDate(line.source_as_of)}` : ""}
              {line.delta_previous !== null && line.delta_previous !== undefined
                ? ` · к прошлому месяцу ${formatSignedMoney(line.delta_previous)}`
                : ""}
              {line.estimated_count > 0
                ? ` · оценочно без документов: ${formatPlainNumber(line.estimated_count)}`
                : ""}
            </small>
          </span>
          <strong>
            {line.amount === null || line.amount === undefined
              ? "Источник не подтверждён"
              : formatMoney(line.amount)}
          </strong>
        </div>
      ))}
    </div>
  );
}

function formatSignedMoney(value: string | number) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  const formatted = formatMoney(Math.abs(numeric));
  return `${numeric > 0 ? "+" : numeric < 0 ? "−" : ""}${formatted}`;
}

function formatTurnoverMoney(value?: string | number | null) {
  return value === null || value === undefined ? "—" : formatMoney(value);
}

function turnoverSourceLabel(sourceKey: string) {
  if (sourceKey === "onec_bp_tax_accounting") return "БП · начисленные налоги";
  if (sourceKey.startsWith("management_")) return "Управленческий расчёт";
  return "УТ 10.3";
}

function ManagementBalanceTurnoverTable({
  turnover,
}: {
  turnover: ExecutiveManagementBalanceTurnoverResponse;
}) {
  const sectionLabels = {
    asset: "Активы",
    liability: "Обязательства",
    equity: "Собственные средства",
  } as const;

  return (
    <section
      className="executive-management-balance__turnover"
      aria-label="Оборотно-сальдовая ведомость по статьям баланса"
    >
      <header>
        <div>
          <h3>Оборотно-сальдовая ведомость</h3>
          <span>
            с {formatDate(turnover.date_from)} по {formatDate(turnover.date_to)}
          </span>
        </div>
        <small>УТ 10.3 · из БП только начисленные налоги</small>
      </header>
      <div className="executive-management-balance__turnover-note" role="note">
        <strong>Сверочная версия</strong>
        <span>{turnover.note}</span>
      </div>
      {turnover.opening_status !== "closed" && (
        <div className="executive-management-balance__turnover-note" role="note">
          <strong>Начальный баланс — рабочая база</strong>
          <span>
            Версия {turnover.opening_version} подтверждена для сверки, но содержит{" "}
            {turnover.opening_validation_error_count} контрольных блокера. Начальные суммы можно
            уточнить позднее.
          </span>
        </div>
      )}
      {(Number(turnover.opening_scope_imbalance_amount) !== 0 ||
        Number(turnover.closing_scope_imbalance_amount) !== 0) && (
        <div className="executive-management-balance__turnover-note" role="alert">
          <strong>Итоги ограниченного контура не равны</strong>
          <span>
            Контроль на начало: {formatSignedMoney(turnover.opening_scope_imbalance_amount)};
            на конец: {formatSignedMoney(turnover.closing_scope_imbalance_amount)}. Расхождение
            остаётся видимым, потому что статьи БП, кроме начисленных налогов, не включаются.
          </span>
        </div>
      )}
      <div className="executive-management-balance__turnover-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Статья баланса</th>
              <th scope="col">Сальдо начальное</th>
              <th scope="col">Дебет</th>
              <th scope="col">Кредит</th>
              <th scope="col">Сальдо конечное</th>
              <th scope="col">Контроль</th>
            </tr>
          </thead>
          <tbody>
            {(["asset", "liability", "equity"] as const).map((section) => {
              const lines = turnover.lines.filter((line) => line.section === section);
              const total = turnover.totals.find((item) => item.section === section);
              return [
                <tr className="executive-management-balance__turnover-section" key={`${section}-title`}>
                  <th colSpan={6} scope="rowgroup">{sectionLabels[section]}</th>
                </tr>,
                ...lines.map((line) => (
                  <tr key={`${section}-${line.key}`}>
                    <th scope="row">
                      <span>{line.label}</span>
                      <small>
                        {turnoverSourceLabel(line.source_key)} · {line.source_status}
                      </small>
                      {line.note && <small>{line.note}</small>}
                    </th>
                    <td>{formatTurnoverMoney(line.opening_balance)}</td>
                    <td>{formatTurnoverMoney(line.debit_turnover)}</td>
                    <td>{formatTurnoverMoney(line.credit_turnover)}</td>
                    <td>{formatTurnoverMoney(line.closing_balance)}</td>
                    <td className={Number(line.reconciliation_difference || 0) === 0 ? "is-ok" : "is-error"}>
                      {formatTurnoverMoney(line.reconciliation_difference)}
                    </td>
                  </tr>
                )),
                total ? (
                  <tr className="executive-management-balance__turnover-total" key={`${section}-total`}>
                    <th scope="row">{total.label}</th>
                    <td>{formatMoney(total.opening_balance)}</td>
                    <td>{formatMoney(total.debit_turnover)}</td>
                    <td>{formatMoney(total.credit_turnover)}</td>
                    <td>{formatMoney(total.closing_balance)}</td>
                    <td className={Number(total.reconciliation_difference) === 0 ? "is-ok" : "is-error"}>
                      {formatMoney(total.reconciliation_difference)}
                    </td>
                  </tr>
                ) : null,
              ];
            })}
          </tbody>
        </table>
      </div>
      {turnover.excluded_lines.length > 0 && (
        <footer>
          Не включено строк БП: {turnover.excluded_lines.length}. Они исключены по принятой
          методике; из БП в ОСВ используется только задолженность по начисленным налогам.
        </footer>
      )}
    </section>
  );
}

export function MonthlyManagementBalance({
  refreshNonce,
  canCloseMonth,
}: {
  refreshNonce: number;
  canCloseMonth: boolean;
}) {
  const [balance, setBalance] = useState<ExecutiveManagementBalanceResponse | null>(null);
  const [turnover, setTurnover] = useState<ExecutiveManagementBalanceTurnoverResponse | null>(null);
  const [month, setMonth] = useState<string | undefined>();
  const [view, setView] = useState<ExecutiveManagementBalanceView | undefined>();
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [turnoverMessage, setTurnoverMessage] = useState("");

  const load = useCallback(async (nextMonth?: string, nextView?: ExecutiveManagementBalanceView) => {
    setMonth(nextMonth);
    setView(nextView);
    setLoading(true);
    setMessage("");
    setTurnoverMessage("");
    setBalance(null);
    setTurnover(null);
    try {
      const payload = await fetchExecutiveManagementBalance({ month: nextMonth, view: nextView });
      setBalance(payload);
      setMonth(payload.month);
      setView(payload.view);
      try {
        setTurnover(
          await fetchExecutiveManagementBalanceTurnover({
            month: payload.month,
            view: payload.view,
          })
        );
      } catch (error: unknown) {
        setTurnoverMessage(errorMessage(error));
      }
    } catch (error: unknown) {
      if (nextView === "closed") {
        try {
          const fallback = await fetchExecutiveManagementBalance({
            month: nextMonth,
            view: "operational",
          });
          setBalance(fallback);
          setMonth(fallback.month);
          setView(fallback.view);
          setMessage("Закрытая версия отсутствует. Показан доступный оперативный срез.");
          try {
            setTurnover(
              await fetchExecutiveManagementBalanceTurnover({
                month: fallback.month,
                view: fallback.view,
              })
            );
          } catch (turnoverError: unknown) {
            setTurnoverMessage(errorMessage(turnoverError));
          }
          return;
        } catch {
          // No operational history exists for this month either.
        }
      }
      setMessage(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.resolve().then(() => {
      if (!cancelled) load();
    });
    return () => {
      cancelled = true;
    };
  }, [load, refreshNonce]);

  const chooseMonth = (nextMonth: string) => {
    load(nextMonth, view || "operational");
  };
  const chooseView = (nextView: ExecutiveManagementBalanceView) => {
    load(month, nextView);
  };
  const closeMonth = () => {
    if (!balance || !window.confirm(`Закрыть управленческий баланс за ${formatBalanceMonth(balance.month)}?`)) {
      return;
    }
    setLoading(true);
    closeExecutiveManagementBalance(balance.month)
      .then((payload) => {
        setBalance(payload);
        setView(payload.view);
        setMessage("");
      })
      .catch((error: unknown) => setMessage(errorMessage(error)))
      .finally(() => setLoading(false));
  };
  const salaryReconciliationValue = balance?.source_summary?.salary_reconciliation;
  const salaryReconciliation =
    salaryReconciliationValue &&
    typeof salaryReconciliationValue === "object" &&
    !Array.isArray(salaryReconciliationValue)
      ? (salaryReconciliationValue as Record<string, unknown>)
      : null;
  const salaryMappingValue = salaryReconciliation?.mapping;
  const salaryMapping =
    salaryMappingValue && typeof salaryMappingValue === "object" && !Array.isArray(salaryMappingValue)
      ? (salaryMappingValue as Record<string, unknown>)
      : null;
  const unconfirmedSalaryAmount = Number(salaryReconciliation?.unconfirmed_amount || 0);
  const salaryMappingCoverage = Number(salaryMapping?.coverage_percent || 0);
  const showSalaryReconciliationWarning = Boolean(
    salaryReconciliation &&
      (salaryReconciliation.status !== "ready" ||
        salaryReconciliation.closing_blocked ||
        unconfirmedSalaryAmount > 0)
  );
  const openingEquityValue = balance?.source_summary?.opening_equity;
  const openingEquity =
    openingEquityValue &&
    typeof openingEquityValue === "object" &&
    !Array.isArray(openingEquityValue)
      ? (openingEquityValue as Record<string, unknown>)
      : null;
  const openingBridgeValue = openingEquity?.bridge;
  const openingBridge =
    openingBridgeValue &&
    typeof openingBridgeValue === "object" &&
    !Array.isArray(openingBridgeValue)
      ? (openingBridgeValue as Record<string, unknown>)
      : null;
  const openingBridgeRows = [
    ["retained_earnings", "Входящий капитал"],
    ["prior_period_adjustments", "Корректировки прошлых периодов"],
    ["owner_capital", "Уставный и добавочный капитал"],
    ["owner_contributed_funds", "Средства, внесённые собственниками"],
    ["current_period_result", "Чистая прибыль текущего года"],
    ["dividends_paid_ytd", "Минус выплаченные дивиденды"],
  ].filter(([key]) => openingBridge?.[key] !== undefined);

  return (
    <section className={`executive-block executive-block--management-balance executive-block--${balance?.source_status || "source_missing"}`}>
      <header className="executive-block__header executive-management-balance__header executive-panel__header">
        <div>
          <h2>{balance?.status === "closed" ? "Полный управленческий баланс" : "Частичный управленческий баланс"}</h2>
          <span>
            {balance
              ? balance.status === "closed"
                ? `Закрыт на ${formatDate(balance.balance_date)} · версия ${balance.version}`
                : `Не закрыт, данные на ${formatDate(balance.balance_date)} · версия ${balance.version}`
              : loading
                ? "Загрузка помесячного среза"
                : month
                  ? `Нет доступного снимка за ${formatBalanceMonth(month)}`
                  : "Период не выбран"}
          </span>
        </div>
        <div className="executive-management-balance__controls executive-panel__filters">
          <label>
            <span>Месяц</span>
            <select
              aria-label="Месяц управленческого баланса"
              disabled={loading}
              onChange={(event) => chooseMonth(event.target.value)}
              value={month || ""}
            >
              {(balance?.available_months || (month ? [month] : [])).map((item) => (
                <option key={item} value={item}>{formatBalanceMonth(item)}</option>
              ))}
            </select>
          </label>
          <div className="executive-management-balance__view" aria-label="Режим баланса">
            <button
              aria-pressed={view === "closed"}
              className={view === "closed" ? "is-active" : ""}
              disabled={loading}
              onClick={() => chooseView("closed")}
              type="button"
            >
              Закрытый месяц
            </button>
            <button
              aria-pressed={view === "operational"}
              className={view === "operational" ? "is-active" : ""}
              disabled={loading}
              onClick={() => chooseView("operational")}
              type="button"
            >
              Оперативный на сегодня
            </button>
          </div>
        </div>
      </header>

      {message && (
        <div className="executive-management-balance__warning" role="alert">
          <strong>{balance ? "Закрытая версия отсутствует" : "Срез недоступен"}</strong>
          <span>{message}</span>
        </div>
      )}
      {loading && !balance && <LoadingState title="Загрузка управленческого баланса..." />}
      {balance && (
        <>
          <div className="executive-management-balance__totals">
            <MetricCard
              label="Активы"
              tooltip="Сумма активов на дату баланса."
              value={formatMoney(balance.assets_total)}
            />
            <MetricCard
              label="Пассивы"
              tooltip="Обязательства и собственный капитал на дату баланса."
              value={formatMoney(balance.liabilities_and_equity_total)}
            />
            <MetricCard
              label="Расхождение"
              tone={Number(balance.imbalance_amount) === 0 ? "success" : "danger"}
              tooltip="Активы минус пассивы; ноль — баланс сходится."
              value={formatSignedMoney(balance.imbalance_amount)}
            />
          </div>
          {showSalaryReconciliationWarning && (
            <div className="executive-management-balance__warning" role="status">
              <strong>Сверка зарплаты выполнена частично</strong>
              <span>
                Неподтверждено: {formatMoney(unconfirmedSalaryAmount)} — в итог баланса не включено.
                Сопоставлено сотрудников: {formatPlainNumber(salaryMappingCoverage)}%.
              </span>
            </div>
          )}
          {openingEquity && openingBridge && (
            <section
              className="executive-management-balance__equity-bridge"
              aria-label="Мост собственного капитала"
            >
              <header>
                <div>
                  <h3>Мост собственного капитала</h3>
                  <span>
                    Рассчитано автоматически на{" "}
                    {formatDate(String(openingEquity.baseline_date || "2026-01-01"))}
                  </span>
                </div>
                <small>
                  версия {String(openingEquity.version || "—")} ·{" "}
                  {String(openingEquity.source_hash || "").slice(0, 12)}
                </small>
              </header>
              <div>
                {openingBridgeRows.map(([key, label]) => (
                  <p key={key}>
                    <span>{label}</span>
                    <strong>{formatSignedMoney(openingBridge[key] as string | number)}</strong>
                  </p>
                ))}
                <p className="executive-management-balance__equity-bridge-total">
                  <span>Итого собственный капитал по мосту</span>
                  <strong>
                    {formatMoney(
                      openingBridge.equity_bridge_total as string | number
                    )}
                  </strong>
                </p>
              </div>
            </section>
          )}
          {Number(balance.imbalance_amount) !== 0 && (
            <div className="executive-management-balance__warning" role="status">
              <strong>Стороны баланса не равны</strong>
              <span>Закрытие заблокировано до сверки источников; балансирующая статья не создаётся.</span>
            </div>
          )}
          <div className="executive-management-balance" aria-label="Помесячный управленческий баланс">
            <section className="executive-management-balance__column executive-management-balance__column--assets">
              <h3>Активы</h3>
              <MonthlyBalanceRows lines={balance.assets} />
              <footer><span>Итого активы</span><strong>{formatMoney(balance.assets_total)}</strong></footer>
            </section>
            <section className="executive-management-balance__column executive-management-balance__column--liabilities">
              <h3>Пассивы</h3>
              <h4 className="executive-management-balance__subsection-title">Обязательства</h4>
              <MonthlyBalanceRows lines={balance.liabilities} />
              <footer><span>Итого обязательства</span><strong>{formatMoney(balance.liabilities_total)}</strong></footer>
              <h4 className="executive-management-balance__subsection-title executive-management-balance__equity-title">
                Собственные средства
              </h4>
              <MonthlyBalanceRows lines={balance.equity} />
              <footer><span>Итого собственные средства</span><strong>{formatMoney(balance.equity_total)}</strong></footer>
            </section>
          </div>
          {turnover && <ManagementBalanceTurnoverTable turnover={turnover} />}
          {turnoverMessage && (
            <div className="executive-management-balance__warning" role="status">
              <strong>ОСВ пока недоступна</strong>
              <span>{turnoverMessage}</span>
            </div>
          )}
          <footer className="executive-block__footer executive-management-balance__footer">
            <span>{balance.note}</span>
            {balance.validation_errors.length > 0 && (
              <span>Закрытие заблокировано: {balance.validation_errors.length} контрольных ошибок.</span>
            )}
            {canCloseMonth && balance.can_close && (
              <Button disabled={loading} onClick={closeMonth}>Закрыть месяц</Button>
            )}
          </footer>
        </>
      )}
    </section>
  );
}

function ReconciliationBlockCard({
  block,
  drilldownHref,
}: {
  block: ExecutiveDashboardBlock;
  drilldownHref?: string | null;
}) {
  const note = typeof block.summary.note === "string" ? block.summary.note : null;
  const issueBreakdown = summaryArray<ReconciliationIssueBreakdown>(block.summary, "issue_breakdown");
  const issueExamples = summaryArray<ReconciliationIssueExample>(block.summary, "issue_examples").slice(0, 3);
  const ddsExamples = summaryArray<Record<string, unknown>>(block.summary, "dds_issue_examples").slice(0, 2);
  const delivery = reportDelivery(block.summary);
  const taskIds = Array.isArray(delivery.task_ids) ? delivery.task_ids.filter(Boolean) : [];
  const visibleMetrics = visibleMetricsForBlock(block);
  return (
    <section className={`executive-block executive-block--reconciliation executive-block--${block.source_status}`}>
      <header className="executive-block__header">
        <div>
          <h2>{block.title}</h2>
          {block.as_of && <span>на {formatDate(block.as_of)}</span>}
        </div>
        <em>{statusLabel(block.source_status)}</em>
      </header>
      <div className="executive-block__metrics">
        {visibleMetrics.map((metric) => (
          <div className={`executive-metric executive-metric--${metric.tone}`} key={metric.key}>
            <span>{metric.label}</span>
            <strong>{metricDisplay(metric)}</strong>
          </div>
        ))}
      </div>
      {(issueBreakdown.length > 0 || taskIds.length > 0) && (
        <div className="executive-reconciliation-summary">
          {issueBreakdown.length > 0 && (
            <div className="executive-reconciliation-chips" aria-label="Типы расхождений">
              {issueBreakdown.map((item) => (
                <span key={item.issue_type || item.label}>
                  {item.label || item.issue_type || "Расхождение"}: <strong>{item.count || 0}</strong>
                </span>
              ))}
            </div>
          )}
          {taskIds.length > 0 && (
            <div className="executive-reconciliation-report">
              <span>Отчет уже отправлен</span>
              <strong>задача {taskIds.join(", ")}</strong>
            </div>
          )}
        </div>
      )}
      {issueExamples.length > 0 && (
        <div className="executive-reconciliation-list" aria-label="Примеры расхождений">
          {issueExamples.map((issue, index) => (
            <div className="executive-reconciliation-item" key={issue.issue_key || `${issue.issue_type}-${index}`}>
              <strong>{issue.issue_type_label || issue.issue_type || "Расхождение"}</strong>
              <span>
                {[issue.department, issue.operation_date].filter(Boolean).join(" · ")}
                {issue.amount_delta !== undefined && issue.amount_delta !== null
                  ? ` · дельта ${formatMoney(issue.amount_delta)}`
                  : ""}
              </span>
              {issue.proposed_action && <small>{issue.proposed_action}</small>}
            </div>
          ))}
        </div>
      )}
      {ddsExamples.length > 0 && (
        <div className="executive-reconciliation-list executive-reconciliation-list--dds" aria-label="Ошибки ДДС">
          {ddsExamples.map((issue, index) => (
            <div className="executive-reconciliation-item" key={String(issue.issue_key || index)}>
              <strong>{String(issue.description || issue.issue_type || "ДДС на проверку")}</strong>
              {typeof issue.proposed_action === "string" && issue.proposed_action.trim() && (
                <small>{issue.proposed_action}</small>
              )}
            </div>
          ))}
        </div>
      )}
      {(note || drilldownHref) && (
        <footer className="executive-block__footer">
          {note && <span>{note}</span>}
          {drilldownHref && (
            <a className="executive-block__drilldown" href={drilldownHref}>
              Открыть отчет сверки
            </a>
          )}
        </footer>
      )}
    </section>
  );
}

function SourceFreshness({ sources }: { sources: ExecutiveSourceStatus[] }) {
  const [expanded, setExpanded] = useState(false);
  const issueSources = sources.filter((source) => source.source_status !== "ready");
  const summaryText =
    sources.length === 0
      ? "Источники пока не переданы"
      : issueSources.length === 0
      ? `Все источники готовы: ${sources.length}`
      : `Проблемных источников: ${issueSources.length} из ${sources.length}`;
  return (
    <section className="executive-sources-panel">
      <button
        aria-expanded={expanded}
        aria-controls={sources.length > 0 ? "executive-source-details" : undefined}
        className="executive-sources-panel__summary"
        disabled={sources.length === 0}
        onClick={() => setExpanded((value) => !value)}
        type="button"
      >
        <span>Источники данных</span>
        <strong>{summaryText}</strong>
        <em>{sources.length === 0 ? "Нет деталей" : expanded ? "Свернуть" : "Показать детали"}</em>
      </button>
      {expanded && (
        <div className="executive-sources" id="executive-source-details">
          {sources.map((source) => (
            <div className={`executive-source executive-source--${source.source_status}`} key={source.source_key}>
              <strong>{source.title}</strong>
              <span>{statusLabel(source.source_status)}</span>
              {source.as_of && <small>{formatDateTime(source.as_of)}</small>}
              {source.note && <small>{source.note}</small>}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function cashflowTotal(data: ExecutiveCashflowPeriodResponse | null, key: string) {
  const value = data?.totals?.[key];
  return value === null || value === undefined ? null : value;
}

function ratioByKey(data: ExecutiveCashflowPeriodResponse | null, key: string): ExecutiveCashflowRatio | null {
  return data?.ratios.find((ratio) => ratio.key === key) || null;
}

function profitLossTotal(data: ExecutiveProfitLossPeriodResponse | null, key: string) {
  const value = data?.totals?.[key];
  return value === null || value === undefined ? null : value;
}

function profitLossRatioByKey(
  data: ExecutiveProfitLossPeriodResponse | null,
  key: string
): ExecutiveProfitLossRatio | null {
  return data?.ratios.find((ratio) => ratio.key === key) || null;
}

function ProfitLossExpenseBreakdown({ data }: { data: ExecutiveProfitLossPeriodResponse }) {
  const visibleRows = data.expense_breakdown.slice(0, 8);
  if (visibleRows.length === 0) {
    return (
      <div className="executive-cashflow-period__empty">
        Нет подтвержденных операционных расходов по ДДС.
      </div>
    );
  }
  return (
    <div className="executive-profit-loss-drilldown__grid">
      {visibleRows.map((row) => (
        <div className="executive-cashflow-row" key={row.key}>
          <span>{row.label}</span>
          <strong>{formatMoney(row.amount)}</strong>
          <small>{profitLossExpenseDetail(row)}</small>
        </div>
      ))}
    </div>
  );
}

const PROFIT_LOSS_FORMULA_LINES: Record<string, string[]> = {
  revenue: ["gross_revenue", "customer_refunds"],
  gross_profit: ["revenue", "cost_of_sales"],
  operating_profit: ["gross_profit", "operating_expenses", "operating_taxes", "inventory_loss"],
  other_income_expenses: ["debt_adjustment_income", "debt_adjustment_expense"],
  profit_before_tax: ["operating_profit", "other_income_expenses"],
  net_profit: ["profit_before_tax", "taxes"],
};

function ProfitLossFormulaBreakdown({
  data,
  keys,
}: {
  data: ExecutiveProfitLossPeriodResponse;
  keys: string[];
}) {
  const lines = keys
    .map((key) => data.lines.find((line) => line.key === key))
    .filter((line): line is ExecutiveProfitLossLineItem => Boolean(line));
  return (
    <div className="executive-profit-loss-drilldown__grid">
      {lines.map((line) => (
        <div className="executive-cashflow-row" key={line.key}>
          <span>{line.label}</span>
          <strong>{formatProfitLossAmount(line.amount)}</strong>
          <small>{line.note || statusLabel(line.source_status)}</small>
        </div>
      ))}
    </div>
  );
}

function ProfitLossSalesBreakdown({
  data,
  metric,
}: {
  data: ExecutiveProfitLossPeriodResponse;
  metric: "revenue" | "cost_of_sales" | "gross_profit";
}) {
  const renderRows = (rows: ExecutiveProfitLossBreakdownRow[]) =>
    rows.slice(0, 6).map((row) => (
      <div className="executive-cashflow-row" key={row.key}>
        <span>{row.label}</span>
        <strong>{formatMoney(row[metric])}</strong>
        <small>{profitLossRowDetail(row)}</small>
      </div>
    ));
  return (
    <div className="executive-profit-loss-drilldown__columns">
      <section>
        <h4>По магазинам</h4>
        {data.by_store.length > 0 ? renderRows(data.by_store) : <div className="executive-cashflow-period__empty">Нет данных.</div>}
      </section>
      <section>
        <h4>По менеджерам</h4>
        {data.by_manager.length > 0 ? renderRows(data.by_manager) : <div className="executive-cashflow-period__empty">Нет данных.</div>}
      </section>
    </div>
  );
}

function ProfitLossLineDrilldown({
  data,
  line,
}: {
  data: ExecutiveProfitLossPeriodResponse;
  line: ExecutiveProfitLossLineItem;
}) {
  if (line.key === "gross_revenue") {
    return <ProfitLossSalesBreakdown data={data} metric="revenue" />;
  }
  if (line.key === "cost_of_sales") {
    return <ProfitLossSalesBreakdown data={data} metric="cost_of_sales" />;
  }
  if (line.key === "operating_expenses") {
    return <ProfitLossExpenseBreakdown data={data} />;
  }
  if (line.key === "inventory_loss") {
    return <InventoryLossPanel data={data.inventory_loss} embedded />;
  }
  const formulaKeys = PROFIT_LOSS_FORMULA_LINES[line.key];
  return formulaKeys ? <ProfitLossFormulaBreakdown data={data} keys={formulaKeys} /> : null;
}

function profitLossLineHasDrilldown(data: ExecutiveProfitLossPeriodResponse, lineKey: string) {
  if (["gross_revenue", "cost_of_sales"].includes(lineKey)) {
    return data.by_store.length > 0 || data.by_manager.length > 0;
  }
  if (lineKey === "operating_expenses") return true;
  if (lineKey === "inventory_loss") return Boolean(data.inventory_loss);
  return Boolean(PROFIT_LOSS_FORMULA_LINES[lineKey]);
}

function ProfitLossStatementRow({
  data,
  line,
}: {
  data: ExecutiveProfitLossPeriodResponse;
  line: ExecutiveProfitLossLineItem;
}) {
  const className = [
    "executive-profit-loss-line",
    `executive-profit-loss-line--${line.source_status}`,
    `executive-profit-loss-line--${line.line_type}`,
  ]
    .filter(Boolean)
    .join(" ");
  const supportingText = line.note || (line.source_status === "ready" ? "" : statusLabel(line.source_status));
  if (!profitLossLineHasDrilldown(data, line.key)) {
    return (
      <div className={className}>
        <span>{line.label}</span>
        <strong>{formatProfitLossAmount(line.amount)}</strong>
        <span aria-hidden="true" className="executive-profit-loss-line__action-placeholder" />
        {supportingText && <small>{supportingText}</small>}
      </div>
    );
  }
  return (
    <details className={`${className} executive-profit-loss-line--expandable`}>
      <summary>
        <span>{line.label}</span>
        <strong>{formatProfitLossAmount(line.amount)}</strong>
        {supportingText && <small>{supportingText}</small>}
        <span className="executive-profit-loss-line__toggle">
          <span className="is-closed">Расшифровать</span>
          <span className="is-open">Свернуть</span>
        </span>
      </summary>
      <div className="executive-profit-loss-line__drilldown">
        <ProfitLossLineDrilldown data={data} line={line} />
      </div>
    </details>
  );
}

function numericValue(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function cashPositionBoundary(
  data: ExecutiveCashflowPeriodResponse,
  boundary: "opening" | "closing"
) {
  const value = data.cash_position?.[boundary];
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function cashPositionBoundaryBalance(
  data: ExecutiveCashflowPeriodResponse,
  boundary: "opening" | "closing"
) {
  const row = cashPositionBoundary(data, boundary);
  if (!row) return null;
  return numericValue(recordValue(row, "total_balance") || recordValue(row, "total_balance_rub"));
}

function cashPositionBoundaryDate(
  data: ExecutiveCashflowPeriodResponse,
  boundary: "opening" | "closing"
) {
  const row = cashPositionBoundary(data, boundary);
  const value = row?.snapshot_date;
  return typeof value === "string" && value ? value : null;
}

function cashflowGroupNet(data: ExecutiveCashflowPeriodResponse, groupKey: string) {
  const row = data.by_group.find((item) => {
    const metaGroup = item.meta.dds_group;
    return item.key === groupKey || metaGroup === groupKey;
  });
  return numericValue(row?.net_amount) || 0;
}

function formatNullableMoney(value: number | null) {
  return value === null ? "нет данных" : formatMoney(value);
}

function formatProfitLossAmount(value: string | number | null | undefined) {
  return value === null || value === undefined ? "не подключено" : formatMoney(value);
}

function isReceiptSurplus(value: string | number | null | undefined) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed < 0;
}

function formatInventoryLossMagnitude(value: string | number | null | undefined) {
  if (!isReceiptSurplus(value)) return formatProfitLossAmount(value);
  return formatMoney(Math.abs(Number(value)));
}

function formatInventoryLossResult(value: string | number | null | undefined) {
  if (!isReceiptSurplus(value)) return formatProfitLossAmount(value);
  return `Превышение оприходований: ${formatMoney(Math.abs(Number(value)))}`;
}

function inventoryStoreKey(store: { store_ref: string; store_name: string }) {
  return store.store_ref || `name:${store.store_name}`;
}

function profitLossRowDetail(row: ExecutiveProfitLossBreakdownRow) {
  const margin = row.gross_margin_pct === null || row.gross_margin_pct === undefined
    ? "маржа: нет данных"
    : `маржа: ${formatPercent(row.gross_margin_pct)}`;
  return `${formatPlainNumber(row.sales_count)} продаж, ${margin}`;
}

function profitLossExpenseDetail(row: ExecutiveProfitLossExpenseBreakdownRow) {
  const review = row.review_count > 0 ? `, на проверку: ${formatPlainNumber(row.review_count)}` : "";
  const method = row.recognition_method === "accrual" ? "по начислению" : "по оплате";
  const estimated = row.estimated_count > 0
    ? `, оценочно без документов: ${formatPlainNumber(row.estimated_count)}`
    : "";
  return `${method}, ${formatPlainNumber(row.movement_count)} оплат${review}${estimated}`;
}

function profitLossQuestionDetail(row: ExecutiveProfitLossOpenQuestion) {
  const action = row.proposed_action ? ` ${row.proposed_action}` : "";
  return `${row.reason}${action}`;
}

function CashflowStatement({
  data,
  includeInternal,
}: {
  data: ExecutiveCashflowPeriodResponse;
  includeInternal: boolean;
}) {
  const openingBalance = cashPositionBoundaryBalance(data, "opening");
  const closingBalance = cashPositionBoundaryBalance(data, "closing");
  const openingDate = cashPositionBoundaryDate(data, "opening");
  const closingDate = cashPositionBoundaryDate(data, "closing");
  const operatingNet = cashflowGroupNet(data, "operating");
  const investingNet = cashflowGroupNet(data, "investing");
  const financingNet = cashflowGroupNet(data, "financing");
  const internalNet = cashflowGroupNet(data, "internal");
  const technicalNet = cashflowGroupNet(data, "technical") + cashflowGroupNet(data, "unclassified");
  const externalNet = numericValue(cashflowTotal(data, "external_net_amount")) ?? operatingNet + investingNet + financingNet + technicalNet;
  const freeCashflow = operatingNet + investingNet;
  const registerChange =
    openingBalance === null || closingBalance === null ? null : closingBalance - openingBalance;
  const movementForControl = externalNet + (includeInternal ? internalNet : 0);
  const controlDelta =
    registerChange === null || !includeInternal ? null : registerChange - movementForControl;
  const hasControlWarning = controlDelta !== null && Math.abs(controlDelta) >= 1;

  const rows = [
    {
      key: "opening",
      label: "Остаток денег на начало",
      value: openingBalance,
      detail: openingDate ? `снимок на ${formatDate(openingDate)}` : "нет снимка на границу периода",
      tone: "reference",
    },
    {
      key: "operating",
      label: "CFO: операционная деятельность",
      value: operatingNet,
      detail: "основные поступления и списания",
      tone: "default",
    },
    {
      key: "investing",
      label: "CFI: инвестиционная деятельность",
      value: investingNet,
      detail: "оборудование, развитие, долгие активы",
      tone: "default",
    },
    {
      key: "free_cashflow",
      label: "Free Cash Flow",
      value: freeCashflow,
      detail: "CFO + CFI",
      tone: "total",
    },
    {
      key: "financing",
      label: "CFF: финансовая деятельность",
      value: financingNet,
      detail: "кредиты, займы, собственники",
      tone: "default",
    },
    ...(technicalNet
      ? [
          {
            key: "technical",
            label: "Технические / неразнесенные",
            value: technicalNet,
            detail: "строки без управленческой классификации",
            tone: "warning",
          },
        ]
      : []),
    {
      key: "external_net",
      label: "Чистый денежный поток по ОДДС",
      value: externalNet,
      detail: "без внутренних перемещений",
      tone: "total",
    },
    {
      key: "internal",
      label: "Внутренние перемещения, справочно",
      value: includeInternal ? internalNet : null,
      detail: includeInternal ? "между своими счетами и кассами" : "скрыты фильтром",
      tone: "reference",
    },
    {
      key: "register_change",
      label: "Изменение остатка по регистру",
      value: registerChange,
      detail: "остаток на конец минус остаток на начало",
      tone: "reference",
    },
    {
      key: "closing",
      label: "Остаток денег на конец",
      value: closingBalance,
      detail: closingDate ? `снимок на ${formatDate(closingDate)}` : "нет снимка на границу периода",
      tone: "reference",
    },
    {
      key: "control",
      label: "Контроль ОДДС",
      value: controlDelta,
      detail: includeInternal
        ? "изменение остатка минус движение"
        : "включите внутренние переводы для полного контроля",
      tone: hasControlWarning ? "warning" : "reference",
    },
  ];

  return (
    <section className="executive-cashflow-statement" aria-label="Форма ОДДС CashFlow">
      <header>
        <div>
          <h3>Форма ОДДС CashFlow</h3>
          <span>
            {formatDate(data.date_from)} - {formatDate(data.date_to)}
          </span>
        </div>
      </header>
      <div className="executive-cashflow-statement__rows">
        {rows.map((row) => (
          <div
            className={[
              "executive-cashflow-statement__row",
              row.tone === "total" ? "executive-cashflow-statement__row--total" : "",
              row.tone === "warning" ? "executive-cashflow-statement__row--warning" : "",
              row.tone === "reference" ? "executive-cashflow-statement__row--reference" : "",
            ]
              .filter(Boolean)
              .join(" ")}
            key={row.key}
          >
            <span>{row.label}</span>
            <strong>{formatNullableMoney(row.value)}</strong>
            <small>{row.detail}</small>
          </div>
        ))}
      </div>
    </section>
  );
}

function CashflowPeriodPanel({ asOf }: { asOf: string }) {
  const [dateFrom, setDateFrom] = useState(monthStartIso(asOf));
  const [dateTo, setDateTo] = useState(asOf);
  const [includeInternal, setIncludeInternal] = useState(true);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [message, setMessage] = useState("");
  const [data, setData] = useState<ExecutiveCashflowPeriodResponse | null>(null);

  useEffect(() => {
    setDateFrom(monthStartIso(asOf));
    setDateTo(asOf);
  }, [asOf]);

  useEffect(() => {
    let cancelled = false;
    setStatus("loading");
    fetchExecutiveCashflowPeriod({
      date_from: dateFrom,
      date_to: dateTo,
      include_internal: includeInternal,
    })
      .then((payload) => {
        if (cancelled) return;
        setData(payload);
        setStatus("ready");
        setMessage("");
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        console.error("Не удалось загрузить управленческую витрину", errorMessage(error));
        setStatus("error");
        setMessage("Проверьте доступ к источнику и повторите загрузку.");
      });
    return () => {
      cancelled = true;
    };
  }, [dateFrom, dateTo, includeInternal]);

  const daysOnHand = ratioByKey(data, "cash_days_on_hand");
  const coverage = ratioByKey(data, "inflow_outflow_coverage");
  const reviewShare = ratioByKey(data, "review_share");
  const avgDailyOutflowRatio = ratioByKey(data, "average_daily_external_outflow");
  const internalTurnoverRatio = ratioByKey(data, "internal_turnover_share");
  const netMarginRatio = ratioByKey(data, "net_cashflow_margin");
  const maxDailyFlow = Math.max(
    ...((data?.daily || []).map((row) => Math.max(Number(row.inflow_amount) || 0, Number(row.outflow_amount) || 0))),
    1
  );

  const setQuickRange = (days: number) => {
    setDateTo(asOf);
    setDateFrom(addDaysIso(asOf, -(days - 1)));
  };

  return (
    <section className="executive-cashflow-period" aria-label="ОДДС CashFlow за период">
      <header className="executive-panel__header">
        <div>
          <h2>ОДДС CashFlow</h2>
          <span>Форма для финансистов: остатки, CFO / CFI / CFF, FCF и контроль движения денег</span>
        </div>
        <div className="executive-panel__filters">
          <button type="button" onClick={() => setQuickRange(7)}>
            7 дней
          </button>
          <button type="button" onClick={() => setQuickRange(30)}>
            30 дней
          </button>
          <button type="button" onClick={() => setDateFrom(monthStartIso(asOf))}>
            Месяц
          </button>
          <input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} />
          <input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} />
          <label>
            <input
              checked={includeInternal}
              onChange={(event) => setIncludeInternal(event.target.checked)}
              type="checkbox"
            />
            внутренние переводы
          </label>
        </div>
      </header>

      {status === "error" && <div className="executive-cashflow-period__empty">{message}</div>}
      {status === "loading" && <div className="executive-cashflow-period__empty">Загрузка ОДДС...</div>}
      {status === "ready" && data && (
        <>
          {data.note && <div className="executive-cashflow-period__note">{data.note}</div>}
          <div className="executive-panel__kpis">
            <MetricCard
              hint="без внутренних переводов"
              label="Чистый поток без внутренних переводов"
              tooltip="Внешние поступления за вычетом внешних расходов, без учёта переводов между своими счетами."
              value={formatMoney(cashflowTotal(data, "external_net_amount"))}
            />
            <MetricCard
              hint="внешний поток"
              label="Поступило"
              tooltip="Сумма внешних поступлений за выбранный период."
              value={formatMoney(cashflowTotal(data, "external_inflow_amount"))}
            />
            <MetricCard
              hint="внешний поток"
              label="Списано"
              tooltip="Сумма внешних расходов за выбранный период."
              value={formatMoney(cashflowTotal(data, "external_outflow_amount"))}
            />
            <MetricCard
              hint={coverage ? `покрытие: ${formatMetricValue(coverage.value, coverage.unit)}` : "остаток / расход"}
              label={daysOnHand?.label || "Дней запаса"}
              tone={(daysOnHand?.tone as MetricTone) || "neutral"}
              tooltip={daysOnHand?.note || undefined}
              value={formatMetricValue(daysOnHand?.value, daysOnHand?.unit)}
            />
            <MetricCard
              hint={reviewShare ? `строк на проверку: ${formatMetricValue(reviewShare.value, reviewShare.unit)}` : "контроль качества"}
              label="Ошибки ДДС"
              tooltip="Число строк ДДС, требующих проверки или ручной классификации."
              value={formatPlainNumber(cashflowTotal(data, "quality_issue_count"))}
            />
            {avgDailyOutflowRatio && (
              <MetricCard
                label={avgDailyOutflowRatio.label}
                tone={(avgDailyOutflowRatio.tone as MetricTone) || "neutral"}
                tooltip={avgDailyOutflowRatio.note || undefined}
                value={formatMetricValue(avgDailyOutflowRatio.value, avgDailyOutflowRatio.unit)}
              />
            )}
            {internalTurnoverRatio && (
              <MetricCard
                label={internalTurnoverRatio.label}
                tone={(internalTurnoverRatio.tone as MetricTone) || "neutral"}
                tooltip={internalTurnoverRatio.note || undefined}
                value={formatMetricValue(internalTurnoverRatio.value, internalTurnoverRatio.unit)}
              />
            )}
            {netMarginRatio && (
              <MetricCard
                label={netMarginRatio.label}
                tone={(netMarginRatio.tone as MetricTone) || "neutral"}
                tooltip={netMarginRatio.note || undefined}
                value={formatMetricValue(netMarginRatio.value, netMarginRatio.unit)}
              />
            )}
          </div>

          <CashflowStatement data={data} includeInternal={includeInternal} />

          <div className="executive-cashflow-period__chart" aria-label="Динамика ОДДС по дням">
            {data.daily.slice(-31).map((row) => {
              const inflowWidth = `${Math.max(3, Math.round(((Number(row.inflow_amount) || 0) / maxDailyFlow) * 100))}%`;
              const outflowWidth = `${Math.max(3, Math.round(((Number(row.outflow_amount) || 0) / maxDailyFlow) * 100))}%`;
              return (
                <div className="executive-cashflow-day" key={row.business_date}>
                  <span>{formatDate(row.business_date)}</span>
                  <div>
                    <i style={{ width: inflowWidth }} />
                    <b style={{ width: outflowWidth }} />
                  </div>
                  <strong>{formatMoney(row.external_net_amount)}</strong>
                </div>
              );
            })}
          </div>

          <div className="executive-cashflow-period__tables">
            <div>
              <h3>По группам ДДС</h3>
              {data.by_group.slice(0, 6).map((row) => (
                <div className="executive-cashflow-row" key={row.key}>
                  <span>{row.label}</span>
                  <strong>{formatMoney(row.net_amount)}</strong>
                  <small>
                    +{formatMoney(row.inflow_amount)} / -{formatMoney(row.outflow_amount)}
                  </small>
                </div>
              ))}
            </div>
            <div>
              <h3>Топ статей</h3>
              {data.by_article.slice(0, 6).map((row) => (
                <div className="executive-cashflow-row" key={row.key}>
                  <span>{row.label}</span>
                  <strong>{formatMoney(row.net_amount)}</strong>
                  <small>{row.movement_count} движ.</small>
                </div>
              ))}
            </div>
            <div>
              <h3>Контроль</h3>
              {data.quality_issues.length === 0 ? (
                <div className="executive-cashflow-period__empty">Нет ошибок ДДС в выбранном периоде.</div>
              ) : (
                data.quality_issues.slice(0, 4).map((issue) => (
                  <div className="executive-cashflow-row" key={issue.issue_key}>
                    <span>{issue.issue_label}</span>
                    <strong>{formatMoney(issue.amount_abs)}</strong>
                    <small>
                      {issue.drilldown_url ? (
                        <a href={issue.drilldown_url} rel="noreferrer" target="_blank">
                          {issue.document_number || `Задача №${issue.bitrix_task_id}`}
                        </a>
                      ) : (
                        issue.document_number || formatDate(issue.business_date)
                      )}
                      {issue.task_status && ` · ${issue.task_status}`}
                    </small>
                  </div>
                ))
              )}
            </div>
          </div>
        </>
      )}
    </section>
  );
}

export function InventoryLossPanel({
  data,
  embedded = false,
}: {
  data?: ExecutiveProfitLossInventoryLoss | null;
  embedded?: boolean;
}) {
  const [storeMode, setStoreMode] = useState<"all" | "above_norm">("all");
  const [storeRef, setStoreRef] = useState("");
  const [operationMode, setOperationMode] = useState<"all" | "writeoff" | "receipt">("all");

  if (!data || ["source_missing", "source_error"].includes(data.source_status)) {
    return (
      <section
        className={embedded ? "executive-inventory-loss executive-inventory-loss--embedded" : "executive-profit-loss-lines"}
        aria-label={embedded ? "Расшифровка товарных потерь" : "Товарные потери за месяц"}
      >
        {!embedded && (
          <header>
            <h3>Товарные потери за месяц</h3>
            <span>{data ? statusLabel(data.source_status) : "нет данных"}</span>
          </header>
        )}
        <div className="executive-cashflow-period__empty">
          {data?.note || "Месячный отчет по списаниям и оприходованиям не подключен."}
        </div>
      </section>
    );
  }

  const visibleStores = data.stores.filter((store) => storeMode === "all" || store.above_norm);
  const visibleDocuments = data.top_documents.filter((document) => {
    if (storeRef && inventoryStoreKey(document) !== storeRef) return false;
    if (operationMode === "receipt") return document.operation_kind === "inventory_receipt";
    if (operationMode === "writeoff") return document.operation_kind !== "inventory_receipt";
    return true;
  });
  const maxHistoryLoss = Math.max(
    ...data.history.map((item) => Math.abs(Number(item.loss_amount) || 0)),
    1
  );
  const detailReady = !["source_missing", "source_error"].includes(data.detail_source_status);
  const receiptSurplus = isReceiptSurplus(data.loss_amount);
  const normSourceStatus = data.data_quality.norm_source_status || "unknown";
  const storeScopeStatus = data.data_quality.store_scope_status || "unknown";
  const normHint = {
    approved: "утвержденный KPI",
    provided: "явно заданный норматив",
    fallback: "резервный норматив",
    missing: "норматив не найден",
    unknown: "источник не указан",
  }[normSourceStatus] || statusLabel(normSourceStatus);
  const storeScopeCountLabel = storeScopeStatus === "approved"
    ? "Утверждено магазинов"
    : storeScopeStatus === "draft"
      ? "Магазинов в черновике"
      : "Магазинов в контуре";

  return (
    <section
      className={`executive-inventory-loss${embedded ? " executive-inventory-loss--embedded" : ""}`}
      aria-label={embedded ? "Расшифровка товарных потерь" : "Товарные потери за месяц"}
    >
      {!embedded && (
        <header className="executive-inventory-loss__header">
          <div>
            <h3>Товарные потери за месяц</h3>
            <span>{data.note || "Чистые товарные потери включены в расчет операционной прибыли."}</span>
          </div>
          <strong>{`${data.month} · ${statusLabel(data.source_status)}`}</strong>
        </header>
      )}

      <div className="executive-profit-loss-lines__rows">
        <div className="executive-profit-loss-line executive-profit-loss-line--metric">
          <span>Списания товаров</span>
          <strong>{formatProfitLossAmount(data.writeoff_amount)}</strong>
          <small>Инвентаризационные и дополнительные списания.</small>
        </div>
        <div className="executive-profit-loss-line executive-profit-loss-line--metric">
          <span>Оприходования товаров</span>
          <strong>{formatProfitLossAmount(data.receipt_amount)}</strong>
          <small>Оприходования по результатам инвентаризаций.</small>
        </div>
        <div className={`executive-profit-loss-line executive-profit-loss-line--total${receiptSurplus ? " executive-profit-loss-line--receipt-surplus" : ""}`}>
          <span>{receiptSurplus ? "Превышение оприходований" : "Чистые товарные потери"}</span>
          <strong>{formatInventoryLossMagnitude(data.loss_amount)}</strong>
          <small>
            {receiptSurplus ? "Оприходования минус списания" : "Списания минус оприходования"}
            {data.loss_pct !== null && data.loss_pct !== undefined
              ? ` · ${formatPercentPoints(receiptSurplus ? Math.abs(Number(data.loss_pct)) : data.loss_pct)} от продаж`
              : ""}
          </small>
        </div>
      </div>

      <div className="executive-panel__kpis executive-inventory-loss__kpis">
        <MetricCard
          hint={normHint}
          label="Норматив"
          tooltip={`Норматив shrinkage_rate для руководителя сети. Статус источника: ${statusLabel(normSourceStatus)}.`}
          value={formatPercentPoints(data.norm_pct)}
        />
        <MetricCard
          hint="факт минус норматив"
          label="Отклонение"
          tone={
            data.variance_to_norm_pct === null || data.variance_to_norm_pct === undefined
              ? "neutral"
              : Number(data.variance_to_norm_pct) > 0
                ? "warning"
                : "success"
          }
          tooltip="Положительное значение означает превышение норматива."
          value={formatPercentPoints(data.variance_to_norm_pct)}
        />
        <MetricCard
          hint={data.previous_month?.month || "нет опубликованного месяца"}
          label="Прошлый месяц"
          tooltip="Чистые товарные потери за непосредственно предыдущий календарный месяц."
          value={formatInventoryLossResult(data.previous_month?.loss_amount)}
        />
        <MetricCard
          hint={statusLabel(data.history_source_status)}
          label="Среднее за 3 месяца"
          tooltip="Среднее по трем предыдущим опубликованным месяцам в пределах года."
          value={formatInventoryLossResult(data.average_loss_amount_3m)}
        />
      </div>

      {data.history.length > 0 && (
        <section className="executive-inventory-loss__history" aria-label="Динамика товарных потерь">
          <h4>Динамика</h4>
          {data.history.map((item) => (
            <div className="executive-cashflow-day" key={item.month}>
              <span>{item.month}</span>
              <div>
                <b
                  className={isReceiptSurplus(item.loss_amount) ? "is-receipt-surplus" : undefined}
                  style={{
                    width: `${Math.max(3, Math.round((Math.abs(Number(item.loss_amount) || 0) / maxHistoryLoss) * 100))}%`,
                  }}
                />
              </div>
              <strong>{formatInventoryLossResult(item.loss_amount)}</strong>
            </div>
          ))}
        </section>
      )}

      {!detailReady ? (
        <div className="executive-cashflow-period__empty">
          {data.schema_version < 2
            ? "Источник v1 содержит только сетевые итоги. Детализация по магазинам и документам недоступна."
            : "Источник v2 опубликован без доступной детализации по магазинам и документам."}
        </div>
      ) : (
        <>
          <section className="executive-inventory-loss__section" aria-label="Потери по магазинам">
            <header>
              <div>
                <h4>По магазинам</h4>
                <span>Сначала показаны точки с наибольшими чистыми потерями.</span>
              </div>
              <div className="executive-panel__filters">
                <button
                  aria-pressed={storeMode === "all"}
                  onClick={() => setStoreMode("all")}
                  type="button"
                >
                  Все
                </button>
                <button
                  aria-pressed={storeMode === "above_norm"}
                  onClick={() => setStoreMode("above_norm")}
                  type="button"
                >
                  Выше норматива
                </button>
              </div>
            </header>
            <div className="executive-inventory-loss__table-wrap">
              <table className="executive-inventory-loss__table">
                <thead>
                  <tr>
                    <th>Магазин</th>
                    <th>Списания</th>
                    <th>Оприходования</th>
                    <th>Результат</th>
                    <th>Доля / норматив</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleStores.map((store) => (
                    <tr
                      className={store.above_norm ? "is-warning" : isReceiptSurplus(store.loss_amount) ? "is-receipt-surplus" : ""}
                      key={inventoryStoreKey(store)}
                    >
                      <td>{store.store_name}</td>
                      <td>{formatProfitLossAmount(store.writeoff_amount)}</td>
                      <td>{formatProfitLossAmount(store.receipt_amount)}</td>
                      <td>{formatInventoryLossResult(store.loss_amount)}</td>
                      <td>
                        {formatPercentPoints(store.loss_pct)} / {formatPercentPoints(store.norm_pct)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {visibleStores.length === 0 && (
                <div className="executive-cashflow-period__empty">Нет магазинов по выбранному фильтру.</div>
              )}
            </div>
          </section>

          <section className="executive-inventory-loss__section" aria-label="Крупнейшие товарные операции">
            <header>
              <div>
                <h4>Крупнейшие операции</h4>
                <span>До 20 документов из опубликованного месячного снимка.</span>
              </div>
              <div className="executive-panel__filters">
                <label>
                  Магазин
                  <select aria-label="Магазин документов" onChange={(event) => setStoreRef(event.target.value)} value={storeRef}>
                    <option value="">Все</option>
                    {data.stores.map((store) => (
                      <option key={inventoryStoreKey(store)} value={inventoryStoreKey(store)}>{store.store_name}</option>
                    ))}
                  </select>
                </label>
                <label>
                  Операция
                  <select
                    aria-label="Тип товарной операции"
                    onChange={(event) => setOperationMode(event.target.value as "all" | "writeoff" | "receipt")}
                    value={operationMode}
                  >
                    <option value="all">Все</option>
                    <option value="writeoff">Списания</option>
                    <option value="receipt">Оприходования</option>
                  </select>
                </label>
              </div>
            </header>
            <div className="executive-inventory-loss__table-wrap">
              <table className="executive-inventory-loss__table">
                <thead>
                  <tr>
                    <th>Документ</th>
                    <th>Дата</th>
                    <th>Операция</th>
                    <th>Магазин</th>
                    <th>Сумма</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleDocuments.map((document) => (
                    <tr key={document.stable_key}>
                      <td>{document.document_number || "Без номера"}</td>
                      <td>{formatDate(document.document_date)}</td>
                      <td>{document.operation_label}</td>
                      <td>{document.store_name}</td>
                      <td>{formatProfitLossAmount(document.amount)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {visibleDocuments.length === 0 && (
                <div className="executive-cashflow-period__empty">Нет документов по выбранному фильтру.</div>
              )}
            </div>
          </section>

          <section className="executive-inventory-loss__section" aria-label="Требует действий">
            <header>
              <div>
                <h4>Требует действий</h4>
                <span>Read-only очередь: задачи в Bitrix24 не создаются.</span>
              </div>
              <strong>{data.actions.length}</strong>
            </header>
            {data.actions.length === 0 ? (
              <div className="executive-cashflow-period__empty">Сигналов по нормативу и качеству данных нет.</div>
            ) : (
              <div className="executive-inventory-loss__actions">
                {data.actions.map((action) => (
                  <article key={action.stable_key}>
                    <div>
                      <strong>{action.title}</strong>
                      <span>{action.description}</span>
                    </div>
                    <small>
                      Ответственный: {action.responsible_name || "Руководитель сети"}. {action.recommended_action}
                    </small>
                  </article>
                ))}
              </div>
            )}
          </section>
        </>
      )}

      <details className="executive-inventory-loss__quality">
        <summary>Контроль качества данных · {statusLabel(data.data_quality.source_status)}</summary>
        <dl>
          <div><dt>{storeScopeCountLabel}</dt><dd>{data.data_quality.approved_store_count}</dd></div>
          <div><dt>Статус контура</dt><dd>{statusLabel(storeScopeStatus)}{data.data_quality.store_scope_month ? ` · ${data.data_quality.store_scope_month}` : ""}</dd></div>
          <div><dt>Источник норматива</dt><dd>{statusLabel(normSourceStatus)}</dd></div>
          <div><dt>Сопоставлено магазинов</dt><dd>{data.data_quality.matched_store_count}</dd></div>
          <div><dt>Несопоставлено документов</dt><dd>{data.data_quality.unmatched_document_count}</dd></div>
          <div><dt>Исключено тех. документов</dt><dd>{data.data_quality.excluded_document_count || 0}</dd></div>
          <div><dt>Исключённые тех. списания</dt><dd>{formatMoney(data.data_quality.excluded_writeoff_amount)}</dd></div>
          <div><dt>Исключённые тех. оприходования</dt><dd>{formatMoney(data.data_quality.excluded_receipt_amount)}</dd></div>
          <div><dt>Несопоставленные списания</dt><dd>{formatMoney(data.data_quality.unmatched_writeoff_amount)}</dd></div>
          <div><dt>Несопоставленные оприходования</dt><dd>{formatMoney(data.data_quality.unmatched_receipt_amount)}</dd></div>
        </dl>
      </details>

      {data.warnings.map((warning) => (
        <div className="executive-cashflow-period__note" key={warning}>{warning}</div>
      ))}
    </section>
  );
}

type ProfitLossChartMode = "profit" | "margin";

function profitLossMonthTooltipLabel(row: ExecutiveProfitLossMonthlyRow) {
  const quality = row.is_preliminary ? "предварительные данные" : "данные готовы";
  return [
    formatMonth(row.month),
    `валовая прибыль ${formatMoney(row.gross_profit)}`,
    `операционные расходы ${formatMoney(row.operating_expenses)}`,
    `операционная прибыль ${formatMoney(row.operating_profit)}`,
    `чистая прибыль ${formatMoney(row.net_profit)}`,
    `рентабельность чистой прибыли ${formatPercent(row.net_profit_margin_pct)}`,
    quality,
  ].join(", ");
}

function ProfitLossMonthlyChart({ monthly }: { monthly: ExecutiveProfitLossMonthlyRow[] }) {
  const [mode, setMode] = useState<ProfitLossChartMode>("profit");
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);
  const width = 1000;
  const height = 300;
  const padding = { top: 24, right: 76, bottom: 42, left: 86 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const bandWidth = monthly.length > 0 ? chartWidth / monthly.length : 0;

  const profitValues = monthly.flatMap((row) => [
    row.gross_profit,
    row.operating_profit,
    row.net_profit,
    row.comparison_net_profit,
    numericValue(row.operating_expenses) === null
      ? null
      : -Math.abs(numericValue(row.operating_expenses) || 0),
  ]).map(numericValue).filter((value): value is number => value !== null);
  const marginValues = monthly.flatMap((row) => [
    row.gross_margin_pct,
    row.operating_margin_pct,
    row.net_profit_margin_pct,
  ]).map(numericValue).filter((value): value is number => value !== null);
  const values = mode === "profit" ? profitValues : marginValues;
  const minValue = Math.min(0, ...values);
  const maxValue = Math.max(mode === "profit" ? 1 : 0.01, ...values);
  const pointFor = buildPointScale(
    monthly.length,
    padding,
    chartWidth,
    chartHeight,
    minValue,
    maxValue,
    mode === "profit" ? 1 : 0.01
  );
  const netMarginValues = monthly
    .map((row) => numericValue(row.net_profit_margin_pct))
    .filter((value): value is number => value !== null);
  const netMarginMin = Math.min(0, ...netMarginValues);
  const netMarginMax = Math.max(0.01, ...netMarginValues);
  const pointForNetMargin = buildPointScale(
    monthly.length,
    padding,
    chartWidth,
    chartHeight,
    netMarginMin,
    netMarginMax,
    0.01
  );
  const zeroY = pointFor(0, 0).y;
  const pointsFor = (key: keyof ExecutiveProfitLossMonthlyRow) => monthly.flatMap((row, index) => {
    const value = numericValue(row[key] as string | number | null | undefined);
    return value === null ? [] : [{ ...pointFor(index, value), row, value }];
  });
  const grossProfitPoints = pointsFor(mode === "profit" ? "gross_profit" : "gross_margin_pct");
  const operatingProfitPoints = pointsFor(mode === "profit" ? "operating_profit" : "operating_margin_pct");
  const netProfitPoints = pointsFor(mode === "profit" ? "net_profit" : "net_profit_margin_pct");
  const comparisonPoints = mode === "profit" ? pointsFor("comparison_net_profit") : [];
  const netMarginProfitPoints = mode === "profit" ? monthly.flatMap((row, index) => {
    const value = numericValue(row.net_profit_margin_pct);
    return value === null ? [] : [{ ...pointForNetMargin(index, value), row, value }];
  }) : [];
  const yTicks = [maxValue, (maxValue + minValue) / 2, minValue];
  const hoveredRow = hoveredIndex === null ? null : monthly[hoveredIndex];
  const hoveredLeft = monthly.length > 1 && hoveredIndex !== null
    ? (hoveredIndex / (monthly.length - 1)) * 100
    : 50;

  if (monthly.length === 0) return null;

  return (
    <section className="executive-profit-loss-trend" aria-label="Помесячная динамика ОПиУ">
      <header>
        <div>
          <h3>Динамика ОПиУ по месяцам</h3>
          <span>Оранжевая точка — месяц с неполными или предварительными данными.</span>
        </div>
        <div className="executive-profit-loss-trend__switch" aria-label="Режим графика">
          <button aria-pressed={mode === "profit"} onClick={() => setMode("profit")} type="button">Прибыль</button>
          <button aria-pressed={mode === "margin"} onClick={() => setMode("margin")} type="button">Рентабельность</button>
        </div>
      </header>
      <div className="executive-profit-loss-trend__legend">
        {mode === "profit" ? (
          <>
            <span><i className="gross" />Валовая прибыль</span>
            <span><i className="operating" />Операционная прибыль</span>
            <span><i className="net" />Чистая прибыль</span>
            <span><i className="net-margin" />Рентабельность чистой прибыли</span>
            <span><i className="expense" />Операционные расходы</span>
            <span><i className="comparison" />Чистая прибыль год назад</span>
          </>
        ) : (
          <>
            <span><i className="gross" />Валовая маржа</span>
            <span><i className="operating" />Операционная маржа</span>
            <span><i className="net" />Рентабельность чистой прибыли</span>
          </>
        )}
      </div>
      <div className="executive-profit-loss-trend__canvas">
        <svg role="img" viewBox={`0 0 ${width} ${height}`}>
          {mode === "profit" && monthly.map((row, index) => {
            const expense = Math.abs(numericValue(row.operating_expenses) || 0);
            const expenseY = pointFor(index, -expense).y;
            return (
              <rect
                className="executive-profit-loss-trend__expense-bar"
                height={Math.abs(expenseY - zeroY)}
                key={`expense-${row.month}`}
                rx="3"
                width={Math.max(12, bandWidth * 0.46)}
                x={pointFor(index, 0).x - Math.max(12, bandWidth * 0.46) / 2}
                y={Math.min(expenseY, zeroY)}
              />
            );
          })}
          {yTicks.map((value) => (
            <g key={value}>
              <line className="executive-profit-loss-trend__grid" x1={padding.left} x2={width - padding.right} y1={pointFor(0, value).y} y2={pointFor(0, value).y} />
              <text className="executive-profit-loss-trend__axis" x={padding.left - 10} y={pointFor(0, value).y + 4} textAnchor="end">
                {mode === "profit" ? formatMoney(value) : formatPercent(value)}
              </text>
            </g>
          ))}
          {mode === "profit" && [netMarginMax, (netMarginMax + netMarginMin) / 2, netMarginMin].map((value) => (
            <text className="executive-profit-loss-trend__axis executive-profit-loss-trend__axis--right" key={`margin-${value}`} x={width - padding.right + 10} y={pointForNetMargin(0, value).y + 4} textAnchor="start">
              {formatPercent(value)}
            </text>
          ))}
          {monthly.map((row, index) => (
            <text className="executive-profit-loss-trend__axis" key={row.month} x={pointFor(index, minValue).x} y={height - 12} textAnchor="middle">
              {formatMonth(row.month)}
            </text>
          ))}
          {grossProfitPoints.length > 1 && <path className="executive-profit-loss-trend__line executive-profit-loss-trend__line--gross" d={pathFor(grossProfitPoints)} />}
          {operatingProfitPoints.length > 1 && <path className="executive-profit-loss-trend__line executive-profit-loss-trend__line--operating" d={pathFor(operatingProfitPoints)} />}
          {netProfitPoints.length > 1 && <path className="executive-profit-loss-trend__line executive-profit-loss-trend__line--net" d={pathFor(netProfitPoints)} />}
          {comparisonPoints.length > 1 && <path className="executive-profit-loss-trend__line executive-profit-loss-trend__line--comparison" d={pathFor(comparisonPoints)} />}
          {netMarginProfitPoints.length > 1 && <path className="executive-profit-loss-trend__line executive-profit-loss-trend__line--net-margin" d={pathFor(netMarginProfitPoints)} />}
          {[grossProfitPoints, operatingProfitPoints, netProfitPoints].flatMap((series, seriesIndex) => series.map((point) => (
            <circle
              className={`executive-profit-loss-trend__point executive-profit-loss-trend__point--${["gross", "operating", "net"][seriesIndex]}${point.row.is_preliminary ? " executive-profit-loss-trend__point--preliminary" : ""}`}
              cx={point.x}
              cy={point.y}
              key={`${seriesIndex}-${point.row.month}`}
              r={point.row.is_preliminary ? 5 : 3.5}
            />
          )))}
          {netMarginProfitPoints.map((point) => (
            <circle className={`executive-profit-loss-trend__point executive-profit-loss-trend__point--net-margin${point.row.is_preliminary ? " executive-profit-loss-trend__point--preliminary" : ""}`} cx={point.x} cy={point.y} key={`net-margin-${point.row.month}`} r={point.row.is_preliminary ? 5 : 3.5} />
          ))}
          {hoveredIndex !== null && <line className="executive-sales-chart-crosshair" x1={pointFor(hoveredIndex, 0).x} x2={pointFor(hoveredIndex, 0).x} y1={padding.top} y2={height - padding.bottom} />}
          {monthly.map((row, index) => (
            <rect
              aria-label={profitLossMonthTooltipLabel(row)}
              className="executive-sales-chart-hit"
              height={chartHeight}
              key={`hit-${row.month}`}
              onBlur={() => setHoveredIndex(null)}
              onFocus={() => setHoveredIndex(index)}
              onMouseEnter={() => setHoveredIndex(index)}
              onMouseLeave={() => setHoveredIndex(null)}
              role="button"
              tabIndex={0}
              width={bandWidth}
              x={padding.left + index * bandWidth}
              y={padding.top}
            />
          ))}
        </svg>
        {hoveredRow && (
          <div className="executive-sales-month-tooltip executive-profit-loss-trend__tooltip" role="status" style={{ left: `${hoveredLeft}%` }}>
            <strong>{formatMonth(hoveredRow.month)}{hoveredRow.is_preliminary ? " · предварительно" : ""}</strong>
            <span>Выручка: {formatMoney(hoveredRow.revenue)}</span>
            <span>Валовая прибыль: {formatMoney(hoveredRow.gross_profit)}</span>
            <span>Операционные расходы: {formatMoney(hoveredRow.operating_expenses)}</span>
            <span>Операционная прибыль: {formatMoney(hoveredRow.operating_profit)}</span>
            <span>Чистая прибыль: {formatMoney(hoveredRow.net_profit)}</span>
            <span>Рентабельность чистой прибыли: {formatPercent(hoveredRow.net_profit_margin_pct)}</span>
            {numericValue(hoveredRow.comparison_net_profit) !== null && <span>Чистая прибыль год назад: {formatMoney(hoveredRow.comparison_net_profit)}</span>}
            {hoveredRow.note && <small>{hoveredRow.note}</small>}
          </div>
        )}
      </div>
    </section>
  );
}

function ProfitLossPeriodPanel({
  dateFrom,
  dateTo,
  refreshNonce,
}: {
  dateFrom: string;
  dateTo: string;
  refreshNonce: number;
}) {
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [message, setMessage] = useState("");
  const [data, setData] = useState<ExecutiveProfitLossPeriodResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.resolve().then(() => {
      if (!cancelled) setStatus("loading");
    });
    fetchExecutiveProfitLossPeriod({
      date_from: dateFrom,
      date_to: dateTo,
    })
      .then((payload) => {
        if (cancelled) return;
        setData(payload);
        setStatus("ready");
        setMessage("");
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setStatus("error");
        setMessage(errorMessage(error));
      });
    return () => {
      cancelled = true;
    };
  }, [dateFrom, dateTo, refreshNonce]);

  const grossMargin = profitLossRatioByKey(data, "gross_margin_pct");
  const operatingMargin = profitLossRatioByKey(data, "operating_margin_pct");
  const netProfitMargin = profitLossRatioByKey(data, "net_profit_margin_pct");
  const netProfitLine = data?.lines.find((line) => line.key === "net_profit");
  return (
    <section className="executive-cashflow-period executive-profit-loss-period" aria-label="Отчет о прибылях и убытках за период">
      <header className="executive-panel__header">
        <div>
          <h2>Отчет о прибылях и убытках</h2>
          <span>Чистая выручка, себестоимость, валовая прибыль и операционные расходы</span>
        </div>
      </header>

      {status === "error" && <div className="executive-cashflow-period__empty">{message}</div>}
      {status === "loading" && <div className="executive-cashflow-period__empty">Загрузка ОПУ...</div>}
      {status === "ready" && data && (
        <>
          {data.note && <div className="executive-cashflow-period__note">{data.note}</div>}
          <div className="executive-panel__kpis">
            <MetricCard
              hint={`${formatDate(data.date_from)} - ${formatDate(data.date_to)}`}
              label="Чистая выручка"
              tooltip="Выручка по продажам 1С за вычетом возвратов покупателям."
              value={formatMoney(profitLossTotal(data, "revenue"))}
            />
            <MetricCard
              hint="из 1С продаж"
              label="Себестоимость"
              tooltip="Себестоимость проданных товаров и услуг за период."
              value={formatMoney(profitLossTotal(data, "cost_of_sales"))}
            />
            <MetricCard
              hint="выручка минус себестоимость"
              label="Валовая прибыль"
              tooltip="Выручка за вычетом себестоимости продаж."
              value={formatMoney(profitLossTotal(data, "gross_profit"))}
            />
            <MetricCard
              hint={`${formatPlainNumber(profitLossTotal(data, "sales_count"))} продаж`}
              label={grossMargin?.label || "Валовая маржа"}
              tone={(grossMargin?.tone as MetricTone) || "neutral"}
              tooltip={grossMargin?.note || "Валовая прибыль к выручке, %."}
              value={formatMetricValue(grossMargin?.value, grossMargin?.unit)}
            />
            <MetricCard
              hint={statusLabel(data.expense_source_status)}
              label="Операционные расходы"
              tooltip="Расходы по данным ДДС плюс начисленные операционные налоги и взносы."
              value={formatMoney(profitLossTotal(data, "operating_expenses_total"))}
            />
            <MetricCard
              hint={formatMetricValue(operatingMargin?.value, operatingMargin?.unit)}
              label="Операционная прибыль"
              tone={(operatingMargin?.tone as MetricTone) || "neutral"}
              tooltip={operatingMargin?.note || "Валовая прибыль за вычетом операционных расходов."}
              value={formatMoney(profitLossTotal(data, "operating_profit"))}
            />
            <MetricCard
              hint={netProfitLine?.note || statusLabel(netProfitLine?.source_status || "source_missing")}
              label="Чистая прибыль"
              tone={
                Number(profitLossTotal(data, "net_profit")) < 0
                  ? "danger"
                  : "info"
              }
              tooltip="Операционная прибыль с учетом товарных потерь, прочих доходов и расходов и начисленных налогов БП."
              value={formatMoney(profitLossTotal(data, "net_profit"))}
            />
            <MetricCard
              hint={netProfitMargin?.note || statusLabel(netProfitLine?.source_status || "source_missing")}
              label={netProfitMargin?.label || "Рентабельность чистой прибыли"}
              tone={(netProfitMargin?.tone as MetricTone) || "neutral"}
              tooltip="Чистая прибыль к выручке за выбранный период, %."
              value={
                netProfitMargin
                  ? formatMetricValue(netProfitMargin.value, netProfitMargin.unit)
                  : statusLabel(netProfitLine?.source_status || "source_missing")
              }
            />
            <MetricCard
              hint={formatMoney(profitLossTotal(data, "expense_open_question_amount"))}
              label="Открытые вопросы"
              tone={Number(profitLossTotal(data, "expense_open_question_count")) > 0 ? "warning" : "neutral"}
              tooltip="Расходы без закрывающих документов, требующие уточнения."
              value={formatPlainNumber(profitLossTotal(data, "expense_open_question_count"))}
            />
          </div>

          <ProfitLossMonthlyChart monthly={data.monthly || []} />

          <section className="executive-profit-loss-lines" aria-label="Структура ОПУ">
            <header>
              <h3>Структура ОПУ</h3>
              <span>{statusLabel(data.source_status)}</span>
            </header>
            <div className="executive-profit-loss-lines__rows">
              {data.lines.map((line) => (
                <ProfitLossStatementRow data={data} key={line.key} line={line} />
              ))}
            </div>
          </section>

          {data.expense_open_questions.length > 0 && (
            <div className="executive-cashflow-period__tables executive-profit-loss-open-questions">
            <div>
              <h3>Открытые вопросы</h3>
              {data.expense_open_questions.slice(0, 6).map((row) => (
                <div className="executive-cashflow-row" key={row.key}>
                  <span>{row.label}</span>
                  <strong>{formatMoney(row.amount)}</strong>
                  <small>{profitLossQuestionDetail(row)}</small>
                </div>
              ))}
            </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function onlineStoreValue(
  data: ExecutiveOnlineStorePeriodResponse | null,
  key: string,
  scope: "totals" | "comparison" = "totals"
) {
  const value = data?.[scope]?.[key];
  return value === null || value === undefined ? null : value;
}

function onlineStoreDelta(
  current: string | number | null,
  previous: string | number | null
): MetricDelta {
  const currentValue = numericValue(current);
  const previousValue = numericValue(previous);
  if (currentValue === null || previousValue === null || previousValue === 0) {
    return { text: "нет сопоставимой базы", direction: "flat", isFavorable: null };
  }
  const delta = ((currentValue - previousValue) / Math.abs(previousValue)) * 100;
  const direction: MetricDelta["direction"] = delta > 0 ? "up" : delta < 0 ? "down" : "flat";
  return {
    text: `${delta > 0 ? "+" : ""}${delta.toFixed(1)}% к прошлому периоду`,
    direction,
    isFavorable: direction === "flat" ? null : direction === "up",
  };
}

function onlineStoreConversionDelta(
  current: string | number | null,
  previous: string | number | null
): MetricDelta {
  const currentValue = numericValue(current);
  const previousValue = numericValue(previous);
  if (currentValue === null || previousValue === null) {
    return { text: "нет сопоставимой базы", direction: "flat", isFavorable: null };
  }
  const delta = currentValue - previousValue;
  const direction: MetricDelta["direction"] = delta > 0 ? "up" : delta < 0 ? "down" : "flat";
  return {
    text: `${delta > 0 ? "+" : ""}${delta.toFixed(2)} п.п. к прошлому периоду`,
    direction,
    isFavorable: direction === "flat" ? null : direction === "up",
  };
}

function landingPageLabel(value: string) {
  try {
    const url = new URL(value);
    return `${url.pathname}${url.search}` || "/";
  } catch {
    return value;
  }
}

function landingPageHref(value: string) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.toString() : null;
  } catch {
    return null;
  }
}

export function OnlineStorePanel({
  data,
  message,
  status,
}: {
  data: ExecutiveOnlineStorePeriodResponse | null;
  message: string;
  status: "loading" | "ready" | "error";
}) {
  const daily = data?.daily.slice(-31) || [];
  const maxDailyVisits = Math.max(1, ...daily.map((row) => row.visits));
  const funnel = data
    ? [
        { key: "click_buy", label: "Кликнули «Купить»", value: numericValue(onlineStoreValue(data, "click_buy")) || 0 },
        { key: "begin_checkout", label: "Начали оформление", value: numericValue(onlineStoreValue(data, "begin_checkout")) || 0 },
        { key: "purchases", label: "Покупки", value: numericValue(onlineStoreValue(data, "purchases")) || 0 },
      ]
    : [];
  const maxFunnelValue = Math.max(1, ...funnel.map((row) => row.value));

  return (
    <section className="executive-cashflow-period executive-online-store" aria-label="Интернет-магазин">
      <header className="executive-panel__header">
        <div>
          <h2>Интернет-магазин</h2>
          <span>Спрос, поведение и покупки на master-mobile.ru по данным Яндекс Метрики</span>
        </div>
      </header>

      {status === "error" && <div className="executive-cashflow-period__empty">{message}</div>}
      {status === "loading" && !data && <div className="executive-cashflow-period__empty">Загрузка Яндекс Метрики...</div>}
      {data && (
        <>
          <div aria-label="Статус источника интернет-магазина" className="executive__topline">
            <div><span>Источник</span><strong>Яндекс Метрика</strong></div>
            <div><span>Статус</span><strong>{statusLabel(data.source_status)}</strong></div>
            <div><span>Период</span><strong>{formatDate(data.date_from)} — {formatDate(data.date_to)}</strong></div>
            <div><span>Обновлено</span><strong>{formatDateTime(data.generated_at)}</strong></div>
          </div>

          {data.note && <div className="executive-cashflow-period__note">{data.note}</div>}

          <div aria-label="Основные KPI интернет-магазина" className="executive-panel__kpis">
            <MetricCard
              delta={onlineStoreDelta(onlineStoreValue(data, "visits"), onlineStoreValue(data, "visits", "comparison"))}
              label="Визиты"
              tooltip="Сессии на сайте за выбранный период."
              value={formatMetricValue(onlineStoreValue(data, "visits"))}
            />
            <MetricCard
              delta={onlineStoreDelta(onlineStoreValue(data, "visitors"), onlineStoreValue(data, "visitors", "comparison"))}
              label="Посетители"
              tooltip="Уникальные посетители сайта по модели Яндекс Метрики."
              value={formatMetricValue(onlineStoreValue(data, "visitors"))}
            />
            <MetricCard
              delta={onlineStoreDelta(onlineStoreValue(data, "purchases"), onlineStoreValue(data, "purchases", "comparison"))}
              label="Покупки на сайте"
              tooltip="Достижения e-commerce цели «Покупка»; это не финансовая выручка 1С."
              value={formatMetricValue(onlineStoreValue(data, "purchases"))}
            />
            <MetricCard
              delta={onlineStoreConversionDelta(
                onlineStoreValue(data, "purchase_conversion_pct"),
                onlineStoreValue(data, "purchase_conversion_pct", "comparison")
              )}
              label="Конверсия в покупку"
              tooltip="Покупки, делённые на визиты выбранного периода."
              value={formatPercentPoints(onlineStoreValue(data, "purchase_conversion_pct"))}
            />
            <MetricCard
              hint={`${formatMetricValue(onlineStoreValue(data, "primary_source_purchases"))} покупок · ${formatPercentPoints(onlineStoreValue(data, "primary_source_purchase_share_pct"))}`}
              label="Главный канал покупок"
              tooltip="Канал, выбранный текущим управленческим правилом Метрики."
              value={String(onlineStoreValue(data, "primary_source_name") || "Не определено")}
            />
          </div>

          <div aria-label="Сигналы намерения посетителей" className="executive-panel__kpis">
            <MetricCard label="Клики «Купить»" value={formatMetricValue(onlineStoreValue(data, "click_buy"))} />
            <MetricCard label="Начали оформление" value={formatMetricValue(onlineStoreValue(data, "begin_checkout"))} />
            <MetricCard label="Клики по телефону" value={formatMetricValue(onlineStoreValue(data, "phone_clicks"))} />
            <MetricCard label="Поиск по сайту" value={formatMetricValue(onlineStoreValue(data, "site_searches"))} />
          </div>

          <div className="executive-online-store__analysis">
            <section aria-label="Динамика трафика интернет-магазина" className="executive-sales-daily">
              <h3>Визиты и покупки по дням</h3>
              {daily.length === 0 ? (
                <div className="executive-cashflow-period__empty">За период нет дневных данных.</div>
              ) : (
                daily.map((row) => (
                  <div className="executive-sales-day" key={row.business_date}>
                    <span>{formatDate(row.business_date)}</span>
                    <div><i style={{ width: `${Math.max(2, Math.round((row.visits / maxDailyVisits) * 100))}%` }} /></div>
                    <strong>{formatMetricValue(row.visits)} визитов · {formatMetricValue(row.purchases)} покупок</strong>
                  </div>
                ))
              )}
            </section>

            <section aria-label="Воронка интернет-магазина" className="executive-sales-daily executive-online-store__funnel">
              <h3>Воронка намерения</h3>
              {funnel.map((row) => (
                <div className="executive-sales-day" key={row.key}>
                  <span>{row.label}</span>
                  <div><i style={{ width: `${Math.max(2, Math.round((row.value / maxFunnelValue) * 100))}%` }} /></div>
                  <strong>{formatMetricValue(row.value)}</strong>
                </div>
              ))}
            </section>
          </div>

          <div className="executive-cashflow-period__tables executive-online-store__tables">
            <section aria-label="Каналы трафика интернет-магазина">
              <h3>Каналы трафика</h3>
              {data.traffic_sources.slice(0, 8).map((row) => (
                <div className="executive-cashflow-row" key={row.key}>
                  <span>{row.label}</span>
                  <strong>{formatMetricValue(row.purchases)} покупок</strong>
                  <small>{formatMetricValue(row.visits)} визитов · конверсия {formatPercentPoints(row.purchase_conversion_pct)}</small>
                </div>
              ))}
              {data.traffic_sources.length === 0 && <div className="executive-cashflow-period__empty">Нет данных по каналам.</div>}
            </section>
            <section aria-label="Посадочные страницы интернет-магазина">
              <h3>Посадочные страницы</h3>
              {data.landing_pages.slice(0, 10).map((row) => {
                const href = landingPageHref(row.url);
                return (
                  <div className="executive-cashflow-row" key={row.url}>
                    <span>
                      {href ? <a href={href} rel="noreferrer" target="_blank">{landingPageLabel(row.url)}</a> : landingPageLabel(row.url)}
                    </span>
                    <strong>{formatMetricValue(row.purchases)} покупок</strong>
                    <small>{formatMetricValue(row.visits)} визитов · {formatMetricValue(row.click_buy)} кликов «Купить» · конверсия {formatPercentPoints(row.purchase_conversion_pct)}</small>
                  </div>
                );
              })}
              {data.landing_pages.length === 0 && <div className="executive-cashflow-period__empty">Нет данных по посадочным страницам.</div>}
            </section>
          </div>
        </>
      )}
    </section>
  );
}

function salesValue(
  data: ExecutiveSalesPeriodResponse | null,
  key: string,
  scope: "totals" | "comparison" = "totals"
) {
  const value = data?.[scope]?.[key];
  return value === null || value === undefined ? null : value;
}

function salesDelta(
  current: string | number | null,
  previous: string | number | null,
  options?: { percentagePoints?: boolean }
): MetricDelta {
  const currentValue = numericValue(current);
  const previousValue = numericValue(previous);
  if (currentValue === null || previousValue === null || previousValue === 0) {
    return { text: "нет сопоставимой базы", direction: "flat", isFavorable: null };
  }
  const delta = options?.percentagePoints
    ? (currentValue - previousValue) * 100
    : ((currentValue - previousValue) / Math.abs(previousValue)) * 100;
  const sign = delta > 0 ? "+" : "";
  const text = options?.percentagePoints
    ? `${sign}${delta.toFixed(1)} п.п. к прошлому периоду`
    : `${sign}${delta.toFixed(1)}% к прошлому периоду`;
  const direction: MetricDelta["direction"] = delta > 0 ? "up" : delta < 0 ? "down" : "flat";
  return { text, direction, isFavorable: direction === "flat" ? null : direction === "up" };
}

export function SalesBreakdown({
  emptyMessage = "Нет продаж в выбранном периоде.",
  onReset,
  title,
  rows,
  onSelect,
}: {
  emptyMessage?: string;
  onReset?: () => void;
  title: string;
  rows: ExecutiveSalesBreakdownRow[];
  onSelect: (key: string) => void;
}) {
  const visibleRows = rows.slice(0, 8);
  const maxRevenue = Math.max(1, ...visibleRows.map((row) => Math.abs(numericValue(row.revenue) || 0)));
  return (
    <section aria-label={title} className="executive-sales-breakdown-section">
      <header className="executive-sales-breakdown-section__header">
        <h3>{title}</h3>
        {onReset && (
          <button className="executive-sales-breakdown-section__reset" onClick={onReset} type="button">
            Показать всех
          </button>
        )}
      </header>
      {visibleRows.length === 0 ? (
        <div className="executive-cashflow-period__empty">{emptyMessage}</div>
      ) : (
        visibleRows.map((row) => {
          const revenue = Math.abs(numericValue(row.revenue) || 0);
          const width = Math.max(2, Math.round((revenue / maxRevenue) * 100));
          const planAttainment = numericValue(
            row.meta?.plan_attainment_pct as string | number | null | undefined
          );
          const marginGapPp = numericValue(
            row.meta?.margin_gap_pp as string | number | null | undefined
          );
          return (
            <button className="executive-sales-breakdown" key={row.key} onClick={() => onSelect(row.key)} type="button">
              <span className="executive-sales-breakdown__label">{row.label}</span>
              <strong className="executive-sales-breakdown__value">{formatMoney(row.revenue)}</strong>
              <div className="executive-sales-breakdown__track">
                <i style={{ width: `${width}%` }} />
              </div>
              <small>
                {formatPlainNumber(row.sales_count)} ед. · {formatPercent(row.gross_margin_pct)}
                {planAttainment !== null && ` · план ${formatPercent(planAttainment)}`}
                {marginGapPp !== null && ` · к плану ${marginGapPp > 0 ? "+" : ""}${marginGapPp.toFixed(1)} п.п.`}
              </small>
            </button>
          );
        })
      )}
    </section>
  );
}

function formatMonth(value: string) {
  const parsed = new Date(`${value}-01T00:00:00`);
  return Number.isNaN(parsed.getTime())
    ? value
    : new Intl.DateTimeFormat("ru-RU", { month: "short", year: "2-digit" }).format(parsed);
}

function monthTooltipLabel(row: ExecutiveSalesMonthlyRow) {
  const parts = [
    formatMonth(row.month),
    `выручка ${formatMoney(row.revenue)}`,
    `валовая прибыль ${formatMoney(row.gross_profit)}`,
    `маржа ${formatPercent(row.gross_margin_pct)}`,
    `объём ${formatPlainNumber(row.sales_count)} ед.`,
  ];
  if (row.forecast_revenue !== null && row.forecast_revenue !== undefined) {
    parts.push(`прогноз ${formatMoney(row.forecast_revenue)}`);
  }
  if (row.comparison_sales_count !== null && row.comparison_sales_count !== undefined) {
    parts.push(`объём за аналогичный прошлый период ${formatPlainNumber(row.comparison_sales_count)} ед.`);
  }
  return parts.join(", ");
}

function buildPointScale(
  length: number,
  padding: { top: number; right: number; bottom: number; left: number },
  chartWidth: number,
  chartHeight: number,
  minValue: number,
  maxValue: number,
  minRange: number
) {
  const valueRange = Math.max(maxValue - minValue, minRange);
  return (index: number, value: number) => ({
    x: padding.left + (length <= 1 ? 0 : (index / (length - 1)) * chartWidth),
    y: padding.top + ((maxValue - value) / valueRange) * chartHeight,
  });
}

function pathFor(points: Array<{ x: number; y: number }>) {
  return points.map((point, index) => `${index === 0 ? "M" : "L"}${point.x} ${point.y}`).join(" ");
}

function SalesLineChart({
  monthly,
  hoveredIndex,
  onHover,
}: {
  monthly: ExecutiveSalesMonthlyRow[];
  hoveredIndex: number | null;
  onHover: (index: number | null) => void;
}) {
  const width = 1000;
  const height = 260;
  const padding = { top: 18, right: 22, bottom: 38, left: 72 };
  const values = monthly.flatMap((row) => [row.revenue, row.gross_profit, row.forecast_revenue])
    .map((value) => numericValue(value))
    .filter((value): value is number => value !== null);
  const minValue = Math.min(0, ...values);
  const maxValue = Math.max(1, ...values);
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const pointFor = buildPointScale(monthly.length, padding, chartWidth, chartHeight, minValue, maxValue, 1);
  const revenuePoints = monthly.map((row, index) => ({
    ...pointFor(index, numericValue(row.revenue) || 0),
    value: numericValue(row.revenue) || 0,
    row,
  }));
  const grossProfitPoints = monthly.map((row, index) => ({
    ...pointFor(index, numericValue(row.gross_profit) || 0),
    value: numericValue(row.gross_profit) || 0,
    row,
  }));
  const forecastPoints = monthly.flatMap((row, index) => {
    const value = numericValue(row.forecast_revenue);
    return value === null ? [] : [{ ...pointFor(index, value), value, row }];
  });
  const yTicks = [maxValue, (maxValue + minValue) / 2, minValue];
  const xTickIndexes = monthly.map((_, index) => index);
  const bandWidth = monthly.length > 0 ? chartWidth / monthly.length : 0;
  const crosshairX = hoveredIndex === null ? null : pointFor(hoveredIndex, 0).x;

  const marginValues = monthly
    .map((row) => numericValue(row.gross_margin_pct))
    .filter((value): value is number => value !== null);
  const marginMin = Math.min(0, ...marginValues);
  const marginMax = Math.max(0.01, ...marginValues);
  const pointForMargin = buildPointScale(monthly.length, padding, chartWidth, chartHeight, marginMin, marginMax, 0.01);
  const marginPoints = monthly.map((row, index) => ({
    ...pointForMargin(index, numericValue(row.gross_margin_pct) || 0),
    value: numericValue(row.gross_margin_pct),
    row,
  }));
  const marginTicks = [marginMax, (marginMax + marginMin) / 2, marginMin];

  const volumes = monthly.map((row) => numericValue(row.sales_count) || 0);
  const comparisonVolumes = monthly.map((row) => numericValue(row.comparison_sales_count));
  const hasComparisonVolume = comparisonVolumes.some((value) => value !== null);
  const maxVolume = Math.max(
    1,
    ...volumes,
    ...comparisonVolumes.filter((value): value is number => value !== null)
  );
  const volumeBarWidth = hasComparisonVolume ? bandWidth * 0.32 : bandWidth * 0.6;

  return (
    <div className="executive-sales-line-chart" aria-label="Помесячная динамика продаж за год">
      <div className="executive-sales-period__legend">
        <span><i />Выручка</span>
        <span><em />Валовая прибыль</span>
        <span><b />Прогноз текущего месяца</span>
        <span><s />Валовая маржа, %</span>
        <span><u />Объём продаж</span>
        {hasComparisonVolume && <span><mark />Объём за аналогичный прошлый период</span>}
      </div>
      <svg role="img" viewBox={`0 0 ${width} ${height}`}>
        {monthly.map((row, index) => {
          const volume = volumes[index] || 0;
          const barHeight = volume === 0 ? 0 : (volume / maxVolume) * chartHeight;
          const comparisonVolume = comparisonVolumes[index];
          const showComparison = hasComparisonVolume && comparisonVolume !== null;
          const barX = showComparison
            ? pointFor(index, 0).x - volumeBarWidth - 2
            : pointFor(index, 0).x - volumeBarWidth / 2;
          return (
            <g key={`volume-${row.month}`}>
              <rect
                className="executive-sales-line-chart__volume-bar"
                height={barHeight}
                rx="2"
                width={volumeBarWidth}
                x={barX}
                y={height - padding.bottom - barHeight}
              />
              {showComparison && (
                <rect
                  className="executive-sales-line-chart__volume-bar--comparison"
                  height={(comparisonVolume / maxVolume) * chartHeight}
                  rx="2"
                  width={volumeBarWidth}
                  x={pointFor(index, 0).x + 2}
                  y={height - padding.bottom - (comparisonVolume / maxVolume) * chartHeight}
                />
              )}
            </g>
          );
        })}
        {yTicks.map((value) => {
          const point = pointFor(0, value);
          return (
            <g key={value}>
              <line className="executive-sales-line-chart__grid" x1={padding.left} x2={width - padding.right} y1={point.y} y2={point.y} />
              <text className="executive-sales-line-chart__axis" x={padding.left - 10} y={point.y + 4} textAnchor="end">
                {formatMoney(value)}
              </text>
            </g>
          );
        })}
        {marginTicks.map((value) => {
          const point = pointForMargin(0, value);
          return (
            <text className="executive-sales-line-chart__axis-right" key={value} x={width - padding.right + 10} y={point.y + 4} textAnchor="start">
              {formatPercent(value)}
            </text>
          );
        })}
        {xTickIndexes.map((index) => {
          const point = pointFor(index, minValue);
          return (
            <text className="executive-sales-line-chart__axis" key={index} x={point.x} y={height - 12} textAnchor="middle">
              {formatMonth(monthly[index]?.month || "")}
            </text>
          );
        })}
        {revenuePoints.length > 1 && <path className="executive-sales-line-chart__fact" d={pathFor(revenuePoints)} />}
        {grossProfitPoints.length > 1 && <path className="executive-sales-line-chart__profit" d={pathFor(grossProfitPoints)} />}
        {marginPoints.length > 1 && <path className="executive-sales-line-chart__margin" d={pathFor(marginPoints)} />}
        {revenuePoints.map((point) => (
          <circle className="executive-sales-line-chart__fact-point" cx={point.x} cy={point.y} key={`revenue-${point.row.month}`} r="3.5" />
        ))}
        {grossProfitPoints.map((point) => (
          <circle className="executive-sales-line-chart__profit-point" cx={point.x} cy={point.y} key={`profit-${point.row.month}`} r="3.5" />
        ))}
        {forecastPoints.map((point) => (
          <circle className="executive-sales-line-chart__forecast-point" cx={point.x} cy={point.y} key={`forecast-${point.row.month}`} r="5" />
        ))}
        {marginPoints.map((point) => (
          <circle className="executive-sales-line-chart__margin-point" cx={point.x} cy={point.y} key={`margin-${point.row.month}`} r="3.5" />
        ))}
        {crosshairX !== null && (
          <line className="executive-sales-chart-crosshair" x1={crosshairX} x2={crosshairX} y1={padding.top} y2={height - padding.bottom} />
        )}
        {monthly.map((row, index) => (
          <rect
            aria-label={monthTooltipLabel(row)}
            className="executive-sales-chart-hit"
            height={chartHeight}
            key={`hit-${row.month}`}
            onBlur={() => onHover(null)}
            onFocus={() => onHover(index)}
            onMouseEnter={() => onHover(index)}
            onMouseLeave={() => onHover(null)}
            role="button"
            tabIndex={0}
            width={bandWidth}
            x={padding.left + index * bandWidth}
            y={padding.top}
          />
        ))}
      </svg>
    </div>
  );
}

function SalesDailyChart({ daily }: { daily: ExecutiveSalesDailyRow[] }) {
  const visibleDays = daily.slice(-31);
  const values = visibleDays.map(
    (row) => numericValue(row.actual_revenue) ?? numericValue(row.forecast_revenue) ?? 0
  );
  if (!values.some((value) => value !== 0)) return null;
  const maxValue = Math.max(1, ...values.map((value) => Math.abs(value)));
  return (
    <div aria-label="Выручка по дням выбранного периода" className="executive-sales-daily">
      <h3>По дням выбранного периода</h3>
      {visibleDays.map((row, index) => {
        const actual = numericValue(row.actual_revenue);
        const isForecast = actual === null && numericValue(row.forecast_revenue) !== null;
        const value = values[index];
        const width = `${Math.max(2, Math.round((Math.abs(value) / maxValue) * 100))}%`;
        return (
          <div className="executive-sales-day" key={row.business_date}>
            <span>{formatDate(row.business_date)}</span>
            <div>
              <i
                className={isForecast ? "executive-sales-day__bar--forecast" : undefined}
                style={{ width }}
              />
            </div>
            <strong>
              {formatMoney(value)}
              {isForecast && <small>прогноз</small>}
            </strong>
          </div>
        );
      })}
    </div>
  );
}

function SalesMonthTooltip({
  monthly,
  hoveredIndex,
}: {
  monthly: ExecutiveSalesMonthlyRow[];
  hoveredIndex: number | null;
}) {
  if (hoveredIndex === null || !monthly[hoveredIndex]) return null;
  const row = monthly[hoveredIndex];
  const left = monthly.length > 1 ? (hoveredIndex / (monthly.length - 1)) * 100 : 50;
  const forecastValue = numericValue(row.forecast_revenue);
  const comparisonVolume = numericValue(row.comparison_sales_count);
  return (
    <div className="executive-sales-month-tooltip" role="status" style={{ left: `${left}%` }}>
      <strong>{formatMonth(row.month)}</strong>
      <span>Выручка: {formatMoney(row.revenue)}</span>
      <span>Валовая прибыль: {formatMoney(row.gross_profit)}</span>
      <span>Валовая маржа: {formatPercent(row.gross_margin_pct)}</span>
      <span>Объём: {formatPlainNumber(row.sales_count)} ед.</span>
      {forecastValue !== null && <span>Прогноз: {formatMoney(forecastValue)}</span>}
      {comparisonVolume !== null && (
        <span>Объём за аналогичный прошлый период: {formatPlainNumber(comparisonVolume)} ед.</span>
      )}
    </div>
  );
}

type ExecutiveDomainTabLayoutProps = {
  filters?: ReactNode;
  sourceStatus?: ReactNode;
  primaryKpis?: ReactNode;
  mainChart?: ReactNode;
  diagnosticKpis?: ReactNode;
  breakdowns?: ReactNode;
  actions?: ReactNode;
};

export function ExecutiveDomainTabLayout({
  filters,
  sourceStatus,
  primaryKpis,
  mainChart,
  diagnosticKpis,
  breakdowns,
  actions,
}: ExecutiveDomainTabLayoutProps) {
  return (
    <div className="executive-domain-tab-layout">
      {filters}
      {sourceStatus}
      {primaryKpis}
      {mainChart}
      {diagnosticKpis}
      {breakdowns}
      {actions}
    </div>
  );
}

const SALES_DIAGNOSTIC_LABELS: Record<string, string> = {
  lost_gross_profit_margin_gap: "Потерянная валовая прибыль",
  gross_profit_per_unit: "Валовая прибыль на единицу",
  cost_per_unit: "Себестоимость на единицу",
  margin_gap_pp: "Отклонение маржи от плана",
  stores_below_plan_count: "Магазины ниже плана",
  managers_below_target_margin_count: "Менеджеры ниже целевой маржи",
};

const SALES_DIAGNOSTIC_GROUPS = [
  {
    key: "economics",
    label: "Экономика продаж",
    metricKeys: [
      "lost_gross_profit_margin_gap",
      "gross_profit_per_unit",
      "cost_per_unit",
      "margin_gap_pp",
    ],
  },
  {
    key: "data_quality",
    label: "Качество данных и плана",
    metricKeys: ["stores_below_plan_count", "managers_below_target_margin_count"],
  },
] as const;

type SalesProblemFocus = "stores" | "managers" | null;

function salesDiagnosticValue(metric: ExecutiveSalesDiagnosticKpi) {
  const value = numericValue(metric.value);
  if (value === null) return "—";
  if (metric.unit === "RUB" || metric.unit === "RUB_PER_UNIT") return formatMoney(value);
  if (metric.unit === "PERCENTAGE_POINT") {
    return `${value > 0 ? "+" : ""}${value.toFixed(1)} п.п.`;
  }
  return formatPlainNumber(value);
}

function salesDiagnosticTone(metric: ExecutiveSalesDiagnosticKpi): MetricTone {
  if (metric.source_status === "source_error") return "danger";
  if (metric.source_status === "not_applicable") return "neutral";
  if (!["ready", "complete"].includes(metric.source_status)) return "warning";
  const value = numericValue(metric.value);
  if (value === null) return "neutral";
  if (metric.key === "margin_gap_pp") return value < 0 ? "warning" : "success";
  if (metric.key === "lost_gross_profit_margin_gap") return value > 0 ? "warning" : "success";
  if (metric.unit === "COUNT") return value > 0 ? "warning" : "success";
  return "neutral";
}

function salesDiagnosticStatus(metric: ExecutiveSalesDiagnosticKpi): { label: string; tone: MetricTone } {
  if (metric.source_status === "source_error") return { label: "Ошибка", tone: "danger" };
  if (metric.source_status === "not_applicable") return { label: "Не применяется", tone: "neutral" };
  if (metric.source_status === "source_missing") return { label: "Нет данных", tone: "warning" };
  if (!["ready", "complete"].includes(metric.source_status)) {
    return { label: "Нужна проверка", tone: "warning" };
  }
  const tone = salesDiagnosticTone(metric);
  if (tone === "warning" || tone === "danger") return { label: "Требует внимания", tone };
  if (tone === "success") return { label: "В норме", tone };
  return { label: "Рассчитано", tone: "info" };
}

function salesDiagnosticDetail(metric: ExecutiveSalesDiagnosticKpi) {
  const evaluated = numericValue(metric.meta?.evaluated_count as string | number | null | undefined);
  if (evaluated !== null && metric.value !== null && metric.value !== undefined) {
    const entity = metric.key === "stores_below_plan_count" ? "магазинов" : "менеджеров";
    return `Проверено: ${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(evaluated)} ${entity}`;
  }
  const note = metric.note?.trim();
  if (!note) return "По фактическим продажам 1С";
  if (note.startsWith("Не все продажи сопоставлены с frozen-планом магазинов")) {
    return "Не все продажи сопоставлены с утверждённым планом магазинов.";
  }
  if (note.startsWith("Не все магазины факта присутствуют во frozen-плане")) {
    return "Не все магазины из фактических продаж найдены в утверждённом плане.";
  }
  if (note.startsWith("У магазинов отсутствует утверждённый план выручки")) {
    return "Не у всех магазинов есть утверждённый план выручки.";
  }
  if (/\b0x[0-9a-f]{16,}\b/i.test(note)) {
    const summary = note.split(":", 1)[0].trim();
    return `${summary
      .replace("frozen-планом", "утверждённым планом")
      .replace("frozen-плане", "утверждённом плане")}.`;
  }
  return note
    .replaceAll("frozen-планом", "утверждённым планом")
    .replaceAll("frozen-плане", "утверждённом плане")
    .replaceAll("frozen-планов", "утверждённых планов")
    .replaceAll("frozen-план", "утверждённый план")
    .replaceAll("snapshot", "источник данных");
}

function salesPlanSummary(status?: string) {
  if (status === "ready" || status === "complete") return "План сопоставлен";
  if (status === "partial") return "План сопоставлен частично";
  if (status === "not_applicable") return "План не применяется";
  if (status === "source_error") return "Ошибка плана";
  return "План не утверждён";
}

function diagnosticCountLabel(value: number, one: string, few: string, many: string) {
  const mod10 = value % 10;
  const mod100 = value % 100;
  const word = mod10 === 1 && mod100 !== 11 ? one : mod10 >= 2 && mod10 <= 4 && !(mod100 >= 12 && mod100 <= 14) ? few : many;
  return `${value} ${word}`;
}

function salesProblemRows(
  metric: ExecutiveSalesDiagnosticKpi | undefined,
  rows: ExecutiveSalesBreakdownRow[]
): ExecutiveSalesBreakdownRow[] {
  const raw = metric?.meta?.problem;
  if (!Array.isArray(raw)) return [];
  const rowByKey = new Map(rows.map((row) => [row.key, row]));
  return raw.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const entry = item as Record<string, unknown>;
    const key = typeof entry.key === "string" ? entry.key : "";
    if (!key) return [];
    const existing = rowByKey.get(key);
    if (existing) return [existing];
    const label = (typeof entry.label === "string" && entry.label) || key;
    return [
      {
        key,
        label,
        revenue: "0",
        gross_profit: "0",
        sales_count: "0",
        gross_margin_pct: null,
        meta: {},
      },
    ];
  });
}

export function SalesPeriodPanel({
  actions,
  data,
  message,
  onSelectManager,
  onSelectStore,
  status,
}: {
  actions?: ReactNode;
  data: ExecutiveSalesPeriodResponse | null;
  message: string;
  onSelectManager: (managerRef: string) => void;
  onSelectStore: (storeRef: string) => void;
  status: "loading" | "ready" | "error";
}) {
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);
  const [hoveredIndexForData, setHoveredIndexForData] = useState(data);
  const [problemFocus, setProblemFocus] = useState<SalesProblemFocus>(null);
  const breakdownsRef = useRef<HTMLDivElement>(null);
  if (data !== hoveredIndexForData) {
    setHoveredIndexForData(data);
    setHoveredIndex(null);
    setProblemFocus(null);
  }

  const diagnosticMetrics = data?.diagnostic_kpis || [];
  const diagnosticMetricsByKey = new Map(diagnosticMetrics.map((metric) => [metric.key, metric]));
  const problemStores = useMemo(
    () =>
      salesProblemRows(
        (data?.diagnostic_kpis || []).find((metric) => metric.key === "stores_below_plan_count"),
        data?.by_store || []
      ),
    [data]
  );
  const problemManagers = useMemo(
    () =>
      salesProblemRows(
        (data?.diagnostic_kpis || []).find(
          (metric) => metric.key === "managers_below_target_margin_count"
        ),
        data?.by_manager || []
      ),
    [data]
  );
  const attentionCount = diagnosticMetrics.filter((metric) =>
    ["warning", "danger"].includes(salesDiagnosticTone(metric))
  ).length;
  const calculatedCount = diagnosticMetrics.filter(
    (metric) =>
      numericValue(metric.value) !== null &&
      ["ready", "complete"].includes(metric.source_status)
  ).length;
  const planDiagnosticStatuses = diagnosticMetrics
    .filter((metric) =>
      [
        "lost_gross_profit_margin_gap",
        "margin_gap_pp",
        "stores_below_plan_count",
        "managers_below_target_margin_count",
      ].includes(metric.key)
    )
    .map((metric) => metric.source_status);
  const effectivePlanStatus = planDiagnosticStatuses.includes("source_error")
    ? "source_error"
    : planDiagnosticStatuses.includes("partial")
      ? "partial"
      : data?.plan_status;

  const showProblems = (focus: Exclude<SalesProblemFocus, null>) => {
    setProblemFocus(focus);
    breakdownsRef.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  };

  const actualRevenue = salesValue(data, "revenue");
  const grossProfit = salesValue(data, "gross_profit");
  const grossMargin = salesValue(data, "gross_margin_pct");
  const salesCount = salesValue(data, "sales_count");
  const forecastRevenue = salesValue(data, "forecast_revenue_period_end");
  const actualRevenueNum = numericValue(actualRevenue);
  const salesCountNum = numericValue(salesCount);
  const forecastRevenueNum = numericValue(forecastRevenue);
  const revenuePerUnit = actualRevenueNum !== null && salesCountNum ? actualRevenueNum / salesCountNum : null;
  const previousRevenueNum = numericValue(salesValue(data, "revenue", "comparison"));
  const previousSalesCountNum = numericValue(salesValue(data, "sales_count", "comparison"));
  const previousRevenuePerUnit =
    previousRevenueNum !== null && previousSalesCountNum ? previousRevenueNum / previousSalesCountNum : null;
  const planAttainment = numericValue(data?.plan?.plan_attainment_pct);
  const approvedPlanRevenue = numericValue(data?.plan?.approved_revenue);
  const planAttainmentValue = (() => {
    if (planAttainment !== null) return formatPercent(planAttainment);
    if (data?.plan?.scope_type === "manager") return "Нет плана выручки";
    if (data?.plan_status === "not_applicable") return "Только режим «Месяц»";
    if (data?.plan_status === "source_missing") return "План не утверждён";
    return statusLabel(data?.plan_status || "source_missing");
  })();
  const planAttainmentTone: MetricTone =
    planAttainment === null ? "warning" : planAttainment >= 1 ? "success" : "warning";

  return (
    <section className="executive-cashflow-period executive-sales-period" aria-label="Продажи">
      <header className="executive-panel__header">
        <div>
          <h2>Продажи</h2>
          <span>Факт 1С и прогноз выручки до конца периода</span>
        </div>
      </header>

      {status === "error" && <div className="executive-cashflow-period__empty">{message}</div>}
      {status === "loading" && !data && <div className="executive-cashflow-period__empty">Загрузка продаж...</div>}
      {data && (
        <>
          {data.note && <div className="executive-cashflow-period__note">{data.note}</div>}
          {data.forecast_note && <div className="executive-sales-period__forecast-note">{data.forecast_note}</div>}
          <ExecutiveDomainTabLayout
            sourceStatus={(
              <div aria-label="Статус источников продаж" className="executive__topline executive-sales-period__status">
                <div><span>Факт 1С</span><strong>{statusLabel(data.source_status)}</strong></div>
                <div><span>Свежесть факта</span><strong>{statusLabel(data.freshness_status)}</strong></div>
                <div><span>План продаж</span><strong>{statusLabel(data.plan_status || "source_missing")}</strong></div>
                <div><span>План заморожен</span><strong>{data.plan?.frozen_at ? formatDateTime(data.plan.frozen_at) : "—"}</strong></div>
              </div>
            )}
            primaryKpis={(
              <div aria-label="Основные KPI продаж" className="executive-panel__kpis">
                <MetricCard
                  delta={salesDelta(actualRevenue, salesValue(data, "revenue", "comparison"))}
                  label="Выручка факт"
                  tooltip="Сумма продаж 1С за выбранный период, включая возвраты, отражённые в витрине."
                  value={formatNullableMoney(actualRevenueNum)}
                />
                <MetricCard
                  hint={data.forecast_status === "ready" ? "на конец периода" : statusLabel(data.forecast_status)}
                  label="Прогноз выручки"
                  tone={["ready", "complete"].includes(data.forecast_status) ? "neutral" : "warning"}
                  tooltip="Медиана выручки по тому же дню недели за 4 предыдущие недели; для периода, уже полностью в прошлом, не строится."
                  value={forecastRevenueNum === null ? "нет данных" : formatMoney(forecastRevenueNum)}
                />
                <MetricCard
                  delta={salesDelta(grossProfit, salesValue(data, "gross_profit", "comparison"))}
                  label="Валовая прибыль"
                  tooltip="Выручка за вычетом себестоимости продаж за выбранный период."
                  value={formatNullableMoney(numericValue(grossProfit))}
                />
                <MetricCard
                  delta={salesDelta(grossMargin, salesValue(data, "gross_margin_pct", "comparison"), { percentagePoints: true })}
                  label="Валовая маржа"
                  tooltip="Валовая прибыль к выручке, % за выбранный период."
                  value={formatPercent(grossMargin)}
                />
                <MetricCard
                  delta={salesDelta(salesCount, salesValue(data, "sales_count", "comparison"))}
                  label="Объём продаж"
                  tooltip="Количество проданных единиц товаров и услуг, не число чеков."
                  value={`${formatPlainNumber(salesCount)} ед.`}
                />
                <MetricCard
                  delta={revenuePerUnit !== null && previousRevenuePerUnit ? salesDelta(revenuePerUnit, previousRevenuePerUnit) : undefined}
                  label="Выручка на единицу"
                  tooltip="Выручка за период, делённая на количество проданных единиц товаров и услуг."
                  value={revenuePerUnit === null ? "нет данных" : formatMoney(revenuePerUnit)}
                />
                <MetricCard
                  hint={approvedPlanRevenue === null ? data.plan_note || data.plan?.note : `план ${formatMoney(approvedPlanRevenue)}`}
                  label="Выполнение плана"
                  tone={planAttainmentTone}
                  tooltip="Для открытого месяца прогноз выручки сравнивается с утверждённым планом; для закрытого месяца используется факт."
                  value={planAttainmentValue}
                />
              </div>
            )}
            mainChart={data.monthly.length > 0 && (
              <>
                <div className="executive-sales-charts">
                  <SalesLineChart hoveredIndex={hoveredIndex} monthly={data.monthly} onHover={setHoveredIndex} />
                  <SalesMonthTooltip hoveredIndex={hoveredIndex} monthly={data.monthly} />
                </div>
                <SalesDailyChart daily={data.daily} />
              </>
            )}
            diagnosticKpis={(
              <section aria-label="Диагностические KPI продаж" className="executive-sales-diagnostics">
                <div className="executive-sales-diagnostics__header">
                  <h3>Диагностика продаж</h3>
                  <div aria-label="Сводка диагностики продаж" className="executive-sales-diagnostics__summary">
                    <span className={attentionCount > 0 ? "executive-sales-diagnostics__summary-item--warning" : undefined}>
                      {diagnosticCountLabel(attentionCount, "требует внимания", "требуют внимания", "требуют внимания")}
                    </span>
                    <span>{diagnosticCountLabel(calculatedCount, "рассчитан", "рассчитаны", "рассчитано")}</span>
                    <span>{salesPlanSummary(effectivePlanStatus)}</span>
                  </div>
                </div>
                <div className="executive-sales-diagnostics__table-wrap">
                  <table className="executive-sales-diagnostics__table">
                    <caption className="visually-hidden">Показатели диагностики продаж</caption>
                    <thead>
                      <tr>
                        <th scope="col">Показатель</th>
                        <th scope="col">Значение</th>
                        <th scope="col">Статус</th>
                        <th scope="col">Что это значит</th>
                      </tr>
                    </thead>
                    {SALES_DIAGNOSTIC_GROUPS.map((group) => (
                      <tbody aria-label={group.label} key={group.key}>
                        <tr className="executive-sales-diagnostics__group-row">
                          <th colSpan={4} scope="rowgroup">{group.label}</th>
                        </tr>
                        {group.metricKeys.map((metricKey) => diagnosticMetricsByKey.get(metricKey)).filter(Boolean).map((metric) => {
                          const diagnosticMetric = metric as ExecutiveSalesDiagnosticKpi;
                          const diagnosticStatus = salesDiagnosticStatus(diagnosticMetric);
                          const diagnosticTone = salesDiagnosticTone(diagnosticMetric);
                          const focusTarget: SalesProblemFocus =
                            diagnosticMetric.key === "managers_below_target_margin_count"
                              ? "managers"
                              : ["lost_gross_profit_margin_gap", "margin_gap_pp", "stores_below_plan_count"].includes(diagnosticMetric.key)
                                ? "stores"
                                : null;
                          const focusCount = focusTarget === "stores" ? problemStores.length : focusTarget === "managers" ? problemManagers.length : 0;
                          const showAction = focusTarget && focusCount > 0 && ["warning", "danger"].includes(diagnosticTone);
                          return (
                            <tr key={diagnosticMetric.key}>
                              <th scope="row">{SALES_DIAGNOSTIC_LABELS[diagnosticMetric.key] || diagnosticMetric.key}</th>
                              <td className={`executive-sales-diagnostics__value executive-sales-diagnostics__value--${diagnosticTone}`}>
                                {salesDiagnosticValue(diagnosticMetric)}
                              </td>
                              <td>
                                {["warning", "danger"].includes(diagnosticStatus.tone) ? (
                                  <StatusBadge tone={diagnosticStatus.tone}>{diagnosticStatus.label}</StatusBadge>
                                ) : (
                                  <span className="executive-sales-diagnostics__calm-status">{diagnosticStatus.label}</span>
                                )}
                              </td>
                              <td className="executive-sales-diagnostics__detail">
                                <span>{salesDiagnosticDetail(diagnosticMetric)}</span>
                                {showAction && (
                                  <button
                                    className="executive-sales-diagnostics__action"
                                    onClick={() => focusTarget && showProblems(focusTarget)}
                                    type="button"
                                  >
                                    {focusTarget === "stores" ? "Показать проблемные магазины" : "Показать менеджеров ниже маржи"}
                                  </button>
                                )}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    ))}
                  </table>
                </div>
              </section>
            )}
            breakdowns={(
              <div className="executive-cashflow-period__tables executive-sales-period__tables" ref={breakdownsRef}>
                <SalesBreakdown
                  emptyMessage="Проблемных магазинов не найдено."
                  onReset={problemFocus === "stores" ? () => setProblemFocus(null) : undefined}
                  onSelect={onSelectStore}
                  rows={problemFocus === "stores" ? problemStores : data.by_store}
                  title={problemFocus === "stores" ? "Проблемные магазины" : "По магазинам"}
                />
                <SalesBreakdown
                  emptyMessage="Проблемных менеджеров не найдено."
                  onReset={problemFocus === "managers" ? () => setProblemFocus(null) : undefined}
                  onSelect={onSelectManager}
                  rows={problemFocus === "managers" ? problemManagers : data.by_manager}
                  title={problemFocus === "managers" ? "Проблемные менеджеры" : "По менеджерам"}
                />
              </div>
            )}
            actions={actions}
          />
        </>
      )}
    </section>
  );
}

const ACTION_PREVIEW_LIMIT = 10;

function actionPayloadText(action: ExecutiveDashboardAction, key: string) {
  const value = action.payload[key];
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export function ActionTable({
  actions,
  onOpen,
}: {
  actions: ExecutiveDashboardAction[];
  onOpen: (action: ExecutiveDashboardAction) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const visibleActions = expanded ? actions : actions.slice(0, ACTION_PREVIEW_LIMIT);
  if (actions.length === 0) {
    return (
      <div className="executive-actions__empty">
        На выбранный день нет открытых решений. Проверьте источники ниже или выберите другой раздел.
      </div>
    );
  }
  return (
    <div className="executive-actions__table-wrap">
      <table className="executive-actions__table">
        <caption className="visually-hidden">Решения руководителя на выбранный день</caption>
        <thead>
          <tr>
            <th>Важность</th>
            <th>Домен</th>
            <th>Решение</th>
            <th>Сумма</th>
            <th>Ответственный</th>
            <th>Срок</th>
            <th>Источник</th>
          </tr>
        </thead>
        <tbody>
          {visibleActions.map((action) => (
            <tr key={action.stable_key}>
              <td>
                <span className={`executive-severity executive-severity--${action.severity}`}>
                  {severityLabel(action.severity)}
                </span>
              </td>
              <td>{DOMAIN_LABELS[action.domain] || action.domain}</td>
              <td>
                <button
                  aria-label={`Открыть решение: ${action.title}`}
                  className="executive-actions__open"
                  onClick={() => onOpen(action)}
                  type="button"
                >
                  <strong>{action.title}</strong>
                  {action.description && <span>{action.description}</span>}
                </button>
              </td>
              <td>{action.amount ? formatMoney(action.amount, action.currency) : ""}</td>
              <td>{action.responsible_bitrix_user_id || ""}</td>
              <td>{formatDateTime(action.deadline_at)}</td>
              <td>
                {action.drilldown_url ? (
                  <a href={action.drilldown_url} rel="noreferrer" target="_blank">
                    {action.source_system}
                  </a>
                ) : (
                  action.source_system
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {actions.length > ACTION_PREVIEW_LIMIT && (
        <button
          className="executive-actions__expand"
          onClick={() => setExpanded((value) => !value)}
          type="button"
        >
          {expanded ? "Свернуть список" : `Показать все ${actions.length}`}
        </button>
      )}
    </div>
  );
}

export function ActionDetail({
  action,
  onClose,
}: {
  action: ExecutiveDashboardAction;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const sourceNumber = actionPayloadText(action, "onec_source_number") || action.source_ref || "";
  const recommendation =
    actionPayloadText(action, "recommendation") || action.description || "Проверить источник и устранить причину.";
  const correctionSystem = actionPayloadText(action, "correction_system") || action.source_system;
  const correctionDocument = actionPayloadText(action, "correction_document");
  const correctionField = actionPayloadText(action, "correction_field");
  const managementStage = actionPayloadText(action, "management_stage_label");
  const deadlineDate = actionPayloadText(action, "deadline_date");
  const daysOverdue = actionPayloadText(action, "days_overdue");
  const responsibleName = actionPayloadText(action, "responsible_name");
  const riskFormula = actionPayloadText(action, "risk_formula");

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const copySourceNumber = async () => {
    if (!sourceNumber) return;
    await navigator.clipboard.writeText(sourceNumber);
    setCopied(true);
  };

  return (
    <div className="executive-action-detail__overlay" onMouseDown={onClose} role="presentation">
      <section
        aria-labelledby="executive-action-detail-title"
        aria-modal="true"
        className="executive-action-detail"
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
      >
        <header>
          <div>
            <span>{DOMAIN_LABELS[action.domain] || action.domain}</span>
            <h2 id="executive-action-detail-title">{action.title}</h2>
          </div>
          <button aria-label="Закрыть карточку решения" onClick={onClose} type="button">×</button>
        </header>

        <dl>
          {sourceNumber && <><dt>Номер заказа</dt><dd>{sourceNumber}</dd></>}
          <dt>Система факта</dt><dd>{correctionSystem}</dd>
          {correctionDocument && <><dt>Документ</dt><dd>{correctionDocument}</dd></>}
          {correctionField && <><dt>Поле</dt><dd>{correctionField}</dd></>}
          {managementStage && <><dt>Этап</dt><dd>{managementStage}</dd></>}
          {deadlineDate && <><dt>Расчётный срок</dt><dd>{formatDate(deadlineDate)}</dd></>}
          {daysOverdue && <><dt>Просрочка</dt><dd>{daysOverdue} дн.</dd></>}
          {responsibleName && <><dt>Ответственный</dt><dd>{responsibleName}</dd></>}
          {action.amount && <><dt>Сумма</dt><dd>{formatMoney(action.amount, action.currency)}</dd></>}
        </dl>

        {riskFormula && <div className="executive-action-detail__formula"><strong>Почему это риск</strong><p>{riskFormula}</p></div>}

        <div className="executive-action-detail__instruction">
          <strong>Что сделать</strong>
          <p>{recommendation}</p>
          {action.domain === "procurement_import" && (
            <small>Действие исчезнет после следующего обновления, когда исправление подтвердится в 1С.</small>
          )}
        </div>

        <footer>
          {sourceNumber && (
            <button className="btn btn--secondary" onClick={copySourceNumber} type="button">
              {copied ? "Номер скопирован" : "Скопировать номер для 1С"}
            </button>
          )}
          {action.drilldown_url && (
            <a className="btn btn--primary" href={action.drilldown_url} rel="noreferrer" target="_blank">
              Открыть источник
            </a>
          )}
          <button className="btn btn--ghost" onClick={onClose} type="button">Закрыть</button>
        </footer>
      </section>
    </div>
  );
}

export function ExecutiveDashboard({ bitrixMode, bitrixUserName, accessLevel }: ExecutiveDashboardProps) {
  const [date, setDate] = useState(readInitialDashboardDate);
  const [tab, setTab] = useState(readInitialDashboardTab);
  const [historyDepth, setHistoryDepth] = useState(0);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [message, setMessage] = useState("");
  const [data, setData] = useState<ExecutiveDashboardResponse | null>(null);
  const [actions, setActions] = useState<ExecutiveDashboardAction[]>([]);
  const [selectedAction, setSelectedAction] = useState<ExecutiveDashboardAction | null>(null);
  const [refreshNonce, setRefreshNonce] = useState(0);

  const [profitLossDateFrom, setProfitLossDateFrom] = useState(() => monthStartIso(date));
  const [profitLossDateTo, setProfitLossDateTo] = useState(date);
  const [profitLossFiltersForDate, setProfitLossFiltersForDate] = useState(date);
  if (date !== profitLossFiltersForDate) {
    setProfitLossFiltersForDate(date);
    setProfitLossDateFrom(monthStartIso(date));
    setProfitLossDateTo(date);
  }

  const [salesDateFrom, setSalesDateFrom] = useState(() => monthStartIso(date));
  const [salesDateTo, setSalesDateTo] = useState(() => monthEndIso(date));
  const [salesStoreRef, setSalesStoreRef] = useState("");
  const [salesManagerRef, setSalesManagerRef] = useState("");
  const [salesStatus, setSalesStatus] = useState<"loading" | "ready" | "error">("loading");
  const [salesMessage, setSalesMessage] = useState("");
  const [salesData, setSalesData] = useState<ExecutiveSalesPeriodResponse | null>(null);
  const [salesFiltersForDate, setSalesFiltersForDate] = useState(date);
  if (date !== salesFiltersForDate) {
    setSalesFiltersForDate(date);
    setSalesDateFrom(monthStartIso(date));
    setSalesDateTo(monthEndIso(date));
    setSalesStoreRef("");
    setSalesManagerRef("");
  }

  const [onlineStoreDateFrom, setOnlineStoreDateFrom] = useState(() => monthStartIso(date));
  const [onlineStoreDateTo, setOnlineStoreDateTo] = useState(date);
  const [onlineStoreStatus, setOnlineStoreStatus] = useState<"loading" | "ready" | "error">("loading");
  const [onlineStoreMessage, setOnlineStoreMessage] = useState("");
  const [onlineStoreData, setOnlineStoreData] = useState<ExecutiveOnlineStorePeriodResponse | null>(null);
  const [onlineStoreFiltersForDate, setOnlineStoreFiltersForDate] = useState(date);
  if (date !== onlineStoreFiltersForDate) {
    setOnlineStoreFiltersForDate(date);
    setOnlineStoreDateFrom(monthStartIso(date));
    setOnlineStoreDateTo(date);
  }

  const setSalesQuickRange = (days: number) => {
    setSalesDateTo(date);
    setSalesDateFrom(addDaysIso(date, -(days - 1)));
    setSalesStoreRef("");
    setSalesManagerRef("");
  };
  const setSalesFullMonth = () => {
    setSalesDateFrom(monthStartIso(date));
    setSalesDateTo(monthEndIso(date));
    setSalesStoreRef("");
    setSalesManagerRef("");
  };
  const setOnlineStoreQuickRange = (days: number) => {
    setOnlineStoreDateTo(date);
    setOnlineStoreDateFrom(addDaysIso(date, -(days - 1)));
  };
  const setOnlineStoreCurrentMonth = () => {
    setOnlineStoreDateFrom(monthStartIso(date));
    setOnlineStoreDateTo(date);
  };
  const setProfitLossQuickRange = (days: number) => {
    setProfitLossDateTo(date);
    setProfitLossDateFrom(addDaysIso(date, -(days - 1)));
  };
  const setProfitLossCurrentMonth = () => {
    setProfitLossDateFrom(monthStartIso(date));
    setProfitLossDateTo(date);
  };

  useEffect(() => {
    if (tab !== SALES_TAB_KEY) return;
    let cancelled = false;
    Promise.resolve().then(() => {
      if (!cancelled) setSalesStatus("loading");
    });
    fetchExecutiveSalesPeriod({
      date_from: salesDateFrom,
      date_to: salesDateTo,
      store_ref: salesStoreRef || undefined,
      manager_ref: salesManagerRef || undefined,
    })
      .then((payload) => {
        if (cancelled) return;
        setSalesData(payload);
        setSalesStatus("ready");
        setSalesMessage("");
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setSalesStatus("error");
        setSalesMessage(errorMessage(error));
      });
    return () => {
      cancelled = true;
    };
  }, [tab, salesDateFrom, salesDateTo, salesStoreRef, salesManagerRef, refreshNonce]);

  useEffect(() => {
    if (tab !== ONLINE_STORE_TAB_KEY) return;
    let cancelled = false;
    Promise.resolve().then(() => {
      if (!cancelled) setOnlineStoreStatus("loading");
    });
    fetchExecutiveOnlineStorePeriod({
      date_from: onlineStoreDateFrom,
      date_to: onlineStoreDateTo,
    })
      .then((payload) => {
        if (cancelled) return;
        setOnlineStoreData(payload);
        setOnlineStoreStatus("ready");
        setOnlineStoreMessage("");
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setOnlineStoreStatus("error");
        setOnlineStoreMessage(errorMessage(error));
      });
    return () => {
      cancelled = true;
    };
  }, [tab, onlineStoreDateFrom, onlineStoreDateTo, refreshNonce]);

  const navigateDashboard = useCallback(
    (next: { date?: string; tab?: string }, mode: "push" | "replace" = "push") => {
      const nextDate = next.date || date;
      const nextTab = next.tab || tab;
      const changed = nextDate !== date || nextTab !== tab;
      if (!changed && mode === "push") return;
      setDate(nextDate);
      setTab(nextTab);
      updateDashboardHistory(nextDate, nextTab, mode);
      if (mode === "push" && changed) setHistoryDepth((value) => value + 1);
    },
    [date, tab]
  );

  useEffect(() => {
    updateDashboardHistory(readInitialDashboardDate(), readInitialDashboardTab(), "replace");
    const handlePopState = () => {
      setDate(readInitialDashboardDate());
      setTab(readInitialDashboardTab());
      setHistoryDepth((value) => Math.max(0, value - 1));
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const navigateBack = useCallback(() => {
    if (historyDepth > 0) {
      window.history.back();
      return;
    }
    navigateDashboard({ tab: "today" }, "replace");
  }, [historyDepth, navigateDashboard]);

  useEffect(() => {
    let cancelled = false;
    Promise.resolve().then(() => {
      if (!cancelled) setStatus("loading");
    });
    fetchExecutiveDashboard(date)
      .then((dashboard) => {
        const tabAllowed = isTabAllowed(dashboard, tab);
        const effectiveTab = tabAllowed ? tab : "today";
        if (!tabAllowed && !cancelled) navigateDashboard({ tab: "today" }, "replace");
        if (effectiveTab === ONLINE_STORE_TAB_KEY) {
          return [
            dashboard,
            {
              as_of: date,
              freshness_status: "fresh",
              source_status: "ready",
              total_count: 0,
              payload: [] as ExecutiveDashboardAction[],
            },
          ] as const;
        }
        return fetchExecutiveDashboardActions({
          date,
          status: "open",
          domain: actionDomainForTab(effectiveTab),
        }).then((actionList) => [dashboard, actionList] as const);
      })
      .then(([dashboard, actionList]) => {
        if (cancelled) return;
        setData(dashboard);
        setActions(actionList.payload);
        setStatus("ready");
        setMessage("");
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setData(null);
        setActions([]);
        setStatus("error");
        setMessage(errorMessage(error));
      });
    return () => {
      cancelled = true;
    };
  }, [date, navigateDashboard, refreshNonce, tab]);

  const blocks = useMemo(() => visibleBlocks(data, tab), [data, tab]);
  const { metricBlocks, managementBalance } = useMemo(
    () => splitManagementBalanceBlock(blocks),
    [blocks],
  );
  const visibleSourceFreshness = useMemo(() => {
    if (!data) return [];
    const relevantKey = SOURCE_FRESHNESS_KEY_BY_TAB[tab];
    if (!relevantKey) return data.source_freshness;
    return data.source_freshness.filter((source) => source.source_key === relevantKey);
  }, [data, tab]);
  const tabs = useMemo(() => tabsForData(data), [data]);
  const tabOverviewBlock = useMemo(() => {
    if (!data || tab === "today") return null;
    if (tab === ODDS_CASHFLOW_TAB_KEY) return moneyBlock(data);
    if ([PROFIT_LOSS_TAB_KEY, SALES_TAB_KEY, ONLINE_STORE_TAB_KEY, "creditors_payables"].includes(tab)) return null;
    return blocks[0] || null;
  }, [blocks, data, tab]);
  const currentAccess = accessLevel || data?.access_level;
  const refreshDashboard = useCallback(() => setRefreshNonce((value) => value + 1), []);

  return (
    <PageShell aria-busy={status === "loading"} className="app executive">
      <header className="executive__header">
        <div>
          <h1>Единая управленческая витрина</h1>
          <span>
            {bitrixMode ? "Bitrix24" : "Прямая ссылка"}
            {bitrixUserName ? ` · ${bitrixUserName}` : ""}
            {currentAccess === "domain" ? " · доменный доступ" : ""}
          </span>
        </div>
        <div className="executive__controls">
          {![PROFIT_LOSS_TAB_KEY, SALES_TAB_KEY, ONLINE_STORE_TAB_KEY].includes(tab) && (
            <label className="executive__date-field">
              <span>Дата</span>
              <input
                aria-label="Дата управленческой витрины"
                className="app__select executive__date"
                onChange={(event) => navigateDashboard({ date: event.target.value })}
                type="date"
                value={date}
              />
            </label>
          )}
          {tab === PROFIT_LOSS_TAB_KEY && (
            <div className="executive__controls-group">
              <span className="executive__controls-divider" aria-hidden="true" />
              <Button onClick={() => setProfitLossQuickRange(7)} variant="secondary">
                7 дней
              </Button>
              <Button onClick={() => setProfitLossQuickRange(30)} variant="secondary">
                30 дней
              </Button>
              <Button onClick={setProfitLossCurrentMonth} variant="secondary">
                Месяц
              </Button>
              <label className="executive__date-field">
                <span>С</span>
                <input
                  aria-label="Начало периода прибыли и убытков"
                  className="app__select executive__date"
                  onChange={(event) => setProfitLossDateFrom(event.target.value)}
                  type="date"
                  value={profitLossDateFrom}
                />
              </label>
              <label className="executive__date-field">
                <span>По</span>
                <input
                  aria-label="Конец периода прибыли и убытков"
                  className="app__select executive__date"
                  onChange={(event) => setProfitLossDateTo(event.target.value)}
                  type="date"
                  value={profitLossDateTo}
                />
              </label>
            </div>
          )}
          {tab === SALES_TAB_KEY && (
            <div className="executive__controls-group">
              <span className="executive__controls-divider" aria-hidden="true" />
              <Button disabled={salesStatus === "loading"} onClick={() => setSalesQuickRange(7)} variant="secondary">
                7 дней
              </Button>
              <Button disabled={salesStatus === "loading"} onClick={() => setSalesQuickRange(30)} variant="secondary">
                30 дней
              </Button>
              <Button disabled={salesStatus === "loading"} onClick={setSalesFullMonth} variant="secondary">
                Месяц
              </Button>
              <label className="executive__date-field">
                <span>С</span>
                <input
                  aria-label="Начало периода продаж"
                  className="app__select executive__date"
                  onChange={(event) => {
                    setSalesDateFrom(event.target.value);
                    setSalesStoreRef("");
                    setSalesManagerRef("");
                  }}
                  type="date"
                  value={salesDateFrom}
                />
              </label>
              <label className="executive__date-field">
                <span>По</span>
                <input
                  aria-label="Конец периода продаж"
                  className="app__select executive__date"
                  onChange={(event) => {
                    setSalesDateTo(event.target.value);
                    setSalesStoreRef("");
                    setSalesManagerRef("");
                  }}
                  type="date"
                  value={salesDateTo}
                />
              </label>
              <label className="executive__date-field">
                <span>Магазин</span>
                <select
                  aria-label="Магазин"
                  className="app__select executive__controls-select"
                  onChange={(event) => setSalesStoreRef(event.target.value)}
                  value={salesStoreRef}
                >
                  <option value="">Все магазины</option>
                  {(salesData?.stores || []).map((item) => (
                    <option key={item.key} value={item.key}>{item.label}</option>
                  ))}
                </select>
              </label>
              <label className="executive__date-field">
                <span>Менеджер</span>
                <select
                  aria-label="Менеджер"
                  className="app__select executive__controls-select"
                  onChange={(event) => setSalesManagerRef(event.target.value)}
                  value={salesManagerRef}
                >
                  <option value="">Все менеджеры</option>
                  {(salesData?.managers || []).map((item) => (
                    <option key={item.key} value={item.key}>{item.label}</option>
                  ))}
                </select>
              </label>
            </div>
          )}
          {tab === ONLINE_STORE_TAB_KEY && (
            <div className="executive__controls-group">
              <span className="executive__controls-divider" aria-hidden="true" />
              <Button disabled={onlineStoreStatus === "loading"} onClick={() => setOnlineStoreQuickRange(7)} variant="secondary">
                7 дней
              </Button>
              <Button disabled={onlineStoreStatus === "loading"} onClick={() => setOnlineStoreQuickRange(30)} variant="secondary">
                30 дней
              </Button>
              <Button disabled={onlineStoreStatus === "loading"} onClick={setOnlineStoreCurrentMonth} variant="secondary">
                Месяц
              </Button>
              <label className="executive__date-field">
                <span>С</span>
                <input
                  aria-label="Начало периода интернет-магазина"
                  className="app__select executive__date"
                  onChange={(event) => setOnlineStoreDateFrom(event.target.value)}
                  type="date"
                  value={onlineStoreDateFrom}
                />
              </label>
              <label className="executive__date-field">
                <span>По</span>
                <input
                  aria-label="Конец периода интернет-магазина"
                  className="app__select executive__date"
                  onChange={(event) => setOnlineStoreDateTo(event.target.value)}
                  type="date"
                  value={onlineStoreDateTo}
                />
              </label>
            </div>
          )}
          <Button
            disabled={status === "loading"}
            onClick={() => navigateDashboard({ date: todayIso() })}
            variant="secondary"
          >
            Сегодня
          </Button>
          <Button disabled={status === "loading"} onClick={refreshDashboard}>
            {status === "loading" ? "Обновляем..." : "Обновить"}
          </Button>
        </div>
      </header>

      {tab !== "today" && (
        <div className="executive-backbar">
          <button className="btn btn--ghost" onClick={navigateBack} type="button">
            Назад
          </button>
          <span>{tabLabel(tab)}</span>
        </div>
      )}

      <nav className="executive-tabs" aria-label="Разделы витрины">
        {tabs.map((item) => (
          <button
            className={tab === item.key ? "executive-tabs__item executive-tabs__item--active" : "executive-tabs__item"}
            aria-current={tab === item.key ? "page" : undefined}
            key={item.key}
            onClick={() => navigateDashboard({ tab: item.key })}
            type="button"
          >
            {item.label}
          </button>
        ))}
      </nav>

      {status === "error" && (
        <ErrorState
          actionLabel="Повторить загрузку"
          description={message}
          onAction={refreshDashboard}
          title="Витрина не загрузилась"
        />
      )}

      {status === "loading" && !data && <LoadingState title="Загрузка данных..." />}

      {data && (
        <>
          {!["procurement_import", ONLINE_STORE_TAB_KEY].includes(tab) && <section aria-label="Состояние витрины" aria-live="polite" className="executive__topline">
            <div>
              <span>Данные</span>
              <strong>{statusLabel(data.source_status)}</strong>
            </div>
            <div>
              <span>Свежесть</span>
              <strong>{statusLabel(data.freshness_status)}</strong>
            </div>
            <div>
              <span>Собрано</span>
              <strong>{formatDateTime(data.generated_at)}</strong>
            </div>
            <div>
              <span>Решений в фокусе</span>
              <strong>{data.top_actions.length}</strong>
            </div>
          </section>}

          {tab === "today" ? (
            <FlowMap
              activeTab={tab}
              data={data}
              onSelect={(nextTab) => navigateDashboard({ tab: nextTab })}
            />
          ) : (
            tabOverviewBlock &&
            ![SALES_TAB_KEY, "procurement_import"].includes(tab) &&
            <TabKpiOverview block={tabOverviewBlock} data={data} />
          )}

          {tab === PROFIT_LOSS_TAB_KEY && (
            <ProfitLossPeriodPanel
              dateFrom={profitLossDateFrom}
              dateTo={profitLossDateTo}
              refreshNonce={refreshNonce}
            />
          )}

          {tab === SALES_TAB_KEY && (
            <SalesPeriodPanel
              actions={(
                <section className="executive-actions">
                  <header className="executive-actions__header">
                    <div>
                      <h2>{`Решения: ${tabLabel(tab)}`}</h2>
                      <span>5-10 действий с ответственным, сроком и ссылкой на источник</span>
                    </div>
                  </header>
                  <ActionTable actions={actions} onOpen={setSelectedAction} />
                </section>
              )}
              data={salesData}
              message={salesMessage}
              onSelectManager={setSalesManagerRef}
              onSelectStore={setSalesStoreRef}
              status={salesStatus}
            />
          )}

          {tab === ONLINE_STORE_TAB_KEY && (
            <OnlineStorePanel
              data={onlineStoreData}
              message={onlineStoreMessage}
              status={onlineStoreStatus}
            />
          )}

          {tab === "procurement_import" && (
            <ProcurementImportPanel
              actions={actions}
              block={data.blocks.find((block) => block.key === "procurement_import") || null}
              dashboardSourceStatus={data.source_status}
              generatedAt={data.generated_at}
              onOpenAction={setSelectedAction}
            />
          )}

          {tab === ODDS_CASHFLOW_TAB_KEY && <CashflowPeriodPanel asOf={date} />}

          {metricBlocks.length > 0 && tab !== "procurement_import" && (
            <div className="executive-grid">
              {metricBlocks.map((block) => (
                <BlockCard
                  activeTab={tab}
                  bitrixMode={bitrixMode}
                  block={block}
                  date={date}
                  key={block.key}
                />
              ))}
            </div>
          )}

          {managementBalance && (
            <div className="executive-management-balance-section">
              <MonthlyManagementBalance
                canCloseMonth={currentAccess === "full" || data.roles.includes("finance")}
                refreshNonce={refreshNonce}
              />
            </div>
          )}

          {![SALES_TAB_KEY, ONLINE_STORE_TAB_KEY, "procurement_import"].includes(tab) && (
            <section className="executive-actions">
              <header className="executive-actions__header">
                <div>
                  <h2>{tab === "today" ? "Фокус дня" : `Решения: ${tabLabel(tab)}`}</h2>
                  <span>5-10 действий с ответственным, сроком и ссылкой на источник</span>
                </div>
              </header>
              <ActionTable actions={actions} onOpen={setSelectedAction} />
            </section>
          )}

          <SourceFreshness sources={visibleSourceFreshness} />
          {selectedAction && <ActionDetail action={selectedAction} onClose={() => setSelectedAction(null)} />}
        </>
      )}
    </PageShell>
  );
}
