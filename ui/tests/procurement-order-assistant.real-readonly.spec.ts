import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const sessionToken = process.env.PROCUREMENT_ASSISTANT_QA_SESSION_TOKEN;

test("production data renders in the selected supplier-panel layout without writes", async ({ page }, testInfo) => {
  test.skip(!sessionToken, "Set PROCUREMENT_ASSISTANT_QA_SESSION_TOKEN for the read-only production-data check");
  await page.setViewportSize({ width: 1440, height: 1024 });
  await page.addInitScript((token) => {
    window.sessionStorage.setItem("mm_procurement_order_formation_bitrix_session", JSON.stringify({
      session_token: token,
      expires_at: "2099-01-01T00:00:00Z",
      expires_in: 3600,
      cached_at: new Date().toISOString(),
      user: { user_id: "qa-readonly", name: "Визуальная проверка" },
    }));
    window.sessionStorage.setItem("mm_procurement_order_formation_left_menu_v3_bound", "1");
  }, sessionToken);

  const browserErrors: string[] = [];
  const serverErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) {
      browserErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (url.pathname.startsWith("/api/") && response.status() >= 400) {
      serverErrors.push(`${response.status()} ${url.pathname}`);
    }
  });

  await page.goto("/bitrix/procurement-order-formation/assistant");
  await expect(page.getByRole("heading", { name: "Помощник заказов" })).toBeVisible();
  await expect(page.locator(".order-assistant__table tbody tr").first()).toBeVisible();
  await expect(page.getByRole("complementary", { name: /Профиль поставщика/ })).toBeVisible();

  await page.getByRole("button", { name: /^Без фото\s*2$/ }).click();
  await expect(page.locator(".order-assistant__table tbody tr")).toHaveCount(2);
  await page.getByRole("button", { name: "Сбросить" }).click();
  await page.getByRole("button", { name: "Все фильтры" }).click();
  await page.getByPlaceholder("Товар, код или поставщик").fill("РБ000049297");
  await expect(page.locator(".order-assistant__table tbody tr")).toHaveCount(1);
  await page.getByRole("button", { name: "Сбросить" }).click();

  await page.getByRole("button", { name: "Закрыть панель поставщика" }).click();
  await expect(page.getByRole("complementary", { name: /Профиль поставщика/ })).toBeHidden();
  await page.locator(".order-assistant__link-button").first().click();
  await expect(page.getByRole("complementary", { name: /Профиль поставщика/ })).toBeVisible();

  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(results.violations).toEqual([]);
  expect(browserErrors).toEqual([]);
  expect(serverErrors).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("production-realdata-1440x1024.png"),
    fullPage: false,
  });
});
