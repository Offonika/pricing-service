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
} from "../api/procurementAssortment";
import { ProcurementOrderFormationApp } from "./ProcurementOrderFormationApp";

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

    expect(screen.getByText("Потребность исчезла в новом расчёте")).toBeInTheDocument();
    expect(screen.getByText(/Решение человека: 7.000 · новый расчёт: 9/)).toBeInTheDocument();
    expect(screen.getByText(/Цена человека: 90.0000 · новая цена: 110/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Сохранить" })).toBeDisabled();
  });
});
