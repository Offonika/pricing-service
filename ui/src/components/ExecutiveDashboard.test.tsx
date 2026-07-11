import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ExecutiveDashboardAction, ExecutiveDashboardBlock } from "../api/executiveDashboard";
import { ActionDetail, ActionTable, PayablesBlockCard } from "./ExecutiveDashboard";

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

describe("executive payables", () => {
  it("separates counterparties and reveals the full read-only list", () => {
    const counterparties = Array.from({ length: 6 }, (_, index) => ({
      counterparty_ref: `0x${index + 1}`,
      counterparty_code: `К${index + 1}`,
      counterparty_name: `Контрагент ${index + 1}`,
      payable_amount: `${(6 - index) * 100}.00`,
      group_title: index === 5 ? "Взаиморасчеты с сотрудниками" : "Поставщики товаров",
      source_report: "1C: тестовый отчет",
    }));
    const block: ExecutiveDashboardBlock = {
      key: "creditors_payables",
      title: "Кредиторская задолженность",
      source_status: "ready",
      freshness_status: "fresh",
      as_of: "2026-07-11",
      summary: {
        source_anchor: "1C: кредиторка",
        counterparties,
      },
      metrics: [
        { key: "total_payable", label: "Всего", value: "2100.00", unit: "RUB", tone: "warning", masked: false, source_status: "ready" },
        { key: "supplier_payable", label: "Поставщики", value: "2000.00", unit: "RUB", tone: "warning", masked: false, source_status: "ready" },
        { key: "employee_payable", label: "Сотрудники", value: "100.00", unit: "RUB", tone: "warning", masked: false, source_status: "ready" },
      ],
    };

    render(<PayablesBlockCard block={block} />);

    expect(screen.queryByText("Контрагент 6")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Показать всех 6" }));
    expect(screen.getByText("Контрагент 6")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: /Контрагент 1/ }));
    const dialog = screen.getByRole("dialog", { name: "Контрагент 1" });
    expect(dialog).toHaveTextContent("Поставщики товаров");
    expect(dialog).toHaveTextContent("Режим просмотра");
  });
});
