#!/usr/bin/env python
"""Day 3 (part 2) exit-condition run: proves a seeded row-loss defect
fails reconciliation (master doc §17.1, Tue 18 Aug).

As of Day 5, the state machine requires MIGRATING as the prior state
(RISK_ASSESSED -> PLANNED -> MIGRATING -> VALIDATING is now the only
legal path — see run_lifecycle.py). Run agents/planner/run_planner.py
and tools/migration_executor.py (or
agents/orchestrator/run_full_migration.py, which chains all of this
automatically) before calling this standalone for a hand-run debugging
session.

Usage (from repo root, after run_discovery.py, run_lineage.py,
run_risk.py, run_planner.py, and a migration_executor run/
seed_row_loss.py have all run for the same run_id):
    python agents/validation/run_validation.py [run_id]
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from agents.orchestrator.run_lifecycle import get_latest_run_id, transition_state  # noqa: E402
from agents.validation.agent import AGENT_FRAMEWORK, run_reconciliation  # noqa: E402


def main() -> None:
    print(f"[validation-agent] framework: {AGENT_FRAMEWORK}")

    run_id = sys.argv[1] if len(sys.argv) > 1 else get_latest_run_id()
    print(f"[validation-agent] operating on run: {run_id}")

    transition_state(run_id, "VALIDATING")
    summary = run_reconciliation(run_id)

    for check in summary["checks"]:
        print(f"  {check['check_type']:<12} {check['status']}")

    if summary["overall_status"] == "PASSED":
        transition_state(run_id, "PASSED")
    else:
        transition_state(run_id, "FAILED")

    print(
        "[validation-agent] Day 3 (validation) exit condition check: "
        f"run_id={run_id}, overall_status={summary['overall_status']}, "
        f"state={summary['overall_status']}"
    )

    row_count_check = next(c for c in summary["checks"] if c["check_type"] == "row_count")
    if row_count_check["status"] != "FAIL":
        print(
            "[validation-agent] WARNING: expected the seeded row-loss defect to "
            "fail the row_count check — did you run "
            "simulator/failure_injector/seed_row_loss.py for this run_id first?"
        )


if __name__ == "__main__":
    main()
