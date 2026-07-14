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
    if (url.pathname.endsWith("/management-balance")) {
      const month = url.searchParams.get("month") || "2026-07";
      const view = url.searchParams.get("view") || "operational";
      if (month === "2026-07" && view === "closed") {
        return route.fulfill({
          status: 404,
          contentType: "application/json",
          body: JSON.stringify({ detail: "Закрытый снимок отсутствует" }),
        });
      }
      const balanceDate = month === "2026-06" ? "2026-06-30" : "2026-07-11";
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          month, balance_date: balanceDate, view, version: 1,
          status: view === "closed" ? "closed" : "draft", source_status: "partial", freshness_status: "fresh",
          generated_at: "2026-07-11T00:00:00Z", currency: "RUB",
          assets: [
            { key: "cash", label: "Денежные средства", section: "asset", amount: "100.00", source_key: "onec_cash", source_status: "ready", source_as_of: "2026-07-11" },
            { key: "inventory_cost", label: "Товарные остатки по себестоимости", section: "asset", amount: null, source_key: "onec_inventory", source_status: "source_missing" },
            { key: "supplier_receivables", label: "Дебиторка поставщиков", section: "asset", amount: "80.00", source_key: "onec_settlements", source_status: "ready", source_as_of: "2026-07-11" },
            { key: "employee_receivables", label: "Дебиторка сотрудников", section: "asset", amount: "50.00", source_key: "onec_settlements", source_status: "ready", source_as_of: "2026-07-11" },
            { key: "other_receivables", label: "Прочие дебиторы", section: "asset", amount: "20.00", source_key: "onec_settlements", source_status: "ready", source_as_of: "2026-07-11" },
          ],
          liabilities: [
            { key: "owners", label: "Задолженность собственникам", section: "liability", amount: "200.00", source_key: "onec_settlements", source_status: "ready", source_as_of: "2026-07-11" },
          ],
          equity: [
            { key: "owner_capital", label: "Вклады собственников", section: "equity", amount: null, source_key: "ka_bp", source_status: "source_missing" },
          ],
          assets_total: "100.00", liabilities_total: "200.00", equity_total: "0.00",
          liabilities_and_equity_total: "200.00", imbalance_amount: "-100.00",
          can_close: false, validation_errors: [{ code: "mandatory_sources_incomplete" }],
          available_months: ["2026-07", "2026-06"], note: "Полный баланс не подтверждён",
        }),
      });
    }
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
  await expect(page.locator(".executive-management-balance-section")).toContainText("Собственные средства");
  await expect(page.getByLabel("Месяц управленческого баланса")).toHaveValue("2026-07");
  await expect(page.locator(".executive-management-balance-section")).toContainText("Источник не подтверждён");
  await expect(page.locator(".executive-management-balance-section")).toContainText("Дебиторка поставщиков");
  await expect(page.locator(".executive-management-balance-section")).toContainText("Дебиторка сотрудников");
  await expect(page.locator(".executive-management-balance-section")).toContainText("Прочие дебиторы");
  await page.getByRole("button", { name: "Закрытый месяц" }).click();
  await expect(page.getByText("Закрытая версия отсутствует", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Оперативный на сегодня" })).toHaveAttribute("aria-pressed", "true");
  const [gridBox, balanceBox] = await Promise.all([
    page.locator(".executive-grid").boundingBox(),
    page.locator(".executive-management-balance-section").boundingBox(),
  ]);
  expect(gridBox).not.toBeNull();
  expect(balanceBox).not.toBeNull();
  expect(balanceBox!.y).toBeGreaterThan(gridBox!.y + gridBox!.height);
  expect(Math.abs(balanceBox!.width - gridBox!.width)).toBeLessThanOrEqual(1);
  await page.getByLabel("Дата управленческой витрины").fill("2026-06-30");
  await expect(page.getByLabel("Месяц управленческого баланса")).toHaveValue("2026-06");
  await expect(page.getByRole("button", { name: "Закрытый месяц" })).toHaveAttribute("aria-pressed", "true");
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
