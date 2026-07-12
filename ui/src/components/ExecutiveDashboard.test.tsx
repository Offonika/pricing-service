import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ExecutiveDashboardAction, ExecutiveDashboardBlock } from "../api/executiveDashboard";
import {
  ActionDetail,
  ActionTable,
  ManagementBalanceBlockCard,
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
