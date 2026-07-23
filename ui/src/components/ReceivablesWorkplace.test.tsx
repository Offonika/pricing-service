import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReceivableWorkplaceItem, ReceivableWorkplaceResponse } from "../api/receivables";
import {
  fetchCounterpartyFolderRecommendations,
  fetchReceivableWorkplace,
  fetchReceivableWorkplaceMeta,
} from "../api/receivables";

vi.mock("../api/receivables", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/receivables")>();
  return {
    ...actual,
    fetchCounterpartyFolderRecommendations: vi.fn(),
    fetchReceivableWorkplace: vi.fn(),
    fetchReceivableWorkplaceMeta: vi.fn(),
    updateReceivableWorkplaceItem: vi.fn(),
  };
});

import { ReceivablesWorkplace } from "./ReceivablesWorkplace";

const item: ReceivableWorkplaceItem = {
  snapshot_date: "2026-07-23",
  stable_key: "receivable:test-client",
  counterparty_ref: "test-client",
  counterparty_code: "РБ000001",
  counterparty_name: "Клиент Тест",
  department_ref: "department-1",
  department_name: "Горбушка",
  responsible_name: "Ответственный Тест",
  phone: "+70000000000",
  phone_status: "present",
  current_balance: "10000.00",
  overdue_amount: "10000.00",
  effective_overdue_days: 10,
  invoice_count: 1,
  overdue_invoice_count: 1,
  status: "waiting_payment",
  payment_postponed: false,
  payment_postponed_count: 0,
  comment: "Комментарий тест",
  needs_call_today: false,
  no_phone_marker: false,
  needs_credit_depth_default: false,
  criticality: "normal",
  documents: [],
  staff_options: [],
};

const response: ReceivableWorkplaceResponse = {
  as_of: "2026-07-23",
  freshness_status: "fresh",
  source_status: "cache_ready",
  summary: {
    row_count: 1,
    total_receivable: "10000.00",
    total_overdue: "10000.00",
    overdue_over_30_amount: "0",
    overdue_over_90_amount: "0",
    need_call_today_amount: "0",
    no_phone_count: 0,
    credit_depth_default_count: 0,
  },
  total_count: 1,
  visible_count: 1,
  summary_scope: "filtered_total",
  department_options: [{ department_ref: "department-1", department_name: "Горбушка" }],
  cache_status: {},
  status_options: [{ value: "waiting_payment", label: "Ждем оплату", scope: "common" }],
  payload: [item],
};

describe("ReceivablesWorkplace", () => {
  beforeEach(() => {
    vi.mocked(fetchReceivableWorkplaceMeta).mockResolvedValue({
      latest_snapshot_date: "2026-07-23",
      department_options: response.department_options,
      cache_status: {},
    });
    vi.mocked(fetchReceivableWorkplace).mockResolvedValue(response);
    vi.mocked(fetchCounterpartyFolderRecommendations).mockResolvedValue({
      as_of: "2026-07-23",
      freshness_status: "fresh",
      source_status: "cache_ready",
      report_revision: "test",
      summary: {},
      payload: [],
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("keeps one save action inside the expanded comment editor", async () => {
    render(<ReceivablesWorkplace bitrixMode />);

    expect(await screen.findByText("Клиент Тест")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Сохранить" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Комментарий тест" }));

    expect(screen.getByRole("button", { name: "Сохранить комментарий" })).toBeVisible();
  });
});
