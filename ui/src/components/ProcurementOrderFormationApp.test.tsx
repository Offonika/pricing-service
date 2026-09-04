import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import toast from "react-hot-toast";
import type {
  ProcurementOrderFormation,
  ProcurementOrderFormationLine,
  ProcurementOrderLabelPreview,
} from "../api/procurementAssortment";
import {
  previewProcurementSupplierDistribution,
  searchProcurementSupplierOptions,
  selectProcurementLineMainSupplier,
  createProcurementClassification,
  downloadProcurementOrderLabels,
  fetchProcurementOrder,
  fetchProcurementOrderLabelPreview,
  linkProcurementOrderLabelSource,
  updateProcurementOrderLine,
} from "../api/procurementAssortment";
import { ProcurementOrderFormationApp as ProductionOrderFormationApp } from "./ProcurementOrderFormationApp";
import { procurementBlockerSummaryLabel } from "../utils/procurementRiskLabels";

vi.mock("../api/procurementAssortment", () => ({
  applyProcurementSupplierDistribution: vi.fn(),
  approveProcurementClassification: vi.fn(),
  createProcurementClassification: vi.fn(),
  downloadProcurementOrderLabels: vi.fn(),
  fetchProcurementOrder: vi.fn(),
  fetchProcurementOrderLabelPreview: vi.fn(),
  linkProcurementOrderLabelSource: vi.fn(),
  previewProcurementSupplierDistribution: vi.fn(),
  searchProcurementSupplierOptions: vi.fn(),
  selectProcurementLineMainSupplier: vi.fn(),
  submitProcurementOrder: vi.fn(),
  updateProcurementOrderLine: vi.fn(),
}));

vi.mock("react-hot-toast", () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

function ProcurementOrderFormationApp(props: ComponentProps<typeof ProductionOrderFormationApp>) {
  return <ProductionOrderFormationApp {...props} initialView="detailed" />;
}

function line(overrides: Partial<ProcurementOrderFormationLine> = {}) {
  return {
    id: 40,
    line_number: 1,
    version: 1,
    bitrix_product_id: "2695",
    bitrix_product_xml_id: "11111111-2222-3333-4444-555555555555",
    nomenclature_ref: "11111111-2222-3333-4444-555555555555",
    nomenclature_code: "РБ000031600",
    nomenclature_name: "Дисплей для Huawei P10 Lite",
    recommended_quantity: "1.000",
    final_quantity: "1.000",
    purchase_price: "39.0000",
    amount: "39.00",
    currency: "RUB",
    source_kind: "auto",
    explicit_demand: false,
    risk_codes: [],
    blockers: [],
    removed: false,
    effective_assortment_status: "working",
    effective_assortment_status_label: "Рабочий",
    ...overrides,
  } satisfies ProcurementOrderFormationLine;
}

function order(overrides: Partial<ProcurementOrderFormation> = {}) {
  return {
    id: 12,
    stable_key: "order-12",
    status: "draft",
    version: 1,
    supplier_ref: "supplier-ref",
    supplier_name: "03-Мария",
    contract_name: "Основной договор",
    currency: "RUB",
    warehouse_name: "Сдэк Склад",
    procurement_contour: "ordinary",
    route: "ordinary",
    batch_id: "2026-07-31",
    order_date: "2026-07-31",
    calculation_id: "calc-1",
    onec_status: "not_sent",
    blockers: [],
    total_amount: "39.00",
    lines: [line()],
    manual_status_options: { nonliquid: "Кандидат на неликвид", working: "Рабочий" },
    ...overrides,
  } satisfies ProcurementOrderFormation;
}

function labelPreview(overrides: Partial<ProcurementOrderLabelPreview> = {}) {
  return {
    order_id: 12,
    onec_number: "РБГУ0000543",
    onec_date: "2026-08-03",
    label_size: "50x40",
    source_checksum: "a".repeat(64),
    max_page_count: 1000,
    position_count: 1,
    product_label_count: 2,
    separator_count: 0,
    total_page_count: 2,
    export_file_count: 1,
    ready: true,
    blockers: [],
    rows: [
      {
        line_no: 1,
        onec_item_code: "062852",
        item_name: "Дисплей HUA NV 10 Pro",
        article_1c: "062852",
        barcode: "2900000636873",
        quantity: 2,
      },
    ],
    ...overrides,
  } satisfies ProcurementOrderLabelPreview;
}

async function proposeClassification() {
  fireEvent.click(screen.getByRole("button", { name: "Изменить классификацию" }));
  fireEvent.change(screen.getByPlaceholderText("Обязательная причина"), {
    target: { value: "старая модель, выводим карточку" },
  });
  fireEvent.click(screen.getByRole("button", { name: "На согласование" }));
}

async function proposePension() {
  fireEvent.click(screen.getByRole("button", { name: "Изменить классификацию" }));
  fireEvent.change(screen.getByRole("combobox", {
    name: "Новая классификация Дисплей для Huawei P10 Lite",
  }), { target: { value: "pension" } });
  fireEvent.change(screen.getByPlaceholderText("Обязательная причина"), {
    target: { value: "ведём аналог дешевле" },
  });
}

const versionConflict = {
  response: { status: 409, data: { detail: "order version changed; refresh the order" } },
};

describe("ProcurementOrderFormationApp компактный список и отчёты", () => {
  beforeEach(() => {
    window.__MM_BITRIX_LAUNCH__ = { domain: "crm.example.test" };
  });

  afterEach(() => {
    delete window.__MM_BITRIX_LAUNCH__;
    window.history.replaceState({}, "", "/");
    cleanup();
  });

  it("показывает ключевые сигналы и открывает три разные поверхности из меню отчётов", () => {
    const productLine = line({
      id: 42,
      line_number: 2,
      nomenclature_name: "Проблемная строка",
      blockers: ["defect_rate_suspected"],
      profitability_pct: "37.5",
      product_defect_pct: "0.4",
      risk_codes: ["adaptive_lead_time_sync_ready", "speed_horizon_rule_applied"],
    });

    render(<ProductionOrderFormationApp initialOrder={order({ lines: [productLine] })} />);

    expect(screen.getByRole("columnheader", { name: "Блокеры" })).toBeInTheDocument();
    expect(screen.getByText("1 блокер")).toBeInTheDocument();
    expect(screen.getByText("37,5%")).toBeInTheDocument();
    expect(screen.getByText("0,4%")).toBeInTheDocument();
    const compactRow = screen.getByText("Проблемная строка").closest("tr");
    expect(compactRow).not.toBeNull();
    expect(within(compactRow!).getAllByText("2")).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "Отчёты по товару Проблемная строка" }));

    expect(screen.getByRole("link", { name: /Показатели товара/ })).toHaveAttribute(
      "href",
      "/bitrix/procurement-order-formation?view=product_insights&productId=2695&orderId=12&lineId=42"
    );
    expect(screen.getByRole("link", { name: /Карточка Bitrix24/ })).toHaveAttribute(
      "href",
      "https://crm.example.test/crm/catalog/17/product/2695/"
    );

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("link", { name: /Показатели товара/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Отчёты по товару Проблемная строка" }));

    fireEvent.click(screen.getByRole("button", { name: /Подробный разбор строки/ }));
    expect(screen.getByRole("heading", { name: "Подробный разбор строки" })).toBeInTheDocument();
    expect(screen.getByText("Операционный отчёт · строка 2")).toBeInTheDocument();
    expect(screen.queryByRole("searchbox", { name: "Поиск товаров" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "К списку товаров" }));
    expect(screen.getByRole("searchbox", { name: "Поиск товаров" })).toBeInTheDocument();
  });

  it("фильтрует компактный список по состоянию и поисковой строке", () => {
    render(<ProductionOrderFormationApp initialOrder={order({
      lines: [
        line({ id: 41, nomenclature_name: "Готовая строка", blockers: [] }),
        line({ id: 42, line_number: 2, nomenclature_name: "Проблемная строка", blockers: ["defect_rate_suspected"] }),
      ],
    })} />);

    fireEvent.click(screen.getByRole("button", { name: "С блокерами1" }));
    expect(screen.getByText("Проблемная строка")).toBeInTheDocument();
    expect(screen.queryByText("Готовая строка")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Все2" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Поиск товаров" }), {
      target: { value: "Готовая" },
    });
    expect(screen.getByText("Готовая строка")).toBeInTheDocument();
    expect(screen.queryByText("Проблемная строка")).not.toBeInTheDocument();
  });
});

