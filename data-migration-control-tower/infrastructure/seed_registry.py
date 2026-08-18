#!/usr/bin/env python
"""Seeds the Agent Registry (Block C, master doc §20) with cards for the
six migration agents. Publisher and approver are deliberately distinct
identities — publish() alone leaves every card in DRAFT; approve()
would raise PermissionError if called with the same identity.

Permissions are generated from policies/agent_permissions.yaml (today's
single source of truth) rather than hand-duplicated, so the registry
card and the policy engine's actual enforcement can't silently drift
apart. See contracts/metadata_model.json's AgentCard.permissions
description for the migration path to making the registry authoritative.

Usage (from repo root, idempotent — re-running republishes+reapproves):
    python infrastructure/seed_registry.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from tools import registry  # noqa: E402

PUBLISHED_BY = "platform-eng-registry-publisher@example.internal"
APPROVED_BY = "platform-governance@example.internal"
OWNER = {"team": "Data Platform Engineering", "department": "Technology", "contact": "platform-eng@example.internal"}
MODEL = {"name": "gemini-3.5-flash", "temperature": 0.1}

AGENTS = [
    {
        "agent_id": "discovery-agent",
        "display_name": "Discovery Agent",
        "capabilities": ["discovery.catalog.estate"],
        "handler": "agents.discovery.agent:discover_estate",
        "permissions_key": "discovery",
    },
    {
        "agent_id": "lineage-agent",
        "display_name": "Lineage Agent",
        "capabilities": ["lineage.graph.build"],
        "handler": "agents.lineage.agent:build_dependency_graph",
        "permissions_key": "lineage",
    },
    {
        "agent_id": "risk-agent",
        "display_name": "Risk & Compliance Agent",
        "capabilities": ["risk.assess.estate"],
        "handler": "agents.risk.agent:classify_estate",
        "permissions_key": "risk",
    },
    {
        "agent_id": "planner-agent",
        "display_name": "Migration Planner",
        "capabilities": ["planner.plan.propose"],
        "handler": "agents.planner.agent:propose_plan",
        "permissions_key": "planner",
    },
    {
        "agent_id": "validation-agent",
        "display_name": "Validation & Reconciliation Agent",
        "capabilities": ["validation.reconcile.source_target"],
        "handler": "agents.validation.agent:run_reconciliation",
        "permissions_key": "validation",
    },
    {
        "agent_id": "cutover-agent",
        "display_name": "Cutover Agent",
        "capabilities": ["cutover.request_approval"],
        "handler": "agents.cutover.agent:request_approval",
        "permissions_key": "cutover",
    },
]


def _load_permissions(permissions_key: str) -> dict:
    with open(REPO_ROOT / "policies" / "agent_permissions.yaml", encoding="utf-8") as f:
        all_permissions = yaml.safe_load(f)
    return all_permissions.get(permissions_key, {})


def main() -> None:
    for spec in AGENTS:
        permissions = _load_permissions(spec["permissions_key"])
        card = {
            "agent_id": spec["agent_id"],
            "display_name": spec["display_name"],
            "version": "1.0.0",
            "owner": OWNER,
            "capabilities": spec["capabilities"],
            "handler": spec["handler"],
            "runtime": {"type": "local", "service_account": f"sa-{spec['permissions_key']}"},
            "model": MODEL,
            "permissions": permissions,
            "sla": {"p95_latency_ms": 45000, "max_retries": 3, "timeout_ms": 120000},
        }
        registry.publish(card, published_by=PUBLISHED_BY)
        approved = registry.approve(spec["agent_id"], "1.0.0", approved_by=APPROVED_BY)
        print(f"[registry-seed] {spec['agent_id']} v1.0.0 -> {approved['status']}")

    print(f"\n[registry-seed] {len(AGENTS)} agent cards published and approved.")


if __name__ == "__main__":
    main()
