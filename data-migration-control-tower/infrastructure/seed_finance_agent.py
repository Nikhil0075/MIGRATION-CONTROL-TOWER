#!/usr/bin/env python
"""Seeds the Finance Reporting Impact Agent — the §20.3 cross-department
discovery proof. Deliberately a SEPARATE script from
infrastructure/seed_registry.py: this agent is owned by a different
department (Finance Systems, not Technology) and published by a
different identity, with no migration tool rights — modeling a real
cross-department registry contribution, not something the platform team
authored for itself.

Usage (from repo root):
    python infrastructure/seed_finance_agent.py            # publish + approve
    python infrastructure/seed_finance_agent.py --deprecate # the negative proof
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from tools import registry  # noqa: E402

AGENT_ID = "finance-impact-agent"
VERSION = "1.0.0"

# Distinct from platform-eng-registry-publisher / platform-governance in
# infrastructure/seed_registry.py — a genuinely different department's
# identities, per §20.3's table ("Published by: A separate service
# account with registry publish rights but no migration tool rights").
PUBLISHED_BY = "finance-systems-registry-publisher@example.internal"
APPROVED_BY = "finance-systems-governance@example.internal"

CARD = {
    "agent_id": AGENT_ID,
    "display_name": "Finance Reporting Impact Agent",
    "version": VERSION,
    "owner": {
        "team": "Finance Systems",
        "department": "Finance Systems",
        "contact": "finance-systems@example.internal",
    },
    "capabilities": ["impact.assessment.finance_reporting"],
    "handler": "agents.finance.impact_agent:assess_impact",
    # Deploy & Harden Phase 1a: tools/registry.py::invoke_capability()'s
    # capability-dispatch gate looks this up in policies/agent_permissions.yaml.
    # Without it, evaluate() sees agent_key=None -> "no policy declared" ->
    # DENY, which would silently break the cross-department finance check
    # the moment universal capability-gate enforcement went live (see
    # docs/adr/0001-two-layer-policy-enforcement.md).
    "permissions_key": "finance",
    "runtime": {"type": "local", "service_account": "sa-finance-impact"},
    "model": {
        "provider": "vertex-ai",
        "name": "gemini-3.7-flash",
        "thinking_level": "high",
        "prompt_version": "1.0",
        "output_schema_version": "1.0",
    },
    # Informational snapshot only — tools/policy_engine.py reads
    # policies/agent_permissions.yaml's "finance" block at evaluation
    # time, not this field. Kept identical to that block so the registry
    # card doesn't display a permission set that isn't what's enforced.
    "permissions": {
        "allowed_tools": ["capability:impact.assessment.finance_reporting", "lineage.graph.read"],
        "denied_tools": ["source.raw_pii_read", "target.write", "production.write"],
        "data_classes": ["METADATA"],
    },
    "sla": {"p95_latency_ms": 30000, "max_retries": 2, "timeout_ms": 60000},
}


def main() -> None:
    if "--deprecate" in sys.argv:
        deprecated = registry.deprecate(AGENT_ID, VERSION, deprecated_by=APPROVED_BY)
        print(f"[finance-agent-seed] {AGENT_ID} v{VERSION} -> {deprecated['status']}")
        print(
            "[finance-agent-seed] negative proof ready: re-run the orchestrator "
            "and trigger_finance_impact_check should log 'no approved provider' "
            "and return None rather than crash or invent an answer."
        )
        return

    registry.publish(CARD, published_by=PUBLISHED_BY)
    approved = registry.approve(AGENT_ID, VERSION, approved_by=APPROVED_BY)
    print(f"[finance-agent-seed] {AGENT_ID} v{VERSION} -> {approved['status']}")
    print(f"[finance-agent-seed] owner={approved['owner']}, published_by={approved['published_by']}")


if __name__ == "__main__":
    main()
