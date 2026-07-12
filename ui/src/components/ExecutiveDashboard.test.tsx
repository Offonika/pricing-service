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
  it("shows only the negative net debt", () => {
    const block: ExecutiveDashboardBlock = {
      key: "creditors_payables",
      title: "Кредиторская задолженность",
      source_status: "ready",
      freshness_status: "fresh",
      as_of: "2026-07-11",
      summary: {
        source_anchor: "1C: кредиторка",
      },
      metrics: [
        { key: "total_payable", label: "Чистый долг", value: "-1900.00", unit: "RUB", tone: "warning", masked: false, source_status: "ready" },
        { key: "supplier_payable", label: "Поставщикам", value: "-1800.00", unit: "RUB", tone: "warning", masked: false, source_status: "ready" },
        { key: "employee_payable", label: "Сотрудникам", value: "-100.00", unit: "RUB", tone: "warning", masked: false, source_status: "ready" },
      ],
    };

    render(<PayablesBlockCard block={block} />);

    expect(screen.getByText("Чистый долг").parentElement).toHaveTextContent(/-1\s*900 ₽/);
    expect(screen.getByText("Поставщикам").parentElement).toHaveTextContent(/-1\s*800 ₽/);
    expect(screen.getByText("Сотрудникам").parentElement).toHaveTextContent(/-100 ₽/);
    expect(screen.queryByText("Долг до авансов")).not.toBeInTheDocument();
    expect(screen.queryByText("Авансы / переплаты (−)")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Контрагенты с кредиторской задолженностью")).not.toBeInTheDocument();
  });
});
