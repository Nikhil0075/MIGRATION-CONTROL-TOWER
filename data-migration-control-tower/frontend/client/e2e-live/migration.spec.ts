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
  await page.getByLabel("Domain email address").fill(liveEmail);
  await page.getByLabel("Password").fill(livePassword);
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
  await expect(page.getByText("Operation accepted and queued.")).toBeVisible();
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
  await expect(page.getByRole("heading", { name: "Runs", exact: true })).toBeVisible();
  await expect(page.getByLabel("Active estate")).toHaveValue(liveEstate, { timeout: 60_000 });
  const startMigration = page.getByRole("button", { name: "Start migration" });
  await expect(startMigration).toBeEnabled();
  await startMigration.click();
  await page.getByRole("dialog", { name: "Start migration" }).getByLabel("Justification").fill(
    "Execute the live pack-driven migration",
  );
  await page.getByRole("dialog", { name: "Start migration" }).getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByText("Operation accepted and queued.")).toBeVisible();
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
  await expect(page.getByText("Operation accepted and queued.")).toBeVisible();
  await page.keyboard.press("Escape");
  await reloadUntil(
    page,
    async () => (await page.getByText("COMPLETE", { exact: true }).count()) > 0,
    "the approved run to reach COMPLETE",
    liveEstate,
  );

  await expect(page.getByText("100%", { exact: true }).first()).toBeVisible();
  expect(consoleErrors).toEqual([]);
  console.log(JSON.stringify({ live_estate_id: liveEstate, live_run_id: runId, final_state: "COMPLETE" }));
});
