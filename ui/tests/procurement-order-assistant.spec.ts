import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";

const readyLine = {
  id: 40,
  line_number: 1,
  version: 1,
  bitrix_product_id: "40699",
  bitrix_product_xml_id: "11111111-2222-3333-4444-555555555555",
  nomenclature_ref: "11111111-2222-3333-4444-555555555555",
  nomenclature_code: "044702",
  nomenclature_name: "Аккумулятор iPhone 11 Premium",
  recommended_quantity: "12",
  final_quantity: "12",
  purchase_price: "750",
  amount: "9000",
  currency: "RUB",
  source_kind: "automatic",
  explicit_demand: false,
  risk_codes: [],
  blockers: [],
  payload: {},
  removed: false,
  photo_thumbnail_url: "https://master-mobile.ru/upload/thumb/40699.webp",
  photo_original_url: "https://master-mobile.ru/upload/original/40699.webp",
  product_card_url: "https://master-mobile.ru/catalog/zapchasti/akkumulyatory/40699/",
  photo_source: "master_mobile_site",
  photo_count: 1,
  profitability_pct: "34.6",
  supplier_defect_pct: "0.8",
  supplier_defect_history_units: 1842,
  supplier_defect_attribution: "supplier_exact",
  supplier_defect_confidence: "reliable",
  price_change_pct: "-2.1",
  delivery_days: 12,
  supplier_prepare_days: 5,
  logistics_days: 7,
  lead_time_days: 12,
  lead_time_source_level: "sku",
  lead_time_confidence: "high",
};

const readyOrder = {
  id: 12,
  stable_key: "order-12",
  status: "draft",
  version: 1,
  supplier_ref: "supplier-ref",
  supplier_name: "Tianma",
  contract_ref: "contract-ref",
  contract_name: "Основной договор",
  currency: "RUB",
  warehouse_name: "Главный склад",
  procurement_contour: "ordinary",
  route: "ordinary",
  batch_id: "2026-08-01",
  order_date: "2026-08-05",
  calculation_id: "calc-1",
  onec_status: "not_sent",
  blockers: [],
  total_amount: "9000",
  manual_status_options: {},
  supplier_profile: {
    qualification_class: "A",
    qualification_label: "Лучшие условия",
    profitability_pct: "34.6",
    defect_pct: "0.8",
    defect_history_units: 1842,
    on_time_pct: "94",
    payment_terms: "30/70",
    credit_days: 45,
    credit_limit: "250000",
    advantages: ["Компенсация брака"],
    history_order_count: 24,
    updated_at: "2026-08-01",
    data_status: "ready",
  },
  lines: [readyLine],
};

const blockedOrder = {
  ...readyOrder,
  id: 13,
  stable_key: "order-13",
  supplier_name: "Без фото",
  lines: [{
    ...readyLine,
    id: 41,
    nomenclature_code: "099999",
    nomenclature_name: "Строка без подтверждённой карточки",
    photo_thumbnail_url: null,
    photo_original_url: null,
    product_card_url: null,
    photo_source: null,
    photo_count: 0,
  }],
};

const assistantResponse = {
  updated_at: "2026-08-01T10:00:00Z",
  summary: {
    lines: 2,
    ready_lines: 1,
    supplier_missing_lines: 0,
    price_changed_lines: 2,
    low_profitability_lines: 0,
    high_defect_lines: 0,
    photo_missing_lines: 1,
    orders: 2,
  },
  orders: [readyOrder, blockedOrder],
};

test("assistant buttons, disabled states, supplier CSV and accessibility work", async ({ page }, testInfo) => {
  await page.addInitScript(() => {
    window.sessionStorage.setItem("mm_procurement_order_formation_bitrix_session", JSON.stringify({
      session_token: "test-session-placeholder",
      expires_at: "2099-01-01T00:00:00Z",
      expires_in: 3600,
      cached_at: new Date().toISOString(),
      user: { user_id: "130757", name: "Омар" },
    }));
    window.sessionStorage.setItem("mm_procurement_order_formation_left_menu_v3_bound", "1");
  });
  let assembleBody: Record<string, unknown> | null = null;
  await page.route("**/api/procurement-order-formation/assistant**", async (route) => {
    if (new URL(route.request().url()).pathname.endsWith("/assistant/assemble")) {
      assembleBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          approved: 1,
          blocked: 0,
          stale: 0,
          items: [{ order_id: 12, status: "approved", message: "Проект заказа собран" }],
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(assistantResponse),
    });
  });
  await page.route("https://master-mobile.ru/upload/**", (route) => route.fulfill({
    status: 200,
    contentType: "image/gif",
    body: Buffer.from("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==", "base64"),
  }));

  await page.goto("/bitrix/procurement-order-formation/assistant");
  await expect(page.getByRole("heading", { name: "Помощник заказов" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Карточка товара" })).toHaveAttribute(
    "href",
    readyLine.product_card_url,
  );
  await expect(page.getByRole("checkbox", { name: /Строка без подтверждённой/ })).toBeDisabled();
  await expect(page.getByText("Недоступно: не найдена точная карточка товара")).toBeVisible();

  const decision = page.getByRole("button", { name: "Включено" });
  await expect(decision).toBeInViewport();
  await expect(decision).toHaveAttribute("aria-pressed", "true");
  await decision.click();
  await expect(page.getByRole("button", { name: "Включить", exact: true }).first()).toHaveAttribute(
    "aria-pressed",
    "false",
  );
  await expect(page.getByText("Выберите хотя бы один полностью готовый проект заказа.")).toBeVisible();
  await page.getByRole("button", { name: "Включить", exact: true }).first().click();

  await page.getByText("Пакет поставщику").click();
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Список + фото" }).click();
  const download = await downloadPromise;
  const downloadPath = await download.path();
  expect(downloadPath).not.toBeNull();
  const csv = await readFile(downloadPath!, "utf-8");
  expect(csv).toContain(readyLine.product_card_url);
  expect(csv).toContain(readyLine.photo_original_url);
  await expect(page.getByText("Файл заказа поставщику подготовлен")).toBeVisible();

  await page.getByRole("button", { name: "Собрать 1 проект заказа" }).click();
  await expect.poll(() => assembleBody).not.toBeNull();
  expect(assembleBody).toMatchObject({ items: [{ order_id: 12, expected_version: 1 }] });

  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(results.violations).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("procurement-order-assistant.png"), fullPage: true });
});
