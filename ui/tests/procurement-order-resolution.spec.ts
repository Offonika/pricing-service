import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const baseLine = {
  version: 1,
  bitrix_product_id: "40699",
  bitrix_product_xml_id: "11111111-2222-3333-4444-555555555555",
  nomenclature_ref: "11111111-2222-3333-4444-555555555555",
  nomenclature_code: "РБ000037607",
  recommended_quantity: "12",
  final_quantity: "12",
  purchase_price: "750",
  amount: "9000",
  currency: "RUB",
  source_kind: "automatic",
  explicit_demand: false,
  risk_codes: [],
  payload: {
    batch_error_return_qty: "8",
    batch_error_share_pct: "44.4",
    suspected_batch: "Партия 2026-07-15",
  },
  removed: false,
  photo_thumbnail_url: "https://master-mobile.ru/upload/thumb/40699.webp",
  photo_original_url: "https://master-mobile.ru/upload/original/40699.webp",
  product_card_url: "https://master-mobile.ru/catalog/displei/40699/",
  photo_source: "master_mobile_site",
  photo_count: 1,
  profitability_pct: "18.4",
  supplier_defect_pct: "12.6",
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

const blockedLines = [
  { lineNumber: 20, returnQty: 24, sharePct: 72.7 },
  { lineNumber: 30, returnQty: 8, sharePct: 44.4 },
].map(({ lineNumber, returnQty, sharePct }, index) => ({
  ...baseLine,
  id: 50 + index,
  line_number: lineNumber,
  nomenclature_code: `РБ00003760${7 + index}`,
  nomenclature_name: `Проблемная строка ${lineNumber}`,
  blockers: ["batch_error_suspected"],
  profitability_pct: null,
  supplier_defect_pct: "0",
  supplier_defect_attribution: null,
  blocker_details: [{
    code: "batch_error_suspected",
    scope: "line",
    severity: "hard",
    line_id: 50 + index,
    line_number: lineNumber,
    message: `Подозрение на партийную ошибку: ${returnQty} возвратов, ${sharePct}% за 90 дней.`,
    evidence: {
      return_qty: returnQty,
      share_pct: sharePct,
      minimum_return_qty: 5,
      minimum_share_pct: 40,
      window_days: 90,
      suspected_batch: "Партия 2026-07-15",
    },
    resolution_actions: [
      { kind: "remove_line", label: "Исключить строку", requires_reason: true },
      { kind: "recalculate", label: "Дождаться нового расчёта" },
    ],
  }],
}));

const safeLine = {
  ...baseLine,
  id: 49,
  line_number: 1,
  nomenclature_code: "РБ000037600",
  nomenclature_name: "Готовая строка",
  blockers: [],
  blocker_details: [],
};

const removedLines = Array.from({ length: 35 }, (_, index) => ({
  ...baseLine,
  id: 100 + index,
  line_number: 101 + index,
  nomenclature_code: `REMOVED-${index + 1}`,
  nomenclature_name: `Исключённая строка ${index + 1}`,
  blockers: [],
  blocker_details: [],
  removed: true,
}));

const project94 = {
  id: 94,
  stable_key: "order-94",
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
  batch_id: "2026-08-20",
  order_date: "2026-08-20",
  calculation_id: "calc-94",
  onec_status: "not_sent",
  blockers: blockedLines.map((line) => `line_${line.line_number}:batch_error_suspected`),
  blocker_details: blockedLines.map((line) => ({ ...line.blocker_details[0], scope: "order" })),
  total_amount: "36000",
  manual_status_options: { pension: "Допродаём", working: "Рабочий" },
  supplier_profile: {
    qualification_class: "A",
    qualification_label: "Лучшие условия",
    profitability_pct: "18.4",
    defect_pct: "12.6",
    defect_history_units: 1842,
    on_time_pct: "94",
    payment_terms: "30/70",
    credit_days: 45,
    credit_limit: "250000",
    advantages: ["Компенсация брака"],
    history_order_count: 24,
    updated_at: "2026-08-20",
    data_status: "ready",
  },
  lines: [safeLine, ...blockedLines, ...removedLines],
};

const assistantResponse = {
  updated_at: "2026-08-20T10:00:00Z",
  summary: {
    lines: 38,
    ready_lines: 0,
    supplier_missing_lines: 0,
    price_changed_lines: 4,
    low_profitability_lines: 1,
    high_defect_lines: 0,
    photo_missing_lines: 0,
    orders: 1,
  },
  orders: [project94],
};

const viewports = [
  { width: 1440, height: 1024 },
  { width: 1024, height: 900 },
  { width: 820, height: 900 },
  { width: 768, height: 900 },
  { width: 390, height: 844 },
];

async function expectNoPageOverflow(page: import("@playwright/test").Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth
  );
  expect(overflow).toBe(false);
}

async function expectAxeClean(page: import("@playwright/test").Page) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(results.violations).toEqual([]);
}

