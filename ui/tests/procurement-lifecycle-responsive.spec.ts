import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const lifecycleQueue = {
  status: "working",
  scope: "action",
  total: 1,
  page: 1,
  page_size: 50,
  ready_count: 0,
  review_count: 1,
  blocked_count: 0,
  stale_count: 0,
  items: [{
    proposal_id: 77,
    nomenclature_code: "РБ000037607",
    nomenclature_ref: "ref-77",
    product_guid: "guid-77",
    product_name: "Дисплей без напарников в сегменте",
    folder: "Дисплеи",
    action_kind: "review",
    current_status: "working",
    current_status_label: "Рабочий",
    target_status: null,
    target_status_label: null,
    proposal_status: "pending",
    reason: "В сегменте нет второго доступного SKU",
    facts: { evidence: { family_member_count: 1 } },
    blockers: [],
    risk_codes: ["lifecycle_stage_not_exported"],
    run_id: 361,
    run_key: "display-run-361",
    facts_hash: "a".repeat(64),
    responsible_bitrix_user_id: "130757",
    responsible_name: "Омар",
    decision_state: "review",
    actionability: "manual_decision",
    suggested_manual_status: "pension",
    ready: false,
    selectable: false,
    stale: false,
    created_at: "2026-08-20T09:00:00",
  }],
};

const viewports = [
  { width: 1440, height: 1024 },
  { width: 1024, height: 900 },
  { width: 820, height: 900 },
  { width: 768, height: 900 },
  { width: 390, height: 844 },
];

test("lifecycle keeps the product, reason and action usable at target widths", async ({ page }, testInfo) => {
  await page.addInitScript(() => {
    window.sessionStorage.setItem("mm_procurement_order_formation_bitrix_session", JSON.stringify({
      session_token: "test-session-placeholder",
      expires_at: "2099-01-01T00:00:00Z",
      expires_in: 3600,
      cached_at: new Date().toISOString(),
      user: { user_id: "130757", name: "Омар" },
    }));
    window.sessionStorage.setItem("mm_procurement_order_formation_left_menu_v3_bound", "1");
  });
  await page.route("**/api/procurement-order-formation/lifecycle/transitions**", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(lifecycleQueue),
  }));

  await page.goto("/bitrix/procurement-order-formation/lifecycle/working?scope=action&readiness=review");
  await expect(page.getByText("Дисплей без напарников в сегменте")).toBeVisible();
  await expect(page.getByText("В сегменте нет второго доступного SKU")).toBeVisible();
  await expect(page.getByRole("button", { name: "Принять решение" })).toBeEnabled();

  for (const viewport of viewports) {
    await page.setViewportSize(viewport);
    const overflow = await page.evaluate(() => ({
      page: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      elements: Array.from(document.querySelectorAll<HTMLElement>("body *"))
        .filter((element) => {
          const rect = element.getBoundingClientRect();
          return rect.right > document.documentElement.clientWidth + 1 || rect.left < -1;
        })
        .slice(0, 8)
        .map((element) => `${element.tagName.toLowerCase()}.${element.className}:${Math.round(element.getBoundingClientRect().right)}`),
    }));
    expect(overflow.page, `page overflow at ${viewport.width}px: ${overflow.elements.join(", ")}`).toBe(false);
    if (viewport.width <= 1100) {
      await expect(page.locator(".lifecycle-queue__table thead")).toBeHidden();
      const cardWidth = await page.locator(".lifecycle-queue__table tbody tr").first().evaluate(
        (element) => element.getBoundingClientRect().width
      );
      expect(cardWidth).toBeLessThanOrEqual(viewport.width - 24);
    } else {
      await expect(page.locator(".lifecycle-queue__table thead")).toBeVisible();
    }
    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      .analyze();
    expect(results.violations).toEqual([]);
    await page.screenshot({
      path: testInfo.outputPath(`lifecycle-${viewport.width}.png`),
      fullPage: false,
    });
  }
});
