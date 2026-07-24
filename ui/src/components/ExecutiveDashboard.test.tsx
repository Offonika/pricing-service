import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ExecutiveDashboardAction,
  ExecutiveDashboardBlock,
  ExecutiveDashboardResponse,
  ExecutiveOnlineStorePeriodResponse,
  ExecutiveProfitLossInventoryLoss,
  ExecutiveProfitLossPeriodResponse,
  ExecutiveSalesPeriodResponse,
} from "../api/executiveDashboard";
import {
  fetchExecutiveDashboard,
  fetchExecutiveDashboardActions,
  fetchExecutiveManagementBalance,
  fetchExecutiveManagementBalanceTurnover,
  fetchExecutiveOnlineStorePeriod,
  fetchExecutiveProfitLossPeriod,
  fetchExecutiveSalesPeriod,
} from "../api/executiveDashboard";

vi.mock("../api/executiveDashboard", () => ({
  closeExecutiveManagementBalance: vi.fn(),
  fetchExecutiveCashflowPeriod: vi.fn(),
  fetchExecutiveDashboard: vi.fn(),
  fetchExecutiveDashboardActions: vi.fn(),
  fetchExecutiveManagementBalance: vi.fn(),
  fetchExecutiveManagementBalanceTurnover: vi.fn(),
  fetchExecutiveOnlineStorePeriod: vi.fn(),
  fetchExecutiveProfitLossPeriod: vi.fn(),
  fetchExecutiveSalesPeriod: vi.fn(),
}));

import {
  ActionDetail,
  ActionTable,
  ExecutiveDashboard,
  InventoryLossPanel,
  ManagementBalanceBlockCard,
  MonthlyManagementBalance,
  OnlineStorePanel,
} from "./ExecutiveDashboard";
import { splitManagementBalanceBlock } from "./executiveDashboardLayout";

function inventoryLoss(): ExecutiveProfitLossInventoryLoss {
  return {
    schema_version: 2,
    month: "2026-06",
    source_status: "ready",
    detail_source_status: "partial",
    writeoff_amount: "1229121.82",
    receipt_amount: "526672.97",
    loss_amount: "702448.85",
    loss_pct: "0.8499",
    norm_pct: "0.3000",
    variance_to_norm_pct: "0.5499",
    matched_store_count: 2,
    previous_month: {
      month: "2026-05",
      source_status: "ready",
      loss_amount: "600000.00",
      loss_pct: "0.7000",
    },
    average_loss_amount_3m: "500000.00",
    average_loss_pct_3m: "0.6000",
    history_source_status: "ready",
    history: [
      { month: "2026-04", source_status: "ready", loss_amount: "400000.00", loss_pct: "0.5" },
      { month: "2026-05", source_status: "ready", loss_amount: "600000.00", loss_pct: "0.7" },
      { month: "2026-06", source_status: "ready", loss_amount: "702448.85", loss_pct: "0.8499" },
    ],
    stores: [
      {
        store_ref: "store-1",
        store_name: "Горбушкин Двор с очень длинным названием магазина",
        sales_amount: "1000000",
        writeoff_amount: "8000",
        receipt_amount: "1000",
        loss_amount: "7000",
        loss_pct: "0.7000",
        norm_pct: "0.3000",
        variance_to_norm_pct: "0.4000",
        above_norm: true,
        source_status: "ready",
        has_operations: true,
      },
      {
        store_ref: "store-2",
        store_name: "Склад Сайт",
        sales_amount: "250000",
        writeoff_amount: "100",
        receipt_amount: "200",
        loss_amount: "-100",
        loss_pct: "-0.0400",
        norm_pct: "0.3000",
        variance_to_norm_pct: "-0.3400",
        above_norm: false,
        source_status: "ready",
        has_operations: true,
      },
    ],
    top_documents: [
      {
        stable_key: "writeoff-1",
        operation_kind: "inventory_writeoff",
        operation_label: "Инвентаризационное списание",
        document_type: "_Document210",
        document_ref: "doc-1",
        document_number: "СП-1",
        document_date: "2026-06-20",
        store_ref: "store-1",
        store_name: "Горбушкин Двор с очень длинным названием магазина",
        amount: "8000",
        effect_amount: "8000",
      },
      {
        stable_key: "receipt-1",
        operation_kind: "inventory_receipt",
        operation_label: "Оприходование по инвентаризации",
        document_type: "_Document170",
        document_ref: "doc-2",
        document_number: "ОП-1",
        document_date: "2026-06-21",
        store_ref: "store-2",
        store_name: "Склад Сайт",
        amount: "200",
        effect_amount: "-200",
      },
    ],
    actions: [
      {
        stable_key: "action-1",
        action_type: "store_above_norm",
        severity: "warning",
        title: "Потери выше норматива: Горбушкин Двор",
        description: "Факт 0.7% при нормативе 0.3%.",
        amount: "7000",
        store_ref: "store-1",
        store_name: "Горбушкин Двор",
        responsible_name: "Руководитель сети",
        recommended_action: "Проверить крупнейшие документы.",
      },
    ],
    data_quality: {
      source_status: "partial",
      approved_store_count: 3,
      source_store_count: 3,
      matched_store_count: 2,
      unmatched_store_count: 1,
      source_document_count: 3,
      matched_document_count: 2,
      unmatched_document_count: 1,
      unmatched_writeoff_amount: "500",
      unmatched_receipt_amount: "0",
      excluded_store_count: 1,
      excluded_document_count: 2,
      excluded_writeoff_amount: "100",
      excluded_receipt_amount: "20",
      store_scope_status: "approved",
      store_scope_source: "approved_freeze",
      store_scope_month: "2026-06",
      norm_source_status: "approved",
      norm_source: "bitrix_kpi_v2_export",
    },
    owner: { employee_name: "Руководитель сети", role_code: "retail_director" },
    warnings: ["Одна операция требует сопоставления."],
    note: "Товарные потери включены в ОПУ.",
  };
}