test("blocked project explains resolution and stays usable at all target widths", async ({ page }, testInfo) => {
  const browserErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));
  await page.addInitScript(() => {
    window.__MM_BITRIX_LAUNCH__ = { domain: "crm.example.test" };
    window.sessionStorage.setItem("mm_procurement_order_formation_bitrix_session", JSON.stringify({
      session_token: "test-session-placeholder",
      expires_at: "2099-01-01T00:00:00Z",
      expires_in: 3600,
      cached_at: new Date().toISOString(),
      user: { user_id: "130757", name: "Омар" },
    }));
    window.sessionStorage.setItem("mm_procurement_order_formation_left_menu_v3_bound", "1");
  });
  await page.route("https://master-mobile.ru/upload/**", (route) => route.fulfill({
    status: 200,
    contentType: "image/gif",
    body: Buffer.from("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==", "base64"),
  }));
  await page.route("https://api.bitrix24.com/api/v1/", (route) => route.fulfill({
    status: 200,
    contentType: "application/javascript",
    body: "",
  }));
  await page.route("**/api/procurement-order-formation/assistant**", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(assistantResponse),
  }));
  await page.route("**/api/procurement-order-formation/orders/94", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(project94),
  }));

  await page.goto("/bitrix/procurement-order-formation/assistant");
  const exactReason = (
    "Проект №94 заблокирован: подозрение на партийную ошибку — строки 20, 30"
  );
  await expect(page.getByText(exactReason)).toHaveCount(1);
  await expect(page.getByText("1 причина · 2 проблемные строки")).toBeVisible();
  await expect(page.getByRole("complementary", { name: "Профиль поставщика Tianma" })).toHaveCount(0);
  const problem = page.getByText("Проблемная строка 20").first();
  const safe = page.getByText("Готовая строка").first();
  await expect(problem).toBeVisible();
  expect((await problem.boundingBox())!.y).toBeLessThan((await safe.boundingBox())!.y);
  await expect(page.getByText(/Возвраты партии:/)).toHaveCount(2);
  await expect(page.getByText("Возвраты партии: 72,7%").first()).toBeVisible();
  await expect(page.getByText("24 возврата").first()).toBeVisible();
  await expect(page.getByText("Подтверждённый брак поставщика: данных нет").first()).toBeVisible();
  await expect(page.locator(".order-assistant__table tr").filter({ hasText: "Готовая строка" }).getByText(/Возвраты партии:/)).toHaveCount(0);
  await expect(page.locator(".order-assistant__table tr.is-unavailable").filter({ hasText: "Готовая строка" })).toHaveCount(1);
  await expect(page.locator(".order-assistant__table tr.is-blocked").filter({ hasText: "Готовая строка" })).toHaveCount(0);
  await expect(page.getByText("Проект заблокирован другой строкой")).toBeVisible();
  await expect(page.getByText("Исключённая строка 1")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Исключённые: 35 · Показать" })).toBeVisible();
  await expect(page.getByText(/sku · high|reliable/)).toHaveCount(0);

  for (const viewport of viewports) {
    await page.setViewportSize(viewport);
    await expectNoPageOverflow(page);
    const action = page.getByRole("button", { name: "Разобрать 2 проблемные строки" });
    await expect(action).toBeVisible();
    expect((await action.boundingBox())!.height).toBeGreaterThanOrEqual(40);
    if (viewport.width <= 820) {
      await expect(page.locator(".order-assistant__table thead")).toBeHidden();
    } else {
      await expect(page.locator(".order-assistant__table thead")).toBeVisible();
    }
    await expectAxeClean(page);
    await page.screenshot({
      path: testInfo.outputPath(`assistant-${viewport.width}.png`),
      fullPage: false,
    });
  }

  const removedToggle = page.getByRole("button", { name: "Исключённые: 35 · Показать" });
  await removedToggle.click();
  await expect(page.getByText("Исключённая строка 1").first()).toBeVisible();
  await page.getByRole("button", { name: "Исключённые: 35 · Скрыть" }).click();
  await expect(page.getByText("Исключённая строка 1")).toHaveCount(0);

  const action = page.getByRole("button", { name: "Разобрать 2 проблемные строки" });
  await action.focus();
  await expect(action).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/orders\/94\?line=50$/);
  const compactRow = page.locator(".order-formation__compact-table tr.is-blocked").filter({
    hasText: "Проблемная строка 20",
  });
  await expect(compactRow).toBeVisible();
  await expect(compactRow.getByText("1 блокер")).toBeVisible();
  await expect(compactRow.getByText("—").first()).toBeVisible();
  await expect(compactRow.getByRole("link", { name: "Проблемная строка 20" })).toHaveAttribute(
    "href",
    "https://crm.example.test/crm/catalog/17/product/40699/"
  );
  await expect(page.getByRole("searchbox", { name: "Поиск товаров" })).toBeVisible();

  for (const viewport of viewports) {
    await page.setViewportSize(viewport);
    await expectNoPageOverflow(page);
    if (viewport.width <= 520) {
      await expect(page.locator(".order-formation__compact-table thead")).toBeHidden();
    } else {
      await expect(page.locator(".order-formation__compact-table thead")).toBeVisible();
    }
    await expectAxeClean(page);
    await page.screenshot({
      path: testInfo.outputPath(`order-compact-${viewport.width}.png`),
      fullPage: false,
    });
  }

  await page.setViewportSize({ width: 390, height: 844 });
  await compactRow.getByRole("button", { name: "Отчёты по товару Проблемная строка 20" }).click();
  await expect(page.locator(".order-formation__reports-menu-heading").getByText("Отчёты", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: /Показатели товара/ })).toBeVisible();
  await expect(page.getByRole("link", { name: /Карточка Bitrix24/ })).toBeVisible();
  await expectAxeClean(page);
  await page.screenshot({ path: testInfo.outputPath("order-reports-390.png"), fullPage: false });
  await page.getByRole("button", { name: "Закрыть меню отчётов" }).click();

  await page.setViewportSize({ width: 1440, height: 1024 });
  await compactRow.getByRole("button", { name: "Отчёты по товару Проблемная строка 20" }).click();
  await page.getByRole("button", { name: /Подробный разбор строки/ }).click();
  const focusedRow = page.locator(".order-formation__row--blocked").filter({
    hasText: "Проблемная строка 20",
  });
  await expect(page.getByText(/Подозрение на партийную ошибку: 24 возврата, 72,7% за 90 дней/).first()).toBeVisible();
  await expect(page.getByText("Возвраты партии: 72,7%")).toBeVisible();
  await expect(page.getByText("24 возврата · порог 5 возвратов и 40%")).toBeVisible();
  await expect(page.getByText("Подтверждённый брак поставщика: данных нет").first()).toBeVisible();
  await expect(page.getByText("Рентабельность: нет данных").first()).toBeVisible();
  await expect(page.getByText(/24 возвратов/)).toHaveCount(0);
  await expect(page.getByText("Брак: 0%")).toHaveCount(0);
  const transferAlert = page.locator(".order-formation__alert");
  await expect(transferAlert).toContainText("Подозрение на партийную ошибку — строки 20, 30");
  await expect(transferAlert).not.toContainText("24 возврата");
  await expect(page.getByText("Обычная закупка")).toBeVisible();
  await expect(page.getByText("1С: Не отправлен")).toBeVisible();
  await expect(page.getByText("Товар Bitrix24: 40699").first()).toBeVisible();
  await expect(focusedRow.getByText("1 блокер")).toBeVisible();
  await expect(
    focusedRow.getByRole("link", { name: "Открыть отчёт показателей товара Проблемная строка 20" })
  ).toHaveAttribute(
    "href",
    "/bitrix/procurement-order-formation?view=product_insights&productId=40699&orderId=94&lineId=50"
  );
  await expect(focusedRow.getByRole("link", { name: "Показатели товара" })).toHaveAttribute(
    "href",
    "/bitrix/procurement-order-formation?view=product_insights&productId=40699&orderId=94&lineId=50"
  );
  await expect(page.getByRole("button", { name: "Проверить и создать черновик в 1С" })).toBeDisabled();
  await expect(page.getByText("Сначала разберите строки 20, 30.")).toBeVisible();
  await expect(page.getByText(/not_sent|ordinary|Bitrix product/)).toHaveCount(0);

  for (const viewport of viewports) {
    await page.setViewportSize(viewport);
    await expectNoPageOverflow(page);
    if (viewport.width <= 1100) {
      await expect(page.locator(".order-formation__table thead")).toBeHidden();
    } else {
      await expect(page.locator(".order-formation__table thead")).toBeVisible();
    }
    await expectAxeClean(page);
    await page.screenshot({
      path: testInfo.outputPath(`order-detailed-${viewport.width}.png`),
      fullPage: false,
    });
    if (viewport.width === 1440 || viewport.width === 390) {
      await focusedRow.screenshot({ path: testInfo.outputPath(`order-row-${viewport.width}.png`) });
    }
  }

  await page.setViewportSize({ width: 390, height: 844 });
  const orderFooter = page.locator(".order-formation__footer");
  await orderFooter.scrollIntoViewIfNeeded();
  await expect(orderFooter.getByText("3 строки")).toBeVisible();
  await expect(page.getByText("Сначала разберите строки 20, 30.")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("order-footer-390.png"), fullPage: false });

  const removalTrigger = focusedRow.getByRole("button", { name: "Исключить строку" });
  await removalTrigger.click();
  const dialog = page.getByRole("dialog", { name: "Исключить строку 20" });
  await expect(dialog).toBeVisible();
  await expect(page.getByPlaceholder("Обязательно укажите, почему строку исключают")).toBeFocused();
  const dialogBox = await dialog.boundingBox();
  expect(dialogBox!.width).toBeGreaterThanOrEqual(320);
  expect(dialogBox!.width).toBeLessThanOrEqual(366);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(removalTrigger).toBeFocused();

  expect(browserErrors).toEqual([]);
});
