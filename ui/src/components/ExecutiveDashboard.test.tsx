import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ExecutiveDashboardAction, ExecutiveDashboardBlock, ExecutiveDashboardResponse } from "../api/executiveDashboard";
import {
  fetchExecutiveDashboard,
  fetchExecutiveDashboardActions,
  fetchExecutiveManagementBalance,
  fetchExecutiveSalesPeriod,
} from "../api/executiveDashboard";

vi.mock("../api/executiveDashboard", () => ({
  closeExecutiveManagementBalance: vi.fn(),
  fetchExecutiveCashflowPeriod: vi.fn(),
  fetchExecutiveDashboard: vi.fn(),
  fetchExecutiveDashboardActions: vi.fn(),
  fetchExecutiveManagementBalance: vi.fn(),
  fetchExecutiveProfitLossPeriod: vi.fn(),
  fetchExecutiveSalesPeriod: vi.fn(),
}));

import {
  ActionDetail,
  ActionTable,
  ExecutiveDashboard,
  ManagementBalanceBlockCard,
  MonthlyManagementBalance,
} from "./ExecutiveDashboard";
import { splitManagementBalanceBlock } from "./executiveDashboardLayout";

function action(index: number): ExecutiveDashboardAction {
  return {
    stable_key: `procurement:${index}`,
    business_date: "2026-07-11",
    domain: "procurement_import",
    severity: "high",
    title: `Заказ РБГУ${String(index).padStart(4, "0")}: заполнить «Сдача в карго»`,
    amount: "1000.00",
    currency: "RUB",
    status: "open",
    source_system: "1C",
    source_ref: `0x${index}`,
    dedupe_key: `procurement:${index}`,
    payload: {
      onec_source_number: `РБГУ${String(index).padStart(4, "0")}`,
      correction_system: "1C",
      correction_document: "Заказ поставщику",
      correction_field: "Сдача в карго",
      recommendation: "Заполнить поле в документе 1С.",
    },
  };
}

describe("executive procurement actions", () => {
  it("opens an action and expands beyond the first ten rows", () => {
    const actions = Array.from({ length: 12 }, (_, index) => action(index + 1));
    const onOpen = vi.fn();
    render(<ActionTable actions={actions} onOpen={onOpen} />);

    expect(screen.queryByText(/РБГУ0011/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Показать все 12" }));
    expect(screen.getByText(/РБГУ0011/)).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: /Открыть решение: Заказ РБГУ0001/ }));
    expect(onOpen).toHaveBeenCalledWith(actions[0]);
  });

  it("shows the exact 1C correction target", () => {
    render(<ActionDetail action={action(1)} onClose={vi.fn()} />);

    expect(screen.getByRole("dialog")).toHaveTextContent("РБГУ0001");
    expect(screen.getByRole("dialog")).toHaveTextContent("Заказ поставщику");
    expect(screen.getByRole("dialog")).toHaveTextContent("Сдача в карго");
    expect(screen.getByText(/исчезнет после следующего обновления/)).toBeVisible();
  });
});

