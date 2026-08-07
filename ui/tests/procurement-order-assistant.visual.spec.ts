import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const artifactRoot =
  "/root/.codex/visualizations/2026/08/01/019fbd4c-7110-7461-9a9f-e21a5eac5cac/procurement-order-assistant-design-qa";
const assetRoot = path.join(artifactRoot, "assets");

const products = {
  tianma: [
    ["Дисплей iPhone 15 Pro OLED (Tianma)", "MMI-15P-OLED-TM", "48.20", "34.6", "0.8", "-2.1", 12, 1],
    ["Аккумулятор iPhone 15 3349mAh", "MMI-15-BAT-3349", "11.80", "22.7", "1.1", "5.2", 18, 4],
    ["Стекло защитное iPhone 15 (OEM)", "MMI-15-GLASS-OEM", "0.68", "31.2", "0.6", "0", 9, 5],
    ["Лоток SIM iPhone 15 (Blue)", "MMI-15-SIM-TRAY-BL", "0.42", "18.1", "2.4", "0", 15, 8],
    ["Вибромотор iPhone 15", "MMI-15-VIB-ENGINE", "1.85", "33.8", "0.7", "-0.5", 7, 7],
  ],
  visionox: [
    ["Камера задняя iPhone 15 Pro 48MP", "MMI-15P-CAM-R48", "23.40", "28.1", "1.9", "-1.3", 10, 2],
    ["Динамик полифонический iPhone 15", "MMI-15-SPK-POLY", "2.95", "20.3", "1.9", "6.1", 11, 6],
    ["Дисплей iPhone 15 OLED (Visionox)", "MMI-15-OLED-VX", "45.60", "27.8", "1.7", "1.2", 13, 1],
    ["Шлейф зарядки iPhone 15 (USB-C)", "MMI-15-FLEX-USB-VX", "4.10", "25.4", "1.5", "0", 14, 3],
  ],
  boe: [
    ["Шлейф зарядки iPhone 15 (USB-C)", "MMI-15-FLEX-USB", "4.35", "17.4", "3.6", "8.7", 14, 3],
    ["Плата нижняя iPhone 15", "MMI-15-BOARD-LOW", "8.90", "16.8", "3.2", "4.5", 16, 9],
    ["Дисплей iPhone 15 OLED (BOE)", "MMI-15-OLED-BOE", "42.70", "19.1", "3.4", "2.8", 17, 1],
  ],
} as const;

function line(
  orderId: number,
  lineNumber: number,
  row: readonly [string, string, string, string, string, string, number, number],
) {
  const [name, code, price, profitability, defect, priceChange, deliveryDays, imageIndex] = row;
  const quantity = 200 + lineNumber * 200;
  return {
    id: orderId * 100 + lineNumber,
    line_number: lineNumber,
    version: 1,
    bitrix_product_xml_id: `00000000-0000-0000-${orderId}-${String(lineNumber).padStart(12, "0")}`,
    nomenclature_ref: `ref-${orderId}-${lineNumber}`,
    nomenclature_code: code,
    nomenclature_name: name,
    recommended_quantity: String(quantity),
    final_quantity: String(quantity),
    purchase_price: price,
    amount: String(Number(price) * quantity),
    currency: "USD",
    source_kind: "automatic",
    explicit_demand: false,
    risk_codes: [],
    blockers: [],
    payload: {},
    removed: false,
    photo_thumbnail_url: `https://qa-assets.local/product-${imageIndex}.png`,
    photo_original_url: `https://qa-assets.local/product-${imageIndex}.png`,
    photo_count: 1,
    profitability_pct: profitability,
    supplier_defect_pct: defect,
    supplier_defect_history_units: orderId === 101 ? 1842 : orderId === 102 ? 940 : 510,
    price_change_pct: priceChange,
    delivery_days: deliveryDays,
  };
}

function order(
  id: number,
  supplier: string,
  rows: readonly (readonly [string, string, string, string, string, string, number, number])[],
  supplierProfile: Record<string, unknown>,
) {
  const lines = rows.map((row, index) => line(id, index + 1, row));
  return {
    id,
    stable_key: `qa-order-${id}`,
    status: "draft",
    version: 1,
    supplier_ref: `supplier-${id}`,
    supplier_code: String(id),
    supplier_name: supplier,
    contract_ref: `contract-${id}`,
    contract_name: "Основной договор",
    warehouse_name: "Главный склад",
    currency: "USD",
    procurement_contour: "ordinary",
    route: "ordinary",
    batch_id: "2026-08-01",
    order_date: "2026-08-05",
    calculation_id: "qa-assistant-2026-08-01",
    onec_status: "not_sent",
    blockers: [],
    total_amount: String(lines.reduce((sum, item) => sum + Number(item.amount), 0)),
    lines,
    manual_status_options: {},
    supplier_profile: supplierProfile,
  };
}

