import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import toast from "react-hot-toast";
import type { ProcurementOrderAssistant } from "../api/procurementAssortment";
import {
  approveProcurementClassification,
  assembleProcurementOrderProjects,
  confirmProcurementMatchingReview,
  fetchProcurementOrderAssistant,
  rejectProcurementClassification,
  updateProcurementSupplierProfile,
} from "../api/procurementAssortment";
import { ProcurementOrderAssistant as ProcurementOrderAssistantView } from "./ProcurementOrderAssistant";

vi.mock("../api/procurementAssortment", () => ({
  assembleProcurementOrderProjects: vi.fn(),
  approveProcurementClassification: vi.fn(),
  confirmProcurementMatchingReview: vi.fn(),
  fetchProcurementOrderAssistant: vi.fn(),
  rejectProcurementClassification: vi.fn(),
  updateProcurementSupplierProfile: vi.fn(),
}));

vi.mock("react-hot-toast", () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

function assistantData(photoOriginal: string | null = "https://cdn.example.test/original/display.jpg"): ProcurementOrderAssistant {
  return {
    updated_at: "2026-08-01T10:00:00",
    summary: {
      lines: 1,
      ready_lines: photoOriginal ? 1 : 0,
      supplier_missing_lines: 0,
      price_changed_lines: 1,
      low_profitability_lines: 0,
      high_defect_lines: 0,
      photo_missing_lines: photoOriginal ? 0 : 1,
      orders: 1,
    },
    orders: [{
      id: 12,
      stable_key: "order-12",
      status: "draft",
      version: 1,
      supplier_ref: "supplier-ref",
      supplier_name: "Tianma",
      contract_ref: "contract-ref",
      contract_name: "Основной договор",
      currency: "USD",
      warehouse_name: "Главный склад",
      procurement_contour: "ordinary",
      route: "ordinary",
      batch_id: "2026-08-01",
      order_date: "2026-08-05",
      calculation_id: "calc-1",
      onec_status: "not_sent",
      blockers: [],
      total_amount: "578.40",
      manual_status_options: {},
      supplier_profile: {
        qualification_class: "A",
        qualification_label: "Лучшие условия",
        profitability_pct: "34.6",
        defect_pct: "0.8",
        defect_history_units: 1842,
        on_time_pct: "94",
        payment_terms: "30/70",
        credit_days: 45,
        credit_limit: "25000",
        advantages: ["Компенсация брака", "Быстрый ответ"],
        history_order_count: 24,
        updated_at: "2026-08-01",
        data_status: "ready",
      },
      lines: [{
        id: 40,
        line_number: 1,
        version: 1,
        bitrix_product_id: "2695",
        bitrix_product_xml_id: "11111111-2222-3333-4444-555555555555",
        nomenclature_ref: "11111111-2222-3333-4444-555555555555",
        nomenclature_code: "MMI-15P-OLED-TM",
        nomenclature_name: "Дисплей iPhone 15 Pro OLED",
        recommended_quantity: "12",
        final_quantity: "12",
        purchase_price: "48.2",
        amount: "578.4",
        currency: "USD",
        source_kind: "automatic",
        explicit_demand: false,
        risk_codes: [],
        blockers: [],
        payload: {},
        display_family_recommendation: {
          schema: "display_family_order_recommendation.v1",
          mode: "active_registry_order_pool_shadow_v1",
          status: "allocated_shadow",
          registry_version_number: 2,
          registry_inventory_checksum: "a".repeat(64),
          family_record_id: 10,
          family_id: "family-iphone-15-pro",
          family_label: "Apple iPhone 15 Pro",
          registry_member_count: 3,
          calculation_member_count: 2,
          segment_id: "premium|soft_oled",
          quality_segment: "premium",
          construction_segment: "soft_oled",
          baseline_order_qty: "14",
          allocated_order_qty: "12",
          family_pool_order_qty: "20",
          segment_pool_order_qty: "20",
          baseline_share_pct: "70",
          target_share_pct: "60",
          allocation_source: "completed_sales_rate_30_90",
          confidence: "medium",
          manual_approval_required: true,
          registry_warning_codes: [],
          conflict_codes: ["accepted_matching_review"],
          reason_ru: "Пул распределён внутри подтверждённого сегмента.",
        },
        removed: false,
        photo_thumbnail_url: photoOriginal ? "https://cdn.example.test/thumb/display.jpg" : null,
        photo_original_url: photoOriginal,
        product_card_url: photoOriginal ? "https://master-mobile.ru/catalog/displei/40699/" : null,
        photo_source: photoOriginal ? "master_mobile_site" : null,
        photo_count: photoOriginal ? 1 : 0,
        profitability_pct: "34.6",
        supplier_defect_pct: "0.8",
        supplier_defect_history_units: 1842,
        price_change_pct: "-2.1",
        delivery_days: 12,
      }],
    }],
  };
}

describe("ProcurementOrderAssistant", () => {
  beforeEach(() => {
    window.__MM_BITRIX_LAUNCH__ = { domain: "crm.example.test" };
    vi.mocked(fetchProcurementOrderAssistant).mockReset();
    vi.mocked(assembleProcurementOrderProjects).mockReset();
    vi.mocked(approveProcurementClassification).mockReset();
    vi.mocked(confirmProcurementMatchingReview).mockReset();
    vi.mocked(rejectProcurementClassification).mockReset();
    vi.mocked(updateProcurementSupplierProfile).mockReset();
    vi.mocked(toast.success).mockReset();
    vi.mocked(toast.error).mockReset();
  });

  afterEach(cleanup);

  it("показывает утверждённые показатели, исходное фото и не возвращает колонку блокеров", async () => {
    vi.mocked(fetchProcurementOrderAssistant).mockResolvedValue(assistantData());

    render(<ProcurementOrderAssistantView />);

    expect(await screen.findByRole("heading", { name: "Помощник заказов" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Рентабельность" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Брак" })).toBeInTheDocument();
    expect(screen.queryByText("Что мешает")).not.toBeInTheDocument();
    expect(screen.getAllByText("Класс A").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Лучшие условия").length).toBeGreaterThan(0);
    expect(screen.getAllByText("30/70").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: /Открыть карточку товара/ })).toHaveAttribute(
      "href",
      "https://crm.example.test/crm/catalog/17/product/2695/"
    );
    expect(screen.getByRole("link", { name: /Открыть исходное фото/ })).toHaveAttribute(
      "href",
      "https://cdn.example.test/original/display.jpg"
    );
    expect(screen.getByRole("link", { name: "Дисплей iPhone 15 Pro OLED" })).toHaveAttribute(
      "href",
      "https://crm.example.test/crm/catalog/17/product/2695/"
    );
    expect(screen.getByRole("link", { name: "Карточка на сайте" })).toHaveAttribute(
      "href",
      "https://master-mobile.ru/catalog/displei/40699/"
    );
    expect(screen.getByText("Семья · только вручную")).toBeInTheDocument();
    expect(screen.getByText(/SKU: 14 → 12 шт/)).toBeInTheDocument();
  });

  it("не скрывает исчезнувшую потребность и показывает новую рекомендацию", async () => {
    const data = assistantData();
    data.orders[0].lines[0].removed = true;
    data.orders[0].lines[0].final_quantity = "7";
    data.orders[0].lines[0].payload = {
      need_status: "disappeared",
      recommendation_discrepancy: {
        final_quantity: { manual: "7", recommended: "9" },
        purchase_price: { manual: "48.2", recommended: "50" },
      },
    };
    vi.mocked(fetchProcurementOrderAssistant).mockResolvedValue(data);

    render(<ProcurementOrderAssistantView />);

    expect(await screen.findAllByText("Потребность исчезла")).not.toHaveLength(0);
    expect(screen.getByText("Новый расчёт: 9 шт.")).toBeInTheDocument();
    expect(screen.getByText(/Новая цена:/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Выбрать Дисплей iPhone 15 Pro OLED/)).toBeDisabled();
  });

  it("собирает только готовую полностью выбранную группу и не отправляет её в 1С", async () => {
    const data = assistantData();
    vi.mocked(fetchProcurementOrderAssistant).mockResolvedValue(data);
    vi.mocked(assembleProcurementOrderProjects).mockResolvedValue({
      approved: 1,
      blocked: 0,
      stale: 0,
      items: [{ order_id: 12, status: "approved", message: "Проект заказа собран" }],
    });

    render(<ProcurementOrderAssistantView />);
    fireEvent.click(await screen.findByRole("button", { name: "Собрать 1 проект заказа" }));

    await waitFor(() => expect(assembleProcurementOrderProjects).toHaveBeenCalledWith(data.orders));
    expect(toast.success).toHaveBeenCalledWith("Собрано проектов заказов: 1");
    expect(screen.getByText(/Проекты не будут отправлены в 1С автоматически/)).toBeInTheDocument();
  });

  it("не позволяет собрать проект без исходного фото", async () => {
    vi.mocked(fetchProcurementOrderAssistant).mockResolvedValue(assistantData(null));

    render(<ProcurementOrderAssistantView />);

    expect(await screen.findByText("Нет фото")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Собрать 0/ })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /Выбрать Дисплей/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Включить" })).toBeDisabled();
    expect(screen.getAllByText("Недоступно: не найдена точная карточка товара").length).toBeGreaterThan(0);
    expect(assembleProcurementOrderProjects).not.toHaveBeenCalled();
  });

  it("использует один переключатель решения, который можно включать повторно", async () => {
    vi.mocked(fetchProcurementOrderAssistant).mockResolvedValue(assistantData());

    render(<ProcurementOrderAssistantView />);

    const included = await screen.findByRole("button", { name: "Включено" });
    expect(included).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(included);
    const excluded = screen.getByRole("button", { name: "Включить" });
    expect(excluded).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: /Собрать 0/ })).toBeDisabled();
    fireEvent.click(excluded);
    expect(screen.getByRole("button", { name: "Включено" })).toHaveAttribute(
      "aria-pressed",
      "true"
    );
  });

  it("фильтрует очередь быстрыми кнопками и показывает оба варианта пакета", async () => {
    vi.mocked(fetchProcurementOrderAssistant).mockResolvedValue(assistantData());

    render(<ProcurementOrderAssistantView />);
    expect(await screen.findByText("Дисплей iPhone 15 Pro OLED")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Подтверждённый брак >10%/ }));
    expect(screen.queryByText("Дисплей iPhone 15 Pro OLED")).not.toBeInTheDocument();
    expect(screen.getByText("По выбранным фильтрам строк нет.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^Все1$/ }));
    fireEvent.click(screen.getByRole("button", { name: "Все фильтры" }));
    expect(screen.getByPlaceholderText("Товар, код или поставщик")).toBeInTheDocument();
    expect(screen.getByText("Пакет поставщику")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Список + фото" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Фото отдельно" })).toBeInTheDocument();
  });

  it("показывает полное предложение в панели и требует причину отклонения", async () => {
    const data = assistantData();
    const order = data.orders[0];
    const line = order.lines[0];
    line.blockers = ["classification_approval_pending"];
    order.blockers = ["line_1:classification_approval_pending"];
    line.latest_classification = {
      id: 77,
      status: "proposed",
      previous_status: "working",
      proposed_status: "matrix",
      proposed_status_label: "Матричный",
      reason: "Товар нужен в постоянной матрице",
      blocks_order_line: false,
      requested_at: "2026-08-01T10:00:00",
      requested_by_bitrix_user_id: "77",
      requested_by_name: "Автор предложения",
      onec_status: "not_sent",
      can_approve: true,
      can_reject: true,
    };
    vi.mocked(fetchProcurementOrderAssistant).mockResolvedValue(data);
    vi.mocked(rejectProcurementClassification).mockResolvedValue({
      order,
      proposal: { ...line.latest_classification, status: "rejected" },
    });

    render(<ProcurementOrderAssistantView />);
    fireEvent.click(await screen.findByRole("button", { name: "Tianma" }));
    expect(await screen.findByText("working → Матричный")).toBeInTheDocument();
    expect(screen.getByText("Автор предложения")).toBeInTheDocument();
    expect(screen.getByText("Товар нужен в постоянной матрице")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Принять" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Отклонить" }));
    expect(screen.getByText("Выберите причину, чтобы отклонить предложение.")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: /Причина отклонения/ }), {
      target: { value: "Недостаточно подтверждённых данных" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Отклонить" }));

    await waitFor(() => expect(rejectProcurementClassification).toHaveBeenCalledWith(
      order.id,
      line.id,
      77,
      {
        expected_order_version: order.version,
        expected_line_version: line.version,
        reason: "Недостаточно подтверждённых данных",
      }
    ));
  });

  it("открывает профиль по поставщику и позволяет закрыть правую панель", async () => {
    vi.mocked(fetchProcurementOrderAssistant).mockResolvedValue(assistantData());

    render(<ProcurementOrderAssistantView />);
    expect(screen.queryByRole("complementary", { name: "Профиль поставщика Tianma" })).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Tianma" }));
    expect(screen.getByRole("complementary", { name: "Профиль поставщика Tianma" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Закрыть панель поставщика" }));
    expect(screen.queryByRole("complementary", { name: "Профиль поставщика Tianma" })).not.toBeInTheDocument();
  });

  it("сохраняет ручной класс поставщика с ожидаемой версией", async () => {
    const data = assistantData();
    const profile = data.orders[0].supplier_profile!;
    profile.version = 2;
    profile.can_edit = true;
    vi.mocked(fetchProcurementOrderAssistant).mockResolvedValue(data);
    vi.mocked(updateProcurementSupplierProfile).mockResolvedValue({
      ...profile,
      version: 3,
      qualification_class: "B",
    });

    render(<ProcurementOrderAssistantView />);
    fireEvent.click(await screen.findByRole("button", { name: "Tianma" }));
    fireEvent.click(await screen.findByRole("button", { name: "Изменить профиль" }));
    fireEvent.change(screen.getByLabelText("Класс"), { target: { value: "B" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить профиль" }));

    await waitFor(() => expect(updateProcurementSupplierProfile).toHaveBeenCalledWith(
      "supplier-ref",
      expect.objectContaining({ expected_version: 2, qualification_class: "B" })
    ));
  });

  it("фильтр «Можно собрать» не показывает строки со снятой потребностью", async () => {
    // Счётчик считал только живые строки, а таблица показывала ещё и снятые
    // строки того же заказа: на кнопке было 31, в списке — 67.
    const data = assistantData();
    const gone = structuredClone(data.orders[0].lines[0]);
    gone.id = 41;
    gone.line_number = 2;
    gone.nomenclature_name = "Дисплей снятый";
    gone.removed = true;
    data.orders[0].lines.push(gone);
    vi.mocked(fetchProcurementOrderAssistant).mockResolvedValue(data);

    render(<ProcurementOrderAssistantView />);

    // «Все» показывает столько строк, сколько реально видно, включая снятые.
    expect(await screen.findByText("Дисплей снятый")).toBeInTheDocument();
    const allButton = screen
      .getAllByRole("button")
      .find((button) => button.textContent?.startsWith("Все"));
    expect(allButton?.textContent).toBe("Все2");

    fireEvent.click(screen.getByRole("button", { name: /^Можно собрать/ }));

    await waitFor(() => expect(screen.queryByText("Дисплей снятый")).not.toBeInTheDocument());
    expect(screen.getByText("1 строк в текущем фильтре")).toBeInTheDocument();
  });

  it("поднимает проблемные строки, объясняет проект и открывает первый блокер", async () => {
    const data = assistantData();
    const base = data.orders[0].lines[0];
    const safe = { ...structuredClone(base), id: 44, line_number: 1, nomenclature_name: "Готовая строка" };
    const problemLines = [23, 31, 36].map((lineNumber, index) => ({
      ...structuredClone(base),
      id: 50 + index,
      line_number: lineNumber,
      nomenclature_name: `Проблемная строка ${lineNumber}`,
      blockers: ["batch_error_suspected"],
      blocker_details: [{
        code: "batch_error_suspected",
        scope: "line",
        severity: "hard",
        line_id: 50 + index,
        line_number: lineNumber,
        message: "Подозрение на партийную ошибку: 8 возвратов, 44,4% за 90 дней.",
        evidence: { return_qty: 8, share_pct: 44.4, window_days: 90 },
        resolution_actions: [{ kind: "remove_line", label: "Исключить строку", requires_reason: true }],
      }],
    }));
    data.orders[0].id = 94;
    data.orders[0].lines = [safe, ...problemLines];
    data.orders[0].blockers = problemLines.map(
      (item) => `line_${item.line_number}:batch_error_suspected`
    );
    data.orders[0].blocker_details = problemLines.map((item) => ({
      ...item.blocker_details[0],
      scope: "order",
    }));
    vi.mocked(fetchProcurementOrderAssistant).mockResolvedValue(data);
    const onOpenOrder = vi.fn();

    render(<ProcurementOrderAssistantView onOpenOrder={onOpenOrder} />);

    const firstProblem = await screen.findByText("Проблемная строка 23");
    const safeName = screen.getByText("Готовая строка");
    expect(firstProblem.compareDocumentPosition(safeName) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
    expect(screen.getAllByText(
      "Проект №94 заблокирован: подозрение на партийную ошибку — строки 23, 31, 36"
    )).toHaveLength(1);
    expect(screen.getByText("1 причина · 3 проблемные строки")).toBeInTheDocument();
    const action = screen.getByRole("button", { name: "Разобрать 3 проблемные строки" });
    expect(action).toBeEnabled();
    fireEvent.click(action);
    expect(onOpenOrder).toHaveBeenCalledWith(94, 50);
    expect(screen.queryByText("batch_error_suspected")).not.toBeInTheDocument();
  });

  it("сохраняет проверку сопоставления по версии реестра", async () => {
    const data = assistantData();
    vi.mocked(fetchProcurementOrderAssistant).mockResolvedValue(data);
    vi.mocked(confirmProcurementMatchingReview).mockResolvedValue({
      order_id: 12,
      line_id: 40,
      family_id: 10,
      nomenclature_code: "MMI-15P-OLED-TM",
      registry_version_number: 2,
      registry_inventory_checksum: "a".repeat(64),
      confirmed_at: "2026-08-20T10:00:00",
      confirmed_by: "Омар",
      idempotent: false,
    });

    render(<ProcurementOrderAssistantView />);
    fireEvent.click(await screen.findByRole("button", { name: "Сопоставление проверено" }));

    await waitFor(() => expect(confirmProcurementMatchingReview).toHaveBeenCalledWith(12, 40, {
      expected_registry_version_number: 2,
      expected_registry_inventory_checksum: "a".repeat(64),
    }));
    expect(toast.success).toHaveBeenCalledWith("Проверка сопоставления сохранена");
    expect(screen.queryByText("Конфликты: accepted_matching_review")).not.toBeInTheDocument();
  });

  it("показывает буквенный код AED вместо числового кода валюты 784", async () => {
    const data = assistantData();
    data.orders[0].lines[0].price_change_pct = null;
    data.orders[0].lines[0].price_change_status = "currency_mismatch";
    data.orders[0].lines[0].price_history_expected_currency = "RUB";
    data.orders[0].lines[0].price_history_available_currencies = ["784", "USD"];
    vi.mocked(fetchProcurementOrderAssistant).mockResolvedValue(data);

    render(<ProcurementOrderAssistantView />);

    expect(await screen.findByText("Нет истории в RUB; есть AED, USD")).toBeInTheDocument();
    expect(screen.queryByText("Нет истории в RUB; есть 784, USD")).not.toBeInTheDocument();
  });

  it("показывает skeleton с признаком занятости во время загрузки", () => {
    vi.mocked(fetchProcurementOrderAssistant).mockReturnValue(new Promise(() => undefined));

    render(<ProcurementOrderAssistantView />);

    expect(screen.getByLabelText("Загрузка помощника заказов")).toHaveAttribute("aria-busy", "true");
  });

});
