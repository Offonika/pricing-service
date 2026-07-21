import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { CustomerPriceTypeEvidence } from "./CustomerPriceTypeEvidence";

afterEach(cleanup);

describe("читаемая расшифровка исходных данных типа цены", () => {
  it("показывает экономику русскими подписями вместо технического JSON", () => {
    render(
      <CustomerPriceTypeEvidence
        kind="economics"
        title="Экономика"
        value={{
          revenue_30: "0.00",
          revenue_60: "0.00",
          revenue_90: "50.00",
          cost_of_sales_90: "17.65",
          gross_profit_90: "32.35",
          gross_margin_pct_90: "64.70",
          profitability_pct_90: "183.29",
          source_status: "ready",
          source_note: "1С read-only: выручка из _AccumRg7550, себестоимость из _AccumRg7580.",
        }}
      />,
    );

    fireEvent.click(screen.getByText("Экономика"));
    const table = screen.getByRole("table");
    expect(within(table).getByText("Выручка")).toBeVisible();
    expect(within(table).getByText("50 ₽")).toBeVisible();
    expect(screen.getByText("Валовая маржа за 90 дней")).toBeVisible();
    expect(screen.getByText("64,7%")).toBeVisible();
    expect(screen.getByText(/Данные только для чтения из 1С/)).toBeVisible();
    expect(screen.queryByText(/revenue_90|AccumRg7550/)).toBeNull();
  });

  it("расшифровывает возвраты и отсутствие дополнительной проверки", () => {
    render(
      <CustomerPriceTypeEvidence
        kind="returns"
        title="Возвраты"
        value={{
          source_status: "ready",
          defect_return_amount_90: "0.00",
          return_rate_pct: "0.00",
          review_type: null,
        }}
      />,
    );

    fireEvent.click(screen.getByText("Возвраты"));
    expect(screen.getByText("Возвраты по браку за 90 дней")).toBeVisible();
    expect(screen.getByText("0 ₽")).toBeVisible();
    expect(screen.getByText("0%")).toBeVisible();
    expect(screen.getByText("Не требуется")).toBeVisible();
  });

  it("расшифровывает форму оплаты", () => {
    render(
      <CustomerPriceTypeEvidence
        kind="payments"
        title="Оплаты"
        value={{ payment_form_primary: "mixed", cash_share_90: "45.5", bank_share_90: "54.5", source_status: "ready" }}
      />,
    );

    fireEvent.click(screen.getByText("Оплаты"));
    expect(screen.getByText("Смешанная")).toBeVisible();
    expect(screen.getByText("45,5%")).toBeVisible();
    expect(screen.getByText("54,5%")).toBeVisible();
    expect(screen.getByText("Готово")).toBeVisible();
  });
});