const assistantPayload = {
  updated_at: "2026-08-01T10:00:00",
  summary: {
    lines: 74,
    ready_lines: 46,
    supplier_missing_lines: 9,
    price_changed_lines: 6,
    low_profitability_lines: 5,
    high_defect_lines: 4,
    photo_missing_lines: 3,
    orders: 3,
  },
  orders: [
    order(101, "Tianma", products.tianma, {
      qualification_class: "A",
      qualification_label: "Лучшие условия",
      profitability_pct: "34.6",
      defect_pct: "0.8",
      defect_history_units: 1842,
      on_time_pct: "94",
      payment_terms: "30/70",
      credit_days: 45,
      credit_limit: "25000",
      advantages: ["Компенсация брака", "Бесплатные образцы", "Быстрый ответ"],
      history_order_count: 24,
      updated_at: "2026-08-01",
      data_status: "ready",
    }),
    order(102, "Visionox", products.visionox, {
      qualification_class: "B",
      qualification_label: "Стабильные поставки",
      profitability_pct: "28.1",
      defect_pct: "1.9",
      defect_history_units: 940,
      on_time_pct: "88",
      payment_terms: "50/50",
      credit_days: 15,
      advantages: ["Стабильное качество"],
      history_order_count: 18,
      updated_at: "2026-07-30",
      data_status: "ready",
    }),
    order(103, "BOE", products.boe, {
      qualification_class: "C",
      qualification_label: "Только предоплата",
      profitability_pct: "17.4",
      defect_pct: "3.6",
      defect_history_units: 510,
      on_time_pct: "76",
      payment_terms: "Предоплата 100%",
      advantages: [],
      history_order_count: 11,
      updated_at: "2026-07-28",
      data_status: "partial",
    }),
  ],
};

test.use({ viewport: { width: 1486, height: 1059 }, deviceScaleFactor: 1 });

test("captures and verifies the procurement order assistant", async ({ page }) => {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.addInitScript(() => {
    window.sessionStorage.setItem(
      "mm_procurement_order_formation_bitrix_session",
      JSON.stringify({
        session_token: "qa-session-placeholder",
        expires_at: "2099-01-01T00:00:00Z",
        expires_in: 3600,
        cached_at: new Date().toISOString(),
        user: { user_id: "130757", name: "Омар" },
      }),
    );
    window.sessionStorage.setItem("mm_procurement_order_formation_left_menu_v3_bound", "1");
  });

  await page.route("https://qa-assets.local/**", async (route) => {
    const filename = new URL(route.request().url()).pathname.split("/").pop() || "product-1.png";
    await route.fulfill({ path: path.join(assetRoot, filename), contentType: "image/png" });
  });
  await page.route("**/api/procurement-order-formation/assistant**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/assemble")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ approved: 3, blocked: 0, stale: 0, items: [] }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(assistantPayload),
    });
  });

  await page.goto("/bitrix/procurement-order-formation/assistant");
  await expect(page.getByRole("heading", { name: "Помощник заказов" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Выбрано 12 строк" })).toBeVisible();
  await expect(page.getByText("Класс A")).toBeVisible();
  await expect(page.getByText("Класс B")).toBeVisible();
  await expect(page.getByText("Класс C")).toBeVisible();
  await expect(page.locator(".order-assistant__table tbody tr")).toHaveCount(12);
  await page.waitForFunction(() =>
    Array.from(document.images).every((image) => image.complete && image.naturalWidth > 0),
  );

  const firstPackage = page.locator(".order-assistant__supplier-card details").first();
  await firstPackage.locator("summary").click();
  await expect(firstPackage.getByRole("button", { name: "Список + фото" })).toBeVisible();

  await page.screenshot({
    path: path.join(artifactRoot, "implementation-1486x1059-final.png"),
    animations: "disabled",
  });

  const quickFilters = page.getByRole("region", { name: "Быстрые фильтры" });
  await quickFilters.getByRole("button", { name: /Брак выше 2%/ }).click();
  await expect(page.locator(".order-assistant__table tbody tr")).toHaveCount(4);
  await expect(page.getByText("Плата нижняя iPhone 15")).toBeVisible();
  await quickFilters.getByRole("button", { name: /^Все\s*74$/ }).click();
  await page.getByRole("button", { name: "Все фильтры" }).click();
  await page.getByPlaceholder("Товар, код или поставщик").fill("Камера");
  await expect(page.locator(".order-assistant__table tbody tr")).toHaveCount(1);
  await expect(page.getByText("Камера задняя iPhone 15 Pro 48MP")).toBeVisible();
  await page.getByRole("button", { name: "Сбросить" }).click();
  await expect(page.locator(".order-assistant__table tbody tr")).toHaveCount(12);

  const [listDownload] = await Promise.all([
    page.waitForEvent("download"),
    firstPackage.getByRole("button", { name: "Список + фото" }).click(),
  ]);
  const listPath = path.join(artifactRoot, "tianma-order-with-photos.csv");
  await listDownload.saveAs(listPath);
  expect(await readFile(listPath, "utf-8")).toContain("https://qa-assets.local/product-1.png");

  const [photoDownload] = await Promise.all([
    page.waitForEvent("download"),
    firstPackage.getByRole("button", { name: "Фото отдельно" }).click(),
  ]);
  const photoPath = path.join(artifactRoot, "tianma-photos.csv");
  await photoDownload.saveAs(photoPath);
  expect(await readFile(photoPath, "utf-8")).toContain("Оригинал фото");

  const desktopA11y = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  await writeFile(
    path.join(artifactRoot, "axe-desktop.json"),
    JSON.stringify(desktopA11y, null, 2),
    "utf-8",
  );

  await page.setViewportSize({ width: 1024, height: 900 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({
    path: path.join(artifactRoot, "implementation-tablet-1024x900.png"),
    fullPage: true,
    animations: "disabled",
  });

  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({
    path: path.join(artifactRoot, "implementation-mobile-390x844.png"),
    fullPage: true,
    animations: "disabled",
  });

  expect(desktopA11y.violations.filter((item) => ["critical", "serious"].includes(item.impact || ""))).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);
});
