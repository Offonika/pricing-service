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
    if (url.pathname.endsWith("/management-balance-turnover")) {
      const month = url.searchParams.get("month") || "2026-07";
      const selectedTo = url.searchParams.get("month_to") || month;
      const dateTo = selectedTo === "2026-06" ? "2026-06-30" : "2026-07-11";
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          month: selectedTo,
          date_from: "2026-01-01",
          date_to: dateTo,
          opening_balance_date: "2026-01-01",
          view: url.searchParams.get("view") || "operational",
          opening_version: 1,
          closing_version: 1,
          opening_status: "draft",
          closing_status: "draft",
          opening_validation_error_count: 4,
          opening_content_sha256: "a".repeat(64),
          closing_content_sha256: "b".repeat(64),
          turnover_method: "mixed_gross_cashflow_and_net_change",
          source_scope: "onec_ut_10_3_plus_bp_accrued_taxes",
          source_status: "partial",
          currency: "RUB",
          lines: [
            {
              key: "cash", label: "Денежные средства", section: "asset",
              opening_balance: "80.00", debit_turnover: "20.00", credit_turnover: "0.00",
              closing_balance: "100.00", reconciliation_difference: "0.00",
              turnover_method: "gross_cashflow_movements", source_key: "onec_cash",
              source_status: "ready",
            },
          ],
          totals: [
            {
              section: "asset", label: "Итого активы", opening_balance: "80.00",
              debit_turnover: "20.00", credit_turnover: "0.00", closing_balance: "100.00",
              reconciliation_difference: "0.00", unknown_line_count: 0,
            },
            {
              section: "liability", label: "Итого обязательства", opening_balance: "0.00",
              debit_turnover: "0.00", credit_turnover: "0.00", closing_balance: "0.00",
              reconciliation_difference: "0.00", unknown_line_count: 0,
            },
            {
              section: "equity", label: "Итого собственные средства", opening_balance: "0.00",
              debit_turnover: "0.00", credit_turnover: "0.00", closing_balance: "0.00",
              reconciliation_difference: "0.00", unknown_line_count: 0,
            },
          ],
          excluded_lines: [],
          opening_imbalance_amount: "0.00",
          closing_imbalance_amount: "0.00",
          opening_scope_imbalance_amount: "80.00",
          closing_scope_imbalance_amount: "100.00",
          unknown_line_count: 0,
          available_months: ["2026-06", "2026-07"],
          available_period_starts: ["2026-01", "2026-07"],
          available_period_ends: ["2026-06", "2026-07"],
          selected_month_from: url.searchParams.get("month_from") || "2026-01",
          selected_month_to: selectedTo,
          note:
            "Диапазон применяется ко всей ОСВ. По денежным средствам показаны валовые движения УТ 10.3.",
        }),
      });
    }
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
          liabilities: [],
          equity: [
            { key: "owner_contributed_funds", label: "Средства, внесённые собственниками", section: "equity", amount: "200.00", source_key: "onec_settlements", source_status: "ready", source_as_of: "2026-07-11" },
          ],
          assets_total: "100.00", liabilities_total: "0.00", equity_total: "200.00",
          liabilities_and_equity_total: "200.00", imbalance_amount: "-100.00",
          can_close: false, validation_errors: [{ code: "mandatory_sources_incomplete" }],
          source_summary: {
            opening_equity: {
              status: "partial",
              baseline_date: "2026-01-01",
              version: 1,
              source_hash: "abcdef1234567890",
              bridge: {
                retained_earnings: "300000000.00",
                owner_contributed_funds: "24808062.82",
                current_period_result: "44972856.05",
                dividends_paid_ytd: "-13765228.19",
                equity_bridge_total: "356015690.68",
              },
            },
          },
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
              balance_liabilities: [],
              balance_equity: [{ key: "owner_contributed_funds", label: "Средства, внесённые собственниками", amount: "200.00" }],
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
  await expect(page.locator(".executive-management-balance-section")).toContainText("Средства, внесённые собственниками");
  await expect(page.locator(".executive-management-balance-section")).toContainText("Собственные средства");
  await expect(page.getByLabel("Месяц управленческого баланса")).toHaveValue("2026-07");
  await expect(page.locator(".executive-management-balance-section")).toContainText("Источник не подтверждён");
  await expect(page.locator(".executive-management-balance-section")).toContainText("Дебиторка поставщиков");
  await expect(page.locator(".executive-management-balance-section")).toContainText("Дебиторка сотрудников");
  await expect(page.locator(".executive-management-balance-section")).toContainText("Прочие дебиторы");
  await expect(page.getByRole("heading", { name: "Мост собственного капитала" })).toBeVisible();
  await expect(page.getByText("Рассчитано автоматически на 01.01.2026")).toBeVisible();
  await expect(page.getByLabel("Начальный месяц оборотно-сальдовой ведомости")).toHaveValue("2026-01");
  await expect(page.getByLabel("Конечный месяц оборотно-сальдовой ведомости")).toHaveValue("2026-07");
  await expect(page.getByRole("table", { name: "Оборотно-сальдовая ведомость по статьям баланса" })).toBeVisible();
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
  await page.getByLabel("Месяц управленческого баланса").selectOption("2026-06");
  await expect(page.getByLabel("Месяц управленческого баланса")).toHaveValue("2026-06");
  await page.getByRole("button", { name: "Закрытый месяц" }).click();
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

