import AxeBuilder from "@axe-core/playwright";
import { expect, Page, test } from "@playwright/test";

const generatedAt = "2026-08-17T10:00:00Z";

const routes = [
  ["overview", "Overview"],
  ["estates", "Estates"],
  ["assessments", "Assessments"],
  ["waves", "Wave Manager"],
  ["runs", "Runs"],
  ["lineage", "Lineage"],
  ["reconciliation", "Reconciliation"],
  ["policies", "Policies & Approvals"],
  ["agents", "Agents"],
  ["evaluations", "Evaluations"],
  ["system-health", "System Health"],
] as const;

const fixtureByPath: Record<string, unknown> = {
  "/api/v1/adapter-types": [
    { adapter_type: "sqlserver", capabilities: ["discover", "health", "reconcile", "transfer"] },
    { adapter_type: "oracle_corpus", capabilities: ["discover"] },
  ],
  "/api/v1/overview": {
    fleet_health: "HEALTHY",
    estate: { objects: 58, pipelines: 4, sources: [{ source_id: "wwi-sqlserver", health: "HEALTHY" }] },
    runs: { migrated_percent: 100, complete: 1, active: 0, latest: { progress: { percent: 100, status: "complete", label: "Migration complete", current_stage: "COMPLETE", completed_units: 12, total_units: 12, run_id: "run-live", last_observed_at: generatedAt } } },
    waves: { queued_operations: 0 },
    policy_denials: 2,
    recovery_rate: 1,
    human_interventions: 1,
    estimated_cost: { status: "not_configured", reason: "Measured usage is unavailable." },
    actual_cost: { status: "not_configured", reason: "Billing export not configured." },
    estimated_bytes: { status: "not_configured", reason: "Job byte telemetry is unavailable." },
    latency: {},
  },
  "/api/v1/estates": [
    { estate_id: "wwi-demo-estate", display_name: "Worldwide Importers", status: "ACTIVE", owner: "Data Platform", health: "HEALTHY", objects: 58, pipelines: 4, pipeline_options: [{ pipeline_id: "wwi.sales.customers", name: "Customers" }], sources: [{ source_id: "wwi-sqlserver", adapter: "sqlserver", health: "HEALTHY", execution_profiles: ["wwi-default"] }], target: { system: "BigQuery", dataset_env: "Production" } },
    { estate_id: "retail-postgres-estate", display_name: "Retail PostgreSQL", status: "ACTIVE", objects: 12, pipelines: 1, sources: [{ source_id: "retail-postgres", adapter: "postgres", health: "HEALTHY" }], target: { system: "BigQuery" } },
  ],
  "/api/v1/assessments": { packs: [{ pack_id: "wwi", label: "WWI", execution_supported: true }], runs: [] },
  "/api/v1/waves": {
    state: { running_critical: [], running_by_source: {} },
    limits: { max_concurrent_critical: 1, max_concurrent_per_source: {}, backlog_age_escalation_minutes: 30, approval_window: { enabled: true } },
    oldest_backlog_age_ms: null,
    queued: [], blocked: [], overrides: [], events: [],
  },
  "/api/v1/runs": [{ run_id: "run-live", state: "COMPLETE", mode: "execution", updated_at: generatedAt, progress: { percent: 100, status: "complete", label: "Migration complete" } }],
  "/api/v1/lineage": { nodes: [{ id: "source.orders", label: "Orders", classification: "PII", confidence: 0.98 }], edges: [] },
  "/api/v1/reconciliation": [{ run_id: "run-live", status: "PASSED", delta: 0, tolerance: 0 }],
  "/api/v1/policies": { decisions: [{ policy_id: "cutover-approval", decision: "ALLOW", acting_identity: "approver@example.test" }], approvals: [] },
  "/api/v1/agents": { cards: [{ agent_id: "orchestrator", version: "1.0.0", status: "HEALTHY", owner: "Data Platform" }], pinned_run_counts: {} },
  "/api/v1/evaluations": { runs: [{ scenario: "full-migration", status: "PASSED" }], scale_metrics: null, scale_report_reason: "No persisted scale report." },
  "/api/v1/system-health": { build_version: "e2e", services: [{ service: "Cloud Run", status: "HEALTHY", freshness: generatedAt }] },
  "/api/v1/workers": {
    enabled: true,
    started_at: generatedAt,
    lease: { held: true, owner_id: "e2e-owner", holder: { hostname: "e2e", pid: 1, is_self: true }, standby_reason: null },
    consumers: [
      { name: "assessment", subscription: "assessment-requested-sub", state: "idle", last_message_at: generatedAt, processed_count: 3, error_count: 0, backlog: null },
      { name: "plan", subscription: "plan-created-sub", state: "paused", last_message_at: generatedAt, processed_count: 1, error_count: 0, backlog: null },
    ],
  },
  "/api/v1/search": [{ id: "run-live", kind: "run", title: "run-live", subtitle: "COMPLETE", route: "/runs/run-live" }],
  "/api/v1/notifications": [{ id: "run-pending", kind: "approval", severity: "info", title: "Cutover approval required", status: "READY_FOR_APPROVAL", route: "/runs/run-pending" }],
  "/api/v1/estate-validations": { status: "NOT_APPLICABLE", detail: "Static source" },
};

