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
MODEL = {
    "provider": "vertex-ai",
    "name": "gemini-3.7-flash",
    "thinking_level": "high",
    "prompt_version": "1.0",
    "output_schema_version": "1.0",
}

AGENTS = [
    {
        "agent_id": "discovery-agent",
        "display_name": "Discovery Agent",
        "capabilities": ["discovery.catalog.estate"],
        "handler": "agents.discovery.agent:discover_estate",
        "permissions_key": "discovery",
        "version": "2.0.0",
        "model_required": True,
    },
    {
        "agent_id": "lineage-agent",
        "display_name": "Lineage Agent",
        "capabilities": ["lineage.graph.build"],
        "handler": "agents.lineage.agent:build_dependency_graph",
        "permissions_key": "lineage",
        "version": "2.0.0",
        "model_required": True,
    },
    {
        "agent_id": "risk-agent",
        "display_name": "Risk & Compliance Agent",
        "capabilities": ["risk.assess.estate"],
        "handler": "agents.risk.agent:classify_estate",
        "permissions_key": "risk",
        "version": "1.1.0",
        "model_required": False,
    },
    {
        "agent_id": "planner-agent",
        "display_name": "Migration Planner",
        "capabilities": ["planner.plan.propose"],
        "handler": "agents.planner.agent:propose_plan",
        "permissions_key": "planner",
        "version": "2.0.0",
        "model_required": True,
    },
    {
        "agent_id": "validation-agent",
        "display_name": "Validation & Reconciliation Agent",
        "capabilities": ["validation.reconcile.source_target"],
        "handler": "agents.validation.agent:run_reconciliation",
        "permissions_key": "validation",
        "version": "1.1.0",
        "model_required": False,
    },
    {
        "agent_id": "cutover-agent",
        "display_name": "Cutover Agent",
        "capabilities": ["cutover.request_approval"],
        "handler": "agents.cutover.agent:request_approval",
        "permissions_key": "cutover",
        "version": "1.1.0",
        "model_required": False,
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
            "version": spec.get("version", "1.0.0"),
            "owner": OWNER,
            "capabilities": spec["capabilities"],
            "handler": spec["handler"],
            "runtime": {"type": "local", "service_account": f"sa-{spec['permissions_key']}"},
            "model_required": bool(spec.get("model_required")),
            "permissions": permissions,
            "sla": {"p95_latency_ms": 45000, "max_retries": 3, "timeout_ms": 120000},
        }
        # The AgentCard schema deliberately treats ``model`` as an object,
        # not a nullable placeholder. Deterministic agents therefore omit the
        # field entirely; this also keeps the console from implying that a
        # model participates in policy, validation, or cutover controls.
        if spec.get("model_required"):
            card["model"] = MODEL
        registry.publish(card, published_by=PUBLISHED_BY)
        version = spec.get("version", "1.0.0")
        approved = registry.approve(spec["agent_id"], version, approved_by=APPROVED_BY)
        print(f"[registry-seed] {spec['agent_id']} v{version} -> {approved['status']}")

    print(f"\n[registry-seed] {len(AGENTS)} agent cards published and approved.")


if __name__ == "__main__":
    main()
