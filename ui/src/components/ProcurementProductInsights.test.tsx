import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  fetchProcurementProductCard,
  type ProcurementProductCard,
} from "../api/procurementAssortment";
import { ProcurementProductInsights } from "./ProcurementProductInsights";

vi.mock("../api/procurementAssortment", () => ({
  fetchProcurementProductCard: vi.fn(),
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
    properties: {
      assortment_status: "Продажа",
      quality: "Оригинал",
      procurement_profile: "Обычный",
      manual_minimum: "2",
      subject: "Дисплей",
      category: "Samsung",
      brand: "Samsung",
      model: "A16",
      characteristics: { Матрица: "OLED", Цвет: "Чёрный" },
    },
    lifecycle: { status: "working", label: "Рабочий" },
    demand: {
      sales_30: null,
      sales_90: "45",
      sales_180: "72",
      rate_30: null,
      rate_90: "0.5",
      rate_180: "0.4",
      sellable_stock: "4",
      customer_orders: "2",
      incoming: "3",
      target_stock: "9",
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
    blockers: [
      {
        code: "batch_error_suspected",
        scope: "line",
        severity: "hard",
        line_id: 10,
        line_number: 1,
        message: "Проверить причины возвратов",
        evidence: { return_qty: 5 },
        resolution_actions: [
          { label: "Проверить документы", kind: "manual" },
        ],
      },
    ],
    orders: [
      {
        order_id: 14,
        label: "Заказ 000014",
        status: "transmitted",
        onec_status: "created",
        onec_document_number: "000014",
        bitrix_process_url: "/crm/type/1200/details/7001/",
        app_url: "/bitrix/procurement-order-formation/orders/14",
      },
    ],
    recommendation: "Проверить возвраты до заказа",
    source: { state: "ready", calculated_at: "2026-09-02" },
  };
}

describe("ProcurementProductInsights", () => {
  beforeEach(() => {
    window.__MM_BITRIX_LAUNCH__ = { domain: "crm.example.test" };
    vi.mocked(fetchProcurementProductCard).mockReset();
  });

  afterEach(cleanup);

  it("показывает блокеры, характеристики и не подменяет пропуски нулём", async () => {
    vi.mocked(fetchProcurementProductCard).mockResolvedValue(productCard());

    render(<ProcurementProductInsights productId="1646" />);

    expect(await screen.findByRole("heading", { name: "Дисплей Samsung A16" }))
      .toBeInTheDocument();
    expect(fetchProcurementProductCard).toHaveBeenCalledWith("1646");
    expect(screen.getByText("1 блокер(а)")).toBeInTheDocument();
    expect(screen.getByText("Проверить причины возвратов")).toBeInTheDocument();
    expect(screen.getByText("OLED")).toBeInTheDocument();
    expect(screen.getAllByText("нет данных").length).toBeGreaterThan(0);
    expect(screen.queryByText("0 шт./день")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Бизнес-процесс" })).toHaveAttribute(
      "href",
      "https://crm.example.test/crm/type/1200/details/7001/"
    );
  });

  it("повторяет запрос после ошибки", async () => {
    vi.mocked(fetchProcurementProductCard)
      .mockRejectedValueOnce(new Error("Сервис временно недоступен"))
      .mockResolvedValueOnce(productCard());

    render(<ProcurementProductInsights productId="1646" />);

    fireEvent.click(await screen.findByRole("button", { name: "Повторить" }));
    await waitFor(() => expect(fetchProcurementProductCard).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("heading", { name: "Дисплей Samsung A16" }))
      .toBeInTheDocument();
  });
});