describe("executive management balance", () => {
  afterEach(() => {
    cleanup();
    vi.mocked(fetchExecutiveManagementBalance).mockReset();
  });

  it("renders the balance separately from the KPI cards", () => {
    const balance = { key: "creditors_payables" } as ExecutiveDashboardBlock;
    const money = { key: "money_today" } as ExecutiveDashboardBlock;

    const result = splitManagementBalanceBlock([money, balance]);

    expect(result.metricBlocks).toEqual([money]);
    expect(result.managementBalance).toBe(balance);
  });

  it("places assets on the left and liabilities on the right", () => {
    const block: ExecutiveDashboardBlock = {
      key: "creditors_payables",
      title: "Управленческий баланс",
      source_status: "ready",
      freshness_status: "fresh",
      as_of: "2026-07-11",
      summary: {
        source_anchor: "1C: тест",
        balance_assets: [
          { key: "cash", label: "Денежные средства", amount: "1000.00" },
          { key: "advances", label: "Авансы и переплаты", amount: "300.00" },
        ],
        balance_liabilities: [
          { key: "suppliers", label: "Задолженность поставщикам", amount: "2100.00" },
          { key: "employees", label: "Задолженность сотрудникам", amount: "100.00" },
        ],
        balance_assets_total: "1300.00",
        balance_liabilities_total: "2200.00",
      },
      metrics: [
        { key: "balance_assets_total", label: "Активы", value: "1300.00", unit: "RUB", tone: "info", masked: false, source_status: "ready" },
        { key: "balance_liabilities_total", label: "Пассивы", value: "2200.00", unit: "RUB", tone: "warning", masked: false, source_status: "ready" },
      ],
    };

    render(<ManagementBalanceBlockCard block={block} />);

    expect(screen.getByText("Активы").parentElement).toHaveTextContent("Денежные средства");
    expect(screen.getByText("Пассивы").parentElement).toHaveTextContent("Задолженность поставщикам");
    expect(screen.getByText("Итого активы").parentElement).toHaveTextContent(/1\s*300 ₽/);
    expect(screen.getByText("Итого пассивы").parentElement).toHaveTextContent(/2\s*200 ₽/);
    expect(screen.queryByText("Чистый долг")).not.toBeInTheDocument();
  });

  it("shows the unconfirmed salary amount outside the balance total", async () => {
    vi.mocked(fetchExecutiveManagementBalance).mockResolvedValue({
      month: "2026-07",
      balance_date: "2026-07-13",
      view: "operational",
      version: 12,
      status: "partial",
      source_status: "partial",
      freshness_status: "fresh",
      generated_at: "2026-07-13T10:00:00+03:00",
      currency: "RUB",
      assets: [],
      liabilities: [],
      equity: [],
      assets_total: "0.00",
      liabilities_total: "0.00",
      equity_total: "0.00",
      liabilities_and_equity_total: "0.00",
      imbalance_amount: "0.00",
      can_close: false,
      validation_errors: [],
      source_summary: {
        salary_reconciliation: {
          status: "partial",
          closing_blocked: true,
          unconfirmed_amount: "4301900.00",
          mapping: { coverage_percent: "0.00" },
        },
      },
      available_months: ["2026-07"],
      note: "Оперативный срез",
    });

    render(<MonthlyManagementBalance asOf="2026-07-13" canCloseMonth={false} refreshNonce={0} />);

    expect(await screen.findByText("Сверка зарплаты выполнена частично")).toBeVisible();
    expect(screen.getByText(/Неподтверждено:/)).toHaveTextContent(/4\s*301\s*900 ₽/);
    expect(screen.getByText(/Неподтверждено:/)).toHaveTextContent("в итог баланса не включено");
    expect(screen.getByText(/Неподтверждено:/)).toHaveTextContent("Сопоставлено сотрудников: 0%");
  });
});

