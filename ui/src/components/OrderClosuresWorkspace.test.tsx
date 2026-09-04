import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "../api/orderClosures";
import { OrderClosuresWorkspace } from "./OrderClosuresWorkspace";

vi.mock("../api/orderClosures", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/orderClosures")>();
  return {
    ...actual,
    createExcelClosureBatch: vi.fn(),
    createFilterClosureBatch: vi.fn(),
    readClosureBatch: vi.fn(),
    readClosureReasons: vi.fn(),
    repeatClosureDiagnosis: vi.fn(),
    confirmClosureBatch: vi.fn(),
  };
});

vi.mock("react-hot-toast", () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

const draft: api.OrderClosureBatch = {
  id: "11111111-2222-3333-4444-555555555555",
  status: "draft",
  source_type: "excel",
  actor_id: "bitrix:m:42",
  actor_name: "Иван",
  confirmed_by: null,
  diagnosis_hash: null,
  command_kind: "diagnose",
  attempt_count: 0,
  last_error_code: null,
  last_polled_at: null,
  lease_until: null,
  applied_at: null,
  created_at: "2026-09-04T09:00:00Z",
  updated_at: "2026-09-04T09:00:00Z",
  items: [],
};

describe("OrderClosuresWorkspace", () => {
  afterEach(cleanup);

  beforeEach(() => {
    vi.mocked(api.createExcelClosureBatch).mockResolvedValue(draft);
  });

  it("starts with a read-only dry-run and never offers immediate close", async () => {
    render(<OrderClosuresWorkspace canConfirm userName="Иван" />);
    expect(screen.getByText(/Сначала 1С выполняет read-only проверку/)).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "223210\t2026" } });
    fireEvent.click(screen.getByRole("button", { name: "Выполнить dry-run" }));
    await waitFor(() => {
      expect(api.createExcelClosureBatch).toHaveBeenCalledWith("223210\t2026");
    });
    expect(screen.queryByRole("button", { name: /Подтвердить и отправить/ })).not.toBeInTheDocument();
  });

  it("exposes filter mode and keeps confirmation role visible", () => {
    render(<OrderClosuresWorkspace canConfirm={false} userName="Наблюдатель" />);
    expect(screen.getByText("Только просмотр")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Сформировать по фильтрам" }));
    expect(screen.getByLabelText("Год")).toBeInTheDocument();
    expect(screen.getByLabelText("Категория")).toBeInTheDocument();
  });
});