test("procurement exceptions survive direct navigation and expose an accessible action form", async ({ page }, testInfo) => {
  await page.addInitScript(() => window.sessionStorage.setItem("mm_procurement_order_formation_bitrix_session", JSON.stringify({
    session_token: "test-session-placeholder", expires_at: "2099-01-01T00:00:00Z",
    cached_at: new Date().toISOString(), user: { user_id: "1", name: "Тестовый закупщик" },
  })));
  await page.route("**/api/procurement-order-formation/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = path.endsWith("/orders/450") ? { id: 450, version: 1, lines: [] } : {
      total: 1, overdue_count: 1, items: [{
        id: 1, order_id: 450, line_id: null, title: "Сверить исполнение — РБГУ0000560",
        reason_code: "receipt_reconciliation", status: "new", version: 1,
        facts_hash: "a".repeat(64), overdue: true,
        first_seen_at: "2026-09-04T09:00:00Z", response_due_at: "2026-09-07T15:00:00Z",
        next_action: null, facts: {},
      }],
    };
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/bitrix/procurement-order-formation/exceptions");
  await expect(page.getByRole("heading", { name: "Исключения закупки" })).toBeVisible();
  await page.getByRole("button", { name: "Обработать", exact: true }).click();
  await expect(page.getByRole("region", { name: "Обработка исключения" })).toBeFocused();
  await expect(page.getByLabel("Следующее действие")).toBeVisible();
  await expect(page.getByLabel("Срок действия, МСК")).toBeVisible();
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(results.violations).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("procurement-exceptions.png"), fullPage: true });
});

test("procurement price context distinguishes agreed price and reference cost", async ({ page }, testInfo) => {
  await page.addInitScript(() => window.sessionStorage.setItem("mm_procurement_order_formation_bitrix_session", JSON.stringify({
    session_token: "test-session-placeholder", expires_at: "2099-01-01T00:00:00Z",
    cached_at: new Date().toISOString(), user: { user_id: "1", name: "Тестовый закупщик" },
  })));
  const unknown = { value: null, currency: "RUB", status: "unconfirmed", documents: [] };
  const cost = { value: "2108.65", currency: "RUB", status: "reference", unit_name: "шт", at: "2026-08-21T11:39:06",
    documents: [{ kind: "УстановкаЦенНоменклатуры", ref: "cost-ref", number: "РБ000001527", at: "2026-08-21T11:39:06" }] };
  await page.route("**/api/procurement-order-formation/**", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
    id: 12, stable_key: "order-12", status: "draft", version: 1, supplier_name: "Поставщик дисплеев", supplier_ref: "supplier-ref",
    contract_name: "Основной договор", currency: "CNY", warehouse_name: "Склад", procurement_contour: "ordinary", route: "ordinary",
    batch_id: "2026-09-06", order_date: "2026-09-06", calculation_id: "calc-1", onec_status: "not_sent", blockers: [],
    total_amount: "0", manual_status_options: { working: "Поддерживаем" }, lines: [{
      id: 40, line_number: 1, version: 1, bitrix_product_id: "2695", bitrix_product_xml_id: "item-ref", nomenclature_ref: "item-ref",
      nomenclature_code: "РБ000064181", nomenclature_name: "Дисплей для проверки цен", recommended_quantity: "30", final_quantity: "30",
      purchase_price: "1", amount: "30", currency: "CNY", source_kind: "automatic", explicit_demand: false,
      risk_codes: [], blockers: [], removed: false, effective_assortment_status: "working", effective_assortment_status_label: "Поддерживаем",
      price_status: "unconfirmed", price_context: { schema_version: 1, agreed_purchase: { ...unknown, currency: "CNY" }, purchase_rub: unknown,
        reference_cost_rub: cost, receipt_purchases_rub: [], actual_cost_status: "not_formed", actual_costs_rub: [],
        supplier_quotes: [{ ...cost, value: "160", currency: "CNY", documents: [] }], source_status: "ready", checked_on: "2026-09-06",
        last_success_on: "2026-09-06", stale: false },
    }],
  }) }));
  await page.goto("/bitrix/procurement-order-formation/orders/12");
  const summary = page.getByLabel("Цена, курс и себестоимость: Дисплей для проверки цен");
  await summary.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("Себестоимость в рублях · справочно")).toBeVisible();
  await expect(page.getByText(/РБ000001527/)).toBeVisible();
  await expect(page.getByText(/Пока не подтверждена связанными документами/)).toBeVisible();
  const dialog = page.getByRole("dialog", { name: "Цена, курс и себестоимость" });
  await expect(dialog).toBeVisible();
  const bounds = await dialog.boundingBox();
  expect(bounds?.width).toBeGreaterThan(600);
  const results = await new AxeBuilder({ page }).include(".procurement-price-context__dialog").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(results.violations).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("procurement-prices.png"), fullPage: true });
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(summary).toBeFocused();
  await page.setViewportSize({ width: 390, height: 844 });
  await summary.click();
  const narrowBounds = await dialog.boundingBox();
  expect(narrowBounds?.width).toBeLessThanOrEqual(358);
  await expect(page.getByRole("button", { name: "Закрыть ценовой контекст" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("procurement-prices-mobile.png"), fullPage: true });
  await page.getByRole("button", { name: "Закрыть ценовой контекст" }).click();
  await expect(summary).toBeFocused();
});
