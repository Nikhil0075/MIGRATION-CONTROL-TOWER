import { cleanup, render, screen } from "@testing-library/preact";
import { h } from "preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
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

// ---------------------------------------------------------------------------
// The Workers panel
// ---------------------------------------------------------------------------
//
// "Queued, but nothing is happening" is the failure this panel exists to
// explain. Each test below pins one of the three reasons it can be true,
// because a panel that renders an empty table for all three would leave
// the operator exactly where they started.

function mockApi(responses: Record<string, unknown>) {
  vi.mocked(api).mockImplementation((path: string) => {
    const match = Object.keys(responses).find((key) => path.startsWith(key));
    if (!match) return new Promise(() => undefined) as any;
    return Promise.resolve({
      data: responses[match],
      meta: { generated_at: "2026-01-01T00:00:00+00:00", freshness: "live" },
    }) as any;
  });
}

const operatorProps = {
  ...pageProps,
  session: { ...session, roles: ["viewer", "operator"] } as any,
  route: "system-health",
};

describe("workers panel", () => {
  it("lists each consumer with its subscription and state", async () => {
    mockApi({
      "/api/v1/workers": {
        enabled: true,
        lease: { held: true, standby_reason: null },
        consumers: [
          {
            name: "plan",
            subscription: "plan-created-sub",
            state: "idle",
            processed_count: 4,
            error_count: 0,
            backlog: null,
          },
        ],
      },
    });
    render(<PageRouter {...operatorProps} />);
    expect(await screen.findByText("plan-created-sub")).toBeTruthy();
  });

  it("says backlog is not available rather than showing a zero", async () => {
    // Real queue depth needs google-cloud-monitoring and an IAM role this
    // project does not grant. A rendered 0 would read as "nothing queued",
    // which is the opposite of the truth when work is piling up.
    mockApi({
      "/api/v1/workers": {
        enabled: true,
        lease: { held: true, standby_reason: null },
        consumers: [
          // Every other field is populated, so "Not available" below can
          // only have come from the backlog cell.
          {
            name: "plan",
            subscription: "plan-created-sub",
            state: "idle",
            last_message_at: "2026-01-01T00:00:00+00:00",
            processed_count: 7,
            error_count: 2,
            backlog: null,
          },
        ],
      },
    });
    render(<PageRouter {...operatorProps} />);
    expect(await screen.findByText("Not available")).toBeTruthy();
  });

  it("explains a disabled process instead of rendering an empty table", async () => {
    mockApi({
      "/api/v1/workers": {
        enabled: false,
        reason: "CONTROL_TOWER_WORKERS is set to off for this process",
        lease: { held: false },
        consumers: [],
      },
    });
    render(<PageRouter {...operatorProps} />);
    expect(await screen.findByText(/CONTROL_TOWER_WORKERS is set to off/)).toBeTruthy();
  });

  it("explains standby rather than looking broken", async () => {
    // A second Cloud Run instance consuming nothing is the lock working,
    // not a fault, and the panel has to say which.
    mockApi({
      "/api/v1/workers": {
        enabled: true,
        lease: { held: false, standby_reason: "another instance holds the worker lease (host-b:42)" },
        consumers: [],
      },
    });
    render(<PageRouter {...operatorProps} />);
    expect(await screen.findByText(/another instance holds the worker lease/)).toBeTruthy();
  });

  it("does not offer pause to a viewer", async () => {
    mockApi({
      "/api/v1/workers": {
        enabled: true,
        lease: { held: true, standby_reason: null },
        consumers: [
          { name: "plan", subscription: "plan-created-sub", state: "idle", backlog: null },
        ],
      },
    });
    render(<PageRouter {...pageProps} route="system-health" />);
    const button = (await screen.findByRole("button", { name: "Pause plan" })) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });

  it("offers resume, not pause, for a paused consumer", async () => {
    mockApi({
      "/api/v1/workers": {
        enabled: true,
        lease: { held: true, standby_reason: null },
        consumers: [
          { name: "plan", subscription: "plan-created-sub", state: "paused", backlog: null },
        ],
      },
    });
    render(<PageRouter {...operatorProps} />);
    expect(await screen.findByRole("button", { name: "Resume plan" })).toBeTruthy();
  });
});
