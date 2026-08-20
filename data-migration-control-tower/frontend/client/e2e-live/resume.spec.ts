import { expect, Page, test } from "@playwright/test";

const required = (name: string): string => {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required for the live continuation test.`);
  return value;
};

async function login(page: Page, estateId: string) {
  await page.goto("/overview");
  await page.getByRole("button", { name: /Sign in with domain email/ }).click();
  await page.getByLabel("Domain email address").fill(required("CONTROL_TOWER_E2E_EMAIL"));
  await page.getByLabel("Password", { exact: true }).fill(required("CONTROL_TOWER_E2E_PASSWORD"));
  await page.getByRole("button", { name: /Sign in with email/ }).click();
  await expect(page.getByRole("heading", { name: "Overview", exact: true })).toBeVisible({ timeout: 120_000 });
  await page.evaluate((id) => localStorage.setItem("mct.activeEstate", id), estateId);
}

async function waitForComplete(page: Page, url: string) {
  const deadline = Date.now() + 8 * 60_000;
  while (Date.now() < deadline) {
    if (await page.getByText("COMPLETE", { exact: true }).count()) return;
    await page.waitForTimeout(3_000);
    await page.goto(url, { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { name: "Run detail", exact: true })).toBeVisible({ timeout: 30_000 });
  }
  if (await page.getByText("COMPLETE", { exact: true }).count()) return;
  throw new Error("Timed out waiting for the approved run to reach COMPLETE.");
}

async function generateReport(page: Page): Promise<string[]> {
  const generate = page.getByRole("button", { name: "Generate report" });
  const print = page.getByRole("button", { name: "Print" });
  await expect.poll(
    async () => (await generate.count()) + (await print.count()),
    { timeout: 120_000 },
  ).toBeGreaterThan(0);
  if (await generate.count()) await generate.click();
  await expect(print).toBeVisible({ timeout: 10 * 60_000 });
  const hashes: string[] = [];
  for (const [label, format] of [["Download PDF", "pdf"], ["Download JSON", "json"]] as const) {
    const [download, response] = await Promise.all([
      page.waitForEvent("download"),
      page.waitForResponse((candidate) => candidate.url().includes("/download") && candidate.url().includes(`format=${format}`)),
      page.getByRole("button", { name: label }).click(),
    ]);
    expect(await download.failure()).toBeNull();
    const hash = await response.headerValue("x-content-sha256");
    expect(hash).toMatch(/^[a-f0-9]{64}$/);
    hashes.push(hash!);
  }
  return hashes;
}

test("LIVE continuation: READY_FOR_APPROVAL → COMPLETE → reports → assistant", async ({ page }) => {
  const estateId = required("CONTROL_TOWER_E2E_ESTATE_ID");
  const runId = required("CONTROL_TOWER_E2E_RUN_ID");
  const assessmentEstateId = required("CONTROL_TOWER_E2E_ASSESSMENT_ESTATE_ID");
  const assessmentRunId = required("CONTROL_TOWER_E2E_ASSESSMENT_RUN_ID");
  const errors: string[] = [];
  page.on("console", (message) => message.type() === "error" && errors.push(message.text()));
  page.on("pageerror", (error) => errors.push(error.message));

  await login(page, estateId);
  const runUrl = `/runs/${runId}?estate_id=${encodeURIComponent(estateId)}`;
  await page.goto(runUrl);
  await expect(page.getByRole("heading", { name: "Run detail", exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(page.locator(".spinner")).toHaveCount(0, { timeout: 60_000 });
  const approve = page.getByRole("button", { name: "Approve cutover" });
  if (await approve.count()) {
    await approve.click();
    await page.getByRole("dialog", { name: "Approve cutover" }).getByLabel("Justification").fill(
      "Human acceptance review of AI audit and migration evidence",
    );
    await page.getByRole("dialog", { name: "Approve cutover" }).getByRole("button", { name: "Confirm" }).click();
    await expect(page.getByText("Operation accepted and queued.")).toBeVisible({ timeout: 45_000 });
    await page.keyboard.press("Escape");
    await waitForComplete(page, runUrl);
  } else {
    await expect(page.getByText("COMPLETE", { exact: true }).first()).toBeVisible({ timeout: 60_000 });
  }
  await expect(page.getByText("100%", { exact: true }).first()).toBeVisible({ timeout: 30_000 });

  await page.getByRole("button", { name: "Agents", exact: true }).click();
  await expect(
    page.getByLabel("Agent execution trail table, horizontally scrollable")
      .getByRole("cell", { name: "gemini-3.7-flash", exact: true })
      .first(),
  ).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("Decision & generation trail", { exact: true })).toBeVisible();

  const hashes: string[] = [];
  await page.goto(runUrl);
  hashes.push(...await generateReport(page));

  await page.goto(
    `/assessments?estate_id=${encodeURIComponent(assessmentEstateId)}&run_id=${encodeURIComponent(assessmentRunId)}`,
  );
  await expect(page.getByRole("heading", { name: "Assessments", exact: true })).toBeVisible({ timeout: 30_000 });
  hashes.push(...await generateReport(page));

  await page.goto(
    `/reconciliation?estate_id=${encodeURIComponent(estateId)}&run_id=${encodeURIComponent(runId)}`,
  );
  await expect(page.getByRole("heading", { name: "Reconciliation", exact: true })).toBeVisible({ timeout: 30_000 });
  hashes.push(...await generateReport(page));

  await page.goto(
    `/policies?estate_id=${encodeURIComponent(estateId)}&run_id=${encodeURIComponent(runId)}`,
  );
  await expect(page.getByRole("heading", { name: "Policies & Approvals", exact: true })).toBeVisible({ timeout: 30_000 });
  hashes.push(...await generateReport(page));
  expect(hashes).toHaveLength(8);

  await page.goto(runUrl);
  await page.getByRole("button", { name: /Ask Control Tower/ }).click();
  const assistant = page.getByRole("dialog", { name: "Ask Control Tower" });
  await assistant.getByPlaceholder("Ask about this estate…").fill("Summarize this completed run and cite its evidence.");
  await assistant.getByRole("button", { name: "Ask" }).click();
  await expect(assistant.getByRole("button", { name: "Ask", exact: true })).toBeVisible({ timeout: 5 * 60_000 });
  const answer = assistant.locator(".assistant-message.assistant p").last();
  await expect(answer).not.toHaveText("Reviewing authorized evidence…");
  await expect(answer).not.toBeEmpty();
  await expect(assistant.locator(".assistant-citations button").first()).toBeVisible({ timeout: 5 * 60_000 });

  expect(errors).toEqual([]);
  console.log(JSON.stringify({ estate_id: estateId, run_id: runId, final_state: "COMPLETE", report_hashes: hashes }));
});
