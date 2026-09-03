import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  fetchProcurementOrder,
  fetchProcurementProductCard,
  updateProcurementOrderLine,
  type ProcurementOrderFormation,
  type ProcurementProductCard,
} from "../api/procurementAssortment";
import { ProcurementProductInsights } from "./ProcurementProductInsights";

vi.mock("../api/procurementAssortment", () => ({
  fetchProcurementOrder: vi.fn(),
  fetchProcurementProductCard: vi.fn(),
  updateProcurementOrderLine: vi.fn(),
}));

function productCard(): ProcurementProductCard {
  return {
    identity: {
      bitrix_product_id: "1646",
      xml_id: "2685293e-967c-11e1-bdb9-0025901e48ef",
      nomenclature_code: "РБ000006737",
      name: "Дисплей Samsung A16",
      article: "A16-OLED",
      photo_url: "https://cdn.example.test/a16.webp",
      website_url: "https://shop.example.test/a16/",
      bitrix_url: "/crm/catalog/17/product/1646/",
    },
    properties: { assortment_status: "Продажа" },
    lifecycle: { status: "working", label: "Рабочий" },
    demand: {
      sales_30: "18",
      sales_90: "45",
      sales_180: "72",
      rate_30: "0.6",
      rate_90: "0.5",
      rate_180: "0.4",
      sellable_stock: "4",
      customer_orders: "2",
      incoming: "3",
      target_stock: "9",
      recommended_order: "7",
      current_order: "5",
    },
    quality: {
      return_qty_180: "8",
      batch_return_qty_90: "5",
      defect_pct: "3.5",
      confidence: "medium",
    },
    supply: {
      supplier_name: "Поставщик тест",
      purchase_price: "115",
      currency: "RUB",
      profitability_pct: "21.4",
      lead_time_days: 26,
    },
    family: { label: "Samsung A16", member_count: 4 },
    blockers: [{
      code: "batch_error_suspected",
      scope: "line",
      severity: "hard",
      line_id: 10,
      line_number: 2,
      message: "Проверить причины возвратов",
      evidence: { return_qty: 5, share_pct: 41.7 },
      resolution_actions: [{ label: "Проверить документы", kind: "manual" }],
    }],
    orders: [{
      order_id: 14,
      label: "Заказ 000014",
      status: "review",
      onec_status: "created",
      onec_document_number: "000014",
      bitrix_process_url: "/crm/type/1200/details/7001/",
      app_url: "/bitrix/procurement-order-formation/orders/14",
    }],
    recommendation: "Проверить возвраты до заказа",
    source: { state: "ready", calculated_at: "2026-09-02" },
  };
}

function order(version = 3, lineVersion = 2): ProcurementOrderFormation {
  return {
    id: 14,
    version,
    status: "review",
    supplier_name: "GOOYEE ANDROID LCDs",
    lines: [{
      id: 10,
      line_number: 2,
      version: lineVersion,
      bitrix_product_id: "1646",
      bitrix_product_xml_id: "2685293e-967c-11e1-bdb9-0025901e48ef",
      nomenclature_ref: "ref-1",
      nomenclature_name: "Дисплей Samsung A16",
      recommended_quantity: "7",
      final_quantity: "5",
      purchase_price: "115",
      amount: "575",
      currency: "RUB",
      source_kind: "automatic",
      explicit_demand: false,
      risk_codes: [],
      blockers: [],
      removed: false,
    }],
  } as unknown as ProcurementOrderFormation;
}

