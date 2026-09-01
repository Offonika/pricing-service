import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import toast from "react-hot-toast";
import type { ProcurementLifecycleTransitionList } from "../api/procurementAssortment";
import {
  decideProcurementLifecycleTransition,
  fetchProcurementLifecycleTransitions,
  fetchProcurementOrders,
} from "../api/procurementAssortment";
import { LifecycleQueue, OrdersRegistry } from "./ProcurementOrderFormationWorkspace";

vi.mock("../api/procurementAssortment", () => ({
  approveProcurementClassification: vi.fn(),
  approveProcurementLifecycleTransitions: vi.fn(),
  decideProcurementLifecycleTransition: vi.fn(),
  exportProcurementOrdersExcel: vi.fn(),
  fetchProcurementClassifications: vi.fn(),
  fetchProcurementDashboard: vi.fn(),
  fetchProcurementEvents: vi.fn(),
  fetchProcurementLifecycleTransitions: vi.fn(),
  fetchProcurementOrder: vi.fn(),
  fetchProcurementOrders: vi.fn(),
}));

vi.mock("./ProcurementOrderAssistant", () => ({
  ProcurementOrderAssistant: () => null,
}));

vi.mock("./ProcurementOrderFormationApp", () => ({
  ProcurementOrderFormationApp: () => null,
}));