describe("executive inventory loss", () => {
  afterEach(cleanup);

  it("renders comparisons, filters stores and filters both operation types", () => {
    render(<InventoryLossPanel data={inventoryLoss()} />);

    expect(screen.getByText("Норматив").parentElement).toHaveTextContent("0,30%");
    expect(screen.getByText("Прошлый месяц").parentElement).toHaveTextContent(/600\s*000/);
    expect(screen.getByLabelText("Динамика товарных потерь")).toHaveTextContent("2026-04");

    const stores = screen.getByLabelText("Потери по магазинам");
    expect(within(stores).getByText("Склад Сайт")).toBeVisible();
    fireEvent.click(within(stores).getByRole("button", { name: "Выше норматива" }));
    expect(within(stores).queryByText("Склад Сайт")).not.toBeInTheDocument();

    const documents = screen.getByLabelText("Крупнейшие товарные операции");
    fireEvent.change(within(documents).getByLabelText("Тип товарной операции"), {
      target: { value: "receipt" },
    });
    expect(within(documents).getByText("ОП-1")).toBeVisible();
    expect(within(documents).queryByText("СП-1")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Требует действий")).toHaveTextContent("Read-only очередь");
  });

  it("keeps v1 totals visible and explains missing detail", () => {
    const data = inventoryLoss();
    data.schema_version = 1;
    data.detail_source_status = "source_missing";
    data.stores = [];
    data.top_documents = [];
    data.actions = [];

    render(<InventoryLossPanel data={data} />);

    expect(screen.getByText("Чистые товарные потери").parentElement).toHaveTextContent(/702\s*449/);
    expect(screen.getByText(/Источник v1 содержит только сетевые итоги/)).toBeVisible();
  });

  it("labels receipt surplus without presenting it as a loss", () => {
    const data = inventoryLoss();
    data.loss_amount = "-250.00";
    data.loss_pct = "-0.0500";
    data.previous_month = {
      month: "2026-05",
      source_status: "ready",
      loss_amount: "-100.00",
      loss_pct: "-0.0200",
    };
    data.history = [
      { month: "2026-05", source_status: "ready", loss_amount: "-100.00", loss_pct: "-0.0200" },
    ];

    render(<InventoryLossPanel data={data} />);

    expect(screen.getByText("Превышение оприходований").parentElement).toHaveTextContent(/250/);
    expect(screen.getByText("Прошлый месяц").parentElement).toHaveTextContent(/Превышение оприходований/);
    expect(screen.getByLabelText("Динамика товарных потерь")).toHaveTextContent(/Превышение оприходований/);
    expect(screen.getByLabelText("Динамика товарных потерь").querySelector("b")).toHaveClass("is-receipt-surplus");
  });

  it("filters documents for a store without store_ref by its name", () => {
    const data = inventoryLoss();
    data.stores[0].store_ref = "";
    data.top_documents[0].store_ref = "";

    render(<InventoryLossPanel data={data} />);

    const documents = screen.getByLabelText("Крупнейшие товарные операции");
    fireEvent.change(within(documents).getByLabelText("Магазин документов"), {
      target: { value: `name:${data.stores[0].store_name}` },
    });

    expect(within(documents).getByText("СП-1")).toBeVisible();
    expect(within(documents).queryByText("ОП-1")).not.toBeInTheDocument();
  });

  it("distinguishes a v2 detail error from the v1 fallback", () => {
    const data = inventoryLoss();
    data.detail_source_status = "source_error";
    data.stores = [];
    data.top_documents = [];

    render(<InventoryLossPanel data={data} />);

    expect(screen.getByText(/Источник v2 опубликован без доступной детализации/)).toBeVisible();
    expect(screen.queryByText(/Источник v1 содержит только сетевые итоги/)).not.toBeInTheDocument();
  });

  it("shows draft store scope and fallback norm honestly", () => {
    const data = inventoryLoss();
    data.data_quality.store_scope_status = "draft";
    data.data_quality.norm_source_status = "fallback";

    render(<InventoryLossPanel data={data} />);

    expect(screen.getByText("Норматив").parentElement).toHaveTextContent("резервный норматив");
    fireEvent.click(screen.getByText(/Контроль качества данных/));
    expect(screen.getByText("Магазинов в черновике")).toBeVisible();
    expect(screen.getByText("Статус контура").parentElement).toHaveTextContent("черновик");
  });

  it("uses a neutral tone when variance to norm is unavailable", () => {
    const data = inventoryLoss();
    data.variance_to_norm_pct = null;

    render(<InventoryLossPanel data={data} />);

    const card = screen.getByText("Отклонение").parentElement;
    expect(card).toHaveTextContent("нет данных");
    expect(card?.className).toContain("metric_neutral");
    expect(card?.className).not.toContain("metric_success");
  });
});

function action(index: number): ExecutiveDashboardAction {
  return {
    stable_key: `procurement:${index}`,
    business_date: "2026-07-11",
    domain: "procurement_import",
    severity: index === 1 ? "critical" : "warning",
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
      supplier_title: "Shenzhen Parts",
      responsible_name: "Ирина Закупкина",
      management_stage_label: "Обработка поставщиком",
      deadline_date: "2026-07-09",
      days_overdue: 2,
      reason_code: "supplier_preparation_critical",
      reason: "Срок подготовки поставщиком превышен.",
      risk_formula: "p75=18; critical=max(p75×1,6; p75+7)=29",
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

function procurementDashboardResponse(): ExecutiveDashboardResponse {
  return {
    as_of: "2026-07-11",
    generated_at: "2026-07-11T10:00:00Z",
    freshness_status: "fresh",
    source_status: "ready",
    access_level: "full",
    roles: [],
    allowed_blocks: ["procurement_import"],
    allowed_action_domains: ["procurement_import"],
    blocks: [
      {
        key: "procurement_import",
        title: "Закупки / импорт",
        source_status: "ready",
        freshness_status: "fresh",
        as_of: "2026-07-11",
        summary: {
          note: "Открытые заказы из 1С.",
          risk_scoring_version: 2,
          risk_summary: { at_risk_count: 2, at_risk_amount_rub: "350000", at_risk_share_pct: "28.0", critical_count: 1 },
          stage_breakdown: [
            { key: "supplier_processing", label: "Обработка поставщиком", count: 3, amount_rub: "900000" },
            { key: "in_transit", label: "В пути", count: 1, amount_rub: "350000" },
          ],
          currency_breakdown: [
            { currency: "RMB", count: 3, amount_rub: "900000" },
            { currency: "RUB", count: 1, amount_rub: "350000" },
          ],
          data_quality: { responsible_coverage_pct: "75.0", missing_responsible_count: 1, missing_expected_receipt_after_cargo_count: 0 },
        },
        metrics: [
          { key: "open_supplier_orders", label: "Заказы поставщику", value: 4, unit: "COUNT", tone: "neutral", source_status: "ready", masked: false },
          { key: "open_order_amount_rub", label: "Сумма открытых заказов", value: "1250000", unit: "RUB", tone: "neutral", source_status: "ready", masked: false },
          { key: "procurement_at_risk_count", label: "Заказы под риском", value: 2, unit: "COUNT", tone: "warning", source_status: "ready", masked: false },
          { key: "procurement_at_risk_amount_rub", label: "Сумма под риском", value: "350000", unit: "RUB", tone: "warning", source_status: "ready", masked: false },
          { key: "critical_overdue_count", label: "Критические просрочки", value: 1, unit: "COUNT", tone: "danger", source_status: "ready", masked: false },
          { key: "foreign_open_order_amount_rub", label: "Открытые закупки в валюте", value: "120000", unit: "RUB", tone: "neutral", source_status: "ready", masked: false },
        ],
      },
    ],
    source_freshness: [],
    top_actions: [],
    summary: {},
  };
}

describe("executive procurement tab", () => {
  beforeEach(() => {
    vi.mocked(fetchExecutiveDashboard).mockReset();
    vi.mocked(fetchExecutiveDashboardActions).mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it("shows procurement KPIs and decisions in one operational panel", async () => {
    window.history.pushState({}, "", "?tab=procurement_import&date=2026-07-11");
    vi.mocked(fetchExecutiveDashboard).mockResolvedValue(procurementDashboardResponse());
    vi.mocked(fetchExecutiveDashboardActions).mockResolvedValue({
      as_of: "2026-07-11",
      freshness_status: "fresh",
      source_status: "ready",
      total_count: 1,
      payload: [action(1)],
    });

    render(<ExecutiveDashboard />);

    const panel = await screen.findByLabelText("Закупки");
    expect(within(panel).getByLabelText("Основные KPI закупок")).toHaveTextContent("Открытые заказы");
    expect(within(panel).getByLabelText("Основные KPI закупок")).toHaveTextContent("Сумма: 1 250 000 ₽");
    expect(within(panel).getByLabelText("Основные KPI закупок")).toHaveTextContent("2 заказов · 28.0% открытых закупок");
    expect(within(panel).getByLabelText("Основные KPI закупок")).toHaveTextContent("Критично");
    expect(within(panel).getByLabelText("Основные KPI закупок")).toHaveTextContent("Открытые закупки в валюте");
    expect(within(panel).getByLabelText("Этапы закупок")).toHaveTextContent("Обработка поставщиком · 72%");
    expect(within(panel).getByLabelText("Валютная структура закупок")).toHaveTextContent("Рублёвые заказы");
    expect(within(panel).getByLabelText("Фильтры закупочной очереди")).toBeVisible();
    expect(within(panel).getByLabelText("Решения по закупкам")).toHaveTextContent("РБГУ0001");
    expect(within(panel).getByText("Заказы в зоне внимания").compareDocumentPosition(within(panel).getByText("Этапы открытых заказов")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByLabelText("Состояние витрины")).not.toBeInTheDocument();
    expect(screen.queryByText("Решения: Закупки")).not.toBeInTheDocument();
  });

  it("shows five priority orders first and filters the complete queue", async () => {
    window.history.pushState({}, "", "?tab=procurement_import&date=2026-07-11");
    vi.mocked(fetchExecutiveDashboard).mockResolvedValue(procurementDashboardResponse());
    vi.mocked(fetchExecutiveDashboardActions).mockResolvedValue({
      as_of: "2026-07-11",
      freshness_status: "fresh",
      source_status: "ready",
      total_count: 7,
      payload: Array.from({ length: 7 }, (_, index) => ({
        ...action(index + 1),
        payload: {
          ...action(index + 1).payload,
          responsible_name: index === 6 ? "Пётр ВЭД" : "Ирина Закупкина",
        },
      })),
    });

    render(<ExecutiveDashboard />);

    const queue = await screen.findByLabelText("Решения по закупкам");
    expect(within(queue).getAllByRole("row")).toHaveLength(6);
    expect(within(queue).queryByText("РБГУ0007")).not.toBeInTheDocument();
    fireEvent.change(within(queue).getByLabelText("Ответственный"), { target: { value: "Пётр ВЭД" } });
    expect(within(queue).getByText("РБГУ0007")).toBeVisible();
    expect(within(queue).queryByRole("button", { name: "Показать все 1" })).not.toBeInTheDocument();
    fireEvent.change(within(queue).getByLabelText("Ответственный"), { target: { value: "" } });
    fireEvent.click(within(queue).getByRole("button", { name: "Показать все 7" }));
    expect(within(queue).getAllByRole("row")).toHaveLength(8);
  });

  it("keeps the v1 and stale states explicit without calculated shares", async () => {
    const response = procurementDashboardResponse();
    response.blocks[0].freshness_status = "stale";
    response.blocks[0].summary = {
      note: "Старый снимок",
      stage_breakdown: response.blocks[0].summary.stage_breakdown,
      currency_breakdown: response.blocks[0].summary.currency_breakdown,
    };
    window.history.pushState({}, "", "?tab=procurement_import&date=2026-07-11");
    vi.mocked(fetchExecutiveDashboard).mockResolvedValue(response);
    vi.mocked(fetchExecutiveDashboardActions).mockResolvedValue({
      as_of: "2026-07-11",
      freshness_status: "stale",
      source_status: "ready",
      total_count: 0,
      payload: [],
    });

    render(<ExecutiveDashboard />);

    const panel = await screen.findByLabelText("Закупки");
    expect(within(panel).getByLabelText("Статус источника закупок")).toHaveTextContent("устарело");
    expect(within(panel).getByText(/Источник v1/)).toBeVisible();
    expect(within(panel).getByLabelText("Этапы закупок")).not.toHaveTextContent("72%");
    expect(within(panel).getByText("Заказов по выбранным фильтрам нет.")).toBeVisible();
  });

  it("preserves amount masking and the source error fallback", async () => {
    const masked = procurementDashboardResponse();
    masked.blocks[0].metrics = masked.blocks[0].metrics.map((metric) =>
      metric.unit === "RUB" ? { ...metric, masked: true, value: null } : metric
    );
    window.history.pushState({}, "", "?tab=procurement_import&date=2026-07-11");
    vi.mocked(fetchExecutiveDashboard).mockResolvedValue(masked);
    vi.mocked(fetchExecutiveDashboardActions).mockResolvedValue({
      as_of: "2026-07-11",
      freshness_status: "fresh",
      source_status: "ready",
      total_count: 1,
      payload: [{ ...action(1), amount: null }],
    });

    const { unmount } = render(<ExecutiveDashboard />);
    const panel = await screen.findByLabelText("Закупки");
    expect(within(panel).getByLabelText("Основные KPI закупок")).toHaveTextContent("скрыто");
    expect(within(panel).getByLabelText("Решения по закупкам")).toHaveTextContent("скрыто");
    expect(within(panel).getByLabelText("Основные KPI закупок")).not.toHaveTextContent("% суммы открытых заказов");

    unmount();
    const sourceError = procurementDashboardResponse();
    sourceError.blocks[0].source_status = "source_error";
    sourceError.blocks[0].summary.note = "Источник временно недоступен.";
    vi.mocked(fetchExecutiveDashboard).mockResolvedValue(sourceError);
    vi.mocked(fetchExecutiveDashboardActions).mockResolvedValue({
      as_of: "2026-07-11",
      freshness_status: "source_error",
      source_status: "source_error",
      total_count: 0,
      payload: [],
    });

    render(<ExecutiveDashboard />);
    expect(await screen.findByText("Источник временно недоступен.")).toBeVisible();
  });
});

describe("executive management balance", () => {
  afterEach(() => {
    cleanup();
    vi.mocked(fetchExecutiveManagementBalance).mockReset();
    vi.mocked(fetchExecutiveManagementBalanceTurnover).mockReset();
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

    render(<MonthlyManagementBalance canCloseMonth={false} refreshNonce={0} />);

    expect(await screen.findByText("Сверка зарплаты выполнена частично")).toBeVisible();
    expect(fetchExecutiveManagementBalance).toHaveBeenCalledWith({
      month: undefined,
      view: undefined,
    });
    expect(screen.getByText(/Неподтверждено:/)).toHaveTextContent(/4\s*301\s*900 ₽/);
    expect(screen.getByText(/Неподтверждено:/)).toHaveTextContent("в итог баланса не включено");
    expect(screen.getByText(/Неподтверждено:/)).toHaveTextContent("Сопоставлено сотрудников: 0%");
  });

  it("shows a trial balance scoped to UT 10.3 and accrued BP taxes", async () => {
    vi.mocked(fetchExecutiveManagementBalance).mockResolvedValue({
      month: "2026-06",
      balance_date: "2026-06-30",
      view: "closed",
      version: 2,
      status: "closed",
      source_status: "ready",
      freshness_status: "fresh",
      generated_at: "2026-06-30T10:00:00+03:00",
      currency: "RUB",
      assets: [],
      liabilities: [],
      equity: [],
      assets_total: "120.00",
      liabilities_total: "85.00",
      equity_total: "35.00",
      liabilities_and_equity_total: "120.00",
      imbalance_amount: "0.00",
      can_close: false,
      validation_errors: [],
      source_summary: {},
      available_months: ["2026-06"],
      note: "Закрытый месяц",
    });
    vi.mocked(fetchExecutiveManagementBalanceTurnover).mockResolvedValue({
      month: "2026-06",
      date_from: "2026-01-01",
      date_to: "2026-06-30",
      view: "closed",
      opening_version: 1,
      closing_version: 2,
      opening_status: "closed",
      closing_status: "closed",
      opening_validation_error_count: 0,
      opening_content_sha256: "a".repeat(64),
      closing_content_sha256: "b".repeat(64),
      turnover_method: "net_change_from_snapshots",
      source_scope: "onec_ut_10_3_plus_bp_accrued_taxes",
      source_status: "ready",
      currency: "RUB",
      lines: [
        {
          key: "cash",
          label: "Денежные средства",
          section: "asset",
          opening_balance: "100.00",
          debit_turnover: "20.00",
          credit_turnover: "0.00",
          closing_balance: "120.00",
          reconciliation_difference: "0.00",
          turnover_method: "net_change_from_snapshots",
          source_key: "onec_cash_position",
          source_status: "ready",
        },
        {
          key: "taxes_payable",
          label: "Начисленные налоги",
          section: "liability",
          opening_balance: "10.00",
          debit_turnover: "0.00",
          credit_turnover: "5.00",
          closing_balance: "15.00",
          reconciliation_difference: "0.00",
          turnover_method: "net_change_from_snapshots",
          source_key: "onec_bp_tax_accounting",
          source_status: "ready",
        },
      ],
      totals: [
        {
          section: "asset",
          label: "Итого активы",
          opening_balance: "100.00",
          debit_turnover: "20.00",
          credit_turnover: "0.00",
          closing_balance: "120.00",
          reconciliation_difference: "0.00",
          unknown_line_count: 0,
        },
        {
          section: "liability",
          label: "Итого обязательства",
          opening_balance: "10.00",
          debit_turnover: "0.00",
          credit_turnover: "5.00",
          closing_balance: "15.00",
          reconciliation_difference: "0.00",
          unknown_line_count: 0,
        },
        {
          section: "equity",
          label: "Итого собственные средства",
          opening_balance: "0.00",
          debit_turnover: "0.00",
          credit_turnover: "0.00",
          closing_balance: "0.00",
          reconciliation_difference: "0.00",
          unknown_line_count: 0,
        },
      ],
      excluded_lines: [
        {
          key: "fixed_assets_net",
          source_key: "onec_bp_fixed_assets",
          reason: "В БП для ОСВ разрешена только строка начисленных налогов",
        },
      ],
      opening_imbalance_amount: "0.00",
      closing_imbalance_amount: "0.00",
      unknown_line_count: 0,
      note: "Обороты рассчитаны как чистое изменение между снимками.",
    });

    render(<MonthlyManagementBalance canCloseMonth={false} refreshNonce={0} />);

    expect(await screen.findByText("Оборотно-сальдовая ведомость")).toBeVisible();
    expect(screen.getByText("УТ 10.3 · из БП только начисленные налоги")).toBeVisible();
    const table = screen.getByRole("table");
    expect(within(table).getByText("Денежные средства").closest("tr")).toHaveTextContent(
      /100 ₽/
    );
    expect(within(table).getByText("Начисленные налоги").closest("tr")).toHaveTextContent(
      /10 ₽/
    );
    expect(screen.getByText(/Не включено строк БП: 1/)).toBeVisible();
  });
});

function profitLossPeriodResponse(): ExecutiveProfitLossPeriodResponse {
  return {
    date_from: "2026-06-01",
    date_to: "2026-06-30",
    generated_at: "2026-06-30T10:00:00Z",
    source_status: "partial",
    freshness_status: "fresh",
    note: "Операционная прибыль включает товарные потери.",
    totals: {
      revenue: "1000000.00",
      cost_of_sales: "700000.00",
      gross_profit: "300000.00",
      operating_expenses: "100000.00",
      inventory_loss_expense: "50000.00",
      operating_profit: "200000.00",
      tax_expense_accrued: "20000.00",
      net_profit: "180000.00",
      expense_open_question_count: "0",
    },
    ratios: [
      { key: "gross_margin_pct", label: "Валовая маржа", value: "0.3", unit: "PCT", tone: "neutral" },
      { key: "operating_margin_pct", label: "Операционная маржа", value: "0.2", unit: "PCT", tone: "neutral" },
      { key: "net_profit_margin_pct", label: "Рентабельность чистой прибыли", value: "0.18", unit: "PCT", tone: "info" },
    ],
    lines: [
      { key: "gross_revenue", label: "Выручка до возвратов", amount: "1000000.00", line_type: "income", tone: "info", source_status: "ready" },
      { key: "customer_refunds", label: "Возвраты покупателям", amount: "-10000.00", line_type: "expense", tone: "warning", source_status: "ready" },
      { key: "revenue", label: "Чистая выручка", amount: "990000.00", line_type: "subtotal", tone: "info", source_status: "ready" },
      { key: "cost_of_sales", label: "Себестоимость продаж", amount: "-690000.00", line_type: "expense", tone: "warning", source_status: "ready" },
      { key: "gross_profit", label: "Валовая прибыль", amount: "300000.00", line_type: "subtotal", tone: "info", source_status: "ready" },
      { key: "operating_expenses", label: "Операционные расходы по ДДС", amount: "-100000.00", line_type: "expense", tone: "warning", source_status: "ready" },
      { key: "inventory_loss", label: "Чистые товарные потери", amount: "-10000.00", line_type: "expense", tone: "warning", source_status: "ready" },
      { key: "operating_profit", label: "Операционная прибыль", amount: "190000.00", line_type: "subtotal", tone: "info", source_status: "ready" },
      { key: "profit_before_tax", label: "Прибыль до налогообложения", amount: "185000.00", line_type: "total", tone: "info", source_status: "ready" },
      { key: "taxes", label: "Налоги ниже операционной прибыли", amount: "-5000.00", line_type: "expense", tone: "warning", source_status: "ready" },
      { key: "net_profit", label: "Чистая прибыль", amount: "180000.00", line_type: "total", tone: "info", source_status: "partial", note: "Предварительно." },
    ],
    daily: [],
    monthly: [
      {
        month: "2026-05",
        revenue: "900000.00",
        gross_profit: "270000.00",
        operating_expenses: "95000.00",
        operating_profit: "175000.00",
        net_profit: "160000.00",
        gross_margin_pct: "0.3",
        operating_margin_pct: "0.1944",
        net_profit_margin_pct: "0.1778",
        comparison_net_profit: "140000.00",
        source_status: "ready",
        is_preliminary: false,
      },
      {
        month: "2026-06",
        revenue: "1000000.00",
        gross_profit: "300000.00",
        operating_expenses: "100000.00",
        operating_profit: "200000.00",
        net_profit: "215000.00",
        gross_margin_pct: "0.3",
        operating_margin_pct: "0.2",
        net_profit_margin_pct: "0.215",
        comparison_net_profit: null,
        source_status: "partial",
        is_preliminary: true,
        note: "Предварительно: начисления налогов неполны.",
      },
    ],
    by_store: [],
    by_manager: [],
    expense_source_status: "ready",
    expense_breakdown: [
      {
        key: "rent",
        label: "Аренда",
        amount: "100000.00",
        movement_count: 2,
        review_count: 0,
        source_status: "ready",
        recognition_method: "cashflow_fallback",
        estimated_count: 0,
        meta: {},
      },
    ],
    expense_open_questions: [],
    inventory_loss: inventoryLoss(),
    filters: {},
  };
}

function profitLossDashboardResponse(): ExecutiveDashboardResponse {
  return {
    as_of: "2026-06-30",
    generated_at: "2026-06-30T10:00:00Z",
    freshness_status: "fresh",
    source_status: "partial",
    access_level: "full",
    roles: [],
    allowed_blocks: ["profit_loss"],
    allowed_action_domains: ["profit_loss"],
    blocks: [
      {
        key: "profit_loss",
        title: "Прибыли / убытки",
        source_status: "partial",
        freshness_status: "fresh",
        as_of: "2026-06-30",
        summary: {},
        metrics: [],
      },
    ],
    source_freshness: [],
    top_actions: [],
    summary: {},
  };
}

async function renderProfitLossTab() {
  window.history.pushState({}, "", "?tab=profit_loss&date=2026-06-30");
  vi.mocked(fetchExecutiveDashboard).mockResolvedValue(profitLossDashboardResponse());
  vi.mocked(fetchExecutiveDashboardActions).mockResolvedValue({
    as_of: "2026-06-30",
    freshness_status: "fresh",
    source_status: "ready",
    total_count: 0,
    payload: [],
  });
  vi.mocked(fetchExecutiveProfitLossPeriod).mockResolvedValue(profitLossPeriodResponse());

  const result = render(<ExecutiveDashboard />);
  await screen.findByRole("heading", { name: "Структура ОПУ" });
  return result;
}

describe("executive profit and loss period", () => {
  beforeEach(() => {
    vi.mocked(fetchExecutiveDashboard).mockReset();
    vi.mocked(fetchExecutiveDashboardActions).mockReset();
    vi.mocked(fetchExecutiveProfitLossPeriod).mockReset();
  });

  afterEach(cleanup);

  it("moves the period controls into the page header and keeps them connected to the report", async () => {
    const { container } = await renderProfitLossTab();
    const pageHeader = container.querySelector(".executive__header");
    expect(pageHeader).not.toBeNull();

    expect(within(pageHeader as HTMLElement).getByRole("button", { name: "7 дней" })).toBeVisible();
    expect(within(pageHeader as HTMLElement).getByRole("button", { name: "30 дней" })).toBeVisible();
    expect(within(pageHeader as HTMLElement).getByRole("button", { name: "Месяц" })).toBeVisible();
    expect(screen.getByLabelText("Начало периода прибыли и убытков")).toHaveValue("2026-06-01");
    expect(screen.getByLabelText("Конец периода прибыли и убытков")).toHaveValue("2026-06-30");
    expect(screen.queryByLabelText("Дата управленческой витрины")).not.toBeInTheDocument();

    const report = screen.getByLabelText("Отчет о прибылях и убытках за период");
    expect(within(report).queryByRole("button", { name: "7 дней" })).not.toBeInTheDocument();
    expect(within(report).queryByRole("button", { name: "Месяц" })).not.toBeInTheDocument();

    fireEvent.click(within(pageHeader as HTMLElement).getByRole("button", { name: "7 дней" }));
    await waitFor(() => {
      expect(fetchExecutiveProfitLossPeriod).toHaveBeenLastCalledWith({
        date_from: "2026-06-24",
        date_to: "2026-06-30",
      });
    });

    fireEvent.change(screen.getByLabelText("Начало периода прибыли и убытков"), {
      target: { value: "2026-06-01" },
    });
    fireEvent.change(screen.getByLabelText("Конец периода прибыли и убытков"), {
      target: { value: "2026-06-15" },
    });
    await waitFor(() => {
      expect(fetchExecutiveProfitLossPeriod).toHaveBeenLastCalledWith({
        date_from: "2026-06-01",
        date_to: "2026-06-15",
      });
    });

    fireEvent.click(within(pageHeader as HTMLElement).getByRole("button", { name: "Месяц" }));
    await waitFor(() => {
      expect(fetchExecutiveProfitLossPeriod).toHaveBeenLastCalledWith({
        date_from: "2026-06-01",
        date_to: "2026-06-30",
      });
    });
  });

  it("shows the monthly profit trend and profitability mode", async () => {
    const { container } = await renderProfitLossTab();
    const chart = screen.getByLabelText("Помесячная динамика ОПиУ");

    expect(within(chart).getByText("Валовая прибыль")).toBeVisible();
    expect(within(chart).getByText("Операционные расходы")).toBeVisible();
    expect(within(chart).getByText("Чистая прибыль год назад")).toBeVisible();
    expect(container.querySelectorAll(".executive-profit-loss-trend__expense-bar")).toHaveLength(2);

    fireEvent.click(within(chart).getByRole("button", { name: "Рентабельность" }));
    expect(within(chart).getByText("Валовая маржа")).toBeVisible();
    expect(within(chart).getByText("Операционная маржа")).toBeVisible();
    expect(within(chart).getByText("Рентабельность чистой прибыли")).toBeVisible();
  });

  it("puts line drilldowns inside the P&L structure and removes duplicate detail blocks", async () => {
    await renderProfitLossTab();

    expect(screen.queryByLabelText("Динамика ОПУ по дням")).not.toBeInTheDocument();
    const structure = screen.getByLabelText("Структура ОПУ");
    expect(screen.queryByLabelText("Операционные расходы по ДДС")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Товарные потери за месяц" })).not.toBeInTheDocument();

    const refundsLabel = within(structure)
      .getAllByText("Возвраты покупателям")
      .find((node) => node.parentElement?.classList.contains("executive-profit-loss-line"));
    const refundsRow = refundsLabel?.parentElement || null;
    expect(refundsRow).not.toBeNull();
    expect(refundsRow?.querySelector(".executive-profit-loss-line__action-placeholder")).not.toBeNull();
    expect(within(refundsRow as HTMLElement).queryByText("готово")).not.toBeInTheDocument();

    const expenseRow = within(structure)
      .getByText("Операционные расходы по ДДС", { selector: "summary > span:first-child" })
      .closest("details");
    expect(expenseRow).not.toBeNull();
    expect(within(expenseRow as HTMLElement).getByText("Аренда")).not.toBeVisible();
    fireEvent.click(within(expenseRow as HTMLElement).getByText("Расшифровать"));
    expect(within(expenseRow as HTMLElement).getByText("Аренда")).toBeVisible();

    const netProfitRow = within(structure)
      .getByText("Чистая прибыль", { selector: "summary > span:first-child" })
      .closest("details");
    expect(netProfitRow).not.toBeNull();
    fireEvent.click(within(netProfitRow as HTMLElement).getByText("Расшифровать"));
    expect(within(netProfitRow as HTMLElement).getByText("Прибыль до налогообложения")).toBeVisible();
    expect(within(netProfitRow as HTMLElement).getByText("Налоги ниже операционной прибыли")).toBeVisible();
  });
});

function salesPeriodResponse(): ExecutiveSalesPeriodResponse {
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
      { key: "stores_below_plan_count", value: 0, unit: "COUNT", source_status: "ready", note: null, meta: { evaluated_count: 1, problem: [] } },
      { key: "managers_below_target_margin_count", value: 0, unit: "COUNT", source_status: "ready", note: null, meta: { evaluated_count: 1, problem: [] } },
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

async function renderSalesTab(response = salesPeriodResponse()) {
  window.history.pushState({}, "", "?tab=sales&date=2026-06-05");
  vi.mocked(fetchExecutiveDashboard).mockResolvedValue(salesDashboardResponse());
  vi.mocked(fetchExecutiveDashboardActions).mockResolvedValue({
    as_of: "2026-06-05",
    freshness_status: "fresh",
    source_status: "ready",
    total_count: 0,
    payload: [],
  });
  vi.mocked(fetchExecutiveSalesPeriod).mockResolvedValue(response);

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

  it("renders the daily dynamics of the selected period with dashed forecast days", async () => {
    const { container } = await renderSalesTab();

    const dailyChart = screen.getByLabelText("Выручка по дням выбранного периода");
    expect(within(dailyChart).getByText("По дням выбранного периода")).toBeVisible();
    const days = dailyChart.querySelectorAll(".executive-sales-day");
    expect(days).toHaveLength(2);
    expect(days[0].querySelector(".executive-sales-day__bar--forecast")).toBeNull();
    expect(days[1].querySelector(".executive-sales-day__bar--forecast")).not.toBeNull();
    expect(days[1]).toHaveTextContent("прогноз");
    expect(container.querySelectorAll(".executive-sales-daily")).toHaveLength(1);
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
    const diagnosticTable = screen.getByRole("table", { name: "Показатели диагностики продаж" });
    expect(diagnosticTable.querySelectorAll("tbody tr:not(.executive-sales-diagnostics__group-row)")).toHaveLength(6);
    expect(
      Array.from(diagnosticTable.querySelectorAll(".executive-sales-diagnostics__group-row")).map((row) => row.textContent)
    ).toEqual(["Экономика продаж", "Качество данных и плана"]);
    expect(
      Array.from(diagnosticTable.querySelectorAll(".executive-sales-diagnostics__group-row th")).map(
        (header) => header.getAttribute("scope")
      )
    ).toEqual(["rowgroup", "rowgroup"]);
    expect(screen.getByLabelText("Сводка диагностики продаж")).toHaveTextContent(
      "0 требуют внимания6 рассчитаноПлан сопоставлен"
    );
    expect(screen.getByText("Выручка на единицу")).toBeVisible();
    expect(screen.getByText("Выполнение плана")).toBeVisible();
    expect(screen.queryByText("Средний чек")).not.toBeInTheDocument();
    expect(screen.getByText(/план 5/)).toBeVisible();
  });

  it("hides internal store references from sales diagnostics", async () => {
    const response = salesPeriodResponse();
    await renderSalesTab({
      ...response,
      diagnostic_kpis: (response.diagnostic_kpis || []).map((metric) =>
        metric.key === "stores_below_plan_count"
          ? {
              ...metric,
              note: "Не все магазины факта присутствуют во frozen-плане: 0x93040025901E48EE11E3B5A2, 0xBB780025901E48EF11E160486.",
              source_status: "partial",
              value: null,
            }
          : metric
      ),
    });

    expect(
      screen.getByText("Не все магазины из фактических продаж найдены в утверждённом плане.")
    ).toBeVisible();
    expect(screen.queryByText(/0x[0-9a-f]{16,}/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Сводка диагностики продаж")).toHaveTextContent(
      "План сопоставлен частично"
    );
  });

  it("opens backend-provided problem lists and restores the full breakdown", async () => {
    const response = salesPeriodResponse();
    await renderSalesTab({
      ...response,
      diagnostic_kpis: (response.diagnostic_kpis || []).map((metric) => {
        if (metric.key === "stores_below_plan_count") {
          return {
            ...metric,
            value: 2,
            meta: {
              evaluated_count: 3,
              problem: [
                { key: "store-low", label: "Магазин ниже плана" },
                { key: "store-silent", label: "Магазин без продаж" },
              ],
            },
          };
        }
        if (metric.key === "managers_below_target_margin_count") {
          return {
            ...metric,
            value: 1,
            meta: {
              evaluated_count: 2,
              problem: [{ key: "manager-low", label: "Менеджер ниже маржи" }],
            },
          };
        }
        return metric;
      }),
      by_store: [
        { key: "store-low", label: "Магазин ниже плана", revenue: "800", gross_profit: "200", sales_count: "4", gross_margin_pct: "0.25", meta: { plan_status: "ready", plan_attainment_pct: "0.8" } },
        { key: "store-ok", label: "Магазин в плане", revenue: "1200", gross_profit: "500", sales_count: "5", gross_margin_pct: "0.42", meta: { plan_status: "ready", plan_attainment_pct: "1.1" } },
      ],
      by_manager: [
        { key: "manager-low", label: "Менеджер ниже маржи", revenue: "900", gross_profit: "180", sales_count: "4", gross_margin_pct: "0.2", meta: { plan_status: "ready", margin_gap_pp: "-3" } },
        { key: "manager-ok", label: "Менеджер в норме", revenue: "1500", gross_profit: "600", sales_count: "6", gross_margin_pct: "0.4", meta: { plan_status: "ready", margin_gap_pp: "2" } },
      ],
    });

    fireEvent.click(screen.getByRole("button", { name: "Показать проблемные магазины" }));
    const problemStores = screen.getByRole("region", { name: "Проблемные магазины" });
    expect(within(problemStores).getByRole("button", { name: /Магазин ниже плана/ })).toBeVisible();
    expect(within(problemStores).getByRole("button", { name: /Магазин без продаж/ })).toBeVisible();
    expect(within(problemStores).queryByRole("button", { name: /Магазин в плане/ })).not.toBeInTheDocument();

    fireEvent.click(within(problemStores).getByRole("button", { name: "Показать всех" }));
    const allStores = screen.getByRole("region", { name: "По магазинам" });
    expect(within(allStores).getByRole("button", { name: /Магазин в плане/ })).toBeVisible();
    expect(within(allStores).queryByRole("button", { name: /Магазин без продаж/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Показать менеджеров ниже маржи" }));
    const problemManagers = screen.getByRole("region", { name: "Проблемные менеджеры" });
    expect(within(problemManagers).getByRole("button", { name: /Менеджер ниже маржи/ })).toBeVisible();
    expect(within(problemManagers).queryByRole("button", { name: /Менеджер в норме/ })).not.toBeInTheDocument();
  });

  it("shows plan-only metrics as unavailable outside full-month mode", async () => {
    const response = salesPeriodResponse();
    vi.mocked(fetchExecutiveSalesPeriod).mockResolvedValue({
      ...response,
      plan_status: "not_applicable",
      plan_note: "Плановые показатели доступны только в режиме «Месяц».",
      plan: null,
      diagnostic_kpis: (response.diagnostic_kpis || []).map((metric) =>
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

    expect(await screen.findByText("Только режим «Месяц»")).toBeVisible();
    expect(screen.getAllByText("Не применяется")).toHaveLength(4);
    expect(screen.getByLabelText("Сводка диагностики продаж")).toHaveTextContent(
      "0 требуют внимания"
    );
    const diagnosticTable = screen.getByRole("table", { name: "Показатели диагностики продаж" });
    expect(
      Array.from(diagnosticTable.querySelectorAll(".executive-sales-diagnostics__value")).filter(
        (cell) => cell.textContent === "—"
      )
    ).toHaveLength(4);
  });

  it("shows a fully collected forecast period as complete", async () => {
    const response = salesPeriodResponse();
    await renderSalesTab({
      ...response,
      forecast_status: "complete",
      forecast_note: "Период полностью закрыт фактическими данными.",
    });

    expect(screen.getByText("период закрыт")).toBeVisible();
    expect(screen.queryByText("месяц закрыт")).not.toBeInTheDocument();
  });

  it("renders an info tooltip on the sales KPI cards", async () => {
    await renderSalesTab();

    const trigger = screen.getByRole("button", { name: "Пояснение: Выручка факт" });
    fireEvent.mouseEnter(trigger);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Сумма продаж 1С за выбранный период");
    fireEvent.mouseLeave(trigger);
    expect(screen.queryByRole("tooltip")).toBeNull();

    const planTrigger = screen.getByRole("button", { name: "Пояснение: Выполнение плана" });
    fireEvent.mouseEnter(planTrigger);
    expect(screen.getByRole("tooltip")).toHaveTextContent("утверждённым планом");
    expect(screen.getByRole("tooltip")).not.toHaveTextContent("frozen");
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

function onlineStorePeriodResponse(): ExecutiveOnlineStorePeriodResponse {
  return {
    date_from: "2026-07-01",
    date_to: "2026-07-07",
    compare_date_from: "2026-06-24",
    compare_date_to: "2026-06-30",
    generated_at: "2026-07-07T12:00:00Z",
    source_status: "ready",
    freshness_status: "fresh",
    counter_id: "49993429",
    site: "master-mobile.ru",
    note: "Яндекс Метрика показывает онлайн-спрос; это не финансовая выручка 1С.",
    totals: {
      visits: 1000,
      visitors: 700,
      purchases: 25,
      purchase_conversion_pct: "2.50",
      click_buy: 80,
      begin_checkout: 40,
      phone_clicks: 7,
      site_searches: 120,
      primary_source_name: "Переходы из поисковых систем",
      primary_source_purchases: 20,
      primary_source_purchase_share_pct: "80.00",
    },
    comparison: {
      visits: 800,
      visitors: 600,
      purchases: 16,
      purchase_conversion_pct: "2.00",
    },
    daily: [
      {
        business_date: "2026-07-01",
        visits: 120,
        visitors: 90,
        purchases: 4,
        click_buy: 12,
        begin_checkout: 5,
        phone_clicks: 1,
        site_searches: 8,
        purchase_conversion_pct: "3.33",
      },
    ],
    traffic_sources: [
      {
        key: "organic",
        label: "Переходы из поисковых систем",
        visits: 600,
        visitors: 350,
        purchases: 20,
        purchase_conversion_pct: "3.33",
      },
    ],
    landing_pages: [
      {
        url: "https://master-mobile.ru/catalog/item/",
        visits: 120,
        visitors: 90,
        purchases: 4,
        click_buy: 12,
        begin_checkout: 5,
        purchase_conversion_pct: "3.33",
      },
    ],
  };
}

describe("executive online store tab", () => {
  beforeEach(() => {
    vi.mocked(fetchExecutiveDashboard).mockReset();
    vi.mocked(fetchExecutiveDashboardActions).mockReset();
    vi.mocked(fetchExecutiveOnlineStorePeriod).mockReset();
  });

  afterEach(cleanup);

  it("renders traffic, conversion, funnel, sources and landing pages", () => {
    render(<OnlineStorePanel data={onlineStorePeriodResponse()} message="" status="ready" />);

    expect(screen.getByLabelText("Основные KPI интернет-магазина")).toHaveTextContent("1 000");
    expect(screen.getByLabelText("Основные KPI интернет-магазина")).toHaveTextContent("2,50%");
    expect(screen.getByLabelText("Воронка интернет-магазина")).toHaveTextContent("Начали оформление");
    expect(screen.getByLabelText("Каналы трафика интернет-магазина")).toHaveTextContent("20 покупок");
    expect(screen.getByLabelText("Посадочные страницы интернет-магазина")).toHaveTextContent("/catalog/item/");
    expect(screen.getByText(/не финансовая выручка 1С/)).toBeVisible();
  });

  it("loads the tab from the allowed-block policy without requesting action items", async () => {
    window.history.pushState({}, "", "?tab=online_store&date=2026-07-07");
    vi.mocked(fetchExecutiveDashboard).mockResolvedValue({
      as_of: "2026-07-07",
      generated_at: "2026-07-07T10:00:00Z",
      freshness_status: "fresh",
      source_status: "ready",
      access_level: "full",
      roles: ["full"],
      allowed_blocks: ["online_store"],
      allowed_action_domains: [],
      blocks: [],
      source_freshness: [],
      top_actions: [],
      summary: {},
    });
    vi.mocked(fetchExecutiveOnlineStorePeriod).mockResolvedValue(onlineStorePeriodResponse());

    render(<ExecutiveDashboard />);

    expect(await screen.findByLabelText("Интернет-магазин")).toBeVisible();
    expect(fetchExecutiveOnlineStorePeriod).toHaveBeenCalledWith({
      date_from: "2026-07-01",
      date_to: "2026-07-07",
    });
    expect(fetchExecutiveDashboardActions).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Начало периода интернет-магазина")).toBeVisible();
    expect(screen.queryByLabelText("Дата управленческой витрины")).not.toBeInTheDocument();
  });
});