function salesPeriodResponse() {
  return {
    month: "2026-06",
    date_from: "2026-06-01",
    date_to: "2026-06-30",
    as_of: "2026-06-05",
    source_status: "ready",
    freshness_status: "fresh",
    forecast_status: "ready",
    plan_status: "ready",
    note: "Факт 1С",
    forecast_note: "Прогноз по неделям",
    plan_note: null,
    plan: {
      source_status: "ready",
      period_month: "2026-06",
      revision_no: 3,
      snapshot_id: "snapshot-2026-06-v3",
      frozen_at: "2026-06-01T09:00:00Z",
      scope_type: "network",
      scope_key: "network",
      approved_revenue: "5000.00",
      approved_margin_pct: "0.35",
      approved_gross_profit: "1750.00",
      comparison_basis: "forecast",
      comparison_revenue: "5000.00",
      plan_attainment_pct: "1.0",
      note: null,
    },
    diagnostic_kpis: [
      { key: "lost_gross_profit_margin_gap", value: "0.00", unit: "RUB", source_status: "ready", note: null, meta: {} },
      { key: "gross_profit_per_unit", value: "80.00", unit: "RUB_PER_UNIT", source_status: "ready", note: null, meta: {} },
      { key: "cost_per_unit", value: "120.00", unit: "RUB_PER_UNIT", source_status: "ready", note: null, meta: {} },
      { key: "margin_gap_pp", value: "5.00", unit: "PERCENTAGE_POINT", source_status: "ready", note: null, meta: {} },
      { key: "stores_below_plan_count", value: 0, unit: "COUNT", source_status: "ready", note: null, meta: { evaluated_count: 1 } },
      { key: "managers_below_target_margin_count", value: 0, unit: "COUNT", source_status: "ready", note: null, meta: { evaluated_count: 1 } },
    ],
    totals: {
      revenue: "1000.00",
      forecast_revenue_period_end: "5000.00",
      gross_profit: "400.00",
      gross_margin_pct: "0.4",
      sales_count: "5.000",
    },
    comparison: {
      revenue: "800.00",
      gross_profit: "300.00",
      gross_margin_pct: "0.375",
      sales_count: "4.000",
    },
    daily: [
      { business_date: "2026-06-05", actual_revenue: "1000.00", forecast_revenue: null },
      { business_date: "2026-06-06", actual_revenue: null, forecast_revenue: "200.00" },
    ],
    monthly: [
      { month: "2026-05", revenue: "800.00", gross_profit: "300.00", sales_count: "4.000", gross_margin_pct: "0.375", forecast_revenue: null, comparison_sales_count: null },
      { month: "2026-06", revenue: "1000.00", gross_profit: "400.00", sales_count: "5.000", gross_margin_pct: "0.4", forecast_revenue: "5000.00", comparison_sales_count: "4.000" },
    ],
    by_store: [{ key: "store-2", label: "Склад Сайт", revenue: "1000.00", gross_profit: "400.00", sales_count: "5.000", gross_margin_pct: "0.4", meta: {} }],
    by_manager: [],
    stores: [{ key: "store-2", label: "Склад Сайт" }],
    managers: [],
    filters: {},
  };
}

function salesDashboardResponse(): ExecutiveDashboardResponse {
  return {
    as_of: "2026-06-05",
    generated_at: "2026-06-05T10:00:00Z",
    freshness_status: "fresh",
    source_status: "ready",
    access_level: "full",
    roles: [],
    allowed_blocks: ["sales"],
    allowed_action_domains: ["sales"],
    blocks: [
      {
        key: "sales",
        title: "Продажи",
        source_status: "ready",
        freshness_status: "fresh",
        as_of: "2026-06-05",
        summary: {},
        metrics: [],
      },
    ],
    source_freshness: [],
    top_actions: [],
    summary: {},
  };
}

async function renderSalesTab() {
  window.history.pushState({}, "", "?tab=sales&date=2026-06-05");
  vi.mocked(fetchExecutiveDashboard).mockResolvedValue(salesDashboardResponse());
  vi.mocked(fetchExecutiveDashboardActions).mockResolvedValue({
    as_of: "2026-06-05",
    freshness_status: "fresh",
    source_status: "ready",
    total_count: 0,
    payload: [],
  });
  vi.mocked(fetchExecutiveSalesPeriod).mockResolvedValue(salesPeriodResponse());

  const result = render(<ExecutiveDashboard />);
  await screen.findByText("Прогноз выручки");
  return result;
}

