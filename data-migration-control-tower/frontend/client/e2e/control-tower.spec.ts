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
  ["incidents", "Incidents"],
  ["dead-letters", "Dead letters"],
  ["memory", "Memory Bank"],
  ["approvals", "Approvals"],
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
    runs: {
      migrated_percent: 100, complete: 1, active: 0,
      latest: {
        run_id: "run-live",
        // The real /overview returns the whole latest run document, so the
        // orchestration map reads state and state_history straight off it.
        // This run recovered from the seeded row-loss defect, which is the
        // path worth having in a baseline.
        state: "MONITORING",
        state_history: [
          "REQUESTED", "DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED", "MIGRATING",
          "VALIDATING", "FAILED", "INVESTIGATING", "REMEDIATING", "VALIDATING", "PASSED",
          "READY_FOR_APPROVAL", "APPROVED", "CUTOVER", "MONITORING",
        ].map((state, index) => ({ state, at: `2026-08-17T09:${String(index).padStart(2, "0")}:00Z` })),
        progress: { percent: 100, status: "complete", label: "Migration complete", current_stage: "COMPLETE", completed_units: 12, total_units: 12, run_id: "run-live", last_observed_at: generatedAt },
      },
    },
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
    { estate_id: "wwi-demo-estate", display_name: "Worldwide Importers", status: "ACTIVE", owner: "Data Platform", health: "HEALTHY", objects: 58, pipelines: 4, pipeline_options: [{ pipeline_id: "wwi.sales.customers", name: "Customers" }], sources: [{ source_id: "wwi-sqlserver", adapter: "sqlserver", health: "HEALTHY", pack_id: "wwi_sqlserver_v1" }], execution_readiness: { status: "ready", options: [{ source_id: "wwi-sqlserver", pack_id: "wwi_sqlserver_v1", label: "wwi-sqlserver · WWI SQL Server" }], blockers: [] }, target: { system: "BigQuery", dataset_env: "Production" } },
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
  "/api/v1/lineage": {
    // The endpoint now reports WHICH run it drew, and offers the runs that
    // actually have a catalog — a queued run has none.
    run_id: "run-live",
    // Mirrors the real shape: a few connected assets, one endpoint the
    // catalog does not contain, and a majority with no relationships.
    nodes: [
      { id: "Sales.Orders", label: "Sales.Orders", type: "table", classification: "METADATA" },
      { id: "Sales.Customers", label: "Sales.Customers", type: "table", classification: "PII" },
      { id: "Sales.V_ACCOUNT_SUMMARY", label: "Sales.V_ACCOUNT_SUMMARY", type: "table", classification: "METADATA" },
      { id: "Application.People", label: "Application.People", type: "table", classification: "PII" },
      { id: "Application.Cities", label: "Application.Cities", type: "table", classification: "METADATA" },
      { id: "Warehouse.ColdRoomTemperatures", label: "Warehouse.ColdRoomTemperatures", type: "table", classification: "METADATA" },
      { id: "Purchasing.Suppliers", label: "Purchasing.Suppliers", type: "table", classification: "PII" },
    ],
    edges: [
      { from: "Sales.Orders", to: "Sales.V_ACCOUNT_SUMMARY", relationship: "reads", confidence: 0.85, source: "sql_view_parse" },
      { from: "Sales.Customers", to: "Sales.V_ACCOUNT_SUMMARY", relationship: "reads", confidence: 0.85, source: "sql_view_parse" },
      { from: "Application.People", to: "wwi.sales.customers", relationship: "reads", confidence: 1, source: "dag_reference" },
    ],
    available_runs: [
      { run_id: "run-live", state: "COMPLETE", created_at: generatedAt },
      { run_id: "run-older", state: "PASSED", created_at: generatedAt },
    ],
  },
  "/api/v1/reconciliation": [{ run_id: "run-live", status: "PASSED", delta: 0, tolerance: 0 }],
  "/api/v1/policies": { decisions: [{ policy_id: "cutover-approval", decision: "ALLOW", acting_identity: "approver@example.test" }], approvals: [] },
  // Real registry shape: the ids the seeds actually publish, and APPROVED,
  // which is what the registry emits. The previous fixture used an
  // "orchestrator" agent that is not in the registry at all and a HEALTHY
  // status that no card ever carries, so it exercised a state the console
  // cannot encounter.
  "/api/v1/agents": {
    cards: [
      { agent_id: "discovery-agent", version: "1.0.0", status: "APPROVED", owner: "Data Platform", model: "gemini-2.0-flash", framework: "adk", capabilities: ["discovery.catalog.estate"] },
      { agent_id: "discovery-agent", version: "1.1.0", status: "APPROVED", owner: "Data Platform", model: "gemini-2.0-flash", framework: "adk", capabilities: ["discovery.catalog.estate"] },
      { agent_id: "lineage-agent", version: "1.0.0", status: "APPROVED", owner: "Data Platform", model: "gemini-2.0-flash", framework: "adk", capabilities: ["lineage.graph.build"] },
      { agent_id: "risk-agent", version: "1.0.0", status: "APPROVED", owner: "Data Platform", model: "gemini-2.0-flash", framework: "adk", capabilities: ["risk.assess.estate"] },
      { agent_id: "planner-agent", version: "1.0.0", status: "APPROVED", owner: "Data Platform", model: "gemini-2.0-flash", framework: "adk", capabilities: ["planner.plan.propose"] },
      { agent_id: "validation-agent", version: "1.0.0", status: "APPROVED", owner: "Data Platform", model: "gemini-2.0-flash", framework: "adk", capabilities: ["validation.reconcile.source_target"] },
      { agent_id: "cutover-agent", version: "1.0.0", status: "APPROVED", owner: "Data Platform", model: "gemini-2.0-flash", framework: "adk", capabilities: ["cutover.request_approval"] },
      { agent_id: "finance-impact-agent", version: "1.0.0", status: "APPROVED", owner: "Finance Systems", model: "gemini-2.0-flash", framework: "adk", capabilities: ["impact.assessment.finance_reporting"] },
    ],
    pinned_run_counts: { "discovery-agent": 3, "risk-agent": 1 },
  },
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
  "/api/v1/approvals": {
    stale_bindings: 1,
    awaiting: [
      {
        run_id: "run-pending", estate_id: "wwi-demo-estate", run_state: "READY_FOR_APPROVAL",
        status: "PENDING", requested_by: "cutover-agent", requested_at: generatedAt,
        approved_plan_hash: "abc123def456", current_plan_hash: "abc123def456", binding: "intact",
        checks_total: 5, checks_failed: 0, risk_findings: 3, critical_findings: 0,
        expires_at: null, expired: false, route: "/runs/run-pending",
      },
      {
        // The case the screen exists for: approved against a plan that has
        // since moved on, so cutover would be refused.
        run_id: "run-stale", estate_id: "wwi-demo-estate", run_state: "READY_FOR_APPROVAL",
        status: "PENDING", requested_by: "cutover-agent", requested_at: generatedAt,
        approved_plan_hash: "abc123def456", current_plan_hash: "999999999999", binding: "stale",
        checks_total: 5, checks_failed: 1, risk_findings: 4, critical_findings: 1,
        expires_at: null, expired: false, route: "/runs/run-stale",
      },
    ],
    decided: [
      {
        run_id: "run-live", estate_id: "wwi-demo-estate", run_state: "COMPLETE",
        status: "APPROVED", requested_by: "cutover-agent", requested_at: generatedAt,
        approved_by: "approver@example.test", approved_at: generatedAt,
        justification: "Reconciliation passed on all five checks.",
        token_id: "tok-1", approved_plan_hash: "abc123def456",
        current_plan_hash: "abc123def456", binding: "intact",
        checks_total: 5, checks_failed: 0, risk_findings: 3, critical_findings: 0,
        expires_at: "2026-09-16T10:00:00Z", expired: false, route: "/runs/run-live",
      },
    ],
  },
  "/api/v1/memory-bank": {
    reused_facts: 1,
    facts: [
      {
        signature: "row_loss:Sales.Customers",
        root_cause: "The extract dropped 7 of 663 rows before load.",
        fix: "Reloaded Sales.Customers from source and re-validated.",
        recalled_by_count: 16, confirmations: 17,
        recalled_by_run_ids: ["run-a"], source_run_ids: ["run-0"],
        first_learned_at: generatedAt, last_confirmed_at: generatedAt,
      },
      {
        signature: "schema_drift:Sales.Orders",
        root_cause: "A column was added at source after planning.",
        fix: "Re-derived the migration plan.",
        recalled_by_count: 0, confirmations: 1,
        recalled_by_run_ids: [], source_run_ids: ["run-1"],
        first_learned_at: generatedAt, last_confirmed_at: generatedAt,
      },
    ],
  },
  "/api/v1/incidents": {
    open_count: 1,
    incidents: [
      {
        incident_id: "inc-1", run_id: "run-live", signature: "row_loss:Sales.Customers",
        table_ref: "Sales.Customers", outcome: "RESOLVED",
        root_cause: "The extract dropped 7 of 663 rows before load.",
        explained_by: "deterministic", fix: "Reloaded Sales.Customers from source.",
        opened_at: generatedAt, run_state: "COMPLETE", memory_refs: ["incident/row_loss:Sales.Customers"],
        route: "/runs/run-live",
      },
    ],
    policy_denials: [
      { run_id: "run-live", agent_id: "risk-agent", action: "read_raw_pii", resource_class: "PII", decided_at: generatedAt },
    ],
  },
  "/api/v1/dead-letters": {
    pending: [
      {
        message_id: "21367188131118311", source_subscription: "plan-created-sub",
        delivery_attempts: 10, published_at: generatedAt, run_id: "run-stuck",
        payload: { run_id: "run-stuck" }, attributes: {},
      },
    ],
    archive: [
      {
        message_id: "9900", event: "replayed", source_subscription: "assessment-requested-sub",
        run_id: "run-old", actor: "operator@example.test",
        justification: "Transient Firestore outage during the first attempt.",
        recorded_at: generatedAt,
      },
    ],
  },
};

