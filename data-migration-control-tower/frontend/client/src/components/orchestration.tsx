/**
 * Which agent is working, which have finished, and which is stuck.
 *
 * The run document already answers this — `state_history` records every
 * transition with a timestamp, and `_CANONICAL_TRANSITIONS` in
 * agents/orchestrator/run_lifecycle.py is the authoritative graph — but
 * the console rendered it as a numbered list of state names. An operator
 * had to know that ANALYZED means Lineage finished and that REMEDIATING
 * means Validation is on its second attempt.
 *
 * Everything here is DERIVED from the run document. Nothing is fetched,
 * nothing is estimated, and no status is invented: a stage is complete
 * only because its state appears in the run's own history.
 *
 * The data plane appears as a stage with no agent on purpose. Bulk
 * movement is deterministic Python with no model involved, and a map that
 * showed seven agents and hid the thing that actually moves the rows
 * would misrepresent the architecture it exists to explain.
 *
 * Finance Impact is deliberately absent: it is resolved through a wildcard
 * capability by a different department, outside the linear lifecycle, so
 * placing it on the run's critical path would imply a dependency that does
 * not exist.
 */

import { h } from "preact";
import { AGENT_IDENTITY, agentIdentity } from "./agents";

export type StageStatus = "complete" | "active" | "waiting" | "failed" | "retrying";

export type Stage = {
  key: string;
  label: string;
  /** null for the data plane, which is not an agent. */
  agentId: string | null;
  status: StageStatus;
  at: string | null;
};

type StageSpec = {
  key: string;
  agentId: string | null;
  label: string;
  /** The state that exists once this stage has done its work. */
  doneState: string;
};

/** In lifecycle order. `doneState` is the state the run reaches on success. */
export const LIFECYCLE_STAGES: StageSpec[] = [
  { key: "discovery", agentId: "discovery-agent", label: "Discovery", doneState: "DISCOVERED" },
  { key: "lineage", agentId: "lineage-agent", label: "Lineage", doneState: "ANALYZED" },
  { key: "risk", agentId: "risk-agent", label: "Risk", doneState: "RISK_ASSESSED" },
  { key: "planner", agentId: "planner-agent", label: "Planner", doneState: "PLANNED" },
  { key: "data-plane", agentId: null, label: "Data plane", doneState: "VALIDATING" },
  { key: "validation", agentId: "validation-agent", label: "Validation", doneState: "PASSED" },
  { key: "cutover", agentId: "cutover-agent", label: "Cutover", doneState: "COMPLETE" },
];

const RECOVERY_STATES = new Set(["INVESTIGATING", "REMEDIATING"]);

export function deriveStages(run: {
  state?: string;
  state_history?: { state: string; at: string }[];
}): Stage[] {
  const history = run.state_history || [];
  const reached = new Map(history.map((entry) => [entry.state, entry.at]));
  const current = run.state || (history.length ? history[history.length - 1].state : "");

  // The recovery loop re-enters VALIDATING, so PASSED can be absent while
  // the run is very much alive. Validation is the stage that owns those
  // states; attributing them anywhere else would point an operator at the
  // wrong agent during the one situation where it matters most.
  const failed = current === "FAILED";
  const retrying = RECOVERY_STATES.has(current);

  let activeAssigned = false;
  return LIFECYCLE_STAGES.map((spec) => {
    const at = reached.get(spec.doneState) ?? null;
    let status: StageStatus;

    if (at) {
      status = "complete";
    } else if (spec.key === "validation" && (failed || retrying)) {
      status = failed ? "failed" : "retrying";
      activeAssigned = true;
    } else if (!activeAssigned && current) {
      // The first unfinished stage is the one doing the work.
      status = "active";
      activeAssigned = true;
    } else {
      status = "waiting";
    }

    return { key: spec.key, label: spec.label, agentId: spec.agentId, status, at };
  });
}

const STATUS_LABEL: Record<StageStatus, string> = {
  complete: "Complete",
  active: "Working",
  waiting: "Waiting",
  failed: "Failed",
  retrying: "Retrying",
};

export function OrchestrationMap({
  run,
  compact = false,
}: {
  run: { state?: string; state_history?: { state: string; at: string }[] } | null | undefined;
  compact?: boolean;
}) {
  if (!run || !run.state) {
    return <p class="empty-state">No run selected, so there is no orchestration to show.</p>;
  }
  const stages = deriveStages(run);

  return (
    <ol class={`orchestration-map ${compact ? "compact" : ""}`} aria-label="Agent orchestration">
      {stages.map((stage) => {
        const identity = stage.agentId ? agentIdentity(stage.agentId) : null;
        return (
          <li key={stage.key} class={`orchestration-stage is-${stage.status}`}>
            <span
              class="orchestration-node"
              style={identity ? { "--agent-accent": identity.accent } : undefined}
            >
              {identity ? (
                <img src={identity.art} alt="" aria-hidden="true" width={compact ? 28 : 40} height={compact ? 28 : 40} />
              ) : (
                // No icon: the data plane is not an agent, and giving it
                // one would be the misrepresentation this map avoids.
                <span class="orchestration-node-plain" aria-hidden="true" />
              )}
            </span>
            <span class="orchestration-text">
              <strong>{stage.label}</strong>
              {/* Status is spelled out, never carried by colour alone. */}
              <small>{STATUS_LABEL[stage.status]}</small>
            </span>
          </li>
        );
      })}
    </ol>
  );
}

/** Exposed for the test that keeps this map honest against the registry. */
export const LIFECYCLE_AGENT_IDS = LIFECYCLE_STAGES.map((s) => s.agentId).filter(
  (id): id is string => Boolean(id),
);

export const KNOWN_AGENT_IDS = Object.keys(AGENT_IDENTITY);
