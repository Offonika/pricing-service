import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import toast from "react-hot-toast";
import type {
  ProcurementOrderFormation,
  ProcurementOrderFormationLine,
} from "../api/procurementAssortment";
import {
  createProcurementClassification,
  fetchProcurementOrder,
  updateProcurementOrderLine,
} from "../api/procurementAssortment";
import { ProcurementOrderFormationApp } from "./ProcurementOrderFormationApp";
import { procurementBlockerSummaryLabel } from "../utils/procurementRiskLabels";

vi.mock("../api/procurementAssortment", () => ({
  approveProcurementClassification: vi.fn(),
  createProcurementClassification: vi.fn(),
  fetchProcurementOrder: vi.fn(),
  submitProcurementOrder: vi.fn(),
  updateProcurementOrderLine: vi.fn(),
}));

vi.mock("react-hot-toast", () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

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

async function proposeClassification() {
  fireEvent.click(screen.getByRole("button", { name: "Изменить классификацию" }));
  fireEvent.change(screen.getByPlaceholderText("Обязательная причина"), {
    target: { value: "старая модель, выводим карточку" },
  });
  fireEvent.click(screen.getByRole("button", { name: "На согласование" }));
}

async function proposePension() {
  fireEvent.click(screen.getByRole("button", { name: "Изменить классификацию" }));
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "pension" } });
  fireEvent.change(screen.getByPlaceholderText("Обязательная причина"), {
    target: { value: "ведём аналог дешевле" },
  });
}

const versionConflict = {
  response: { status: 409, data: { detail: "order version changed; refresh the order" } },
};

describe("ProcurementOrderFormationApp «Допродаём»", () => {
  beforeEach(() => {
    vi.mocked(createProcurementClassification).mockReset();
    vi.mocked(fetchProcurementOrder).mockReset();
    vi.mocked(toast.error).mockReset();
    vi.mocked(toast.success).mockReset();
  });

  afterEach(cleanup);

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
    vi.mocked(updateProcurementOrderLine).mockReset();
    vi.mocked(toast.success).mockReset();
    vi.mocked(toast.error).mockReset();
  });

  afterEach(cleanup);

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
    expect(screen.getByText("Округление не применено: нет подтверждённой закупочной цены.")).toBeInTheDocument();
    expect(screen.queryByText(/Сигнал:/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Открыть карточку" })).toHaveAttribute(
      "href",
      "https://master-mobile.ru/catalog/displei/40699/"
    );
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

    expect(screen.getByRole("combobox")).toHaveValue("replace_candidate");
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
    expect(screen.getByRole("button", { name: "Сохранить" })).toBeDisabled();
  });
});
