import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const baseMember = (code: string, name: string, order: number) => ({
  identity: { bitrix_product_id: code, xml_id: "", nomenclature_code: code, name, article: code, bitrix_url: `/crm/catalog/17/product/${code}/` },
  properties: { quality: "Новый" }, lifecycle: { label: "Разбор", birthday: "2026-01-10" },
  demand: { sales_30: 12, sales_90: 30, sales_180: 50, rate_30: .4, rate_90: .33, rate_180: .28, sellable_stock: 3, customer_orders: 2, incoming: 4, target_stock: 12, current_order: order, recommended_order: order },
  quality: { return_qty_180: 4, return_document_count_180: 2, new_quality_return_qty_90: 2, new_quality_return_document_count_90: 1, site_excluded_return_qty_90: 1, site_excluded_return_document_count_90: 1, return_reasons_90: ["Не понадобился"], defect_pct: 0, diagnostic_signal_pct: 6.7 },
  supply: { supplier_name: "GOOYEE ANDROID LCDs", receipt_documents: ["Поступление №123"] },
  family: {}, blockers: [{ code: "display_family_recommendation_review_required", message: "Нужно решение", scope: "line", severity: "hard" }],
  orders: [{ order_id: 94, label: "Заказ №94", status: "review", onec_status: "not_sent", app_url: "/bitrix/procurement-order-formation/orders/94" }],
  recommendation: "Проверить причины и распределить количество", source: { state: "ready", calculated_at: "2026-09-04" },
});

const members = [
  baseMember("101", "Основной дисплей Samsung A16", 8),
  baseMember("102", "Дисплей Samsung A16 OLED", 3),
  baseMember("103", "Дисплей Samsung A16 InCell", 1),
  baseMember("104", "Дисплей Samsung A16 с рамкой", 0),
  baseMember("105", "Дисплей Samsung A16 оригинал", 0),
];

const familyCard = {
  ...members[0], facts_hash: "a".repeat(64), facts_snapshot: {},
  family: {
    id: "samsung-a16", record_id: 10, label: "Samsung A16", member_count: 9,
    total_member_count: 9, visible_member_count: 5, hidden_member_count: 4,
    member_codes: ["101", "102", "103", "104", "105", "106", "107", "108", "109"],
    registry_version_number: 7, ranking_source_label: "скорость завершённых продаж за 30 и 90 дней",
    comparison_members: members.map((card, index) => ({ role: index ? "candidate" : "primary", role_label: index ? "Кандидат семьи" : "Основная карточка", rank: index, speed_score: 1 - index / 10, card })),
  },
  review_requirements: { quality: true, distribution: true },
  decisions: { quality: null, distribution: null, blocker_ready: false },
};

const dashboard = {
  folder: "Дисплеи", responsible_user_id: "130757", responsible_name: "Омар",
  updated_at: "2026-09-04T12:00:00Z",
  cards: ["fruit", "newborn", "new_item", "sales_start", "sale", "working", "pension"].map((status, index) => ({
    status, label: `Статус ${index + 1}`, total_count: 10 + index, action_count: index,
    action_kind: index === 6 ? "review" : "transition", action_label: index ? "Проверить" : "Решений нет",
    action_breakdown: {}, ready_count: index, blocked_count: index === 5 ? 2 : 0,
    review_count: 0, overdue_count: 0, urgency: index === 5 ? "blocked" : "neutral",
  })),
  decision_summary: { ready_count: 1, review_count: 1, blocked_count: 1 },
  manual_status_counts: { matrix: 2, on_demand: 1, replace_candidate: 1, nonliquid: 0, do_not_order: 0, pension: 1, review: 1 },
  attention: [{ proposal_id: 77, nomenclature_code: "101", product_name: members[0].identity.name, current_status: "working", current_status_label: "Поддерживаем", kind: "lifecycle", filter_status: "review", action_label: "Открыть разбор", fact_summary: "Возвраты: 4", decision_state: "review", decision_state_label: "Нужен разбор", reason: "Возвраты", recommendation: "Открыть разбор", deadline_label: "Сегодня", urgency: "review", responsible_name: "Сергей", overdue: false }],
  manual_attention: [],
};