async function installApiFixtures(page: Page) {
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const data = url.pathname === "/api/v1/config"
      ? {
          product_name: "Migration Control Tower",
          build_version: "e2e",
          poll_interval_ms: 30_000,
          progress_poll_interval_ms: 2_000,
          environment: "Test",
          authentication_configured: true,
          firebase: {},
        }
      : url.pathname === "/api/v1/estate-validations"
        ? fixtureByPath[url.pathname]
        : url.pathname.startsWith("/api/v1/operations/")
        ? { operation_id: "op-e2e", status: "active", progress: { percent: 42, status: "active", label: "Analyzing dependencies", current_stage: "ANALYZED", completed_units: 2, total_units: 12, run_id: "run-e2e", last_observed_at: generatedAt } }
        : route.request().method() !== "GET"
          ? { operation_id: "op-e2e", status: "published" }
          : fixtureByPath[url.pathname] ?? [];
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data, meta: { generated_at: generatedAt, freshness: "live" } }),
    });
  });
}

for (const [route, title] of routes) {
  test(`${title} route renders and matches its desktop snapshot`, async ({ page }) => {
    await installApiFixtures(page);
    await page.setViewportSize({ width: 1440, height: 1000 });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`/${route}`);
    await expect(page.getByRole("heading", { name: title, exact: true })).toBeVisible();
    // Wait for the loading spinner to clear before capturing. The heading
    // renders before the data does, so a fullPage screenshot taken here
    // caught a short page that then grew — the baseline was 1000px tall
    // and a later run produced 1888px. It passed in isolation and failed
    // in a full run, which is the worst kind of visual test: it trains
    // people to re-run until green.
    await expect(page.locator(".spinner")).toHaveCount(0);
    await expect(page).toHaveScreenshot(`${route}-1440.png`, { fullPage: true });
    expect(errors).toEqual([]);
  });
}

for (const width of [1440, 1024, 768, 390]) {
  test(`responsive shell at ${width}px`, async ({ page }) => {
    await installApiFixtures(page);
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/overview");
    await expect(page.getByRole("heading", { name: "Overview", exact: true })).toBeVisible();
    const overflows = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    expect(overflows).toBe(false);
  });
}