describe("ProcurementOrderFormationApp связанный процесс", () => {
  afterEach(cleanup);

  it("показывает стадию без второй кнопки открытия процесса", async () => {
    render(<ProcurementOrderFormationApp initialOrder={order({
      linked_process: {
        state: "linked",
        process_title: "Закупка/Заказ",
        entity_type_id: 1056,
        item_id: "324",
        category_id: 53,
        category_name: "Карго",
        stage_id: "DT1056_53:PAYREQ",
        stage_name: "Заявка на оплату / оплата в работе",
      },
    })} />);

    expect(screen.getByText("Закупка/Заказ №324")).toBeInTheDocument();
    expect(screen.getByText("Заявка на оплату / оплата в работе")).toBeInTheDocument();
    expect(screen.queryByText("Bitrix24")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Открыть процесс" })).not.toBeInTheDocument();
  });

  it("показывает диагностируемую ошибку товаров без нарушения связи", () => {
    render(<ProcurementOrderFormationApp initialOrder={order({
      linked_process: {
        state: "linked",
        process_title: "Закупка/Заказ",
        entity_type_id: 1056,
        item_id: "317",
        product_rows_sync: {
          state: "error",
          expected_count: 249,
          synced_count: 0,
          error: "Access denied",
        },
      },
    })} />);

    expect(screen.getByText("Закупка/Заказ №317")).toBeInTheDocument();
    expect(screen.getByText("Товары Smart Process не синхронизированы")).toBeInTheDocument();
    expect(screen.getByText("Ожидается позиций: 249. Access denied")).toBeInTheDocument();
  });

  it("показывает товарное зеркало и отдельно объясняет исключённые расходники", () => {
    render(<ProcurementOrderFormationApp initialOrder={order({
      linked_process: {
        state: "linked",
        process_title: "Закупка/Заказ",
        entity_type_id: 1056,
        item_id: "317",
        product_rows_sync: {
          state: "synced",
          expected_count: 1,
          synced_count: 1,
          excluded_count: 2,
          rows: [{
            line_number: 7,
            product_id: "2695",
            name: "Дисплей для Huawei P10 Lite",
            quantity: "3.000",
            purchase_price: "39.5000",
            currency: "CNY",
            sort: 70,
            catalog_matched: true,
          }],
        },
      },
    })} />);

    const mirror = screen.getByRole("region", { name: "Товары заказа" });
    expect(within(mirror).getByText("Источник: 1С · только для просмотра")).toBeInTheDocument();
    expect(within(mirror).getByText("1 позиция")).toBeInTheDocument();
    expect(within(mirror).getByText("Дисплей для Huawei P10 Lite")).toBeInTheDocument();
    expect(within(mirror).getByText("3")).toBeInTheDocument();
    expect(within(mirror).getByText("39,5")).toBeInTheDocument();
    expect(within(mirror).getByText("CNY")).toBeInTheDocument();
    expect(within(mirror).getByText(/Исключено из товарного зеркала: 2 расходника/)).toBeInTheDocument();
  });

  it("оставляет несопоставленный товар в таблице и показывает причину", () => {
    render(<ProcurementOrderFormationApp initialOrder={order({
      linked_process: {
        state: "linked",
        process_title: "Закупка/Заказ",
        entity_type_id: 1056,
        item_id: "314",
        product_rows_sync: {
          state: "error",
          error: "нет товара Bitrix в строке 1",
          rows: [{
            line_number: 1,
            product_id: null,
            name: "Кабель USB-C",
            quantity: "4",
            purchase_price: "2.1",
            currency: "CNY",
            sort: 10,
            catalog_matched: false,
          }],
        },
      },
    })} />);

    const mirror = screen.getByRole("region", { name: "Товары заказа" });
    expect(within(mirror).getByText("Кабель USB-C")).toBeInTheDocument();
    expect(within(mirror).getByText("Нет связи с каталогом Bitrix24")).toBeInTheDocument();
    expect(screen.getByText("Товары Smart Process не синхронизированы")).toBeInTheDocument();
  });

  it("показывает состояние немедленной синхронизации", () => {
    render(<ProcurementOrderFormationApp initialOrder={order({
      linked_process: {
        state: "pending",
        process_title: "Закупка/Заказ",
        entity_type_id: 1056,
      },
    })} />);

    expect(screen.getByText("Карточка создаётся…")).toBeInTheDocument();
    expect(screen.getByText("Заказ уже создан в 1С; связь с процессом проверяется")).toBeInTheDocument();
  });

  it("объясняет, почему у черновика ещё нет процесса", () => {
    render(<ProcurementOrderFormationApp initialOrder={order({
      linked_process: {
        state: "not_created",
        process_title: "Закупка/Заказ",
        entity_type_id: 1056,
      },
    })} />);

    expect(screen.getByText("Процесс ещё не создан")).toBeInTheDocument();
    expect(screen.getByText("Он появится после создания документа в 1С")).toBeInTheDocument();
  });
});

describe("ProcurementOrderFormationApp «Допродаём»", () => {
  beforeEach(() => {
    vi.mocked(createProcurementClassification).mockReset();
    vi.mocked(fetchProcurementOrder).mockReset();
    vi.mocked(toast.error).mockReset();
    vi.mocked(toast.success).mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
    cleanup();
  });

  it("требует код карточки-замены и отправляет его вместе со статусом", async () => {
    const withPension = order({
      manual_status_options: { pension: "Допродаём", working: "Рабочий" },
    });
    vi.mocked(createProcurementClassification).mockResolvedValue(withPension);

    render(<ProcurementOrderFormationApp initialOrder={withPension} />);
    await proposePension();

    const submit = screen.getByRole("button", { name: "Перевести в Допродаём" });
    expect(submit).toBeDisabled();

    fireEvent.change(screen.getByPlaceholderText("Взамен ведём: код 1С (РБ...)"), {
      target: { value: "РБ000057818" },
    });
    fireEvent.click(submit);

    await waitFor(() => expect(createProcurementClassification).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createProcurementClassification).mock.calls[0][2]).toMatchObject({
      proposed_status: "pension",
      replacement_sku_code: "РБ000057818",
      no_replacement: false,
    });
  });

  it("разрешает отправку без кода, когда модель снята с производства", async () => {
    const withPension = order({
      manual_status_options: { pension: "Допродаём", working: "Рабочий" },
    });
    vi.mocked(createProcurementClassification).mockResolvedValue(withPension);

    render(<ProcurementOrderFormationApp initialOrder={withPension} />);
    await proposePension();

    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Перевести в Допродаём" }));

    await waitFor(() => expect(createProcurementClassification).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createProcurementClassification).mock.calls[0][2]).toMatchObject({
      proposed_status: "pension",
      replacement_sku_code: null,
      no_replacement: true,
    });
  });
});

