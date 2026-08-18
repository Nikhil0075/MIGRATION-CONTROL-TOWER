#!/usr/bin/env python
"""Day 3 exit-condition run (classification + PII-denial proof), extended
Day 8 with the multimodal documentation-drift and fast-PII-prescreen
proofs (master doc §17.1/§17.2, §22).

Usage (from repo root):
    python agents/risk/run_risk.py [run_id]

Defaults to the most recently created run if run_id is omitted.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from agents.orchestrator.run_lifecycle import get_latest_run_id, transition_state  # noqa: E402
from agents.risk.agent import (  # noqa: E402
    AGENT_FRAMEWORK,
    assess_documentation_drift,
    classify_estate,
    run_fast_pii_prescreen,
    verify_pii_access_boundary,
)


def main() -> None:
    print(f"[risk-agent] framework: {AGENT_FRAMEWORK}")

    run_id = sys.argv[1] if len(sys.argv) > 1 else get_latest_run_id()
    print(f"[risk-agent] operating on run: {run_id}")

    summary = classify_estate(run_id)
    print(f"[risk-agent] classify_estate: {summary}")

    prescreen = run_fast_pii_prescreen(run_id)
    print(f"[risk-agent] run_fast_pii_prescreen: {prescreen}")

    drift = assess_documentation_drift(run_id)
    print(f"[risk-agent] assess_documentation_drift: {drift}")

    decision = verify_pii_access_boundary(run_id)
    print(f"[risk-agent] verify_pii_access_boundary: {decision}")

    if decision["decision"] != "DENY":
        print(
            "[risk-agent] FAIL: expected the unauthorized raw-PII read to be "
            "DENIED — policy engine returned "
            f"{decision['decision']!r} instead. Check policies/agent_permissions.yaml."
        )
        raise SystemExit(1)

    transition_state(run_id, "RISK_ASSESSED")
    print(
        "[risk-agent] Day 3/8 (risk) exit condition check: "
        f"run_id={run_id}, pii_tables={summary['pii_tables']}, "
        f"dialect_findings={summary['dialect_findings']}, "
        f"screen_disagreements={prescreen['disagreements']}, "
        f"documentation_drift_findings={drift['total_drift_findings']}, "
        f"unauthorized_read_denied=True (policy_id={decision['policy_id']}), "
        "state=RISK_ASSESSED"
    )


if __name__ == "__main__":
    main()
