#!/usr/bin/env python
"""Day 4 (part 1) run: derives the dependency graph for a run and
transitions it DISCOVERED -> ANALYZED (master doc §17.1, Wed 19 Aug).

Usage (from repo root):
    python agents/lineage/run_lineage.py [run_id]
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from agents.lineage.agent import AGENT_FRAMEWORK, build_dependency_graph  # noqa: E402
from agents.orchestrator.run_lifecycle import get_latest_run_id, transition_state  # noqa: E402


def main() -> None:
    print(f"[lineage-agent] framework: {AGENT_FRAMEWORK}")

    run_id = sys.argv[1] if len(sys.argv) > 1 else get_latest_run_id()
    print(f"[lineage-agent] operating on run: {run_id}")

    summary = build_dependency_graph(run_id)
    print(f"[lineage-agent] build_dependency_graph: {summary}")

    transition_state(run_id, "ANALYZED")
    print(
        "[lineage-agent] Day 4 (lineage) exit condition check: "
        f"run_id={run_id}, edges_written={summary['edges_written']} "
        f"(derived, not seeded), state=ANALYZED"
    )


if __name__ == "__main__":
    main()