describe("ProcurementOrderFormationApp проблемные строки", () => {
  beforeEach(() => {
    window.__MM_BITRIX_LAUNCH__ = { domain: "crm.example.test" };
    vi.mocked(updateProcurementOrderLine).mockReset();
    vi.mocked(toast.success).mockReset();
    vi.mocked(toast.error).mockReset();
  });

  afterEach(() => {
    window.__MM_BITRIX_LAUNCH__ = undefined;
    vi.useRealTimers();
    cleanup();
  });

  it("поднимает проблемы вверх и показывает конкретные показатели и карточку товара", () => {
    const ready = line({ id: 41, line_number: 1, nomenclature_name: "Готовая строка" });
    const blocked = line({
      id: 42,
      line_number: 7,
      nomenclature_name: "Проблемная строка",
      blockers: ["defect_rate_suspected"],
      risk_codes: ["adaptive_lead_time_sync_ready", "speed_horizon_rule_applied"],
      product_card_url: "https://master-mobile.ru/catalog/displei/40699/",
      profitability_pct: "18.4",
      payload: {
        defect_share_pct: "12.6",
        defect_return_qty: "6",
        recommended_order_qty_raw: "14",
        order_rounding_price_gate: "no_purchase_price",
      },
    });

    render(<ProcurementOrderFormationApp initialOrder={order({ lines: [ready, blocked] })} />);

    const problem = screen.getByText("Проблемная строка");
    const readyName = screen.getByText("Готовая строка");
    expect(problem.compareDocumentPosition(readyName) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText(/Проблема: Высокий процент брака: 12,6%/)).toBeInTheDocument();
    expect(screen.getByText("Брак товара: 12,6% · поставщик не подтверждён")).toBeInTheDocument();
    expect(screen.getByText("Рентабельность: 18,4%")).toBeInTheDocument();
    expect(screen.getByText("Рентабельность: 18,4%")).toHaveAttribute(
      "title",
      "Доля прибыли в обороте, 180 дней"
    );
    expect(screen.getByText("Округление не применено: нет подтверждённой закупочной цены.")).toBeInTheDocument();
    expect(screen.queryByText(/Сигнал:/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Проблемная строка" })).toHaveAttribute(
      "href",
      "https://crm.example.test/crm/catalog/17/product/2695/"
    );
    expect(screen.getByRole("link", { name: "Карточка на сайте" })).toHaveAttribute(
      "href",
      "https://master-mobile.ru/catalog/displei/40699/"
    );
    const problemRow = problem.closest("tr");
    expect(problemRow).not.toBeNull();
    expect(within(problemRow!).getByText("1 блокер")).toBeInTheDocument();
    expect(within(problemRow!).getByText("Рентабельность 18,4%")).toBeInTheDocument();
    expect(within(problemRow!).getByText("Брак 12,6%")).toBeInTheDocument();
    expect(within(problemRow!).getByText("2 сигнала")).toHaveAttribute(
      "title",
      "Строка пересчитана по живым срокам и готова к заказу\nГоризонт заказа задан классом скорости"
    );
    const signalsLink = within(problemRow!).getByRole("link", { name: "Открыть отчёт показателей товара Проблемная строка" });
    expect(signalsLink).toHaveAttribute(
      "href",
      "/bitrix/procurement-order-formation?view=product_insights&productId=2695&orderId=12&lineId=42"
    );
    expect(signalsLink).not.toHaveAttribute("target");
    const insightsLink =
      screen.getAllByRole("link", { name: "Показатели товара" }).find(
        (item) => item.getAttribute("href")?.endsWith("lineId=42")
      );
    expect(insightsLink).toHaveAttribute(
      "href",
      "/bitrix/procurement-order-formation?view=product_insights&productId=2695&orderId=12&lineId=42"
    );
    expect(insightsLink).not.toHaveAttribute("target");
  });

  it("показывает причину, когда рентабельность действительно не рассчитана", () => {
    render(
      <ProcurementOrderFormationApp
        initialOrder={order({
          lines: [
            line({
              profitability_pct: null,
              profitability_status: "revenue_missing",
              profitability_explanation: "Чистая выручка за период отсутствует или равна нулю",
            }),
          ],
        })}
      />
    );

    expect(
      screen.getByText(
        "Рентабельность: не рассчитана: Чистая выручка за период отсутствует или равна нулю"
      )
    ).toBeInTheDocument();
  });

  it("скрывает семейную фразу, если количество между SKU не перераспределялось", () => {
    const familyReason = "В подтверждённом сегменте меньше двух доступных SKU; оставлено базовое количество.";
    render(
      <ProcurementOrderFormationApp
        initialOrder={order({
          lines: [line({
            recommendation_reason: familyReason,
            display_family_recommendation: {
              schema: "display_family_order_recommendation.v1",
              mode: "shadow",
              status: "identity_insufficient_eligible_skus",
              registry_inventory_checksum: "a".repeat(64),
              family_id: "family-1",
              family_label: "Apple iPhone",
              segment_id: "medium|incell",
              quality_segment: "medium",
              construction_segment: "incell",
              baseline_order_qty: "14",
              allocated_order_qty: "14",
              family_pool_order_qty: "14",
              segment_pool_order_qty: "14",
              baseline_share_pct: "100",
              target_share_pct: "100",
              allocation_source: "base_sku_order_pool",
              confidence: "low",
              manual_approval_required: true,
              registry_warning_codes: [],
              conflict_codes: [],
              reason_ru: familyReason,
            },
          })],
        })}
      />
    );

    expect(screen.queryByText(new RegExp(familyReason))).not.toBeInTheDocument();
  });

  it("открывает ручное поле «Взамен ведём» из семейного сигнала", () => {
    render(
      <ProcurementOrderFormationApp
        initialOrder={order({
          manual_status_options: {
            working: "Поддерживаем (Рабочий)",
            replace_candidate: "Кандидат на замену",
          },
          lines: [line({
            display_family_recommendation: {
              schema: "display_family_order_recommendation.v1",
              mode: "shadow",
              status: "identity_insufficient_eligible_skus",
              registry_inventory_checksum: "a".repeat(64),
              family_id: "family-1",
              family_label: "Apple iPhone",
              segment_id: "medium|incell",
              quality_segment: "medium",
              construction_segment: "incell",
              baseline_order_qty: "1",
              allocated_order_qty: "1",
              family_pool_order_qty: "1",
              segment_pool_order_qty: "1",
              baseline_share_pct: "100",
              target_share_pct: "100",
              allocation_source: "base_sku_order_pool",
              confidence: "low",
              manual_approval_required: true,
              registry_warning_codes: [],
              conflict_codes: [],
              reason_ru: "В сегменте нет второго SKU.",
            },
          })],
        })}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "Указать «Взамен ведём»" }));

    expect(screen.getByRole("combobox", {
      name: "Новая классификация Дисплей для Huawei P10 Lite",
    })).toHaveValue("replace_candidate");
    expect(screen.getByPlaceholderText("Взамен ведём: код 1С (РБ...)")).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "Ручной минимум Дисплей для Huawei P10 Lite" }))
      .toHaveAttribute("step", "1");
  });

  it("изменяет количество заказа целыми штуками", () => {
    render(<ProcurementOrderFormationApp initialOrder={order()} />);

    expect(screen.getByRole("spinbutton", { name: "Количество Дисплей для Huawei P10 Lite" }))
      .toHaveAttribute("step", "1");
  });

  it("считает отдельно типы блокеров и затронутые строки", () => {
    expect(procurementBlockerSummaryLabel([
      "line_1:defect_rate_suspected",
      "line_2:defect_rate_suspected",
      "line_3:batch_error_suspected",
      "line_4:batch_error_suspected",
    ])).toBe("2 блокера · 4 строки");
  });

  it("не показывает технический английский текст старого блокера семейства дисплеев", () => {
    const code = "display_family_recommendation_review_required";
    render(<ProcurementOrderFormationApp initialOrder={order({
      blockers: [`line_1:${code}`],
      blocker_details: [{
        code,
        scope: "order",
        severity: "hard",
        line_id: 40,
        line_number: 1,
        message: "display family recommendation review required",
        evidence: {},
        resolution_actions: [],
      }],
      lines: [line({ blockers: [code] })],
    })} />);

    expect(screen.getByText(
      "Требуется проверить и подтвердить распределение заказа внутри семейства дисплеев — строки 1"
    )).toBeInTheDocument();
    expect(screen.queryByText(/display family recommendation review required/i)).not.toBeInTheDocument();
  });

  it("показывает доказательства блокера и исключает строку только с причиной", async () => {
    const blockedLine = line({
      blockers: ["batch_error_suspected"],
      blocker_details: [{
        code: "batch_error_suspected",
        scope: "line",
        severity: "hard",
        line_id: 40,
        line_number: 1,
        message: "Подозрение на партийную ошибку: 8 возвратов качества «Новый», 44,4% от продаж за 90 дней (порог: 5 возвратов и 40%).",
        evidence: {
          return_qty: 8,
          share_pct: 44.4,
          minimum_return_qty: 5,
          minimum_share_pct: 40,
          window_days: 90,
          suspected_batch: "Партия 2026-07-15",
        },
        resolution_actions: [
          { kind: "remove_line", label: "Исключить строку", requires_reason: true },
          { kind: "recalculate", label: "Дождаться нового расчёта" },
        ],
      }],
      profitability_pct: "18.4",
    });
    const initial = order({
      blockers: ["line_1:batch_error_suspected"],
      blocker_details: [{ ...blockedLine.blocker_details![0], scope: "order" }],
      lines: [blockedLine],
    });
    vi.mocked(updateProcurementOrderLine).mockResolvedValue(
      order({ blockers: [], blocker_details: [], lines: [{ ...blockedLine, removed: true }] })
    );

    render(<ProcurementOrderFormationApp initialOrder={initial} />);

    expect(screen.getAllByText(/44,4% за 90 дней/).length).toBeGreaterThan(0);
    expect(screen.getByText("Возвраты партии: 44,4%")).toBeInTheDocument();
    expect(screen.getByText("8 возвратов · порог 5 возвратов и 40%")).toBeInTheDocument();
    expect(screen.getByText("Партия: 2026-07-15")).toBeInTheDocument();
    expect(screen.getByText("Подтверждённый брак поставщика: данных нет")).toBeInTheDocument();
    expect(screen.getByText("Рентабельность: 18,4%")).toBeInTheDocument();
    expect(screen.getByText("Подозрение на партийную ошибку — строки 1")).toBeInTheDocument();
    expect(screen.getByText("1С: Не отправлен")).toBeInTheDocument();
    expect(screen.getByText("Обычная закупка")).toBeInTheDocument();
    expect(screen.getByText("Товар Bitrix24: 2695")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Проверить и создать черновик в 1С" })).toBeDisabled();
    expect(screen.getByText("Сначала разберите строки 1.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Исключить строку" }));
    expect(screen.getByRole("dialog", { name: "Исключить строку 1" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Исключить строку" })).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText("Обязательно укажите, почему строку исключают"))
      .toHaveFocus();
    const submit = screen.getByRole("button", { name: "Исключить из проекта" });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByPlaceholderText("Обязательно укажите, почему строку исключают"), {
      target: { value: "Проверяем пересорт отдельно" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "Указать «Взамен ведём»" }));
    fireEvent.change(screen.getByLabelText("Взамен ведём для Дисплей для Huawei P10 Lite"), {
      target: { value: "РБ000057818" },
    });
    fireEvent.click(submit);

    await waitFor(() => expect(updateProcurementOrderLine).toHaveBeenCalledWith(12, 40, {
      expected_order_version: 1,
      expected_line_version: 1,
      removed: true,
      removal_reason: "Проверяем пересорт отдельно",
      replacement_sku_code: "РБ000057818",
    }));
    expect(toast.success).toHaveBeenCalledWith(
      "Строка исключена из проекта; причина сохранена в журнале"
    );
  });
});


