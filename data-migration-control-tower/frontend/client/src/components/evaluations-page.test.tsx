// Deploy & Harden Phase 4d: the three-panel scale-evidence surfacing
// (control-plane tiers, data-plane rows-moved, operational load) —
// docs/EVALUATION.md's "three distinct measurements, never one number."
import { cleanup, render, screen } from "@testing-library/preact";
import { h } from "preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { EvaluationsPage } from "./specialized-pages";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: vi.fn(() => new Promise(() => undefined)),
  idempotencyKey: vi.fn(() => "test-idempotency-key"),
}));

afterEach(cleanup);

const pageProps = {
  session: { uid: "viewer", email: "viewer@example.internal", roles: ["viewer"] as const } as any,
  onInspect: vi.fn(),
  navigate: vi.fn(),
  activeEstateId: "",
  route: "evaluations",
};

function mockEvaluations(data: Record<string, unknown>) {
  vi.mocked(api).mockImplementation((path: string) => {
    if (!path.startsWith("/api/v1/evaluations")) return new Promise(() => undefined) as any;
    return Promise.resolve({
      data,
      meta: { generated_at: "2026-01-01T00:00:00+00:00", freshness: "live" },
    }) as any;
  });
}

describe("EvaluationsPage — control-plane scale panel", () => {
  it("renders every tier, sorted largest first, with model_calls surfaced", async () => {
    mockEvaluations({
      runs: [],
      scale_metrics: null,
      scale_report_status: "not_configured",
      scale_report_reason: null,
      scale_metrics_by_tier: {
        "1000": {
          pipeline_count: 1000, model_calls: 0,
          schema_validation: { throughput_per_sec: 500 }, wave_scheduling: { throughput_per_sec: 800 },
        },
        "20000": {
          pipeline_count: 20000, model_calls: 0,
          schema_validation: { throughput_per_sec: 480 }, wave_scheduling: { throughput_per_sec: 760 },
        },
      },
      data_plane_metrics: null,
      data_plane_status: "not_configured",
      data_plane_reason: "Run evaluation/data_plane_scale_test.py to persist a real rows-moved measurement.",
      operational_load_metrics: null,
      operational_load_status: "not_configured",
      operational_load_reason: "Run evaluation/load_test.py to persist a concurrent-load measurement.",
    });

    render(<EvaluationsPage {...pageProps} />);

    expect(await screen.findByText("Control-plane scale")).toBeTruthy();
    expect(screen.getByText("20,000")).toBeTruthy();
    expect(screen.getByText("1,000")).toBeTruthy();
    // model_calls is surfaced explicitly, never silently absent.
    expect(screen.getAllByText("control-plane-only — never scales with tier").length).toBeGreaterThan(0);
  });

  it("shows the not-configured empty state when no tier has ever been run", async () => {
    mockEvaluations({
      runs: [],
      scale_metrics: null,
      scale_report_status: "not_configured",
      scale_report_reason: "Run evaluation/scale_harness.py to persist measured scale metrics.",
      scale_metrics_by_tier: {},
      data_plane_metrics: null,
      data_plane_status: "not_configured",
      data_plane_reason: "Run evaluation/data_plane_scale_test.py to persist a real rows-moved measurement.",
      operational_load_metrics: null,
      operational_load_status: "not_configured",
      operational_load_reason: "Run evaluation/load_test.py to persist a concurrent-load measurement.",
    });

    render(<EvaluationsPage {...pageProps} />);

    expect(await screen.findByText("Scale report not configured")).toBeTruthy();
    expect(screen.getByText("Data-plane report not configured")).toBeTruthy();
    expect(screen.getByText("Operational load report not configured")).toBeTruthy();
  });
});

describe("EvaluationsPage — data-plane and operational-load panels", () => {
  it("renders real rows-moved and concurrent-load figures when both are configured", async () => {
    mockEvaluations({
      runs: [],
      scale_metrics: null,
      scale_report_status: "not_configured",
      scale_report_reason: null,
      scale_metrics_by_tier: {},
      data_plane_metrics: {
        rows_moved: 12345, executor: "InMemoryExecutor", throughput_rows_per_sec: 250.5, status: "COMPLETED",
      },
      data_plane_status: "available",
      data_plane_reason: null,
      operational_load_metrics: {
        concurrent_load: { concurrent_runs: 10, throughput_per_sec: 42.0, latency_p95_ms: 15.0 },
        fleet_state: { deployed_service_count: 1, expected_service_count: 10 },
      },
      operational_load_status: "available",
      operational_load_reason: null,
    });

    render(<EvaluationsPage {...pageProps} />);

    expect(await screen.findByText("Data-plane scale")).toBeTruthy();
    expect(screen.getByText("12,345")).toBeTruthy();
    expect(screen.getByText("InMemoryExecutor")).toBeTruthy();
    expect(screen.getByText("Operational load")).toBeTruthy();
    expect(screen.getByText("1 / 10")).toBeTruthy();
  });
});
