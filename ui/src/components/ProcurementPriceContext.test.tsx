import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, expect, it } from "vitest";
import type { ProcurementPriceContext as PriceContext, ProcurementPriceFact } from "../api/procurementAssortment";
import { ProcurementPriceContext } from "./ProcurementPriceContext";

afterEach(cleanup);
beforeAll(() => {
  // jsdom has no native dialog implementation; browser tests verify focus, Escape and layout.
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); this.dispatchEvent(new Event("close")); };
});
const fact = (value: string | null, currency = "RUB"): ProcurementPriceFact => ({ value, currency, status: value ? "reference" : "unconfirmed", documents: [], unit_name: "шт" });
const context = (): PriceContext => ({ schema_version: 1, agreed_purchase: fact(null, "CNY"), purchase_rub: fact(null),
  reference_cost_rub: { ...fact("2108.65"), documents: [{ kind: "УстановкаЦенНоменклатуры", ref: "cost-doc", number: "РБ000001527", at: "2026-08-21T11:39:06" }] },
  receipt_purchases_rub: [], actual_cost_status: "not_formed", actual_costs_rub: [], supplier_quotes: [fact("160", "CNY")],
  source_status: "ready", checked_on: "2026-09-06", last_success_on: "2026-09-06", stale: false });

it("shows a reference cost with evidence without treating a quote as the agreed price", () => {
  render(<ProcurementPriceContext context={context()} productName="Дисплей" />);
  fireEvent.click(screen.getByLabelText("Цена, курс и себестоимость: Дисплей"));
  expect(screen.getAllByText("Цена не согласована")).toHaveLength(2);
  expect(screen.getByText("Себестоимость в рублях · справочно")).toBeVisible();
  expect(screen.getByText(/РБ000001527/)).toBeVisible();
  expect(screen.getByText("160 CNY")).toBeVisible();
  expect(screen.getByText(/Пока не подтверждена связанными документами/)).toBeVisible();
});

it("shows document rate and preserves the last confirmed state on a source failure", () => {
  const data = context();
  data.stale = true;
  data.source_status = "unavailable";
  data.agreed_purchase = { ...fact("160", "CNY"), status: "confirmed" };
  data.purchase_rub = { ...fact("2056"), status: "confirmed", exchange_rate: "12.85", exchange_multiplicity: "1", exchange_rate_at: "2026-08-21T11:03:13" };
  render(<ProcurementPriceContext context={data} productName="Дисплей" />);
  fireEvent.click(screen.getByLabelText("Цена, курс и себестоимость: Дисплей"));
  expect(screen.getByRole("status")).toHaveTextContent(/последние подтверждённые данные от 06.09.2026/);
  expect(screen.getByText(/Курс 12,85 · кратность 1/)).toBeVisible();
  expect(screen.queryByText("Цена не согласована")).not.toBeInTheDocument();
});

it("lists partial receipts separately without an invented average", () => {
  const data = context();
  data.purchase_rub = { ...fact(null), status: "ambiguous", reason: "see_individual_receipt_prices" };
  data.receipt_purchases_rub = [fact("2056"), fact("2080")];
  data.actual_costs_rub = [fact("2108.65")];
  data.actual_cost_status = "partial";
  render(<ProcurementPriceContext context={data} productName="Дисплей" />);
  fireEvent.click(screen.getByLabelText("Цена, курс и себестоимость: Дисплей"));
  expect(screen.getByText(/Подтверждена для части поступлений/)).toBeVisible();
  expect(screen.getByText("Закупочная стоимость по поступлениям · в рублях")).toBeVisible();
  expect(screen.getByText("См. стоимость по отдельным поступлениям")).toBeVisible();
});
