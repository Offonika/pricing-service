import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const devices = [
  {
    device_key: "critical-server",
    name: "Критичный сервер 1С",
    kind: "server",
    lifecycle_status: "active",
    health_status: "critical",
    connectivity_status: "offline",
    criticality: "critical",
    location: "Москва",
    purpose: ["1С КА и бухгалтерия"],
    technical_owner_ids: ["1"],
    technical_owners: ["Технический владелец"],
    business_owner: "Андрей Платонов",
    last_attempted_at: "2026-08-04T08:55:00Z",
    last_success_at: "2026-08-04T08:15:00Z",
    incident_started_at: "2026-08-04T08:20:00Z",
    outage_duration_seconds: 2700,
    availability_24h_pct: 96.8,
    availability_30d_pct: 99.4,
    monitoring_coverage_24h_pct: 100,
    monitoring_coverage_30d_pct: 99.8,
    metrics: { cpu_used_pct: 96, memory_used_pct: 88, disk_free_pct: 8 },
    services: [{
      service_key: "onec-ka",
      name: "1С КА",
      component_kind: "service",
      status: "critical",
      criticality: "critical",
      source_project: "1C_Dev_Workflow",
    }],
    backup: {
      status: "warning",
      protected_datastores: 2,
      unprotected_datastores: 1,
      lag_minutes: 180,
      off_host_verified: false,
      readback_verified: true,
    },
    integrations: { status: "warning", count: 1, last_success_at: "2026-08-04T08:10:00Z" },
    access: {
      status: "warning",
      active_grants: 4,
      pending_grants: 0,
      review_required_grants: 1,
      mfa_review_count: 1,
      unowned_credentials: 0,
      attention_grant_count: 1,
      next_review_at: "2026-08-15",
    },
    problems: [
      {
        problem_key: "connectivity:offline",
        category: "connectivity",
        severity: "critical",
        title: "Сервер не отвечает",
        evidence: ["Нет связи с 11:20", "Последний успех: 11:15"],
        started_at: "2026-08-04T08:20:00Z",
        recommended_action: "Проверить питание и разрешённый канал мониторинга",
      },
      {
        problem_key: "resources:cpu",
        category: "resources",
        severity: "critical",
        title: "Критическая загрузка CPU",
        evidence: ["CPU: 96%; критический порог: 95%"],
        started_at: "2026-08-04T08:30:00Z",
        recommended_action: "Проверить процессы с высокой нагрузкой",
      },
      {
        problem_key: "backup:gap",
        category: "backup",
        severity: "warning",
        title: "Не все базы защищены",
        evidence: ["Незащищённых хранилищ: 1"],
        recommended_action: "Добавить базу в резервное копирование",
      },
    ],
    issue: "Сервер не отвечает",
    recommended_action: "Проверить питание и разрешённый канал мониторинга",
  },
  {
    device_key: "warning-workstation",
    name: "Рабочая станция оператора",
    kind: "workstation",
    lifecycle_status: "active",
    health_status: "warning",
    connectivity_status: "online",
    criticality: "standard",
    location: "Склад",
    purpose: ["Рабочее место оператора"],
    technical_owner_ids: [],
    technical_owners: [],
    last_attempted_at: "2026-08-04T08:55:00Z",
    last_success_at: "2026-08-04T08:55:00Z",
    monitoring_coverage_24h_pct: 82,
    metrics: { memory_used_pct: 87 },
    services: [],
    backup: { status: "not_configured", protected_datastores: 0, unprotected_datastores: 0, off_host_verified: false, readback_verified: false },
    integrations: { status: "not_configured", count: 0 },
    access: { status: "ready", active_grants: 1, pending_grants: 0, review_required_grants: 0, mfa_review_count: 0, unowned_credentials: 0, attention_grant_count: 0 },
    problems: [{
      problem_key: "monitoring:coverage",
      category: "monitoring",
      severity: "warning",
      title: "Неполное покрытие мониторингом",
      evidence: ["Покрытие за 24 часа: 82%; требуется не менее 90%"],
      recommended_action: "Проверить регулярность диагностических циклов",
    }],
    issue: "Неполное покрытие мониторингом",
    recommended_action: "Проверить регулярность диагностических циклов",
  },
  {
    device_key: "healthy-device",
    name: "Исправное устройство",
    kind: "network",
    lifecycle_status: "active",
    health_status: "ready",
    connectivity_status: "online",
    criticality: "standard",
    location: "Офис",
    purpose: ["Сетевая инфраструктура"],
    technical_owner_ids: ["1"],
    technical_owners: ["Технический владелец"],
    monitoring_coverage_24h_pct: 100,
    metrics: {},
    services: [],
    backup: { status: "not_configured", protected_datastores: 0, unprotected_datastores: 0, off_host_verified: false, readback_verified: false },
    integrations: { status: "not_configured", count: 0 },
    access: { status: "ready", active_grants: 1, pending_grants: 0, review_required_grants: 0, mfa_review_count: 0, unowned_credentials: 0, attention_grant_count: 0 },
    problems: [],
  },
];

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => window.sessionStorage.setItem("mm_executive_dashboard_bitrix_session", JSON.stringify({
    session_token: "test-session-placeholder",
    expires_at: "2099-01-01T00:00:00Z",
    expires_in: 3600,
    cached_at: new Date().toISOString(),
    user: { user_id: "1", name: "Андрей Платонов" },
    access_level: "domain",
    roles: ["operations_director"],
    allowed_blocks: ["infrastructure"],
    allowed_action_domains: [],
  })));
  await page.route("**/api/management/executive-dashboard**", (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/instruments")) {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        schema_version: 3,
        generated_at: "2026-08-04T09:00:00Z",
        source_status: "partial",
        freshness_status: "fresh",
        summary: {
          total_count: 3,
          online_count: 2,
          critical_count: 1,
          warning_count: 1,
          not_monitored_count: 0,
          backup_gap_count: 1,
          access_review_count: 1,
          monitoring_coverage_24h_pct: 94,
        },
        devices,
        warnings: ["Для одного устройства владелец ещё не назначен."],
        capabilities: { access_governance: "read_only", access_mutations: false, network_scanning: false },
      }) });
    }
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      as_of: "2026-08-04",
      generated_at: "2026-08-04T09:00:00Z",
      freshness_status: "fresh",
      source_status: "ready",
      access_level: "domain",
      roles: ["operations_director"],
      allowed_blocks: ["infrastructure"],
      allowed_action_domains: [],
      blocks: [],
      source_freshness: [],
      top_actions: [],
      summary: {},
    }) });
  });
});

