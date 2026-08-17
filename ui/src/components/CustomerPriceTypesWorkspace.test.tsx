import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import toast from "react-hot-toast";

import * as customerPriceTypes from "../api/customerPriceTypes";
import { CustomerPriceTypesWorkspace } from "./CustomerPriceTypesWorkspace";

vi.mock("../api/customerPriceTypes", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/customerPriceTypes")>();
  return {
    ...actual,
    fetchCptCases: vi.fn(),
    fetchCptDataIssues: vi.fn(),
    fetchCptPortfolio: vi.fn(),
    fetchCptQualityMetrics: vi.fn(),
    fetchCptQualitySamples: vi.fn(),
    fetchCptSummary: vi.fn(),
    fetchCptWorklists: vi.fn(),
    reviewCptQualitySample: vi.fn(),
    searchCptProfiles: vi.fn(),
  };
});

vi.mock("react-hot-toast", () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

const envelope = {
  run_id: 1,
  snapshot_month: "2026-06",
  ruleset_version: "test",
  source_status: "completed",
};

const sample: customerPriceTypes.CptQualitySample = {
  id: 10,
  run_id: 1,
  snapshot_id: 20,
  counterparty_ref: "0x01",
  counterparty_code: "РБ000001",
  counterparty_name: "Клиент Тест",
  current_price_type: "2.Бронзовый",
  recommended_price_type: "Розница",
  system_recommendation: "isolate",
  recommendation_reason: "Нужна проверка результата.",
  stop_factors: [],
  system_group: "isolate",
  correct_group: null,
  review_result: null,
  status: "pending",
  selected_by: "system",
  selected_at: "2026-08-12T10:00:00",
  reviewed_by: null,
  reviewed_at: null,
  comment: null,
  version: 1,
};

function renderWorkspace() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <CustomerPriceTypesWorkspace
        bitrixUserName="Арсен"
        role="network_head"
        canViewMoney
      />
    </QueryClientProvider>,
  );
}

describe("CustomerPriceTypesWorkspace", () => {
  beforeEach(() => {
    vi.mocked(customerPriceTypes.fetchCptSummary).mockResolvedValue({
      ...envelope,
      summary: {
        profile_count: 1,
        actionable_count: 1,
        levels: { bronze: 1 },
        recommendations: { isolate: 1 },
        source_statuses: { ready: 1 },
        review_types: { retention: 1 },
        departments: { test: 1 },
      },
    });
    vi.mocked(customerPriceTypes.fetchCptWorklists).mockResolvedValue({
      ...envelope,
      worklists: { isolate: 1 },
    });
    vi.mocked(customerPriceTypes.fetchCptPortfolio).mockResolvedValue({
      ...envelope,
      batch_key: "test",
      batch_label: "test",
      expected_counts: {},
      counts: {},
      review_status_counts: {},
      mismatch_count: 0,
      total: 0,
      limit: 100,
      offset: 0,
      payload: [],
    });
    vi.mocked(customerPriceTypes.fetchCptCases).mockResolvedValue({
      ...envelope,
      total: 0,
      limit: 50,
      offset: 0,
      payload: [],
    });
    vi.mocked(customerPriceTypes.fetchCptQualityMetrics).mockResolvedValue({
      ...envelope,
      metrics_scope: "portfolio",
      metrics_ready: false,
      population_count: 1,
      selected_count: 1,
      reviewed_count: 0,
      coverage: 0,
      override_rate: 0,
      critical_false_downgrade_count: 0,
      groups: {},
      matrix: {},
    });
    vi.mocked(customerPriceTypes.fetchCptQualitySamples).mockResolvedValue({
      ...envelope,
      total: 1,
      limit: 500,
      offset: 0,
      payload: [sample],
    });
    vi.mocked(customerPriceTypes.searchCptProfiles).mockResolvedValue({
      ...envelope,
      total: 1,
      limit: 50,
      offset: 0,
      payload: [
        {
          counterparty_ref: "0x02",
          counterparty_code: "РБ000002",
          counterparty_name: "Проблемный клиент",
          current_price_type: "2.Бронзовый",
          recommended_price_type: null,
          result_state: "data_issue",
          result_label: "Данные проверяет техническая команда",
          can_review: false,
          quality_sample_id: null,
          quality_sample_status: null,
        },
      ],
    });
    vi.mocked(toast.success).mockReset();
    vi.mocked(toast.error).mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("показывает Арсену понятный поиск и действия оценки", async () => {
    renderWorkspace();
    fireEvent.click(await screen.findByRole("button", { name: "Экспертная оценка →" }));

    expect(await screen.findByText(/не утверждает изменение типа цены/i)).toBeVisible();
    expect(screen.queryByText("Проверенный пакет 82")).not.toBeInTheDocument();
    const search = screen.getByRole("searchbox", { name: "Поиск клиента по всему портфелю" });
    fireEvent.change(search, { target: { value: "РБ000002" } });
    expect(await screen.findByText("Данные проверяет техническая команда")).toBeVisible();
    expect(screen.getByText("Только просмотр")).toBeVisible();

    expect(screen.getByRole("button", { name: "Результат верный" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Ошибка в данных" }));
    const submit = screen.getByRole("button", { name: "Передать технической команде" });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByPlaceholderText("Какие данные выглядят неверно"), {
      target: { value: "Не сходится сумма за июнь" },
    });
    expect(submit).toBeEnabled();
  });

  it("показывает русскую ошибку глобального поиска", async () => {
    vi.mocked(customerPriceTypes.searchCptProfiles).mockRejectedValueOnce(new Error("failed"));
    renderWorkspace();
    fireEvent.click(await screen.findByRole("button", { name: "Экспертная оценка →" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Поиск клиента по всему портфелю" }), {
      target: { value: "Ошибка" },
    });
    await waitFor(() => expect(screen.getByText("Не удалось выполнить поиск.")).toBeVisible());
  });
});