vi.mock("react-hot-toast", () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

function lifecycleQueue(): ProcurementLifecycleTransitionList {
  return {
    status: "working",
    scope: "action",
    total: 1,
    page: 1,
    page_size: 50,
    ready_count: 0,
    review_count: 1,
    blocked_count: 0,
    stale_count: 0,
    items: [{
      proposal_id: 77,
      nomenclature_code: "РБ000037607",
      nomenclature_ref: "ref-77",
      product_guid: "guid-77",
      product_name: "Дисплей без напарников в сегменте",
      folder: "Дисплеи",
      action_kind: "review",
      current_status: "working",
      current_status_label: "Рабочий",
      target_status: null,
      target_status_label: null,
      proposal_status: "pending",
      reason: "В сегменте нет второго доступного SKU",
      facts: { evidence: { family_member_count: 1 } },
      blockers: [],
      risk_codes: ["lifecycle_stage_not_exported"],
      run_id: 361,
      run_key: "display-run-361",
      facts_hash: "a".repeat(64),
      responsible_bitrix_user_id: "130757",
      responsible_name: "Омар",
      decision_state: "review",
      actionability: "manual_decision",
      suggested_manual_status: "pension",
      ready: false,
      selectable: false,
      stale: false,
      created_at: "2026-08-20T09:00:00",
    }],
  };
}

describe("LifecycleQueue manual decision", () => {
  beforeEach(() => {
    vi.mocked(fetchProcurementLifecycleTransitions).mockReset();
    vi.mocked(decideProcurementLifecycleTransition).mockReset();
    vi.mocked(toast.success).mockReset();
    vi.mocked(toast.error).mockReset();
  });

  afterEach(cleanup);

  it("показывает активное ручное решение без чекбокса и сохраняет «Взамен ведём»", async () => {
    const data = lifecycleQueue();
    vi.mocked(fetchProcurementLifecycleTransitions).mockResolvedValue(data);
    vi.mocked(decideProcurementLifecycleTransition).mockResolvedValue({
      proposal_id: 77,
      result: "approved",
      message: "Карточка переведена в «Допродаём»",
      decision: "pension",
      approved_at: "2026-08-20T10:00:00",
    });

    render(
      <LifecycleQueue
        initialReadiness="review"
        onClose={vi.fn()}
        scope="action"
        status="working"
      />
    );

    expect(await screen.findByText("Дисплей без напарников в сегменте")).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /Выбрать Дисплей/ })).not.toBeInTheDocument();
    const open = screen.getByRole("button", { name: "Принять решение" });
    expect(open).toBeEnabled();
    fireEvent.click(open);
    expect(screen.queryByRole("button", { name: "Принять решение" })).not.toBeInTheDocument();

    const save = screen.getByRole("button", { name: "Сохранить решение" });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByPlaceholderText("Обязательная причина решения"), {
      target: { value: "Ведём другую карточку семьи" },
    });
    fireEvent.change(screen.getByPlaceholderText("Код 1С (РБ...)"), {
      target: { value: "РБ000057818" },
    });
    fireEvent.click(save);

    await waitFor(() => expect(decideProcurementLifecycleTransition).toHaveBeenCalledWith(
      data.items[0],
      {
        decision: "pension",
        reason: "Ведём другую карточку семьи",
        replacement_sku_code: "РБ000057818",
        no_replacement: false,
      }
    ));
    expect(toast.success).toHaveBeenCalledWith("Карточка переведена в «Допродаём»");
  });

  it("позволяет оставить карточку рабочей с обязательной причиной", async () => {
    const data = lifecycleQueue();
    vi.mocked(fetchProcurementLifecycleTransitions).mockResolvedValue(data);
    vi.mocked(decideProcurementLifecycleTransition).mockResolvedValue({
      proposal_id: 77,
      result: "approved",
      message: "Карточка оставлена в статусе «Рабочий»",
      decision: "working",
      approved_at: "2026-08-20T10:00:00",
    });

    render(
      <LifecycleQueue
        initialReadiness="review"
        onClose={vi.fn()}
        scope="action"
        status="working"
      />
    );
    fireEvent.click(await screen.findByRole("button", { name: "Принять решение" }));
    fireEvent.change(screen.getByLabelText("Решение"), { target: { value: "working" } });
    fireEvent.change(screen.getByPlaceholderText("Обязательная причина решения"), {
      target: { value: "Продажи стабильны, оставляем рабочим" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить решение" }));

    await waitFor(() => expect(decideProcurementLifecycleTransition).toHaveBeenCalledWith(
      data.items[0],
      {
        decision: "working",
        reason: "Продажи стабильны, оставляем рабочим",
        replacement_sku_code: null,
        no_replacement: false,
      }
    ));
    expect(screen.queryByPlaceholderText("Код 1С (РБ...)")).not.toBeInTheDocument();
  });
});

describe("OrdersRegistry", () => {
  beforeEach(() => {
    vi.mocked(fetchProcurementOrders).mockReset();
    vi.mocked(fetchProcurementOrders).mockResolvedValue({
      total: 1,
      page: 1,
      page_size: 100,
      summary: {
        orders: 1,
        lines: 2,
        quantity: "10",
        amount: "1000",
        by_status: { partially_received: 1 },
      },
      items: [{
        id: 543,
        stable_key: "onec:supplier-order:543",
        status: "transmitted",
        lifecycle_status: "partially_received",
        lifecycle_status_label: "Частично поступил",
        origin: "onec_import",
        version: 1,
        supplier_name: "Поставщик 1С",
        contract_name: "Основной договор",
        warehouse_name: "Основной склад",
        currency: "RUB",
        route: "ordinary",
        procurement_contour: "ordinary",
        batch_id: "onec-543",
        order_date: "2026-08-31",
        onec_status: "transmitted",
        onec_document_number: "РБГУ0000543",
        onec_document_date: "2026-08-31",
        line_count: 2,
        ordered_quantity: "10",
        received_quantity: "4",
        open_quantity: "6",
        total_quantity: "10",
        total_amount: "1000",
        blockers: [],
        updated_at: "2026-09-01T08:00:00",
      }],
    });
  });

  afterEach(cleanup);

  it("показывает импортированный заказ и передаёт фильтры единого реестра", async () => {
    render(<OrdersRegistry onOpenOrder={vi.fn()} />);

    expect(await screen.findByText("РБГУ0000543")).toBeInTheDocument();
    expect(screen.getAllByText("Частично поступил")).toHaveLength(2);
    expect(screen.getByText("Источник: 1С")).toBeInTheDocument();
    fireEvent.change(screen.getByDisplayValue("Все контуры"), { target: { value: "ordinary" } });

    await waitFor(() => expect(fetchProcurementOrders).toHaveBeenLastCalledWith(
      expect.objectContaining({ contour: "ordinary", page_size: 100 })
    ));
  });
});
