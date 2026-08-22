import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReceivableWorkplaceItem, ReceivableWorkplaceResponse } from "../api/receivables";
import {
  deleteReceivableSupervisorNote,
  fetchCounterpartyFolderRecommendations,
  fetchReceivablePkoShadow,
  fetchReceivableWorkplace,
  fetchReceivableWorkplaceMeta,
  updateReceivableWorkplaceItem,
  upsertReceivableSupervisorNote,
} from "../api/receivables";

vi.mock("../api/receivables", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/receivables")>();
  return {
    ...actual,
    fetchCounterpartyFolderRecommendations: vi.fn(),
    fetchReceivablePkoShadow: vi.fn(),
    fetchReceivableWorkplace: vi.fn(),
    fetchReceivableWorkplaceMeta: vi.fn(),
    updateReceivableWorkplaceItem: vi.fn(),
    upsertReceivableSupervisorNote: vi.fn(),
    deleteReceivableSupervisorNote: vi.fn(),
  };
});

import { receivableStatusTone } from "../receivableStatusTone";
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
  supervisor_notes: [],
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
    vi.mocked(fetchReceivablePkoShadow).mockResolvedValue({
      as_of: "2026-07-23",
      algorithm_version: "pko-shadow-v1",
      run_id: "run-1",
      computed_at: "2026-07-23T12:00:00",
      summary: {
        row_count: 1,
        matched_count: 1,
        data_quality_count: 0,
        no_candidate_count: 0,
      },
      payload: [
        {
          snapshot_date: "2026-07-23",
          algorithm_version: "pko-shadow-v1",
          run_id: "run-1",
          counterparty_ref: "test-client",
          counterparty_code: "РБ025702",
          counterparty_name: "Контрольный клиент ПКО",
          current_balance: "500.00",
          base_payment_ref: "pko-1",
          base_payment_number: "ПКО-1",
          base_payment_date: "2026-07-20T10:00:00",
          base_balance_after: "500.00",
          current_origin_document_ref: "sale-old",
          current_origin_document_number: "РТУ-OLD",
          current_origin_document_date: "2025-10-01T10:00:00",
          candidate_origin_document_ref: "sale-new",
          candidate_origin_document_number: "РТУ-NEW",
          candidate_origin_document_date: "2025-11-01T10:00:00",
          candidate_responsible_ref: "manager-1",
          candidate_responsible_name: "Мария",
          candidate_origin_open_amount: "500.00",
          selected_open_amount: "500.00",
          delta: "0.00",
          status: "matched",
          reason: "pko_cycle_matched",
          current_documents: [],
          candidate_documents: [],
          diagnostics: {},
          computed_at: "2026-07-23T12:00:00",
        },
      ],
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

  it("shows the read-only PKO shadow comparison only to full-access Bitrix users", async () => {
    const { unmount } = render(
      <ReceivablesWorkplace bitrixMode accessLevel="full" />
    );

    fireEvent.click(await screen.findByRole("button", { name: "Тест ПКО" }));
    expect(
      await screen.findByRole("region", { name: "Теневое сравнение ПКО-алгоритма" })
    ).toHaveAttribute("tabindex", "0");
    expect(screen.getByText("Контрольный клиент ПКО")).toBeVisible();
    expect(screen.getByText("РТУ-OLD")).toBeVisible();
    expect(screen.getByText("РТУ-NEW")).toBeVisible();
    expect(screen.getByText("Цикл от ПКО полностью сошёлся")).toBeVisible();
    expect(
      screen.getByText(/статусы, комментарии, карточки Bitrix24 и данные 1С не изменяются/i)
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: /сохранить/i })).not.toBeInTheDocument();
    expect(fetchReceivablePkoShadow).toHaveBeenCalledWith("2026-07-23");

    unmount();
    render(<ReceivablesWorkplace bitrixMode accessLevel="department" />);
    expect(await screen.findByText("Клиент Тест")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Тест ПКО" })).not.toBeInTheDocument();
  });

  it("exposes the workplace table as a keyboard-scrollable region", async () => {
    render(<ReceivablesWorkplace bitrixMode />);

    const region = await screen.findByRole("region", {
      name: "Рабочий список дебиторской задолженности",
    });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region).toHaveAccessibleDescription(
      "Используйте горизонтальную полосу прокрутки или стрелки влево и вправо, чтобы увидеть остальные столбцы.",
    );

    Object.defineProperties(region, {
      clientWidth: { configurable: true, value: 600 },
      scrollLeft: { configurable: true, value: 0, writable: true },
      scrollWidth: { configurable: true, value: 1460 },
    });
    fireEvent(window, new Event("resize"));
    await waitFor(() =>
      expect(region.parentElement).toHaveAttribute("data-can-scroll-right", "true"),
    );
    expect(screen.getByText("Прокрутите вправо →")).toBeVisible();

    fireEvent.keyDown(region, { key: "ArrowRight" });
    expect(region.scrollLeft).toBe(120);

    region.scrollLeft = 860;
    fireEvent.scroll(region);
    await waitFor(() =>
      expect(region.parentElement).toHaveAttribute("data-can-scroll-right", "false"),
    );
    expect(screen.getByText("Прокрутите вправо →")).not.toBeVisible();
  });

  it("shows human debt rules and keeps technical values in titles", async () => {
    vi.mocked(fetchReceivableWorkplace).mockResolvedValue({
      ...response,
      payload: [
        {
          ...item,
          documents: [
            {
              amount: "4746.00",
              document_number: "РБГУ0052636",
              is_overdue: true,
              match_details: [],
              open_amount: "4746.00",
              selection_rule: "onec_canonical_continuous_balance_origin",
            },
            {
              amount: "100.00",
              document_number: "РБГУ0052637",
              is_overdue: true,
              match_details: [],
              open_amount: "100.00",
              selection_rule: "legacy_rule_without_label",
            },
          ],
        },
      ],
    });

    render(<ReceivablesWorkplace bitrixMode />);

    fireEvent.click(await screen.findByTitle("Накладные"));
    expect(screen.getByText("Подтверждено непрерывным балансом 1С")).toHaveAttribute(
      "title",
      "onec_canonical_continuous_balance_origin",
    );
    expect(screen.getByText("Неизвестное правило")).toHaveAttribute(
      "title",
      "legacy_rule_without_label",
    );
    expect(screen.queryByText("onec_canonical_continuous_balance_origin")).not.toBeInTheDocument();
  });

  it("keeps folder controls outside an accessible table scroll region", async () => {
    vi.mocked(fetchCounterpartyFolderRecommendations).mockResolvedValue({
      as_of: "2026-07-23",
      freshness_status: "fresh",
      source_status: "cache_ready",
      report_revision: "test",
      summary: { total_count: 1 },
      payload: [
        {
          action_required: true,
          counterparty_name: "Клиент с очень длинным непрерывным названием",
          counterparty_ref: "folder-client",
          current_balance: "10000.00",
          current_folder_name: "ТекущаяПапкаБезПробелов",
          queue: "actionable",
          recommended_folder_name: "РекомендованнаяПапкаБезПробелов",
          status: "move_recommended",
        },
      ],
    });

    render(<ReceivablesWorkplace bitrixMode />);

    fireEvent.click(await screen.findByRole("button", { name: "Контроль папок" }));
    const region = await screen.findByRole("region", {
      name: "Таблица контроля папок контрагентов",
    });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region).not.toContainElement(screen.getByRole("button", { name: "Проверка данных" }));
  });

  it("maps every receivable status to the approved color tone with a neutral fallback", () => {
    expect(
      Object.fromEntries(
        [
          "new_debt",
          "no_answer",
          "calling",
          "sms_sent",
          "waiting_payment",
          "promised_payment",
          "call_back",
          "remind",
          "data_quality_error",
          "intervention_required",
          "escalated",
          "dispute",
          "dispute_check",
          "paid",
          "closed",
          "transfer",
          "not_ours_transfer",
          "on_card_route",
          "no_phone",
          "legacy_unknown",
        ].map((status) => [status, receivableStatusTone(status)]),
      ),
    ).toEqual({
      new_debt: "blue",
      no_answer: "blue",
      calling: "blue",
      sms_sent: "blue",
      waiting_payment: "light-green",
      promised_payment: "light-green",
      call_back: "yellow",
      remind: "yellow",
      data_quality_error: "yellow",
      intervention_required: "red",
      escalated: "red",
      dispute: "red",
      dispute_check: "red",
      paid: "green",
      closed: "green",
      transfer: "purple",
      not_ours_transfer: "purple",
      on_card_route: "graphite",
      no_phone: "gray",
      legacy_unknown: "gray",
    });
  });

  it("warns about paid and clears the visible comment only after a successful response", async () => {
    let resolveSave: ((value: Awaited<ReturnType<typeof updateReceivableWorkplaceItem>>) => void) | undefined;
    const pendingSave = new Promise<Awaited<ReturnType<typeof updateReceivableWorkplaceItem>>>((resolve) => {
      resolveSave = resolve;
    });
    vi.mocked(fetchReceivableWorkplace).mockResolvedValue({
      ...response,
      status_options: [
        { value: "waiting_payment", label: "Ждем оплату", scope: "common" },
        { value: "paid", label: "Оплачено", scope: "common" },
      ],
    });
    vi.mocked(updateReceivableWorkplaceItem).mockReturnValueOnce(pendingSave);

    render(<ReceivablesWorkplace bitrixMode />);

    expect(await screen.findByText("Клиент Тест")).toBeVisible();
    const statusSelect = screen.getByDisplayValue("Ждем оплату");
    expect(statusSelect).toHaveClass("receivables__status-select--light-green");
    fireEvent.change(statusSelect, { target: { value: "paid" } });
    expect(statusSelect).toHaveClass("receivables__status-select--green");
    expect(
      screen.getByText("Комментарий менеджера очистится после успешного сохранения."),
    ).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Комментарий тест" }));
    const comment = screen.getByRole("textbox", { name: /Комментарий менеджера/ });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить комментарий" }));
    expect(comment).toHaveValue("Комментарий тест");

    resolveSave?.({
      item: { ...item, status: "paid", comment: null },
      event: {
        event_type: "manager_update",
        event_at: "2026-07-23T12:00:00",
        source: "web_workplace",
      },
    });
    await waitFor(() => expect(comment).toHaveValue(""));
  });

  it("passes 500 and 1000 ruble debt filters with the other server filters", async () => {
    render(<ReceivablesWorkplace bitrixMode />);

    expect(await screen.findByText("Клиент Тест")).toBeVisible();
    fireEvent.change(screen.getByDisplayValue("Все подразделения"), {
      target: { value: "department-1" },
    });
    fireEvent.change(screen.getByDisplayValue("Все статусы"), {
      target: { value: "waiting_payment" },
    });
    fireEvent.change(screen.getByDisplayValue("Любая сумма долга"), {
      target: { value: "500" },
    });
    fireEvent.change(screen.getByDisplayValue("По сумме"), {
      target: { value: "overdue_days" },
    });
    fireEvent.change(screen.getByDisplayValue("По убыванию"), {
      target: { value: "asc" },
    });

    await waitFor(() =>
      expect(fetchReceivableWorkplace).toHaveBeenLastCalledWith({
        date: "2026-07-23",
        department_ref: "department-1",
        min_debt: 500,
        sort_by: "overdue_days",
        sort_dir: "asc",
        status: "waiting_payment",
      }),
    );

    fireEvent.change(screen.getByDisplayValue("Долг > 500 ₽"), {
      target: { value: "1000" },
    });

    await waitFor(() =>
      expect(fetchReceivableWorkplace).toHaveBeenLastCalledWith(
        expect.objectContaining({ min_debt: 1000 }),
      ),
    );
    expect(screen.getByDisplayValue("Долг > 1 000 ₽")).toBeVisible();
  });

  it("explains filtered overdue total and warns about stale verified data", async () => {
    vi.mocked(fetchReceivableWorkplaceMeta).mockResolvedValueOnce({
      latest_snapshot_date: "2000-01-01",
      department_options: response.department_options,
      cache_status: {},
    });

    render(<ReceivablesWorkplace bitrixMode />);

    expect(await screen.findByText("Просроченная дебиторка в выборке")).toBeVisible();
    expect(
      screen.getByText(
        "Сумма учитывает доступы, подразделение, статус и порог долга и рассчитывается до применения limit.",
      ),
    ).toBeVisible();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Показаны последние проверенные данные за 2000-01-01",
    );
  });

  it("loads actionable folder queue by default and switches to data quality", async () => {
    render(<ReceivablesWorkplace bitrixMode />);

    expect(await screen.findByText("Клиент Тест")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Контроль папок" }));

    await waitFor(() =>
      expect(fetchCounterpartyFolderRecommendations).toHaveBeenCalledWith(
        "2026-07-23",
        "actionable",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Проверка данных" }));
    await waitFor(() =>
      expect(fetchCounterpartyFolderRecommendations).toHaveBeenLastCalledWith(
        "2026-07-23",
        "data_quality",
      ),
    );
  });

  it("shows and saves personal and shared supervisor notes for full access", async () => {
    const sharedNote = {
      id: 10,
      visibility: "shared" as const,
      comment: "Заметка коллеги",
      author_user_id: "43",
      author_name: "Мария Руководитель",
      created_at: "2026-07-23T10:00:00",
      updated_at: "2026-07-23T11:00:00",
      can_edit: false,
    };
    vi.mocked(fetchReceivableWorkplace).mockResolvedValue({
      ...response,
      payload: [{ ...item, supervisor_notes: [sharedNote] }],
    });
    vi.mocked(upsertReceivableSupervisorNote).mockResolvedValueOnce({
      note: {
        id: 11,
        visibility: "shared",
        comment: "Моя общая заметка",
        author_user_id: "42",
        author_name: "Иван Петров",
        created_at: "2026-07-23T12:00:00",
        updated_at: "2026-07-23T12:00:00",
        can_edit: true,
      },
      event: {
        event_type: "supervisor_note_upserted",
        event_at: "2026-07-23T12:00:00",
        source: "web_workplace",
      },
    });

    render(<ReceivablesWorkplace bitrixMode accessLevel="full" />);

    fireEvent.click(await screen.findByRole("button", { name: "Комментарий тест" }));
    expect(screen.getByText("Заметки руководителя")).toBeVisible();
    expect(screen.getByRole("button", { name: "Личная" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Коллегам" }));
    expect(screen.getByText("Заметка коллеги")).toBeVisible();
    expect(screen.getByText(/Мария Руководитель/)).toBeVisible();
    fireEvent.change(screen.getByRole("textbox", { name: "Заметка коллегам" }), {
      target: { value: "Моя общая заметка" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить заметку" }));

    await waitFor(() =>
      expect(upsertReceivableSupervisorNote).toHaveBeenCalledWith(
        "2026-07-23",
        "test-client",
        "shared",
        "Моя общая заметка",
        expect.any(String),
      ),
    );
    expect(screen.getAllByText("Моя общая заметка")[0]).toBeVisible();
  });

  it("keeps supervisor notes read-only for department access", async () => {
    vi.mocked(fetchReceivableWorkplace).mockResolvedValue({
      ...response,
      payload: [
        {
          ...item,
          supervisor_notes: [
            {
              id: 10,
              visibility: "shared",
              comment: "Общая заметка для отдела",
              author_user_id: "42",
              author_name: "Иван Петров",
              created_at: "2026-07-23T10:00:00",
              updated_at: "2026-07-23T11:00:00",
              can_edit: false,
            },
          ],
        },
      ],
    });

    render(<ReceivablesWorkplace bitrixMode accessLevel="department" />);

    fireEvent.click(await screen.findByRole("button", { name: "Комментарий тест" }));
    expect(screen.getByText("Общая заметка для отдела")).toBeVisible();
    expect(screen.getByText("Общие заметки доступны только для чтения.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Личная" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Сохранить заметку" })).not.toBeInTheDocument();
    expect(deleteReceivableSupervisorNote).not.toHaveBeenCalled();
  });
});
