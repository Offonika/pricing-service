import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";

const viewports = [
  { name: "1920", width: 1920, height: 1080 },
  { name: "1366", width: 1366, height: 900 },
  { name: "1024", width: 1024, height: 800 },
  { name: "768", width: 768, height: 800 },
  { name: "zoom-200", width: 683, height: 768 },
];

const documentRow = {
  amount: "4746.00",
  document_date: "2026-02-04",
  document_number: "РБГУ0052636",
  due_date: "2026-02-11",
  is_overdue: true,
  manager_name: "ОченьДлинноеИмяОтветственногоБезПробелов",
  match_details: [],
  open_amount: "4746.00",
  overdue_days: 176,
  selection_rule: "onec_canonical_continuous_balance_origin",
};

const workItems = Array.from({ length: 20 }, (_, index) => ({
  comment: `Комментарий менеджера ${index + 1}`,
  counterparty_code: `РБ${String(index + 1).padStart(6, "0")}`,
  counterparty_name: `КлиентСОченьДлиннымНепрерывнымНазваниеБезПробелов${index + 1}`,
  counterparty_ref: `layout-client-${index + 1}`,
  criticality: "normal",
  current_balance: "148095.00",
  department_name: "09. СПБ Садовая",
  documents: [documentRow],
  effective_overdue_days: 176,
  invoice_count: 1,
  needs_call_today: true,
  needs_credit_depth_default: true,
  no_phone_marker: false,
  overdue_amount: "148095.00",
  overdue_invoice_count: 1,
  payment_postponed: false,
  payment_postponed_count: 0,
  phone: "+79990000000",
  phone_status: "present",
  responsible_name: "ОтветственныйБезПробелов",
  snapshot_date: "2026-07-23",
  stable_key: `receivable:layout-client-${index + 1}`,
  staff_options: [],
  status: "calling",
  supervisor_notes: [],
}));

const workplaceResponse = {
  as_of: "2026-07-23",
  cache_status: {},
  department_options: [],
  freshness_status: "fresh",
  payload: workItems,
  source_status: "cache_ready",
  status_options: [{ label: "Звоним", scope: "common", value: "calling" }],
  summary: {
    credit_depth_default_count: 20,
    need_call_today_amount: "2961900.00",
    no_phone_count: 0,
    overdue_over_30_amount: "2961900.00",
    overdue_over_90_amount: "2961900.00",
    row_count: 20,
    total_overdue: "2961900.00",
    total_receivable: "2961900.00",
  },
  summary_scope: "filtered_total",
  total_count: 20,
  visible_count: 20,
};

const folderResponse = {
  as_of: "2026-07-23",
  freshness_status: "fresh",
  payload: [
    {
      action_required: true,
      counterparty_code: "РБ000001",
      counterparty_name: "КлиентСОченьДлиннымНепрерывнымНазваниеБезПробелов",
      counterparty_ref: "folder-layout-client",
      current_balance: "148095.00",
      current_folder_name: "ТекущаяПапкаСОченьДлиннымНепрерывнымНазваниеБезПробелов",
      debt_document_date: "2026-02-04",
      debt_document_number: "РБГУ0052636",
      queue: "actionable",
      recommended_folder_name: "РекомендованнаяПапкаСОченьДлиннымНепрерывнымНазваниеБезПробелов",
      review_reason: "open_debt_document_total_above_balance",
      status: "move_recommended",
    },
  ],
  report_revision: "layout-test",
  source_status: "cache_ready",
  summary: { total_count: 1 },
};

async function mockReceivables(page: Page) {
  await page.addInitScript(() => {
    window.sessionStorage.setItem("pricing.receivables.session_token.v1", "layout-test-token");
  });
  await page.route("**/api/receivables/workplace**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/meta")) {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          cache_status: {},
          department_options: [],
          latest_snapshot_date: "2026-07-23",
        }),
      });
      return;
    }
    if (url.pathname.endsWith("/folder-recommendations")) {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(folderResponse) });
      return;
    }
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(workplaceResponse) });
  });
}