test("keyboard navigation and WCAG audit", async ({ page }) => {
  await installApiFixtures(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/overview");

  // Press Tab THROUGH the body rather than via page.keyboard, and bring the
  // page to front first. page.keyboard delivers to whatever the browser
  // considers focused; with parallel workers sharing a machine the page is
  // often not the focused window, so the key went nowhere and :focus stayed
  // empty. That made this assertion pass serially and fail in parallel —
  // a flaky accessibility test is worse than none, because it trains people
  // to re-run until green.
  await page.bringToFront();

  // Wait for the app to finish hydrating BEFORE pressing anything. Tab can
  // only move focus to an element that exists; pressing while the shell was
  // still rendering left focus on <body> and :focus empty. That is why this
  // passed on an idle machine and failed when the backend suite was running
  // alongside it — the failure was load-sensitive, not random.
  const skipLink = page.getByRole("link", { name: /Skip to operational workspace/i });
  await expect(skipLink).toBeAttached();
  await expect(page.locator(".spinner")).toHaveCount(0);

  await page.locator("body").press("Tab");

  const focused = page.locator(":focus");
  await expect(focused).toBeVisible();
  // The first stop must be the skip link — keyboard users should reach the
  // workspace without tabbing through the whole command bar.
  await expect(focused).toHaveText(/Skip to operational workspace/i);
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
});

test("tables expose accessible controls and never encode status by color alone", async ({ page }) => {
  await installApiFixtures(page);
  await page.goto("/runs");
  await expect(page.getByRole("table")).toBeVisible();
  await expect(page.getByText("COMPLETE", { exact: true })).toBeVisible();
  await expect(page.locator(".status-pill .status-dot")).toHaveAttribute("aria-hidden", "true");
  await expect(page.locator("summary", { hasText: "Columns" })).toBeVisible();
  await page.getByPlaceholder("Filter runs").fill("does-not-exist");
  await expect(page.getByText(/No records match/)).toBeVisible();
  await page.getByPlaceholder("Filter runs").fill("");
  await page.getByRole("columnheader", { name: /Run/ }).getByRole("button").click();
  await page.locator("summary", { hasText: "Columns" }).click();
  await expect(page.getByLabel("Updated")).toBeVisible();
});

test("action dialogs close with Escape, preserve focus, and show measured operation progress", async ({ page }) => {
  await installApiFixtures(page);
  await page.goto("/assessments");
  const trigger = page.getByRole("button", { name: "Start assessment" });
  await trigger.click();
  await expect(page.getByRole("dialog", { name: "Start assessment" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Start assessment" })).toBeHidden();
  await expect(trigger).toBeFocused();
  await trigger.click();
  await page.getByLabel("Justification").fill("Browser click audit assessment request");
  await page.getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByText("42%", { exact: true })).toBeVisible();
  await expect(page.getByText("Analyzing dependencies")).toBeVisible();
  await expect(page.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "42");
});

test("responsive navigation drawer opens and navigates by click", async ({ page }) => {
  await installApiFixtures(page);
  await page.setViewportSize({ width: 768, height: 900 });
  await page.goto("/overview");
  const toggle = page.getByRole("button", { name: "Toggle navigation" });
  await toggle.click();
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Runs", exact: true })).toBeVisible();
});

test("command bar estate, search, notifications and account controls are clickable", async ({ page }) => {
  await installApiFixtures(page);
  await page.goto("/overview");
  const estate = page.getByLabel("Active estate");
  await expect(estate).toHaveValue("wwi-demo-estate");
  await estate.selectOption("retail-postgres-estate");
  await expect(page).toHaveURL(/estate_id=retail-postgres-estate/);

  const search = page.getByPlaceholder("Search");
  await search.fill("run-live");
  await expect(page.getByRole("dialog", { name: "Search commands" })).toBeVisible();
  await expect(page.getByText("run-live", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Search commands" })).toBeHidden();

  await page.getByRole("button", { name: "Notifications" }).click();
  await expect(page.getByRole("heading", { name: "Notifications" })).toBeVisible();
  await page.getByRole("button", { name: "Close inspector" }).click();

  await page.locator(".user-menu").click();
  await expect(page.getByRole("menuitem", { name: /Sign out/ })).toBeVisible();
  await page.getByRole("menuitem", { name: /Sign out/ }).click();
});

test("every primary navigation item and product home action works by click", async ({ page }) => {
  await installApiFixtures(page);
  await page.goto("/overview");
  const navNames: Record<string, string> = { waves: "Waves" };
  for (const [route, title] of routes.slice(1)) {
    await page.getByRole("button", { name: navNames[route] || title, exact: true }).click();
    await expect(page.getByRole("heading", { name: title, exact: true })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`/${route}\\?estate_id=`));
  }
  await page.getByRole("button", { name: "Migration Control Tower overview" }).click();
  await expect(page.getByRole("heading", { name: "Overview", exact: true })).toBeVisible();
});


// --- Estate onboarding wizard (Day 11 Phase 6) --------------------------

test("estate onboarding wizard walks its steps and never offers a password field", async ({ page }) => {
  await installApiFixtures(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));

  // A nested deep link. Every other route in this app is a single
  // segment, so this is the only test that would catch relative asset
  // paths resolving against /estates/ instead of the origin — the bug
  // that made this route serve index.html for its own JavaScript.
  await page.goto("/estates/new");
  await expect(page.getByRole("heading", { name: "Onboard an estate", exact: true })).toBeVisible();

  // Step 1 gates on a valid identity.
  const next = page.getByRole("button", { name: "Next", exact: true });
  await expect(next).toBeDisabled();
  await page.getByLabel("Estate ID").fill("acme-finance");
  await page.getByLabel("Display name").fill("ACME Finance");
  await expect(next).toBeEnabled();
  await next.click();

  // Step 2 lists adapters fetched from the API, not a hardcoded list.
  await expect(page.getByText("Source", { exact: true }).first()).toBeVisible();
  await page.getByLabel("Source ID").fill("finance-sqlserver");
  await page.getByLabel("Adapter").selectOption("sqlserver");
  await next.click();

  // Step 3 collects REFERENCES. This assertion is the point of the test.
  await expect(page.getByLabel("Password — Secret Manager reference")).toBeVisible();
  expect(await page.locator("input[type=password]").count()).toBe(0);

  expect(errors).toEqual([]);
});

test("an assessment-only adapter follows credential-free validation", async ({ page }) => {
  await installApiFixtures(page);
  await page.goto("/estates/new");
  await page.getByLabel("Estate ID").fill("corpus-estate");
  await page.getByLabel("Display name").fill("Corpus estate");
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await page.getByLabel("Source ID").fill("corpus");
  await page.getByLabel("Adapter").selectOption("oracle_corpus");

  // Declared capabilities drive a credential-free connection and
  // validation path; the limitation is stated rather than implied.
  await expect(page.getByText(/supports discovery only/)).toBeVisible();
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.getByText(/has no server to connect to/)).toBeVisible();
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await page.getByRole("button", { name: "Validate source" }).click();
  await expect(page.getByText("NOT_APPLICABLE")).toBeVisible();
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.getByText("Migration Pack", { exact: true }).first()).toBeVisible();
});


test("an operator can pause a consumer from System Health", async ({ page }) => {
  // The console has to be able to stop the fleet as well as watch it —
  // otherwise "everything is operated from the dashboard" is only true
  // while nothing is going wrong.
  const posted: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/api/v1/workers/")) {
      posted.push(new URL(request.url()).pathname);
    }
  });
  await installApiFixtures(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/system-health");

  // A paused consumer must offer Resume, not Pause: a button that says
  // the opposite of what it does is worse than no button.
  await expect(page.getByRole("button", { name: "Resume plan" })).toBeVisible();

  await page.getByRole("button", { name: "Pause assessment" }).click();
  const dialog = page.getByRole("dialog", { name: "Pause assessment" });
  await expect(dialog).toBeVisible();
  await dialog
    .getByLabel("Justification")
    .fill("Holding assessments while the source estate is patched.");
  await dialog.getByRole("button", { name: "Confirm" }).click();

  await expect.poll(() => posted).toContain("/api/v1/workers/assessment/pause");
});

test("the workers panel names the reason when nothing is consuming", async ({ page }) => {
  // "Queued but nothing is happening" was the confusion this whole change
  // exists to remove. An empty table would reproduce it exactly.
  await installApiFixtures(page);
  await page.route("**/api/v1/workers", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          enabled: false,
          reason: "CONTROL_TOWER_WORKERS is set to off for this process",
          lease: { held: false, standby_reason: null },
          consumers: [],
        },
        meta: { generated_at: generatedAt, freshness: "live" },
      }),
    });
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/system-health");
  await expect(
    page.getByText(/CONTROL_TOWER_WORKERS is set to off for this process/),
  ).toBeVisible();
});
