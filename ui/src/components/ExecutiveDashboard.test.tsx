import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ExecutiveDashboardAction, ExecutiveDashboardBlock } from "../api/executiveDashboard";
import { fetchExecutiveSalesPeriod } from "../api/executiveDashboard";

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
  ManagementBalanceBlockCard,
  SalesPeriodPanel,
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
});

describe("executive sales period", () => {
  beforeEach(() => {
    vi.mocked(fetchExecutiveSalesPeriod).mockReset();
  });

  it("loads the sales dashboard and applies a store from the breakdown", async () => {
    vi.mocked(fetchExecutiveSalesPeriod).mockResolvedValue({
      month: "2026-06",
      date_from: "2026-06-01",
      date_to: "2026-06-30",
      as_of: "2026-06-05",
      source_status: "ready",
      freshness_status: "fresh",
      forecast_status: "ready",
      note: "Факт 1С",
      forecast_note: "Прогноз по неделям",
      totals: {
        revenue_mtd: "1000.00",
        forecast_revenue_month_end: "5000.00",
        gross_profit_mtd: "400.00",
        gross_margin_pct_mtd: "0.4",
        sales_count_mtd: "5.000",
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
        { month: "2026-05", revenue: "800.00", gross_profit: "300.00", sales_count: "4.000", forecast_revenue: null },
        { month: "2026-06", revenue: "1000.00", gross_profit: "400.00", sales_count: "5.000", forecast_revenue: "5000.00" },
      ],
      by_store: [{ key: "store-2", label: "Склад Сайт", revenue: "1000.00", gross_profit: "400.00", sales_count: "5.000", gross_margin_pct: "0.4", meta: {} }],
      by_manager: [],
      stores: [{ key: "store-2", label: "Склад Сайт" }],
      managers: [],
      filters: {},
    });

    render(<SalesPeriodPanel asOf="2026-06-05" />);

    expect(await screen.findByText("Прогноз выручки")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Объём продаж" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /Склад Сайт/ }));
    expect(await screen.findByDisplayValue("Склад Сайт")).toBeVisible();
    expect(fetchExecutiveSalesPeriod).toHaveBeenLastCalledWith({
      month: "2026-06",
      store_ref: "store-2",
      manager_ref: undefined,
    });
  });
});
