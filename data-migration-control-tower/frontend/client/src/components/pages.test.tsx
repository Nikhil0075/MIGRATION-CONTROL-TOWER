import { cleanup, render, screen } from "@testing-library/preact";
import { h } from "preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LifecycleProgress, PageRouter, StatusPill } from "./pages";
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

  it.each([
    ["waiting", 67, "Waiting for cutover approval"],
    ["held", 42, "Held by operator"],
    ["failed", 50, "Validation failed"],
    ["complete", 100, "Migration complete"],
  ])("renders %s progress with text and accessible value", (status, percent, label) => {
    render(
      <LifecycleProgress
        progress={{
          percent,
          status: status as any,
          label,
          current_stage: status.toUpperCase(),
          completed_units: 1,
          total_units: 12,
          run_id: "run_test",
          last_observed_at: "2026-08-18T00:00:00Z",
        }}
      />,
    );
    expect(screen.getByText(`${percent}%`)).toBeTruthy();
    expect(screen.getByText(label)).toBeTruthy();
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe(String(percent));
  });
});

describe("access levels on the sign-in screen", () => {
  it("names both access levels and what each can do", async () => {
    const { AuthenticationGate } = await import("./access-gates");
    render(<AuthenticationGate configured={true} onSignIn={vi.fn()} />);

    expect(screen.getByText("Operator")).toBeTruthy();
    expect(screen.getByText("Onboards estates")).toBeTruthy();
    expect(screen.getByText(/SME/)).toBeTruthy();
    expect(screen.getByText("Acts on the rest")).toBeTruthy();
  });

  it("offers one sign-in, not a choice of privilege", async () => {
    const { AuthenticationGate } = await import("./access-gates");
    const { container } = render(
      <AuthenticationGate configured={true} onSignIn={vi.fn()} />,
    );
    // Two "sign in as <role>" buttons would imply you can self-select
    // privilege. Google proves identity; the role is granted to the account.
    const buttons = Array.from(container.querySelectorAll("button"));
    expect(buttons).toHaveLength(1);
    expect(buttons[0].textContent).toMatch(/Sign in with Google/);
  });

  it("tells a role-less account how to get access instead of 403ing silently", async () => {
    const { NoAccessGate } = await import("./access-gates");
    render(<NoAccessGate email="new.user@example.test" onSignOut={vi.fn()} />);

    expect(screen.getByText("No access yet")).toBeTruthy();
    expect(screen.getByText("new.user@example.test")).toBeTruthy();
    expect(screen.getByText(/OPERATOR_ALLOWLIST/)).toBeTruthy();
    expect(screen.getByText(/APPROVER_ALLOWLIST/)).toBeTruthy();
  });
});
