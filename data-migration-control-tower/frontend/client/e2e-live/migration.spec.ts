import { expect, Page, test } from "@playwright/test";

const email = process.env.CONTROL_TOWER_E2E_EMAIL;
const password = process.env.CONTROL_TOWER_E2E_PASSWORD;
const estateId = process.env.CONTROL_TOWER_E2E_ESTATE_ID;

function required(name: string, value: string | undefined): string {
  if (!value) throw new Error(`${name} is required for the separately labelled live smoke test.`);
  return value;
}

async function next(page: Page) {
  const button = page.getByRole("button", { name: "Next", exact: true });
  await expect(button).toBeEnabled();
  await button.click();
}

async function reloadUntil(
  page: Page,
  ready: () => Promise<boolean>,
  description: string,
  estateId?: string,
  timeoutMs = 8 * 60_000,
) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await ready()) return;
    await page.waitForTimeout(3_000);
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator(".spinner")).toHaveCount(0, { timeout: 30_000 });
    if (estateId) {
      await expect(page.getByLabel("Active estate")).toHaveValue(estateId, {
        timeout: 60_000,
      });
    }
  }
  throw new Error(`Timed out waiting for ${description}.`);
}

function dataTable(page: Page, label: string) {
  return page
    .getByLabel(`${label} table, horizontally scrollable`)
    .locator("table");
}

async function generateAndDownloadReport(page: Page): Promise<string[]> {
  const generate = page.getByRole("button", { name: "Generate report" });
  await expect(generate).toBeVisible({ timeout: 30_000 });
  await generate.click();
  await expect(page.getByRole("button", { name: "Print" })).toBeVisible({ timeout: 90_000 });
  const hashes: string[] = [];
  for (const [label, format] of [["Download PDF", "pdf"], ["Download JSON", "json"]] as const) {
    const observed: string[] = [];
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const [download, response] = await Promise.all([
        page.waitForEvent("download"),
        page.waitForResponse((candidate) => candidate.url().includes("/download") && candidate.url().includes(`format=${format}`)),
        page.getByRole("button", { name: label }).click(),
      ]);
      expect(await download.failure()).toBeNull();
      const digest = await response.headerValue("x-content-sha256");
      expect(digest).toMatch(/^[a-f0-9]{64}$/);
      observed.push(digest!);
    }
    expect(observed[1]).toBe(observed[0]);
    hashes.push(observed[0]);
  }
  return hashes;
}