describe("ProcurementOrderFormationApp комната разбора поставщиков", () => {
  beforeEach(() => {
    vi.mocked(searchProcurementSupplierOptions).mockReset();
    vi.mocked(selectProcurementLineMainSupplier).mockReset();
    vi.mocked(previewProcurementSupplierDistribution).mockReset();
  });

  afterEach(cleanup);

  it("назначает поставщика в строке и показывает предпросмотр разнесения", async () => {
    const room = order({
      supplier_ref: null,
      supplier_code: null,
      supplier_name: "Не определён",
      blockers: ["line_1:supplier_1c_reference_missing"],
      lines: [line({ blockers: ["supplier_1c_reference_missing"] })],
    });
    const selected = order({
      ...room,
      version: 2,
      lines: [line({
        version: 2,
        blockers: ["supplier_1c_reference_missing"],
        payload: {
          main_supplier_selection: {
            ref: "0xsamsung",
            code: "S9",
            name: "Samsung display",
            status: "pending_onec_write",
          },
        },
      })],
    });
    vi.mocked(searchProcurementSupplierOptions).mockResolvedValue([
      { ref: "0xsamsung", code: "S9", name: "Samsung display" },
    ]);
    vi.mocked(selectProcurementLineMainSupplier).mockResolvedValue(selected);
    vi.mocked(previewProcurementSupplierDistribution).mockResolvedValue({
      source_order_id: 12,
      source_order_version: 2,
      groups: [{
        supplier_ref: "0xsamsung",
        supplier_code: "S9",
        supplier_name: "Samsung display",
        line_ids: [40],
        line_numbers: [1],
        nomenclature_codes: ["РБ000031600"],
        target_order_id: null,
        target_order_status: "new",
      }],
      unresolved_line_numbers: [],
    });

    render(<ProcurementOrderFormationApp initialOrder={room} />);
    fireEvent.change(screen.getByLabelText("Поставщик Дисплей для Huawei P10 Lite"), {
      target: { value: "Samsung" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Найти" }));
    await waitFor(() => expect(searchProcurementSupplierOptions).toHaveBeenCalledWith("Samsung"));
    fireEvent.click(await screen.findByRole("button", { name: /Samsung display/ }));
    await waitFor(() => expect(selectProcurementLineMainSupplier).toHaveBeenCalledTimes(1));
    expect(screen.getByText(/выбран, в карточке ещё не записан/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Разнести по поставщикам" }));
    expect(await screen.findByText("Предпросмотр разнесения")).toBeInTheDocument();
    expect(screen.getByText(/будет создан новый проект/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Подтвердить разнесение" })).toBeEnabled();
  });
});

describe("ProcurementOrderFormationApp version conflicts", () => {
  beforeEach(() => {
    vi.mocked(createProcurementClassification).mockReset();
    vi.mocked(fetchProcurementOrder).mockReset();
    vi.mocked(toast.success).mockReset();
    vi.mocked(toast.error).mockReset();
  });

  afterEach(cleanup);

  it("перезагружает карточку и повторяет отправку, когда версия заказа выросла в другом окне", async () => {
    const refreshed = order({ version: 2 });
    const saved = order({ version: 3, lines: [line({ version: 1 })] });
    vi.mocked(createProcurementClassification)
      .mockRejectedValueOnce(versionConflict)
      .mockResolvedValueOnce(saved);
    vi.mocked(fetchProcurementOrder).mockResolvedValue(refreshed);

    render(<ProcurementOrderFormationApp initialOrder={order()} />);
    await proposeClassification();

    await waitFor(() => expect(createProcurementClassification).toHaveBeenCalledTimes(2));
    expect(vi.mocked(createProcurementClassification).mock.calls[0][2]).toMatchObject({
      expected_order_version: 1,
      expected_line_version: 1,
    });
    expect(vi.mocked(createProcurementClassification).mock.calls[1][2]).toMatchObject({
      expected_order_version: 2,
      expected_line_version: 1,
    });
    expect(toast.error).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText(/версия 3/)).toBeInTheDocument());
  });

  it("не повторяет отправку и предупреждает по-русски, когда изменилась сама строка", async () => {
    vi.mocked(createProcurementClassification).mockRejectedValueOnce(versionConflict);
    vi.mocked(fetchProcurementOrder).mockResolvedValue(
      order({ version: 2, lines: [line({ version: 2, final_quantity: "5.000" })] })
    );

    render(<ProcurementOrderFormationApp initialOrder={order()} />);
    await proposeClassification();

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Строку уже изменили в другом окне. Карточка обновлена — проверьте данные и повторите."
      )
    );
    expect(createProcurementClassification).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByText(/версия 2/)).toBeInTheDocument());
  });

  it("показывает понятное русское сообщение вместо технического текста бэкенда", async () => {
    vi.mocked(createProcurementClassification).mockRejectedValue({
      response: {
        status: 403,
        data: { detail: "user cannot approve product classification" },
      },
    });

    render(<ProcurementOrderFormationApp initialOrder={order()} />);
    await proposeClassification();

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("У вас нет прав согласовывать классификацию товара.")
    );
    expect(fetchProcurementOrder).not.toHaveBeenCalled();
  });

  it("показывает защищённое ручное решение и исчезнувшую потребность", () => {
    render(
      <ProcurementOrderFormationApp
        initialOrder={order({
          lines: [
            line({
              removed: true,
              final_quantity: "7.000",
              purchase_price: "90.0000",
              payload: {
                need_status: "disappeared",
                manual_overrides: { final_quantity: true, purchase_price: true },
                recommendation_discrepancy: {
                  final_quantity: { manual: "7.000", recommended: "9" },
                  purchase_price: { manual: "90.0000", recommended: "110" },
                },
              },
            }),
          ],
        })}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /Исключённые строки: 1/ }));
    expect(screen.getByText(/Проблема: Потребность исчезла в новом расчёте/)).toBeInTheDocument();
    expect(screen.getByText(/Решение человека: 7 · новый расчёт: 9/)).toBeInTheDocument();
    expect(screen.getByText(/Цена человека: 90,00.*· новая цена: 110,00/)).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "Количество Дисплей для Huawei P10 Lite" }))
      .toHaveValue(7);
    expect(screen.getByRole("button", { name: "Сохранить количество и цену" })).toBeDisabled();
  });
});

