import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { ProcurementExceptions } from "./ProcurementExceptions";

vi.mock("../api/client", () => ({ api: { get: vi.fn(), post: vi.fn() } }));
vi.mock("../api/procurementAssortment", () => ({ fetchProcurementOrder: vi.fn() }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

it("shows an addressed exception and opens its order", async () => {
  vi.mocked(api.get).mockResolvedValue({ data: { total: 1, overdue_count: 1, items: [{
    id: 1, order_id: 450, line_id: null, title: "Сверить исполнение — РБГУ0000560",
    reason_code: "receipt_reconciliation", status: "new", version: 1,
    facts_hash: "hash", overdue: true, response_due_at: "2026-09-07T15:00:00Z",
    first_seen_at: "2026-09-04T09:00:00Z", next_action: null, facts: {},
  }] } });
  const open = vi.fn();
  render(<ProcurementExceptions onOpenOrder={open} />);
  expect(await screen.findByText("Сверить исполнение — РБГУ0000560")).toBeVisible();
  expect(screen.getByText("Новое · Просрочено")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Открыть заказ" }));
  expect(open).toHaveBeenCalledWith(450);
});
