import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  fetchProcurementProductCardByCode,
  saveProcurementFamilyDistributionReview,
  saveProcurementFamilyQualityReview,
  type ProcurementFamilyReviewCard,
  type ProcurementProductCard,
} from "../api/procurementAssortment";
import { ProcurementFamilyReview } from "./ProcurementFamilyReview";

vi.mock("../api/procurementAssortment", () => ({
  fetchProcurementProductCardByCode: vi.fn(),
  saveProcurementFamilyDistributionReview: vi.fn(),
  saveProcurementFamilyQualityReview: vi.fn(),
}));

vi.mock("react-hot-toast", () => ({ default: { success: vi.fn(), error: vi.fn() } }));

function member(code: string, name: string, recommended: string): ProcurementProductCard {
  return {
    identity: { bitrix_product_id: code, xml_id: "", nomenclature_code: code, name, article: "" },
    properties: {}, lifecycle: { label: "Разбор" },
    demand: { sales_30: "3", sales_90: "9", sales_180: "18", rate_30: ".1", rate_90: ".1", rate_180: ".1", recommended_order: recommended },
    quality: { return_qty_180: "1", defect_pct: "0" },
    supply: { supplier_name: "Поставщик" }, family: {}, blockers: [], orders: [],
    recommendation: "Проверить", source: { state: "ready", calculated_at: "2026-09-04" },
  };
}

function card(): ProcurementFamilyReviewCard {
  const primary = member("A", "Основной дисплей", "2");
  const candidate = member("B", "Дисплей-кандидат", "0");
  return {
    ...primary,
    identity: { ...primary.identity, bitrix_url: "/crm/catalog/17/product/1/" },
    family: {
      label: "Samsung A16", total_member_count: 6, hidden_member_count: 4,
      member_codes: ["A", "B", "C", "D", "E", "F"], registry_version_number: 7,
      comparison_members: [
        { role: "primary", role_label: "Основная карточка", rank: 0, speed_score: 1, card: primary },
        { role: "candidate", role_label: "Кандидат семьи", rank: 1, speed_score: .5, card: candidate },
      ],
    },
    facts_hash: "a".repeat(64), facts_snapshot: {},
    review_requirements: { quality: true, distribution: true },
    decisions: { quality: null, distribution: null, blocker_ready: false },
  };
}

describe("ProcurementFamilyReview", () => {
  beforeEach(() => {
    window.__MM_BITRIX_LAUNCH__ = { domain: "crm.example.test" };
    vi.mocked(fetchProcurementProductCardByCode).mockResolvedValue(card());
    vi.mocked(saveProcurementFamilyQualityReview).mockResolvedValue({
      event: { id: 1, type: "quality", actor: "Сергей", created_at: "2026-09-04", effective_at: "2026-09-04", reason: "Проверено", facts_hash: "a".repeat(64), registry_version_number: 7, decision: {} },
      idempotent: false,
      decisions: { quality: { id: 1, type: "quality", actor: "Сергей", created_at: "2026-09-04", effective_at: "2026-09-04", reason: "Проверено", facts_hash: "a".repeat(64), registry_version_number: 7, decision: {} }, distribution: null, blocker_ready: false },
      blocker_ready: false,
    });
    vi.mocked(saveProcurementFamilyDistributionReview).mockResolvedValue({
      event: { id: 2, type: "distribution", actor: "Омар", created_at: "2026-09-04", effective_at: "2026-09-04", reason: "Спрос", facts_hash: "a".repeat(64), registry_version_number: 7, decision: {} },
      idempotent: false,
      decisions: { quality: null, distribution: { id: 2, type: "distribution", actor: "Омар", created_at: "2026-09-04", effective_at: "2026-09-04", reason: "Спрос", facts_hash: "a".repeat(64), registry_version_number: 7, decision: {} }, blocker_ready: false },
      blocker_ready: false,
    });
  });

  afterEach(cleanup);

  it("показывает основную карточку первой, скрытых членов и мобильный переключатель", async () => {
    render(<ProcurementFamilyReview nomenclatureCode="A" onBack={vi.fn()} />);

    expect(await screen.findByRole("heading", { name: "Основной дисплей", level: 1 })).toBeInTheDocument();
    const headers = screen.getAllByRole("columnheader");
    expect(headers[1]).toHaveTextContent("Основная карточка");
    expect(screen.getByText(/Показано 2 из 6 · скрыто 4/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Предыдущий товар" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Следующий товар" })).toBeInTheDocument();
  });

  it("сохраняет решения Сергея и Омара независимо", async () => {
    render(<ProcurementFamilyReview nomenclatureCode="A" onBack={vi.fn()} />);
    await screen.findByRole("heading", { name: "Основной дисплей", level: 1 });

    fireEvent.change(screen.getByLabelText("Корневая причина"), { target: { value: "Не связано с качеством" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить решение Сергея" }));
    await waitFor(() => expect(saveProcurementFamilyQualityReview).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText("Обоснование"), { target: { value: "Спрос семьи" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить решение Омара" }));
    await waitFor(() => expect(saveProcurementFamilyDistributionReview).toHaveBeenCalledTimes(1));
    expect(vi.mocked(saveProcurementFamilyDistributionReview).mock.calls[0][1].quantities).toEqual({ A: 2, B: 0, C: 0, D: 0, E: 0, F: 0 });
  });
});