describe("ProcurementOrderFormationApp массовые этикетки", () => {
  beforeEach(() => {
    vi.mocked(downloadProcurementOrderLabels).mockReset();
    vi.mocked(fetchProcurementOrderLabelPreview).mockReset();
    vi.mocked(linkProcurementOrderLabelSource).mockReset();
    vi.mocked(toast.error).mockReset();
    vi.mocked(toast.success).mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
    cleanup();
  });

  it("подключает существующий заказ 1С, показывает состав и лимит до таблицы заказа", async () => {
    vi.mocked(linkProcurementOrderLabelSource).mockResolvedValue({
      label_source: {
        origin: "manual",
        onec_number: "РБГУ0000543",
        onec_date: "2026-08-03",
        linked_at: "2026-08-31T12:00:00",
      },
      preview: labelPreview(),
    });
    render(<ProcurementOrderFormationApp initialOrder={order()} />);

    const labels = screen.getByRole("region", { name: "Массовые этикетки" });
    const orderTable = document.querySelector(".order-formation__table");
    expect(orderTable).not.toBeNull();
    expect(
      labels.compareDocumentPosition(orderTable as Node) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
    expect(screen.getByText(/Один номер для всех товаров проекта/)).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Введите номер заказа 1С")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Подключить весь заказ" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Скачать PDF" })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Полный номер заказа 1С для этикеток"), {
      target: { value: "РБГУ0000543" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Подключить весь заказ" }));

    await waitFor(() => expect(linkProcurementOrderLabelSource).toHaveBeenCalledWith(
      12,
      "РБГУ0000543",
      "50x40"
    ));
    await waitFor(() => {
      expect(screen.getByText("Страниц:").parentElement).toHaveTextContent("2 / 1000");
    });
    expect(fetchProcurementOrderLabelPreview).not.toHaveBeenCalled();
    expect(screen.getByText(/Дисплей HUA NV 10 Pro/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Скачать PDF" })).toBeEnabled();
  });

  it("заменяет ошибочную ручную привязку", async () => {
    vi.mocked(fetchProcurementOrderLabelPreview).mockResolvedValue(labelPreview({
      onec_number: "РБГУ0000496",
    }));
    vi.mocked(linkProcurementOrderLabelSource).mockResolvedValue({
      label_source: {
        origin: "manual",
        onec_number: "РБГУ0000543",
        onec_date: "2026-08-03",
        linked_at: "2026-08-31T12:00:00",
      },
      preview: labelPreview(),
    });
    render(
      <ProcurementOrderFormationApp
        initialOrder={order({
          label_source: {
            origin: "manual",
            onec_number: "РБГУ0000496",
            onec_date: "2026-08-01",
          },
        })}
      />
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Обновить данные из 1С" })).toBeEnabled();
    });
    fireEvent.click(screen.getByText("Изменить привязанный заказ 1С"));
    fireEvent.change(screen.getByLabelText("Полный номер заказа 1С для этикеток"), {
      target: { value: "РБГУ0000543" },
    });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Сохранить другой заказ" })).toBeEnabled();
    });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить другой заказ" }));

    await waitFor(() => expect(linkProcurementOrderLabelSource).toHaveBeenCalledWith(
      12,
      "РБГУ0000543",
      "50x40"
    ));
    expect(screen.getByText(/Заказ 1С РБГУ0000543/)).toBeInTheDocument();
  });

  it("автоматически проверяет весь привязанный заказ при открытии и смене размера", async () => {
    vi.mocked(fetchProcurementOrderLabelPreview)
      .mockResolvedValueOnce(labelPreview())
      .mockResolvedValueOnce(labelPreview({ label_size: "40x30" }));
    render(
      <ProcurementOrderFormationApp
        initialOrder={order({
          label_source: {
            origin: "manual",
            onec_number: "РБГУ0000543",
            onec_date: "2026-08-03",
          },
        })}
      />
    );

    await waitFor(() => expect(fetchProcurementOrderLabelPreview).toHaveBeenCalledWith(
      12,
      "50x40"
    ));
    await waitFor(() => expect(screen.getByRole("button", { name: "Скачать PDF" })).toBeEnabled());

    fireEvent.change(screen.getByRole("combobox", { name: "Размер этикетки" }), {
      target: { value: "40x30" },
    });

    await waitFor(() => expect(fetchProcurementOrderLabelPreview).toHaveBeenLastCalledWith(
      12,
      "40x30"
    ));
    await waitFor(() => expect(screen.getByRole("button", { name: "Скачать PDF" })).toBeEnabled());
  });

  it("передаёт checksum проверенного preview и безопасно завершает скачивание", async () => {
    const preview = labelPreview();
    vi.mocked(fetchProcurementOrderLabelPreview).mockResolvedValue(preview);
    vi.mocked(downloadProcurementOrderLabels).mockResolvedValue({
      blob: new Blob(["pdf"]),
      filename: "supplier-order-РБГУ0000543-labels-50x40.pdf",
    });
    const createObjectURL = vi.fn(() => "blob:labels");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });
    render(
      <ProcurementOrderFormationApp
        initialOrder={order({
          label_source: {
            origin: "manual",
            onec_number: "РБГУ0000543",
            onec_date: "2026-08-03",
          },
        })}
      />
    );

    await waitFor(() => expect(screen.getByRole("button", { name: "Скачать PDF" })).toBeEnabled());
    vi.useFakeTimers();
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    fireEvent.click(screen.getByRole("button", { name: "Скачать PDF" }));

    await act(async () => Promise.resolve());
    expect(downloadProcurementOrderLabels).toHaveBeenCalledWith(
      12,
      "50x40",
      "pdf",
      preview.source_checksum
    );
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    const link = document.querySelector<HTMLAnchorElement>(`a[download="supplier-order-РБГУ0000543-labels-50x40.pdf"]`);
    expect(link).toBeInTheDocument();
    expect(revokeObjectURL).not.toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(1000));

    expect(link).not.toBeInTheDocument();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:labels");
    clickSpy.mockRestore();
  });

  it("передаёт checksum проверенного preview при скачивании XLSX", async () => {
    const preview = labelPreview();
    vi.mocked(fetchProcurementOrderLabelPreview).mockResolvedValue(preview);
    vi.mocked(downloadProcurementOrderLabels).mockResolvedValue({
      blob: new Blob(["xlsx"]),
      filename: "supplier-order-РБГУ0000543-labels-50x40.xlsx",
    });
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => "blob:labels-xlsx"),
    });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(
      <ProcurementOrderFormationApp
        initialOrder={order({
          label_source: {
            origin: "manual",
            onec_number: "РБГУ0000543",
            onec_date: "2026-08-03",
          },
        })}
      />
    );

    await waitFor(() => expect(screen.getByRole("button", { name: "Скачать XLSX" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Скачать XLSX" }));

    await waitFor(() => expect(downloadProcurementOrderLabels).toHaveBeenCalledWith(
      12,
      "50x40",
      "xlsx",
      preview.source_checksum
    ));
    clickSpy.mockRestore();
  });

  it("показывает каждый blocker отдельным пунктом и блокирует файлы", async () => {
    vi.mocked(fetchProcurementOrderLabelPreview).mockResolvedValue(labelPreview({
      ready: false,
      blockers: ["строка 1: не найден штрихкод 1С"],
    }));
    render(
      <ProcurementOrderFormationApp
        initialOrder={order({
          label_source: {
            origin: "exchange",
            onec_number: "РБГУ0000543",
            onec_date: "2026-08-03",
          },
        })}
      />
    );

    await waitFor(() => expect(screen.getAllByRole("listitem")).toHaveLength(1));
    expect(screen.getByRole("button", { name: "Скачать PDF" })).toBeDisabled();
    expect(screen.queryByLabelText("Полный номер заказа 1С для этикеток")).not.toBeInTheDocument();
  });

  it("для импортированного заказа показывает источник и скрывает повторную передачу", async () => {
    vi.mocked(fetchProcurementOrderLabelPreview).mockResolvedValue(labelPreview({
      onec_number: "РБГУ0000590",
    }));
    render(
      <ProcurementOrderFormationApp
        initialOrder={order({
          origin: "onec_import",
          batch_id: "onec-РБГУ0000590",
          onec_document_number: "РБГУ0000590",
          label_source: {
            origin: "exchange",
            onec_number: "РБГУ0000590",
            onec_date: "2026-08-31",
          },
          blockers: ["supplier_contract_missing", "warehouse_missing"],
          lines: [line({ bitrix_product_id: null, source_kind: "onec_import" })],
        })}
      />
    );

    expect(screen.getByText("Источник").parentElement).toHaveTextContent(
      "Заказ 1С РБГУ0000590"
    );
    expect(screen.queryByText("onec-РБГУ0000590")).not.toBeInTheDocument();
    expect(screen.queryByText("Передача заблокирована")).not.toBeInTheDocument();
    expect(screen.getByText("Связь с каталогом Bitrix24 обновляется")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Проверить и создать черновик в 1С" })
    ).not.toBeInTheDocument();
  });

  it("разрешает крупный заказ и предупреждает о ZIP-архиве", async () => {
    vi.mocked(fetchProcurementOrderLabelPreview).mockResolvedValue(labelPreview({
      onec_number: "РБГУ0000590",
      product_label_count: 5765,
      separator_count: 248,
      total_page_count: 6013,
      export_file_count: 7,
    }));
    render(
      <ProcurementOrderFormationApp
        initialOrder={order({
          origin: "onec_import",
          onec_document_number: "РБГУ0000590",
          label_source: {
            origin: "exchange",
            onec_number: "РБГУ0000590",
            onec_date: "2026-08-31",
          },
        })}
      />
    );

    expect(await screen.findByText("7 файлов")).toBeInTheDocument();
    expect(screen.getByText(/до 1000 страниц каждый/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Скачать PDF" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Скачать XLSX" })).toBeEnabled();
  });
});
