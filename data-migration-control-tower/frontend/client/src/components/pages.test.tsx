import { cleanup, fireEvent, render, screen } from "@testing-library/preact";
import { h } from "preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { EmptyState, LifecycleProgress, LoadingState, PageRouter, StatusPill } from "./pages";
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
    render(
      <AuthenticationGate
        configured={true}
        onGoogleSignIn={vi.fn()}
        onPasswordSignIn={vi.fn()}
      />,
    );

    expect(screen.getByText("Operator")).toBeTruthy();
    expect(screen.getByText("Onboards estates")).toBeTruthy();
    expect(screen.getByText(/SME/)).toBeTruthy();
    expect(screen.getByText("Acts on the rest")).toBeTruthy();
  });

  it("offers identity providers without offering a choice of privilege", async () => {
    const { AuthenticationGate } = await import("./access-gates");
    const { container } = render(
      <AuthenticationGate
        configured={true}
        onGoogleSignIn={vi.fn()}
        onPasswordSignIn={vi.fn()}
      />,
    );
    // Providers prove identity; neither button chooses operator/approver.
    const buttons = Array.from(container.querySelectorAll("button"));
    expect(buttons).toHaveLength(2);
    expect(buttons.map((button) => button.textContent)).toEqual([
      expect.stringMatching(/Sign in with email/),
      expect.stringMatching(/Continue with Google/),
    ]);
    expect(screen.queryByText(/sign in as operator/i)).toBeNull();
    expect(screen.queryByText(/sign in as approver/i)).toBeNull();
  });

  it("submits a domain email and password without exposing the password", async () => {
    const { AuthenticationGate } = await import("./access-gates");
    const onPasswordSignIn = vi.fn(async () => undefined);
    const { container } = render(
      <AuthenticationGate
        configured={true}
        onGoogleSignIn={vi.fn()}
        onPasswordSignIn={onPasswordSignIn}
      />,
    );
    const email = screen.getByLabelText("Domain email address") as HTMLInputElement;
    const password = screen.getByLabelText("Password") as HTMLInputElement;
    fireEvent.input(email, { target: { value: "operator@example.test" } });
    fireEvent.input(password, { target: { value: "not-rendered-after-submit" } });
    (screen.getByRole("button", { name: /Sign in with email/ }) as HTMLButtonElement).click();
    await vi.waitFor(() => {
      expect(onPasswordSignIn).toHaveBeenCalledWith(
        "operator@example.test",
        "not-rendered-after-submit",
      );
    });
    expect(password.type).toBe("password");
    expect(container.textContent).not.toContain("not-rendered-after-submit");
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

describe("pack-driven migration readiness", () => {
  const readyEstate = {
    estate_id: "sql-estate",
    display_name: "SQL estate",
    status: "ACTIVE",
    sources: [{ source_id: "primary", adapter: "sqlserver", pack_id: "wwi_sqlserver_v1" }],
    pipeline_options: [],
    execution_readiness: {
      status: "ready" as const,
      options: [{ source_id: "primary", pack_id: "wwi_sqlserver_v1", label: "Primary SQL · WWI" }],
      blockers: [],
    },
  };

  it("enables a SQL-only estate from its executable pack without a DAG pipeline", async () => {
    mockApi({ "/api/v1/runs": [] });
    render(
      <PageRouter
        {...operatorProps}
        route="runs"
        activeEstateId="sql-estate"
        activeEstate={readyEstate}
      />,
    );
    const start = await screen.findByRole("button", { name: "Start migration" }) as HTMLButtonElement;
    expect(start.disabled).toBe(false);
    expect(screen.queryByText(/pipeline has been discovered/i)).toBeNull();
  });

  it("requires an explicit choice when several pack bindings are executable", async () => {
    mockApi({ "/api/v1/runs": [] });
    const estate = {
      ...readyEstate,
      execution_readiness: {
        status: "selection_required" as const,
        blockers: [],
        options: [
          ...readyEstate.execution_readiness.options,
          { source_id: "secondary", pack_id: "wwi_sqlserver_v1", label: "Secondary SQL · WWI" },
        ],
      },
    };
    render(
      <PageRouter {...operatorProps} route="runs" activeEstateId="sql-estate" activeEstate={estate} />,
    );
    expect(await screen.findByText("Select a source and Migration Pack.")).toBeTruthy();
    const start = screen.getByRole("button", { name: "Start migration" }) as HTMLButtonElement;
    expect(start.disabled).toBe(true);
    const select = screen.getByLabelText("Source and Migration Pack") as HTMLSelectElement;
    select.value = "primary::wwi_sqlserver_v1";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await vi.waitFor(() => expect(start.disabled).toBe(false));
  });

  it.each([
    ["No executable Migration Pack is assigned.", "NO_EXECUTABLE_PACK"],
    ["The selected pack supports assessment only.", "ASSESSMENT_ONLY_PACK"],
  ])("shows the exact backend blocker: %s", async (message, code) => {
    mockApi({ "/api/v1/runs": [] });
    render(
      <PageRouter
        {...operatorProps}
        route="runs"
        activeEstateId="sql-estate"
        activeEstate={{
          ...readyEstate,
          execution_readiness: { status: "blocked", options: [], blockers: [{ code, message }] },
        }}
      />,
    );
    expect(await screen.findByText(message)).toBeTruthy();
  });

  it("distinguishes missing permission from configuration", async () => {
    mockApi({ "/api/v1/runs": [] });
    render(
      <PageRouter
        {...pageProps}
        route="runs"
        activeEstateId="sql-estate"
        activeEstate={readyEstate}
      />,
    );
    expect(await screen.findByText("Operator permission is required.")).toBeTruthy();
  });

  it("keeps loading distinct from a configuration blocker", async () => {
    mockApi({ "/api/v1/runs": [] });
    render(<PageRouter {...operatorProps} route="runs" activeEstateId="sql-estate" />);
    expect(await screen.findByText("Loading execution readiness…")).toBeTruthy();
    expect(screen.queryByText("No executable Migration Pack is assigned.")).toBeNull();
  });
});


// ---------------------------------------------------------------------------
// The agent fleet
// ---------------------------------------------------------------------------

describe("agent fleet cards", () => {
  const cards = [
    { agent_id: "discovery-agent", version: "1.0.0", status: "APPROVED" },
    // The registry keeps both, deliberately: bumping a capability while the
    // old card stays approved is how runs pinned to it keep working.
    { agent_id: "discovery-agent", version: "1.1.0", status: "APPROVED" },
    { agent_id: "risk-agent", version: "1.0.0", status: "APPROVED" },
    { agent_id: "planner-agent", version: "2.0.0", status: "DRAFT" },
  ];

  it("shows one card per agent, at its newest approved version", async () => {
    mockApi({ "/api/v1/agents": { cards, pinned_run_counts: {} } });
    render(<PageRouter {...pageProps} route="agents" />);
    expect(await screen.findByText("Discovery")).toBeTruthy();
    expect(screen.getAllByText("Discovery")).toHaveLength(1);
    expect(screen.getByText(/v1\.1\.0/)).toBeTruthy();
  });

  it("leaves unapproved versions out of the fleet", async () => {
    mockApi({ "/api/v1/agents": { cards, pinned_run_counts: {} } });
    render(<PageRouter {...pageProps} route="agents" />);
    await screen.findByText("Discovery");
    // Planner is DRAFT only — it is a real registry state, not a fleet member.
    expect(screen.queryByText("Planner")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Shared empty and loading states
// ---------------------------------------------------------------------------

describe("empty states", () => {
  it("always carries text, so the meaning never lives only in a picture", () => {
    render(<EmptyState kind="no-runs" title="No migrations yet" />);
    expect(screen.getByText("No migrations yet")).toBeTruthy();
  });

  it("treats the illustration as decorative", () => {
    // The title already says what is going on; announcing the drawing too
    // would just repeat it for a screen reader.
    const { container } = render(<EmptyState kind="no-incidents" title="No incidents" />);
    const art = container.querySelector(".empty-state-art") as HTMLImageElement;
    expect(art.getAttribute("alt")).toBe("");
    expect(art.getAttribute("aria-hidden")).toBe("true");
    expect(art.getAttribute("src")).toMatch(/^\/assets\/brand\/v1\/empty\//);
  });

  it("works with no illustration at all", () => {
    // Not every empty state has art, and the component must not require it.
    const { container } = render(<EmptyState title="Scale report not configured" />);
    expect(container.querySelector(".empty-state-art")).toBeNull();
    expect(screen.getByText("Scale report not configured")).toBeTruthy();
  });
});

describe("loading states", () => {
  it("names the work being waited for", () => {
    // "Loading…" tells an operator nothing. Naming the work turns a wait
    // into progress they can reason about.
    render(<LoadingState message="Building the dependency graph…" />);
    expect(screen.getByText("Building the dependency graph…")).toBeTruthy();
  });

  it("falls back to a generic message rather than an empty label", () => {
    render(<LoadingState />);
    expect(screen.getByText(/Loading operational data/)).toBeTruthy();
  });
});