test("LIVE: estate → assessment → pack migration → approval → COMPLETE", async ({ page }) => {
  const liveEmail = required("CONTROL_TOWER_E2E_EMAIL", email);
  const livePassword = required("CONTROL_TOWER_E2E_PASSWORD", password);
  const liveEstate = required("CONTROL_TOWER_E2E_ESTATE_ID", estateId);
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(error.message));

  await page.goto("/overview");
  await page.getByRole("button", { name: /Sign in with domain email/ }).click();
  await page.getByLabel("Domain email address").fill(liveEmail);
  await page.getByLabel("Password", { exact: true }).fill(livePassword);
  await page.getByRole("button", { name: /Sign in with email/ }).click();
  await expect(page.getByRole("heading", { name: "Overview", exact: true })).toBeVisible({
    timeout: 30_000,
  });

  await page.getByRole("button", { name: "System Health", exact: true }).click();
  const consumers = page
    .getByLabel("Consumers table, horizontally scrollable")
    .locator("table");
  await expect(consumers.getByRole("row")).toHaveCount(10, { timeout: 30_000 });
  await expect(consumers.getByRole("cell", { name: "cutover", exact: true })).toBeVisible();
  await expect(consumers.getByText("ERROR", { exact: true })).toHaveCount(0);
  await expect(consumers.getByText("PAUSED", { exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: "Estates", exact: true }).click();
  await page.getByRole("button", { name: "Onboard estate" }).click();
  await page.getByLabel("Estate ID").fill(liveEstate);
  await page.getByLabel("Display name").fill(`Live acceptance ${liveEstate}`);
  await next(page);
  await page.getByLabel("Source ID").fill("acceptance-sqlserver");
  await page.getByLabel("Adapter").selectOption("sqlserver");
  await page.getByLabel("Database").fill("WideWorldImporters");
  await next(page);
  await next(page); // default environment-variable references are intentional
  await page.getByRole("button", { name: "Validate connection" }).click();
  await expect(page.getByText("HEALTHY", { exact: true })).toBeVisible({ timeout: 30_000 });
  await next(page);
  await page.getByLabel("Pack").selectOption("wwi_sqlserver_v1");
  await next(page);
  await page.getByLabel("Justification").fill("Live pack-driven migration acceptance evidence");
  await page.getByRole("button", { name: "Create estate" }).click();
  await expect(page.getByText(new RegExp(`Estate ${liveEstate} created`))).toBeVisible({
    timeout: 30_000,
  });

  await page.getByRole("button", { name: "Start assessment" }).click();
  await page.getByRole("dialog", { name: "Start assessment" }).getByLabel("Justification").fill(
    "Assess the registered estate before migration",
  );
  await page.getByRole("dialog", { name: "Start assessment" }).getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByText("Operation accepted and queued.")).toBeVisible({ timeout: 45_000 });
  await page.keyboard.press("Escape");
  await page.evaluate((id) => localStorage.setItem("mct.activeEstate", id), liveEstate);
  await page.goto(`/assessments?estate_id=${encodeURIComponent(liveEstate)}`);
  await expect(page.getByRole("heading", { name: "Assessments", exact: true })).toBeVisible();
  await expect(page.getByLabel("Active estate")).toHaveValue(liveEstate, { timeout: 60_000 });
  await reloadUntil(
    page,
    async () => (await page.locator(".status-pill").filter({ hasText: "PLANNED" }).count()) > 0,
    "the assessment to reach PLANNED",
    liveEstate,
  );

  await page.goto(`/runs?estate_id=${encodeURIComponent(liveEstate)}`);
  await expect(page.getByRole("heading", { name: "Runs", exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByLabel("Active estate")).toHaveValue(liveEstate, { timeout: 60_000 });
  const startMigration = page.getByRole("button", { name: "Start migration" });
  await expect(startMigration).toBeEnabled();
  await startMigration.click();
  await page.getByRole("dialog", { name: "Start migration" }).getByLabel("Justification").fill(
    "Execute the live pack-driven migration",
  );
  await page.getByRole("dialog", { name: "Start migration" }).getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByText("Operation accepted and queued.")).toBeVisible({ timeout: 45_000 });
  await page.keyboard.press("Escape");

  await reloadUntil(
    page,
    async () => (await page.locator(".status-pill").filter({ hasText: "READY FOR APPROVAL" }).count()) > 0,
    "the execution run to reach READY_FOR_APPROVAL",
    liveEstate,
  );
  const readyRow = page.getByRole("row").filter({ hasText: "READY FOR APPROVAL" }).first();
  await readyRow.click();
  await expect(page.getByRole("button", { name: "Approve cutover" })).toBeVisible();
  const runId = new URL(page.url()).pathname.split("/").at(-1);
  expect(runId).toMatch(/^run_/);

  await page.getByRole("button", { name: "Approve cutover" }).click();
  await page.getByRole("dialog", { name: "Approve cutover" }).getByLabel("Justification").fill(
    "Human acceptance review of plan scope and evidence",
  );
  await page.getByRole("dialog", { name: "Approve cutover" }).getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByText("Operation accepted and queued.")).toBeVisible({ timeout: 45_000 });
  await page.keyboard.press("Escape");
  await reloadUntil(
    page,
    async () => (await page.getByText("COMPLETE", { exact: true }).count()) > 0,
    "the approved run to reach COMPLETE",
    liveEstate,
  );

  await expect(page.getByText("100%", { exact: true }).first()).toBeVisible();

  // Required reasoning is real and visible, not merely declared on a card.
  await page.getByRole("button", { name: "Agents", exact: true }).click();
  await expect(page.getByText("gemini-3.7-flash", { exact: true }).first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("Semantic proposal")).toHaveCount(0); // fixture-only language must never appear live
  await expect(page.getByLabel("Agent execution trail table, horizontally scrollable").getByText("FAILED", { exact: true })).toHaveCount(0);

  const artifactHashes: string[] = [];
  // Complete run/evidence report.
  await page.goto(`/runs/${runId}?estate_id=${encodeURIComponent(liveEstate)}`);
  artifactHashes.push(...await generateAndDownloadReport(page));

  // Assessment report.
  await page.goto(`/assessments?estate_id=${encodeURIComponent(liveEstate)}`);
  await dataTable(page, "Assessments").getByRole("row").filter({ hasText: "PLANNED" }).first().click();
  artifactHashes.push(...await generateAndDownloadReport(page));

  // Reconciliation report.
  await page.goto(`/reconciliation?estate_id=${encodeURIComponent(liveEstate)}`);
  await dataTable(page, "Reconciliation checks").getByRole("row").filter({ hasText: runId! }).first().click();
  artifactHashes.push(...await generateAndDownloadReport(page));

  // Approval and audit report.
  await page.goto(`/policies?estate_id=${encodeURIComponent(liveEstate)}`);
  const policyRow = dataTable(page, "Policy decisions").getByRole("row").filter({ hasText: runId! }).first();
  const approvalRow = dataTable(page, "Approvals").getByRole("row").filter({ hasText: runId! }).first();
  if (await policyRow.count()) await policyRow.click(); else await approvalRow.click();
  artifactHashes.push(...await generateAndDownloadReport(page));
  expect(new Set(artifactHashes).size).toBe(8);

  // The on-screen assistant is estate-scoped, cited and read-only.
  await page.goto(`/runs/${runId}?estate_id=${encodeURIComponent(liveEstate)}`);
  await page.getByRole("button", { name: /Ask Control Tower/ }).click();
  const assistant = page.getByRole("dialog", { name: "Ask Control Tower" });
  await expect(assistant.getByText(/cannot start, retry, approve or modify work/i)).toBeVisible();
  await assistant.getByPlaceholder("Ask about this estate…").fill("Summarize this completed run and cite the evidence used.");
  await assistant.getByRole("button", { name: "Ask" }).click();
  await expect(assistant.locator(".assistant-message.assistant p")).not.toBeEmpty({ timeout: 90_000 });
  await expect(assistant.locator(".assistant-citations button").first()).toBeVisible();

  expect(consoleErrors).toEqual([]);
  console.log(JSON.stringify({
    live_estate_id: liveEstate,
    live_run_id: runId,
    final_state: "COMPLETE",
    report_hashes: artifactHashes,
    agent_reasoning: "gemini-3.7-flash",
    assistant: "gemini-3.5-flash",
  }));
});
