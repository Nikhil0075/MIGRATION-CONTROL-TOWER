#!/usr/bin/env python
"""Day 4 exit-condition run: one command, one published event, and the
run advances DISCOVERED -> ANALYZED -> RISK_ASSESSED automatically — no
manual per-agent script invocation (master doc §17.1, Wed 19 Aug).

Usage (from repo root):
    python agents/orchestrator/run_orchestrator.py [pipeline_id]

Defaults pipeline_id to "wwi.sales.customers" (the §2.3 demo pipeline).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")

from agents.orchestrator.orchestrator import run_once  # noqa: E402
from agents.orchestrator.run_lifecycle import get_run  # noqa: E402


def main() -> None:
    pipeline_id = sys.argv[1] if len(sys.argv) > 1 else "wwi.sales.customers"
    print(f"[orchestrator] publishing migration.requested for pipeline: {pipeline_id}")

    result = run_once(pipeline_id)
    run_id = result["discovery"]["run_id"]
    run = get_run(run_id)

    print(f"[orchestrator] discovery: {result['discovery']}")
    print(f"[orchestrator] analysis:  {result['analysis']}")
    print(
        "[orchestrator] Day 4 exit condition check: "
        f"run_id={run_id}, final_state={run['state']}, "
        f"lineage_edges={result['analysis']['lineage']['edges_written']} (derived, not seeded), "
        f"pii_access_denied={result['analysis']['pii_access_denied']}, "
        "manual_per_agent_invocations=0"
    )


if __name__ == "__main__":
    main()