async function assertReachableRightEdge(region: Locator) {
  const metrics = await region.evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
  }));
  if (metrics.scrollWidth > metrics.clientWidth + 1) {
    await region.evaluate((element) => {
      element.scrollLeft = element.scrollWidth;
      element.dispatchEvent(new Event("scroll"));
    });
    const rightEdge = await region.evaluate((element) => ({
      allowance: Math.max(1, element.offsetWidth - element.clientWidth + 1),
      remaining: Math.abs(element.scrollWidth - element.clientWidth - element.scrollLeft),
    }));
    expect(rightEdge.remaining).toBeLessThanOrEqual(rightEdge.allowance);
  }
}

for (const viewport of viewports) {
  test(`receivables tables stay navigable without text overlap at ${viewport.name}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockReceivables(page);
    await page.goto("/receivables/workplace?date=2026-07-23");

    await expect(page.getByText(workItems[0].counterparty_name, { exact: true })).toBeVisible();
    const workRegion = page.getByRole("region", {
      name: "Рабочий список дебиторской задолженности",
    });
    await expect(workRegion).toBeVisible();
    await expect
      .poll(() =>
        workRegion.evaluate((element) => ({
          clientHeight: element.clientHeight,
          overflowX: getComputedStyle(element).overflowX,
          overflowY: getComputedStyle(element).overflowY,
          scrollHeight: element.scrollHeight,
        })),
      )
      .toMatchObject({ overflowX: "auto", overflowY: "auto" });

    await workRegion.focus();
    await page.keyboard.press("ArrowRight");
    if ((await workRegion.evaluate((element) => element.scrollWidth > element.clientWidth + 1))) {
      await expect.poll(() => workRegion.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
    }
    await assertReachableRightEdge(workRegion);
    await expect(
      workRegion.locator("xpath=..").getByText("Прокрутите вправо →", { exact: true }),
    ).toBeHidden();

    const rightHeader = page.getByRole("columnheader", { name: "Комментарий" });
    const [regionBox, rightHeaderBox] = await Promise.all([
      workRegion.boundingBox(),
      rightHeader.boundingBox(),
    ]);
    expect(regionBox).not.toBeNull();
    expect(rightHeaderBox).not.toBeNull();
    expect(rightHeaderBox!.x + rightHeaderBox!.width).toBeLessThanOrEqual(
      regionBox!.x + regionBox!.width + 1,
    );

    await workRegion.evaluate((element) => {
      element.scrollLeft = 0;
      element.dispatchEvent(new Event("scroll"));
    });
    await page.getByTitle("Накладные").first().click();
    const debtRule = page.getByText("Подтверждено непрерывным балансом 1С").first();
    await expect(debtRule).toHaveAttribute("title", "onec_canonical_continuous_balance_origin");
    const [ruleBox, ruleCellBox] = await Promise.all([
      debtRule.boundingBox(),
      debtRule.locator("xpath=..").boundingBox(),
    ]);
    expect(ruleBox).not.toBeNull();
    expect(ruleCellBox).not.toBeNull();
    expect(ruleBox!.x).toBeGreaterThanOrEqual(ruleCellBox!.x - 1);
    expect(ruleBox!.x + ruleBox!.width).toBeLessThanOrEqual(
      ruleCellBox!.x + ruleCellBox!.width + 1,
    );

    await page.screenshot({
      path: testInfo.outputPath(`receivables-work-list-${viewport.name}.png`),
      fullPage: true,
    });

    await page.getByRole("button", { name: "Контроль папок" }).click();
    const folderRegion = page.getByRole("region", { name: "Таблица контроля папок контрагентов" });
    await expect(folderRegion).toBeVisible();
    await assertReachableRightEdge(folderRegion);
    await expect(
      folderRegion.locator("xpath=..").getByText("Прокрутите вправо →", { exact: true }),
    ).toBeHidden();
    await expect(page.getByRole("button", { name: "Проверка данных" })).toBeVisible();

    const accessibility = await new AxeBuilder({ page })
      .include(".receivables__scroll-shell")
      .analyze();
    expect(accessibility.violations).toEqual([]);

    await page.screenshot({
      path: testInfo.outputPath(`receivables-folder-control-${viewport.name}.png`),
      fullPage: true,
    });
  });
}
