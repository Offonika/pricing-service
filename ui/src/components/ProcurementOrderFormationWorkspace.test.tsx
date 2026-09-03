import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import toast from "react-hot-toast";
import type {
  ProcurementDashboard,
  ProcurementLifecycleTransitionList,
  ProcurementOrderListItem,
} from "../api/procurementAssortment";
import {
  decideProcurementLifecycleTransition,
  fetchProcurementDashboard,
  fetchProcurementEvents,
  fetchProcurementLifecycleTransitions,
} from "../api/procurementAssortment";
import {
  EventHistory,
  LifecycleQueue,
  OrderBlockerCell,
  ProcurementOrderFormationWorkspace,
} from "./ProcurementOrderFormationWorkspace";

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

vi.mock("./ProcurementProductInsights", () => ({
  ProcurementProductInsights: ({
    nomenclatureCode,
    onBack,
  }: {
    nomenclatureCode: string;
    onBack: () => void;
  }) => (
    <div>
      <span>Карточка разбора {nomenclatureCode}</span>
      <button onClick={onBack} type="button">Назад на Витрину</button>
    </div>
  ),
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

function blockedOrder(productCount = 1): ProcurementOrderListItem {
  return {
    id: 14,
    stable_key: "order-14",
    status: "draft",
    version: 1,
    supplier_name: "Поставщик",
    contract_name: "Договор",
    warehouse_name: "Склад",
    currency: "RUB",
    route: "ordinary",
    batch_id: "2026-09-02",
    order_date: "2026-09-02",
    onec_status: "not_sent",
    line_count: productCount,
    total_quantity: "5",
    total_amount: "575",
    blockers: ["batch_error_suspected"],
    blocked_products: Array.from({ length: productCount }, (_item, index) => ({
      line_id: index + 1,
      line_number: index + 1,
      bitrix_product_id: String(1646 + index),
      xml_id: `${index + 1}`.padStart(8, "0") + "-0000-0000-0000-000000000000",
      nomenclature_code: `РБ00000673${7 + index}`,
      name: `Дисплей ${index + 1}`,
      blocker_count: 1,
      blockers: [],
      bitrix_url: `/crm/catalog/17/product/${1646 + index}/`,
    })),
    updated_at: "2026-09-02T10:00:00",
  };
}

function dashboardWithManualReview(): ProcurementDashboard {
  return {
    folder: "Дисплеи",
    responsible_user_id: "130757",
    responsible_name: "Омар",
    run_id: 361,
    run_key: "display-run-361",
    updated_at: "2026-09-03T09:32:22",
    cards: [],
    decision_summary: {
      ready_count: 0,
      review_count: 0,
      blocked_count: 0,
    },
    manual_status_counts: { review: 1 },
    attention: [],
    manual_attention: [{
      proposal_id: null,
      nomenclature_code: "РБ000006737",
      product_name: "Дисплей Samsung A16",
      current_status: "review",
      current_status_label: "Разбор",
      kind: "manual",
      filter_status: "review",
      action_label: "Принять решение",
      fact_summary: "Нужно сравнить семью",
      decision_state: "review",
      decision_state_label: "Нужен разбор",
      reason: "Семейное решение",
      recommendation: "Сравнить карточки",
      deadline_label: "Сегодня",
      urgency: "review",
    }],
  };
}

describe("Dashboard review navigation", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/bitrix/procurement-order-formation");
    vi.mocked(fetchProcurementDashboard).mockReset();
  });

  afterEach(cleanup);

  it("открывает семейный разбор из карточки Разбор и возвращает на Витрину", async () => {
    vi.mocked(fetchProcurementDashboard).mockResolvedValue(dashboardWithManualReview());

    render(<ProcurementOrderFormationWorkspace bitrixUserName="Омар" />);

    fireEvent.click(await screen.findByRole("button", { name: "Показать товары: Разбор" }));
    fireEvent.click(screen.getByRole("button", { name: "Открыть разбор" }));
    expect(await screen.findByText("Карточка разбора РБ000006737")).toBeInTheDocument();
    expect(window.location.pathname).toContain("/review/");

    fireEvent.click(screen.getByRole("button", { name: "Назад на Витрину" }));
    expect(await screen.findByRole("heading", { name: "Жизненные статусы" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/bitrix/procurement-order-formation");
  });
});

describe("OrderBlockerCell", () => {
  beforeEach(() => {
    window.__MM_BITRIX_LAUNCH__ = { domain: "crm.example.test" };
  });

  afterEach(cleanup);

  it("открывает единственный проблемный товар сразу", () => {
    render(<OrderBlockerCell order={blockedOrder()} />);

    expect(screen.getByRole("link")).toHaveAttribute(
      "href",
      "https://crm.example.test/crm/catalog/17/product/1646/"
    );
  });

  it("показывает список карточек, когда проблемных товаров несколько", () => {
    render(<OrderBlockerCell order={blockedOrder(2)} />);

    expect(screen.getByRole("link", { name: /Дисплей 1/ })).toHaveAttribute(
      "href",
      "https://crm.example.test/crm/catalog/17/product/1646/"
    );
    expect(screen.getByRole("link", { name: /Дисплей 2/ })).toHaveAttribute(
      "href",
      "https://crm.example.test/crm/catalog/17/product/1647/"
    );
  });
});

describe("EventHistory product navigation", () => {
  beforeEach(() => {
    window.__MM_BITRIX_LAUNCH__ = { domain: "crm.example.test" };
    vi.mocked(fetchProcurementEvents).mockReset();
  });

  afterEach(cleanup);

  it("связывает товарное событие с оригинальной карточкой Bitrix24", async () => {
    vi.mocked(fetchProcurementEvents).mockResolvedValue({
      total: 1,
      page: 1,
      page_size: 100,
      items: [
        {
          id: 91,
          order_id: 14,
          entity_type: "order_line",
          entity_id: "10",
          event_type: "order_line_changed",
          actor: "test",
          before: {},
          after: {},
          payload: {},
          product: {
            bitrix_product_id: "1646",
            xml_id: "2685293e-967c-11e1-bdb9-0025901e48ef",
            nomenclature_code: "РБ000006737",
            name: "Дисплей Samsung A16",
            bitrix_url: "/crm/catalog/17/product/1646/",
          },
          created_at: "2026-09-02T10:00:00",
        },
      ],
    });

    render(<EventHistory />);

    expect(await screen.findByRole("link", { name: "Дисплей Samsung A16" }))
      .toHaveAttribute(
        "href",
        "https://crm.example.test/crm/catalog/17/product/1646/"
      );
  });
});

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
