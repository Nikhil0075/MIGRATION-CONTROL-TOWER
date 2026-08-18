#!/usr/bin/env python
"""The human approval step (master doc §5.2, §8.1's "Human approval
service"). Deliberately NOT part of agents/cutover/agent.py — this
script is the only caller of tools/approval_service.approve() anywhere
in the codebase, standing in for an actual person clicking "approve" in
the Control Tower UI (a later build day).

Usage (from repo root):
    python agents/cutover/approve_cutover.py [run_id] [approver_identity]
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from agents.orchestrator.run_lifecycle import get_latest_run_id, transition_state  # noqa: E402
from tools import approval_service  # noqa: E402


def main() -> None:
    run_id = sys.argv[1] if len(sys.argv) > 1 else get_latest_run_id()
    approver = sys.argv[2] if len(sys.argv) > 2 else "ops-lead@example.internal"

    print(f"[human-approval] approving cutover for run: {run_id} (as {approver})")
    record = approval_service.approve(run_id, approver_identity=approver)
    print(f"[human-approval] token issued: {record['token_id']}")

    transition_state(run_id, "APPROVED")
    print(f"[human-approval] run {run_id} -> APPROVED")


if __name__ == "__main__":
    main()
