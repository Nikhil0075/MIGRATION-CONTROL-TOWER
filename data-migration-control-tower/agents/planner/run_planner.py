#!/usr/bin/env python
"""Day 5 (part 1) run: proposes a migration plan and transitions
RISK_ASSESSED -> PLANNED (master doc §17.1, Thu 20 Aug).

Usage (from repo root):
    python agents/planner/run_planner.py [run_id]
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from agents.orchestrator.run_lifecycle import get_latest_run_id, transition_state  # noqa: E402
from agents.planner.agent import AGENT_FRAMEWORK, propose_plan  # noqa: E402


def main() -> None:
    print(f"[planner-agent] framework: {AGENT_FRAMEWORK}")

    run_id = sys.argv[1] if len(sys.argv) > 1 else get_latest_run_id()
    print(f"[planner-agent] operating on run: {run_id}")

    plan = propose_plan(run_id)
    scheduled = [s for s in plan["steps"] if s["scheduled"]]
    print(
        f"[planner-agent] plan: {len(plan['steps'])} steps, "
        f"{len(scheduled)} scheduled this run, plan_hash={plan['plan_hash'][:12]}..."
    )
    for step in scheduled:
        print(f"    scheduled: {step['table_id']} -> {step['target_table']}")

    transition_state(run_id, "PLANNED")
    print(f"[planner-agent] Day 5 (planner) exit condition check: run_id={run_id}, state=PLANNED")


if __name__ == "__main__":
    main()