test("Приборы: desktop, drawer, URL и возврат фокуса", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/bitrix/executive-dashboard/?tab=infrastructure&date=2026-08-04");

  await expect(page.getByRole("heading", { name: "Серверы и устройства" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Требуют внимания\s*2/ })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByLabel("Список приборов").getByText("Исправное устройство")).toHaveCount(0);
  await expect(page.getByLabel("Список приборов").getByText("Сервер не отвечает")).toBeVisible();

  const detailsButton = page.getByRole("button", { name: "Подробнее: Критичный сервер 1С" });
  await detailsButton.click();
  await expect(page).toHaveURL(/device=critical-server/);
  await expect(page.getByRole("dialog", { name: "Критичный сервер 1С" })).toBeVisible();
  await expect(page.getByText("CPU: 96%; критический порог: 95%")).toBeVisible();
  await expect(page.getByText(/Выдача и отзыв доступов отключены/)).toBeVisible();

  await page.goBack();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(detailsButton).toBeFocused();

  await page.goForward();
  await expect(page.getByRole("dialog", { name: "Критичный сервер 1С" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);

  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(results.violations).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("instruments-desktop-1440.png"), fullPage: true });
});

test("Приборы: mobile cards и полноэкранная панель без overflow", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/bitrix/executive-dashboard/?tab=infrastructure&date=2026-08-04");

  await expect(page.getByLabel("Карточки приборов")).toBeVisible();
  await expect(page.getByLabel("Список приборов")).toBeHidden();
  await page.getByRole("button", { name: "Подробнее: Критичный сервер 1С" }).click();
  const drawer = page.getByRole("dialog", { name: "Критичный сервер 1С" });
  await expect(drawer).toBeVisible();
  expect((await drawer.boundingBox())?.width).toBe(390);

  const hasOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  expect(hasOverflow).toBe(false);
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(results.violations).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("instruments-mobile-390.png"), fullPage: true });
});