describe("ProcurementProductInsights", () => {
  beforeEach(() => {
    window.__MM_BITRIX_LAUNCH__ = { domain: "crm.example.test" };
    vi.mocked(fetchProcurementProductCard).mockReset();
    vi.mocked(fetchProcurementOrder).mockReset();
    vi.mocked(updateProcurementOrderLine).mockReset();
  });

  afterEach(cleanup);

  it("показывает операционную карточку без заказного блока при обычном входе", async () => {
    vi.mocked(fetchProcurementProductCard).mockResolvedValue(productCard());

    render(<ProcurementProductInsights productId="1646" />);

    expect(await screen.findByRole("heading", { name: "Дисплей Samsung A16" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "В этом заказе" })).not.toBeInTheDocument();
    expect(fetchProcurementOrder).not.toHaveBeenCalled();
    expect(screen.getByRole("img", { name: "Дисплей Samsung A16" })).toBeInTheDocument();
    expect(screen.getByText("РБ000006737", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("7 шт.")).toBeInTheDocument();
    expect(screen.getByText("Проверить причины возвратов")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Проверить документы" })).toHaveAttribute(
      "href",
      "/bitrix/procurement-order-formation/orders/14?line=10"
    );
    expect(screen.getByRole("link", { name: /Открыть карточку Bitrix24/ })).toHaveAttribute(
      "href",
      "https://crm.example.test/crm/catalog/17/product/1646/"
    );
    await waitFor(() => expect(document.title).toBe("Дисплей Samsung A16 — показатели товара"));
  });

  it("загружает контекст строки, редактирует её и остаётся в карточке после сохранения", async () => {
    vi.mocked(fetchProcurementProductCard).mockResolvedValue(productCard());
    vi.mocked(fetchProcurementOrder).mockResolvedValue(order());
    vi.mocked(updateProcurementOrderLine).mockResolvedValue(order(4, 3));

    render(<ProcurementProductInsights productId="1646" orderId={14} lineId={10} />);

    expect(await screen.findByRole("heading", { name: "В этом заказе" })).toBeInTheDocument();
    expect(screen.getByText("Заказ №14 · строка 2")).toBeInTheDocument();
    const quantityInput = screen.getByRole("spinbutton", { name: "Количество в заказе" });
    const priceInput = screen.getByRole("spinbutton", { name: "Цена закупки" });
    fireEvent.change(quantityInput, { target: { value: "8" } });
    fireEvent.change(priceInput, { target: { value: "120" } });
    fireEvent.click(screen.getByRole("button", { name: "Подтвердить строку" }));

    await waitFor(() => expect(updateProcurementOrderLine).toHaveBeenCalledWith(14, 10, {
      expected_order_version: 3,
      expected_line_version: 2,
      final_quantity: "8",
      purchase_price: "120",
    }));
    expect(await screen.findByText("Строка подтверждена и сохранена")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Дисплей Samsung A16" })).toBeInTheDocument();
  });

  it("после конфликта версии обновляет заказ и повторяет сохранение неизменённой строки", async () => {
    vi.mocked(fetchProcurementProductCard).mockResolvedValue(productCard());
    vi.mocked(fetchProcurementOrder)
      .mockResolvedValueOnce(order())
      .mockResolvedValueOnce(order(4, 2));
    vi.mocked(updateProcurementOrderLine)
      .mockRejectedValueOnce({ response: { status: 409 } })
      .mockResolvedValueOnce(order(5, 3));

    render(<ProcurementProductInsights productId="1646" orderId={14} lineId={10} />);
    const confirmButton = await screen.findByRole("button", { name: "Подтвердить строку" });
    await waitFor(() => expect(confirmButton).toBeEnabled());
    fireEvent.click(confirmButton);

    await waitFor(() => expect(updateProcurementOrderLine).toHaveBeenCalledTimes(2));
    expect(updateProcurementOrderLine).toHaveBeenLastCalledWith(14, 10, expect.objectContaining({
      expected_order_version: 4,
      expected_line_version: 2,
    }));
    expect(await screen.findByText("Строка подтверждена и сохранена")).toBeInTheDocument();
  });

  it("показывает понятную ошибку, если строка изменилась при конфликте версии", async () => {
    vi.mocked(fetchProcurementProductCard).mockResolvedValue(productCard());
    vi.mocked(fetchProcurementOrder)
      .mockResolvedValueOnce(order())
      .mockResolvedValueOnce(order(4, 3));
    vi.mocked(updateProcurementOrderLine).mockRejectedValue({ response: { status: 409 } });

    render(<ProcurementProductInsights productId="1646" orderId={14} lineId={10} />);
    const confirmButton = await screen.findByRole("button", { name: "Подтвердить строку" });
    await waitFor(() => expect(confirmButton).toBeEnabled());
    fireEvent.click(confirmButton);

    expect(await screen.findByText(/Строку уже изменили в другом окне/)).toBeInTheDocument();
    expect(updateProcurementOrderLine).toHaveBeenCalledTimes(1);
  });

  it("повторяет запрос карточки после ошибки", async () => {
    vi.mocked(fetchProcurementProductCard)
      .mockRejectedValueOnce(new Error("Сервис временно недоступен"))
      .mockResolvedValueOnce(productCard());

    render(<ProcurementProductInsights productId="1646" />);
    fireEvent.click(await screen.findByRole("button", { name: "Повторить" }));

    await waitFor(() => expect(fetchProcurementProductCard).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("heading", { name: "Дисплей Samsung A16" })).toBeInTheDocument();
  });
});
