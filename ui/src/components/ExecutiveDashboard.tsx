import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchExecutiveCashflowPeriod,
  closeExecutiveManagementBalance,
  fetchExecutiveDashboard,
  fetchExecutiveDashboardActions,
  fetchExecutiveManagementBalance,
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
  type ExecutiveManagementBalanceView,
  type ExecutiveProfitLossBreakdownRow,
  type ExecutiveProfitLossExpenseBreakdownRow,
  type ExecutiveProfitLossOpenQuestion,
  type ExecutiveProfitLossPeriodResponse,
  type ExecutiveProfitLossRatio,
  type ExecutiveSalesBreakdownRow,
  type ExecutiveSalesFilterOption,
  type ExecutiveSalesMonthlyRow,
  type ExecutiveSalesPeriodResponse,
  type ExecutiveSourceStatus,
} from "../api/executiveDashboard";
import { splitManagementBalanceBlock } from "./executiveDashboardLayout";
import { Button, ErrorState, LoadingState, PageShell } from "./ui";

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
};

const MONEY_TAB_KEY = "money_today";
const PROFIT_LOSS_TAB_KEY = "profit_loss";
const SALES_TAB_KEY = "sales";
const ODDS_CASHFLOW_TAB_KEY = "odds_cashflow";

const TAB_DEFINITIONS = [
  { key: "today", label: "Сегодня" },
  { key: MONEY_TAB_KEY, label: "Деньги / ДДС" },
  { key: PROFIT_LOSS_TAB_KEY, label: "Прибыли / убытки" },
  { key: SALES_TAB_KEY, label: "Продажи" },
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

const FLOW_STEPS = [
  { key: "money_today", label: "Деньги / ДДС", metricKeys: ["cash_position_total_balance", "cashflow_inflow_amount"] },
  { key: "profit_loss", label: "Прибыли / убытки", metricKeys: ["gross_profit", "gross_margin_pct"] },
  { key: "sales", label: "Продажи", metricKeys: ["revenue_mtd", "forecast_revenue_month_end"] },
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
  return value.toISOString().slice(0, 10);
}

function addDaysIso(value: string, days: number) {
  const parsed = new Date(`${value}T00:00:00`);
  parsed.setDate(parsed.getDate() + days);
  return isoFromDate(parsed);
}

function monthStartIso(value: string) {
  return `${value.slice(0, 8)}01`;
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
    complete: "месяц закрыт",
  };
  return labels[status] || status;
}