describe("executive sales period", () => {
  beforeEach(() => {
    vi.mocked(fetchExecutiveDashboard).mockReset();
    vi.mocked(fetchExecutiveDashboardActions).mockReset();
    vi.mocked(fetchExecutiveSalesPeriod).mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it("loads the sales dashboard and applies a store from the breakdown", async () => {
    await renderSalesTab();

    expect(screen.getAllByText("Объём продаж").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: /Склад Сайт/ }));
    expect(await screen.findByDisplayValue("Склад Сайт")).toBeVisible();
    expect(fetchExecutiveSalesPeriod).toHaveBeenLastCalledWith({
      date_from: "2026-06-01",
      date_to: "2026-06-30",
      store_ref: "store-2",
      manager_ref: undefined,
    });
  });

  it("plots the monthly gross margin trend and sales volume on the single main chart", async () => {
    const { container } = await renderSalesTab();

    expect(screen.getByText("Валовая маржа, %")).toBeVisible();
    expect(container.querySelector(".executive-sales-period__legend")).toHaveTextContent("Объём продаж");
    expect(container.querySelector(".executive-sales-line-chart__margin")).not.toBeNull();
    expect(container.querySelectorAll(".executive-sales-line-chart__volume-bar")).toHaveLength(2);
  });

  it("keeps native browser tooltips off the sales chart", async () => {
    const { container } = await renderSalesTab();

    expect(container.querySelectorAll("title")).toHaveLength(0);
    expect(container.querySelector("[title]")).toBeNull();
  });

  it("shows the shared tooltip and crosshair on hover", async () => {
    const { container } = await renderSalesTab();

    expect(container.querySelector(".executive-sales-month-tooltip")).toBeNull();

    const hitTargets = container.querySelectorAll(".executive-sales-chart-hit");
    expect(hitTargets).toHaveLength(2);
    fireEvent.mouseEnter(hitTargets[1]);

    expect(container.querySelectorAll(".executive-sales-chart-crosshair")).toHaveLength(1);
    const tooltip = container.querySelector(".executive-sales-month-tooltip");
    expect(tooltip).not.toBeNull();
    expect(tooltip).toHaveTextContent("Валовая маржа:");
    expect(tooltip).toHaveTextContent("Объём:");

    fireEvent.mouseLeave(hitTargets[1]);
    expect(container.querySelector(".executive-sales-month-tooltip")).toBeNull();
  });

  it("shows a comparison bar and tooltip line for months with year-over-year volume data", async () => {
    const { container } = await renderSalesTab();

    expect(container.querySelector(".executive-sales-period__legend")).toHaveTextContent(
      "Объём за аналогичный прошлый период"
    );
    expect(container.querySelectorAll(".executive-sales-line-chart__volume-bar--comparison")).toHaveLength(1);

    const hitTargets = container.querySelectorAll(".executive-sales-chart-hit");
    fireEvent.mouseEnter(hitTargets[1]);
    expect(container.querySelector(".executive-sales-month-tooltip")).toHaveTextContent(
      "Объём за аналогичный прошлый период"
    );

    fireEvent.mouseLeave(hitTargets[1]);
    fireEvent.mouseEnter(hitTargets[0]);
    expect(container.querySelector(".executive-sales-month-tooltip")).not.toHaveTextContent(
      "Объём за аналогичный прошлый период"
    );
  });

  it("shows the shared tooltip on keyboard focus of a chart point", async () => {
    const { container } = await renderSalesTab();

    const hitTargets = container.querySelectorAll(".executive-sales-chart-hit");
    expect(hitTargets.length).toBeGreaterThan(0);
    fireEvent.focus(hitTargets[0]);
    expect(container.querySelector(".executive-sales-month-tooltip")).not.toBeNull();
    fireEvent.blur(hitTargets[0]);
    expect(container.querySelector(".executive-sales-month-tooltip")).toBeNull();
  });

  it("renders the universal sales KPI rows with the fixed card counts", async () => {
    await renderSalesTab();

    expect(screen.getByLabelText("Основные KPI продаж").children).toHaveLength(7);
    expect(
      screen.getByLabelText("Диагностические KPI продаж").querySelector(".executive-panel__kpis")?.children
    ).toHaveLength(6);
    expect(screen.getByText("Выручка на единицу")).toBeVisible();
    expect(screen.getByText("Выполнение плана")).toBeVisible();
    expect(screen.queryByText("Средний чек")).not.toBeInTheDocument();
    expect(screen.getByText(/план 5/)).toBeVisible();
  });

  it("shows plan-only metrics as unavailable outside full-month mode", async () => {
    const response = salesPeriodResponse();
    vi.mocked(fetchExecutiveSalesPeriod).mockResolvedValue({
      ...response,
      plan_status: "not_applicable",
      plan_note: "Плановые показатели доступны только в режиме «Месяц».",
      plan: null,
      diagnostic_kpis: response.diagnostic_kpis.map((metric) =>
        ["lost_gross_profit_margin_gap", "margin_gap_pp", "stores_below_plan_count", "managers_below_target_margin_count"].includes(metric.key)
          ? { ...metric, value: null, source_status: "not_applicable" }
          : metric
      ),
    });
    window.history.pushState({}, "", "?tab=sales&date=2026-06-05");
    vi.mocked(fetchExecutiveDashboard).mockResolvedValue(salesDashboardResponse());
    vi.mocked(fetchExecutiveDashboardActions).mockResolvedValue({
      as_of: "2026-06-05",
      freshness_status: "fresh",
      source_status: "ready",
      total_count: 0,
      payload: [],
    });

    render(<ExecutiveDashboard />);

    expect((await screen.findAllByText("Только режим «Месяц»")).length).toBeGreaterThanOrEqual(5);
  });

  it("renders an info tooltip on the sales KPI cards", async () => {
    await renderSalesTab();

    const trigger = screen.getByRole("button", { name: "Пояснение: Выручка факт" });
    fireEvent.mouseEnter(trigger);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Сумма продаж 1С за выбранный период");
    fireEvent.mouseLeave(trigger);
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("moves the date range/store/manager filters into the page header, replacing the redundant global date field", async () => {
    await renderSalesTab();

    expect(screen.getByLabelText("Начало периода продаж")).toBeVisible();
    expect(screen.getByLabelText("Конец периода продаж")).toBeVisible();
    expect(screen.getByLabelText("Магазин")).toBeVisible();
    expect(screen.getByLabelText("Менеджер")).toBeVisible();
    expect(screen.queryByLabelText("Дата управленческой витрины")).not.toBeInTheDocument();
  });
});

describe("executive dashboard tab overview de-duplication", () => {
  beforeEach(() => {
    vi.mocked(fetchExecutiveDashboard).mockReset();
    vi.mocked(fetchExecutiveDashboardActions).mockReset();
    vi.mocked(fetchExecutiveSalesPeriod).mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it("does not render the generic block overview or metric grid on the Sales tab", async () => {
    window.history.pushState({}, "", "?tab=sales&date=2026-06-05");
    vi.mocked(fetchExecutiveDashboard).mockResolvedValue({
      as_of: "2026-06-05",
      generated_at: "2026-06-05T10:00:00Z",
      freshness_status: "fresh",
      source_status: "ready",
      access_level: "full",
      roles: [],
      allowed_blocks: ["sales"],
      allowed_action_domains: ["sales"],
      blocks: [
        {
          key: "sales",
          title: "Продажи",
          source_status: "ready",
          freshness_status: "fresh",
          as_of: "2026-06-05",
          summary: {},
          metrics: [
            {
              key: "revenue_mtd",
              label: "Выручка с начала месяца",
              value: "1000",
              unit: "RUB",
              tone: "success",
              masked: false,
              source_status: "ready",
            },
          ],
        },
      ],
      source_freshness: [],
      top_actions: [],
      summary: {},
    });
    vi.mocked(fetchExecutiveDashboardActions).mockResolvedValue({
      as_of: "2026-06-05",
      freshness_status: "fresh",
      source_status: "ready",
      total_count: 0,
      payload: [],
    });
    vi.mocked(fetchExecutiveSalesPeriod).mockResolvedValue(salesPeriodResponse());

    const { container } = render(<ExecutiveDashboard />);

    expect(await screen.findByText("Выручка факт")).toBeVisible();
    expect(screen.queryByText("Выручка с начала месяца")).toBeNull();
    expect(container.querySelector(".executive-grid")).toBeNull();
  });
});