async function prepare(page: Page) {
  await page.addInitScript(() => {
    window.__MM_BITRIX_LAUNCH__ = { domain: "crm.example.test" };
    window.sessionStorage.setItem("mm_procurement_order_formation_bitrix_session", JSON.stringify({
      session_token: "test-session-placeholder", expires_at: "2099-01-01T00:00:00Z", expires_in: 3600,
      cached_at: new Date().toISOString(), user: { user_id: "130757", name: "Омар" },
    }));
  });
  await page.route("https://api.bitrix24.com/api/v1/", (route) => route.fulfill({ status: 200, contentType: "application/javascript", body: "" }));
  await page.route("**/api/procurement-order-formation/dashboard", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(dashboard) }));
  await page.route("**/api/procurement-order-formation/products/by-code/101/card", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(familyCard) }));
}

async function expectNoOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
}

test("Витрина открывает семейный разбор и восстанавливает состояние", async ({ page }, testInfo) => {
  await prepare(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/bitrix/procurement-order-formation");

  await expect(page.getByRole("heading", { name: "Общая картина" })).toBeVisible();
  await expect(page.locator(".attention-panel")).toHaveCount(0);
  await page.getByRole("button", { name: /Нужен разбор/ }).click();
  await expect(page.getByRole("heading", { name: "Нужен разбор" })).toBeVisible();
  await page.getByPlaceholder("Поиск товара или причины").fill("Samsung");
  await page.getByRole("button", { name: "Открыть разбор" }).click();
  await expect(page).toHaveURL(/\/review\/101$/);
  await expect(page.getByRole("heading", { name: members[0].identity.name, level: 1 })).toBeVisible();
  await expect(page.getByText("Показано 5 из 9 · скрыто 4")).toBeVisible();
  await expect(page.getByRole("columnheader", { name: /Основная карточка/ })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("family-review-1440.png"), fullPage: true });
  await page.getByRole("button", { name: /Вернуться на Витрину/ }).click();
  await expect(page.getByPlaceholder("Поиск товара или причины")).toHaveValue("Samsung");
  await expect(page.getByRole("heading", { name: "Нужен разбор" })).toBeVisible();
  await expect(page.getByText(/run/i)).toHaveCount(0);
  await page.locator(".attention-panel h2").click();
  await page.keyboard.press("j");
  await page.keyboard.press("Space");
  await expect(page.getByRole("checkbox", { name: /Выбрать/ })).toBeChecked();
  await page.setViewportSize({ width: 390, height: 844 });
  for (const tab of ["Витрина", "Помощник", "Заказы", "Свойства", "История"]) {
    await expect(page.getByRole("button", { name: tab, exact: true })).toBeVisible();
  }
  await expectNoOverflow(page);
});

for (const width of [1440, 1024, 768, 390]) {
  test(`Витрина остаётся обзором и открывает очередь на ${width}px`, async ({ page }, testInfo) => {
    await prepare(page);
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await page.goto("/bitrix/procurement-order-formation");

    await expect(page.getByRole("heading", { name: "Общая картина" })).toBeVisible();
    await expect(page.getByRole("button", { name: /Товаров на витрине/ })).toBeVisible();
    await expect(page.locator(".attention-panel")).toHaveCount(0);
    await expectNoOverflow(page);
    await page.screenshot({ path: testInfo.outputPath(`dashboard-overview-${width}.png`), fullPage: true });

    await page.getByRole("button", { name: /Нужен разбор/ }).click();
    await expect(page.getByRole("heading", { name: "Нужен разбор" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Открыть разбор" })).toBeVisible();
    await expectNoOverflow(page);
    if (width === 390) {
      await expect(page.getByRole("button", { name: "Открыть разбор" })).toHaveCSS("min-height", "44px");
    }
    const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
    expect(axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical")).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath(`dashboard-queue-${width}.png`), fullPage: true });
  });
}

for (const width of [1024, 768, 390]) {
  test(`семейный разбор адаптивен на ${width}px и доступен с клавиатуры`, async ({ page }, testInfo) => {
    await prepare(page);
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await page.goto("/bitrix/procurement-order-formation/review/101");
    await expect(page.getByRole("heading", { name: members[0].identity.name, level: 1 })).toBeVisible();
    await expectNoOverflow(page);
    if (width === 390) {
      await expect(page.locator(".family-review__matrix")).toBeHidden();
      await expect(page.getByRole("button", { name: "Следующий товар" })).toBeVisible();
      await expect(page.getByRole("button", { name: "Следующий товар" })).toHaveCSS("min-height", "44px");
    }
    const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
    expect(axe.violations.filter((item) => item.impact === "serious" || item.impact === "critical")).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath(`family-review-${width}.png`), fullPage: true });
  });
}
