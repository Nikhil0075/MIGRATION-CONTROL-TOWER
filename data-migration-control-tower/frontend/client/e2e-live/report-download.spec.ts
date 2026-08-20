import { expect, test } from "@playwright/test";

const required = (name: string): string => {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required for the live report test.`);
  return value;
};

test("LIVE report download originates from one authorized page request", async ({ page }) => {
  const estateId = required("CONTROL_TOWER_E2E_ESTATE_ID");
  const runId = required("CONTROL_TOWER_E2E_RUN_ID");
  const reportRequests: Array<{ resource: string; authorized: boolean; status?: number }> = [];
  page.on("request", (request) => {
    if (request.url().includes("/download?format=pdf")) {
      reportRequests.push({
        resource: request.resourceType(),
        authorized: Boolean(request.headers().authorization),
      });
    }
  });
  page.on("response", (response) => {
    if (response.url().includes("/download?format=pdf")) {
      const pending = reportRequests.find((item) => item.status === undefined && item.authorized === Boolean(response.request().headers().authorization));
      if (pending) pending.status = response.status();
    }
  });
  await page.goto("/overview");
  await page.getByRole("button", { name: /Sign in with domain email/ }).click();
  await page.getByLabel("Domain email address").fill(required("CONTROL_TOWER_E2E_EMAIL"));
  await page.getByLabel("Password", { exact: true }).fill(required("CONTROL_TOWER_E2E_PASSWORD"));
  await page.getByRole("button", { name: /Sign in with email/ }).click();
  await expect(page.getByRole("heading", { name: "Overview", exact: true })).toBeVisible({ timeout: 120_000 });
  await page.evaluate((id) => localStorage.setItem("mct.activeEstate", id), estateId);
  await page.goto(`/runs/${encodeURIComponent(runId)}?estate_id=${encodeURIComponent(estateId)}`);
  await expect(page.getByRole("button", { name: "Download PDF" })).toBeVisible({ timeout: 60_000 });
  const [download, response] = await Promise.all([
    page.waitForEvent("download"),
    page.waitForResponse((candidate) => candidate.url().includes("/download?format=pdf")),
    page.getByRole("button", { name: "Download PDF" }).click(),
  ]);
  expect(response.status()).toBe(200);
  expect(await response.headerValue("x-content-sha256")).toMatch(/^[a-f0-9]{64}$/);
  expect(await download.failure()).toBeNull();
  console.log(JSON.stringify({ report_requests: reportRequests }));
});
