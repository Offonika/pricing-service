import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import toast from "react-hot-toast";
import type { ProcurementOrderAssistant } from "../api/procurementAssortment";
import {
  approveProcurementClassification,
  assembleProcurementOrderProjects,
  fetchProcurementOrderAssistant,
  rejectProcurementClassification,
  updateProcurementSupplierProfile,
} from "../api/procurementAssortment";
import { ProcurementOrderAssistant as ProcurementOrderAssistantView } from "./ProcurementOrderAssistant";

vi.mock("../api/procurementAssortment", () => ({
  assembleProcurementOrderProjects: vi.fn(),
  approveProcurementClassification: vi.fn(),
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
    vi.mocked(fetchProcurementOrderAssistant).mockReset();
    vi.mocked(assembleProcurementOrderProjects).mockReset();
    vi.mocked(approveProcurementClassification).mockReset();
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
    expect(screen.getByText("Класс A")).toBeInTheDocument();
    expect(screen.getByText("Лучшие условия")).toBeInTheDocument();
    expect(screen.getByText("30/70")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Открыть исходное фото/ })).toHaveAttribute(
      "href",
      "https://cdn.example.test/original/display.jpg"
    );
    expect(screen.getByRole("link", { name: "Карточка товара" })).toHaveAttribute(
      "href",
      "https://master-mobile.ru/catalog/displei/40699/"
    );
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
    expect(screen.getByText("Недоступно: не найдена точная карточка товара")).toBeInTheDocument();
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

  it("показывает полное предложение и требует причину для inline-отклонения", async () => {
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
    expect(await screen.findByText("working → Матричный")).toBeInTheDocument();
    expect(screen.getByText("Автор: Автор предложения")).toBeInTheDocument();
    expect(screen.getByText("Товар нужен в постоянной матрице")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Принять" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Отклонить" }));
    const confirm = screen.getByRole("button", { name: "Подтвердить отклонение" });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Причина отклонения"), {
      target: { value: "Недостаточно подтверждённых продаж" },
    });
    fireEvent.click(confirm);

    await waitFor(() => expect(rejectProcurementClassification).toHaveBeenCalledWith(
      order.id,
      line.id,
      77,
      {
        expected_order_version: order.version,
        expected_line_version: line.version,
        reason: "Недостаточно подтверждённых продаж",
      }
    ));
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
    fireEvent.click(await screen.findByRole("button", { name: "Изменить профиль" }));
    fireEvent.change(screen.getByLabelText("Класс"), { target: { value: "B" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить профиль" }));

    await waitFor(() => expect(updateProcurementSupplierProfile).toHaveBeenCalledWith(
      "supplier-ref",
      expect.objectContaining({ expected_version: 2, qualification_class: "B" })
    ));
  });
});