function severityLabel(value: string) {
  const labels: Record<string, string> = {
    critical: "критично",
    high: "важно",
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
  return data.blocks.some((block) => block.key === tab);
}

function actionDomainForTab(tab: string) {
  if (tab === "today") return undefined;
  if (tab === ODDS_CASHFLOW_TAB_KEY) return MONEY_TAB_KEY;
  return tab;
}

function visibleBlocks(data: ExecutiveDashboardResponse | null, tab: string) {
  if (!data) return [];
  if (tab === "today") return data.blocks.filter((block) => block.key !== "daily_focus");
  if (tab === ODDS_CASHFLOW_TAB_KEY) return [];
  return data.blocks.filter((block) => block.key === tab);
}

function tabsForData(data: ExecutiveDashboardResponse | null) {
  if (!data) return TAB_DEFINITIONS;
  const availableKeys = new Set(data.blocks.map((block) => block.key));
  return TAB_DEFINITIONS.filter(
    (item) =>
      item.key === "today" ||
      availableKeys.has(item.key) ||
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
          "cash_position_cashbox_balance_total",
          "cash_position_card_balance_total",
          "cash_position_other_balance_total",
        ]
      : [
          "cash_position_total_balance",
          "cash_position_bank_balance_total",
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
              {line.source_as_of ? `на ${formatDate(line.source_as_of)}` : statusLabel(line.source_status)}
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

function MonthlyManagementBalance({
  asOf,
  refreshNonce,
  canCloseMonth,
}: {
  asOf: string;
  refreshNonce: number;
  canCloseMonth: boolean;
}) {
  const [balance, setBalance] = useState<ExecutiveManagementBalanceResponse | null>(null);
  const [month, setMonth] = useState<string | undefined>();
  const [view, setView] = useState<ExecutiveManagementBalanceView | undefined>();
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");

  const load = useCallback(async (nextMonth?: string, nextView?: ExecutiveManagementBalanceView) => {
    setMonth(nextMonth);
    setView(nextView);
    setLoading(true);
    setMessage("");
    setBalance(null);
    try {
      const payload = await fetchExecutiveManagementBalance({ month: nextMonth, view: nextView });
      setBalance(payload);
      setMonth(payload.month);
      setView(payload.view);
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
    const selectedMonth = asOf.slice(0, 7);
    const selectedView: ExecutiveManagementBalanceView =
      selectedMonth === todayIso().slice(0, 7) ? "operational" : "closed";
    let cancelled = false;
    Promise.resolve().then(() => {
      if (!cancelled) load(selectedMonth, selectedView);
    });
    return () => {
      cancelled = true;
    };
  }, [asOf, load, refreshNonce]);

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

  return (
    <section className={`executive-block executive-block--management-balance executive-block--${balance?.source_status || "source_missing"}`}>
      <header className="executive-block__header executive-management-balance__header">
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
        <div className="executive-management-balance__controls">
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
            <div><span>Активы</span><strong>{formatMoney(balance.assets_total)}</strong></div>
            <div><span>Пассивы</span><strong>{formatMoney(balance.liabilities_and_equity_total)}</strong></div>
            <div className={Number(balance.imbalance_amount) === 0 ? "is-balanced" : "is-unbalanced"}>
              <span>Расхождение</span><strong>{formatSignedMoney(balance.imbalance_amount)}</strong>
            </div>
          </div>
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
      <header className="executive-cashflow-period__header">
        <div>
          <h2>ОДДС CashFlow</h2>
          <span>Форма для финансистов: остатки, CFO / CFI / CFF, FCF и контроль движения денег</span>
        </div>
        <div className="executive-cashflow-period__filters">
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
          <div className="executive-cashflow-period__kpis">
            <div>
              <span>Чистый поток без внутренних переводов</span>
              <strong>{formatMoney(cashflowTotal(data, "external_net_amount"))}</strong>
              <small>без внутренних переводов</small>
            </div>
            <div>
              <span>Поступило</span>
              <strong>{formatMoney(cashflowTotal(data, "external_inflow_amount"))}</strong>
              <small>внешний поток</small>
            </div>
            <div>
              <span>Списано</span>
              <strong>{formatMoney(cashflowTotal(data, "external_outflow_amount"))}</strong>
              <small>внешний поток</small>
            </div>
            <div>
              <span>{daysOnHand?.label || "Дней запаса"}</span>
              <strong>{formatMetricValue(daysOnHand?.value, daysOnHand?.unit)}</strong>
              <small>{coverage ? `покрытие: ${formatMetricValue(coverage.value, coverage.unit)}` : "остаток / расход"}</small>
            </div>
            <div>
              <span>Ошибки ДДС</span>
              <strong>{formatPlainNumber(cashflowTotal(data, "quality_issue_count"))}</strong>
              <small>{reviewShare ? `строк на проверку: ${formatMetricValue(reviewShare.value, reviewShare.unit)}` : "контроль качества"}</small>
            </div>
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
                    <small>{formatDate(issue.business_date)}</small>
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

function ProfitLossPeriodPanel({ asOf }: { asOf: string }) {
  const [dateFrom, setDateFrom] = useState(monthStartIso(asOf));
  const [dateTo, setDateTo] = useState(asOf);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [message, setMessage] = useState("");
  const [data, setData] = useState<ExecutiveProfitLossPeriodResponse | null>(null);

  useEffect(() => {
    setDateFrom(monthStartIso(asOf));
    setDateTo(asOf);
  }, [asOf]);

  useEffect(() => {
    let cancelled = false;
    setStatus("loading");
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
  }, [dateFrom, dateTo]);

  const grossMargin = profitLossRatioByKey(data, "gross_margin_pct");
  const operatingMargin = profitLossRatioByKey(data, "operating_margin_pct");
  const maxDailyValue = Math.max(
    ...((data?.daily || []).map((row) =>
      Math.max(Math.abs(Number(row.revenue) || 0), Math.abs(Number(row.gross_profit) || 0))
    )),
    1
  );
  const expenseHasAmounts = Boolean(
    data && !["source_missing", "source_error"].includes(data.expense_source_status)
  );

  const setQuickRange = (days: number) => {
    setDateTo(asOf);
    setDateFrom(addDaysIso(asOf, -(days - 1)));
  };

  return (
    <section className="executive-cashflow-period executive-profit-loss-period" aria-label="Отчет о прибылях и убытках за период">
      <header className="executive-cashflow-period__header">
        <div>
          <h2>Отчет о прибылях и убытках</h2>
          <span>Выручка, себестоимость, валовая прибыль и расходы по оплатам ДДС</span>
        </div>
        <div className="executive-cashflow-period__filters">
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
        </div>
      </header>

      {status === "error" && <div className="executive-cashflow-period__empty">{message}</div>}
      {status === "loading" && <div className="executive-cashflow-period__empty">Загрузка ОПУ...</div>}
      {status === "ready" && data && (
        <>
          {data.note && <div className="executive-cashflow-period__note">{data.note}</div>}
          <div className="executive-cashflow-period__kpis">
            <div>
              <span>Выручка</span>
              <strong>{formatMoney(profitLossTotal(data, "revenue"))}</strong>
              <small>{formatDate(data.date_from)} - {formatDate(data.date_to)}</small>
            </div>
            <div>
              <span>Себестоимость</span>
              <strong>{formatMoney(profitLossTotal(data, "cost_of_sales"))}</strong>
              <small>из 1С продаж</small>
            </div>
            <div>
              <span>Валовая прибыль</span>
              <strong>{formatMoney(profitLossTotal(data, "gross_profit"))}</strong>
              <small>выручка минус себестоимость</small>
            </div>
            <div>
              <span>{grossMargin?.label || "Валовая маржа"}</span>
              <strong>{formatMetricValue(grossMargin?.value, grossMargin?.unit)}</strong>
              <small>{formatPlainNumber(profitLossTotal(data, "sales_count"))} продаж</small>
            </div>
            <div>
              <span>Расходы по ДДС</span>
              <strong>
                {!expenseHasAmounts
                  ? statusLabel(data.expense_source_status)
                  : formatMoney(profitLossTotal(data, "operating_expenses"))}
              </strong>
              <small>{statusLabel(data.expense_source_status)}</small>
            </div>
            <div>
              <span>Операционная прибыль</span>
              <strong>
                {!expenseHasAmounts
                  ? statusLabel(data.expense_source_status)
                  : formatMoney(profitLossTotal(data, "operating_profit"))}
              </strong>
              <small>{formatMetricValue(operatingMargin?.value, operatingMargin?.unit)}</small>
            </div>
            <div>
              <span>Открытые вопросы</span>
              <strong>{formatPlainNumber(profitLossTotal(data, "expense_open_question_count"))}</strong>
              <small>{formatMoney(profitLossTotal(data, "expense_open_question_amount"))}</small>
            </div>
          </div>

          <section className="executive-profit-loss-lines" aria-label="Структура ОПУ">
            <header>
              <h3>Структура ОПУ</h3>
              <span>{statusLabel(data.source_status)}</span>
            </header>
            <div className="executive-profit-loss-lines__rows">
              {data.lines.map((line) => (
                <div
                  className={[
                    "executive-profit-loss-line",
                    `executive-profit-loss-line--${line.source_status}`,
                    `executive-profit-loss-line--${line.line_type}`,
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  key={line.key}
                >
                  <span>{line.label}</span>
                  <strong>{formatProfitLossAmount(line.amount)}</strong>
                  <small>{line.note || statusLabel(line.source_status)}</small>
                </div>
              ))}
            </div>
          </section>

          <div className="executive-cashflow-period__chart" aria-label="Динамика ОПУ по дням">
            {data.daily.slice(-31).map((row) => {
              const revenueWidth = `${Math.max(3, Math.round(((Number(row.revenue) || 0) / maxDailyValue) * 100))}%`;
              const profitWidth = `${Math.max(3, Math.round((Math.abs(Number(row.gross_profit) || 0) / maxDailyValue) * 100))}%`;
              return (
                <div className="executive-cashflow-day" key={row.business_date}>
                  <span>{formatDate(row.business_date)}</span>
                  <div>
                    <i style={{ width: revenueWidth }} />
                    <b style={{ width: profitWidth }} />
                  </div>
                  <strong>{formatMoney(row.gross_profit)}</strong>
                </div>
              );
            })}
          </div>

          <div className="executive-cashflow-period__tables">
            <div>
              <h3>По магазинам</h3>
              {data.by_store.length === 0 ? (
                <div className="executive-cashflow-period__empty">Нет продаж в выбранном периоде.</div>
              ) : (
                data.by_store.slice(0, 6).map((row) => (
                  <div className="executive-cashflow-row" key={row.key}>
                    <span>{row.label}</span>
                    <strong>{formatMoney(row.gross_profit)}</strong>
                    <small>{profitLossRowDetail(row)}</small>
                  </div>
                ))
              )}
            </div>
            <div>
              <h3>По менеджерам</h3>
              {data.by_manager.length === 0 ? (
                <div className="executive-cashflow-period__empty">Нет продаж в выбранном периоде.</div>
              ) : (
                data.by_manager.slice(0, 6).map((row) => (
                  <div className="executive-cashflow-row" key={row.key}>
                    <span>{row.label}</span>
                    <strong>{formatMoney(row.gross_profit)}</strong>
                    <small>{profitLossRowDetail(row)}</small>
                  </div>
                ))
              )}
            </div>
            <div>
              <h3>Расходы по ДДС</h3>
              {data.expense_breakdown.length === 0 ? (
                <div className="executive-cashflow-period__empty">
                  Нет подтвержденных операционных расходов по ДДС.
                </div>
              ) : (
                data.expense_breakdown.slice(0, 8).map((row) => (
                  <div className="executive-cashflow-row" key={row.key}>
                    <span>{row.label}</span>
                    <strong>{formatMoney(row.amount)}</strong>
                    <small>{profitLossExpenseDetail(row)}</small>
                  </div>
                ))
              )}
            </div>
            <div>
              <h3>Открытые вопросы</h3>
              {data.expense_open_questions.length === 0 ? (
                data.lines
                  .filter((line) => line.source_status !== "ready")
                  .slice(0, 5)
                  .map((line) => (
                    <div className="executive-cashflow-row" key={line.key}>
                      <span>{line.label}</span>
                      <strong>{statusLabel(line.source_status)}</strong>
                      <small>{line.note || "Требуется источник"}</small>
                    </div>
                  ))
              ) : (
                data.expense_open_questions.slice(0, 6).map((row) => (
                  <div className="executive-cashflow-row" key={row.key}>
                    <span>{row.label}</span>
                    <strong>{formatMoney(row.amount)}</strong>
                    <small>{profitLossQuestionDetail(row)}</small>
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

function salesValue(
  data: ExecutiveSalesPeriodResponse | null,
  key: string,
  scope: "totals" | "comparison" = "totals"
) {
  const value = data?.[scope]?.[key];
  return value === null || value === undefined ? null : value;
}

function salesChangeLabel(
  current: string | number | null,
  previous: string | number | null,
  options?: { percentagePoints?: boolean }
) {
  const currentValue = numericValue(current);
  const previousValue = numericValue(previous);
  if (currentValue === null || previousValue === null || previousValue === 0) {
    return "нет сопоставимой базы";
  }
  if (options?.percentagePoints) {
    const points = (currentValue - previousValue) * 100;
    const sign = points > 0 ? "+" : "";
    return `${sign}${points.toFixed(1)} п.п. к прошлому месяцу`;
  }
  const delta = ((currentValue - previousValue) / Math.abs(previousValue)) * 100;
  const sign = delta > 0 ? "+" : "";
  return `${sign}${delta.toFixed(1)}% к прошлому месяцу`;
}

export function SalesBreakdown({
  title,
  rows,
  onSelect,
}: {
  title: string;
  rows: ExecutiveSalesBreakdownRow[];
  onSelect: (key: string) => void;
}) {
  return (
    <div>
      <h3>{title}</h3>
      {rows.length === 0 ? (
        <div className="executive-cashflow-period__empty">Нет продаж в выбранном периоде.</div>
      ) : (
        rows.slice(0, 8).map((row) => (
          <button className="executive-sales-breakdown" key={row.key} onClick={() => onSelect(row.key)} type="button">
            <span>{row.label}</span>
            <strong>{formatMoney(row.revenue)}</strong>
            <small>
              {formatPlainNumber(row.sales_count)} ед. · {formatPercent(row.gross_margin_pct)}
            </small>
          </button>
        ))
      )}
    </div>
  );
}

function formatMonth(value: string) {
  const parsed = new Date(`${value}-01T00:00:00`);
  return Number.isNaN(parsed.getTime())
    ? value
    : new Intl.DateTimeFormat("ru-RU", { month: "short", year: "2-digit" }).format(parsed);
}

function SalesLineChart({ monthly }: { monthly: ExecutiveSalesMonthlyRow[] }) {
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
  const valueRange = Math.max(maxValue - minValue, 1);
  const pointFor = (index: number, value: number) => ({
    x: padding.left + (monthly.length <= 1 ? 0 : (index / (monthly.length - 1)) * chartWidth),
    y: padding.top + ((maxValue - value) / valueRange) * chartHeight,
  });
  const pathFor = (points: Array<{ x: number; y: number }>) =>
    points.map((point, index) => `${index === 0 ? "M" : "L"}${point.x} ${point.y}`).join(" ");
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

  return (
    <div className="executive-sales-line-chart" aria-label="Помесячная динамика продаж за год">
      <div className="executive-sales-period__legend">
        <span><i />Выручка</span>
        <span><em />Валовая прибыль</span>
        <span><b />Прогноз текущего месяца</span>
      </div>
      <svg role="img" viewBox={`0 0 ${width} ${height}`}>
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
        {revenuePoints.map((point) => (
          <circle className="executive-sales-line-chart__fact-point" cx={point.x} cy={point.y} key={`revenue-${point.row.month}`} r="3.5">
            <title>{`${formatMonth(point.row.month)}: выручка ${formatMoney(point.value)}`}</title>
          </circle>
        ))}
        {grossProfitPoints.map((point) => (
          <circle className="executive-sales-line-chart__profit-point" cx={point.x} cy={point.y} key={`profit-${point.row.month}`} r="3.5">
            <title>{`${formatMonth(point.row.month)}: валовая прибыль ${formatMoney(point.value)}`}</title>
          </circle>
        ))}
        {forecastPoints.map((point) => (
          <circle className="executive-sales-line-chart__forecast-point" cx={point.x} cy={point.y} key={`forecast-${point.row.month}`} r="5">
            <title>{`${formatMonth(point.row.month)}: прогноз ${formatMoney(point.value)}`}</title>
          </circle>
        ))}
      </svg>
    </div>
  );
}

function SalesVolumeBars({ monthly }: { monthly: ExecutiveSalesMonthlyRow[] }) {
  const volumes = monthly.map((row) => numericValue(row.sales_count) || 0);
  const maxVolume = Math.max(1, ...volumes);

  return (
    <section className="executive-sales-volume-chart" aria-label="Помесячный объём продаж">
      <header>
        <div>
          <h3>Объём продаж</h3>
          <span>единиц товаров и услуг</span>
        </div>
        <strong>{formatPlainNumber(maxVolume)} ед. максимум</strong>
      </header>
      <div className="executive-sales-volume-chart__scroll">
        <div className="executive-sales-volume-chart__bars">
          {monthly.map((row, index) => {
            const volume = volumes[index] || 0;
            const height = volume === 0 ? 0 : Math.max(5, Math.round((volume / maxVolume) * 100));
            const label = `${formatMonth(row.month)}: объём продаж ${formatPlainNumber(volume)} ед.`;
            return (
              <div className="executive-sales-volume-chart__bar" key={row.month} title={label}>
                <div className="executive-sales-volume-chart__track">
                  <i aria-hidden="true" style={{ height: `${height}%` }} />
                </div>
                <strong>{formatPlainNumber(volume)}</strong>
                <span>{formatMonth(row.month)}</span>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}

export function SalesPeriodPanel({ asOf }: { asOf: string }) {
  const [month, setMonth] = useState(monthStartIso(asOf).slice(0, 7));
  const [storeRef, setStoreRef] = useState("");
  const [managerRef, setManagerRef] = useState("");
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [message, setMessage] = useState("");
  const [data, setData] = useState<ExecutiveSalesPeriodResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.resolve().then(() => {
      if (!cancelled) setStatus("loading");
    });
    fetchExecutiveSalesPeriod({
      month,
      store_ref: storeRef || undefined,
      manager_ref: managerRef || undefined,
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
  }, [managerRef, month, storeRef]);

  const stores: ExecutiveSalesFilterOption[] = data?.stores || [];
  const managers: ExecutiveSalesFilterOption[] = data?.managers || [];
  const actualRevenue = salesValue(data, "revenue_mtd");
  const grossProfit = salesValue(data, "gross_profit_mtd");
  const grossMargin = salesValue(data, "gross_margin_pct_mtd");
  const salesCount = salesValue(data, "sales_count_mtd");
  const forecastRevenue = salesValue(data, "forecast_revenue_month_end");

  return (
    <section className="executive-cashflow-period executive-sales-period" aria-label="Продажи">
      <header className="executive-cashflow-period__header">
        <div>
          <h2>Продажи</h2>
          <span>Факт 1С и прогноз выручки до конца месяца</span>
        </div>
        <div className="executive-cashflow-period__filters">
          <label>
            <span>Месяц</span>
            <input
              aria-label="Месяц продаж"
              onChange={(event) => {
                setMonth(event.target.value);
                setStoreRef("");
                setManagerRef("");
              }}
              type="month"
              value={month}
            />
          </label>
          <label>
            <span>Магазин</span>
            <select aria-label="Магазин" onChange={(event) => setStoreRef(event.target.value)} value={storeRef}>
              <option value="">Все магазины</option>
              {stores.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}
            </select>
          </label>
          <label>
            <span>Менеджер</span>
            <select aria-label="Менеджер" onChange={(event) => setManagerRef(event.target.value)} value={managerRef}>
              <option value="">Все менеджеры</option>
              {managers.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}
            </select>
          </label>
        </div>
      </header>

      {status === "error" && <div className="executive-cashflow-period__empty">{message}</div>}
      {status === "loading" && !data && <div className="executive-cashflow-period__empty">Загрузка продаж...</div>}
      {data && (
        <>
          {data.note && <div className="executive-cashflow-period__note">{data.note}</div>}
          {data.forecast_note && <div className="executive-sales-period__forecast-note">{data.forecast_note}</div>}
          <div className="executive-cashflow-period__kpis executive-sales-period__kpis">
            <div>
              <span>Выручка факт</span>
              <strong>{formatNullableMoney(numericValue(actualRevenue))}</strong>
              <small>{salesChangeLabel(actualRevenue, salesValue(data, "revenue"))}</small>
            </div>
            <div>
              <span>Прогноз выручки</span>
              <strong>{forecastRevenue === null ? "нет данных" : formatMoney(forecastRevenue)}</strong>
              <small>{data.forecast_status === "ready" ? "на конец месяца" : statusLabel(data.forecast_status)}</small>
            </div>
            <div>
              <span>Валовая прибыль</span>
              <strong>{formatNullableMoney(numericValue(grossProfit))}</strong>
              <small>{salesChangeLabel(grossProfit, salesValue(data, "gross_profit"))}</small>
            </div>
            <div>
              <span>Валовая маржа</span>
              <strong>{formatPercent(grossMargin)}</strong>
              <small>{salesChangeLabel(grossMargin, salesValue(data, "gross_margin_pct"), { percentagePoints: true })}</small>
            </div>
            <div>
              <span>Объём продаж</span>
              <strong>{formatPlainNumber(salesCount)} ед.</strong>
              <small>{salesChangeLabel(salesCount, salesValue(data, "sales_count"))}</small>
            </div>
          </div>

          {data.monthly.length > 0 && <SalesLineChart monthly={data.monthly} />}
          {data.monthly.length > 0 && <SalesVolumeBars monthly={data.monthly} />}

          <div className="executive-cashflow-period__tables executive-sales-period__tables">
            <SalesBreakdown title="По магазинам" rows={data.by_store} onSelect={setStoreRef} />
            <SalesBreakdown title="По менеджерам" rows={data.by_manager} onSelect={setManagerRef} />
          </div>
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
          {action.amount && <><dt>Сумма</dt><dd>{formatMoney(action.amount, action.currency)}</dd></>}
        </dl>

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
  const tabs = useMemo(() => tabsForData(data), [data]);
  const tabOverviewBlock = useMemo(() => {
    if (!data || tab === "today") return null;
    if (tab === ODDS_CASHFLOW_TAB_KEY) return moneyBlock(data);
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
          <section aria-label="Состояние витрины" aria-live="polite" className="executive__topline">
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
          </section>

          {tab === "today" ? (
            <FlowMap
              activeTab={tab}
              data={data}
              onSelect={(nextTab) => navigateDashboard({ tab: nextTab })}
            />
          ) : (
            tabOverviewBlock && <TabKpiOverview block={tabOverviewBlock} data={data} />
          )}

          {tab === PROFIT_LOSS_TAB_KEY && <ProfitLossPeriodPanel asOf={date} />}

          {tab === SALES_TAB_KEY && <SalesPeriodPanel asOf={date} key={date} />}

          {tab === ODDS_CASHFLOW_TAB_KEY && <CashflowPeriodPanel asOf={date} />}

          {metricBlocks.length > 0 && (
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
                asOf={date}
                canCloseMonth={currentAccess === "full" || data.roles.includes("finance")}
                refreshNonce={refreshNonce}
              />
            </div>
          )}

          <section className="executive-actions">
            <header className="executive-actions__header">
              <div>
                <h2>{tab === "today" ? "Фокус дня" : `Решения: ${tabLabel(tab)}`}</h2>
                <span>5-10 действий с ответственным, сроком и ссылкой на источник</span>
              </div>
            </header>
            <ActionTable actions={actions} onOpen={setSelectedAction} />
          </section>

          <SourceFreshness sources={data.source_freshness} />
          {selectedAction && <ActionDetail action={selectedAction} onClose={() => setSelectedAction(null)} />}
        </>
      )}
    </PageShell>
  );
}
