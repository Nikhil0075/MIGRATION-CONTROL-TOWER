import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { cleanup, render, screen } from "@testing-library/preact";
import { h } from "preact";
import { afterEach, describe, expect, it } from "vitest";
import { AGENT_IDENTITY } from "./agents";
import { LIFECYCLE_AGENT_IDS, LIFECYCLE_STAGES, OrchestrationMap, deriveStages } from "./orchestration";

afterEach(cleanup);

const REPO = resolve(__dirname, "..", "..", "..", "..");

/** Build a run whose history walks the canonical path up to `state`. */
function runAt(state: string, extra: string[] = []) {
  const path = [
    "REQUESTED", "DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED",
    "MIGRATING", "VALIDATING", "PASSED", "READY_FOR_APPROVAL", "APPROVED",
    "CUTOVER", "MONITORING", "COMPLETE",
  ];
  const cut = path.indexOf(state);
  const walked = cut >= 0 ? path.slice(0, cut + 1) : [...path.slice(0, path.indexOf("VALIDATING") + 1)];
  const states = [...walked, ...extra];
  return {
    state: extra.length ? extra[extra.length - 1] : state,
    state_history: states.map((s, i) => ({ state: s, at: `2026-08-19T10:${String(i).padStart(2, "0")}:00Z` })),
  };
}

const statusOf = (run: any, key: string) =>
  deriveStages(run).find((stage) => stage.key === key)!.status;

describe("stage derivation", () => {
  it("marks the first unfinished stage as working", () => {
    const stages = deriveStages(runAt("REQUESTED"));
    expect(stages[0].status).toBe("active");
    expect(stages.slice(1).every((s) => s.status === "waiting")).toBe(true);
  });

  it("completes each stage only when its state is in the run's own history", () => {
    const run = runAt("PLANNED");
    expect(statusOf(run, "discovery")).toBe("complete");
    expect(statusOf(run, "planner")).toBe("complete");
    // PLANNED means the planner finished and the data plane has the work.
    expect(statusOf(run, "data-plane")).toBe("active");
    expect(statusOf(run, "cutover")).toBe("waiting");
  });

  it("attributes a failure to Validation, not to whoever ran last", () => {
    const run = { state: "FAILED", state_history: runAt("VALIDATING").state_history.concat({ state: "FAILED", at: "x" }) };
    expect(statusOf(run, "validation")).toBe("failed");
    expect(statusOf(run, "data-plane")).toBe("complete");
    expect(statusOf(run, "cutover")).toBe("waiting");
  });

  it("shows the recovery loop as retrying rather than as a fresh failure", () => {
    // FAILED -> INVESTIGATING -> REMEDIATING is the documented loop. An
    // operator watching it needs to see work in progress, not an error.
    const run = runAt("VALIDATING", ["FAILED", "INVESTIGATING", "REMEDIATING"]);
    expect(statusOf(run, "validation")).toBe("retrying");
  });

  it("clears the failure once the retry passes", () => {
    // The run has FAILED in its history and is now PASSED. A map keyed off
    // "has this run ever failed" would keep showing red forever.
    const run = runAt("VALIDATING", ["FAILED", "INVESTIGATING", "REMEDIATING", "VALIDATING", "PASSED"]);
    expect(statusOf(run, "validation")).toBe("complete");
    expect(statusOf(run, "cutover")).toBe("active");
  });

  it("completes every stage for a finished run", () => {
    expect(deriveStages(runAt("COMPLETE")).every((s) => s.status === "complete")).toBe(true);
  });

  it("never marks two stages as working at once", () => {
    for (const state of ["REQUESTED", "DISCOVERED", "PLANNED", "MIGRATING", "VALIDATING", "PASSED"]) {
      const working = deriveStages(runAt(state)).filter((s) => s.status === "active");
      expect(working.length, state).toBeLessThanOrEqual(1);
    }
  });
});

describe("what the map claims about the architecture", () => {
  it("keeps every lifecycle state in the map reachable in the real state machine", () => {
    // The map asserts that reaching e.g. RISK_ASSESSED means Risk finished.
    // If run_lifecycle.py renames or drops a state, the map silently stops
    // completing that stage — so the states are checked against the source.
    const source = readFileSync(
      resolve(REPO, "agents", "orchestrator", "run_lifecycle.py"),
      "utf8",
    );
    const graph = source.slice(source.indexOf("_CANONICAL_TRANSITIONS"));
    for (const spec of LIFECYCLE_STAGES) {
      expect(graph, spec.doneState).toContain(`"${spec.doneState}"`);
    }
  });

  it("uses only agents that have an identity", () => {
    for (const id of LIFECYCLE_AGENT_IDS) expect(AGENT_IDENTITY[id]).toBeTruthy();
  });

  it("keeps Finance Impact off the run's critical path", () => {
    // It is resolved through a wildcard capability by another department,
    // outside the lifecycle. Putting it in the chain would imply a
    // dependency that does not exist.
    expect(LIFECYCLE_AGENT_IDS).not.toContain("finance-impact-agent");
  });

  it("shows the data plane as a stage with no agent", () => {
    // Bulk movement is deterministic Python with no model involved. Giving
    // it an agent icon would misstate the architecture the map explains.
    const dataPlane = LIFECYCLE_STAGES.find((s) => s.key === "data-plane")!;
    expect(dataPlane.agentId).toBeNull();
  });
});

describe("rendering", () => {
  it("spells out each status instead of relying on colour", () => {
    render(<OrchestrationMap run={runAt("PLANNED")} />);
    expect(screen.getByText("Working")).toBeTruthy();
    expect(screen.getAllByText("Complete").length).toBeGreaterThan(0);
  });

  it("says so plainly when there is no run", () => {
    render(<OrchestrationMap run={null} />);
    expect(screen.getByText(/no orchestration to show/i)).toBeTruthy();
  });
});
