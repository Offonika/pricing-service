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
    fetchCptReviewCards: vi.fn(),
    fetchCptReviewMetrics: vi.fn(),
    fetchCptSummary: vi.fn(),
    fetchCptWorklists: vi.fn(),
    reviewCptQualitySample: vi.fn(),
    saveCptReview: vi.fn(),
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

const reviewCard: customerPriceTypes.CptReviewCard = {
  snapshot_id: 20,
  case_id: 30,
  counterparty_ref: "0x01",
  counterparty_code: "РБ000001",
  counterparty_name: "Клиент Тест",
  owner_name: "Менеджер",
  department_name: "Розничная сеть",
  snapshot_month: "2026-06-01",
  current_price_type: "2.Бронзовый",
  recommended_price_type: "Розница",
  recommendation_text: "После завершённого изолятора готово изменение типа.",
  data_state: "ready",
  data_state_label: "Данные готовы",
  snapshot_hash: "a".repeat(64),
  contracts: [
    {
      contract_ref: "0x10",
      contract_name: "Основной рабочий договор",
      price_type_name: "2.Бронзовый",
      is_working: true,
    },
  ],
  price_type: {
    kind: "price_type",
    system_value: "Розница",
    system_label: "Изменить на Розница",
    can_review: true,
    unavailable_reason: null,
    allowed_results: ["confirm", "correct", "data_issue"],
    allowed_corrected_values: ["Розница", "2.Бронзовый", "3.Серебряный"],
    review_id: null,
    result: null,
    final_value: null,
    comment: null,
    reviewed_by: null,
    reviewed_at: null,
    version: 0,
    decision_mode: null,
    external_state: "not_created",
    external_message: null,
  },
  client_action: {
    kind: "client_action",
    system_value: "quality",
    system_label: "Проверка качества",
    can_review: true,
    unavailable_reason: null,
    allowed_results: ["confirm", "correct", "no_action", "data_issue"],
    allowed_corrected_values: ["retention", "isolate", "recovery", "quality"],
    review_id: null,
    result: null,
    final_value: null,
    comment: null,
    reviewed_by: null,
    reviewed_at: null,
    version: 0,
    decision_mode: null,
    external_state: "not_created",
    external_message: null,
  },
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
    vi.mocked(customerPriceTypes.fetchCptReviewCards).mockResolvedValue({
      ...envelope,
      total: 1,
      limit: 200,
      offset: 0,
      payload: [reviewCard],
    });
    vi.mocked(customerPriceTypes.fetchCptReviewMetrics).mockResolvedValue({
      ...envelope,
      price_type: { reviewed_count: 0, confirmed_count: 0, corrected_count: 0, no_action_count: 0, data_issue_count: 0, correction_rate: 0 },
      client_action: { reviewed_count: 0, confirmed_count: 0, corrected_count: 0, no_action_count: 0, data_issue_count: 0, correction_rate: 0 },
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

  it("показывает Арсену две независимые проверки", async () => {
    renderWorkspace();
    fireEvent.click(await screen.findByRole("button", { name: "Проверка решений →" }));

    expect((await screen.findAllByText("Изменить на Розница"))[0]).toBeVisible();
    expect(screen.queryByText("Проверенный пакет 82")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Подтвердить и запустить изменение" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Подтвердить действие" })).toBeEnabled();
    const dataIssueButtons = screen.getAllByRole("button", { name: "Ошибка в данных" });
    fireEvent.click(dataIssueButtons[0]);
    const submit = screen.getAllByRole("button", { name: "Сохранить решение" })[0];
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "Не сходится сумма за июнь" },
    });
    expect(submit).toBeEnabled();
  });

  it("показывает русскую ошибку поиска карточек", async () => {
    vi.mocked(customerPriceTypes.fetchCptReviewCards).mockRejectedValue(new Error("failed"));
    renderWorkspace();
    fireEvent.click(await screen.findByRole("button", { name: "Проверка решений →" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Поиск клиента в проверках" }), {
      target: { value: "Ошибка" },
    });
    await waitFor(() => expect(screen.getByText("Не удалось загрузить карточки.")).toBeVisible());
  });
});