/** Per-path deviation from the default fixtures, for a single test. */
type FixtureOverride = {
  /** Replaces the `data` envelope body. */
  body?: unknown;
  /** Holds the response open, to make a transient UI state observable. */
  delayMs?: number;
};

/**
 * Installs the API fixtures, optionally deviating on specific paths.
 *
 * Overrides go THROUGH this handler rather than through a second
 * `page.route`, because Playwright matches routes last-registered-first:
 * a test that registers its own route before calling this one is silently
 * overridden by the catch-all below, the page never reaches the state the
 * test was written for, and the assertions fail somewhere unrelated. That
 * cost a real misdiagnosis — an empty element was read as a bundling bug
 * when the page had simply already loaded. One handler, one order, no
 * trap.
 */
async function installApiFixtures(
  page: Page,
  overrides: Record<string, FixtureOverride> = {},
) {
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const override = overrides[url.pathname];
    if (override?.delayMs) {
      await new Promise((resolve) => setTimeout(resolve, override.delayMs));
    }
    if (override && "body" in override) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          data: override.body,
          meta: { generated_at: generatedAt, freshness: "live" },
        }),
      });
      return;
    }
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

test("loads the local JET 20.1.3 Redwood theme without a CSS compatibility warning", async ({ page }) => {
  await installApiFixtures(page);
  const compatibilityWarnings: string[] = [];
  page.on("console", (message) => {
    if (/theme.*(incompatible|version)|incompatible.*css/i.test(message.text())) {
      compatibilityWarnings.push(message.text());
    }
  });
  await page.goto("/overview");
  await expect(page.locator('link[href="/styles/redwood/20.1.3/web/redwood.min.css"]')).toHaveCount(1);
  await expect(page.getByRole("heading", { name: "Overview", exact: true })).toBeVisible();
  expect(compatibilityWarnings).toEqual([]);
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
  await installApiFixtures(page, {
    "/api/v1/workers": {
      body: {
        enabled: false,
        reason: "CONTROL_TOWER_WORKERS is set to off for this process",
        lease: { held: false, standby_reason: null },
        consumers: [],
      },
    },
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/system-health");
  await expect(
    page.getByText(/CONTROL_TOWER_WORKERS is set to off for this process/),
  ).toBeVisible();
});


test("the brand identity is actually applied, not just present in the CSS", async ({ page }) => {
  // Deliberately a computed-style assertion rather than a screenshot.
  //
  // The snapshot suite CANNOT catch this. Playwright compares pixels with
  // a perceptual `threshold` (default 0.2, YIQ space), and the old command
  // bar (#252321) and the brand navy (#0b2545) are both dark enough that
  // every pixel compares as unchanged — the whole 1440x48 bar can be
  // repainted and `toHaveScreenshot` still passes. Verified: the stored
  // baselines kept the old bar colour while the live page rendered navy,
  // and every snapshot test stayed green.
  await installApiFixtures(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/overview");
  await expect(page.getByRole("heading", { name: "Overview", exact: true })).toBeVisible();

  const bar = page.locator(".command-bar");
  await expect(bar).toHaveCSS("background-color", "rgb(11, 37, 69)");

  // The logo must have actually decoded. A wrong path does not 404 here:
  // publicPath is "/" and the SPA catch-all answers 200 with index.html,
  // so a typo renders a broken image with a clean network log.
  const logo = page.locator("img.brand-mark").first();
  await expect(logo).toHaveAttribute("src", /^\/assets\/brand\//);
  expect(
    await logo.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0),
  ).toBe(true);
});


test("the agent fleet renders one identified card per agent", async ({ page }) => {
  // The fleet story — seven specialists resolved by capability — was
  // previously invisible: every agent was an identical table row.
  await installApiFixtures(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/agents");
  await expect(page.getByRole("heading", { name: "Agents", exact: true })).toBeVisible();

  const fleet = page.locator(".agent-grid .agent-card");
  await expect(fleet).toHaveCount(7);

  // Discovery is seeded at two approved versions; it is one agent.
  await expect(page.getByText("Discovery", { exact: true })).toHaveCount(1);
  await expect(page.getByText("v1.1.0", { exact: false }).first()).toBeVisible();

  // Every icon must have decoded — a wrong path returns index.html with a
  // 200, so a broken icon leaves no trace in the network log.
  const undecoded = await page.locator(".agent-grid img.agent-badge-art").evaluateAll(
    (images) => images.filter((img: HTMLImageElement) => !img.complete || img.naturalWidth === 0).length,
  );
  expect(undecoded).toBe(0);
});


test("the orchestration map reports each agent's state from the run's own history", async ({ page }) => {
  await installApiFixtures(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/overview");
  await expect(page.getByRole("heading", { name: "Overview", exact: true })).toBeVisible();

  const map = page.locator(".orchestration-map");
  await expect(map).toBeVisible();
  await expect(map.locator(".orchestration-stage")).toHaveCount(7);

  // The fixture run recovered from the seeded defect and is at MONITORING:
  // validation PASSED is in its history, so Validation reads complete
  // rather than failed, and only Cutover is still working.
  await expect(map.locator(".orchestration-stage.is-failed")).toHaveCount(0);
  await expect(map.locator(".orchestration-stage.is-active")).toHaveCount(1);
  await expect(map.locator(".orchestration-stage.is-complete")).toHaveCount(6);
});


test("a dead letter names the consumer that gave up and can be replayed", async ({ page }) => {
  // Before this screen the payload was reachable only by running gcloud,
  // and the only symptom was a consumer stuck in `error`.
  const posted: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/api/v1/dead-letters/")) {
      posted.push(new URL(request.url()).pathname);
    }
  });
  await installApiFixtures(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/dead-letters");
  await expect(page.getByRole("heading", { name: "Dead letters", exact: true })).toBeVisible();

  // Which consumer stopped trying, and after how many attempts.
  await expect(page.getByText("plan-created-sub").first()).toBeVisible();
  await expect(page.getByText("10").first()).toBeVisible();

  await page.getByRole("button", { name: /^Replay / }).click();
  const dialog = page.getByRole("dialog", { name: /^Replay / });
  await dialog.getByLabel("Justification").fill("The source estate is reachable again.");
  await dialog.getByRole("button", { name: "Confirm" }).click();

  await expect.poll(() => posted.join(",")).toContain("/replay");
});

test("the incident workspace shows the canonical root cause and the fix", async ({ page }) => {
  await installApiFixtures(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/incidents");
  await expect(page.getByRole("heading", { name: "Incidents", exact: true })).toBeVisible();
  await expect(page.getByText("row_loss:Sales.Customers")).toBeVisible();
  await expect(page.getByText(/dropped 7 of 663 rows/)).toBeVisible();
  // The policy engine's refusals belong beside the failures they explain.
  await expect(page.getByText("read_raw_pii")).toBeVisible();
});


test("the Memory Bank distinguishes reuse from re-confirmation", async ({ page }) => {
  // 16 later runs cited this fact; it was re-confirmed 17 times. Showing
  // the larger number as "reused" would overstate what was proved.
  await installApiFixtures(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/memory");
  await expect(page.getByRole("heading", { name: "Memory Bank", exact: true })).toBeVisible();

  await expect(page.getByText("row_loss:Sales.Customers")).toBeVisible();
  await expect(page.getByText("16", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("17", { exact: true }).first()).toBeVisible();
  // The page states the limit of what it is, rather than implying semantic recall.
  await expect(page.getByText(/not semantic similarity/)).toBeVisible();
});


test("an approval bound to a changed plan is flagged before cutover", async ({ page }) => {
  // approval_service.consume() already refuses this — but at cutover time,
  // long after a human clicked approve. Saying so up front is the point.
  await installApiFixtures(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/approvals");
  await expect(page.getByRole("heading", { name: "Approvals", exact: true })).toBeVisible();

  await expect(page.getByText(/Cutover will be REFUSED until re-approved/).first()).toBeVisible();
  await expect(page.getByText(/bound to a plan that has since/)).toBeVisible();

  // And it states plainly that it cannot itself approve anything.
  await expect(page.getByText(/It cannot approve anything/)).toBeVisible();
});


test("the loading screen holds attention with facts, not invented progress", async ({ page }) => {
  // Captured by holding the API open, because the state is otherwise too
  // brief to inspect — which is also why it was never designed.
  await installApiFixtures(page, {
    "/api/v1/incidents": {
      // Held open so the loading state is observable at all; it is
      // otherwise too brief to inspect, which is why it had never been
      // designed.
      delayMs: 4000,
      body: { incidents: [], policy_denials: [], open_count: 0 },
    },
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/incidents");

  const fact = page.locator(".loading-fact");
  await expect(fact).toBeVisible();
  await expect(page.locator(".loading-fleet img")).toHaveCount(7);

  // Whatever it says must be one of the vetted true statements — not a
  // percentage and not fabricated activity.
  const text = (await fact.textContent())?.trim() || "";
  expect(text.length).toBeGreaterThan(20);
  expect(text).not.toMatch(/\d+\s*%/);

  // The fleet, the message and the fact must share one centre line — they
  // were three different centres, because the progress bar's own width
  // pushed the message off to the right of the other two.
  const centre = async (selector: string) => {
    const box = await page.locator(selector).first().boundingBox();
    return box ? Math.round(box.x + box.width / 2) : -1;
  };
  const [fleetC, statusC, factC] = await Promise.all([
    centre(".loading-fleet"),
    centre(".loading-status span"),
    centre(".loading-fact"),
  ]);
  expect(Math.abs(fleetC - statusC)).toBeLessThanOrEqual(2);
  expect(Math.abs(fleetC - factC)).toBeLessThanOrEqual(2);

  await page.screenshot({ path: "test-results/loading-state.png" });
});


test("lineage names the run it drew and lets an operator pick another", async ({ page }) => {
  // It used to draw the newest run FULL STOP, which is normally queued and
  // has no catalog — so the graph came up empty and read as broken.
  await installApiFixtures(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/lineage");
  await expect(page.getByRole("heading", { name: "Lineage", exact: true })).toBeVisible();

  // The run is stated, not implied.
  await expect(page.getByText(/run run-live/)).toBeVisible();

  // And only runs that have a catalog are offered.
  const picker = page.getByLabel("Lineage run");
  await expect(picker).toBeVisible();
  await expect(picker.locator("option")).toHaveCount(2);
});


test("lineage draws relationships instead of a grid of identical cards", async ({ page }) => {
  // The page used to render every catalogued asset as an equal-sized card
  // and print "N relationships" without drawing one of them.
  await installApiFixtures(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/lineage");
  await expect(page.getByRole("heading", { name: "Lineage", exact: true })).toBeVisible();

  // One drawn path per relationship.
  await expect(page.locator("path.lineage-edge")).toHaveCount(3);

  // The endpoint the catalog does not contain is shown, not dropped.
  await expect(page.locator(".lineage-g-node.is-unresolved")).toHaveCount(1);

  // Assets with no relationships are chips, not graph nodes.
  await expect(page.locator(".asset-chip").first()).toBeVisible();

  // The graph is an image, so it carries a text alternative.
  await expect(page.getByRole("img", { name: /relationship register/i })).toBeVisible();
});


test("the lifecycle progress bar actually fills to its value", async ({ page }) => {
  await installApiFixtures(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/overview");
  await expect(page.getByRole("heading", { name: "Overview", exact: true })).toBeVisible();

  // The bar's own value was never wrong — it sized its fill element
  // correctly all along. Both the track and the fill drew with
  // `background-color: transparent`, because oj-c's stylesheet colours
  // them from --oj-c-measure-* tokens that the shipped Redwood 20.1.3 CSS
  // does not define, and an undefined custom property resolves to
  // nothing. So this asserts PAINT, not value: a bar that reports 100%
  // and renders invisibly is the exact bug being guarded.
  // Wait for the custom element to upgrade and lay out. Measured before
  // that, its children are 0x0 with an empty computed background, which
  // is indistinguishable from the unpainted bug this test exists to catch.
  const bar = page.locator("oj-c-progress-bar").first();
  await expect(bar).toBeVisible();
  await expect
    .poll(async () => (await bar.boundingBox())?.width ?? 0, { timeout: 10_000 })
    .toBeGreaterThan(10);

  const painted = await bar.evaluate((el: any) =>
    Array.from(el.querySelectorAll("*")).map((child: any) => {
      const rect = child.getBoundingClientRect();
      return {
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        background: getComputedStyle(child).backgroundColor,
      };
    }),
  );

  // Not "something in the bar is painted" — the TRACK alone would satisfy
  // that, and a grey track at 100% is indistinguishable from the bug. The
  // fill is the innermost element and is the one that has to carry the
  // brand blue, at full width because this run's progress is 100%.
  const fill = painted[painted.length - 1];
  expect(fill, `the bar has no fill element: ${JSON.stringify(painted)}`).toBeTruthy();
  expect(fill.background, `the fill is not painted: ${JSON.stringify(painted)}`).toBe("rgb(29, 111, 208)");
  expect(fill.width).toBeGreaterThan(200);

  // The screenshot baselines cannot be trusted to catch this on their own:
  // a 6px-tall band across a full-page capture is ~0.003 of the pixels,
  // comfortably inside `maxDiffPixelRatio: 0.01`. The bar could go fully
  // transparent again and every snapshot would stay green.
  const track = painted[painted.length - 2];
  expect(track.background).not.toBe(fill.background);
});

