import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const longRecommendation = [
  "адаптивный расчёт: количество не изменилось (15 шт.); горизонт 89 → 89 дней;",
  "живой срок применён; поставщик GOOYEE ANDROID LCDs; учтена текущая сезонность дороги и подготовки +15 дней;",
  "активный невыполненный остаток заказов покупателей включён в потребность",
].join(" ");

const productCard = {
  identity: {
    bitrix_product_id: "40699",
    xml_id: "11111111-2222-3333-4444-555555555555",
    nomenclature_code: "РБ000029826",
    name: "Дисплей для Apple iPhone 6 + тачскрин (черный) (Medium)",
    article: "037301",
    photo_url: null,
    bitrix_url: "/crm/catalog/17/product/40699/",
  },
  properties: { assortment_status: "Растим" },
  lifecycle: { status: "grow", label: "Растим" },
  demand: {
    sales_30: "15",
    rate_30: "0.6",
    rate_90: "0.5",
    rate_180: "0.4",
    sellable_stock: "4",
    customer_orders: "1",
    incoming: "3",
    target_stock: "15",
    recommended_order: "15",
  },
  quality: { defect_pct: "3.33", confidence: "medium" },
  supply: {
    supplier_name: "0004 - GOOYEE ANDROID LCDs",
    purchase_price: "1",
    currency: "RUB",
    profitability_pct: "30.17",
    lead_time_days: 89,
  },
  family: { label: "Apple iPhone", member_count: 8 },
  blockers: [{
    code: "display_family_recommendation_review_required",
    scope: "line",
    severity: "hard",
    line_id: 50,
    line_number: 1,
    message: "Требуется проверить и подтвердить распределение заказа внутри семейства дисплеев.",
    evidence: {},
    resolution_actions: [{ kind: "recalculate", label: "Обновить расчёт" }],
  }],
  orders: [{
    order_id: 94,
    label: "Заказ №94",
    status: "review",
    onec_status: "not_sent",
    app_url: "/bitrix/procurement-order-formation/orders/94",
  }],
  recommendation: longRecommendation,
  source: { state: "ready", calculated_at: "2026-09-04" },
};

const order = {
  id: 94,
  version: 1,
  status: "review",
  supplier_name: "0004 - GOOYEE ANDROID LCDs",
  lines: [{
    id: 50,
    line_number: 1,
    version: 1,
    bitrix_product_id: "40699",
    bitrix_product_xml_id: productCard.identity.xml_id,
    nomenclature_ref: productCard.identity.xml_id,
    nomenclature_name: productCard.identity.name,
    recommended_quantity: "15",
    final_quantity: "15",
    purchase_price: "1",
    amount: "15",
    currency: "RUB",
    source_kind: "automatic",
    explicit_demand: false,
    risk_codes: [],
    blockers: ["display_family_recommendation_review_required"],
    removed: false,
  }],
};

async function prepare(page: import("@playwright/test").Page) {
  await page.addInitScript(() => {
    window.__MM_BITRIX_LAUNCH__ = { domain: "crm.example.test" };
    window.sessionStorage.setItem("mm_procurement_order_formation_bitrix_session", JSON.stringify({
      session_token: "test-session-placeholder",
      expires_at: "2099-01-01T00:00:00Z",
      expires_in: 3600,
      cached_at: new Date().toISOString(),
      user: { user_id: "130757", name: "Омар" },
    }));
  });
  await page.route("https://api.bitrix24.com/api/v1/", (route) => route.fulfill({
    status: 200,
    contentType: "application/javascript",
    body: "",
  }));
  await page.route("**/api/procurement-order-formation/products/40699/card", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(productCard),
  }));
  await page.route("**/api/procurement-order-formation/orders/94", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(order),
  }));
}

test("сводка товара остаётся компактной и сохраняет основное действие", async ({ page }, testInfo) => {
  await prepare(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/bitrix/procurement-order-formation?view=product_insights&productId=40699&orderId=94&lineId=50");

  await expect(page.getByRole("button", { name: "Подтвердить строку" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Обновить расчёт" })).toBeVisible();
  await expect(page.getByText("Этикетки на весь заказ")).toHaveCount(0);
  const decisionGrid = page.locator(".product-insights__decision-grid");
  await expect(decisionGrid.locator("article")).toHaveCount(4);
  expect((await decisionGrid.boundingBox())!.height).toBeLessThan(270);

  await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze()
    .then((result) => expect(result.violations).toEqual([]));
  await page.screenshot({ path: testInfo.outputPath("product-insights-1440.png"), fullPage: false });

  await page.getByText("Подробнее").click();
  await expect(decisionGrid.locator(".product-insights__recommendation-details > p")).toHaveText(longRecommendation);

  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)).toBe(false);
  await expect(page.getByRole("button", { name: "Подтвердить строку" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("product-insights-390.png"), fullPage: false });
});

test("без контекста строки показывает переход к разбору связанного заказа", async ({ page }) => {
  await prepare(page);
  await page.goto("/bitrix/procurement-order-formation?view=product_insights&productId=40699");

  await expect(page.getByRole("heading", { name: productCard.identity.name })).toBeVisible();
  await expect(page.getByRole("link", { name: "Разобрать блокер" })).toHaveAttribute(
    "href",
    "/bitrix/procurement-order-formation/orders/94?line=50",
  );
  await expect(page.getByRole("button", { name: "Подтвердить строку" })).toHaveCount(0);
});
