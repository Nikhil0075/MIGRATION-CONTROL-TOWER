#!/usr/bin/env python
"""Day 5 (part 3) run: the Cutover Agent's side of the approval gate.

Dispatches on the run's current state (idempotent-ish, so it's safe to
re-run while waiting for human approval):
  - state == PASSED:   requests approval, transitions -> READY_FOR_APPROVAL
  - state == APPROVED: performs cutover + post-cutover monitoring, -> COMPLETE

Usage (from repo root):
    python agents/cutover/run_cutover.py [run_id]

Between the two states, a human must run:
    python agents/cutover/approve_cutover.py [run_id]
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from agents.cutover.agent import (  # noqa: E402
    AGENT_FRAMEWORK,
    attempt_self_approval,
    perform_cutover,
    request_approval,
    trigger_post_cutover_monitoring,
)
from agents.orchestrator.run_lifecycle import get_latest_run_id, get_run, transition_state  # noqa: E402

from tools.migration_plan import primary_target  # noqa: E402


def main() -> None:
    print(f"[cutover-agent] framework: {AGENT_FRAMEWORK}")

    run_id = sys.argv[1] if len(sys.argv) > 1 else get_latest_run_id()
    state = get_run(run_id)["state"]
    print(f"[cutover-agent] operating on run: {run_id} (state={state})")

    # §12/§5.2 proof: cutover cannot self-approve, regardless of state.
    denial = attempt_self_approval(run_id)
    assert denial["decision"] == "DENY"

    if state == "PASSED":
        record = request_approval(run_id)
        transition_state(run_id, "READY_FOR_APPROVAL")
        print(f"[cutover-agent] approval requested (plan_hash={record['plan_hash'][:12]}...)")
        print(
            "[cutover-agent] waiting for human approval — run: "
            f"python agents/cutover/approve_cutover.py {run_id}"
        )
        return

    if state == "APPROVED":
        cutover_record = perform_cutover(run_id)
        transition_state(run_id, "CUTOVER")
        print(f"[cutover-agent] cutover performed: {cutover_record}")

        transition_state(run_id, "MONITORING")
        target = primary_target(run_id)
        monitoring = trigger_post_cutover_monitoring(
            run_id,
            target["source_schema"],
            target["source_table"],
            target["target_table"],
            target["key_column"],
        )
        print(f"[cutover-agent] post-cutover monitoring: {monitoring['status']}")

        if monitoring["status"] == "HEALTHY":
            transition_state(run_id, "COMPLETE")
            print(
                "[cutover-agent] Day 5 (cutover) exit condition check: "
                f"run_id={run_id}, state=COMPLETE, self_approval_denied=True"
            )
        else:
            print(
                "[cutover-agent] WARNING: post-cutover monitoring found a "
                "DEGRADED state — run stays at MONITORING, not COMPLETE."
            )
        return

    print(
        f"[cutover-agent] run {run_id} is in state {state!r} — nothing to do here. "
        "Expected PASSED (request approval) or APPROVED (perform cutover)."
    )


if __name__ == "__main__":
    main()
