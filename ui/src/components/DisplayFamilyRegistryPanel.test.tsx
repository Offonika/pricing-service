import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as matching from "../api/matching";
import type {
  DisplayFamilyRegistryVersion,
  DisplayFamilyRow,
} from "../api/types";
import { DisplayFamilyRegistryPanel } from "./DisplayFamilyRegistryPanel";

vi.mock("../api/matching", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/matching")>();
  return {
    ...actual,
    fetchDisplayFamilies: vi.fn(),
    fetchDisplayFamily: vi.fn(),
    fetchDisplayFamilyRegistrySummary: vi.fn(),
    fetchDisplayFamilyRegistryVersions: vi.fn(),
  };
});

const version: DisplayFamilyRegistryVersion = {
  id: 1,
  version_number: 1,
  status: "active",
  effective_from: "2026-08-16",
  source_schema: "display_family_registry_preflight_manifest.v2",
  inventory_checksum: "a".repeat(64),
  membership_checksum: "b".repeat(64),
  family_count: 1391,
  member_count: 2689,
  created_by: "bootstrap-user",
  created_at: "2026-08-16T10:00:00Z",
};

const family: DisplayFamilyRow = {
  id: 10,
  family_key: "display-family-iphone-17-pro-max",
  member_count: 11,
  is_singleton: false,
  total_current_stock_qty: 495,
  review_member_count: 6,
  matching_review_member_count: 2,
  quality_unknown_member_count: 6,
  construction_unknown_member_count: 0,
  phone_model_ids: [17],
  phone_models: ["apple iphone 17 pro max"],
  segment_ids: ["unknown|soft_oled|without_frame|ic_pad_unknown"],
  warning_codes: ["quality_unknown"],
  note_codes: [],
};

function renderPanel() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <DisplayFamilyRegistryPanel />
    </QueryClientProvider>
  );
}

describe("DisplayFamilyRegistryPanel", () => {
  beforeEach(() => {
    vi.mocked(matching.fetchDisplayFamilyRegistrySummary).mockResolvedValue({
      active_version: version,
      version_count: 1,
      family_count: 1391,
      member_count: 2689,
      singleton_family_count: 831,
      multi_sku_family_count: 560,
      review_member_count: 2593,
      matching_review_member_count: 773,
      quality_unknown_member_count: 221,
      warning_counts: { quality_unknown: 221 },
      status_counts: { proposed_exact_signature: 1858 },
    });
    vi.mocked(matching.fetchDisplayFamilyRegistryVersions).mockResolvedValue([version]);
    vi.mocked(matching.fetchDisplayFamilies).mockResolvedValue({
      items: [family],
      page: 1,
      page_size: 50,
      total: 1,
    });
    vi.mocked(matching.fetchDisplayFamily).mockResolvedValue({
      ...family,
      registry_version: version,
      physical_model_signatures: [["phone-model:17"]],
      evidence_snapshot: {},
      members: [
        {
          id: 100,
          product_id: 1701,
          segment_id: family.segment_ids[0],
          proposal_status: "proposed_exact_signature",
          quality_segment: "unknown",
          construction_segment: "soft_oled",
          requires_manual_review: true,
          current_stock_qty: 25,
          warning_codes: ["quality_unknown"],
          note_codes: [],
          scope_reasons: ["active_catalog", "current_stock"],
          product: {
            article: "IP17PM-TEST",
            nomenclature_code: "CODE-IP17PM",
            name: "Дисплей для Apple iPhone 17 Pro Max",
            last_sale_at: "2026-08-15",
          },
          matching_evidence: {
            accepted_count: 1,
            requires_review: true,
            warnings: ["accepted_matching_review"],
            matches: [{
              competitor: "moba",
              competitor_item_id: 42,
              competitor_name: "Дисплей конкурента iPhone 17 Pro Max",
              method: "manual",
              model_relation: "same_model_ids",
              property_disagreements: [{ field: "quality", our_value: "unknown", competitor_value: "original" }],
            }],
          },
          identity_evidence: {},
        },
      ],
      events: [
        {
          id: 1,
          action: "bootstrap_activate",
          actor: "bootstrap-user",
          reason: "accepted bundle",
          effective_at: "2026-08-16",
          created_at: "2026-08-16T10:00:00Z",
          evidence_snapshot: {},
        },
      ],
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("shows the active accepted registry and evidence without write actions", async () => {
    renderPanel();

    expect(await screen.findByText("Только чтение")).toBeInTheDocument();
    expect(screen.getByText("Версия 1")).toBeInTheDocument();
    expect(screen.getByText("2689")).toBeInTheDocument();
    expect(await screen.findByText("Дисплей для Apple iPhone 17 Pro Max")).toBeInTheDocument();
    await waitFor(() => expect(matching.fetchDisplayFamily).toHaveBeenCalledWith(10));
    expect(screen.getByText("IP17PM-TEST · CODE-IP17PM")).toBeInTheDocument();
    expect(screen.getAllByText(/конфликты с конкурентами/i).length).toBeGreaterThan(0);
    expect(screen.getByText("Сопоставления конкурентов (1)")).toBeInTheDocument();
    expect(screen.getByText(/Дисплей конкурента iPhone 17 Pro Max/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Применить" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /создать заказ/i })).not.toBeInTheDocument();
  });
});
