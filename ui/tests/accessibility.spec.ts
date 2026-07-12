import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

test("Bitrix executive dashboard shell has no automatically detectable accessibility violations", async ({ page }, testInfo) => {
  await page.addInitScript(() => window.sessionStorage.setItem("mm_executive_dashboard_bitrix_session", JSON.stringify({
    session_token: "test-session-placeholder", expires_at: "2099-01-01T00:00:00Z", expires_in: 3600,
    cached_at: new Date().toISOString(), user: { user_id: "1", name: "Тестовый пользователь" },
    access_level: "full", roles: ["admin"], allowed_blocks: [], allowed_action_domains: [],
  })));
  await page.route("**/api/management/executive-dashboard**", (route) => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "test source unavailable" }) }));
  await page.goto("/bitrix/executive-dashboard/");
  await expect(page.getByRole("heading", { name: "Единая управленческая витрина" })).toBeVisible();
  await expect(page.getByText("Источник временно недоступен. Повторите загрузку через минуту.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Повторить загрузку" })).toBeVisible();
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(results.violations).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("executive-dashboard.png"), fullPage: true });
});

test("Bitrix executive dashboard renders a successful API response", async ({ page }, testInfo) => {
  await page.addInitScript(() => window.sessionStorage.setItem("mm_executive_dashboard_bitrix_session", JSON.stringify({
    session_token: "test-session-placeholder", expires_at: "2099-01-01T00:00:00Z", expires_in: 3600,
    cached_at: new Date().toISOString(), user: { user_id: "1", name: "Тестовый пользователь" },
    access_level: "full", roles: ["admin"], allowed_blocks: ["money_today", "creditors_payables"], allowed_action_domains: ["money_today"],
  })));
  await page.route("**/api/management/executive-dashboard**", (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/actions")) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          as_of: "2026-07-11", freshness_status: "fresh", source_status: "ready", total_count: 0, payload: [],
        }),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        as_of: "2026-07-11",
        generated_at: "2026-07-11T00:00:00Z",
        freshness_status: "fresh",
        source_status: "ready",
        access_level: "full",
        roles: ["admin"],
        allowed_blocks: ["money_today", "creditors_payables"],
        allowed_action_domains: ["money_today"],
        blocks: [
          {
            key: "money_today", title: "Деньги / ДДС", source_status: "ready", freshness_status: "fresh",
            as_of: "2026-07-11", summary: {}, drilldown_url: null,
            metrics: [{
              key: "cash_position_total_balance", label: "Остаток", value: "100.00", unit: "RUB",
              tone: "info", masked: false, source_status: "ready",
            }],
          },
          {
            key: "creditors_payables", title: "Управленческий баланс", source_status: "ready", freshness_status: "fresh",
            as_of: "2026-07-11", drilldown_url: null,
            summary: {
              balance_assets: [{ key: "cash", label: "Денежные средства", amount: "100.00" }],
              balance_liabilities: [{ key: "owners", label: "Задолженность собственникам", amount: "200.00" }],
              balance_assets_total: "100.00",
              balance_liabilities_total: "200.00",
            },
            metrics: [
              { key: "balance_assets_total", label: "Активы", value: "100.00", unit: "RUB", tone: "info", masked: false, source_status: "ready" },
              { key: "balance_liabilities_total", label: "Пассивы", value: "200.00", unit: "RUB", tone: "warning", masked: false, source_status: "ready" },
            ],
          },
        ],
        source_freshness: [],
        top_actions: [],
        summary: {},
      }),
    });
  });

  await page.goto("/bitrix/executive-dashboard/?date=2026-07-11");

  await expect(page.getByRole("heading", { name: "Единая управленческая витрина" })).toBeVisible();
  await expect(page.getByText("Деньги / ДДС", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/100\s*₽/).first()).toBeVisible();
  await expect(page.locator(".executive-management-balance-section")).toContainText("Задолженность собственникам");
  const [gridBox, balanceBox] = await Promise.all([
    page.locator(".executive-grid").boundingBox(),
    page.locator(".executive-management-balance-section").boundingBox(),
  ]);
  expect(gridBox).not.toBeNull();
  expect(balanceBox).not.toBeNull();
  expect(balanceBox!.y).toBeGreaterThan(gridBox!.y + gridBox!.height);
  expect(Math.abs(balanceBox!.width - gridBox!.width)).toBeLessThanOrEqual(1);
  await expect(page.getByText("Источники пока не переданы")).toBeVisible();
  const desktopResults = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(desktopResults.violations).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("executive-dashboard-success.png"), fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("button", { name: "Обновить" })).toBeVisible();
  const hasHorizontalPageOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth
  );
  expect(hasHorizontalPageOverflow).toBe(false);
  const mobileResults = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(mobileResults.violations).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("executive-dashboard-mobile.png"), fullPage: true });
});
