import { expect, test } from "@playwright/test";

const viewports = [
  { name: "desktop", width: 1024, height: 900 },
  { name: "compact-desktop", width: 768, height: 900 },
  { name: "mobile", width: 390, height: 844 },
];

for (const viewport of viewports) {
  test(`customer return registration stays inside its card on ${viewport.name}`, async ({ page }) => {
    const browserErrors: string[] = [];
    page.on("console", (message) => {
      if (["error", "warning"].includes(message.type())) browserErrors.push(message.text());
    });
    page.on("pageerror", (error) => browserErrors.push(error.message));

    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.addInitScript(() => {
      window.sessionStorage.setItem(
        "mm_logistics_bitrix_session",
        JSON.stringify({
          session_token: "customer-returns-layout-test",
          token_type: "bearer",
          expires_at: "2099-01-01T00:00:00Z",
          expires_in: 3600,
          cached_at: new Date().toISOString(),
          profile: {
            id: 1,
            full_name: "Тестовый администратор",
            role: "admin",
            default_warehouse_id: null,
            default_warehouse_name: null,
          },
        }),
      );
    });
    await page.route("**/api/bitrix/logistics/bootstrap", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          profile: {
            id: 1,
            full_name: "Тестовый администратор",
            role: "admin",
            default_warehouse_id: null,
            default_warehouse_name: null,
          },
          warehouses: [],
          drivers: [],
          capabilities: ["customer_returns", "customer_return_service_links"],
          open_draft: null,
        }),
      }),
    );
    await page.route("**/api/bitrix/logistics/customer-returns**", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
    );

    await page.goto("/bitrix/logistics/");
    await expect(page.getByRole("heading", { name: "Добавить возврат" })).toBeVisible();

    const registrationCard = page
      .locator("form.customer-returns__registration")
      .locator("xpath=ancestor::section[1]");
    const cardBox = await registrationCard.boundingBox();
    expect(cardBox).not.toBeNull();

    const children = page.locator(".customer-returns__registration > *");
    for (let index = 0; index < (await children.count()); index += 1) {
      const childBox = await children.nth(index).boundingBox();
      expect(childBox).not.toBeNull();
      expect(childBox!.x).toBeGreaterThanOrEqual(cardBox!.x);
      expect(childBox!.x + childBox!.width).toBeLessThanOrEqual(cardBox!.x + cardBox!.width);
    }

    const hasHorizontalOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    );
    expect(hasHorizontalOverflow).toBe(false);
    expect(browserErrors).toEqual([]);
  });
}
