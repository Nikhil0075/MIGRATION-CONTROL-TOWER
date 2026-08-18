import { cleanup, render, screen } from "@testing-library/preact";
import { h } from "preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PageRouter, StatusPill } from "./pages";
import { formatValue, statusTone } from "../status";

vi.mock("../api", () => ({
  api: vi.fn(() => new Promise(() => undefined)),
  idempotencyKey: vi.fn(() => "test-idempotency-key"),
}));

afterEach(cleanup);

const session = { uid: "viewer", email: "viewer@example.internal", roles: ["viewer"] as const };
const pageProps = {
  session: session as any,
  onInspect: vi.fn(),
  navigate: vi.fn(),
};

describe("operator routes", () => {
  it.each([
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
  ])("renders %s as %s", async (route, heading) => {
    render(<PageRouter {...pageProps} route={route} />);
    expect(await screen.findByRole("heading", { name: heading })).toBeTruthy();
  });
});

describe("evidence presentation", () => {
  it("uses a shape and text in addition to status color", () => {
    const { container } = render(<StatusPill value="FAILED" />);
    expect(screen.getByText("FAILED")).toBeTruthy();
    expect(container.querySelector(".status-dot")?.getAttribute("aria-hidden")).toBe("true");
    expect(statusTone("FAILED")).toBe("danger");
  });

  it("does not invent values for unavailable measurements", () => {
    expect(formatValue(null)).toBe("Not available");
    expect(formatValue(0)).toBe("0");
  });
});
